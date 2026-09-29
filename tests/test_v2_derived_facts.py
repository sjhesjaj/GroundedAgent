"""Deterministic derived facts and the DerivedEvidence contract (Stage 4.2)."""

import dataclasses
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aftersales import derived as derived_module
from aftersales.business_tools import BUSINESS_HANDLERS
from aftersales.clock import FixedClock
from aftersales.demo import DEMO_VIRTUAL_NOW
from aftersales.derived import (
    DERIVATION_BUSINESS_STATE_CONFLICT,
    DERIVATION_WINDOW_ELIGIBILITY,
    FACT_BUSINESS_STATE_CONFLICT,
    FACT_DAYS_SINCE_DELIVERY,
    FACT_INVENTORY_AVAILABLE,
    FACT_WITHIN_EXCHANGE_WINDOW,
    FACT_WITHIN_RETURN_WINDOW,
    NOT_DERIVABLE_CATEGORY_OUT_OF_SCOPE,
    NOT_DERIVABLE_CATEGORY_REQUIRED,
    NOT_DERIVABLE_DELIVERY_IN_FUTURE,
    NOT_DERIVABLE_LOGISTICS_INCOMPLETE,
    NOT_DERIVABLE_OBSERVATION_TIME_MISMATCH,
    NOT_DERIVABLE_POLICY_NOT_IN_EFFECT,
    NOT_DERIVABLE_START_EVENT_ABSENT,
    STATE_CONFLICT_RULES,
    NotDerivable,
    derive_business_state_conflict,
    derive_days_since_delivery,
    derive_inventory_available,
    derive_window_eligibility,
    elapsed_natural_days,
)
from aftersales.executor import execute_tool
from aftersales.policy import CountingRule, PolicyRuleType, policy_ref
from aftersales.registry import build_runtime_registry
from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    DerivedEvidence,
    Evidence,
    SourceType,
    evidence_ref,
)

from tests.v2_support import (
    ORDER_A_DELIVERED,
    ORDER_A_IN_TRANSIT,
    ORDER_A_TWO_PACKAGES,
    SKU_STOCKED,
    SKU_ZERO,
    WINDOW_PARAMS_7D,
    at,
    business_evidence,
    make_context,
    memory_connection,
    window_policy,
)

RULE = CountingRule.NATURAL_DAYS_FROM_NEXT_DAY
V1_EVIDENCE_KEYS = {
    "content", "source_type", "source", "locator", "version", "observed_at",
    "authority", "confidence", "metadata",
}
DERIVED_KEYS = {
    "fact_key", "subject", "value", "details", "input_refs", "policy_refs", "derivation_id",
}


def delivered(value, *, observed_at=None, tracking_no="SF1001"):
    kwargs = {} if observed_at is None else {"observed_at": observed_at}
    return business_evidence("logistics", tracking_no, "delivered_at", value, **kwargs)


def days(delivered_at: str, now: datetime) -> int:
    evidence = delivered(delivered_at, observed_at=now.isoformat())
    result = derive_days_since_delivery(
        evidence, clock=FixedClock(now), counting_rule=RULE, utc_offset="+08:00"
    )
    return result.value


def canonical(evidence: Evidence) -> str:
    return json.dumps(evidence.to_dict(), sort_keys=True, ensure_ascii=False)


def minimal_derived(**overrides) -> DerivedEvidence:
    values = dict(
        content="x",
        source_type=SourceType.DERIVED,
        source="aftersales-derivation",
        locator="logistics:SF1001#days_since_delivery",
        observed_at="2026-11-15T10:00:00+08:00",
        authority=100,
        fact_key="days_since_delivery",
        subject="logistics:SF1001",
        value=8,
        details={"window_days": 7},
        input_refs=("ev-business-abc",),
        derivation_id="test/v1",
    )
    values.update(overrides)
    return DerivedEvidence(**values)


