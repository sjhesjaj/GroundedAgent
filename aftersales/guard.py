"""The Stage 6 Policy Guard: capture-then-decide (docs/v2/stage6-design.md §6).

Two layers, frozen at tag v2-stage6-design:

- `Guard.capture(action, context, catalog, *, txn_now, exclude_pending_id)`
  does the Guard's own I/O and nothing else: one GuardStateReader pass inside
  the caller's open BEGIN IMMEDIATE transaction, exactly one
  `catalog.snapshot()`, and the candidate `GuardSnapshot`. It never reads a
  Clock and never decides.
- `Guard.decide(action, state, policy, risk, txn_now)` is pure. No connection,
  no TrustedExecutionContext, no catalog I/O, no Clock: the derived-fact
  functions that take a `clock` get `FixedClock(txn_now)`, an in-memory value
  built from the explicit argument. It walks the ordered prerequisite matrix
  of §6.4; the first failing check decides the reason code.

Guard inputs are exhaustive: the action and its validated args, the trusted
identity (an SQL predicate inside capture only), the Guard's own structured
reads, the explicit txn_now, the captured published policy snapshot, the
action risk policy, and facts derived from those. User text, model output,
claimed roles, observations and business free text have no parameter here.

Infrastructure trouble is `GuardFailure` (FAILED), never a decision.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from typing import Mapping

from .action_errors import (
    FAILURE_GUARD_INTERNAL,
    FAILURE_POLICY_UNAVAILABLE,
    FAILURE_STATE_MALFORMED,
    GuardFailure,
)
from .action_policy import RISK_ALLOW, RISK_REQUIRE_APPROVAL, ActionRiskPolicy
from .actions import (
    ACTION_SPEC_VERSION,
    CREATE_EXCHANGE,
    CREATE_RETURN,
    ESCALATE_TO_HUMAN,
    REASON_HANDOFF_TRIGGER,
    ValidatedAction,
)
from .clock import FixedClock, require_aware
from .context import TrustedExecutionContext
from .derived import (
    NotDerivable,
    derive_business_state_conflict,
    derive_inventory_available,
    derive_item_window_eligibility,
)
from .guard_state import ORDER_STATUSES, GuardState, GuardStateReader
from .policy import PolicyRecord, PolicyRuleType, policy_ref
from .policy_catalog import CatalogSnapshot, PolicyPrecedenceConflict, select_policies
from .policy_source import canonical
from .schema import CaseStatus, CaseType, OrderStatus

GUARD_SNAPSHOT_SCHEMA = "s6-guard-snapshot/1"


class GuardDecisionKind(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


# --------------------------------------------------------------------------
# Reason vocabulary (§6.5), closed
# --------------------------------------------------------------------------

REASON_ORDER_NOT_ACCESSIBLE = "order_not_accessible"
REASON_ORDER_ITEM_NOT_IN_ORDER = "order_item_not_in_order"
REASON_BUSINESS_STATE_CONFLICT = "business_state_conflict"
REASON_ORDER_STATUS_INELIGIBLE = "order_status_ineligible"
REASON_NOT_DELIVERED = "not_delivered"
REASON_DELIVERY_NOT_ESTABLISHED = "delivery_not_established"
REASON_ACTIVE_CASE = "active_after_sales_case_exists"
REASON_ITEM_ALREADY_RETURNED = "item_already_returned"
REASON_PENDING_REQUEST = "pending_request_exists"
REASON_HANDOFF_REQUIRED = "handoff_required"
REASON_NON_RETURNABLE = "non_returnable"
REASON_NO_APPLICABLE_POLICY = "no_applicable_policy"
REASON_POLICY_CONFLICT = "policy_conflict"
REASON_RETURN_WINDOW_CLOSED = "return_window_closed"
REASON_EXCHANGE_WINDOW_CLOSED = "exchange_window_closed"
REASON_EXCHANGE_TARGET_INVALID = "exchange_target_invalid"
REASON_EXCHANGE_TARGET_INCOMPATIBLE = "exchange_target_incompatible"
REASON_INVENTORY_UNAVAILABLE = "inventory_unavailable"
REASON_HANDOFF_NOT_REQUIRED = "handoff_not_required"
REASON_HANDOFF_TICKET_EXISTS = "handoff_ticket_exists"
REASON_RISK_REQUIRES_APPROVAL = "risk_policy_requires_approval"
REASON_RISK_ALLOWS = "risk_policy_allows"

DENY_REASON_CODES = (
    REASON_ORDER_NOT_ACCESSIBLE, REASON_ORDER_ITEM_NOT_IN_ORDER, REASON_BUSINESS_STATE_CONFLICT,
    REASON_ORDER_STATUS_INELIGIBLE, REASON_NOT_DELIVERED, REASON_DELIVERY_NOT_ESTABLISHED,
    REASON_ACTIVE_CASE, REASON_ITEM_ALREADY_RETURNED, REASON_PENDING_REQUEST,
    REASON_HANDOFF_REQUIRED, REASON_NON_RETURNABLE, REASON_NO_APPLICABLE_POLICY,
    REASON_POLICY_CONFLICT, REASON_RETURN_WINDOW_CLOSED, REASON_EXCHANGE_WINDOW_CLOSED,
    REASON_EXCHANGE_TARGET_INVALID, REASON_EXCHANGE_TARGET_INCOMPATIBLE,
    REASON_INVENTORY_UNAVAILABLE, REASON_HANDOFF_NOT_REQUIRED, REASON_HANDOFF_TICKET_EXISTS,
)
REASON_CODES_BY_DECISION: Mapping[GuardDecisionKind, frozenset[str]] = {
    GuardDecisionKind.DENY: frozenset(DENY_REASON_CODES),
    GuardDecisionKind.ALLOW: frozenset({REASON_RISK_ALLOWS}),
    GuardDecisionKind.REQUIRE_APPROVAL: frozenset({REASON_RISK_REQUIRES_APPROVAL}),
}
GUARD_REASON_CODES = frozenset().union(*REASON_CODES_BY_DECISION.values())

UNDELIVERED_ORDER_STATUSES = frozenset({
    OrderStatus.PENDING_PAYMENT.value, OrderStatus.PAID.value, OrderStatus.SHIPPED.value,
})
ACTIVE_CASE_STATUSES = frozenset({CaseStatus.PENDING.value, CaseStatus.IN_PROGRESS.value})
OPEN_TICKET_STATUSES = frozenset({"待处理", "处理中"})

POLICY_REF_PATTERN = re.compile(r"^policy:[^@#\s]+@[^@#\s]+#[^@#\s]+$")
RULE_TYPE_VALUES = frozenset(item.value for item in PolicyRuleType)


# --------------------------------------------------------------------------
# Typed records
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class GuardSnapshot:
    """The comparable candidate snapshot; persisted format in §12.2."""

    schema: str
    action_name: str
    evaluated_at: str
    records: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    policy_build_id: str
    action_spec_version: str
    risk_policy_version: str

    def comparable(self) -> tuple:
        return (self.records, self.policy_build_id, self.action_spec_version,
                self.risk_policy_version)


@dataclass(frozen=True, kw_only=True)
class GuardCapture:
    action: ValidatedAction
    txn_now: datetime
    state: GuardState
    policy: CatalogSnapshot
    candidate_snapshot: GuardSnapshot


_FLAG_FIELDS = (
    "business_state_conflict", "delivery_established", "within_window", "active_case_present",
    "item_returned_before", "other_pending_present", "handoff_routed", "non_returnable",
    "variant_compatible", "inventory_available", "inventory_sufficient", "open_ticket_present",
)


@dataclass(frozen=True, kw_only=True)
class GuardFacts:
    """Closed, typed audit facts. None = the check did not run.

    The only strings are a closed-vocabulary order status and policy refs that
    are format-checked here and membership-checked against the capture's
    CatalogSnapshot by `decide`. No category, SKU, product name, case reason,
    carrier or customer id can be represented.
    """

    order_status: str | None = None
    package_count: int | None = None
    days_since_delivery: int | None = None
    business_state_conflict: bool | None = None
    delivery_established: bool | None = None
    within_window: bool | None = None
    active_case_present: bool | None = None
    item_returned_before: bool | None = None
    other_pending_present: bool | None = None
    handoff_routed: bool | None = None
    non_returnable: bool | None = None
    variant_compatible: bool | None = None
    inventory_available: bool | None = None
    inventory_sufficient: bool | None = None
    open_ticket_present: bool | None = None
    selected_policy_refs: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())

    def __post_init__(self) -> None:
        if self.order_status is not None and self.order_status not in ORDER_STATUSES:
            raise ValueError("GuardFacts.order_status is not an order status")
        for name in ("package_count", "days_since_delivery"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or value < 0):
                raise ValueError("GuardFacts." + name + " must be a non-negative integer")
        for name in _FLAG_FIELDS:
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError("GuardFacts." + name + " must be a bool or None")
        refs = self.selected_policy_refs
        if not isinstance(refs, tuple):
            raise ValueError("GuardFacts.selected_policy_refs must be a tuple")
        rule_types = [entry[0] if isinstance(entry, tuple) and len(entry) == 2 else None
                      for entry in refs]
        if (any(rule_type not in RULE_TYPE_VALUES for rule_type in rule_types)
                or rule_types != sorted(rule_types) or len(set(rule_types)) != len(rule_types)):
            raise ValueError("GuardFacts.selected_policy_refs must be sorted, distinct rule types")
        for _, items in refs:
            if not isinstance(items, tuple) or not all(
                    isinstance(item, str) and POLICY_REF_PATTERN.match(item) for item in items):
                raise ValueError("GuardFacts.selected_policy_refs holds a malformed policy ref")

    def policy_refs(self) -> frozenset[str]:
        return frozenset(item for _, items in self.selected_policy_refs for item in items)

    def to_record(self) -> dict[str, object]:
        """The only persisted form of the facts: closed keys, plain JSON values."""
        record: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "selected_policy_refs":
                value = [[rule_type, list(items)] for rule_type, items in value]
            record[item.name] = value
        return record


GUARD_FACT_FIELDS = tuple(item.name for item in fields(GuardFacts))


@dataclass(frozen=True, kw_only=True)
class GuardDecision:
    decision: GuardDecisionKind
    reason_code: str
    facts: GuardFacts

    def __post_init__(self) -> None:
        if not isinstance(self.decision, GuardDecisionKind):
            raise ValueError("GuardDecision.decision must be a GuardDecisionKind")
        if self.reason_code not in REASON_CODES_BY_DECISION[self.decision]:
            raise ValueError("GuardDecision.reason_code does not belong to its decision")
        if type(self.facts) is not GuardFacts:
            raise ValueError("GuardDecision.facts must be GuardFacts")


def snapshot_document(snapshot: GuardSnapshot, decision: GuardDecision) -> str:
    """The persisted `s6-guard-snapshot/1` JSON: candidate snapshot + decision."""
    return canonical({
        "schema": snapshot.schema,
        "action_name": snapshot.action_name,
        "evaluated_at": snapshot.evaluated_at,
        "records": {table: {pk: version for pk, version in rows}
                    for table, rows in snapshot.records},
        "policy_build_id": snapshot.policy_build_id,
        "action_spec_version": snapshot.action_spec_version,
        "risk_policy_version": snapshot.risk_policy_version,
        "decision": {
            "decision": decision.decision.value,
            "reason_code": decision.reason_code,
            "facts": decision.facts.to_record(),
        },
    })


def snapshot_sha256(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The Guard
# --------------------------------------------------------------------------


class _Denied(Exception):
    """Internal control flow of decide: the first failing prerequisite."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class Guard:
    """capture (own reads, one policy snapshot) then decide (pure)."""

    def __init__(self, risk_policy: ActionRiskPolicy, reader: GuardStateReader | None = None) -> None:
        if not isinstance(risk_policy, ActionRiskPolicy):
            raise ValueError("risk_policy must be an ActionRiskPolicy")
        self._risk_policy = risk_policy
        self._reader = GuardStateReader() if reader is None else reader

    @property
    def risk_policy(self) -> ActionRiskPolicy:
        return self._risk_policy

    def capture(self, action: ValidatedAction, context: TrustedExecutionContext,
                catalog: object, *, txn_now: datetime,
                exclude_pending_id: str | None) -> GuardCapture:
        """One GuardStateReader pass, exactly one catalog.snapshot(), no decision."""
        txn_now = require_aware("txn_now", txn_now)
        state = self._reader.read(action, context, txn_now=txn_now,
                                  exclude_pending_id=exclude_pending_id)
        try:
            policy = catalog.snapshot()
        except Exception:
            # Unreadable, retracted or incompatible publication (typically
            # PolicyCatalogUnavailable): an infrastructure failure, never "no rule".
            raise GuardFailure(FAILURE_POLICY_UNAVAILABLE) from None
        if not isinstance(policy, CatalogSnapshot):
            raise GuardFailure(FAILURE_POLICY_UNAVAILABLE)
        candidate = GuardSnapshot(
            schema=GUARD_SNAPSHOT_SCHEMA,
            action_name=action.action_name,
            evaluated_at=txn_now.isoformat(),
            records=state.versions,
            policy_build_id=policy.build_id,
            action_spec_version=ACTION_SPEC_VERSION,
            risk_policy_version=self._risk_policy.version,
        )
        return GuardCapture(action=action, txn_now=txn_now, state=state, policy=policy,
                            candidate_snapshot=candidate)

    @staticmethod
    def decide(action: ValidatedAction, state: GuardState, policy: CatalogSnapshot,
               risk: ActionRiskPolicy, txn_now: datetime) -> GuardDecision:
        """Pure: the ordered matrix of §6.4 over explicit inputs only."""
        try:
            return _Decision(action, state, policy, risk, txn_now).run()
        except GuardFailure:
            raise
        except Exception:
            # Fail closed: an unexpected error is never ALLOW and never DENY.
            raise GuardFailure(FAILURE_GUARD_INTERNAL) from None


