"""Stage 4.3.7: structural same-order linkage between business evidence.

A window fact must never be derived from an order item of one order and a
delivery of another. The order link is `BusinessEvidence.relations`, produced
by the business tools from the record's own columns - never parsed from
content or locators.
"""

from __future__ import annotations

import dataclasses
import sys
import types
import unittest
from pathlib import Path
from types import MappingProxyType

from aftersales import business_tools, derived as derived_module
from aftersales.business_tools import (
    CASE_QUERY,
    INVENTORY_QUERY,
    LOGISTICS_QUERY,
    ORDER_ITEMS_QUERY,
    ORDER_QUERY,
    TOOL_QUERIES,
    Query,
)
from aftersales.clock import FixedClock
from aftersales.demo import DEMO_VIRTUAL_NOW
from aftersales.derived import (
    NOT_DERIVABLE_CODES,
    NOT_DERIVABLE_ORDER_LINK_MISMATCH,
    NOT_DERIVABLE_ORDER_LINK_MISSING,
    NotDerivable,
    derive_window_eligibility,
)
from aftersales.errors import RecordIntegrityError
from aftersales.executor import execute_tool
from aftersales.registry import build_runtime_registry
from orchestration.contracts import DerivedEvidence, evidence_ref

from tests.v2_support import (
    CUSTOMER_A,
    CUSTOMER_B,
    ORDER_A_DELIVERED,
    ORDER_A_TWO_PACKAGES,
    SKU_STOCKED,
    at,
    business_evidence,
    make_context,
    memory_connection,
    window_policy,
)

NOW = at(2026, 11, 13, 10)
CLOCK = FixedClock(NOW)
GLOBAL_POLICY = window_policy(params={"window_days": 15, "start_event": "delivered",
                                      "counting_rule": "natural_days_from_next_day",
                                      "utc_offset": "+08:00"})
SCOPED_POLICY = dataclasses.replace(GLOBAL_POLICY, policy_id="P-SCOPED", scope=("服装",))


def delivered(order_id: str | None = "ORD-A", tracking_no: str = "SF-A"):
    relations = None if order_id is None else {"order_id": order_id}
    return business_evidence("logistics", tracking_no, "delivered_at", "2026-11-05T14:30:00+08:00",
                             observed_at=NOW.isoformat(), relations=relations)


def category(order_id: str | None = "ORD-A", item_id: str = "OI-A-1", value: str = "服装"):
    relations = None if order_id is None else {"order_id": order_id}
    return business_evidence("order_item", item_id, "category", value,
                             observed_at=NOW.isoformat(), relations=relations)


