"""V2 Evidence Policy: is the collected evidence enough, and what may be used?

A separate entrypoint beside the V1 `evidence_policy` module, which stays as it
is (V1 `Plan` / `ToolName`, three channels). This module takes no plan and
knows no planner. Its inputs are:

- the collected `ToolResult`s (any tools, any number of calls);
- the derived facts computed from them (`DerivedEvidence`);
- explicit `EvidenceRequirement`s - which facts the conclusion needs;
- a `FreshnessRequirement` - the business instant and, optionally, an SLA.

It returns a structured `EvidenceDecision` - usable evidence, derived evidence,
per-requirement outcomes, conflicts, and stable reason codes - and nothing
else: no answer text, no citation numbers, no refusal wording. It is pure: no
clock, database, network, or model. Integration mistakes raise `ValueError`;
a conclusion that the evidence cannot support returns BLOCKED.

Rules this layer enforces:

1. **missing / empty / error stay distinct.** A requirement no evidence meets
   is explained by its provider tools: ERROR -> `tool_error`, EMPTY ->
   `empty_tool_result`, not run -> `missing_evidence`.
2. **Freshness follows the source's freshness contract** (design §6, D4):
   - `authoritative_online`: `observed_at` is the proof. With no `max_age`
     the read must be at `as_of` itself; with one,
     `0 <= as_of - observed_at <= max_age`. A read later than `as_of` is never
     fresh (same for a snapshot's `source_as_of`). `record_updated_at` is never consulted - a record that
     has not changed for two days is current when read directly now.
   - `snapshot`: `source_as_of` is the proof, never `record_updated_at`, and
     only an explicit `max_age` can judge it. Without one the answer is
     `freshness_unsupported`: no TTL is invented here.
   - business evidence without a contract cannot prove currency.
   - derived evidence must be derived at `as_of`, and every input it names
     must be present and usable itself.
3. **Scope isolation.** A current-operational-state requirement is met only by
   business or derived evidence; a policy requirement only by wiki or
   document evidence. The wrong kind never counts, however authoritative.
4. **Conflicts are never resolved silently.** A usable
   `business_state_conflict` fact that is true blocks, whatever else is
   present; an excluded one (stale, other instant, input unavailable) is
   diagnostic only and must be re-derived. Two usable
   observations of one field with different values are always reported, and
   block any requirement that needs that field.
5. **Requirement-driven outcome.** BLOCKED iff some requirement is not
   supported, or a usable business-state conflict is present. Extra evidence or tool
   results - an unrelated EMPTY / ERROR, stale or out-of-scope evidence - are
   diagnostics and never poison an otherwise supported decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping

from .contracts import (
    AUTHORITY_MAX,
    AUTHORITY_MIN,
    BUSINESS_STATE_CONFLICT_FACT,
    BusinessEvidence,
    DerivedEvidence,
    Evidence,
    FreshnessContract,
    SourceType,
    ToolResult,
    ToolStatus,
    canonical_json_value,
    evidence_ref,
)


class ClaimScope(str, Enum):
    POLICY = "policy"
    CURRENT_OPERATIONAL_STATE = "current_operational_state"


class EvidenceOutcome(str, Enum):
    SUFFICIENT = "sufficient"
    BLOCKED = "blocked"


# Which evidence may support which kind of claim. SYSTEM is V1-only and is
# rejected outright on this path.
SCOPE_SOURCE_TYPES: dict[ClaimScope, frozenset[SourceType]] = {
    ClaimScope.POLICY: frozenset({SourceType.WIKI, SourceType.DOCUMENT}),
    ClaimScope.CURRENT_OPERATIONAL_STATE: frozenset(
        {SourceType.BUSINESS, SourceType.DERIVED}
    ),
}
V2_SOURCE_TYPES = frozenset().union(*SCOPE_SOURCE_TYPES.values())


# --------------------------------------------------------------------------
# Reason codes
# --------------------------------------------------------------------------

REASON_MISSING_EVIDENCE = "missing_evidence"
REASON_EMPTY_TOOL_RESULT = "empty_tool_result"
REASON_TOOL_ERROR = "tool_error"
REASON_FRESHNESS_UNSUPPORTED = "freshness_unsupported"
REASON_FRESHNESS_UNSATISFIED = "freshness_requirement_unsatisfied"
REASON_DERIVED_INPUT_UNAVAILABLE = "derived_input_unavailable"
REASON_OUT_OF_SCOPE_EVIDENCE = "out_of_scope_evidence"
REASON_CONFLICTING_OBSERVATIONS = "conflicting_observations"
REASON_BUSINESS_STATE_CONFLICT = "unresolved_business_state_conflict"
REASON_INSUFFICIENT_POLICY_EVIDENCE = "insufficient_policy_evidence"
REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE = "insufficient_operational_evidence"
REASON_EVIDENCE_SUFFICIENT = "evidence_sufficient"

REASON_CODE_ORDER = (
    REASON_MISSING_EVIDENCE,
    REASON_EMPTY_TOOL_RESULT,
    REASON_TOOL_ERROR,
    REASON_FRESHNESS_UNSUPPORTED,
    REASON_FRESHNESS_UNSATISFIED,
    REASON_DERIVED_INPUT_UNAVAILABLE,
    REASON_OUT_OF_SCOPE_EVIDENCE,
    REASON_CONFLICTING_OBSERVATIONS,
    REASON_BUSINESS_STATE_CONFLICT,
    REASON_INSUFFICIENT_POLICY_EVIDENCE,
    REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE,
    REASON_EVIDENCE_SUFFICIENT,
)
# The codes a BLOCKED decision can carry. Membership describes the code; it
# does not decide the outcome. The outcome is decided by the requirements (and
# a business-state conflict), and `reason_codes` lists only what did so.
BLOCKING_REASON_CODES = frozenset(REASON_CODE_ORDER) - {REASON_EVIDENCE_SUFFICIENT}

# Why one evidence item is not usable. A subset of the reason codes.
EXCLUSION_CAUSES = (
    REASON_FRESHNESS_UNSUPPORTED,
    REASON_FRESHNESS_UNSATISFIED,
    REASON_DERIVED_INPUT_UNAVAILABLE,
)

CONFLICT_KIND_BUSINESS_STATE = "business_state"
CONFLICT_KIND_OBSERVATIONS = "observations"

_INSUFFICIENT = {
    ClaimScope.POLICY: REASON_INSUFFICIENT_POLICY_EVIDENCE,
    ClaimScope.CURRENT_OPERATIONAL_STATE: REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE,
}


def _require_text(path: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(path + " must be a string, got " + type(value).__name__)
    if not value.strip():
        raise ValueError(path + " must not be empty")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EvidenceRequirement:
    """One fact a conclusion needs, stated as *what* must be shown.

    `subject` / `field` match evidence whose locator is `subject#field`
    (`logistics:SF1001#delivered_at`), or derived evidence with that subject
    and fact_key. `providers` names the tools whose EMPTY / ERROR result
    explains the fact's absence; it does not prescribe a call order.
    `source_types` may narrow - never widen - what the scope accepts.
    """

    requirement_id: str
    scope: ClaimScope
    subject: str
    field: str
    providers: tuple[str, ...] = ()
    source_types: frozenset[SourceType] | None = None

    def __post_init__(self) -> None:
        _require_text("EvidenceRequirement.requirement_id", self.requirement_id)
        if not isinstance(self.scope, ClaimScope):
            raise ValueError("EvidenceRequirement.scope must be a ClaimScope member")
        _require_text("EvidenceRequirement.subject", self.subject)
        _require_text("EvidenceRequirement.field", self.field)
        if "#" in self.field:
            raise ValueError("EvidenceRequirement.field must not contain '#'")
        if not isinstance(self.providers, tuple):
            raise ValueError("EvidenceRequirement.providers must be a tuple")
        for index, provider in enumerate(self.providers):
            _require_text("EvidenceRequirement.providers[" + str(index) + "]", provider)
        if self.source_types is not None:
            if not isinstance(self.source_types, frozenset) or not self.source_types:
                raise ValueError(
                    "EvidenceRequirement.source_types must be a non-empty frozenset"
                )
            if not self.source_types <= SCOPE_SOURCE_TYPES[self.scope]:
                raise ValueError(
                    "EvidenceRequirement.source_types may only narrow what the "
                    + self.scope.value + " scope accepts"
                )

    @property
    def accepted_source_types(self) -> frozenset[SourceType]:
        if self.source_types is None:
            return SCOPE_SOURCE_TYPES[self.scope]
        return self.source_types

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "scope": self.scope.value,
            "subject": self.subject,
            "field": self.field,
            "providers": list(self.providers),
            "source_types": sorted(item.value for item in self.accepted_source_types),
        }


@dataclass(frozen=True, kw_only=True)
class FreshnessRequirement:
    """When the conclusion is for, and how old its state may be.

    `as_of` is the business instant (the Clock's reading). `max_age` is the
    caller's freshness SLA: how far before `as_of` a state reading may have
    been taken. `None` means no SLA was stated - then only a read at `as_of`
    itself is current, and a snapshot cannot be judged at all.
    """

    as_of: datetime
    max_age: timedelta | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Checked on construction and again before every use."""
        if not isinstance(self.as_of, datetime):
            raise ValueError("FreshnessRequirement.as_of must be a datetime")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("FreshnessRequirement.as_of must be timezone-aware")
        if self.max_age is not None:
            if not isinstance(self.max_age, timedelta):
                raise ValueError("FreshnessRequirement.max_age must be a timedelta or None")
            if self.max_age < timedelta(0):
                raise ValueError("FreshnessRequirement.max_age must not be negative")


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ToolOutcome:
    """Status of one collected call. No error_code or message: they may carry
    infrastructure detail, and a policy decision is serialized and logged."""

    tool: str
    status: ToolStatus

    def to_dict(self) -> dict[str, object]:
        return {"tool": self.tool, "status": self.status.value}


@dataclass(frozen=True, kw_only=True)
class ExcludedEvidence:
    ref: str
    locator: str | None
    cause: str

    def to_dict(self) -> dict[str, object]:
        return {"ref": self.ref, "locator": self.locator, "cause": self.cause}


@dataclass(frozen=True, kw_only=True)
class RequirementOutcome:
    requirement: EvidenceRequirement
    satisfied: bool
    cause: str | None
    supporting_refs: tuple[str, ...] = ()
    # Diagnostics: evidence that matched this fact but is not of an accepted
    # source type. Never support, never a blocker by itself.
    out_of_scope_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "requirement": self.requirement.to_dict(),
            "satisfied": self.satisfied,
            "cause": self.cause,
            "supporting_refs": list(self.supporting_refs),
            "out_of_scope_refs": list(self.out_of_scope_refs),
        }


