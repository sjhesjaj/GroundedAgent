"""Stage 4.3.8: item-level windows fail closed when the order has several packages.

The schema maps order_id -> 0..N tracking_no but has no order_item_id ->
tracking_no mapping. Same order is proven (Stage 4.3.7); same package is not.
So an item's window may only use a delivery when its order has exactly one
package, and never a package picked out of several.
"""

from __future__ import annotations

import dataclasses
import sys
import types
import unittest
from pathlib import Path

from aftersales import derived as derived_module
from aftersales.clock import FixedClock
from aftersales.demo import DEMO_VIRTUAL_NOW
from aftersales.derived import (
    NOT_DERIVABLE_CODES,
    NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS,
    NOT_DERIVABLE_ORDER_LINK_MISMATCH,
    NOT_DERIVABLE_ORDER_LINK_MISSING,
    NOT_DERIVABLE_START_EVENT_ABSENT,
    NotDerivable,
    derive_item_window_eligibility,
    derive_window_eligibility,
)
from aftersales.executor import execute_tool
from aftersales.registry import build_runtime_registry
from orchestration.contracts import DerivedEvidence, evidence_ref

from tests.v2_support import (
    ORDER_A_DELIVERED,
    ORDER_A_TWO_PACKAGES,
    at,
    business_evidence,
    make_context,
    memory_connection,
    window_policy,
)

NOW = at(2026, 11, 13, 10)
CLOCK = FixedClock(NOW)
POLICY = window_policy(params={"window_days": 15, "start_event": "delivered",
                               "counting_rule": "natural_days_from_next_day",
                               "utc_offset": "+08:00"})
SCOPED = dataclasses.replace(POLICY, policy_id="P-SCOPED", scope=("服装",))


def package(tracking_no, value="2026-11-05T14:30:00+08:00", order_id="ORD-A", observation_id=None):
    relations = None if order_id is None else {"order_id": order_id}
    return business_evidence("logistics", tracking_no, "delivered_at", value,
                             observed_at=NOW.isoformat(), relations=relations,
                             observation_id=observation_id)


def category(order_id="ORD-A", value="服装"):
    relations = None if order_id is None else {"order_id": order_id}
    return business_evidence("order_item", "OI-A-1", "category", value,
                             observed_at=NOW.isoformat(), relations=relations)