class RelationsContractTests(unittest.TestCase):
    def test_empty_and_valid_relations(self):
        self.assertEqual(business_evidence("inventory", "SKU-X", "available_qty", 1).relations, {})
        item = category("ORD-1")
        self.assertEqual(item.relations, {"order_id": "ORD-1"})
        two = business_evidence("after_sales_case", "AS-1", "status", "处理中",
                                relations={"order_id": "ORD-1", "order_item_id": "OI-1"})
        self.assertEqual(two.relations, {"order_id": "ORD-1", "order_item_id": "OI-1"})

    def test_invalid_keys_rejected(self):
        for relations in ({1: "ORD-1"}, {"": "ORD-1"}, {"  ": "ORD-1"}, {None: "ORD-1"}):
            with self.subTest(relations=relations):
                with self.assertRaises(ValueError):
                    business_evidence("order_item", "OI-1", "category", "服装", relations=relations)

    def test_invalid_values_rejected(self):
        for value in (None, 1, True, 1.5, "", "   ", ["ORD-1"], {"id": "ORD-1"}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    business_evidence("order_item", "OI-1", "category", "服装",
                                      relations={"order_id": value})

    def test_non_mapping_rejected(self):
        for relations in ([("order_id", "ORD-1")], "order_id=ORD-1", 1):
            with self.subTest(relations=relations):
                with self.assertRaises(ValueError):
                    business_evidence("order_item", "OI-1", "category", "服装", relations=relations)

    def test_relations_are_copied_to_a_plain_dict(self):
        source = {"order_id": "ORD-1"}
        item = business_evidence("order_item", "OI-1", "category", "服装", relations=source)
        source["order_id"] = "ORD-2"
        source["extra"] = "x"
        self.assertEqual(item.relations, {"order_id": "ORD-1"})
        proxied = business_evidence("order_item", "OI-1", "category", "服装",
                                    relations=MappingProxyType({"order_id": "ORD-1"}))
        self.assertIs(type(proxied.relations), dict)

    def test_to_dict_includes_relations(self):
        payload = category("ORD-1").to_dict()
        self.assertEqual(payload["relations"], {"order_id": "ORD-1"})
        self.assertEqual(business_evidence("inventory", "S", "available_qty", 1).to_dict()["relations"], {})

    def test_evidence_ref_changes_with_relations(self):
        self.assertNotEqual(evidence_ref(category("ORD-1")), evidence_ref(category("ORD-2")))
        self.assertNotEqual(evidence_ref(category("ORD-1")), evidence_ref(category(None)))
        self.assertEqual(evidence_ref(category("ORD-1")), evidence_ref(category("ORD-1")))


class ToolRelationTests(unittest.TestCase):
    def setUp(self):
        self.connection = memory_connection()
        self.addCleanup(self.connection.close)
        self.registry = build_runtime_registry()
        self.context = make_context(self.connection)

    def call(self, tool, arguments):
        return execute_tool(self.registry, self.context, tool, arguments, observation_id="obs")

    def test_query_relation_mappings(self):
        self.assertEqual(ORDER_QUERY.relations, ())
        self.assertEqual(ORDER_ITEMS_QUERY.relations, (("order_id", "order_id"),))
        self.assertEqual(LOGISTICS_QUERY.relations, (("order_id", "order_id"),))
        self.assertEqual(CASE_QUERY.relations, (("order_id", "order_id"), ("order_item_id", "order_item_id")))
        self.assertEqual(INVENTORY_QUERY.relations, ())
        for queries in TOOL_QUERIES.values():
            for query in queries:
                for column, name in query.relations:
                    self.assertIn(column, query.columns)
                    self.assertNotIn("customer", column + name)
                    self.assertNotIn("customer_id", query.columns)

    def test_order_items_carry_their_order(self):
        result = self.call("get_order", {"order_id": ORDER_A_DELIVERED})
        items = [e for e in result.evidence if e.metadata["entity"] == "order_item"]
        self.assertEqual({e.metadata["field"] for e in items},
                         {"sku", "product_name", "category", "quantity", "unit_price"})
        self.assertTrue(items)
        for evidence in items:
            self.assertEqual(evidence.relations, {"order_id": ORDER_A_DELIVERED})
        # order_id is selected for linkage only, not published as an item field.
        self.assertNotIn("order_id", {e.metadata["field"] for e in items})

    def test_logistics_carry_their_order(self):
        result = self.call("get_logistics", {"order_id": ORDER_A_TWO_PACKAGES})
        self.assertEqual({e.metadata["record_id"] for e in result.evidence}, {"SF1004A", "YT1004B"})
        for evidence in result.evidence:
            self.assertEqual(evidence.relations, {"order_id": ORDER_A_TWO_PACKAGES})

    def test_cases_carry_order_and_item(self):
        result = self.call("get_after_sales_case", {"order_id": ORDER_A_DELIVERED})
        self.assertTrue(result.evidence)
        for evidence in result.evidence:
            self.assertEqual(evidence.relations, {"order_id": ORDER_A_DELIVERED,
                                                  "order_item_id": "OI-1001-1"})

    def test_inventory_and_order_have_no_relations(self):
        for tool, arguments in (("get_inventory", {"sku": SKU_STOCKED}),):
            for evidence in self.call(tool, arguments).evidence:
                self.assertEqual(evidence.relations, {})
        order = [e for e in self.call("get_order", {"order_id": ORDER_A_DELIVERED}).evidence
                 if e.metadata["entity"] == "order"]
        for evidence in order:
            self.assertEqual(evidence.relations, {})

    def test_customer_id_never_reaches_evidence_or_relations(self):
        for tool, arguments in (("get_order", {"order_id": ORDER_A_DELIVERED}),
                                ("get_logistics", {"order_id": ORDER_A_DELIVERED}),
                                ("get_after_sales_case", {"order_id": ORDER_A_DELIVERED}),
                                ("get_inventory", {"sku": SKU_STOCKED})):
            for evidence in self.call(tool, arguments).evidence:
                rendered = repr(evidence.to_dict())
                self.assertNotIn(CUSTOMER_A, rendered)
                self.assertNotIn(CUSTOMER_B, rendered)
                self.assertNotIn("customer", " ".join(evidence.relations))

    def test_relation_columns_are_integrity_checked(self):
        record = {"order_item_id": "OI-1", "order_id": "ORD-1", "sku": "S", "product_name": "P",
                  "category": "C", "quantity": 1, "unit_price": "1.00",
                  "updated_at": "2026-11-01T00:00:00+08:00", "version": 1}
        business_tools._check_row("get_order", ORDER_ITEMS_QUERY, dict(record))
        for bad in ("", "  ", None, 7):
            with self.subTest(order_id=bad):
                with self.assertRaises(RecordIntegrityError):
                    business_tools._check_row("get_order", ORDER_ITEMS_QUERY, dict(record, order_id=bad))
        unselected = dataclasses.replace(ORDER_ITEMS_QUERY, relations=(("parent_id", "order_id"),))
        with self.assertRaises(RecordIntegrityError):
            business_tools._check_row("get_order", unselected, dict(record))


class WindowLinkageTests(unittest.TestCase):
    def test_same_order_derives(self):
        for policy in (GLOBAL_POLICY, SCOPED_POLICY):
            with self.subTest(policy=policy.policy_id):
                item, delivery = category("ORD-A"), delivered("ORD-A")
                fact = derive_window_eligibility(delivery, policy, clock=CLOCK, category=item)
                self.assertIsInstance(fact, DerivedEvidence)
                self.assertIs(fact.value, True)
                self.assertEqual(fact.details["order_id"], "ORD-A")
                self.assertEqual(fact.input_refs, (evidence_ref(delivery), evidence_ref(item)))

    def test_cross_order_rejected_even_when_everything_else_matches(self):
        # Scope matches, rule in force, timestamps valid, same observed_at.
        item, delivery = category("ORD-A"), delivered("ORD-B")
        self.assertEqual(item.observed_at, delivery.observed_at)
        with self.assertRaises(NotDerivable) as caught:
            derive_window_eligibility(delivery, SCOPED_POLICY, clock=CLOCK, category=item)
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_ORDER_LINK_MISMATCH)

    def test_cross_order_rejected_under_a_global_policy(self):
        with self.assertRaises(NotDerivable) as caught:
            derive_window_eligibility(delivered("ORD-B"), GLOBAL_POLICY, clock=CLOCK,
                                      category=category("ORD-A"))
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_ORDER_LINK_MISMATCH)

    def test_missing_link_rejected(self):
        for item, delivery in ((category(None), delivered("ORD-A")),
                               (category("ORD-A"), delivered(None)),
                               (category(None), delivered(None)),
                               (business_evidence("order_item", "OI-A-1", "category", "服装",
                                                  observed_at=NOW.isoformat(),
                                                  relations={"order_item_id": "OI-A-1"}),
                                delivered("ORD-A"))):
            for policy in (GLOBAL_POLICY, SCOPED_POLICY):
                with self.subTest(policy=policy.policy_id, item=item.relations, delivery=delivery.relations):
                    with self.assertRaises(NotDerivable) as caught:
                        derive_window_eligibility(delivery, policy, clock=CLOCK, category=item)
                    self.assertEqual(caught.exception.code, NOT_DERIVABLE_ORDER_LINK_MISSING)

    def test_no_fallback_to_locator_or_content(self):
        # Locators and content name the same order, but there is no structural link.
        item = business_evidence("order_item", "ORD-A-1", "category", "服装",
                                 observed_at=NOW.isoformat())
        delivery = business_evidence("logistics", "ORD-A", "delivered_at", "2026-11-05T14:30:00+08:00",
                                     observed_at=NOW.isoformat())
        with self.assertRaises(NotDerivable) as caught:
            derive_window_eligibility(delivery, SCOPED_POLICY, clock=CLOCK, category=item)
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_ORDER_LINK_MISSING)

    def test_malformed_relations_are_a_data_fault(self):
        item = category("ORD-A")
        item.relations = {"order_id": 5}  # mutated after construction
        with self.assertRaises(ValueError):
            derive_window_eligibility(delivered("ORD-A"), GLOBAL_POLICY, clock=CLOCK, category=item)
        item.relations = ["order_id"]
        with self.assertRaises(ValueError):
            derive_window_eligibility(delivered("ORD-A"), GLOBAL_POLICY, clock=CLOCK, category=item)

    def test_no_category_global_policy_needs_no_link(self):
        fact = derive_window_eligibility(delivered(None), GLOBAL_POLICY, clock=CLOCK)
        self.assertIs(fact.value, True)
        self.assertEqual(fact.subject, "logistics:SF-A")
        self.assertNotIn("order_id", fact.details)

    def test_earlier_codes_keep_their_order(self):
        inactive = dataclasses.replace(GLOBAL_POLICY, effective_from="2030-01-01T00:00:00+08:00")
        with self.assertRaises(NotDerivable) as caught:
            derive_window_eligibility(delivered("ORD-B"), inactive, clock=CLOCK, category=category("ORD-A"))
        self.assertEqual(caught.exception.code, "policy_not_in_effect")
        with self.assertRaises(NotDerivable) as caught:
            derive_window_eligibility(delivered("ORD-B"), SCOPED_POLICY, clock=CLOCK,
                                      category=category("ORD-A", value="数码"))
        self.assertEqual(caught.exception.code, "category_not_in_scope")

    def test_codes_are_registered(self):
        self.assertIn(NOT_DERIVABLE_ORDER_LINK_MISSING, NOT_DERIVABLE_CODES)
        self.assertIn(NOT_DERIVABLE_ORDER_LINK_MISMATCH, NOT_DERIVABLE_CODES)
        self.assertEqual((NOT_DERIVABLE_ORDER_LINK_MISSING, NOT_DERIVABLE_ORDER_LINK_MISMATCH),
                         ("order_link_missing", "order_link_mismatch"))

    def test_cross_order_on_demo_data(self):
        """Tool-produced evidence: ORD-1001's item with ORD-1004's delivered package."""
        connection = memory_connection()
        self.addCleanup(connection.close)
        registry = build_runtime_registry()
        context = make_context(connection, now=DEMO_VIRTUAL_NOW)
        order = execute_tool(registry, context, "get_order", {"order_id": ORDER_A_DELIVERED})
        other = execute_tool(registry, context, "get_logistics", {"order_id": ORDER_A_TWO_PACKAGES})
        own = execute_tool(registry, context, "get_logistics", {"order_id": ORDER_A_DELIVERED})
        (item,) = [e for e in order.evidence if e.locator == "order_item:OI-1001-1#category"]
        (foreign,) = [e for e in other.evidence if e.locator == "logistics:SF1004A#delivered_at"]
        (mine,) = [e for e in own.evidence if e.locator == "logistics:SF1001#delivered_at"]
        with self.assertRaises(NotDerivable) as caught:
            derive_window_eligibility(foreign, GLOBAL_POLICY, clock=context.clock, category=item)
        self.assertEqual(caught.exception.code, NOT_DERIVABLE_ORDER_LINK_MISMATCH)
        fact = derive_window_eligibility(mine, GLOBAL_POLICY, clock=context.clock, category=item)
        self.assertEqual(fact.details["order_id"], ORDER_A_DELIVERED)


