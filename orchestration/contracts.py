"""Domain-neutral evidence contracts shared by every knowledge-agent tool.

These types are the boundary between a tool's native result shape and the
orchestration layer. They carry no retrieval, wiki, or database logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SourceType(str, Enum):
    WIKI = "wiki"
    DOCUMENT = "document"
    # V1 only. Retires with the V1 system_provider (docs/v2/stage4-design.md §6).
    SYSTEM = "system"
    # V2 after-sales business read tools. Always carried by `BusinessEvidence`.
    BUSINESS = "business"


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