class DerivedEvidenceContractTests(unittest.TestCase):
    def test_is_evidence_and_only_appends_to_the_v1_dict(self):
        item = minimal_derived()
        self.assertIsInstance(item, Evidence)
        payload = item.to_dict()
        self.assertEqual(set(payload), V1_EVIDENCE_KEYS | DERIVED_KEYS)
        self.assertEqual(payload["source_type"], "derived")
        self.assertEqual(payload["input_refs"], ["ev-business-abc"])
        # The V1 shape is untouched.
        plain = Evidence(content="c", source_type=SourceType.DOCUMENT, source="s", authority=80)
        self.assertEqual(set(plain.to_dict()), V1_EVIDENCE_KEYS)
        self.assertEqual(SourceType.DERIVED.value, "derived")

    def test_invalid_derived_evidence_is_rejected(self):
        bad = {
            "wrong_source_type": dict(source_type=SourceType.BUSINESS),
            "no_inputs": dict(input_refs=()),
            "inputs_list": dict(input_refs=["ev-business-abc"]),
            "blank_input": dict(input_refs=(" ",)),
            "repeated_input": dict(input_refs=("a", "a")),
            "repeated_policy": dict(policy_refs=("p", "p")),
            "locator_mismatch": dict(locator="logistics:SF1001#other"),
            "hash_in_fact_key": dict(fact_key="a#b", locator="logistics:SF1001#a#b"),
            "blank_derivation": dict(derivation_id=""),
            "naive_observed_at": dict(observed_at="2026-11-15T10:00:00"),
            "no_observed_at": dict(observed_at=None),
            "float_value": dict(value=1.5),
            "none_value": dict(value=None),
            "float_detail": dict(details={"ratio": 0.5}),
            "object_detail": dict(details={"when": datetime(2026, 1, 1)}),
            "details_not_mapping": dict(details=[("a", 1)]),
        }
        for name, overrides in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    minimal_derived(**overrides)


