"""Domain-neutral evidence contracts shared by every knowledge-agent tool.

These types are the boundary between a tool's native result shape and the
orchestration layer. They carry no retrieval, wiki, or database logic.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping


class SourceType(str, Enum):
    WIKI = "wiki"
    DOCUMENT = "document"
    # V1 only. Retires with the V1 system_provider (docs/v2/stage4-design.md §6).
    SYSTEM = "system"
    # V2 after-sales business read tools. Always carried by `BusinessEvidence`.
    BUSINESS = "business"
    # V2 facts computed deterministically from other evidence. Always carried
    # by `DerivedEvidence`; never produced by a tool.
    DERIVED = "derived"


class FreshnessContract(str, Enum):
    """What a data source promises about how current its reads are.

    Freshness is judged from this contract, never from `record_updated_at`: a
    record that has not changed for two days is still current when it is read
    directly from the authoritative source.
    """

    # A direct read of the authoritative online source: a successful read at
    # `observed_at` is itself a current observation.
    AUTHORITATIVE_ONLINE = "authoritative_online"
    # Reserved. A cache / snapshot / replica must state `source_as_of`. No
    # Stage 4 source uses it.
    SNAPSHOT = "snapshot"


class ToolStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


AUTHORITY_MIN = 0
AUTHORITY_MAX = 100
CONFIDENCE_MIN = 0.0
CONFIDENCE_MAX = 1.0


@dataclass(kw_only=True)
class Evidence:
    """One traceable claim produced by a tool.

    Keyword-only so that `authority` can stay required: a tool that forgets to
    declare its standing is an integration error and must fail loudly rather
    than silently produce valid lowest-authority evidence.

    `observed_at` is deliberately not auto-filled with the query time: that
    would describe when we looked, not how fresh the source is. Only a tool
    that actually knows the source's observation time may set it.

    `confidence` is a calibrated probability. Retrieval scores are ranking
    signals on an arbitrary scale and belong in `metadata` instead.
    """

    content: str
    source_type: SourceType
    source: str
    locator: str | None = None
    version: str | None = None
    observed_at: str | None = None
    authority: int
    confidence: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content or not self.content.strip():
            raise ValueError("Evidence.content must not be empty")
        if not self.source or not self.source.strip():
            raise ValueError("Evidence.source must not be empty")
        if isinstance(self.authority, bool) or not isinstance(self.authority, int):
            raise ValueError("Evidence.authority must be an integer")
        if not AUTHORITY_MIN <= self.authority <= AUTHORITY_MAX:
            raise ValueError(
                f"Evidence.authority must be between {AUTHORITY_MIN} and {AUTHORITY_MAX}"
            )
        if self.confidence is not None and not (
            CONFIDENCE_MIN <= self.confidence <= CONFIDENCE_MAX
        ):
            raise ValueError(
                f"Evidence.confidence must be between {CONFIDENCE_MIN} and {CONFIDENCE_MAX}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "content": self.content,
            "source_type": self.source_type.value,
            "source": self.source,
            "locator": self.locator,
            "version": self.version,
            "observed_at": self.observed_at,
            "authority": self.authority,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


def _require_aware_iso(path: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(path + " must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(path + " must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(path + " must carry a timezone offset")


OBSERVATION_ID_KEY = "observation_id"


@dataclass(kw_only=True)
class BusinessEvidence(Evidence):
    """One field of one business record, as read at one business instant.

    Three first-class time / version fields answer three different questions:

    - `observed_at`: the business instant (from the injected Clock) at which
      the tool read this state. Required here, unlike on `Evidence`.
    - `record_updated_at`: when the source record last changed. It says
      nothing about staleness and must never be used to judge it.
    - `state_version`: which version of the source record was read.

    `metadata[OBSERVATION_ID_KEY]` is always present; it is `None` until the
    executor links the evidence to the tool call that produced it.
    """

    record_updated_at: str | None
    state_version: int
    freshness_contract: FreshnessContract
    source_as_of: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.source_type is not SourceType.BUSINESS:
            raise ValueError("BusinessEvidence.source_type must be business")
        if not isinstance(self.locator, str) or not self.locator.strip():
            raise ValueError("BusinessEvidence.locator must not be empty")
        _require_aware_iso("BusinessEvidence.observed_at", self.observed_at)
        if self.record_updated_at is not None:
            _require_aware_iso(
                "BusinessEvidence.record_updated_at", self.record_updated_at
            )
        if isinstance(self.state_version, bool) or not isinstance(
            self.state_version, int
        ):
            raise ValueError("BusinessEvidence.state_version must be an integer")
        if self.state_version < 1:
            raise ValueError("BusinessEvidence.state_version must be at least 1")
        if not isinstance(self.freshness_contract, FreshnessContract):
            raise ValueError(
                "BusinessEvidence.freshness_contract must be a FreshnessContract"
            )
        if self.freshness_contract is FreshnessContract.AUTHORITATIVE_ONLINE:
            if self.source_as_of is not None:
                raise ValueError(
                    "BusinessEvidence.source_as_of must be None for an "
                    "authoritative_online source"
                )
        else:
            # A snapshot without its as-of time cannot prove anything current.
            _require_aware_iso("BusinessEvidence.source_as_of", self.source_as_of)
        if OBSERVATION_ID_KEY not in self.metadata:
            raise ValueError(
                "BusinessEvidence.metadata must reserve " + OBSERVATION_ID_KEY
            )

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload.update(
            {
                "record_updated_at": self.record_updated_at,
                "state_version": self.state_version,
                "freshness_contract": self.freshness_contract.value,
                "source_as_of": self.source_as_of,
            }
        )
        return payload


# --------------------------------------------------------------------------
# Derived evidence (V2)
# --------------------------------------------------------------------------

# The derived fact that reports an order / logistics state contradiction. The
# rule table that decides it is domain logic (aftersales/derived.py); the key is
# shared here so the Evidence Policy can surface it without importing a domain.
BUSINESS_STATE_CONFLICT_FACT = "business_state_conflict"

EVIDENCE_REF_PREFIX = "ev-"
_REF_DIGEST_LENGTH = 24


def canonical_json_value(path: str, value: object) -> object:
    """Return `value` as plain JSON data, or raise naming `path`.

    Allowed: str, int, bool, None, sequences and string-keyed mappings of
    those. Floats are refused: a derived fact must not depend on float
    formatting to stay byte-stable. Mappings come back with sorted keys and
    sequences as lists, so equal inputs serialize identically.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError(path + " must not be a float")
    if isinstance(value, Mapping):
        result = {}
        for key in sorted(value, key=lambda item: (type(item).__name__, str(item))):
            if not isinstance(key, str):
                raise ValueError(path + " keys must be strings")
            result[key] = canonical_json_value(path + "." + key, value[key])
        return result
    if isinstance(value, (list, tuple)):
        return [
            canonical_json_value(path + "[" + str(index) + "]", item)
            for index, item in enumerate(value)
        ]
    raise ValueError(path + " must be JSON data, got " + type(value).__name__)


