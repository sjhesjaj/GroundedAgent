"""The four read-only business tools, called directly (no executor).

Tests whose docstring says "Ports V1 <test id>" carry a V1 port_before_delete
property (docs/v2/v1-test-inventory.json) over to V2.
"""

import ast
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from aftersales import business_tools
from aftersales.business_tools import (
    AUTHORITY_SCOPE,
    BUSINESS_AUTHORITY,
    BUSINESS_HANDLERS,
    BUSINESS_SOURCE,
    BUSINESS_TRACE_FIELDS,
    IDENTITY_SCOPED_TOOLS,
    TOOL_PARAMETERS,
    TOOL_QUERIES,
)
from aftersales.errors import RecordIntegrityError
from aftersales.executor import execute_tool
from aftersales.registry import build_runtime_registry
from orchestration.contracts import SourceType, ToolStatus
from orchestration.document_adapter import DOCUMENT_AUTHORITY
from orchestration.wiki_adapter import WIKI_AUTHORITY

from tests.v2_support import (
    CUSTOMER_A,
    CUSTOMER_B,
    IDENTITY_SCOPED_CALLS,
    NON_STRING_VALUES,
    ORDER_A_DELIVERED,
    ORDER_A_IN_TRANSIT,
    ORDER_A_TWO_PACKAGES,
    ORDER_A_UNPAID,
    ORDER_B_DELIVERED,
    PERSONA_B,
    SKU_STOCKED,
    SKU_ZERO,
    VALID_BUSINESS_CALLS,
    RecordingConnection,
    make_context,
    memory_connection,
    snapshot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
# The modules that implement read-only tools.
TOOL_MODULES = {
    name: (REPO_ROOT / "aftersales" / name).read_text(encoding="utf-8")
    for name in ("business_tools.py", "policy.py")
}


def call(tool, context, arguments):
    return BUSINESS_HANDLERS[tool](context, arguments)


def string_constants(source: str) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


class BusinessToolTestCase(unittest.TestCase):
    def setUp(self):
        self.connection = memory_connection()
        self.addCleanup(self.connection.close)
        self.context = make_context(self.connection)

    def by_locator(self, result):
        return {item.locator: item for item in result.evidence}


class HappyPathTests(BusinessToolTestCase):
    def test_get_order_returns_the_order_and_its_items(self):
        result = call("get_order", self.context, {"order_id": ORDER_A_DELIVERED})
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.tool_name, "get_order")
        evidence = self.by_locator(result)
        self.assertEqual(evidence["order:ORD-1001#status"].metadata["value"], "已签收")
        self.assertEqual(evidence["order:ORD-1001#total_amount"].metadata["value"], "298.00")
        self.assertEqual(evidence["order_item:OI-1001-1#category"].metadata["value"], "服装")
        self.assertEqual(evidence["order_item:OI-1001-2#category"].metadata["value"], "贴身衣物")
        self.assertEqual(evidence["order_item:OI-1001-1#quantity"].metadata["value"], 2)
        self.assertEqual(
            evidence["order:ORD-1001#status"].content, "订单 ORD-1001 的状态为 已签收。"
        )
        self.assertEqual(result.trace["records_matched"], 3)

    def test_unpaid_order_reports_a_null_paid_at(self):
        result = call("get_order", self.context, {"order_id": ORDER_A_UNPAID})
        paid_at = self.by_locator(result)["order:ORD-1003#paid_at"]
        self.assertIsNone(paid_at.metadata["value"])
        self.assertEqual(paid_at.content, "订单 ORD-1003 的付款时间为 空。")

    def test_get_logistics_delivered_and_in_transit(self):
        delivered = self.by_locator(
            call("get_logistics", self.context, {"order_id": ORDER_A_DELIVERED})
        )
        self.assertEqual(delivered["logistics:SF1001#status"].metadata["value"], "已签收")
        self.assertEqual(
            delivered["logistics:SF1001#delivered_at"].metadata["value"],
            "2026-11-05T14:30:00+08:00",
        )
        transit = self.by_locator(
            call("get_logistics", self.context, {"order_id": ORDER_A_IN_TRANSIT})
        )
        self.assertEqual(transit["logistics:SF1002#status"].metadata["value"], "运输中")
        self.assertIsNone(transit["logistics:SF1002#delivered_at"].metadata["value"])

    def test_order_without_shipment_has_no_logistics(self):
        result = call("get_logistics", self.context, {"order_id": ORDER_A_UNPAID})
        self.assertEqual(result.status, ToolStatus.EMPTY)

    def test_get_inventory(self):
        result = call("get_inventory", self.context, {"sku": SKU_STOCKED})
        (evidence,) = result.evidence
        self.assertEqual(evidence.locator, "inventory:SKU-TSHIRT-M#available_qty")
        self.assertEqual(evidence.metadata["value"], 20)
        self.assertEqual(evidence.content, "SKU SKU-TSHIRT-M 的可售库存为 20。")

    def test_zero_quantity_is_present_not_absent(self):
        """Ports V1 tests.test_system_provider.HappyPathTests.test_zero_quantity_is_present_not_absent"""
        result = call("get_inventory", self.context, {"sku": SKU_ZERO})
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertIsNone(result.error_code)
        (evidence,) = result.evidence
        self.assertEqual(evidence.metadata["value"], 0)
        self.assertEqual(result.trace["records_matched"], 1)

    def test_get_after_sales_case(self):
        result = call("get_after_sales_case", self.context, {"order_id": ORDER_A_DELIVERED})
        evidence = self.by_locator(result)
        self.assertEqual(evidence["after_sales_case:AS-1001#type"].metadata["value"], "exchange")
        self.assertEqual(evidence["after_sales_case:AS-1001#status"].metadata["value"], "已完成")
        self.assertEqual(
            evidence["after_sales_case:AS-1001#order_item_id"].metadata["value"], "OI-1001-1"
        )

    def test_order_without_case_is_empty(self):
        result = call("get_after_sales_case", self.context, {"order_id": ORDER_A_IN_TRANSIT})
        self.assertEqual(result.status, ToolStatus.EMPTY)