@dataclass(frozen=True, kw_only=True)
class ConflictReport:
    """Every evidence ref on every side, so each side can be cited."""

    kind: str
    subject: str
    field: str
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "field": self.field,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, kw_only=True)
class EvidenceDecision:
    outcome: EvidenceOutcome
    reason_codes: tuple[str, ...]
    requirements: tuple[RequirementOutcome, ...] = ()
    usable_evidence: tuple[Evidence, ...] = ()
    derived_evidence: tuple[DerivedEvidence, ...] = ()
    excluded_evidence: tuple[ExcludedEvidence, ...] = ()
    conflicts: tuple[ConflictReport, ...] = ()
    tool_outcomes: tuple[ToolOutcome, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "reason_codes": list(self.reason_codes),
            "requirements": [item.to_dict() for item in self.requirements],
            "usable_evidence": [
                {"ref": evidence_ref(item), "evidence": item.to_dict()}
                for item in self.usable_evidence
            ],
            "derived_evidence_refs": [evidence_ref(item) for item in self.derived_evidence],
            "excluded_evidence": [item.to_dict() for item in self.excluded_evidence],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "tool_outcomes": [item.to_dict() for item in self.tool_outcomes],
        }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate_evidence(path: str, evidence: object) -> None:
    """Re-check what this module reads: Evidence validates on construction but
    is not frozen, so construction-time guarantees may be void by now."""
    if not isinstance(evidence, Evidence):
        raise ValueError(path + " must be an Evidence, got " + type(evidence).__name__)
    _require_text(path + ".content", evidence.content)
    _require_text(path + ".source", evidence.source)
    if not isinstance(evidence.source_type, SourceType):
        raise ValueError(path + ".source_type must be a SourceType member")
    if evidence.source_type not in V2_SOURCE_TYPES:
        raise ValueError(
            path + " has source_type " + evidence.source_type.value
            + ", which the V2 path does not accept"
        )
    if not isinstance(evidence.metadata, Mapping):
        raise ValueError(path + ".metadata must be a mapping")
    for name in ("observed_at", "version", "locator"):
        value = getattr(evidence, name)
        if value is not None and not isinstance(value, str):
            raise ValueError(path + "." + name + " must be a string or None")
    authority = evidence.authority
    if isinstance(authority, bool) or not isinstance(authority, int):
        raise ValueError(path + ".authority must be an integer")
    if not AUTHORITY_MIN <= authority <= AUTHORITY_MAX:
        raise ValueError(path + ".authority is out of range")

    if evidence.source_type is SourceType.DERIVED:
        if not isinstance(evidence, DerivedEvidence):
            raise ValueError(path + " is derived but not a DerivedEvidence")
        for name in ("fact_key", "subject", "derivation_id"):
            _require_text(path + "." + name, getattr(evidence, name))
        if evidence.locator != evidence.subject + "#" + evidence.fact_key:
            raise ValueError(path + ".locator must be subject#fact_key")
        for name in ("input_refs", "policy_refs"):
            refs = getattr(evidence, name)
            if not isinstance(refs, tuple) or not all(
                isinstance(ref, str) and ref.strip() for ref in refs
            ):
                raise ValueError(path + "." + name + " must be a tuple of strings")
        if not evidence.input_refs:
            raise ValueError(path + ".input_refs must not be empty")
        value = evidence.value
        if value is None or isinstance(value, float) or not isinstance(value, (str, int)):
            raise ValueError(path + ".value must be a str, int, or bool")
        canonical_json_value(path + ".details", evidence.details)
    elif isinstance(evidence, DerivedEvidence):
        raise ValueError(path + " is a DerivedEvidence whose source_type is not derived")

    if evidence.source_type is SourceType.BUSINESS and isinstance(
        evidence, BusinessEvidence
    ):
        if not isinstance(evidence.freshness_contract, FreshnessContract):
            raise ValueError(path + ".freshness_contract must be a FreshnessContract")