def evidence_ref(evidence: Evidence) -> str:
    """A stable, deterministic reference to one evidence item.

    The reference is content-addressed: a digest of the item's canonical
    `to_dict()` form, with `metadata[OBSERVATION_ID_KEY]` left out because that
    only links the item to a trace span and says nothing about what was
    observed. Consequences:

    - the same observation (same field, value, state_version, observed_at, ...)
      always gets the same reference, whichever tool call produced it and
      whether or not the executor has linked it yet;
    - any difference in what was observed gives a different reference;
    - no UUID, counter, or wall clock is involved.

    Nothing is stored on the evidence: V1 `Evidence` gains no field.
    """
    if not isinstance(evidence, Evidence):
        raise ValueError("evidence_ref needs an Evidence, got " + type(evidence).__name__)
    if not isinstance(evidence.source_type, SourceType):
        raise ValueError("evidence_ref needs a SourceType member as source_type")
    if not isinstance(evidence.metadata, Mapping):
        raise ValueError("evidence_ref needs a mapping as metadata")
    payload = evidence.to_dict()
    metadata = dict(payload["metadata"])
    metadata.pop(OBSERVATION_ID_KEY, None)
    payload["metadata"] = metadata
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError(
            "evidence is not plain JSON data, so it has no stable reference"
        ) from None
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return EVIDENCE_REF_PREFIX + evidence.source_type.value + "-" + digest[:_REF_DIGEST_LENGTH]


def _require_ref_tuple(path: str, value: object, *, allow_empty: bool) -> None:
    if not isinstance(value, tuple):
        raise ValueError(path + " must be a tuple, got " + type(value).__name__)
    if not allow_empty and not value:
        raise ValueError(path + " must not be empty")
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(path + "[" + str(index) + "] must be a non-empty string")
    if len(set(value)) != len(value):
        raise ValueError(path + " must not repeat a reference")