class MultiPackageLogisticsTests(BusinessToolTestCase):
    """One order, two tracking numbers, two different statuses."""

    ORDER = ORDER_A_TWO_PACKAGES
    PACKAGES = ("SF1004A", "YT1004B")

    def stored(self, tracking_no, column):
        return self.connection.execute(
            "SELECT " + column + " FROM logistics WHERE tracking_no = ?", (tracking_no,)
        ).fetchone()[0]

    def test_every_package_is_returned_none_dropped_or_picked(self):
        result = call("get_logistics", self.context, {"order_id": self.ORDER})
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.trace["records_matched"], 2)
        record_ids = [item.metadata["record_id"] for item in result.evidence]
        # Deterministic order, both packages, each with all its fields.
        self.assertEqual(sorted(set(record_ids)), list(self.PACKAGES))
        self.assertEqual(record_ids, sorted(record_ids))
        fields_per_package = len(business_tools.LOGISTICS_QUERY.fields)
        for tracking_no in self.PACKAGES:
            self.assertEqual(record_ids.count(tracking_no), fields_per_package)
        self.assertEqual(len(result.evidence), 2 * fields_per_package)

    def test_statuses_differ_per_package(self):
        evidence = self.by_locator(call("get_logistics", self.context, {"order_id": self.ORDER}))
        self.assertEqual(evidence["logistics:SF1004A#status"].metadata["value"], "已签收")
        self.assertEqual(
            evidence["logistics:SF1004A#delivered_at"].metadata["value"],
            "2026-11-12T11:00:00+08:00",
        )
        self.assertEqual(evidence["logistics:YT1004B#status"].metadata["value"], "运输中")
        self.assertIsNone(evidence["logistics:YT1004B#delivered_at"].metadata["value"])
        for tracking_no in self.PACKAGES:
            self.assertEqual(
                evidence["logistics:" + tracking_no + "#order_id"].metadata["value"], self.ORDER
            )

    def test_locators_and_versions_are_independent_per_package(self):
        result = call("get_logistics", self.context, {"order_id": self.ORDER})
        locators = [item.locator for item in result.evidence]
        self.assertEqual(len(locators), len(set(locators)))
        for item in result.evidence:
            tracking_no = item.metadata["record_id"]
            with self.subTest(locator=item.locator):
                self.assertTrue(item.locator.startswith("logistics:" + tracking_no + "#"))
                self.assertEqual(item.state_version, self.stored(tracking_no, "version"))
                self.assertEqual(item.record_updated_at, self.stored(tracking_no, "updated_at"))
        versions = {item.metadata["record_id"]: item.state_version for item in result.evidence}
        self.assertNotEqual(versions["SF1004A"], versions["YT1004B"])

    def test_through_the_executor_the_trace_schema_holds(self):
        result = execute_tool(
            build_runtime_registry(), self.context, "get_logistics", {"order_id": self.ORDER},
            observation_id="obs-pkg",
        )
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.trace["records_matched"], 2)
        self.assertEqual(result.trace["evidence_count"], len(result.evidence))
        self.assertNotIn(self.ORDER, repr(result.trace))

    def test_another_customer_still_sees_nothing(self):
        other = make_context(self.connection, persona_id=PERSONA_B)
        result = call("get_logistics", other, {"order_id": self.ORDER})
        self.assertEqual(result.status, ToolStatus.EMPTY)
        self.assertEqual(result.evidence, ())
        missing = call("get_logistics", other, {"order_id": "ORD-9999"})
        self.assertEqual(result.to_dict(), missing.to_dict())