class EvidenceRefTests(unittest.TestCase):
    def test_same_observation_same_ref_regardless_of_observation_id(self):
        a = business_evidence("inventory", "SKU-X", "available_qty", 3, observation_id="obs-1")
        b = business_evidence("inventory", "SKU-X", "available_qty", 3, observation_id="obs-2")
        c = business_evidence("inventory", "SKU-X", "available_qty", 3)
        self.assertEqual(evidence_ref(a), evidence_ref(b))
        self.assertEqual(evidence_ref(a), evidence_ref(c))
        self.assertTrue(evidence_ref(a).startswith("ev-business-"))

    def test_anything_observed_differently_changes_the_ref(self):
        base = business_evidence("inventory", "SKU-X", "available_qty", 3)
        variants = (
            business_evidence("inventory", "SKU-X", "available_qty", 4),
            business_evidence("inventory", "SKU-X", "available_qty", 3, state_version=2),
            business_evidence("inventory", "SKU-X", "available_qty", 3,
                              observed_at="2026-11-15T10:00:01+08:00"),
            business_evidence("inventory", "SKU-Y", "available_qty", 3),
        )
        refs = {evidence_ref(base)} | {evidence_ref(item) for item in variants}
        self.assertEqual(len(refs), 1 + len(variants))

    def test_executor_linking_does_not_change_refs(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        context = make_context(connection)
        direct = BUSINESS_HANDLERS["get_order"](context, {"order_id": ORDER_A_DELIVERED})
        linked = execute_tool(
            build_runtime_registry(), context, "get_order", {"order_id": ORDER_A_DELIVERED},
            observation_id="obs-42",
        )
        self.assertEqual(
            [evidence_ref(item) for item in direct.evidence],
            [evidence_ref(item) for item in linked.evidence],
        )
        self.assertEqual(linked.evidence[0].metadata[OBSERVATION_ID_KEY], "obs-42")

    def test_non_json_evidence_has_no_ref(self):
        item = business_evidence("inventory", "SKU-X", "available_qty", 3)
        item.metadata["value"] = object()
        with self.assertRaises(ValueError):
            evidence_ref(item)


class ElapsedDaysTests(unittest.TestCase):
    """签收次日起算: delivery date is day 0, calendar dates at +08:00."""

    def test_same_day_is_zero(self):
        self.assertEqual(days("2026-11-05T00:00:00+08:00", at(2026, 11, 5, 23, 59)), 0)
        self.assertEqual(days("2026-11-05T14:30:00+08:00", at(2026, 11, 5, 14, 30)), 0)

    def test_next_calendar_day_is_one_regardless_of_hours(self):
        self.assertEqual(days("2026-11-05T23:59:00+08:00", at(2026, 11, 6, 0, 1)), 1)
        self.assertEqual(days("2026-11-05T00:01:00+08:00", at(2026, 11, 6, 23, 59)), 1)

    def test_day_seven_and_day_eight(self):
        self.assertEqual(days("2026-11-05T14:30:00+08:00", at(2026, 11, 12, 23, 59)), 7)
        self.assertEqual(days("2026-11-05T14:30:00+08:00", at(2026, 11, 13, 0, 0)), 8)

    def test_month_end(self):
        self.assertEqual(days("2026-01-31T20:00:00+08:00", at(2026, 2, 7, 12)), 7)
        self.assertEqual(days("2026-01-31T20:00:00+08:00", at(2026, 2, 8, 0)), 8)
        self.assertEqual(days("2028-02-28T10:00:00+08:00", at(2028, 3, 1, 10)), 2)  # leap year

    def test_year_end(self):
        self.assertEqual(days("2026-12-28T09:00:00+08:00", at(2027, 1, 4, 9)), 7)
        self.assertEqual(days("2026-12-31T23:30:00+08:00", at(2027, 1, 1, 0, 10)), 1)

    def test_2031_sentinel(self):
        self.assertEqual(days("2026-11-05T14:30:00+08:00", at(2031, 11, 5, 14, 30)), 1826)
        self.assertEqual(days("2031-03-01T08:00:00+08:00", at(2031, 3, 8, 23)), 7)

    def test_day_boundary_is_the_policy_offset_not_utc(self):
        # 2026-11-05T16:00Z is 2026-11-06 00:00 at +08:00: delivered on the 6th.
        self.assertEqual(days("2026-11-05T16:00:00+00:00", at(2026, 11, 6, 10)), 0)
        self.assertEqual(days("2026-11-05T16:00:00+00:00", at(2026, 11, 13, 23, 59)), 7)
        # A clock reading given in UTC is converted too: 16:00Z on the 12th is
        # already the 13th at +08:00.
        self.assertEqual(
            days("2026-11-05T14:30:00+08:00", datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)), 8
        )
        # The same instants counted at another offset give another answer.
        self.assertEqual(
            elapsed_natural_days(
                datetime(2026, 11, 5, 16, tzinfo=timezone.utc),
                datetime(2026, 11, 6, 10, tzinfo=timezone.utc),
                counting_rule=RULE, utc_offset="+00:00",
            ),
            1,
        )

    def test_future_delivery_is_not_derivable_even_on_the_same_date(self):
        """Absolute instants are compared before calendar dates."""
        now = at(2026, 11, 15, 10)
        cases = {
            # Same local date, eight hours later: not day 0.
            "same_date_later": "2026-11-15T18:00:00+08:00",
            "next_date": "2026-11-16T00:00:00+08:00",
            "one_second": "2026-11-15T10:00:01+08:00",
            # 02:00Z = 10:00:00+08:00 plus one minute, written in UTC.
            "other_offset": "2026-11-15T02:01:00+00:00",
            # 21:00-05:00 on the 14th = 10:00+08:00 on the 15th, plus one hour.
            "negative_offset": "2026-11-14T22:00:00-05:00",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                evidence = delivered(value, observed_at=now.isoformat())
                with self.assertRaises(NotDerivable) as caught:
                    derive_days_since_delivery(
                        evidence, clock=FixedClock(now), counting_rule=RULE, utc_offset="+08:00"
                    )
                self.assertEqual(caught.exception.code, NOT_DERIVABLE_DELIVERY_IN_FUTURE)
                with self.assertRaises(NotDerivable) as caught:
                    derive_window_eligibility(evidence, window_policy(), clock=FixedClock(now))
                self.assertEqual(caught.exception.code, NOT_DERIVABLE_DELIVERY_IN_FUTURE)
                exchange = window_policy(rule_type=PolicyRuleType.EXCHANGE_WINDOW)
                with self.assertRaises(NotDerivable):
                    derive_window_eligibility(evidence, exchange, clock=FixedClock(now))

    def test_delivery_at_exactly_now_is_day_zero(self):
        now = at(2026, 11, 15, 10)
        self.assertEqual(days(now.isoformat(), now), 0)
        # The same instant written at another offset.
        self.assertEqual(days("2026-11-15T02:00:00+00:00", now), 0)
        fact = derive_window_eligibility(
            delivered(now.isoformat(), observed_at=now.isoformat()), window_policy(),
            clock=FixedClock(now),
        )
        self.assertIs(fact.value, True)
        self.assertEqual(fact.details["days_since_delivery"], 0)

    def test_observation_later_than_the_clock_is_rejected(self):
        evidence = delivered("2026-11-05T14:30:00+08:00", observed_at="2026-11-15T10:00:01+08:00")
        with self.assertRaises(ValueError):
            derive_days_since_delivery(
                evidence, clock=FixedClock(at(2026, 11, 15, 10)), counting_rule=RULE,
                utc_offset="+08:00",
            )

    def test_not_delivered_is_not_derivable(self):
        with self.assertRaises(NotDerivable) as caught:
            derive_days_since_delivery(
                delivered(None), clock=FixedClock(DEMO_VIRTUAL_NOW), counting_rule=RULE,
                utc_offset="+08:00",
            )
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_START_EVENT_ABSENT)

    def test_structure_of_the_fact(self):
        evidence = delivered("2026-11-05T14:30:00+08:00")
        fact = derive_days_since_delivery(
            evidence, clock=FixedClock(DEMO_VIRTUAL_NOW), counting_rule=RULE, utc_offset="+08:00"
        )
        self.assertEqual(fact.fact_key, FACT_DAYS_SINCE_DELIVERY)
        self.assertEqual(fact.subject, "logistics:SF1001")
        self.assertEqual(fact.locator, "logistics:SF1001#days_since_delivery")
        self.assertEqual(fact.value, 10)
        self.assertEqual(fact.input_refs, (evidence_ref(evidence),))
        self.assertEqual(fact.policy_refs, ())
        self.assertEqual(fact.observed_at, DEMO_VIRTUAL_NOW.isoformat())
        self.assertEqual(fact.details["delivery_date"], "2026-11-05")
        self.assertEqual(fact.details["as_of_date"], "2026-11-15")

    def test_wrong_input_field_is_rejected(self):
        wrong = (
            business_evidence("logistics", "SF1001", "shipped_at", "2026-11-02T16:00:00+08:00"),
            business_evidence("order", "SF1001", "delivered_at", "2026-11-02T16:00:00+08:00"),
            Evidence(content="签收于 11 月 5 日", source_type=SourceType.DOCUMENT, source="d",
                     locator="logistics:SF1001#delivered_at", authority=80),
            delivered("2026-11-05"),  # no offset
            delivered("11月5日"),
        )
        for item in wrong:
            with self.subTest(locator=item.locator, value=str(item.metadata.get("value"))):
                with self.assertRaises(ValueError):
                    derive_days_since_delivery(
                        item, clock=FixedClock(DEMO_VIRTUAL_NOW), counting_rule=RULE,
                        utc_offset="+08:00",
                    )

    def test_tampered_locator_is_rejected(self):
        item = delivered("2026-11-05T14:30:00+08:00")
        item.locator = "logistics:SF9999#delivered_at"
        with self.assertRaises(ValueError):
            derive_days_since_delivery(
                item, clock=FixedClock(DEMO_VIRTUAL_NOW), counting_rule=RULE, utc_offset="+08:00"
            )