def _validate_result(path: str, result: object) -> None:
    """Mirror the whole ToolResult invariant; results are not frozen either."""
    if not isinstance(result, ToolResult):
        raise ValueError(path + " must be a ToolResult, got " + type(result).__name__)
    _require_text(path + ".tool_name", result.tool_name)
    if not isinstance(result.status, ToolStatus):
        raise ValueError(path + ".status must be a ToolStatus member")
    # Before any truthiness test: a generator is truthy and single-use.
    if not isinstance(result.evidence, tuple):
        raise ValueError(path + ".evidence must be a tuple")
    has_error = result.error_code is not None or result.error_message is not None
    if result.status is ToolStatus.OK:
        if not result.evidence:
            raise ValueError(path + " is ok but carries no evidence")
        if has_error:
            raise ValueError(path + " is ok but carries error fields")
    elif result.status is ToolStatus.EMPTY:
        if result.evidence:
            raise ValueError(path + " is empty but carries evidence")
        if has_error:
            raise ValueError(path + " is empty but carries error fields")
    else:
        if result.evidence:
            raise ValueError(path + " is error but carries evidence")
        for name in ("error_code", "error_message"):
            value = getattr(result, name)
            if not isinstance(value, str) or not value.strip():
                # Describe the violation only; never echo the payload.
                raise ValueError(path + "." + name + " must be a non-empty string")
    for index, item in enumerate(result.evidence):
        item_path = path + ".evidence[" + str(index) + "]"
        _validate_evidence(item_path, item)
        if item.source_type is SourceType.DERIVED:
            raise ValueError(item_path + " is derived; tools never produce derived evidence")