class CustomerIsolationTests(BusinessToolTestCase):
    def test_order_of_another_customer_is_not_readable(self):
        """Ports V1 tests.test_system_provider.SubjectIsolationTests.test_order_of_another_subject_is_not_readable"""
        result = call("get_order", self.context, {"order_id": ORDER_B_DELIVERED})
        self.assertEqual(result.status, ToolStatus.EMPTY)
        self.assertEqual(result.evidence, ())
        # Indistinguishable from a missing order: existence is not leaked.
        missing = call("get_order", self.context, {"order_id": "ORD-9999"})
        self.assertEqual(result.to_dict(), missing.to_dict())

    def test_case_and_logistics_of_another_customer_are_not_readable(self):
        """Ports V1 tests.test_system_provider.SubjectIsolationTests.test_approval_of_another_subject_is_not_readable"""
        for tool in ("get_after_sales_case", "get_logistics"):
            with self.subTest(tool=tool):
                result = call(tool, self.context, {"order_id": ORDER_B_DELIVERED})
                self.assertEqual(result.status, ToolStatus.EMPTY)

    def test_owner_can_read_their_own_record(self):
        """Ports V1 tests.test_system_provider.SubjectIsolationTests.test_owner_can_read_their_own_record"""
        owner = make_context(self.connection, persona_id=PERSONA_B)
        for tool in ("get_order", "get_logistics", "get_after_sales_case"):
            with self.subTest(tool=tool):
                result = call(tool, owner, {"order_id": ORDER_B_DELIVERED})
                self.assertEqual(result.status, ToolStatus.OK)

    def test_inventory_is_not_customer_scoped(self):
        for persona in ("demo-a", PERSONA_B):
            with self.subTest(persona=persona):
                context = make_context(self.connection, persona_id=persona)
                self.assertEqual(
                    call("get_inventory", context, {"sku": SKU_STOCKED}).status, ToolStatus.OK
                )


class UnknownRecordTests(BusinessToolTestCase):
    def test_unknown_records_return_empty(self):
        """Ports V1 tests.test_system_provider.UnknownRecordTests.test_unknown_records_return_empty"""
        cases = (
            ("get_order", {"order_id": "ORD-9999"}),
            ("get_logistics", {"order_id": "ORD-9999"}),
            ("get_inventory", {"sku": "SKU-NOPE"}),
            ("get_after_sales_case", {"order_id": "ORD-9999"}),
        )
        for tool, arguments in cases:
            with self.subTest(tool=tool):
                result = call(tool, self.context, arguments)
                self.assertEqual(result.status, ToolStatus.EMPTY)
                self.assertEqual(result.evidence, ())
                self.assertIsNone(result.error_code)
                self.assertIsNone(result.error_message)
                self.assertEqual(result.tool_name, tool)

    def test_empty_result_reports_zero_counts(self):
        """Ports V1 tests.test_system_provider.TraceContractTests.test_empty_result_reports_zero_counts"""
        result = call("get_inventory", self.context, {"sku": "SKU-NOPE"})
        self.assertEqual(result.trace["records_matched"], 0)
        self.assertEqual(result.trace["evidence_count"], 0)


