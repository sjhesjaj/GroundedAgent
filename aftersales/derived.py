"""Deterministic derived facts: business evidence + structured policy + Clock.

"签收已 8 天", "在退货时限内", "目标规格有库存", "订单与物流状态冲突" are
computed here, never by a model (design §6, D5, D26). Stage 6's Policy Guard is
meant to reuse the same functions, so answers and actions share one rule set.

Every function:

- reads structured fields only - `metadata["value"]` of `BusinessEvidence` and
  the validated params of a `PolicyRecord`; never `content`, a title, or text;
- takes business time only from the injected `Clock`;
- returns one `DerivedEvidence` whose `input_refs` are the `evidence_ref()` of
  every direct input and whose `policy_refs` name every rule version applied;
- is pure: same inputs + same Clock reading -> byte-identical output.

Two ways to not produce a fact, kept apart:

- `ValueError`: the input is malformed or inconsistent (wrong field, a value of
  the wrong type, an observation later than the Clock). A programming or data
  fault; fail loudly.
- `NotDerivable(code)`: the input is well formed but does not establish the
  fact (not delivered yet, a delivery later than the Clock, rule not in
  force, category out of scope, incomplete logistics, order and logistics
  read at different instants, an item and a delivery not structurally linked
  to the same order, an item whose order shipped in several packages). No derived evidence is produced - in particular, an
  incomplete picture never yields a `business_state_conflict`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from orchestration.contracts import (
    BUSINESS_STATE_CONFLICT_FACT,
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    DerivedEvidence,
    SourceType,
    evidence_ref,
)

from .clock import Clock, require_aware
from .policy import (
    CountingRule,
    PolicyRecord,
    PolicyRuleType,
    is_policy_in_effect,
    parse_utc_offset,
    policy_applies_to_category,
    policy_ref,
    window_params,
)

DERIVATION_SOURCE = "aftersales-derivation"
# Derived facts describe current operational state, like the business
# evidence they come from; they never decide what a policy means.
DERIVED_AUTHORITY = 100
DERIVED_AUTHORITY_SCOPE = "current_operational_state"

FACT_DAYS_SINCE_DELIVERY = "days_since_delivery"
FACT_WITHIN_RETURN_WINDOW = "within_return_window"
FACT_WITHIN_EXCHANGE_WINDOW = "within_exchange_window"
FACT_INVENTORY_AVAILABLE = "inventory_available"
FACT_BUSINESS_STATE_CONFLICT = BUSINESS_STATE_CONFLICT_FACT

# Bump the revision whenever a rule's semantics change.
DERIVATION_DAYS_SINCE_DELIVERY = "aftersales.days_since_delivery/v1"
DERIVATION_WINDOW_ELIGIBILITY = "aftersales.window_eligibility/v1"
DERIVATION_INVENTORY_AVAILABLE = "aftersales.inventory_available/v1"
DERIVATION_BUSINESS_STATE_CONFLICT = "aftersales.business_state_conflict/v1"

WINDOW_FACTS = {
    PolicyRuleType.RETURN_WINDOW: FACT_WITHIN_RETURN_WINDOW,
    PolicyRuleType.EXCHANGE_WINDOW: FACT_WITHIN_EXCHANGE_WINDOW,
}

# NotDerivable codes. Stable, and never carry a value.
NOT_DERIVABLE_START_EVENT_ABSENT = "start_event_absent"
NOT_DERIVABLE_DELIVERY_IN_FUTURE = "delivery_in_future"
NOT_DERIVABLE_POLICY_NOT_IN_EFFECT = "policy_not_in_effect"
NOT_DERIVABLE_CATEGORY_REQUIRED = "category_evidence_required"
NOT_DERIVABLE_CATEGORY_OUT_OF_SCOPE = "category_not_in_scope"
NOT_DERIVABLE_LOGISTICS_INCOMPLETE = "logistics_evidence_incomplete"
NOT_DERIVABLE_OBSERVATION_TIME_MISMATCH = "observation_time_mismatch"
NOT_DERIVABLE_ORDER_LINK_MISSING = "order_link_missing"
NOT_DERIVABLE_ORDER_LINK_MISMATCH = "order_link_mismatch"
NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS = "item_package_link_ambiguous"

# The structural relation (BusinessEvidence.relations) naming a record's order.
ORDER_RELATION = "order_id"

NOT_DERIVABLE_CODES = frozenset(
    {
        NOT_DERIVABLE_START_EVENT_ABSENT,
        NOT_DERIVABLE_DELIVERY_IN_FUTURE,
        NOT_DERIVABLE_POLICY_NOT_IN_EFFECT,
        NOT_DERIVABLE_CATEGORY_REQUIRED,
        NOT_DERIVABLE_CATEGORY_OUT_OF_SCOPE,
        NOT_DERIVABLE_LOGISTICS_INCOMPLETE,
        NOT_DERIVABLE_OBSERVATION_TIME_MISMATCH,
        NOT_DERIVABLE_ORDER_LINK_MISSING,
        NOT_DERIVABLE_ORDER_LINK_MISMATCH,
        NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS,
    }
)


class NotDerivable(Exception):
    """Well-formed input that does not establish the fact. Carries a code only."""

    def __init__(self, code: str) -> None:
        if code not in NOT_DERIVABLE_CODES:
            raise ValueError("unknown NotDerivable code")
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------
# Reading inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    evidence: BusinessEvidence
    record_id: str
    value: object
    observed_at: datetime


def _read_field(path: str, evidence: object, entity: str, field: str, now: datetime) -> _Field:
    """One business field, checked against its own locator and the Clock.

    `BusinessEvidence` is validated on construction but not frozen, so the
    parts read here are checked again: metadata and locator must agree, and
    the observation must not postdate the derivation instant.
    """
    if not isinstance(evidence, BusinessEvidence):
        raise ValueError(path + " must be a BusinessEvidence, got " + type(evidence).__name__)
    if evidence.source_type is not SourceType.BUSINESS:
        raise ValueError(path + " must be business evidence")
    metadata = evidence.metadata
    if not isinstance(metadata, dict):
        raise ValueError(path + ".metadata must be a dict")
    if metadata.get("entity") != entity or metadata.get("field") != field:
        raise ValueError(path + " must be the " + entity + "#" + field + " field")
    record_id = metadata.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError(path + ".metadata.record_id must be a non-empty string")
    if evidence.locator != entity + ":" + record_id + "#" + field:
        raise ValueError(path + ".locator disagrees with its metadata")
    if "value" not in metadata:
        raise ValueError(path + ".metadata has no value")
    observed_at = _parse_aware(path + ".observed_at", evidence.observed_at)
    if observed_at > now:
        raise ValueError(path + " was observed after the Clock's current instant")
    return _Field(evidence, record_id, metadata["value"], observed_at)


def _order_relation(path: str, field: _Field) -> str:
    """The order a business field belongs to, from its source-produced relations.

    Read only from `BusinessEvidence.relations` - never from content, locator,
    or labels. A malformed relation map is a data fault (`ValueError`); a
    well-formed map without the order link cannot prove same-order membership
    (`NotDerivable(order_link_missing)`).
    """
    relations = field.evidence.relations
    if not isinstance(relations, dict) or not all(
        isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
        for key, value in relations.items()
    ):
        raise ValueError(path + ".relations must map non-empty strings to non-empty strings")
    if ORDER_RELATION not in relations:
        raise NotDerivable(NOT_DERIVABLE_ORDER_LINK_MISSING)
    return relations[ORDER_RELATION]


def _parse_aware(path: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(path + " must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(path + " must be an ISO-8601 timestamp") from None
    return require_aware(path, parsed)


def _now(clock: object) -> datetime:
    if not isinstance(clock, Clock):
        raise ValueError("clock must implement Clock, got " + type(clock).__name__)
    return require_aware("clock.now()", clock.now())


def _derived(
    *,
    fact_key: str,
    subject: str,
    value: object,
    details: dict[str, object],
    inputs: Sequence[BusinessEvidence],
    policies: Sequence[PolicyRecord] = (),
    derivation_id: str,
    now: datetime,
) -> DerivedEvidence:
    return DerivedEvidence(
        # Mechanical, not answer wording: generation renders its own text.
        content=derivation_id + ": " + subject + "#" + fact_key + " = " + _render(value),
        source_type=SourceType.DERIVED,
        source=DERIVATION_SOURCE,
        locator=subject + "#" + fact_key,
        version=None,
        observed_at=now.isoformat(),
        authority=DERIVED_AUTHORITY,
        confidence=None,
        metadata={"authority_scope": DERIVED_AUTHORITY_SCOPE},
        fact_key=fact_key,
        subject=subject,
        value=value,
        details=details,
        input_refs=tuple(evidence_ref(item) for item in inputs),
        policy_refs=tuple(policy_ref(record) for record in policies),
        derivation_id=derivation_id,
    )


def _render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------
# A. Elapsed days
# --------------------------------------------------------------------------


def _local_date(instant: datetime, utc_offset: str) -> date:
    return instant.astimezone(parse_utc_offset("utc_offset", utc_offset)).date()


def elapsed_natural_days(
    start: datetime, now: datetime, *, counting_rule: CountingRule, utc_offset: str
) -> int:
    """Days elapsed since `start` under `counting_rule`, as of `now`.

    NATURAL_DAYS_FROM_NEXT_DAY: both instants are converted to `utc_offset`
    and their calendar dates subtracted. The start-event day is 0, the next
    calendar day is 1. Time of day never matters: 23:59 and 00:01 on the same
    date are the same day, and 23:59 -> 00:01 is one day.
    """
    start = require_aware("start", start)
    now = require_aware("now", now)
    if counting_rule is not CountingRule.NATURAL_DAYS_FROM_NEXT_DAY:
        raise ValueError("unsupported counting_rule")
    if start > now:
        raise ValueError("start must not be later than now")
    return (_local_date(now, utc_offset) - _local_date(start, utc_offset)).days


def _delivery(path: str, delivered_at: object, now: datetime) -> tuple[_Field, datetime]:
    field = _read_field(path, delivered_at, "logistics", "delivered_at", now)
    if field.value is None:
        # Not delivered yet: there is no start event to count from.
        raise NotDerivable(NOT_DERIVABLE_START_EVENT_ABSENT)
    instant = _parse_aware(path + ".metadata.value", field.value)
    # Absolute instants first, calendar dates only after: a delivery later
    # today (same local date) is still in the future and must not count as
    # day 0. `delivered_at == now` is allowed and is day 0.
    if instant > now:
        raise NotDerivable(NOT_DERIVABLE_DELIVERY_IN_FUTURE)
    return field, instant


def derive_days_since_delivery(
    delivered_at: BusinessEvidence,
    *,
    clock: Clock,
    counting_rule: CountingRule,
    utc_offset: str,
) -> DerivedEvidence:
    """`days_since_delivery` of one package: 0 on the delivery date, then +1
    per calendar date at `utc_offset`."""
    now = _now(clock)
    if not isinstance(counting_rule, CountingRule):
        raise ValueError("counting_rule must be a CountingRule")
    parse_utc_offset("utc_offset", utc_offset)
    field, delivered = _delivery("delivered_at", delivered_at, now)
    days = elapsed_natural_days(
        delivered, now, counting_rule=counting_rule, utc_offset=utc_offset
    )
    return _derived(
        fact_key=FACT_DAYS_SINCE_DELIVERY,
        subject="logistics:" + field.record_id,
        value=days,
        details={
            "counting_rule": counting_rule.value,
            "utc_offset": utc_offset,
            "delivery_date": _local_date(delivered, utc_offset).isoformat(),
            "as_of_date": _local_date(now, utc_offset).isoformat(),
        },
        inputs=(field.evidence,),
        derivation_id=DERIVATION_DAYS_SINCE_DELIVERY,
        now=now,
    )


# --------------------------------------------------------------------------
# B. Return / exchange window eligibility
# --------------------------------------------------------------------------


def _read_category(category: object, now: datetime) -> _Field:
    item = _read_field("category", category, "order_item", "category", now)
    if not isinstance(item.value, str) or not item.value.strip():
        raise ValueError("category.metadata.value must be a non-empty string")
    return item


def _window_rule_gate(policy: PolicyRecord, category: object, now: datetime) -> _Field | None:
    """Rule in force, then scope. Returns the category field when given."""
    item = None if category is None else _read_category(category, now)
    if not is_policy_in_effect(policy, now):
        raise NotDerivable(NOT_DERIVABLE_POLICY_NOT_IN_EFFECT)
    if policy.scope:
        if item is None:
            raise NotDerivable(NOT_DERIVABLE_CATEGORY_REQUIRED)
        if not policy_applies_to_category(policy, item.value):
            raise NotDerivable(NOT_DERIVABLE_CATEGORY_OUT_OF_SCOPE)
    return item


def derive_window_eligibility(
    delivered_at: BusinessEvidence,
    policy: PolicyRecord,
    *,
    clock: Clock,
    category: BusinessEvidence | None = None,
) -> DerivedEvidence:
    """`within_return_window` / `within_exchange_window` under one rule version.

    - The rule must be a window rule in force at `clock.now()`
      ([effective_from, effective_to)); otherwise `NotDerivable`.
    - A rule with a non-empty scope needs the item's `order_item#category`
      evidence, and the category must be listed verbatim in the scope.
    - Inside the window iff `days_since_delivery <= window_days`: the last
      eligible date is delivery date + window_days (local), inclusive.

    The subject is the order item when category evidence is given (eligibility
    is per item), otherwise the delivered package.

    Whenever `category` is given - whatever the rule's scope - it must be
    structurally linked to the same order as `delivered_at`
    (`relations["order_id"]` on both): a missing link is
    `NotDerivable(order_link_missing)`, different orders are
    `NotDerivable(order_link_mismatch)`. No window fact is produced from an
    item of one order and a delivery of another.

    This is the low-level primitive for ONE delivery fact. Same order does not
    mean same package: the schema has no order_item -> tracking_no mapping.
    Stage 4.4 / Stage 5 must not pick one package out of a multi-package
    observation and pass it here for an item-level answer; item-level
    eligibility goes through `derive_item_window_eligibility` (or an
    equivalent ambiguity gate).
    """
    now = _now(clock)
    params = window_params(policy)
    fact_key = WINDOW_FACTS[policy.rule_type]
    item = _window_rule_gate(policy, category, now)

    field, delivered = _delivery("delivered_at", delivered_at, now)
    order_id: str | None = None
    if item is not None:
        item_order = _order_relation("category", item)
        delivery_order = _order_relation("delivered_at", field)
        if item_order != delivery_order:
            raise NotDerivable(NOT_DERIVABLE_ORDER_LINK_MISMATCH)
        order_id = item_order
    days = elapsed_natural_days(
        delivered, now, counting_rule=params.counting_rule, utc_offset=params.utc_offset
    )
    delivery_date = _local_date(delivered, params.utc_offset)
    details: dict[str, object] = {
        "days_since_delivery": days,
        "window_days": params.window_days,
        "start_event": params.start_event.value,
        "counting_rule": params.counting_rule.value,
        "utc_offset": params.utc_offset,
        "delivery_date": delivery_date.isoformat(),
        "last_eligible_date": (delivery_date + timedelta(days=params.window_days)).isoformat(),
        "as_of_date": _local_date(now, params.utc_offset).isoformat(),
        "policy_scope": list(policy.scope),
    }
    inputs: list[BusinessEvidence] = [field.evidence]
    if item is not None:
        details["category"] = item.value
        # Verified above; provenance for traces, not an authorization.
        details["order_id"] = order_id
        inputs.append(item.evidence)
    return _derived(
        fact_key=fact_key,
        subject=(
            "order_item:" + item.record_id if item is not None
            else "logistics:" + field.record_id
        ),
        value=days <= params.window_days,
        details=details,
        inputs=inputs,
        policies=(policy,),
        derivation_id=DERIVATION_WINDOW_ELIGIBILITY,
        now=now,
    )


def derive_item_window_eligibility(
    delivered_at_evidence: Sequence[BusinessEvidence],
    policy: PolicyRecord,
    *,
    clock: Clock,
    category: BusinessEvidence,
) -> DerivedEvidence:
    """Item-level `within_return_window` / `within_exchange_window`.

    The entry Stage 4.4 / Stage 5 must use for an order item's eligibility.
    `delivered_at_evidence` is every `logistics#delivered_at` of the item's
    order, from one `get_logistics` observation - pass the whole observation,
    never a package chosen from it.

    The schema links packages to orders (order_id -> 0..N tracking_no) but not
    items to packages. So:

    - no package: `NotDerivable(start_event_absent)`;
    - exactly one package: it is the only possible start event, and the
      low-level `derive_window_eligibility` decides;
    - several packages: `NotDerivable(item_package_link_ambiguous)`. Which
      package carries the item is unknown, so no delivery is attributed to it:
      not the delivered one, not the earliest or latest, not by tracking
      number, item order or SKU - even when every choice would give the same
      answer.

    Before counting, every package must carry the same `order_id` relation as
    the category (missing -> `order_link_missing`, another order ->
    `order_link_mismatch`), all packages must come from one observation, and
    the rule / scope gate of the low-level primitive applies first.
    """
    now = _now(clock)
    window_params(policy)
    if category is None:
        raise ValueError("category is required for item-level eligibility")
    if not isinstance(delivered_at_evidence, (list, tuple)):
        raise ValueError("delivered_at_evidence must be a list or tuple of BusinessEvidence")
    item = _window_rule_gate(policy, category, now)
    item_order = _order_relation("category", item)

    packages: list[_Field] = []
    observations: set[tuple[object, object]] = set()
    for index, evidence in enumerate(delivered_at_evidence):
        path = "delivered_at_evidence[" + str(index) + "]"
        field = _read_field(path, evidence, "logistics", "delivered_at", now)
        if any(package.record_id == field.record_id for package in packages):
            raise ValueError(path + " repeats a package")
        if _order_relation(path, field) != item_order:
            raise NotDerivable(NOT_DERIVABLE_ORDER_LINK_MISMATCH)
        packages.append(field)
        observations.add((evidence.observed_at, evidence.metadata.get(OBSERVATION_ID_KEY)))
    if len(observations) > 1:
        raise ValueError("delivered_at_evidence must come from a single observation")

    if not packages:
        raise NotDerivable(NOT_DERIVABLE_START_EVENT_ABSENT)
    if len(packages) > 1:
        raise NotDerivable(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS)
    return derive_window_eligibility(packages[0].evidence, policy, clock=clock, category=category)


# --------------------------------------------------------------------------
# C. Inventory availability
# --------------------------------------------------------------------------


def derive_inventory_available(
    available_qty: BusinessEvidence, *, clock: Clock
) -> DerivedEvidence:
    """`inventory_available`: 0 -> false, > 0 -> true. Nothing else is accepted."""
    now = _now(clock)
    field = _read_field("available_qty", available_qty, "inventory", "available_qty", now)
    quantity = field.value
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ValueError("available_qty.metadata.value must be a non-negative integer")
    return _derived(
        fact_key=FACT_INVENTORY_AVAILABLE,
        subject="inventory:" + field.record_id,
        value=quantity > 0,
        details={"available_qty": quantity},
        inputs=(field.evidence,),
        derivation_id=DERIVATION_INVENTORY_AVAILABLE,
        now=now,
    )


# --------------------------------------------------------------------------
# D. Order / logistics state conflict
# --------------------------------------------------------------------------

ORDER_STATUSES = frozenset({"待付款", "已付款", "已发货", "已签收", "已完成", "已取消"})
PACKAGE_STATUSES = frozenset({"运输中", "派送中", "已签收", "异常", "退回"})

_MOVING = frozenset({"运输中", "派送中", "已签收"})
_UNDELIVERED_IN_TRANSIT = frozenset({"运输中", "派送中"})


@dataclass(frozen=True)
class StateConflictRule:
    """Order status in `order_statuses` and (`any` / `all`) packages in
    `package_statuses` is a contradiction."""

    rule_id: str
    order_statuses: frozenset[str]
    quantifier: str
    package_statuses: frozenset[str]


# Explicit and conservative. Only combinations that cannot both be true fire.
# Deliberately *not* listed, because they are not provably contradictory:
# 已发货 with some but not all packages 已签收 (partial delivery); anything with
# 异常 or 退回; 已完成 / 已取消 orders (returns and cancellations have their own
# shipments).
STATE_CONFLICT_RULES = (
    # A07: the order is still at the shipping stage, but every package has
    # been signed for.
    StateConflictRule(
        rule_id="order_shipped_all_packages_delivered",
        order_statuses=frozenset({"已发货"}),
        quantifier="all",
        package_statuses=frozenset({"已签收"}),
    ),
    # A07, the other direction: the order says signed for, but a package is
    # still moving towards the customer.
    StateConflictRule(
        rule_id="order_delivered_package_in_transit",
        order_statuses=frozenset({"已签收"}),
        quantifier="any",
        package_statuses=_UNDELIVERED_IN_TRANSIT,
    ),
    # The order has not shipped, but a package is moving or delivered.
    StateConflictRule(
        rule_id="order_unshipped_package_moving",
        order_statuses=frozenset({"待付款", "已付款"}),
        quantifier="any",
        package_statuses=_MOVING,
    ),
)


def _fires(rule: StateConflictRule, order_status: str, package_statuses: list[str]) -> bool:
    if order_status not in rule.order_statuses:
        return False
    if rule.quantifier == "all":
        return all(status in rule.package_statuses for status in package_statuses)
    return any(status in rule.package_statuses for status in package_statuses)


def derive_business_state_conflict(
    order_status: BusinessEvidence,
    logistics: Sequence[BusinessEvidence],
    *,
    clock: Clock,
) -> DerivedEvidence:
    """`business_state_conflict` for one order, from the rule table above.

    `logistics` is the evidence of one `get_logistics` observation of this
    order; per package the `status` and `order_id` fields are used and the
    rest is ignored. Every package must have both, must name this order, and
    all of it must come from a single observation (one observed_at, one
    observation_id). The order status must have been observed at the same
    business instant as the logistics; otherwise the reads cannot show that
    both states held at once, and the result is
    `NotDerivable(observation_time_mismatch)` - neither a conflict nor a
    "no conflict".

    Caller contract: pass the whole observation. A package filtered out
    before this call cannot be detected here.

    No packages, or a package missing a field -> `NotDerivable`: an incomplete
    picture never produces a conflict, and never a "no conflict" either.
    """
    now = _now(clock)
    order = _read_field("order_status", order_status, "order", "status", now)
    if order.value not in ORDER_STATUSES:
        raise ValueError("order_status.metadata.value is not an order status")
    if not isinstance(logistics, (list, tuple)):
        raise ValueError("logistics must be a list or tuple of BusinessEvidence")

    statuses: dict[str, _Field] = {}
    order_ids: dict[str, _Field] = {}
    observations: set[tuple[str, object]] = set()
    for index, item in enumerate(logistics):
        path = "logistics[" + str(index) + "]"
        if not isinstance(item, BusinessEvidence):
            raise ValueError(path + " must be a BusinessEvidence, got " + type(item).__name__)
        field_name = item.metadata.get("field") if isinstance(item.metadata, dict) else None
        if field_name not in ("status", "order_id"):
            continue
        field = _read_field(path, item, "logistics", field_name, now)
        target = statuses if field_name == "status" else order_ids
        if field.record_id in target:
            raise ValueError(path + " repeats a package field")
        target[field.record_id] = field
        observations.add((item.observed_at, item.metadata.get(OBSERVATION_ID_KEY)))

    if not statuses and not order_ids:
        raise NotDerivable(NOT_DERIVABLE_LOGISTICS_INCOMPLETE)
    if set(statuses) != set(order_ids):
        raise NotDerivable(NOT_DERIVABLE_LOGISTICS_INCOMPLETE)
    if len(observations) != 1:
        raise ValueError("logistics must come from a single observation")
    for tracking_no, field in order_ids.items():
        if field.value != order.record_id:
            raise ValueError("logistics evidence belongs to a different order")
        status = statuses[tracking_no].value
        if status not in PACKAGE_STATUSES:
            raise ValueError("logistics status is not a package status")
    # The order and the logistics must describe one business instant: an order
    # read at 09:59 and logistics read at 10:00 may just show an ordinary
    # transition in between. Compared as absolute instants, so the same moment
    # written at two offsets agrees. The two reads are separate tool calls, so
    # their observation_ids are not compared.
    logistics_at = next(iter(statuses.values())).observed_at
    if order.observed_at != logistics_at:
        raise NotDerivable(NOT_DERIVABLE_OBSERVATION_TIME_MISMATCH)

    packages = sorted(statuses)
    package_statuses = [statuses[tracking_no].value for tracking_no in packages]
    fired = [
        rule.rule_id
        for rule in STATE_CONFLICT_RULES
        if _fires(rule, order.value, package_statuses)
    ]
    inputs = [order.evidence]
    for tracking_no in packages:
        inputs.append(statuses[tracking_no].evidence)
        inputs.append(order_ids[tracking_no].evidence)
    return _derived(
        fact_key=FACT_BUSINESS_STATE_CONFLICT,
        subject="order:" + order.record_id,
        value=bool(fired),
        details={
            "order_status": order.value,
            "package_statuses": {
                tracking_no: statuses[tracking_no].value for tracking_no in packages
            },
            "fired_rules": fired,
        },
        inputs=inputs,
        derivation_id=DERIVATION_BUSINESS_STATE_CONFLICT,
        now=now,
    )