class _Decision:
    """One evaluation of the matrix. Accumulates facts; never performs I/O."""

    def __init__(self, action: ValidatedAction, state: GuardState, policy: CatalogSnapshot,
                 risk: ActionRiskPolicy, txn_now: datetime) -> None:
        if not isinstance(action, ValidatedAction) or not isinstance(state, GuardState):
            raise TypeError("decide needs a ValidatedAction and a GuardState")
        if not isinstance(policy, CatalogSnapshot) or not isinstance(risk, ActionRiskPolicy):
            raise TypeError("decide needs a CatalogSnapshot and an ActionRiskPolicy")
        if state.action_name != action.action_name:
            raise ValueError("state was captured for another action")
        self.action = action
        self.state = state
        self.policy = policy
        self.risk = risk
        self.txn_now = require_aware("txn_now", txn_now)
        # An in-memory value built from the explicit argument; the injected
        # Clock is unreachable from here.
        self.clock = FixedClock(self.txn_now)
        self.facts: dict[str, object] = {}
        self.selected: dict[str, tuple[str, ...]] = {}

    # -- result ------------------------------------------------------------

    def _facts(self) -> GuardFacts:
        refs = tuple((rule_type, self.selected[rule_type]) for rule_type in sorted(self.selected))
        facts = GuardFacts(**self.facts, selected_policy_refs=refs)
        known = {policy_ref(record) for record in self.policy.records}
        if not facts.policy_refs() <= known:
            raise GuardFailure(FAILURE_GUARD_INTERNAL)
        return facts

    def _result(self, decision: GuardDecisionKind, reason_code: str) -> GuardDecision:
        return GuardDecision(decision=decision, reason_code=reason_code, facts=self._facts())

    def run(self) -> GuardDecision:
        try:
            self._check()
        except _Denied as denied:
            return self._result(GuardDecisionKind.DENY, denied.reason_code)
        disposition = self.risk.disposition(self.action.action_name)
        if disposition == RISK_REQUIRE_APPROVAL:
            return self._result(GuardDecisionKind.REQUIRE_APPROVAL, REASON_RISK_REQUIRES_APPROVAL)
        if disposition == RISK_ALLOW:
            return self._result(GuardDecisionKind.ALLOW, REASON_RISK_ALLOWS)
        raise GuardFailure(FAILURE_GUARD_INTERNAL)

    # -- helpers -----------------------------------------------------------

    def _select(self, rule_type: PolicyRuleType, records=None) -> tuple[PolicyRecord, ...]:
        """select_policies on the captured snapshot; a conflict denies here."""
        try:
            return select_policies(
                self.policy.records if records is None else records,
                as_of=self.txn_now, rule_type=rule_type, category=self.state.item.category)
        except PolicyPrecedenceConflict:
            raise _Denied(REASON_POLICY_CONFLICT) from None

    def _handoff(self, trigger: str) -> tuple[PolicyRecord, ...]:
        # Filter by trigger first: handoff rules of different triggers never conflict.
        candidates = [record for record in self.policy.records
                      if record.rule_type is PolicyRuleType.HANDOFF
                      and record.params.get("trigger") == trigger]
        selected = self._select(PolicyRuleType.HANDOFF, candidates)
        self.selected[PolicyRuleType.HANDOFF.value] = tuple(policy_ref(r) for r in selected)
        return selected

    @staticmethod
    def _derived(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except NotDerivable:
            # Excluded by construction (§6.6): an invariant was broken.
            raise GuardFailure(FAILURE_GUARD_INTERNAL) from None
        except ValueError:
            raise GuardFailure(FAILURE_STATE_MALFORMED) from None

    # -- the matrix --------------------------------------------------------

    def _check(self) -> None:
        state = self.state
        # R-1 / E-1 / H-1
        if state.order is None:
            raise _Denied(REASON_ORDER_NOT_ACCESSIBLE)
        self.facts["order_status"] = state.order.status
        # R-2 / E-2 / H-2
        if state.item is None:
            raise _Denied(REASON_ORDER_ITEM_NOT_IN_ORDER)
        if self.action.action_name == ESCALATE_TO_HUMAN:
            self._check_handoff()
        else:
            self._check_after_sales()

    def _check_handoff(self) -> None:
        # H-3
        routed = bool(self._handoff(self.action.args["handoff_trigger"]))
        self.facts["handoff_routed"] = routed
        if not routed:
            raise _Denied(REASON_HANDOFF_NOT_REQUIRED)
        # H-4
        open_ticket = any(ticket.status in OPEN_TICKET_STATUSES for ticket in self.state.tickets)
        self.facts["open_ticket_present"] = open_ticket
        if open_ticket:
            raise _Denied(REASON_HANDOFF_TICKET_EXISTS)

    def _check_after_sales(self) -> None:
        state = self.state
        name = self.action.action_name
        order, item = state.order, state.item
        self.facts["package_count"] = len(state.packages)
        # R-3 / E-3
        if state.packages:
            conflict = self._derived(
                derive_business_state_conflict, state.evidence.order_status,
                state.evidence.logistics, clock=self.clock).value
            self.facts["business_state_conflict"] = conflict
            if conflict:
                raise _Denied(REASON_BUSINESS_STATE_CONFLICT)
        # R-4 / E-4
        if order.status == OrderStatus.CANCELLED.value:
            raise _Denied(REASON_ORDER_STATUS_INELIGIBLE)
        # R-5 / E-5: delivery establishment rule D
        established = self._delivery_established()
        self.facts["delivery_established"] = established is None
        if established is not None:
            raise _Denied(established)
        # R-6 / E-6
        active = any(case.status in ACTIVE_CASE_STATUSES for case in state.item_cases)
        self.facts["active_case_present"] = active
        if active:
            raise _Denied(REASON_ACTIVE_CASE)
        # R-7 / E-7
        returned = any(case.case_type == CaseType.RETURN.value
                       and case.status == CaseStatus.COMPLETED.value for case in state.item_cases)
        self.facts["item_returned_before"] = returned
        if returned:
            raise _Denied(REASON_ITEM_ALREADY_RETURNED)
        # R-8 / E-8
        other = bool(state.other_pendings)
        self.facts["other_pending_present"] = other
        if other:
            raise _Denied(REASON_PENDING_REQUEST)
        # R-9 / E-9
        trigger = REASON_HANDOFF_TRIGGER.get(self.action.args["reason_code"])
        routed = bool(self._handoff(trigger)) if trigger is not None else False
        self.facts["handoff_routed"] = routed
        if routed:
            raise _Denied(REASON_HANDOFF_REQUIRED)
        if name == CREATE_RETURN:
            # R-10
            non_returnable = self._select(PolicyRuleType.NON_RETURNABLE)
            self.selected[PolicyRuleType.NON_RETURNABLE.value] = tuple(
                policy_ref(record) for record in non_returnable)
            self.facts["non_returnable"] = bool(non_returnable)
            if non_returnable:
                raise _Denied(REASON_NON_RETURNABLE)
            window_type, closed = PolicyRuleType.RETURN_WINDOW, REASON_RETURN_WINDOW_CLOSED
        elif name == CREATE_EXCHANGE:
            # E-10
            target = self.action.args["target_sku"]
            if target == item.sku or state.target_inventory is None:
                raise _Denied(REASON_EXCHANGE_TARGET_INVALID)
            groups = {variant.sku: variant.variant_group for variant in state.variants}
            compatible = (item.sku in groups and target in groups
                          and groups[item.sku] == groups[target])
            self.facts["variant_compatible"] = compatible
            if not compatible:
                raise _Denied(REASON_EXCHANGE_TARGET_INCOMPATIBLE)
            window_type, closed = PolicyRuleType.EXCHANGE_WINDOW, REASON_EXCHANGE_WINDOW_CLOSED
        else:
            raise GuardFailure(FAILURE_GUARD_INTERNAL)
        # R-11 / E-11
        windows = self._select(window_type)
        self.selected[window_type.value] = tuple(policy_ref(record) for record in windows)
        if not windows:
            raise _Denied(REASON_NO_APPLICABLE_POLICY)
        # R-12 / E-12: select_policies returns the top tier sorted by policy_ref.
        window = self._derived(
            derive_item_window_eligibility, state.evidence.delivered_at, windows[0],
            clock=self.clock, category=state.evidence.category)
        self.facts["within_window"] = window.value
        self.facts["days_since_delivery"] = window.details["days_since_delivery"]
        if not window.value:
            raise _Denied(closed)
        if name == CREATE_EXCHANGE:
            # E-13
            self.facts["inventory_available"] = self._derived(
                derive_inventory_available, state.evidence.available_qty, clock=self.clock).value
            sufficient = state.target_inventory.available_qty >= item.quantity
            self.facts["inventory_sufficient"] = sufficient
            if not sufficient:
                raise _Denied(REASON_INVENTORY_UNAVAILABLE)

    def _delivery_established(self) -> str | None:
        """Rule D (§6.4). None when delivery is established, else the DENY code."""
        order = self.state.order
        packages = self.state.packages
        if order.status in UNDELIVERED_ORDER_STATUSES:
            return REASON_NOT_DELIVERED
        if len(packages) != 1:
            # None: no start event. Several: which package carries the item is unknown.
            return REASON_DELIVERY_NOT_ESTABLISHED
        delivered_at = packages[0].delivered_at
        if delivered_at is None:
            return REASON_DELIVERY_NOT_ESTABLISHED
        if datetime.fromisoformat(delivered_at) > self.txn_now:
            return REASON_DELIVERY_NOT_ESTABLISHED
        return None