def _validate_inputs(
    results: object, requirements: object, freshness: object, derived: object
) -> None:
    for name, value in (("results", results), ("requirements", requirements),
                        ("derived", derived)):
        if not isinstance(value, tuple):
            raise ValueError(name + " must be a tuple, got " + type(value).__name__)
    if not isinstance(freshness, FreshnessRequirement):
        raise ValueError(
            "freshness must be a FreshnessRequirement, got " + type(freshness).__name__
        )
    freshness.validate()
    if not requirements:
        raise ValueError("requirements must not be empty: state what the conclusion needs")
    for index, result in enumerate(results):
        _validate_result("results[" + str(index) + "]", result)
    seen: set[str] = set()
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, EvidenceRequirement):
            raise ValueError(
                "requirements[" + str(index) + "] must be an EvidenceRequirement, got "
                + type(requirement).__name__
            )
        if requirement.requirement_id in seen:
            raise ValueError(
                "requirements[" + str(index) + "] repeats a requirement_id"
            )
        seen.add(requirement.requirement_id)
    for index, item in enumerate(derived):
        path = "derived[" + str(index) + "]"
        if not isinstance(item, DerivedEvidence):
            raise ValueError(path + " must be a DerivedEvidence, got " + type(item).__name__)
        _validate_evidence(path, item)


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------