class InputContractTests(BusinessToolTestCase):
    def test_arguments_must_be_a_mapping(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_parameters_must_be_a_dict"""
        for arguments in ([], "sku", None, 1, ("sku",)):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    call("get_inventory", self.context, arguments)

    def test_missing_parameters_are_rejected(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_missing_parameters_are_rejected"""
        for tool, valid in VALID_BUSINESS_CALLS:
            for name in TOOL_PARAMETERS[tool]:
                with self.subTest(tool=tool, missing=name):
                    arguments = dict(valid)
                    del arguments[name]
                    with self.assertRaises(ValueError):
                        call(tool, self.context, arguments)

    def test_blank_and_non_string_values_are_rejected(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_blank_and_non_string_values_are_rejected"""
        for tool, valid in VALID_BUSINESS_CALLS:
            for name in TOOL_PARAMETERS[tool]:
                for bad in ("", "   ", "x" * 129) + NON_STRING_VALUES:
                    with self.subTest(tool=tool, name=name, bad=repr(bad)[:20]):
                        arguments = dict(valid)
                        arguments[name] = bad
                        with self.assertRaises(ValueError):
                            call(tool, self.context, arguments)

    def test_extra_parameters_are_rejected(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_extra_parameters_are_rejected"""
        for tool, valid in VALID_BUSINESS_CALLS:
            for extra in ("extra", "sql", "table", "where", "limit"):
                with self.subTest(tool=tool, extra=extra):
                    arguments = dict(valid)
                    arguments[extra] = "x"
                    with self.assertRaises(ValueError):
                        call(tool, self.context, arguments)

    def test_identity_arguments_are_rejected_by_every_tool(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_inventory_rejects_subject_id"""
        for tool, valid in VALID_BUSINESS_CALLS:
            for key in ("customer_id", "persona_id", "subject_id"):
                with self.subTest(tool=tool, key=key):
                    arguments = dict(valid)
                    arguments[key] = CUSTOMER_B
                    with self.assertRaises(ValueError) as caught:
                        call(tool, self.context, arguments)
                    self.assertIn("trusted context", str(caught.exception))
                    self.assertNotIn(CUSTOMER_B, str(caught.exception))

    def test_non_string_parameter_keys_are_rejected(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_non_string_parameter_keys_are_rejected"""
        for key in (1, None, (1, 2), 1.5, True, frozenset({"a"})):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    call("get_inventory", self.context, {"sku": SKU_STOCKED, key: "xyzzy"})
                message = str(caught.exception)
                self.assertIn("get_inventory.arguments", message)
                self.assertNotIn("xyzzy", message)

    def test_mixed_string_and_non_string_extra_keys_are_rejected(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_mixed_string_and_non_string_extra_keys_are_rejected"""
        with self.assertRaises(ValueError):
            call("get_inventory", self.context, {"sku": SKU_STOCKED, "zzz": "x", 1: "y", None: "z"})

    def test_messages_name_the_tool_and_parameter_but_never_the_value(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_messages_name_the_operation_and_parameter_but_never_the_value"""
        secret = "ORD-super-secret"
        for arguments, expected in (
            ({"order_id": ""}, "get_order.arguments.order_id"),
            ({"order_id": secret, "extra": secret}, "get_order.arguments"),
            ({"order_id": secret, "customer_id": secret}, "get_order.arguments"),
            ({"order_id": 5}, "get_order.arguments.order_id"),
        ):
            with self.subTest(arguments=sorted(arguments)):
                with self.assertRaises(ValueError) as caught:
                    call("get_order", self.context, arguments)
                message = str(caught.exception)
                self.assertIn(expected, message)
                self.assertNotIn(secret, message)
                self.assertNotIn(CUSTOMER_A, message)

    def test_invalid_input_never_reaches_the_connection(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_invalid_input_never_reaches_the_connection"""
        recording = RecordingConnection(self.connection)
        context = make_context(recording)
        bad_calls = (
            ("get_order", {}),
            ("get_order", {"order_id": ""}),
            ("get_order", {"order_id": ORDER_A_DELIVERED, "customer_id": CUSTOMER_B}),
            ("get_inventory", {"sku": SKU_STOCKED, "extra": "x"}),
            ("get_inventory", ["sku"]),
            ("get_logistics", {"order_id": None}),
            ("get_after_sales_case", {"order_id": "x" * 500}),
        )
        for tool, arguments in bad_calls:
            with self.subTest(tool=tool, arguments=repr(arguments)[:40]):
                with self.assertRaises(ValueError):
                    call(tool, context, arguments)
        self.assertEqual(recording.cursor_calls, 0)
        self.assertEqual(recording.executed, [])

    def test_non_string_key_never_reaches_the_connection(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_non_string_key_never_reaches_the_connection"""
        recording = RecordingConnection(self.connection)
        context = make_context(recording)
        for arguments in (
            {"sku": SKU_STOCKED, 1: "x"},
            {"sku": SKU_STOCKED, None: "x"},
            {"sku": SKU_STOCKED, (1, 2): "x", "extra": "y"},
            {1: "x"},
        ):
            with self.subTest(arguments=sorted(map(repr, arguments))):
                with self.assertRaises(ValueError):
                    call("get_inventory", context, arguments)
        self.assertEqual(recording.cursor_calls, 0)
        self.assertEqual(recording.executed, [])


class CursorContractTests(BusinessToolTestCase):
    def test_caller_row_factory_is_isolated_and_preserved(self):
        """Ports V1 tests.test_system_provider.CursorContractTests.test_caller_row_factory_is_isolated_and_preserved"""
        baseline = call("get_order", self.context, {"order_id": ORDER_A_DELIVERED})

        def dict_factory(cursor, row):
            return {column[0]: value for column, value in zip(cursor.description, row)}

        self.connection.row_factory = dict_factory
        for tool, arguments in VALID_BUSINESS_CALLS:
            with self.subTest(tool=tool):
                self.assertEqual(call(tool, self.context, arguments).status, ToolStatus.OK)
        with_factory = call("get_order", self.context, {"order_id": ORDER_A_DELIVERED})
        self.assertEqual(with_factory.to_dict(), baseline.to_dict())
        self.assertIs(self.connection.row_factory, dict_factory)

    def test_cursor_is_closed_on_success_and_on_database_error(self):
        """Ports V1 tests.test_system_provider.CursorContractTests.test_cursor_is_closed_on_success_and_on_database_error"""
        recording = RecordingConnection(self.connection)
        context = make_context(recording)
        call("get_order", context, {"order_id": ORDER_A_DELIVERED})
        self.assertEqual(recording.closed_cursors, 2)  # order + items

        self.connection.execute("DROP TABLE logistics")
        with self.assertRaises(sqlite3.Error):
            call("get_logistics", context, {"order_id": ORDER_A_DELIVERED})
        self.assertEqual(recording.closed_cursors, 3)
        self.assertTrue(all(cursor.closed for cursor in recording.cursors))


class SqlSafetyTests(BusinessToolTestCase):
    def test_queries_use_placeholders_and_pass_bindings_separately(self):
        """Ports V1 tests.test_system_provider.SqlSafetyTests.test_queries_use_placeholders_and_pass_bindings_separately"""
        recording = RecordingConnection(self.connection)
        call("get_order", make_context(recording), {"order_id": ORDER_A_DELIVERED})
        self.assertEqual(len(recording.executed), 2)
        for sql, bindings in recording.executed:
            self.assertIn("?", sql)
            self.assertNotIn(CUSTOMER_A, sql)
            self.assertNotIn(ORDER_A_DELIVERED, sql)
            self.assertEqual(bindings, (CUSTOMER_A, ORDER_A_DELIVERED))

        recording = RecordingConnection(self.connection)
        call("get_inventory", make_context(recording), {"sku": SKU_STOCKED})
        ((sql, bindings),) = recording.executed
        self.assertNotIn(SKU_STOCKED, sql)
        # Inventory is not customer-scoped: no identity is bound.
        self.assertEqual(bindings, (SKU_STOCKED,))

    def test_identity_is_bound_from_the_trusted_context_only(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_subject_id_is_injected_for_subject_scoped_operations"""
        for persona, customer in (("demo-a", CUSTOMER_A), (PERSONA_B, CUSTOMER_B)):
            for tool, arguments in IDENTITY_SCOPED_CALLS:
                with self.subTest(persona=persona, tool=tool):
                    recording = RecordingConnection(self.connection)
                    call(tool, make_context(recording, persona_id=persona), arguments)
                    for _sql, bindings in recording.executed:
                        self.assertEqual(bindings, (customer,) + tuple(arguments.values()))

    def test_sql_templates_are_fixed_strings_with_placeholders_only(self):
        """Ports V1 tests.test_system_provider.SqlSafetyTests.test_sql_templates_are_fixed_strings_with_placeholders_only"""
        self.assertEqual(set(TOOL_QUERIES), set(BUSINESS_HANDLERS))
        for tool, queries in TOOL_QUERIES.items():
            for query in queries:
                with self.subTest(tool=tool, table=query.table):
                    sql = query.sql
                    self.assertIsInstance(sql, str)
                    self.assertTrue(sql.startswith("SELECT "))
                    self.assertNotIn(";", sql)
                    for artifact in ("{", "}", "%s", "%d", "+"):
                        self.assertNotIn(artifact, sql)
                    self.assertEqual(sql.count("?"), len(query.bindings))
                    # Only declared parameters and the trusted identity bind.
                    allowed = set(TOOL_PARAMETERS[tool])
                    if tool in IDENTITY_SCOPED_TOOLS:
                        allowed.add("customer_id")
                        self.assertEqual(query.bindings[0], "customer_id")
                    self.assertLessEqual(set(query.bindings), allowed)

    def test_identity_is_a_predicate_never_a_selected_column(self):
        for queries in TOOL_QUERIES.values():
            for query in queries:
                with self.subTest(table=query.table):
                    self.assertNotIn("customer_id", query.columns)
                    selected = query.sql.split(" FROM ")[0]
                    self.assertNotIn("customer_id", selected)

    def test_module_uses_no_f_strings(self):
        """Ports V1 tests.test_system_provider.SqlSafetyTests.test_module_uses_no_f_strings"""
        for name, source in TOOL_MODULES.items():
            with self.subTest(module=name):
                joined = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.JoinedStr)]
                self.assertEqual(joined, [])
                self.assertNotIn(".format(", source)
                self.assertNotIn("% (", source)

    def test_module_contains_no_write_statements(self):
        """Ports V1 tests.test_system_provider.SqlSafetyTests.test_module_contains_no_write_statements"""
        for name, source in TOOL_MODULES.items():
            for forbidden in (
                "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
                "PRAGMA", "ATTACH", "executescript", "executemany", "commit(",
            ):
                with self.subTest(module=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_injection_strings_are_treated_as_values(self):
        """Ports V1 tests.test_system_provider.SqlSafetyTests.test_injection_strings_are_treated_as_values"""
        before = snapshot(self.connection)
        injections = (
            ("get_order", {"order_id": "' OR '1'='1"}),
            ("get_order", {"order_id": ORDER_A_DELIVERED + "' OR 1=1 --"}),
            ("get_order", {"order_id": "x'; DROP TABLE orders;--"}),
            ("get_logistics", {"order_id": "' UNION SELECT * FROM logistics --"}),
            ("get_after_sales_case", {"order_id": "%"}),
            ("get_inventory", {"sku": "%"}),
            ("get_inventory", {"sku": SKU_STOCKED + "' OR 1=1 --"}),
            ("get_inventory", {"sku": "*"}),
        )
        for tool, arguments in injections:
            with self.subTest(tool=tool, value=list(arguments.values())[0]):
                result = call(tool, self.context, arguments)
                self.assertEqual(result.status, ToolStatus.EMPTY)
                self.assertEqual(result.evidence, ())
        self.assertEqual(snapshot(self.connection), before)

    def test_queries_never_write(self):
        """Ports V1 tests.test_system_provider.SqlSafetyTests.test_queries_never_write"""
        before = snapshot(self.connection)
        changes_before = self.connection.total_changes
        for tool, arguments in VALID_BUSINESS_CALLS:
            call(tool, self.context, arguments)
        call("get_inventory", self.context, {"sku": "SKU-NOPE"})
        call("get_order", self.context, {"order_id": ORDER_B_DELIVERED})
        self.assertEqual(snapshot(self.connection), before)
        self.assertEqual(self.connection.total_changes, changes_before)


class MultiRowDefenceTests(unittest.TestCase):
    """A duplicated lookup key must fail loudly, not silently pick a row."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        stamp = "'2026-11-01T00:00:00+08:00'"
        self.connection.executescript(
            "CREATE TABLE orders (order_id TEXT, customer_id TEXT, status TEXT,"
            " paid_at TEXT, total_amount TEXT, updated_at TEXT, version INTEGER);"
            "CREATE TABLE order_items (order_item_id TEXT, order_id TEXT, sku TEXT,"
            " product_name TEXT, category TEXT, quantity INTEGER, unit_price TEXT,"
            " updated_at TEXT, version INTEGER);"
            "CREATE TABLE logistics (tracking_no TEXT, order_id TEXT, carrier TEXT,"
            " status TEXT, shipped_at TEXT, delivered_at TEXT, last_event_at TEXT,"
            " updated_at TEXT, version INTEGER);"
            "CREATE TABLE inventory (sku TEXT, available_qty INTEGER, updated_at TEXT,"
            " version INTEGER);"
            "INSERT INTO orders VALUES ('O-1', 'CUST-001', 'A', NULL, '1.00', " + stamp + ", 1);"
            "INSERT INTO orders VALUES ('O-1', 'CUST-001', 'B', NULL, '1.00', " + stamp + ", 1);"
            "INSERT INTO logistics VALUES ('T-1', 'O-1', 'c', 's', " + stamp + ", NULL, "
            + stamp + ", " + stamp + ", 1);"
            "INSERT INTO logistics VALUES ('T-2', 'O-1', 'c', 's', " + stamp + ", NULL, "
            + stamp + ", " + stamp + ", 1);"
            "INSERT INTO inventory VALUES ('S-1', 1, " + stamp + ", 1);"
            "INSERT INTO inventory VALUES ('S-1', 2, " + stamp + ", 1);"
        )

    def test_duplicate_rows_raise_naming_the_tool(self):
        """Ports V1 tests.test_system_provider.MultiRowDefenceTests.test_duplicate_rows_raise_naming_the_operation"""
        # Primary-key lookups only. get_logistics is a 0..N listing by design.
        for tool, arguments in (
            ("get_order", {"order_id": "O-1"}),
            ("get_inventory", {"sku": "S-1"}),
        ):
            with self.subTest(tool=tool):
                recording = RecordingConnection(self.connection)
                with self.assertRaises(RecordIntegrityError) as caught:
                    call(tool, make_context(recording), arguments)
                self.assertIn(tool, str(caught.exception))
                self.assertEqual(recording.closed_cursors, 1)
                self.assertTrue(all(cursor.closed for cursor in recording.cursors))

    def test_naive_timestamp_or_bad_version_is_an_integrity_fault(self):
        for column, value in (("updated_at", "2026-11-01T00:00:00"), ("version", 0)):
            with self.subTest(column=column):
                connection = sqlite3.connect(":memory:")
                self.addCleanup(connection.close)
                connection.execute(
                    "CREATE TABLE inventory (sku TEXT, available_qty INTEGER,"
                    " updated_at TEXT, version INTEGER)"
                )
                row = {"sku": "S-9", "available_qty": 1,
                       "updated_at": "2026-11-01T00:00:00+08:00", "version": 1}
                row[column] = value
                connection.execute(
                    "INSERT INTO inventory VALUES (?, ?, ?, ?)", tuple(row.values())
                )
                with self.assertRaises(RecordIntegrityError) as caught:
                    call("get_inventory", make_context(connection), {"sku": "S-9"})
                self.assertNotIn("2026-11-01", str(caught.exception))


class DatabaseErrorTests(BusinessToolTestCase):
    def test_missing_table_raises_rather_than_returning_empty(self):
        """Ports V1 tests.test_system_provider.DatabaseErrorTests.test_missing_table_raises_rather_than_returning_empty"""
        for tool, table in (
            ("get_inventory", "inventory"),
            ("get_after_sales_case", "after_sales_cases"),
        ):
            with self.subTest(tool=tool):
                self.connection.execute("DROP TABLE " + table)
                arguments = dict(VALID_BUSINESS_CALLS)[tool]
                with self.assertRaises(sqlite3.Error):
                    call(tool, self.context, arguments)

    def test_closed_connection_raises(self):
        """Ports V1 tests.test_system_provider.DatabaseErrorTests.test_closed_connection_raises"""
        connection = memory_connection()
        connection.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            call("get_inventory", make_context(connection), {"sku": SKU_STOCKED})

    def test_through_the_executor_a_fault_is_an_error_never_empty(self):
        registry = build_runtime_registry()
        closed = memory_connection()
        closed.close()
        broken = memory_connection()
        self.addCleanup(broken.close)
        broken.execute("DROP TABLE orders")
        for name, connection in (("closed", closed), ("missing_table", broken)):
            with self.subTest(fault=name):
                result = execute_tool(
                    registry, make_context(connection), "get_order",
                    {"order_id": ORDER_A_DELIVERED},
                )
                self.assertEqual(result.status, ToolStatus.ERROR)
                self.assertNotEqual(result.status, ToolStatus.EMPTY)
                self.assertEqual(result.error_code, "tool_error")
                rendered = repr(result.to_dict())
                for leaked in (ORDER_A_DELIVERED, CUSTOMER_A, "orders", "SELECT", "no such"):
                    self.assertNotIn(leaked, rendered)


class EvidenceMappingTests(BusinessToolTestCase):
    def test_authority_scope_and_ordering(self):
        """Ports V1 tests.test_system_provider.EvidenceMappingTests.test_authority_scope_and_ordering"""
        self.assertLess(WIKI_AUTHORITY, DOCUMENT_AUTHORITY)
        self.assertLess(DOCUMENT_AUTHORITY, BUSINESS_AUTHORITY)
        self.assertEqual(BUSINESS_AUTHORITY, 100)
        for tool, arguments in VALID_BUSINESS_CALLS:
            for evidence in call(tool, self.context, arguments).evidence:
                with self.subTest(tool=tool, locator=evidence.locator):
                    self.assertEqual(evidence.authority, BUSINESS_AUTHORITY)
                    self.assertEqual(evidence.metadata["authority_scope"], AUTHORITY_SCOPE)
                    self.assertEqual(evidence.source_type, SourceType.BUSINESS)
                    self.assertEqual(evidence.source, BUSINESS_SOURCE)

    def test_confidence_is_none_and_state_version_is_the_record_version(self):
        """Tests the V2 semantics that supersede V1 tests.test_system_provider.EvidenceMappingTests.test_confidence_and_version_are_none"""
        for tool, arguments in VALID_BUSINESS_CALLS:
            for evidence in call(tool, self.context, arguments).evidence:
                with self.subTest(locator=evidence.locator):
                    self.assertIsNone(evidence.confidence)
                    self.assertIsInstance(evidence.state_version, int)
                    self.assertGreaterEqual(evidence.state_version, 1)

    def test_customer_id_never_reaches_evidence(self):
        """Ports V1 tests.test_system_provider.EvidenceMappingTests.test_subject_id_never_reaches_evidence"""
        for persona, customer in (("demo-a", CUSTOMER_A), (PERSONA_B, CUSTOMER_B)):
            context = make_context(self.connection, persona_id=persona)
            for tool in ("get_order", "get_logistics", "get_after_sales_case"):
                order = ORDER_A_DELIVERED if persona == "demo-a" else ORDER_B_DELIVERED
                result = call(tool, context, {"order_id": order})
                self.assertEqual(result.status, ToolStatus.OK)
                for evidence in result.evidence:
                    with self.subTest(persona=persona, locator=evidence.locator):
                        self.assertNotIn(customer, evidence.content)
                        self.assertNotIn(customer, evidence.locator)
                        self.assertNotIn(customer, repr(evidence.metadata))
                        self.assertNotIn(customer, repr(evidence.to_dict()))
                        self.assertNotIn("customer_id", repr(evidence.to_dict()))


class TraceContractTests(BusinessToolTestCase):
    def test_trace_is_exactly_the_published_fields(self):
        """Ports V1 tests.test_system_provider.TraceContractTests.test_trace_has_caller_keys_plus_provider_fields (V2: no caller trace)"""
        result = call("get_order", self.context, {"order_id": ORDER_A_DELIVERED})
        self.assertEqual(set(result.trace), set(BUSINESS_TRACE_FIELDS))
        self.assertEqual(
            result.trace,
            {
                "tool": "get_order",
                "parameter_names": ["order_id"],
                "identity_scoped": True,
                "records_matched": 3,
                "evidence_count": len(result.evidence),
            },
        )

    def test_trace_leaks_no_values_or_sql(self):
        """Ports V1 tests.test_system_provider.TraceContractTests.test_trace_leaks_no_values_or_sql"""
        for tool, arguments in VALID_BUSINESS_CALLS:
            with self.subTest(tool=tool):
                rendered = repr(call(tool, self.context, arguments).trace)
                self.assertNotIn(CUSTOMER_A, rendered)
                for value in arguments.values():
                    self.assertNotIn(value, rendered)
                for sql_token in ("SELECT", "FROM", "WHERE", "?"):
                    self.assertNotIn(sql_token, rendered)


class IsolationTests(BusinessToolTestCase):
    def test_tools_never_open_a_connection(self):
        """Ports V1 tests.test_system_provider.IsolationTests.test_provider_never_opens_a_connection"""
        registry = build_runtime_registry()
        with patch(
            "sqlite3.connect",
            side_effect=AssertionError("tools must not open a connection"),
        ):
            for tool, arguments in VALID_BUSINESS_CALLS:
                with self.subTest(tool=tool):
                    call(tool, self.context, arguments)
                    execute_tool(registry, self.context, tool, arguments)

    def test_tool_source_touches_no_database_file_or_product_table(self):
        """Ports V1 tests.test_system_provider.IsolationTests.test_provider_source_touches_no_database_file_or_product_table"""
        product = ("knowledge_chunks", "conversations", "messages", "app_meta", ".db")
        for name, source in TOOL_MODULES.items():
            with self.subTest(module=name):
                self.assertNotIn("import storage", source)
                self.assertNotIn("sqlite3.connect", source)
                self.assertNotIn("import sqlite3", source)
                for constant in string_constants(source):
                    for forbidden in product:
                        self.assertNotIn(forbidden, constant)

    def test_no_database_file_is_created_in_the_permitted_areas(self):
        """Ports V1 tests.test_system_provider.IsolationTests.test_no_database_file_is_created_in_the_permitted_areas"""
        for directory in (
            REPO_ROOT,
            REPO_ROOT / "aftersales",
            REPO_ROOT / "tests",
            REPO_ROOT / "system_fixtures",
        ):
            with self.subTest(directory=directory.name):
                self.assertEqual(list(directory.glob("*.db")), [])

    def test_module_attribute_exposes_no_connection_factory(self):
        self.assertFalse(hasattr(business_tools, "sqlite3"))


if __name__ == "__main__":
    unittest.main()