class WindowEligibilityTests(unittest.TestCase):
    DELIVERED_AT = "2026-11-05T14:30:00+08:00"

    def eligibility(self, now, *, policy=None, category=None, delivered_at=DELIVERED_AT):
        return derive_window_eligibility(
            delivered(delivered_at, observed_at=now.isoformat()),
            policy or window_policy(),
            clock=FixedClock(now),
            category=category,
        )

    def category(self, value, now=DEMO_VIRTUAL_NOW, item_id="OI-1001-1"):
        return business_evidence("order_item", item_id, "category", value, observed_at=now.isoformat())

    def test_inside_the_window(self):
        fact = self.eligibility(at(2026, 11, 8, 12))
        self.assertIs(fact.value, True)
        self.assertEqual(fact.details["days_since_delivery"], 3)

    def test_exact_boundary_last_day_is_inside(self):
        fact = self.eligibility(at(2026, 11, 12, 23, 59))
        self.assertIs(fact.value, True)
        self.assertEqual(fact.details["days_since_delivery"], 7)
        self.assertEqual(fact.details["last_eligible_date"], "2026-11-12")

    def test_first_day_after_is_outside(self):
        fact = self.eligibility(at(2026, 11, 13, 0, 0))
        self.assertIs(fact.value, False)
        self.assertEqual(fact.details["days_since_delivery"], 8)
        self.assertEqual(fact.details["window_days"], 7)

    def test_delivery_day_itself_is_inside(self):
        self.assertIs(self.eligibility(at(2026, 11, 5, 18)).value, True)

    def test_a01_and_a03_on_the_demo_data(self):
        """A01: delivered, inside the window. A03: past it. Same rule, same seed."""
        connection = memory_connection()
        self.addCleanup(connection.close)
        registry = build_runtime_registry()
        for now, expected, elapsed in ((at(2026, 11, 10, 10), True, 5),
                                       (DEMO_VIRTUAL_NOW, False, 10)):
            with self.subTest(now=now.isoformat()):
                context = make_context(connection, now=now)
                logistics = execute_tool(
                    registry, context, "get_logistics", {"order_id": ORDER_A_DELIVERED},
                    observation_id="obs-logistics",
                )
                (delivered_at,) = [e for e in logistics.evidence
                                   if e.locator == "logistics:SF1001#delivered_at"]
                fact = derive_window_eligibility(
                    delivered_at, window_policy(), clock=context.clock
                )
                self.assertIs(fact.value, expected)
                self.assertEqual(fact.details["days_since_delivery"], elapsed)
                self.assertEqual(fact.input_refs, (evidence_ref(delivered_at),))

    def test_records_policy_and_inputs(self):
        policy = window_policy(scope=("服装",))
        category = self.category("服装")
        fact = self.eligibility(DEMO_VIRTUAL_NOW, policy=policy, category=category)
        self.assertEqual(fact.fact_key, FACT_WITHIN_RETURN_WINDOW)
        self.assertEqual(fact.subject, "order_item:OI-1001-1")
        self.assertEqual(fact.policy_refs, (policy_ref(policy),))
        self.assertEqual(len(fact.input_refs), 2)
        self.assertEqual(fact.input_refs[1], evidence_ref(category))
        self.assertEqual(fact.derivation_id, DERIVATION_WINDOW_ELIGIBILITY)
        self.assertEqual(fact.details["category"], "服装")
        self.assertEqual(fact.details["policy_scope"], ["服装"])

    def test_exchange_window_has_its_own_fact_key(self):
        policy = window_policy(rule_type=PolicyRuleType.EXCHANGE_WINDOW, policy_id="P-EX-15D",
                               params={**WINDOW_PARAMS_7D, "window_days": 15})
        fact = self.eligibility(DEMO_VIRTUAL_NOW, policy=policy)
        self.assertEqual(fact.fact_key, FACT_WITHIN_EXCHANGE_WINDOW)
        self.assertIs(fact.value, True)

    def test_category_scope(self):
        policy = window_policy(scope=("服装", "数码"))
        self.assertIs(
            self.eligibility(at(2026, 11, 8), policy=policy,
                             category=self.category("服装", at(2026, 11, 8))).value,
            True,
        )
        with self.assertRaises(NotDerivable) as caught:
            self.eligibility(at(2026, 11, 8), policy=policy,
                             category=self.category("贴身衣物", at(2026, 11, 8)))
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_CATEGORY_OUT_OF_SCOPE)
        with self.assertRaises(NotDerivable) as caught:
            self.eligibility(at(2026, 11, 8), policy=policy)
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_CATEGORY_REQUIRED)

    def test_inactive_policy_is_not_applied(self):
        promo = window_policy(
            policy_id="P-RETURN-15D-PROMO",
            params={**WINDOW_PARAMS_7D, "window_days": 15},
            effective_from="2026-11-01T00:00:00+08:00",
            effective_to="2026-12-01T00:00:00+08:00",
        )
        # In force: day 10 of 15 is inside.
        self.assertIs(self.eligibility(DEMO_VIRTUAL_NOW, policy=promo).value, True)
        # Last in-force instant still applies it.
        self.assertIs(self.eligibility(at(2026, 11, 30, 23, 59), policy=promo).value, False)
        for now in (at(2026, 12, 1, 0, 0), at(2031, 11, 15)):
            with self.subTest(now=now.isoformat()):
                with self.assertRaises(NotDerivable) as caught:
                    self.eligibility(now, policy=promo)
                self.assertEqual(caught.exception.code, NOT_DERIVABLE_POLICY_NOT_IN_EFFECT)
        not_yet = window_policy(effective_from="2026-11-16T00:00:00+08:00")
        with self.assertRaises(NotDerivable):
            self.eligibility(DEMO_VIRTUAL_NOW, policy=not_yet)

    def test_non_window_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            self.eligibility(DEMO_VIRTUAL_NOW, policy=window_policy(
                rule_type=PolicyRuleType.NON_RETURNABLE, params={}, scope=("生鲜",)))

    def test_not_delivered(self):
        with self.assertRaises(NotDerivable) as caught:
            self.eligibility(DEMO_VIRTUAL_NOW, delivered_at=None)
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_START_EVENT_ABSENT)

    def test_uses_the_policy_counting_offset(self):
        # Delivered 23:30 local on the 5th = 15:30Z. Counted at +08:00 the 12th
        # is day 7; counted at +00:00 the delivery date is still the 5th too.
        utc_policy = window_policy(params={**WINDOW_PARAMS_7D, "utc_offset": "+00:00"})
        now = at(2026, 11, 13, 7, 0)  # the 12th, 23:00Z
        self.assertIs(self.eligibility(now, delivered_at="2026-11-05T23:30:00+08:00").value, False)
        self.assertIs(
            self.eligibility(now, policy=utc_policy, delivered_at="2026-11-05T23:30:00+08:00").value,
            True,
        )


