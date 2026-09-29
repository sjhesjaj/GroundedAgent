"""The V2 tool executor: side-effect refusal, closed arguments, privacy, errors.

Tests whose docstring says "Ports V1 <test id>" carry a V1 port_before_delete
property (docs/v2/v1-test-inventory.json) over to V2.
"""

import dataclasses
import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from aftersales.business_tools import BUSINESS_HANDLERS, TOOL_PARAMETERS
from aftersales.errors import SideEffectForbidden, ToolNotReady
from aftersales.executor import (
    ERROR_CODE_NOT_READY,
    ERROR_CODE_TOOL_ERROR,
    ERROR_CODE_TOOL_REPORTED,
    ReadOnlyViolation,
    execute_tool,
)
from aftersales.registry import ToolKind, ToolRegistry, ToolSpec, build_runtime_registry
from orchestration.contracts import OBSERVATION_ID_KEY, ToolResult, ToolStatus

from tests.v2_support import (
    CUSTOMER_A,
    ORDER_A_DELIVERED,
    SKU_STOCKED,
    VALID_BUSINESS_CALLS,
    RecordingConnection,
    make_context,
    memory_connection,
    snapshot,
)

ORDER_SENTINEL = "ORD-sentinel-8853"


def replaced(registry: ToolRegistry, name: str, **changes) -> ToolRegistry:
    """The same registry with one spec altered."""
    return ToolRegistry(
        dataclasses.replace(spec, **changes) if spec.name == name else spec
        for spec in registry
    )


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.connection = memory_connection()
        self.addCleanup(self.connection.close)
        self.context = make_context(self.connection)
        self.registry = build_runtime_registry()

    def real_order_result(self) -> ToolResult:
        return BUSINESS_HANDLERS["get_order"](self.context, {"order_id": ORDER_A_DELIVERED})

    def with_trace(self, trace, evidence=None):
        """get_order whose handler returns real evidence with `trace`."""
        evidence = self.real_order_result().evidence if evidence is None else evidence
        handler = MagicMock(
            return_value=ToolResult(
                tool_name="get_order", status=ToolStatus.OK, evidence=evidence, trace=trace
            )
        )
        return replaced(self.registry, "get_order", handler=handler)

    def business_trace(self, **overrides):
        trace = {
            "tool": "get_order",
            "parameter_names": ["order_id"],
            "identity_scoped": True,
            "records_matched": 3,
            "evidence_count": len(self.real_order_result().evidence),
        }
        trace.update(overrides)
        return trace

    def assert_value_error(self, registry, tool, arguments, context=None) -> str:
        with self.assertRaises(ValueError) as caught:
            execute_tool(registry, context or self.context, tool, arguments)
        return str(caught.exception)


class SideEffectGuardTests(ExecutorTestCase):
    def test_a_side_effect_tool_is_refused_before_anything_runs(self):
        handler = MagicMock()
        registry = ToolRegistry(
            [
                ToolSpec(
                    name="some_future_action",
                    description="a future action that must never run in Stage 4",
                    kind=ToolKind.BUSINESS_READ,
                    parameters=(),
                    side_effect=True,
                    identity_scoped=True,
                    handler=handler,
                )
            ]
        )
        recording = RecordingConnection(self.connection)
        before = snapshot(self.connection)
        for arguments in ({}, {"anything": "x"}, None):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SideEffectForbidden):
                    execute_tool(registry, make_context(recording), "some_future_action", arguments)
        handler.assert_not_called()
        self.assertEqual(recording.cursor_calls, 0)
        self.assertEqual(snapshot(self.connection), before)

    def test_flipping_a_registered_read_tool_to_side_effect_is_refused(self):
        for name in self.registry.names():
            with self.subTest(tool=name):
                handler = MagicMock()
                registry = replaced(self.registry, name, side_effect=True, handler=handler)
                with self.assertRaises(SideEffectForbidden):
                    execute_tool(registry, self.context, name, {"query": "x"})
                handler.assert_not_called()

    def test_side_effect_must_be_a_real_bool(self):
        spec = next(iter(self.registry))
        for truthy in (1, "false", None, "no"):
            with self.subTest(value=truthy):
                with self.assertRaises(ValueError):
                    dataclasses.replace(spec, side_effect=truthy)