@dataclass(kw_only=True)
class DerivedEvidence(Evidence):
    """One fact computed deterministically from other evidence (design §6, D5).

    Additive to `Evidence`: V1 fields keep their meaning, and `to_dict()` only
    appends keys. The fact itself is structured - `fact_key`, `subject`,
    `value`, `details` - so nothing downstream has to read `content`.

    - `subject` / `fact_key`: what the fact is about and which fact it is. The
      locator is always `subject#fact_key`, the same shape as a business
      locator (`logistics:SF1001#delivered_at`).
    - `value`: the fact, as a str / int / bool.
    - `details`: the calculation basis (e.g. elapsed days and window size), as
      plain JSON data.
    - `input_refs`: `evidence_ref()` of every *direct* input evidence item. A
      derived fact with no input is fabricated, so this is never empty.
    - `policy_refs`: the policy versions the derivation applied, if any.
    - `derivation_id`: which deterministic rule, at which revision, computed it.
    - `observed_at`: the business instant (from the Clock) it was derived at.
    """

    fact_key: str
    subject: str
    value: str | int | bool
    details: dict[str, object] = field(default_factory=dict)
    input_refs: tuple[str, ...]
    policy_refs: tuple[str, ...] = ()
    derivation_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.source_type is not SourceType.DERIVED:
            raise ValueError("DerivedEvidence.source_type must be derived")
        for name in ("fact_key", "subject", "derivation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("DerivedEvidence." + name + " must be a non-empty string")
        if "#" in self.fact_key:
            raise ValueError("DerivedEvidence.fact_key must not contain '#'")
        if self.locator != self.subject + "#" + self.fact_key:
            raise ValueError("DerivedEvidence.locator must be subject#fact_key")
        _require_aware_iso("DerivedEvidence.observed_at", self.observed_at)
        if self.value is None or isinstance(self.value, float) or not isinstance(
            self.value, (str, int)
        ):
            raise ValueError("DerivedEvidence.value must be a str, int, or bool")
        if not isinstance(self.details, Mapping):
            raise ValueError("DerivedEvidence.details must be a mapping")
        self.details = canonical_json_value("DerivedEvidence.details", self.details)
        _require_ref_tuple("DerivedEvidence.input_refs", self.input_refs, allow_empty=False)
        _require_ref_tuple("DerivedEvidence.policy_refs", self.policy_refs, allow_empty=True)

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload.update(
            {
                "fact_key": self.fact_key,
                "subject": self.subject,
                "value": self.value,
                "details": copy.deepcopy(self.details),
                "input_refs": list(self.input_refs),
                "policy_refs": list(self.policy_refs),
                "derivation_id": self.derivation_id,
            }
        )
        return payload


@dataclass
class ToolResult:
    """The outcome of one tool call, including why it produced no evidence.

    The status invariants keep "found nothing" and "failed to run" distinct, so
    an infrastructure failure can never be presented as an absence of evidence.
    """

    tool_name: str
    status: ToolStatus
    evidence: tuple[Evidence, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    trace: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("ToolResult.tool_name must not be empty")
        self.evidence = tuple(self.evidence)
        has_error = bool(self.error_code) or bool(self.error_message)

        if self.status is ToolStatus.OK:
            if not self.evidence:
                raise ValueError("ToolResult 'ok' requires at least one evidence item")
            if has_error:
                raise ValueError("ToolResult 'ok' must not carry error fields")
        elif self.status is ToolStatus.EMPTY:
            if self.evidence:
                raise ValueError("ToolResult 'empty' must not carry evidence")
            if has_error:
                raise ValueError("ToolResult 'empty' must not carry error fields")
        else:
            if self.evidence:
                raise ValueError("ToolResult 'error' must not carry evidence")
            if not self.error_code or not self.error_code.strip():
                raise ValueError("ToolResult 'error' requires a non-empty error_code")
            if not self.error_message or not self.error_message.strip():
                raise ValueError("ToolResult 'error' requires a non-empty error_message")

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "status": self.status.value,
            "evidence": [item.to_dict() for item in self.evidence],
            "error_code": self.error_code,
            "error_message": self.error_message,
            "trace": dict(self.trace),
        }