class InventoryAvailabilityTests(unittest.TestCase):
    def fact(self, quantity):
        return derive_inventory_available(
            business_evidence("inventory", "SKU-X", "available_qty", quantity),
            clock=FixedClock(DEMO_VIRTUAL_NOW),
        )

    def test_zero_is_unavailable_and_positive_is_available(self):
        self.assertIs(self.fact(0).value, False)
        self.assertIs(self.fact(1).value, True)
        self.assertIs(self.fact(20).value, True)
        self.assertEqual(self.fact(0).fact_key, FACT_INVENTORY_AVAILABLE)
        self.assertEqual(self.fact(0).subject, "inventory:SKU-X")
        self.assertEqual(self.fact(0).details, {"available_qty": 0})

    def test_invalid_quantities_are_rejected(self):
        for bad in (-1, None, "0", "20", True, False, 1.0):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    self.fact(bad)

    def test_demo_inventory(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        context = make_context(connection)
        for sku, expected in ((SKU_STOCKED, True), (SKU_ZERO, False)):
            with self.subTest(sku=sku):
                (item,) = BUSINESS_HANDLERS["get_inventory"](context, {"sku": sku}).evidence
                self.assertIs(derive_inventory_available(item, clock=context.clock).value, expected)


class BusinessStateConflictTests(unittest.TestCase):
    ORDER = "ORD-7001"

    def order(self, status):
        return business_evidence("order", self.ORDER, "status", status)

    def packages(self, *statuses, order_id=None, observation_id="obs-l"):
        evidence = []
        for index, status in enumerate(statuses):
            tracking_no = "T" + str(index + 1)
            for field, value in (("order_id", order_id or self.ORDER), ("status", status),
                                 ("carrier", "顺丰速运")):
                evidence.append(business_evidence(
                    "logistics", tracking_no, field, value, observation_id=observation_id))
        return evidence

    def conflict(self, order_status, *statuses):
        return derive_business_state_conflict(
            self.order(order_status), self.packages(*statuses), clock=FixedClock(DEMO_VIRTUAL_NOW)
        )

    def test_clear_inconsistencies_are_surfaced(self):
        cases = {
            ("已发货", ("已签收",)): "order_shipped_all_packages_delivered",
            ("已发货", ("已签收", "已签收")): "order_shipped_all_packages_delivered",
            ("已签收", ("运输中",)): "order_delivered_package_in_transit",
            ("已签收", ("已签收", "派送中")): "order_delivered_package_in_transit",
            ("待付款", ("运输中",)): "order_unshipped_package_moving",
            ("已付款", ("已签收",)): "order_unshipped_package_moving",
        }
        for (order_status, statuses), rule_id in cases.items():
            with self.subTest(order=order_status, packages=statuses):
                fact = self.conflict(order_status, *statuses)
                self.assertIs(fact.value, True)
                self.assertIn(rule_id, fact.details["fired_rules"])
                self.assertEqual(fact.fact_key, FACT_BUSINESS_STATE_CONFLICT)
                self.assertEqual(fact.subject, "order:" + self.ORDER)

    def test_consistent_or_unprovable_states_are_not_flagged(self):
        cases = (
            ("已发货", ("运输中",)),
            ("已发货", ("派送中",)),
            ("已发货", ("已签收", "运输中")),  # partial delivery
            ("已签收", ("已签收",)),
            ("已签收", ("已签收", "已签收")),
            ("已完成", ("已签收",)),
            ("已签收", ("异常",)),
            ("已签收", ("退回",)),
            ("已发货", ("异常",)),
            ("已取消", ("退回",)),
            ("已完成", ("运输中",)),  # e.g. a return shipment: not provable
        )
        for order_status, statuses in cases:
            with self.subTest(order=order_status, packages=statuses):
                fact = self.conflict(order_status, *statuses)
                self.assertIs(fact.value, False)
                self.assertEqual(fact.details["fired_rules"], [])

    def test_every_input_is_referenced(self):
        order = self.order("已发货")
        logistics = self.packages("已签收", "已签收")
        fact = derive_business_state_conflict(order, logistics, clock=FixedClock(DEMO_VIRTUAL_NOW))
        expected = {evidence_ref(order)} | {
            evidence_ref(item) for item in logistics if item.metadata["field"] != "carrier"
        }
        self.assertEqual(set(fact.input_refs), expected)
        self.assertEqual(fact.input_refs[0], evidence_ref(order))
        self.assertEqual(fact.details["package_statuses"], {"T1": "已签收", "T2": "已签收"})
        self.assertEqual(fact.derivation_id, DERIVATION_BUSINESS_STATE_CONFLICT)

    def test_incomplete_evidence_creates_no_conflict_and_no_verdict(self):
        clock = FixedClock(DEMO_VIRTUAL_NOW)
        full = self.packages("已签收")
        without_status = [e for e in full if e.metadata["field"] != "status"]
        without_order_id = [e for e in full if e.metadata["field"] != "order_id"]
        for name, logistics in (("no_packages", []), ("carrier_only", [full[2]]),
                                ("no_status", without_status),
                                ("no_order_link", without_order_id)):
            with self.subTest(case=name):
                with self.assertRaises(NotDerivable) as caught:
                    derive_business_state_conflict(self.order("已发货"), logistics, clock=clock)
                self.assertEqual(caught.exception.code, NOT_DERIVABLE_LOGISTICS_INCOMPLETE)

    def test_mixed_or_foreign_evidence_is_rejected(self):
        clock = FixedClock(DEMO_VIRTUAL_NOW)
        with self.assertRaises(ValueError):
            derive_business_state_conflict(
                self.order("已发货"), self.packages("已签收", order_id="ORD-OTHER"), clock=clock)
        mixed = self.packages("已签收", observation_id="obs-1")
        mixed += [business_evidence("logistics", "T9", field, value, observation_id="obs-2")
                  for field, value in (("order_id", self.ORDER), ("status", "已签收"))]
        with self.assertRaises(ValueError):
            derive_business_state_conflict(self.order("已发货"), mixed, clock=clock)
        for bad_status in ("delivered", "签收", ""):
            with self.subTest(status=bad_status):
                with self.assertRaises(ValueError):
                    derive_business_state_conflict(
                        self.order("已发货"), self.packages(bad_status), clock=clock)
        with self.assertRaises(ValueError):
            derive_business_state_conflict(self.order("shipped"), self.packages("已签收"), clock=clock)
        with self.assertRaises(ValueError):
            derive_business_state_conflict(
                self.order("已发货"), tuple(self.packages("已签收"))[0], clock=clock)

    def at_instant(self, order_status, package_status, order_at, logistics_at):
        order = business_evidence("order", self.ORDER, "status", order_status,
                                  observed_at=order_at, observation_id="obs-order")
        logistics = [
            business_evidence("logistics", "T1", field, value, observed_at=logistics_at,
                              observation_id="obs-logistics")
            for field, value in (("order_id", self.ORDER), ("status", package_status))
        ]
        return derive_business_state_conflict(order, logistics, clock=FixedClock(DEMO_VIRTUAL_NOW))

    def test_order_and_logistics_read_at_different_instants_is_not_derivable(self):
        """A: order @ 09:59 已发货, logistics @ 10:00 已签收 - maybe just a transition."""
        for order_status, package_status in (("已发货", "已签收"), ("已签收", "已签收")):
            with self.subTest(order=order_status, package=package_status):
                with self.assertRaises(NotDerivable) as caught:
                    self.at_instant(order_status, package_status,
                                    "2026-11-15T09:59:00+08:00", "2026-11-15T10:00:00+08:00")
                self.assertEqual(caught.exception.code, NOT_DERIVABLE_OBSERVATION_TIME_MISMATCH)
        # The other direction, and a one-second difference, likewise.
        with self.assertRaises(NotDerivable):
            self.at_instant("已签收", "运输中",
                            "2026-11-15T10:00:00+08:00", "2026-11-15T09:59:59+08:00")

    def test_same_instant_at_different_offsets_is_derivable(self):
        """B: 10:00+08:00 and 02:00Z are one instant."""
        fact = self.at_instant("已签收", "已签收",
                               "2026-11-15T10:00:00+08:00", "2026-11-15T02:00:00+00:00")
        self.assertIs(fact.value, False)

    def test_same_instant_clear_conflict(self):
        """C."""
        fact = self.at_instant("已发货", "已签收",
                               "2026-11-15T02:00:00+00:00", "2026-11-15T10:00:00+08:00")
        self.assertIs(fact.value, True)
        self.assertEqual(fact.details["fired_rules"], ["order_shipped_all_packages_delivered"])

    def test_same_instant_consistent(self):
        """D."""
        fact = self.at_instant("已发货", "运输中",
                               "2026-11-15T10:00:00+08:00", "2026-11-15T10:00:00+08:00")
        self.assertIs(fact.value, False)

    def test_observation_ids_of_the_two_tools_are_not_compared(self):
        # Different tool calls, different observation_ids, same instant: fine.
        fact = self.at_instant("已发货", "已签收", DEMO_VIRTUAL_NOW.isoformat(),
                               DEMO_VIRTUAL_NOW.isoformat())
        self.assertIs(fact.value, True)

    def test_demo_orders(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        registry = build_runtime_registry()
        context = make_context(connection)
        for order_id in (ORDER_A_DELIVERED, ORDER_A_IN_TRANSIT, ORDER_A_TWO_PACKAGES):
            with self.subTest(order=order_id):
                order = execute_tool(registry, context, "get_order", {"order_id": order_id},
                                     observation_id="obs-o")
                logistics = execute_tool(registry, context, "get_logistics",
                                         {"order_id": order_id}, observation_id="obs-l")
                (status,) = [e for e in order.evidence if e.locator == "order:" + order_id + "#status"]
                fact = derive_business_state_conflict(status, logistics.evidence, clock=context.clock)
                self.assertIs(fact.value, False)

    def test_a07_on_demo_data_with_the_order_left_at_shipped(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE orders SET status = '已发货' WHERE order_id = ?", (ORDER_A_DELIVERED,))
        registry = build_runtime_registry()
        context = make_context(connection)
        order = execute_tool(registry, context, "get_order", {"order_id": ORDER_A_DELIVERED})
        logistics = execute_tool(registry, context, "get_logistics", {"order_id": ORDER_A_DELIVERED})
        (status,) = [e for e in order.evidence if e.locator.endswith("#status")]
        fact = derive_business_state_conflict(status, logistics.evidence, clock=context.clock)
        self.assertIs(fact.value, True)
        self.assertEqual(fact.details["fired_rules"], ["order_shipped_all_packages_delivered"])

    def test_rule_table_is_explicit(self):
        self.assertEqual(
            [rule.rule_id for rule in STATE_CONFLICT_RULES],
            ["order_shipped_all_packages_delivered", "order_delivered_package_in_transit",
             "order_unshipped_package_moving"],
        )
        for rule in STATE_CONFLICT_RULES:
            self.assertIn(rule.quantifier, ("any", "all"))


class DeterminismTests(unittest.TestCase):
    def build_all(self, now):
        clock = FixedClock(now)
        delivered_at = delivered("2026-11-05T14:30:00+08:00", observed_at=now.isoformat())
        category = business_evidence("order_item", "OI-1", "category", "服装",
                                     observed_at=now.isoformat())
        inventory = business_evidence("inventory", "SKU-X", "available_qty", 0,
                                      observed_at=now.isoformat())
        order = business_evidence("order", "ORD-1", "status", "已发货", observed_at=now.isoformat())
        logistics = [business_evidence("logistics", "T1", field, value, observed_at=now.isoformat())
                     for field, value in (("order_id", "ORD-1"), ("status", "已签收"))]
        return [
            derive_days_since_delivery(delivered_at, clock=clock, counting_rule=RULE,
                                       utc_offset="+08:00"),
            derive_window_eligibility(delivered_at, window_policy(scope=("服装",)), clock=clock,
                                      category=category),
            derive_inventory_available(inventory, clock=clock),
            derive_business_state_conflict(order, logistics, clock=clock),
        ]

    def test_same_input_gives_byte_stable_output_and_refs(self):
        first = self.build_all(DEMO_VIRTUAL_NOW)
        second = self.build_all(DEMO_VIRTUAL_NOW)
        self.assertEqual([canonical(item) for item in first], [canonical(item) for item in second])
        self.assertEqual([evidence_ref(item) for item in first],
                         [evidence_ref(item) for item in second])
        for item in first:
            self.assertTrue(evidence_ref(item).startswith("ev-derived-"))

    def test_virtual_now_changes_the_output(self):
        now = [canonical(item) for item in self.build_all(DEMO_VIRTUAL_NOW)]
        later = [canonical(item) for item in self.build_all(at(2031, 11, 15, 10))]
        self.assertTrue(all(a != b for a, b in zip(now, later)))

    def test_2031_sentinel_everything_follows_the_clock(self):
        sentinel = at(2031, 3, 4, 5)
        facts = self.build_all(sentinel)
        for fact in facts:
            with self.subTest(fact=fact.fact_key):
                self.assertEqual(fact.observed_at, sentinel.isoformat())
        self.assertEqual(facts[0].details["as_of_date"], "2031-03-04")
        self.assertEqual(facts[0].value, (sentinel.date() - datetime(2026, 11, 5).date()).days)

    def test_no_system_time_is_read(self):
        """Pure in the Clock: a poisoned SystemClock / datetime.now changes nothing."""
        expected = [canonical(item) for item in self.build_all(DEMO_VIRTUAL_NOW)]

        class PoisonedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError("derived facts must not read the system clock")

            @classmethod
            def today(cls):
                raise AssertionError("derived facts must not read the system clock")

        with patch.object(derived_module, "datetime", PoisonedDatetime), \
                patch("aftersales.clock.SystemClock.now",
                      side_effect=AssertionError("no SystemClock")):
            got = [canonical(item) for item in self.build_all(DEMO_VIRTUAL_NOW)]
        self.assertEqual(got, expected)

    def test_inputs_are_not_mutated(self):
        delivered_at = delivered("2026-11-05T14:30:00+08:00")
        before = canonical(delivered_at)
        derive_window_eligibility(delivered_at, window_policy(), clock=FixedClock(DEMO_VIRTUAL_NOW))
        self.assertEqual(canonical(delivered_at), before)

    def test_a_copy_keeps_its_contract_and_ref(self):
        fact = self.build_all(DEMO_VIRTUAL_NOW)[1]
        copied = dataclasses.replace(fact)
        self.assertEqual(evidence_ref(copied), evidence_ref(fact))


if __name__ == "__main__":
    unittest.main()
