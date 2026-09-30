"""Control-layer scoring of one finished case-run (Stage 4 Eval Completion).

    (case labels, CaseRunRecord, EvidenceState) -> CaseScore

The only module of the eval pipeline that reads a case's expected_* labels, and
it runs strictly after the control run and the evidence enrichment are over:
nothing here can reach a ControlPolicy or change what was derived. Inputs are
read-only; a score never mutates the record, the evidence state or the case.

What is scored (schema v2-control-score/1)
    capabilities    every called tool name (ToolObservation or
                    ToolContractFailure), plus "derived_facts" when any
                    derivation was attempted. Required must all appear,
                    forbidden none; call order is not scored; extra
                    capabilities are an efficiency diagnostic only.
    clarification   Clarify decisions against expected_answerability.clarify.
    evidence        the frozen expected_evidence matcher over
                    EvidenceState.evidence_items (tool + derived evidence).
    final           termination "finished" and the policy's disposition equal
                    to expected_answerability.final.
    database        unchanged content, checked again from the record.

`control_success` is the conjunction of those five. It is deliberately not
called task or end-to-end success: there is no answer generation or citation
yet. For the same reason forbidden evidence is only reported as *present*
(`forbidden_evidence_present`) - having retrieved it is not having relied on
it, and it does not fail the case here; generation/citation scoring decides
"used" later.

Expected-evidence matching (structured fields only, never content)
    business  subject.entity/id == metadata entity/record_id, field ==
              metadata field, optional value == metadata value
    policy    subject.id == metadata policy_id, optional subject.version ==
              Evidence.version, field == metadata field, value == metadata value
    derived   field == fact_key, value == DerivedEvidence.value; an id matches
              the record key after the first ':' of the subject exactly
    all       source_types contains source_type.value; optional tool ==
              EvidenceItem.producer; optional locator == Evidence.locator
    values    compared as canonical JSON, type-sensitive: True != 1
    all_of    every requirement has a match
    any_of    every group has at least one matching requirement (AND of ORs)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Sequence

from orchestration.contracts import (
    BusinessEvidence,
    DerivedEvidence,
    SourceType,
    canonical_json_value,
)

from .control import canonical_json, clarification_slots
from .evidence import DERIVED_PRODUCER, EvidenceItem, EvidenceState
from .runner import TERMINATION_FINISHED, CaseRunRecord, control_run_sha256
from .runtime import case_contract, validate_case

CONTROL_SCORE_SCHEMA = "v2-control-score/1"

BUSINESS_ENTITIES = frozenset({"order", "order_item", "logistics", "inventory",
                               "after_sales_case"})
POLICY_ENTITY = "policy"
DERIVED_ENTITY = "derived"


class EvalScoringError(ValueError):
    """The score inputs do not belong together (another case, another run)."""


# --------------------------------------------------------------------------
# JSON value comparison
# --------------------------------------------------------------------------

_NO_MATCH = object()


def json_value_key(value: object) -> object:
    """A type-sensitive canonical form: equal iff equal as JSON values.

    Built on `canonical_json_value`, so True and 1 stay different. A float
    (allowed in a case, never in evidence) gets a key no evidence value has.
    """
    try:
        return canonical_json(canonical_json_value("value", value))
    except ValueError:
        return _NO_MATCH


def json_values_equal(left: object, right: object) -> bool:
    key = json_value_key(left)
    return key is not _NO_MATCH and key == json_value_key(right)


# --------------------------------------------------------------------------
# The expected-evidence matcher
# --------------------------------------------------------------------------


def _derived_record_key(subject: str) -> str:
    return subject.partition(":")[2]


def evidence_matches(spec: Mapping, item: EvidenceItem) -> bool:
    """Whether one collected evidence item satisfies one frozen requirement.

    Absent optional keys do not constrain. `field` is optional only in a
    forbidden entry, where the frozen schema makes it so.
    """
    evidence = item.evidence
    subject = spec["subject"]
    entity = subject["entity"]
    metadata = evidence.metadata if isinstance(evidence.metadata, Mapping) else {}
    if "source_types" in spec and evidence.source_type.value not in spec["source_types"]:
        return False
    if "tool" in spec and item.producer != spec["tool"]:
        return False
    if "locator" in spec and evidence.locator != spec["locator"]:
        return False

    if entity == DERIVED_ENTITY:
        if not isinstance(evidence, DerivedEvidence) or item.producer != DERIVED_PRODUCER:
            return False
        if "id" in subject and _derived_record_key(evidence.subject) != subject["id"]:
            return False
        if "field" in spec and evidence.fact_key != spec["field"]:
            return False
        return "value" not in spec or json_values_equal(spec["value"], evidence.value)

    if isinstance(evidence, DerivedEvidence):
        return False
    if entity == POLICY_ENTITY:
        if evidence.source_type not in (SourceType.DOCUMENT, SourceType.WIKI):
            return False
        if "policy_id" not in metadata or metadata["policy_id"] != subject["id"]:
            return False
        if "version" in subject and evidence.version != subject["version"]:
            return False
    elif entity in BUSINESS_ENTITIES:
        if not isinstance(evidence, BusinessEvidence):
            return False
        if metadata.get("entity") != entity or metadata.get("record_id") != subject["id"]:
            return False
    else:
        raise EvalScoringError("unknown evidence subject entity")
    if "field" in spec and metadata.get("field") != spec["field"]:
        return False
    return "value" not in spec or (
        "value" in metadata and json_values_equal(spec["value"], metadata["value"]))


def matching_refs(spec: Mapping, items: Sequence[EvidenceItem]) -> tuple[str, ...]:
    return tuple(item.ref for item in items if evidence_matches(spec, item))


@dataclass(frozen=True, kw_only=True)
class EvidenceScore:
    all_of_total: int
    all_of_matched: int
    all_of_refs: tuple[tuple[str, ...], ...]
    missing_all_of: tuple[int, ...]
    any_of_groups_total: int
    any_of_groups_matched: int
    missing_any_of_groups: tuple[int, ...]
    forbidden_evidence_present: bool
    forbidden_refs: tuple[str, ...]
    evidence_ok: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "all_of_total": self.all_of_total,
            "all_of_matched": self.all_of_matched,
            "all_of_refs": [list(refs) for refs in self.all_of_refs],
            "missing_all_of": list(self.missing_all_of),
            "any_of_groups_total": self.any_of_groups_total,
            "any_of_groups_matched": self.any_of_groups_matched,
            "missing_any_of_groups": list(self.missing_any_of_groups),
            "forbidden_evidence_present": self.forbidden_evidence_present,
            "forbidden_refs": list(self.forbidden_refs),
            "evidence_ok": self.evidence_ok,
        }


def score_evidence(expected: Mapping, items: Sequence[EvidenceItem]) -> EvidenceScore:
    all_of_refs = tuple(matching_refs(spec, items) for spec in expected["all_of"])
    missing_all_of = tuple(index for index, refs in enumerate(all_of_refs) if not refs)
    missing_groups = tuple(
        index for index, group in enumerate(expected["any_of"])
        if not any(matching_refs(spec, items) for spec in group))
    forbidden: list[str] = []
    for spec in expected["forbidden"]:
        for ref in matching_refs(spec, items):
            if ref not in forbidden:
                forbidden.append(ref)
    return EvidenceScore(
        all_of_total=len(all_of_refs),
        all_of_matched=len(all_of_refs) - len(missing_all_of),
        all_of_refs=all_of_refs,
        missing_all_of=missing_all_of,
        any_of_groups_total=len(expected["any_of"]),
        any_of_groups_matched=len(expected["any_of"]) - len(missing_groups),
        missing_any_of_groups=missing_groups,
        forbidden_evidence_present=bool(forbidden),
        forbidden_refs=tuple(sorted(forbidden)),
        # Presence of forbidden evidence is reported, never folded in here.
        evidence_ok=not missing_all_of and not missing_groups,
    )


# --------------------------------------------------------------------------
# Clarification
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ClarificationScore:
    clarify_required: bool
    clarify_attempted: bool
    clarify_matched: bool
    expected_slots: tuple[str, ...]
    requested_slots: tuple[str, ...]
    missing_slots: tuple[str, ...]
    extra_slots: tuple[str, ...]
    over_ask: bool
    unanswered_clarification: bool
    clarification_ok: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "clarify_required": self.clarify_required,
            "clarify_attempted": self.clarify_attempted,
            "clarify_matched": self.clarify_matched,
            "expected_slots": list(self.expected_slots),
            "requested_slots": list(self.requested_slots),
            "missing_slots": list(self.missing_slots),
            "extra_slots": list(self.extra_slots),
            "over_ask": self.over_ask,
            "unanswered_clarification": self.unanswered_clarification,
            "clarification_ok": self.clarification_ok,
        }


def _in_slot_order(slots) -> tuple[str, ...]:
    return tuple(slot for slot in clarification_slots() if slot in slots)


def score_clarification(expected: Mapping, record: CaseRunRecord) -> ClarificationScore:
    required = expected["required"] is True
    wanted = set(expected["slots"])
    attempted = bool(record.clarifications)
    requested = {slot for item in record.clarifications for slot in item.requested_slots}
    matched = any(item.matched_turn_index is not None for item in record.clarifications)
    unanswered = any(item.matched_turn_index is None for item in record.clarifications)
    missing = wanted - requested
    extra = requested - wanted
    over_ask = attempted and (not required or bool(extra))
    if required:
        ok = matched and not missing and not extra and not unanswered
    else:
        ok = not attempted
    return ClarificationScore(
        clarify_required=required,
        clarify_attempted=attempted,
        clarify_matched=matched,
        expected_slots=_in_slot_order(wanted),
        requested_slots=_in_slot_order(requested),
        missing_slots=_in_slot_order(missing),
        extra_slots=_in_slot_order(extra),
        over_ask=over_ask,
        unanswered_clarification=unanswered,
        clarification_ok=ok,
    )


# --------------------------------------------------------------------------
# The case score
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def capability_vocabulary() -> tuple[str, ...]:
    """The frozen capability enum of the case schema, in schema order."""
    schema = case_contract().load_json(case_contract().CASE_SCHEMA_PATH)
    return tuple(schema["$defs"]["capability"]["enum"])


def actual_capabilities(record: CaseRunRecord, state: EvidenceState) -> tuple[str, ...]:
    used = {observation.tool_name for observation in record.observations}
    if state.derivation_records:
        used.add(DERIVED_PRODUCER)
    vocabulary = capability_vocabulary()
    unknown = used - set(vocabulary)
    if unknown:
        raise EvalScoringError("run used a capability outside the frozen vocabulary")
    return tuple(name for name in vocabulary if name in used)


@dataclass(frozen=True, kw_only=True)
class CaseScore:
    schema: str
    case_id: str
    archetype: str
    termination: str
    control_steps: int
    tool_calls: int
    actual_capabilities: tuple[str, ...]
    missing_required_capabilities: tuple[str, ...]
    used_forbidden_capabilities: tuple[str, ...]
    extra_capabilities: tuple[str, ...]
    capabilities_ok: bool
    clarification: ClarificationScore
    evidence: EvidenceScore
    expected_final: str
    actual_final: str | None
    final_ok: bool
    db_ok: bool
    control_success: bool

    @property
    def clarification_ok(self) -> bool:
        return self.clarification.clarification_ok

    @property
    def evidence_ok(self) -> bool:
        return self.evidence.evidence_ok

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "archetype": self.archetype,
            "termination": self.termination,
            "control_steps": self.control_steps,
            "tool_calls": self.tool_calls,
            "actual_capabilities": list(self.actual_capabilities),
            "missing_required_capabilities": list(self.missing_required_capabilities),
            "used_forbidden_capabilities": list(self.used_forbidden_capabilities),
            "extra_capabilities": list(self.extra_capabilities),
            "capabilities_ok": self.capabilities_ok,
            "clarification": self.clarification.to_dict(),
            "evidence": self.evidence.to_dict(),
            "expected_final": self.expected_final,
            "actual_final": self.actual_final,
            "final_ok": self.final_ok,
            "db_ok": self.db_ok,
            "control_success": self.control_success,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def score_case(case: object, record: CaseRunRecord, state: EvidenceState) -> CaseScore:
    """Score one finished run against its case labels. Reads, never writes."""
    if type(record) is not CaseRunRecord:
        raise ValueError("record must be a CaseRunRecord, got " + type(record).__name__)
    if not isinstance(state, EvidenceState):
        raise ValueError("state must be an EvidenceState, got " + type(state).__name__)
    # A private copy of the labels: the caller's case is never touched.
    case = json.loads(canonical_json(case))
    validate_case(case)
    if record.case_id != case["case_id"]:
        raise EvalScoringError("record belongs to another case")
    if not state.unchanged() or state.control_run_sha256 != control_run_sha256(record):
        raise EvalScoringError("evidence state was not derived from this record")

    capabilities = case["expected_capabilities"]
    actual = actual_capabilities(record, state)
    missing = tuple(name for name in capabilities["required"] if name not in actual)
    forbidden = tuple(name for name in actual if name in capabilities["forbidden"])
    extra = tuple(name for name in actual if name not in capabilities["required"]
                  and name not in capabilities["forbidden"])
    capabilities_ok = not missing and not forbidden

    answerability = case["expected_answerability"]
    clarification = score_clarification(answerability["clarify"], record)
    evidence = score_evidence(case["expected_evidence"], state.evidence_items)
    final_ok = (record.termination == TERMINATION_FINISHED
                and record.final_disposition == answerability["final"])
    db_ok = (record.database_unchanged is True
             and record.initial_db_sha256 == record.final_db_sha256)
    return CaseScore(
        schema=CONTROL_SCORE_SCHEMA,
        case_id=case["case_id"],
        archetype=case["archetype"],
        termination=record.termination,
        control_steps=record.control_steps,
        tool_calls=len(record.observations),
        actual_capabilities=actual,
        missing_required_capabilities=missing,
        used_forbidden_capabilities=forbidden,
        extra_capabilities=extra,
        capabilities_ok=capabilities_ok,
        clarification=clarification,
        evidence=evidence,
        expected_final=answerability["final"],
        actual_final=record.final_disposition,
        final_ok=final_ok,
        db_ok=db_ok,
        control_success=(capabilities_ok and clarification.clarification_ok
                         and evidence.evidence_ok and final_ok and db_ok),
    )
