"""The `search_after_sales_policy` contract and its adapter boundary.

Stage 4.1 freezes the *contract* only: the tool name, its closed input schema
(declared in `registry.py`), the policy record type, and the adapter interface.
The adapter that merges Wiki rule pages with rule source text and filters by
effective window arrives with the Wiki migration (Stage 4.3). Until then the
runtime adapter raises `ToolNotReady`, which the executor reports as an error -
never as "no matching policy", and never with placeholder policy text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
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

    def __post_init__(self) -> None:
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
        _require_aware_iso("PolicyRecord.effective_from", self.effective_from)
        if self.effective_to is not None:
            _require_aware_iso("PolicyRecord.effective_to", self.effective_to)
            if datetime.fromisoformat(self.effective_to) <= datetime.fromisoformat(
                self.effective_from
            ):
                raise ValueError(
                    "PolicyRecord.effective_to must be later than effective_from"
                )


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