class MutationGuardTests(unittest.TestCase):
    """Removing the same-order comparison must be caught by the cross-order test."""

    CHECK = ("        if item_order != delivery_order:\n"
             "            raise NotDerivable(NOT_DERIVABLE_ORDER_LINK_MISMATCH)\n")

    def mutant(self):
        source = Path(derived_module.__file__).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(source.count(self.CHECK), 1)
        name = "aftersales._derived_same_order_mutant"
        module = types.ModuleType(name)
        module.__package__ = "aftersales"
        # dataclasses resolve annotations through sys.modules.
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source.replace(self.CHECK, ""), "derived_mutant.py", "exec"), module.__dict__)
        return module

    def test_without_the_check_cross_order_would_derive(self):
        mutant = self.mutant()
        item = mutant_evidence = category("ORD-A")
        fact = mutant.derive_window_eligibility(delivered("ORD-B"), SCOPED_POLICY, clock=CLOCK,
                                                category=mutant_evidence)
        # The mutant silently produces a window fact: exactly the bug the
        # real module refuses (see test_cross_order_rejected_*).
        self.assertIs(fact.value, True)
        with self.assertRaises(NotDerivable):
            derive_window_eligibility(delivered("ORD-B"), SCOPED_POLICY, clock=CLOCK, category=item)


if __name__ == "__main__":
    unittest.main()
