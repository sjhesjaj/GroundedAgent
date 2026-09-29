"""The `search_after_sales_policy` contract and its adapter boundary.

The closed tool arguments live in registry.py. Stage 4.3's published catalog
implements the adapter using versioned Wiki source snapshots. NotReady remains
an explicit injectable failure fixture; it is no longer the runtime default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from orchestration.contracts import ToolResult

from .clock import require_aware
from .context import TrustedExecutionContext
from .errors import ToolNotReady

POLICY_TOOL_NAME = "search_after_sales_policy"


class PolicyRuleType(str, Enum):
    RETURN_WINDOW = "return_window"
    EXCHANGE_WINDOW = "exchange_window"
    NON_RETURNABLE = "non_returnable"
    HANDOFF = "handoff"


# --------------------------------------------------------------------------
# Structured params: time-window rules
# --------------------------------------------------------------------------


class StartEvent(str, Enum):
    """The business event a window counts from. Only delivery, for now."""

    DELIVERED = "delivered"


class CountingRule(str, Enum):
    """How a window's days are counted (D26). Closed: no free-text rules.

    `natural_days_from_next_day` - 签收次日起算 N 个自然日:
    with D = the calendar date of the start event at the policy's `utc_offset`,
    day 1 is D+1 and day N is D+N. The window is open through the end of D+N
    (local) and closed from D+N+1 00:00. The start-event day itself is day 0
    and is inside the window. Days are calendar dates, never 24-hour spans.
    """

    NATURAL_DAYS_FROM_NEXT_DAY = "natural_days_from_next_day"


WINDOW_RULE_TYPES = frozenset({PolicyRuleType.RETURN_WINDOW, PolicyRuleType.EXCHANGE_WINDOW})
WINDOW_PARAM_KEYS = ("window_days", "start_event", "counting_rule", "utc_offset")

_UTC_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def parse_utc_offset(path: str, value: object) -> timezone:
    """`+08:00` -> timezone(+8h). The day boundary is explicit, never local time."""
    if not isinstance(value, str):
        raise ValueError(path + " must be a string like +08:00")
    match = _UTC_OFFSET.match(value)
    if match is None:
        raise ValueError(path + " must look like +08:00")
    sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3))
    if hours > 14 or minutes > 59 or (hours == 14 and minutes):
        raise ValueError(path + " is outside -14:00..+14:00")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if sign == "-" else delta)


@dataclass(frozen=True, kw_only=True)
class WindowParams:
    """The machine-readable part of a return / exchange window rule."""

    window_days: int
    start_event: StartEvent
    counting_rule: CountingRule
    utc_offset: str

    @property
    def tzinfo(self) -> timezone:
        return parse_utc_offset("WindowParams.utc_offset", self.utc_offset)


def parse_window_params(params: object, path: str = "params") -> WindowParams:
    """Validate window params strictly. Nothing is guessed or defaulted.

    Exactly the keys in WINDOW_PARAM_KEYS: a missing key or an unknown one (a
    misspelling such as `windowDays`) is an error, not something to ignore.
    Values must match exactly - enum values, not lookalike strings.
    """
    if not isinstance(params, Mapping):
        raise ValueError(path + " must be a mapping")
    keys = set(params)
    if not all(isinstance(key, str) for key in keys):
        raise ValueError(path + " keys must be strings")
    missing = [key for key in WINDOW_PARAM_KEYS if key not in keys]
    if missing:
        raise ValueError(path + " is missing: " + ", ".join(missing))
    unknown = sorted(keys - set(WINDOW_PARAM_KEYS))
    if unknown:
        raise ValueError(path + " has unknown key(s): " + ", ".join(unknown))

    window_days = params["window_days"]
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise ValueError(path + ".window_days must be a positive integer")

    start_event = params["start_event"]
    if not isinstance(start_event, str) or start_event not in {
        member.value for member in StartEvent
    }:
        raise ValueError(
            path + ".start_event must be one of: "
            + ", ".join(member.value for member in StartEvent)
        )

    counting_rule = params["counting_rule"]
    if not isinstance(counting_rule, str) or counting_rule not in {
        member.value for member in CountingRule
    }:
        raise ValueError(
            path + ".counting_rule must be one of: "
            + ", ".join(member.value for member in CountingRule)
        )

    utc_offset = params["utc_offset"]
    parse_utc_offset(path + ".utc_offset", utc_offset)

    return WindowParams(
        window_days=window_days,
        start_event=StartEvent(start_event),
        counting_rule=CountingRule(counting_rule),
        utc_offset=utc_offset,
    )


def _require_aware_iso(path: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(path + " must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(path + " must be an ISO-8601 timestamp") from None
    require_aware(path, parsed)


@dataclass(frozen=True, kw_only=True)
class PolicyRecord:
    """One version of one after-sales rule (docs/v2/stage4-design.md §3.1).

    `params` holds the machine-readable rule, including how days are counted
    (e.g. `window_days`, `start_event`), so that day arithmetic is decided by
    the rule rather than by a model. Structured params come from front matter
    parsed by code, never from LLM extraction (D16).

    Window rules (return_window / exchange_window) must carry exactly the
    WINDOW_PARAM_KEYS schema; see `parse_window_params`. Params of the other
    rule types are not consumed by any calculator yet and stay unvalidated.
    `params` is copied into a read-only mapping on construction, so neither the
    caller's dict nor the record can change it afterwards.

    Effective window: `[effective_from, effective_to)` - see `is_policy_in_effect`.

    `priority` is business precedence, unrelated to Evidence authority.
    The published catalog selects the highest applicable priority; ties must
    agree on params. Zero is a compatibility default for older fixtures only;
    formal source front matter must explicitly declare it.
    """

    policy_id: str
    version: str
    title: str
    rule_type: PolicyRuleType
    # Categories the rule applies to; empty means every category.
    scope: tuple[str, ...]
    params: Mapping[str, object] = field(default_factory=dict)
    effective_from: str
    effective_to: str | None = None
    source_doc: str
    locator: str
    build_id: str
    priority: int = 0

    def __post_init__(self) -> None:
        if type(self.priority) is not int:
            raise ValueError("PolicyRecord.priority must be an integer")
        for name in ("policy_id", "version", "title", "source_doc", "locator", "build_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("PolicyRecord." + name + " must be a non-empty string")
        if not isinstance(self.rule_type, PolicyRuleType):
            raise ValueError("PolicyRecord.rule_type must be a PolicyRuleType")
        if not isinstance(self.scope, tuple) or not all(
            isinstance(item, str) and item.strip() for item in self.scope
        ):
            raise ValueError("PolicyRecord.scope must be a tuple of non-empty strings")
        if not isinstance(self.params, Mapping):
            raise ValueError("PolicyRecord.params must be a mapping")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        if self.rule_type in WINDOW_RULE_TYPES:
            parse_window_params(self.params, "PolicyRecord.params")
        _require_aware_iso("PolicyRecord.effective_from", self.effective_from)
        if self.effective_to is not None:
            _require_aware_iso("PolicyRecord.effective_to", self.effective_to)
            if datetime.fromisoformat(self.effective_to) <= datetime.fromisoformat(
                self.effective_from
            ):
                raise ValueError(
                    "PolicyRecord.effective_to must be later than effective_from"
                )


def window_params(record: PolicyRecord) -> WindowParams:
    """The validated window params of a return / exchange window rule."""
    if not isinstance(record, PolicyRecord):
        raise ValueError("record must be a PolicyRecord, got " + type(record).__name__)
    if record.rule_type not in WINDOW_RULE_TYPES:
        raise ValueError(
            "PolicyRecord " + record.policy_id + " is a " + record.rule_type.value
            + " rule, not a time-window rule"
        )
    return parse_window_params(record.params, "PolicyRecord.params")


def policy_ref(record: PolicyRecord) -> str:
    """A stable reference to one version of one rule in one build."""
    if not isinstance(record, PolicyRecord):
        raise ValueError("record must be a PolicyRecord, got " + type(record).__name__)
    return "policy:" + record.policy_id + "@" + record.version + "#" + record.build_id


def is_policy_in_effect(record: PolicyRecord, as_of: datetime) -> bool:
    """Whether the rule is in force at the business instant `as_of`.

    The effective window is half-open, `[effective_from, effective_to)`:

    - `effective_from` is inclusive: in force from that instant on;
    - `effective_to` is exclusive: no longer in force at that instant, so a
      rule ending where its successor starts never overlaps it;
    - `effective_to = None` means open-ended.

    Instants are compared as absolute times, so offsets never matter. `as_of`
    comes from the injected Clock (or an explicit business instant); this
    function never reads the system clock.
    """
    if not isinstance(record, PolicyRecord):
        raise ValueError("record must be a PolicyRecord, got " + type(record).__name__)
    as_of = require_aware("as_of", as_of)
    if as_of < datetime.fromisoformat(record.effective_from):
        return False
    if record.effective_to is not None and as_of >= datetime.fromisoformat(
        record.effective_to
    ):
        return False
    return True


def policy_applies_to_category(record: PolicyRecord, category: str) -> bool:
    """Scope check by structured field: empty scope = every category.

    Exact string equality against `record.scope`. No normalisation, no
    substring or fuzzy matching - a category the scope does not list verbatim
    is out of scope.
    """
    if not isinstance(record, PolicyRecord):
        raise ValueError("record must be a PolicyRecord, got " + type(record).__name__)
    if not isinstance(category, str) or not category.strip():
        raise ValueError("category must be a non-empty string")
    return not record.scope or category in record.scope


@runtime_checkable
class PolicySearchAdapter(Protocol):
    def search(self, query: str, *, as_of: datetime) -> ToolResult:
        """Rules matching `query` that are in force at the business instant `as_of`.

        `as_of` always comes from the injected Clock. The result's tool_name is
        `search_after_sales_policy`; an empty match is `ToolStatus.EMPTY`.
        """
        ...


class PolicyAdapterNotReady:
    """The Stage 4.1 runtime adapter: the Wiki-backed one is not wired yet."""

    def search(self, query: str, *, as_of: datetime) -> ToolResult:
        raise ToolNotReady(POLICY_TOOL_NAME + " adapter is not wired yet")


def make_policy_handler(adapter: PolicySearchAdapter):
    """Bind the adapter chosen by the composition root into a tool handler."""
    if not isinstance(adapter, PolicySearchAdapter):
        raise ValueError(
            "adapter must implement PolicySearchAdapter, got " + type(adapter).__name__
        )

    def search_after_sales_policy(
        context: TrustedExecutionContext, arguments: Mapping[str, str]
    ) -> ToolResult:
        as_of = require_aware("context.clock.now()", context.clock.now())
        return adapter.search(arguments["query"], as_of=as_of)

    return search_after_sales_policy