def _parse_instant(path: str, value: object) -> datetime | None:
    """An aware instant, or None if absent. Garbled or naive text is an error."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(path + " must be an ISO-8601 string or None")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(path + " must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(path + " must carry a timezone offset")
    return parsed


def _age_verdict(reading: datetime, freshness: FreshnessRequirement) -> str | None:
    """Fresh iff 0 <= as_of - reading <= max_age (max_age None: exactly 0).

    A reading later than `as_of` is never fresh: a negative age must not slip
    under `max_age`. Compared as absolute instants, so offsets never matter.
    """
    age = freshness.as_of - reading
    if age < timedelta(0):
        return REASON_FRESHNESS_UNSATISFIED
    if freshness.max_age is None:
        return None if age == timedelta(0) else REASON_FRESHNESS_UNSATISFIED
    return None if age <= freshness.max_age else REASON_FRESHNESS_UNSATISFIED


def assess_business_freshness(
    evidence: Evidence, freshness: FreshnessRequirement
) -> str | None:
    """None if the business evidence meets `freshness`, else the reason code.

    `record_updated_at` is never read here, in either direction: an old value
    does not make a direct read stale, and a recent one does not make a
    snapshot current.
    """
    if not isinstance(freshness, FreshnessRequirement):
        raise ValueError("freshness must be a FreshnessRequirement")
    freshness.validate()
    if not isinstance(evidence, BusinessEvidence):
        # Business evidence with no declared freshness contract cannot prove
        # that it describes the current state.
        return REASON_FRESHNESS_UNSUPPORTED
    contract = evidence.freshness_contract
    if contract is FreshnessContract.AUTHORITATIVE_ONLINE:
        observed_at = _parse_instant("observed_at", evidence.observed_at)
        if observed_at is None:
            return REASON_FRESHNESS_UNSUPPORTED
        return _age_verdict(observed_at, freshness)
    if contract is FreshnessContract.SNAPSHOT:
        source_as_of = _parse_instant("source_as_of", evidence.source_as_of)
        if source_as_of is None:
            return REASON_FRESHNESS_UNSUPPORTED
        if freshness.max_age is None:
            # How old a snapshot may be is the caller's SLA to state; none was
            # given, so there is nothing to judge against.
            return REASON_FRESHNESS_UNSUPPORTED
        return _age_verdict(source_as_of, freshness)
    raise ValueError("freshness_contract must be a FreshnessContract member")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _key(evidence: Evidence) -> tuple[str, str] | None:
    if isinstance(evidence, DerivedEvidence):
        return evidence.subject, evidence.fact_key
    locator = evidence.locator
    if not locator or "#" not in locator:
        return None
    subject, _, field = locator.rpartition("#")
    if not subject or not field:
        return None
    return subject, field


def _fact_value(evidence: Evidence) -> object:
    if isinstance(evidence, DerivedEvidence):
        return evidence.value
    return evidence.metadata.get("value")


def _comparable(value: object) -> str:
    # Values are plain JSON data; compare by canonical form, so 1 and True
    # (equal in Python) stay different values.
    return json.dumps(
        canonical_json_value("value", value), sort_keys=True, ensure_ascii=False
    ) + "|" + type(value).__name__


def evaluate_evidence_v2(
    results: tuple[ToolResult, ...],
    *,
    requirements: tuple[EvidenceRequirement, ...],
    freshness: FreshnessRequirement,
    derived: tuple[DerivedEvidence, ...] = (),
) -> EvidenceDecision:
    """Judge whether the collected evidence supports a conclusion that needs
    `requirements`, as of `freshness.as_of`.

    Every input is read-only. All checks run, so the decision lists every
    reason rather than the first.
    """
    _validate_inputs(results, requirements, freshness, derived)

    # --- collect, deduplicated by reference -------------------------------
    candidates: list[tuple[str, Evidence]] = []
    by_ref: dict[str, Evidence] = {}
    for result in results:
        if result.status is not ToolStatus.OK:
            continue
        for item in result.evidence:
            ref = evidence_ref(item)
            if ref not in by_ref:
                by_ref[ref] = item
                candidates.append((ref, item))
    for item in derived:
        ref = evidence_ref(item)
        if ref not in by_ref:
            by_ref[ref] = item
            candidates.append((ref, item))

    # --- usability ----------------------------------------------------------
    exclusion: dict[str, str] = {}
    for ref, item in candidates:
        if item.source_type is SourceType.BUSINESS:
            verdict = assess_business_freshness(item, freshness)
            if verdict is not None:
                exclusion[ref] = verdict

    # Derived facts: derived at `as_of` itself - a fact may depend on "now"
    # (elapsed days), so reuse across business instants is never assumed safe.
    for ref, item in candidates:
        if item.source_type is not SourceType.DERIVED:
            continue
        derived_at = _parse_instant("derived.observed_at", item.observed_at)
        if derived_at is None:
            exclusion[ref] = REASON_FRESHNESS_UNSUPPORTED
            continue
        # Earlier *or later* than as_of: either way not derived for this instant.
        if derived_at != freshness.as_of:
            exclusion[ref] = REASON_FRESHNESS_UNSATISFIED

    # ... and every direct input present and usable, recursively.
    resolved: set[str] = set()

    def inputs_usable(ref: str, visiting: frozenset[str]) -> bool:
        if ref not in by_ref or ref in exclusion:
            return False
        if ref in resolved:
            return True
        item = by_ref[ref]
        if isinstance(item, DerivedEvidence):
            if ref in visiting or not all(
                inputs_usable(input_ref, visiting | {ref}) for input_ref in item.input_refs
            ):
                exclusion[ref] = REASON_DERIVED_INPUT_UNAVAILABLE
                return False
        resolved.add(ref)
        return True

    for ref, item in candidates:
        if isinstance(item, DerivedEvidence):
            inputs_usable(ref, frozenset())

    usable = [(ref, item) for ref, item in candidates if ref not in exclusion]
    reasons: set[str] = set()

    # --- conflicts ----------------------------------------------------------
    conflicts: list[ConflictReport] = []
    # Only a *usable* conflict fact is a current conflict. One that is stale,
    # derived at another instant, or missing an input goes through the same
    # freshness / dependency checks as any derived fact: it is excluded (and
    # visible in excluded_evidence) and must be re-derived to count.
    for ref, item in usable:
        if (
            isinstance(item, DerivedEvidence)
            and item.fact_key == BUSINESS_STATE_CONFLICT_FACT
            and item.value is True
        ):
            conflicts.append(
                ConflictReport(
                    kind=CONFLICT_KIND_BUSINESS_STATE,
                    subject=item.subject,
                    field=item.fact_key,
                    evidence_refs=(ref,) + tuple(item.input_refs),
                )
            )
            reasons.add(REASON_BUSINESS_STATE_CONFLICT)

    operational = SCOPE_SOURCE_TYPES[ClaimScope.CURRENT_OPERATIONAL_STATE]
    groups: dict[tuple[str, str], list[tuple[str, Evidence]]] = {}
    for ref, item in usable:
        key = _key(item)
        if key is not None and item.source_type in operational:
            groups.setdefault(key, []).append((ref, item))
    conflicting_keys: set[tuple[str, str]] = set()
    for key in sorted(groups):
        members = groups[key]
        if len({_comparable(_fact_value(item)) for _, item in members}) > 1:
            conflicting_keys.add(key)
            conflicts.append(
                ConflictReport(
                    kind=CONFLICT_KIND_OBSERVATIONS,
                    subject=key[0],
                    field=key[1],
                    evidence_refs=tuple(ref for ref, _ in members),
                )
            )
            # Reported always; it blocks only through a requirement that
            # needs this fact (see below).

    # --- requirements -------------------------------------------------------
    outcomes: list[RequirementOutcome] = []
    for requirement in requirements:
        wanted = (requirement.subject, requirement.field)
        accepted = requirement.accepted_source_types
        matching = [(ref, item) for ref, item in candidates if _key(item) == wanted]
        in_scope = [(ref, item) for ref, item in matching if item.source_type in accepted]
        supporting = tuple(ref for ref, _ in in_scope if ref not in exclusion)
        # Diagnostics only: evidence speaking to this fact that may not decide it.
        out_of_scope = tuple(ref for ref, item in matching if item.source_type not in accepted)
        provider_statuses = {
            result.status for result in results if result.tool_name in requirement.providers
        }

        cause: str | None
        if supporting and wanted in conflicting_keys:
            cause = REASON_CONFLICTING_OBSERVATIONS
        elif supporting:
            cause = None
        elif in_scope:
            cause = next(
                code for code in EXCLUSION_CAUSES
                if code in {exclusion[ref] for ref, _ in in_scope}
            )
        elif ToolStatus.ERROR in provider_statuses:
            cause = REASON_TOOL_ERROR
        elif ToolStatus.EMPTY in provider_statuses:
            cause = REASON_EMPTY_TOOL_RESULT
        elif matching:
            # Something speaks to this fact, but not the kind of evidence that
            # may decide it - e.g. a document "stating" an order's status.
            cause = REASON_OUT_OF_SCOPE_EVIDENCE
        else:
            cause = REASON_MISSING_EVIDENCE

        if cause is not None:
            reasons.add(cause)
            reasons.add(_INSUFFICIENT[requirement.scope])
        outcomes.append(
            RequirementOutcome(
                requirement=requirement,
                satisfied=cause is None,
                cause=cause,
                supporting_refs=supporting if cause is None else (),
                out_of_scope_refs=out_of_scope,
            )
        )

    # Requirement-driven: BLOCKED iff a required fact is not supported, or a
    # usable business-state conflict is present. An unrelated EMPTY / ERROR result,
    # stale or out-of-scope evidence, or a disagreement on a fact nobody
    # requires never decides the outcome; they stay visible in tool_outcomes,
    # excluded_evidence, conflicts, and out_of_scope_refs.
    blocked = any(not outcome.satisfied for outcome in outcomes) or any(
        report.kind == CONFLICT_KIND_BUSINESS_STATE for report in conflicts
    )
    if not blocked:
        reasons.add(REASON_EVIDENCE_SUFFICIENT)

    return EvidenceDecision(
        outcome=EvidenceOutcome.BLOCKED if blocked else EvidenceOutcome.SUFFICIENT,
        reason_codes=tuple(code for code in REASON_CODE_ORDER if code in reasons),
        requirements=tuple(outcomes),
        usable_evidence=tuple(item for _, item in usable),
        derived_evidence=tuple(
            item for _, item in usable if isinstance(item, DerivedEvidence)
        ),
        excluded_evidence=tuple(
            ExcludedEvidence(ref=ref, locator=item.locator, cause=exclusion[ref])
            for ref, item in candidates
            if ref in exclusion
        ),
        conflicts=tuple(conflicts),
        tool_outcomes=tuple(
            ToolOutcome(tool=result.tool_name, status=result.status) for result in results
        ),
    )
