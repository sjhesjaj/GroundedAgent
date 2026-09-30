"""Evidence enrichment of a finished control run (Stage 4 Eval Completion).

    CaseRunRecord (or a live ControlState)
    -> collected tool results and contract failures, exactly as observed
    -> observed policy evidence -> validated PolicyRecords
    -> deterministic derived facts (aftersales.derived, via the eval runtime gates)
    -> EvidenceState

This layer is label-free. Its only inputs are the observations of a run and the
run's virtual instant; it never receives the eval case, so no case label can
decide which facts get derived. The same layer serves the Stage 4 Baseline and
the Stage 5 tool loop (`derive_from_control_state`) as well as the scorer.

What it never does
    It opens no database, calls no tool, gateway or registry handler, reads no
    system clock and parses no prose. Business time is a FixedClock at the
    run's virtual_now. Everything it derives comes from the observations.

Policy provenance
    A window rule is used only if a search_after_sales_policy observation
    returned it. The published catalog is used to recover the structured
    PolicyRecord of an observed policy_ref - never to add a rule nobody
    observed - and every observed ref must pass `validate_policy_refs` against
    that observation's own evidence first. A ref the catalog does not know, or
    evidence that does not carry every structured field, raises
    EvalEvidenceError: nothing is skipped silently.

Derivation families, in this fixed order
    inventory_available       every inventory#available_qty observed
    business_state_conflict   get_order(order_id=X) x get_logistics(order_id=X),
                              paired by the call arguments, whole logistics result
    days_since_delivery       every package of every complete get_logistics
                              result x every (counting_rule, utc_offset) that an
                              observed window rule declares
    item_window_eligibility   every order_item#category of get_order(order_id=X)
                              x the complete get_logistics(order_id=X) result x
                              every observed window rule whose own evidence
                              lists that category in selected_categories

NotDerivable is a diagnostic: it becomes a `not_derivable` DerivationRecord
with its stable code and the other derivations go on. ValueError (and the eval
runtime's IncompleteLogisticsObservation) still propagates: malformed input is a
data-integrity or programming fault. A DerivationRecord holds structured
subjects, fact keys, codes, observation ids and policy refs only - never user
text, an exception message, SQL, or an identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from aftersales.business_tools import GET_LOGISTICS, GET_ORDER
from aftersales.clock import FixedClock
from aftersales.derived import (
    FACT_BUSINESS_STATE_CONFLICT,
    FACT_DAYS_SINCE_DELIVERY,
    FACT_INVENTORY_AVAILABLE,
    WINDOW_FACTS,
    NotDerivable,
    derive_business_state_conflict,
    derive_days_since_delivery,
    derive_inventory_available,
)
from aftersales.policy import (
    POLICY_TOOL_NAME,
    WINDOW_RULE_TYPES,
    PolicyRecord,
    policy_ref,
    window_params,
)
from aftersales.policy_catalog import PublishedPolicyCatalog, validate_policy_refs
from orchestration.contracts import (
    BusinessEvidence,
    DerivedEvidence,
    Evidence,
    SourceType,
    ToolResult,
    ToolStatus,
    evidence_ref,
)
from orchestration.evidence_policy_v2 import (
    EvidenceDecision,
    EvidenceRequirement,
    FreshnessRequirement,
    evaluate_evidence_v2,
)

from .control import ControlState, ToolContractFailure, ToolObservation, canonical_json
from .runner import CaseRunRecord, control_run_sha256
from .runtime import complete_delivered_at_evidence, derive_item_window_from_logistics_result

EVIDENCE_STATE_SCHEMA = "v2-evidence-state/1"

# The producer of every derived fact (an EvidenceItem's producer; also the
# capability name of deterministic derivation in the frozen vocabulary).
DERIVED_PRODUCER = "derived_facts"

FAMILY_INVENTORY_AVAILABLE = "inventory_available"
FAMILY_BUSINESS_STATE_CONFLICT = "business_state_conflict"
FAMILY_DAYS_SINCE_DELIVERY = "days_since_delivery"
FAMILY_ITEM_WINDOW = "item_window_eligibility"
DERIVATION_FAMILIES = (
    FAMILY_INVENTORY_AVAILABLE,
    FAMILY_BUSINESS_STATE_CONFLICT,
    FAMILY_DAYS_SINCE_DELIVERY,
    FAMILY_ITEM_WINDOW,
)

STATUS_PRODUCED = "produced"
STATUS_NOT_DERIVABLE = "not_derivable"


class EvalEvidenceError(ValueError):
    """Observed evidence cannot be trusted for derivation (provenance, integrity)."""


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EvidenceItem:
    """One collected evidence item and the capability that produced it.

    `producer` is the observing tool's name for tool evidence and
    "derived_facts" for derived facts. `ref` is always `evidence_ref()`.
    """

    ref: str
    producer: str
    evidence: Evidence

    def to_dict(self) -> dict[str, object]:
        return {"ref": self.ref, "producer": self.producer}


@dataclass(frozen=True, kw_only=True)
class DerivationRecord:
    """One attempted derivation: what was tried on which inputs, and the result.

    `code` is the stable NotDerivable code (None when produced);
    `evidence_ref` is the produced fact's ref (None when not derivable).
    """

    family: str
    fact_key: str
    subject: str
    status: str
    code: str | None
    input_observation_ids: tuple[str, ...]
    policy_refs: tuple[str, ...]
    evidence_ref: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "fact_key": self.fact_key,
            "subject": self.subject,
            "status": self.status,
            "code": self.code,
            "input_observation_ids": list(self.input_observation_ids),
            "policy_refs": list(self.policy_refs),
            "evidence_ref": self.evidence_ref,
        }

    def sort_key(self) -> tuple:
        return (DERIVATION_FAMILIES.index(self.family), self.subject, self.policy_refs,
                self.input_observation_ids, self.fact_key)


@dataclass(frozen=True, kw_only=True)
class EvidenceState:
    """Everything the run collected, plus the facts derived from it.

    `control_run_sha256` names the raw record it was derived from; it is None
    only when derived from a live ControlState (no record exists yet).
    `tool_results` are private copies of every observed ToolResult (ok, empty
    or error); `contract_failures` are the malformed calls, which have no
    ToolResult and never get one. The serialized form is captured on
    construction, so later mutation of a contained object is detectable.
    """

    schema: str
    control_run_sha256: str | None
    virtual_now: str
    tool_results: tuple[ToolResult, ...]
    contract_failures: tuple[ToolContractFailure, ...]
    evidence_items: tuple[EvidenceItem, ...]
    derived_evidence: tuple[DerivedEvidence, ...]
    derivation_records: tuple[DerivationRecord, ...]
    _canonical: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_canonical", canonical_json(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "control_run_sha256": self.control_run_sha256,
            "virtual_now": self.virtual_now,
            "tool_results": [result.to_dict() for result in self.tool_results],
            "contract_failures": [failure.to_dict() for failure in self.contract_failures],
            "evidence_items": [item.to_dict() for item in self.evidence_items],
            "derived_evidence": [
                {"ref": evidence_ref(item), "evidence": item.to_dict()}
                for item in self.derived_evidence
            ],
            "derivation_records": [record.to_dict() for record in self.derivation_records],
        }

    def unchanged(self) -> bool:
        return canonical_json(self._payload()) == self._canonical

    def to_dict(self) -> dict[str, object]:
        return json.loads(self._canonical)

    def canonical_json(self) -> str:
        return self._canonical

    def sha256(self) -> str:
        return hashlib.sha256(self._canonical.encode("utf-8")).hexdigest()

    def as_of(self) -> datetime:
        return datetime.fromisoformat(self.virtual_now)


def evidence_state_sha256(state: EvidenceState) -> str:
    if not isinstance(state, EvidenceState):
        raise ValueError("state must be an EvidenceState, got " + type(state).__name__)
    if not state.unchanged():
        raise EvalEvidenceError("evidence state was modified after it was built")
    return state.sha256()


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def derive_evidence_state(record: CaseRunRecord) -> EvidenceState:
    """Enrich one raw control run. The record is the only input."""
    if type(record) is not CaseRunRecord:
        raise ValueError("record must be a CaseRunRecord, got " + type(record).__name__)
    return _enrich(record.virtual_now, record.observations,
                   control_run_sha256=control_run_sha256(record))


def derive_from_control_state(state: ControlState) -> EvidenceState:
    """The same enrichment from a live ControlState (Baseline / tool loop).

    Reads `state.virtual_now` and `state.observations` and nothing else.
    """
    if type(state) is not ControlState:
        raise ValueError("state must be a ControlState, got " + type(state).__name__)
    return _enrich(state.virtual_now, state.observations, control_run_sha256=None)


def evaluate_evidence_state(
    state: EvidenceState, *, requirements: tuple[EvidenceRequirement, ...]
) -> EvidenceDecision:
    """The production V2 Evidence Policy over an EvidenceState, unchanged.

    Contract failures have no ToolResult and are not passed in as results.
    """
    if not isinstance(state, EvidenceState):
        raise ValueError("state must be an EvidenceState, got " + type(state).__name__)
    if not isinstance(requirements, tuple) or not all(
            isinstance(item, EvidenceRequirement) for item in requirements):
        raise ValueError("requirements must be a tuple of EvidenceRequirement")
    return evaluate_evidence_v2(
        state.tool_results,
        requirements=requirements,
        freshness=FreshnessRequirement(as_of=state.as_of()),
        derived=state.derived_evidence,
    )


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seen:
    """One observed ToolResult (a private copy) with its call facts."""

    observation_id: str
    tool_name: str
    arguments: Mapping[str, str]
    result: ToolResult


def _collect(observations: object) -> tuple[list[_Seen], list[ToolContractFailure]]:
    if not isinstance(observations, tuple):
        raise ValueError("observations must be a tuple")
    seen: list[_Seen] = []
    failures: list[ToolContractFailure] = []
    ids: set[str] = set()
    for index, observation in enumerate(observations):
        path = "observations[" + str(index) + "]"
        if type(observation) is ToolContractFailure:
            failures.append(observation)
            observation_id = observation.observation_id
        elif type(observation) is ToolObservation:
            if not observation.result_unchanged():
                raise EvalEvidenceError(path + " result was modified after it was observed")
            result = copy.deepcopy(observation.result)
            if result.tool_name != observation.tool_name:
                raise EvalEvidenceError(path + " result belongs to another tool")
            observation_id = observation.observation_id
            seen.append(_Seen(observation_id, observation.tool_name,
                              dict(observation.arguments), result))
        else:
            raise ValueError(path + " must be a ToolObservation or ToolContractFailure")
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise ValueError(path + " has no observation_id")
        if observation_id in ids:
            raise EvalEvidenceError(path + " repeats an observation_id")
        ids.add(observation_id)
    return seen, failures


def _enrich(virtual_now: object, observations: object, *,
            control_run_sha256: str | None) -> EvidenceState:
    if not isinstance(virtual_now, str):
        raise ValueError("virtual_now must be an ISO-8601 string")
    clock = FixedClock(datetime.fromisoformat(virtual_now))
    seen, failures = _collect(observations)
    policies = _observed_policies(seen)
    records: list[DerivationRecord] = []
    derived: dict[str, DerivedEvidence] = {}

    def attempt(family: str, fact_key: str, subject: str, observation_ids: tuple[str, ...],
                policy_refs: tuple[str, ...], derive) -> None:
        try:
            fact = derive()
        except NotDerivable as reason:
            records.append(DerivationRecord(
                family=family, fact_key=fact_key, subject=subject, status=STATUS_NOT_DERIVABLE,
                code=reason.code, input_observation_ids=observation_ids,
                policy_refs=policy_refs, evidence_ref=None))
            return
        if (fact.fact_key != fact_key or fact.subject != subject
                or not set(fact.policy_refs) <= set(policy_refs)):
            raise EvalEvidenceError("derived fact disagrees with its derivation record")
        ref = evidence_ref(fact)
        derived.setdefault(ref, fact)
        records.append(DerivationRecord(
            family=family, fact_key=fact_key, subject=subject, status=STATUS_PRODUCED,
            code=None, input_observation_ids=observation_ids,
            policy_refs=policy_refs, evidence_ref=ref))

    ok = [item for item in seen if item.result.status is ToolStatus.OK]
    orders = [item for item in ok if item.tool_name == GET_ORDER]
    # ERROR is not an observation of the logistics; EMPTY is (no package).
    logistics = [item for item in seen if item.tool_name == GET_LOGISTICS
                 and item.result.status in (ToolStatus.OK, ToolStatus.EMPTY)]
    deliveries = {item.observation_id: complete_delivered_at_evidence(item.result)
                  for item in logistics}

    # A. inventory_available
    for item in ok:
        for evidence in item.result.evidence:
            if _is_field(evidence, "inventory", "available_qty"):
                attempt(FAMILY_INVENTORY_AVAILABLE, FACT_INVENTORY_AVAILABLE,
                        "inventory:" + evidence.metadata["record_id"],
                        (item.observation_id,), (),
                        lambda evidence=evidence: derive_inventory_available(
                            evidence, clock=clock))

    # B. business_state_conflict: same order_id by call arguments, never content.
    for order in orders:
        order_id = _order_argument(order)
        status = _order_status(order, order_id)
        for shipment in logistics:
            if _order_argument(shipment) != order_id:
                continue
            attempt(FAMILY_BUSINESS_STATE_CONFLICT, FACT_BUSINESS_STATE_CONFLICT,
                    "order:" + order_id, (order.observation_id, shipment.observation_id), (),
                    lambda shipment=shipment: derive_business_state_conflict(
                        status, shipment.result.evidence, clock=clock))

    # C. days_since_delivery, under every counting rule an observed window rule declares.
    combos: dict[tuple[str, str], list[str]] = {}
    for record in policies.values():
        params = window_params(record)
        combos.setdefault((params.counting_rule.value, params.utc_offset), []).append(
            policy_ref(record))
    for shipment in logistics:
        for delivered_at in deliveries[shipment.observation_id]:
            for (counting_rule, utc_offset), refs in sorted(combos.items()):
                params = window_params(policies[refs[0]])
                attempt(FAMILY_DAYS_SINCE_DELIVERY, FACT_DAYS_SINCE_DELIVERY,
                        "logistics:" + delivered_at.metadata["record_id"],
                        (shipment.observation_id,), tuple(sorted(refs)),
                        lambda delivered_at=delivered_at, params=params:
                        derive_days_since_delivery(
                            delivered_at, clock=clock, counting_rule=params.counting_rule,
                            utc_offset=params.utc_offset))

    # D. item-level window eligibility: the whole logistics result, one gate.
    selected = _selected_categories(seen, policies)
    for order in orders:
        order_id = _order_argument(order)
        for category in order.result.evidence:
            if not _is_field(category, "order_item", "category"):
                continue
            value = category.metadata["value"]
            if not isinstance(value, str) or not value.strip():
                raise EvalEvidenceError("order_item category must be a non-empty string")
            refs = sorted(ref for ref, categories in selected.items() if value in categories)
            for shipment in logistics:
                if _order_argument(shipment) != order_id:
                    continue
                for ref in refs:
                    record = policies[ref]
                    attempt(FAMILY_ITEM_WINDOW, WINDOW_FACTS[record.rule_type],
                            "order_item:" + category.metadata["record_id"],
                            (order.observation_id, shipment.observation_id), (ref,),
                            lambda category=category, shipment=shipment, record=record:
                            derive_item_window_from_logistics_result(
                                shipment.result, record, clock=clock, category=category))

    derived_sorted = tuple(sorted(derived.values(), key=lambda fact: (
        fact.fact_key, fact.subject, evidence_ref(fact))))
    items: list[EvidenceItem] = []
    refs_seen: set[str] = set()
    for observation in seen:
        for evidence in observation.result.evidence:
            _add_item(items, refs_seen, evidence, observation.tool_name)
    for fact in derived_sorted:
        _add_item(items, refs_seen, fact, DERIVED_PRODUCER)
    return EvidenceState(
        schema=EVIDENCE_STATE_SCHEMA,
        control_run_sha256=control_run_sha256,
        virtual_now=virtual_now,
        tool_results=tuple(observation.result for observation in seen),
        contract_failures=tuple(failures),
        evidence_items=tuple(items),
        derived_evidence=derived_sorted,
        derivation_records=tuple(sorted(records, key=DerivationRecord.sort_key)),
    )


def _add_item(items: list[EvidenceItem], refs: set[str], evidence: Evidence,
              producer: str) -> None:
    ref = evidence_ref(evidence)
    if ref not in refs:  # stable: the first occurrence wins
        refs.add(ref)
        items.append(EvidenceItem(ref=ref, producer=producer, evidence=evidence))


# --------------------------------------------------------------------------
# Structured reads
# --------------------------------------------------------------------------


def _is_field(evidence: Evidence, entity: str, field_name: str) -> bool:
    metadata = evidence.metadata
    return (isinstance(evidence, BusinessEvidence) and isinstance(metadata, Mapping)
            and metadata.get("entity") == entity and metadata.get("field") == field_name)


def _order_argument(item: _Seen) -> str:
    order_id = item.arguments.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        raise EvalEvidenceError(item.tool_name + " observation has no order_id argument")
    return order_id


def _order_status(order: _Seen, order_id: str) -> BusinessEvidence:
    statuses = [evidence for evidence in order.result.evidence
                if _is_field(evidence, "order", "status")]
    if len(statuses) != 1 or statuses[0].metadata.get("record_id") != order_id:
        raise EvalEvidenceError("get_order observation needs exactly one order#status "
                                "of the requested order")
    return statuses[0]


# --------------------------------------------------------------------------
# Observed policies
# --------------------------------------------------------------------------


def _policy_evidence(item: _Seen) -> tuple[Evidence, ...]:
    if item.tool_name != POLICY_TOOL_NAME or item.result.status is not ToolStatus.OK:
        return ()
    return item.result.evidence


def _observed_policies(seen: list[_Seen]) -> dict[str, PolicyRecord]:
    """Observed window rules by ref, after provenance validation of EVERY observed ref."""
    observed = [(item, _policy_evidence(item)) for item in seen]
    observed = [(item, evidence) for item, evidence in observed if evidence]
    if not observed:
        return {}
    snapshot = PublishedPolicyCatalog().snapshot()
    records: dict[str, PolicyRecord] = {}
    for item, evidence in observed:
        refs = []
        for entry in evidence:
            ref = entry.metadata.get("policy_ref") if isinstance(entry.metadata, Mapping) else None
            if not isinstance(ref, str) or not ref.strip():
                raise EvalEvidenceError("observed policy evidence has no policy_ref")
            if entry.source_type not in (SourceType.DOCUMENT, SourceType.WIKI):
                raise EvalEvidenceError("observed policy evidence is not document evidence")
            if ref not in refs:
                refs.append(ref)
        try:
            for ref in refs:
                records[ref] = snapshot.lookup(ref)
            # Per observation: its own evidence must carry every structured field.
            validate_policy_refs(tuple(refs), snapshot=snapshot, evidence=evidence)
        except ValueError:
            raise EvalEvidenceError(
                "observed policy_ref failed provenance validation against the "
                "published catalog") from None
    return {ref: record for ref, record in records.items()
            if record.rule_type in WINDOW_RULE_TYPES}


def _selected_categories(seen: list[_Seen],
                         policies: dict[str, PolicyRecord]) -> dict[str, frozenset[str]]:
    """Window-rule ref -> item categories its own observed evidence selected it for.

    The catalog's precedence already chose the rule per category; this only
    reads that choice back from metadata["selected_categories"]. Missing or
    self-contradictory metadata raises.
    """
    selected: dict[str, set[str]] = {ref: set() for ref in policies}
    for item in seen:
        per_ref: dict[str, str] = {}
        for entry in _policy_evidence(item):
            ref = entry.metadata["policy_ref"]
            if ref not in policies:
                continue
            categories = entry.metadata.get("selected_categories")
            if not isinstance(categories, list) or not categories or not all(
                    category is None or (isinstance(category, str) and category.strip())
                    for category in categories) or len(set(categories)) != len(categories):
                raise EvalEvidenceError("observed policy evidence has malformed "
                                        "selected_categories")
            rendered = canonical_json(sorted(categories, key=lambda c: (c is not None, c or "")))
            if per_ref.setdefault(ref, rendered) != rendered:
                raise EvalEvidenceError("observed policy evidence disagrees about "
                                        "selected_categories")
            record = policies[ref]
            for category in categories:
                if category is None:
                    continue
                if record.scope and category not in record.scope:
                    raise EvalEvidenceError("selected category lies outside the rule's scope")
                selected[ref].add(category)
    # One (category, rule type) must never resolve to disagreeing rules.
    chosen: dict[tuple[str, object], tuple[int, str]] = {}
    for ref in sorted(selected):
        record = policies[ref]
        signature = (record.priority, canonical_json(dict(record.params)))
        for category in selected[ref]:
            if chosen.setdefault((category, record.rule_type), signature) != signature:
                raise EvalEvidenceError("observed policies select disagreeing rules for "
                                        "one category")
    return {ref: frozenset(categories) for ref, categories in selected.items()}