class ArgumentBoundaryTests(ExecutorTestCase):
    def test_unknown_or_non_string_tool_is_rejected(self):
        """Ports V1 tests.test_system_provider.InputContractTests.test_unknown_operation_is_rejected"""
        for name in ("drop_everything", "create_return", "GET_ORDER", "", None, 1):
            with self.subTest(name=name):
                message = self.assert_value_error(self.registry, name, {"order_id": "x"})
                if isinstance(name, str) and name:
                    self.assertNotIn(name, message)

    def test_smuggled_identity_is_rejected_without_echo(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_smuggled_subject_id_is_rejected"""
        for tool, valid in VALID_BUSINESS_CALLS:
            for key in ("customer_id", "persona_id", "subject_id", "role"):
                with self.subTest(tool=tool, key=key):
                    arguments = dict(valid)
                    arguments[key] = "attacker-value"
                    message = self.assert_value_error(self.registry, tool, arguments)
                    self.assertNotIn("attacker-value", message)

    def test_parameter_whitelist_is_the_tools_own(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_whitelist_comes_from_m4"""
        for tool, parameters in TOOL_PARAMETERS.items():
            with self.subTest(tool=tool):
                self.assertEqual(self.registry.get(tool).parameter_names, parameters)
        self.assert_value_error(
            self.registry, "get_inventory", {"sku": SKU_STOCKED, "extra": "y"}
        )

    def test_invalid_input_never_reaches_the_handler_or_connection(self):
        handler = MagicMock()
        registry = replaced(self.registry, "get_order", handler=handler)
        recording = RecordingConnection(self.connection)
        for arguments in ({}, {"order_id": ""}, {"order_id": 1}, {"order_id": "x", "y": "z"}, []):
            with self.subTest(arguments=repr(arguments)):
                self.assert_value_error(registry, "get_order", arguments, make_context(recording))
        handler.assert_not_called()
        self.assertEqual(recording.cursor_calls, 0)

    def test_caller_arguments_are_copied_not_reused(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_caller_parameters_are_copied_not_reused"""
        handler = MagicMock(return_value=ToolResult(tool_name="get_order", status=ToolStatus.EMPTY,
                                                    trace=self.business_trace(records_matched=0,
                                                                              evidence_count=0)))
        registry = replaced(self.registry, "get_order", handler=handler)
        caller_arguments = {"order_id": ORDER_SENTINEL}
        execute_tool(registry, self.context, "get_order", caller_arguments)
        passed = handler.call_args[0][1]
        self.assertIsNot(passed, caller_arguments)
        self.assertEqual(passed, {"order_id": ORDER_SENTINEL})
        self.assertEqual(caller_arguments, {"order_id": ORDER_SENTINEL})

    def test_inventory_never_receives_an_identity(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_subject_id_is_not_injected_for_inventory
        Ports V1 tests.test_executor.SystemBoundaryTests.test_inventory_works_without_a_subject
        (V2: every context has a persona; inventory must simply not use it)."""
        recording = RecordingConnection(self.connection)
        result = execute_tool(self.registry, make_context(recording), "get_inventory", {"sku": SKU_STOCKED})
        self.assertEqual(result.status, ToolStatus.OK)
        ((_sql, bindings),) = recording.executed
        self.assertEqual(bindings, (SKU_STOCKED,))
        self.assertIs(self.registry.get("get_inventory").identity_scoped, False)

    def test_identity_scoped_tools_require_a_trusted_identity(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_subject_scoped_operations_require_a_trusted_subject
        (V2: a context cannot exist without a valid persona, so the check moves to construction)."""
        from aftersales.context import Persona, TrustedExecutionContext
        from aftersales.clock import FixedClock
        from aftersales.demo import DEMO_VIRTUAL_NOW

        for customer in (None, "", "   ", 1):
            with self.subTest(customer=customer):
                with self.assertRaises(ValueError):
                    Persona(persona_id="p", customer_id=customer, display_name="d")
        for persona in (None, "CUST-001", {"customer_id": "CUST-001"}):
            with self.subTest(persona=persona):
                with self.assertRaises(ValueError):
                    TrustedExecutionContext(
                        persona=persona, clock=FixedClock(DEMO_VIRTUAL_NOW),
                        connection=self.connection,
                    )
        for context in (None, object(), {"customer_id": CUSTOMER_A}):
            with self.subTest(context=type(context).__name__):
                with self.assertRaises(ValueError):
                    execute_tool(self.registry, context, "get_order", {"order_id": "x"})

    def test_observation_id_must_be_text_or_none(self):
        for bad in ("", "  ", 1, object()):
            with self.subTest(bad=repr(bad)):
                self.assert_value_error_kw(bad)

    def assert_value_error_kw(self, observation_id):
        with self.assertRaises(ValueError):
            execute_tool(self.registry, self.context, "get_inventory", {"sku": SKU_STOCKED},
                         observation_id=observation_id)


class TracePublicationTests(ExecutorTestCase):
    def test_trace_carrying_an_argument_value_or_identity_is_rejected(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_adapter_trace_carrying_a_parameter_value_is_rejected"""
        leaky = {
            "argument_value": {"debug_order_id": ORDER_SENTINEL},
            "identity_value": {"debug_customer": CUSTOMER_A},
            "embedded_in_text": {"note": "looked up " + ORDER_SENTINEL + " ok"},
            "nested": {"outer": {"inner": [ORDER_SENTINEL]}},
            "as_a_key": {ORDER_SENTINEL: "seen"},
            "inside_a_published_field": self.business_trace(tool="get_order " + CUSTOMER_A),
        }
        for name, trace in leaky.items():
            with self.subTest(case=name):
                message = self.assert_value_error(
                    self.with_trace(trace), "get_order", {"order_id": ORDER_SENTINEL}
                )
                self.assertNotIn(ORDER_SENTINEL, message)
                self.assertNotIn(CUSTOMER_A, message)

    def test_sql_in_a_trace_is_rejected(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_sql_in_a_trace_is_rejected"""
        sql = "SELECT order_id, status FROM orders WHERE customer_id = ?"
        leaky = {
            "top_level": {"debug_sql": sql},
            "nested": {"tool": "get_order", "detail": {"query": sql}},
            "in_a_list": {"tool": "get_order", "queries": [sql]},
        }
        for name, trace in leaky.items():
            with self.subTest(case=name):
                message = self.assert_value_error(
                    self.with_trace(trace), "get_order", {"order_id": ORDER_A_DELIVERED}
                )
                self.assertNotIn("SELECT", message.upper())
                self.assertNotIn("orders", message)

    def test_whitespace_separated_sql_is_rejected(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_whitespace_separated_sql_is_rejected"""
        for sql in (
            "SELECT\norder_id\nFROM\norders",
            "SELECT\torder_id\tFROM\torders",
            "select\n\n  order_id  \r\n from   orders",
        ):
            with self.subTest(sql=repr(sql[:20])):
                message = self.assert_value_error(
                    self.with_trace(self.business_trace(tool=sql)),
                    "get_order", {"order_id": ORDER_A_DELIVERED},
                )
                self.assertNotIn("orders", message)

    def test_business_trace_schema_is_enforced(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_system_trace_schema_is_enforced"""
        cases = {
            "payload_in_a_count": self.business_trace(records_matched={"raw_row": "customer-secret-991"}),
            "payload_in_a_list": self.business_trace(evidence_count=["customer-secret-991"]),
            "count_is_a_string": self.business_trace(records_matched="3"),
            "count_is_a_bool": self.business_trace(records_matched=True),
            "count_is_negative": self.business_trace(records_matched=-1),
            "tool_disagrees": self.business_trace(tool="get_inventory"),
            "tool_not_a_string": self.business_trace(tool=7),
            "names_disagree": self.business_trace(parameter_names=["sku"]),
            "names_not_a_list": self.business_trace(parameter_names="order_id"),
            "identity_flag_wrong": self.business_trace(identity_scoped=False),
            "identity_flag_truthy": self.business_trace(identity_scoped=1),
            "evidence_count_disagrees": self.business_trace(evidence_count=99),
            "records_zero_with_evidence": self.business_trace(records_matched=0),
        }
        for name, trace in cases.items():
            with self.subTest(case=name):
                message = self.assert_value_error(
                    self.with_trace(trace), "get_order", {"order_id": ORDER_A_DELIVERED}
                )
                self.assertNotIn("customer-secret-991", message)

    def test_missing_trace_fields_are_rejected(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_missing_system_trace_fields_are_rejected"""
        full = self.business_trace()
        for name in sorted(full):
            with self.subTest(missing=name):
                partial = {k: v for k, v in full.items() if k != name}
                message = self.assert_value_error(
                    self.with_trace(partial), "get_order", {"order_id": ORDER_A_DELIVERED}
                )
                self.assertIn(name, message)
        self.assert_value_error(self.with_trace({}), "get_order", {"order_id": ORDER_A_DELIVERED})

    def test_unpublished_trace_fields_are_rejected(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_unpublished_system_trace_fields_are_rejected"""
        trace = self.business_trace(debug_note="harmless looking")
        message = self.assert_value_error(
            self.with_trace(trace), "get_order", {"order_id": ORDER_A_DELIVERED}
        )
        self.assertIn("debug_note", message)

    def test_parameter_names_in_a_trace_are_allowed(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_parameter_names_in_a_trace_are_allowed"""
        result = execute_tool(
            self.with_trace(self.business_trace()), self.context, "get_order",
            {"order_id": ORDER_A_DELIVERED},
        )
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.trace["parameter_names"], ["order_id"])

    def test_an_argument_equal_to_a_published_name_is_not_a_leak(self):
        # "order_id" is both the argument value and a field name in the trace.
        result = execute_tool(self.registry, self.context, "get_order", {"order_id": "order_id"})
        self.assertEqual(result.status, ToolStatus.EMPTY)

    def test_a_real_trace_passes_the_whole_schema(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_a_real_m4_trace_passes_the_whole_schema"""
        for tool, arguments in VALID_BUSINESS_CALLS:
            with self.subTest(tool=tool):
                result = execute_tool(self.registry, self.context, tool, arguments)
                self.assertEqual(result.status, ToolStatus.OK)
                self.assertEqual(result.trace["tool"], tool)

    def test_evidence_carrying_the_identity_is_rejected(self):
        evidence = self.real_order_result().evidence
        leaked = (dataclasses.replace(
            evidence[0], metadata={**evidence[0].metadata, "owner": CUSTOMER_A}
        ),) + evidence[1:]
        message = self.assert_value_error(
            self.with_trace(self.business_trace(), evidence=leaked),
            "get_order", {"order_id": ORDER_A_DELIVERED},
        )
        self.assertNotIn(CUSTOMER_A, message)


class KnowledgeTracePublicationTests(ExecutorTestCase):
    """The knowledge tool has no pinned trace schema, so the value/SQL scan is
    its only line of defence and must hold on its own."""

    QUERY = "签收后几天可以退货-7731"

    def adapter_registry(self, trace):
        class Adapter:
            def search(self, query, *, as_of):
                return ToolResult(
                    tool_name="search_after_sales_policy", status=ToolStatus.EMPTY, trace=trace
                )

        return build_runtime_registry(Adapter())

    def test_leaky_knowledge_traces_are_rejected(self):
        leaky = {
            "query_value": {"echo": self.QUERY},
            "query_in_text": {"note": "searched for " + self.QUERY},
            "identity_value": {"who": CUSTOMER_A},
            "value_as_key": {self.QUERY: 1},
            "sql": {"debug": "SELECT *\nFROM policies"},
        }
        for name, trace in leaky.items():
            with self.subTest(case=name):
                message = self.assert_value_error(
                    self.adapter_registry(trace), "search_after_sales_policy",
                    {"query": self.QUERY},
                )
                self.assertNotIn(self.QUERY, message)
                self.assertNotIn(CUSTOMER_A, message)

    def test_a_clean_knowledge_trace_is_published(self):
        result = execute_tool(
            self.adapter_registry({"matched_rules": 0, "tool": "search_after_sales_policy"}),
            self.context, "search_after_sales_policy", {"query": self.QUERY},
        )
        self.assertEqual(result.status, ToolStatus.EMPTY)
        self.assertEqual(result.trace["matched_rules"], 0)


class PrivacyMatrixTests(ExecutorTestCase):
    def test_privacy_matrix(self):
        """Ports V1 tests.test_executor.BundleAndTraceTests.test_privacy_matrix"""
        result = execute_tool(
            self.registry, self.context, "get_order", {"order_id": ORDER_A_DELIVERED},
            observation_id="obs-7",
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        whole = serialized + repr(result)
        trace_only = json.dumps(dict(result.trace), ensure_ascii=False)
        # Identity VALUE: forbidden everywhere.
        self.assertNotIn(CUSTOMER_A, whole)
        # Business record key: lives in Evidence ...
        self.assertIn(ORDER_A_DELIVERED, serialized)
        # ... but never in the trace.
        self.assertNotIn(ORDER_A_DELIVERED, trace_only)

    def test_a_real_tool_flows_through_the_executor_without_leaking_identity(self):
        """Ports V1 tests.test_executor.SystemBoundaryTests.test_a_real_m4_trace_flows_through_the_executor"""
        for tool, arguments in VALID_BUSINESS_CALLS:
            with self.subTest(tool=tool):
                result = execute_tool(self.registry, self.context, tool, arguments)
                self.assertEqual(result.status, ToolStatus.OK)
                self.assertNotIn(
                    CUSTOMER_A, json.dumps(result.to_dict(), ensure_ascii=False) + repr(result)
                )


class ErrorTaxonomyTests(ExecutorTestCase):
    def test_database_fault_is_a_sanitized_error_not_empty(self):
        self.connection.execute("DROP TABLE inventory")
        result = execute_tool(self.registry, self.context, "get_inventory", {"sku": SKU_STOCKED})
        self.assertEqual(result.status, ToolStatus.ERROR)
        self.assertEqual(result.error_code, ERROR_CODE_TOOL_ERROR)
        self.assertEqual(result.error_message, "get_inventory failed with OperationalError")
        self.assertEqual(result.trace["exception_type"], "OperationalError")
        self.assertNotIn(SKU_STOCKED, repr(result.to_dict()))

    def test_policy_adapter_not_ready_is_its_own_error(self):
        from aftersales.policy import PolicyAdapterNotReady
        self.registry = build_runtime_registry(PolicyAdapterNotReady())
        result = execute_tool(
            self.registry, self.context, "search_after_sales_policy", {"query": "七天无理由"}
        )
        self.assertEqual(result.status, ToolStatus.ERROR)
        self.assertEqual(result.error_code, ERROR_CODE_NOT_READY)
        self.assertEqual(result.evidence, ())
        self.assertNotIn("七天无理由", repr(result.to_dict()))

    def test_reported_error_is_replaced_with_a_fixed_payload(self):
        handler = MagicMock(return_value=ToolResult(
            tool_name="get_order", status=ToolStatus.ERROR,
            error_code="raw-" + ORDER_SENTINEL, error_message="SELECT * FROM orders",
            trace={"sql": "SELECT"},
        ))
        registry = replaced(self.registry, "get_order", handler=handler)
        result = execute_tool(registry, self.context, "get_order", {"order_id": ORDER_SENTINEL})
        self.assertEqual(result.error_code, ERROR_CODE_TOOL_REPORTED)
        self.assertNotIn(ORDER_SENTINEL, repr(result.to_dict()))
        self.assertNotIn("SELECT", repr(result.to_dict()))

    def test_programmer_errors_propagate(self):
        for exc in (ValueError("boom"), TypeError("boom")):
            with self.subTest(exc=type(exc).__name__):
                registry = replaced(self.registry, "get_order", handler=MagicMock(side_effect=exc))
                with self.assertRaises(type(exc)):
                    execute_tool(registry, self.context, "get_order", {"order_id": "x"})

    def test_malformed_results_are_rejected(self):
        cases = {
            "not_a_result": {"status": "ok"},
            "wrong_tool_name": ToolResult(tool_name="get_inventory", status=ToolStatus.EMPTY),
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                registry = replaced(self.registry, "get_order", handler=MagicMock(return_value=value))
                self.assert_value_error(registry, "get_order", {"order_id": "x"})


class ObservationLinkTests(ExecutorTestCase):
    def test_observation_id_links_every_evidence_item_and_the_trace(self):
        result = execute_tool(
            self.registry, self.context, "get_order", {"order_id": ORDER_A_DELIVERED},
            observation_id="span-42",
        )
        self.assertEqual(result.trace["observation_id"], "span-42")
        self.assertTrue(result.evidence)
        for item in result.evidence:
            self.assertEqual(item.metadata[OBSERVATION_ID_KEY], "span-42")

    def test_without_observation_id_the_slot_is_reserved_as_none(self):
        result = execute_tool(self.registry, self.context, "get_inventory", {"sku": SKU_STOCKED})
        self.assertIsNone(result.trace["observation_id"])
        self.assertIsNone(result.evidence[0].metadata[OBSERVATION_ID_KEY])


class ReadOnlyTests(ExecutorTestCase):
    def test_database_is_unchanged_by_every_read(self):
        before = snapshot(self.connection)
        changes = self.connection.total_changes
        for tool, arguments in VALID_BUSINESS_CALLS:
            execute_tool(self.registry, self.context, tool, arguments)
        execute_tool(self.registry, self.context, "search_after_sales_policy", {"query": "x"})
        self.assertEqual(snapshot(self.connection), before)
        self.assertEqual(self.connection.total_changes, changes)

    def test_a_handler_that_writes_is_caught(self):
        def writing_handler(context, arguments):
            context.connection.execute("UPDATE inventory SET available_qty = 99")
            return BUSINESS_HANDLERS["get_inventory"](context, arguments)

        registry = replaced(self.registry, "get_inventory", handler=writing_handler)
        with self.assertRaises(ReadOnlyViolation):
            execute_tool(registry, self.context, "get_inventory", {"sku": SKU_STOCKED})

    def test_a_handler_that_writes_then_raises_is_caught_not_laundered(self):
        # Every exception class the executor would otherwise classify.
        for failure in (
            RuntimeError("after write"),
            sqlite3.OperationalError("after write"),
            ToolNotReady("after write"),
            ValueError("after write"),
            TypeError("after write"),
        ):
            with self.subTest(failure=type(failure).__name__):
                def writing_then_failing(context, arguments, failure=failure):
                    context.connection.execute(
                        "UPDATE inventory SET available_qty = available_qty + 1"
                    )
                    raise failure

                registry = replaced(self.registry, "get_inventory", handler=writing_then_failing)
                outcome = None
                with self.assertRaises(ReadOnlyViolation) as caught:
                    outcome = execute_tool(
                        registry, self.context, "get_inventory", {"sku": SKU_STOCKED}
                    )
                # Never an ERROR result, and the original failure is not lost.
                self.assertIsNone(outcome)
                self.assertIs(caught.exception.__cause__, failure)
                self.assertNotIn(SKU_STOCKED, str(caught.exception))

    def test_failures_without_a_write_keep_their_classification(self):
        cases = (
            (RuntimeError("x"), ERROR_CODE_TOOL_ERROR),
            (sqlite3.OperationalError("x"), ERROR_CODE_TOOL_ERROR),
            (ToolNotReady("x"), ERROR_CODE_NOT_READY),
        )
        for failure, code in cases:
            with self.subTest(failure=type(failure).__name__):
                registry = replaced(
                    self.registry, "get_inventory", handler=MagicMock(side_effect=failure)
                )
                result = execute_tool(registry, self.context, "get_inventory", {"sku": SKU_STOCKED})
                self.assertEqual(result.status, ToolStatus.ERROR)
                self.assertEqual(result.error_code, code)
        for failure in (ValueError("x"), TypeError("x")):
            with self.subTest(failure=type(failure).__name__):
                registry = replaced(
                    self.registry, "get_inventory", handler=MagicMock(side_effect=failure)
                )
                with self.assertRaises(type(failure)):
                    execute_tool(registry, self.context, "get_inventory", {"sku": SKU_STOCKED})

    def test_executor_opens_nothing(self):
        with patch("sqlite3.connect", side_effect=AssertionError("must not open a connection")):
            for tool, arguments in VALID_BUSINESS_CALLS:
                execute_tool(self.registry, self.context, tool, arguments)


if __name__ == "__main__":
    unittest.main()