class ItemWindowTests(unittest.TestCase):
    def item_window(self, packages, *, policy=POLICY, item=None):
        return derive_item_window_eligibility(packages, policy, clock=CLOCK,
                                              category=category() if item is None else item)

    def assert_code(self, code, packages, **kwargs):
        with self.assertRaises(NotDerivable) as caught:
            self.item_window(packages, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_code_is_registered(self):
        self.assertEqual(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS, "item_package_link_ambiguous")
        self.assertIn(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS, NOT_DERIVABLE_CODES)

    def test_single_package_matching_item_succeeds(self):
        only = package("SF-A")
        item = category()
        for policy in (POLICY, SCOPED):
            with self.subTest(policy=policy.policy_id):
                fact = self.item_window([only], policy=policy, item=item)
                self.assertIsInstance(fact, DerivedEvidence)
                self.assertIs(fact.value, True)
                # Identical to the low-level primitive on that single delivery.
                self.assertEqual(fact.to_dict(), derive_window_eligibility(
                    only, policy, clock=CLOCK, category=item).to_dict())
                self.assertEqual(fact.input_refs, (evidence_ref(only), evidence_ref(item)))
        self.assertIs(self.item_window((only,)).value, True)  # tuples accepted too

    def test_two_packages_same_order_ambiguous(self):
        self.assert_code(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS,
                         [package("SF-A1"), package("SF-A2", "2026-11-07T09:00:00+08:00")])

    def test_one_delivered_one_undelivered_still_ambiguous(self):
        for packages in ([package("SF-A1"), package("SF-A2", None)],
                         [package("SF-A2", None), package("SF-A1")]):
            with self.subTest(order=[p.metadata["record_id"] for p in packages]):
                self.assert_code(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS, packages)

    def test_both_delivered_same_day_still_ambiguous(self):
        # Every choice would give the same answer; the item is still not proven
        # to be in either package.
        self.assert_code(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS,
                         [package("SF-A1", "2026-11-05T09:00:00+08:00"),
                          package("SF-A2", "2026-11-05T18:00:00+08:00")])
        self.assert_code(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS,
                         [package("SF-A1"), package("SF-A2")])

    def test_ambiguity_does_not_depend_on_scope(self):
        self.assert_code(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS,
                         [package("SF-A1"), package("SF-A2")], policy=SCOPED)

    def test_package_of_another_order_is_a_structural_refusal(self):
        for packages in ([package("SF-A", order_id="ORD-A"), package("SF-B", order_id="ORD-B")],
                         [package("SF-B", order_id="ORD-B")]):
            with self.subTest(n=len(packages)):
                self.assert_code(NOT_DERIVABLE_ORDER_LINK_MISMATCH, packages)

    def test_missing_order_links(self):
        self.assert_code(NOT_DERIVABLE_ORDER_LINK_MISSING, [package("SF-A")], item=category(None))
        self.assert_code(NOT_DERIVABLE_ORDER_LINK_MISSING,
                         [package("SF-A1"), package("SF-A2", order_id=None)])
        self.assert_code(NOT_DERIVABLE_ORDER_LINK_MISSING, [package("SF-A", order_id=None)])

    def test_no_package_is_an_absent_start_event(self):
        self.assert_code(NOT_DERIVABLE_START_EVENT_ABSENT, [])
        self.assert_code(NOT_DERIVABLE_START_EVENT_ABSENT, [package("SF-A", None)])

    def test_rule_gate_runs_first(self):
        inactive = dataclasses.replace(POLICY, effective_from="2030-01-01T00:00:00+08:00")
        self.assert_code("policy_not_in_effect", [package("SF-A1"), package("SF-A2")], policy=inactive)
        self.assert_code("category_not_in_scope", [package("SF-A1"), package("SF-A2")],
                         policy=SCOPED, item=category(value="数码"))

    def test_malformed_input_is_a_data_fault(self):
        for bad in ((package("SF-A") for _ in range(1)), package("SF-A"), {"SF-A": package("SF-A")}):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(ValueError):
                    self.item_window(bad)
        with self.assertRaises(ValueError):
            self.item_window([package("SF-A"), package("SF-A")])
        with self.assertRaises(ValueError):
            self.item_window([business_evidence("logistics", "SF-A", "shipped_at",
                                                "2026-11-05T14:30:00+08:00",
                                                observed_at=NOW.isoformat(),
                                                relations={"order_id": "ORD-A"})])
        with self.assertRaises(ValueError):
            self.item_window([package("SF-A1", observation_id="obs-1"),
                              package("SF-A2", observation_id="obs-2")])
        with self.assertRaises(ValueError):
            derive_item_window_eligibility([package("SF-A")], POLICY, clock=CLOCK, category=None)

    def test_category_none_low_level_behaviour_unchanged(self):
        fact = derive_window_eligibility(package("SF-A", order_id=None), POLICY, clock=CLOCK)
        self.assertIs(fact.value, True)
        self.assertEqual(fact.subject, "logistics:SF-A")
        # Package-level facts for each package of a multi-package order still work.
        for tracking_no in ("SF-A1", "SF-A2"):
            self.assertIs(derive_window_eligibility(package(tracking_no), POLICY, clock=CLOCK).value, True)


class DemoOrderTests(unittest.TestCase):
    def test_ord_1004_item_window_is_ambiguous(self):
        """ORD-1004: SF1004A delivered, YT1004B in transit, two items, no item->package map."""
        connection = memory_connection()
        self.addCleanup(connection.close)
        registry = build_runtime_registry()
        context = make_context(connection, now=DEMO_VIRTUAL_NOW)
        order = execute_tool(registry, context, "get_order", {"order_id": ORDER_A_TWO_PACKAGES},
                             observation_id="obs-order")
        logistics = execute_tool(registry, context, "get_logistics", {"order_id": ORDER_A_TWO_PACKAGES},
                                 observation_id="obs-logistics")
        deliveries = [e for e in logistics.evidence if e.metadata["field"] == "delivered_at"]
        self.assertEqual({e.metadata["record_id"]: e.metadata["value"] for e in deliveries},
                         {"SF1004A": "2026-11-12T11:00:00+08:00", "YT1004B": None})
        # Multi-package logistics are still read and reported normally.
        self.assertEqual({e.metadata["record_id"] for e in logistics.evidence}, {"SF1004A", "YT1004B"})
        items = [e for e in order.evidence if e.metadata["field"] == "category"]
        self.assertEqual(len(items), 2)
        for item in items:
            with self.subTest(item=item.metadata["record_id"]):
                with self.assertRaises(NotDerivable) as caught:
                    derive_item_window_eligibility(deliveries, POLICY, clock=context.clock, category=item)
                self.assertEqual(caught.exception.code, NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS)

    def test_ord_1001_single_package_item_window(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        registry = build_runtime_registry()
        context = make_context(connection, now=DEMO_VIRTUAL_NOW)
        order = execute_tool(registry, context, "get_order", {"order_id": ORDER_A_DELIVERED})
        logistics = execute_tool(registry, context, "get_logistics", {"order_id": ORDER_A_DELIVERED})
        deliveries = [e for e in logistics.evidence if e.metadata["field"] == "delivered_at"]
        (item,) = [e for e in order.evidence if e.locator == "order_item:OI-1001-1#category"]
        fact = derive_item_window_eligibility(deliveries, POLICY, clock=context.clock, category=item)
        self.assertEqual(fact.subject, "order_item:OI-1001-1")
        self.assertEqual(fact.details["order_id"], ORDER_A_DELIVERED)


class MutationGuardTests(unittest.TestCase):
    """Removing the ambiguity gate must be caught."""

    CHECK = ("    if len(packages) > 1:\n"
             "        raise NotDerivable(NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS)\n")

    def mutant(self):
        source = Path(derived_module.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(source.count(self.CHECK), 1)
        name = "aftersales._derived_ambiguity_mutant"
        module = types.ModuleType(name)
        module.__package__ = "aftersales"
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source.replace(self.CHECK, ""), "derived_mutant.py", "exec"), module.__dict__)
        return module

    def test_without_the_gate_a_package_would_be_picked(self):
        packages = [package("SF-A1"), package("SF-A2")]
        fact = self.mutant().derive_item_window_eligibility(packages, POLICY, clock=CLOCK,
                                                            category=category())
        self.assertIs(fact.value, True)  # the mutant silently attributes a package
        with self.assertRaises(NotDerivable) as caught:
            derive_item_window_eligibility(packages, POLICY, clock=CLOCK, category=category())
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_ITEM_PACKAGE_LINK_AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
