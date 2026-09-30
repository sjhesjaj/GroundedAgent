"""Stage 4.4.1: the deterministic fault injection gateway.

Synthetic cases live in this file only. No dataset, no Planner, no LLM, no
runner, no scorer; the sealed holdout is never read.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from aftersales.errors import SideEffectForbidden, ToolTimeout
from aftersales.executor import ReadOnlyViolation
from aftersales.registry import ToolRegistry, build_runtime_registry
from eval_v2 import faults as fg
from eval_v2 import runtime as rt
from eval_v2.faults import (
    DEFAULT_SIMULATED_TIMEOUT_MS,
    FaultCallRecord,
    FaultConfigurationError,
    FaultInjectingGateway,
    InjectedToolError,
)
from eval_v2.runtime import (
    DatabaseChanged,
    EvalRuntimeError,
    FaultGatewayRequired,
    IncompleteLogisticsObservation,
    V2CaseRuntime,
    complete_delivered_at_evidence,
    derive_item_window_from_logistics_result,
    execute_observation,
)
from orchestration.contracts import OBSERVATION_ID_KEY, ToolResult, ToolStatus

from tests.test_v2_eval_runtime import base_case
from tests.v2_support import window_policy

ROOT = Path(__file__).resolve().parent.parent
FAULTS_SOURCE = ROOT / "eval_v2" / "faults.py"

CUSTOMER = "CUST-001"


class RealHandlerCalled(BaseException):
    """Not an Exception: the executor cannot classify it into a ToolResult."""


class SleepCalled(BaseException):
    """Raised by every patched wait primitive."""


def fault(tool, mode, on_call=1, **match):
    return {"tool": tool, "match": dict(match), "mode": mode, "on_call": on_call}


class Harness:
    """One case-run whose real handlers are counted (and optionally trapped).

    From the gateway's point of view, the counted handler IS the real handler:
    it is what the runtime registry holds.
    """

    def __init__(self, faults=(), *, trap=False, archetype=None, registry_changes=None):
        self.calls: Counter = Counter()
        self.trap = trap
        real = build_runtime_registry()

        def counted(spec):
            handler = spec.handler

            def run(context, arguments):
                self.calls[spec.name] += 1
                if self.trap:
                    raise RealHandlerCalled(spec.name)
                return handler(context, arguments)
            return run

        specs = [dataclasses.replace(spec, handler=counted(spec)) for spec in real]
        if registry_changes:
            specs = [dataclasses.replace(spec, **registry_changes.get(spec.name, {}))
                     for spec in specs]
        registry = ToolRegistry(specs)
        case = base_case(faults=list(faults))
        if archetype is not None:
            case["archetype"] = archetype
        with mock.patch.object(rt, "build_runtime_registry", return_value=registry):
            self.runtime = V2CaseRuntime.from_case(case)

    def close(self):
        self.runtime.close()


class GatewayTestCase(unittest.TestCase):
    gateway_class = FaultInjectingGateway

    def harness(self, faults=(), **kwargs) -> Harness:
        harness = Harness(faults, **kwargs)
        self.addCleanup(harness.close)
        return harness

    def gateway(self, faults=(), **kwargs) -> tuple[FaultInjectingGateway, Harness]:
        harness = self.harness(faults, **kwargs)
        return self.gateway_class(harness.runtime), harness

    def assert_error(self, result, code, exception_type, observation_id):
        self.assertIsInstance(result, ToolResult)
        self.assertIs(result.status, ToolStatus.ERROR)
        self.assertEqual(result.error_code, code)
        self.assertEqual(result.evidence, ())
        self.assertEqual(result.trace["exception_type"], exception_type)
        self.assertEqual(result.trace["observation_id"], observation_id)

    def assert_fails_closed(self, gateway, harness, arguments):
        counts, records = gateway.matching_counts, gateway.records
        outcome_ = None
        with self.assertRaises(FaultConfigurationError) as caught:
            outcome_ = gateway.execute("get_order", arguments, observation_id="obs-x")
        self.assertIsNone(outcome_)
        self.assertEqual(harness.calls, Counter())
        self.assertIn("[0], [1]", str(caught.exception))
        for value in arguments.values():
            self.assertNotIn(value, str(caught.exception))
        # Not committed, not recorded, and the gateway stays stopped.
        self.assertEqual(gateway.matching_counts, counts)
        self.assertEqual(gateway.records, records)
        for tool, args in (("get_order", {"order_id": "ORD-1002"}),
                           ("get_inventory", {"sku": "SKU-MUG"})):
            with self.assertRaises(FaultConfigurationError):
                gateway.execute(tool, args, observation_id="obs-y")
        self.assertEqual(harness.calls, Counter())
        harness.runtime.assert_database_unchanged()


# --------------------------------------------------------------------------
# Construction and the mirror registry
# --------------------------------------------------------------------------


class ConstructionTests(GatewayTestCase):
    def test_runtime_registry_is_not_mutated(self):
        harness = self.harness([fault("get_order", "error")])
        registry = harness.runtime.registry
        specs = list(registry)
        handlers = [spec.handler for spec in specs]
        dicts = [spec.to_dict() for spec in specs]
        gateway = FaultInjectingGateway(harness.runtime)
        gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
        gateway.execute("get_inventory", {"sku": "SKU-MUG"}, observation_id="obs-2")
        self.assertIs(harness.runtime.registry, registry)
        self.assertEqual([spec for spec in registry], specs)
        for spec, before, handler, as_dict in zip(registry, specs, handlers, dicts):
            with self.subTest(tool=spec.name):
                self.assertIs(spec, before)
                self.assertIs(spec.handler, handler)
                self.assertEqual(spec.to_dict(), as_dict)

    def test_mirror_registry_differs_only_in_handlers(self):
        harness = self.harness()
        gateway = FaultInjectingGateway(harness.runtime)
        mirror = gateway._registry
        self.assertIsNot(mirror, harness.runtime.registry)
        self.assertEqual(mirror.names(), harness.runtime.registry.names())
        for real, wrapped in zip(harness.runtime.registry, mirror):
            with self.subTest(tool=real.name):
                self.assertIsNot(wrapped.handler, real.handler)
                for field in dataclasses.fields(real):
                    if field.name != "handler":
                        self.assertEqual(getattr(wrapped, field.name), getattr(real, field.name))
                self.assertEqual(wrapped.to_dict(), real.to_dict())

    def test_mirror_check_refuses_a_changed_spec(self):
        real = build_runtime_registry()
        wrapped = ToolRegistry(dataclasses.replace(spec, handler=lambda c, a: None)
                               for spec in real)
        fg._require_mirror(real, wrapped)
        for change in ({"description": "changed"}, {"identity_scoped": False},
                       {"side_effect": True}, {"parameters": ()}):
            with self.subTest(change=sorted(change)):
                bad = ToolRegistry(
                    dataclasses.replace(spec, **change) if spec.name == "get_order" else spec
                    for spec in wrapped)
                with self.assertRaises(EvalRuntimeError):
                    fg._require_mirror(real, bad)
        with self.assertRaises(EvalRuntimeError):
            fg._require_mirror(real, real)

    def test_one_gateway_per_runtime(self):
        harness = self.harness([fault("get_order", "error")])
        FaultInjectingGateway(harness.runtime)
        with self.assertRaises(EvalRuntimeError):
            FaultInjectingGateway(harness.runtime)

    def test_rejects_non_runtime_and_closed_runtime(self):
        with self.assertRaises(ValueError):
            FaultInjectingGateway(object())
        harness = self.harness()
        harness.runtime.close()
        with self.assertRaises(EvalRuntimeError):
            FaultInjectingGateway(harness.runtime)

    def test_closed_runtime_refuses_execution(self):
        gateway, harness = self.gateway()
        harness.runtime.close()
        with self.assertRaises(EvalRuntimeError):
            gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
        self.assertEqual(harness.calls, Counter())

    def test_identical_declarations_are_rejected_before_claiming(self):
        harness = self.harness([fault("get_order", "error", 2, order_id="ORD-1001"),
                                fault("get_order", "timeout", 2, order_id="ORD-1001")])
        with self.assertRaises(FaultConfigurationError) as caught:
            FaultInjectingGateway(harness.runtime)
        self.assertIn("faults[0] and faults[1]", str(caught.exception))
        self.assertNotIn("ORD-1001", str(caught.exception))
        # Nothing was claimed: the runtime can still take its one gateway.
        harness.runtime.claim_tool_gateway()

    def test_declarations_are_rechecked_without_echoing_values(self):
        registry = build_runtime_registry()
        secret = "ORD-SECRET-4417"
        good = fault("get_order", "error", 1, order_id=secret)
        bad = {
            "extra_key": {**good, "extra": 1},
            "missing_key": {key: good[key] for key in ("tool", "match", "mode")},
            "unknown_tool": {**good, "tool": "create_return"},
            "match_not_argument": {**good, "match": {"sku": secret}},
            "match_identity": {**good, "match": {"customer_id": secret}},
            "match_blank": {**good, "match": {"order_id": "  "}},
            "match_non_string": {**good, "match": {"order_id": 1001}},
            "match_not_object": {**good, "match": [secret]},
            "mode": {**good, "mode": "slow"},
            "on_call_zero": {**good, "on_call": 0},
            "on_call_bool": {**good, "on_call": True},
            "on_call_float": {**good, "on_call": 1.0},
            "not_object": [good],
        }
        self.assertEqual(len(fg._parse_faults((good,), registry)), 1)
        for name, declaration in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(FaultConfigurationError) as caught:
                    fg._parse_faults((declaration,), registry)
                self.assertNotIn(secret, str(caught.exception))

    def test_observation_id_is_required(self):
        gateway, harness = self.gateway([fault("get_order", "error")])
        for bad in (None, "", "   ", 7):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id=bad)
        with self.assertRaises(TypeError):
            gateway.execute("get_order", {"order_id": "ORD-1001"})
        self.assertEqual(gateway.matching_counts, (0,))
        self.assertEqual(gateway.records, ())

    def test_wrapped_handler_refuses_calls_from_outside_the_gateway(self):
        gateway, harness = self.gateway([fault("get_order", "error")])
        handler = gateway._registry.get("get_order").handler
        with self.assertRaises(FaultConfigurationError):
            handler(harness.runtime.context, {"order_id": "ORD-1001"})
        self.assertEqual(gateway.matching_counts, (0,))
        self.assertEqual(harness.calls, Counter())

    def test_direct_helper_still_refuses_faulted_cases(self):
        harness = self.harness([fault("get_order", "error")])
        FaultInjectingGateway(harness.runtime)
        with self.assertRaises(FaultGatewayRequired) as caught:
            execute_observation(harness.runtime, "get_order", {"order_id": "ORD-1001"},
                                observation_id="obs-1")
        self.assertEqual(str(caught.exception),
                         "faulted case must execute through FaultInjectingGateway")
        self.assertEqual(harness.calls, Counter())


# --------------------------------------------------------------------------
# No faults: the gateway is a transparent execution path
# --------------------------------------------------------------------------


NO_FAULT_CALLS = (
    ("get_order", {"order_id": "ORD-1001"}),
    ("get_order", {"order_id": "ORD-2001"}),  # another customer's: identity-scoped
    ("get_logistics", {"order_id": "ORD-1004"}),
    ("get_logistics", {"order_id": "ORD-1003"}),
    ("get_inventory", {"sku": "SKU-MUG"}),
    ("get_after_sales_case", {"order_id": "ORD-1001"}),
    ("search_after_sales_policy", {"query": "签收后几天内可以无理由退货"}),
)


class NoFaultTests(GatewayTestCase):
    def test_gateway_equals_execute_observation(self):
        gateway, harness = self.gateway()
        direct = self.harness()
        for number, (tool, arguments) in enumerate(NO_FAULT_CALLS, start=1):
            with self.subTest(tool=tool, number=number):
                observation_id = "obs-" + str(number)
                via_gateway = gateway.execute(tool, arguments, observation_id=observation_id)
                via_direct = execute_observation(direct.runtime, tool, arguments,
                                                 observation_id=observation_id)
                self.assertEqual(via_gateway.to_dict(), via_direct.to_dict())
                self.assertEqual(via_gateway, via_direct)
        self.assertEqual(harness.calls, direct.calls)
        self.assertEqual([record.outcome for record in gateway.records],
                         ["delegated"] * len(NO_FAULT_CALLS))
        self.assertEqual(gateway.matching_counts, ())
        harness.runtime.assert_database_unchanged()

    def test_execute_returns_the_executor_result_unchanged(self):
        gateway, harness = self.gateway([fault("get_order", "error")])
        sentinel = object()
        with mock.patch.object(fg, "execute_tool", return_value=sentinel) as patched:
            returned = gateway.execute("get_order", {"order_id": "ORD-1001"},
                                       observation_id="obs-1")
        self.assertIs(returned, sentinel)
        (call,) = patched.call_args_list
        self.assertIs(call.args[0], gateway._registry)
        self.assertIsNot(call.args[0], harness.runtime.registry)
        self.assertIs(call.args[1], harness.runtime.context)
        self.assertEqual(call.kwargs, {"observation_id": "obs-1"})


# --------------------------------------------------------------------------
# The three fault modes
# --------------------------------------------------------------------------


class FaultModeTests(GatewayTestCase):
    def test_error_on_first_matching_call(self):
        gateway, harness = self.gateway([fault("get_order", "error", order_id="ORD-1001")])
        result = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-7")
        self.assert_error(result, "tool_error", "InjectedToolError", "obs-7")
        self.assertEqual(result.error_message, "get_order failed with InjectedToolError")
        self.assertEqual(harness.calls["get_order"], 0)
        (record,) = gateway.records
        self.assertEqual(record.outcome, "injected_error")
        self.assertEqual((record.fault_index, record.fault_mode, record.matching_call_number),
                         (0, "error", 1))
        self.assertIsNone(record.simulated_latency_ms)
        self.assertFalse(issubclass(InjectedToolError, ToolTimeout))

    def test_timeout_on_first_matching_call(self):
        gateway, harness = self.gateway([fault("get_order", "timeout", order_id="ORD-1001")])
        result = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-8")
        self.assert_error(result, "tool_timeout", "ToolTimeout", "obs-8")
        self.assertEqual(result.error_message, "get_order timed out")
        self.assertEqual(harness.calls["get_order"], 0)
        (record,) = gateway.records
        self.assertEqual(record.outcome, "injected_timeout")
        self.assertEqual(DEFAULT_SIMULATED_TIMEOUT_MS, 30_000)
        self.assertEqual(record.simulated_latency_ms, DEFAULT_SIMULATED_TIMEOUT_MS)

    def test_malformed_raises_through_the_executor_contract(self):
        gateway, harness = self.gateway([fault("get_order", "malformed", order_id="ORD-1001")])
        outcome = None
        with self.assertRaises(ValueError) as caught:
            outcome = gateway.execute("get_order", {"order_id": "ORD-1001"},
                                      observation_id="obs-9")
        self.assertIsNone(outcome)
        # The executor's own result check, not the gateway, raised it.
        self.assertEqual(str(caught.exception),
                         "get_order result is _InjectedMalformedResult, expected a ToolResult")
        self.assertNotIsInstance(caught.exception, FaultConfigurationError)
        self.assertEqual(harness.calls["get_order"], 0)
        (record,) = gateway.records
        self.assertEqual(record.outcome, "injected_malformed")
        self.assertIsNone(record.simulated_latency_ms)

    def test_real_handler_is_never_called_on_injection(self):
        # The trap raises a BaseException: had any mode run the real handler
        # (even to edit its result afterwards), this would escape the executor.
        for mode in ("error", "timeout", "malformed"):
            with self.subTest(mode=mode):
                gateway, harness = self.gateway(
                    [fault("get_logistics", mode, order_id="ORD-1004")], trap=True)
                if mode == "malformed":
                    with self.assertRaises(ValueError):
                        gateway.execute("get_logistics", {"order_id": "ORD-1004"},
                                        observation_id="obs-1")
                else:
                    result = gateway.execute("get_logistics", {"order_id": "ORD-1004"},
                                             observation_id="obs-1")
                    self.assertIs(result.status, ToolStatus.ERROR)
                self.assertEqual(harness.calls, Counter())
                # The trap is live: the next, unfaulted call does reach it.
                with self.assertRaises(RealHandlerCalled):
                    gateway.execute("get_logistics", {"order_id": "ORD-1004"},
                                    observation_id="obs-2")
                self.assertEqual(harness.calls["get_logistics"], 1)

    def test_timeout_never_waits(self):
        gateway, harness = self.gateway([fault("get_inventory", "timeout", sku="SKU-MUG")])
        blocked = SleepCalled("a wait primitive was called")
        with mock.patch.object(time, "sleep", side_effect=blocked), \
                mock.patch.object(asyncio, "sleep", side_effect=blocked), \
                mock.patch.object(threading.Event, "wait", side_effect=blocked), \
                mock.patch.object(threading.Condition, "wait", side_effect=blocked):
            result = gateway.execute("get_inventory", {"sku": "SKU-MUG"}, observation_id="obs-1")
        self.assertEqual(result.error_code, "tool_timeout")

    def test_faults_module_has_no_wait_or_clock_machinery(self):
        tree = ast.parse(FAULTS_SOURCE.read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        for module in ("time", "asyncio", "threading", "random", "uuid", "datetime",
                       "concurrent", "signal"):
            self.assertNotIn(module, imported)
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for name in ("sleep", "wait", "now", "monotonic", "perf_counter"):
            self.assertNotIn(name, names)

    def test_records_carry_no_values(self):
        faults = [fault("get_order", "error", 2, order_id="ORD-1001"),
                  fault("get_inventory", "timeout", 1, sku="SKU-MUG"),
                  fault("search_after_sales_policy", "malformed", 1, query="七天无理由退货")]
        gateway, harness = self.gateway(faults)
        gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
        gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-2")
        gateway.execute("get_inventory", {"sku": "SKU-MUG"}, observation_id="obs-3")
        with self.assertRaises(ValueError):
            gateway.execute("search_after_sales_policy", {"query": "七天无理由退货"},
                            observation_id="obs-4")
        gateway.execute("get_logistics", {"order_id": "ORD-1001"}, observation_id="obs-5")
        self.assertEqual(len(gateway.records), 5)
        rendered = json.dumps([record.to_dict() for record in gateway.records],
                              ensure_ascii=False) + repr(gateway.records)
        for leaked in ("ORD-1001", "SKU-MUG", "七天无理由", CUSTOMER, "demo-a",
                       "SELECT", "select", " from ", "order_id", "sku", "query"):
            self.assertNotIn(leaked, rendered)
        for record in gateway.records:
            self.assertEqual(set(record.to_dict()), {
                "sequence", "tool_name", "observation_id", "outcome", "fault_index",
                "fault_mode", "matching_call_number", "simulated_latency_ms",
                "matched_faults"})

    def test_records_are_an_immutable_snapshot(self):
        gateway, harness = self.gateway([fault("get_order", "error")])
        gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
        records = gateway.records
        self.assertIsInstance(records, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            records[0].outcome = "delegated"
        gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-2")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(gateway.records), 2)
        self.assertIsInstance(gateway.matching_counts, tuple)


# --------------------------------------------------------------------------
# on_call semantics
# --------------------------------------------------------------------------


def outcome(gateway, tool, arguments, observation_id):
    """'ok' / 'empty' / an error code / 'malformed' for one gateway call."""
    try:
        result = gateway.execute(tool, arguments, observation_id=observation_id)
    except ValueError as exc:
        if isinstance(exc, FaultConfigurationError):
            raise
        return "malformed"
    if result.status is ToolStatus.ERROR:
        return result.error_code
    return result.status.value


class OnCallTests(GatewayTestCase):
    def test_nth_matching_call_fires_exactly_once(self):
        gateway, harness = self.gateway([fault("get_inventory", "error", 2, sku="SKU-MUG")])
        calls = [("get_inventory", {"sku": "SKU-MUG"}),
                 ("get_order", {"order_id": "ORD-1001"}),
                 ("get_inventory", {"sku": "SKU-KETTLE"}),
                 ("get_inventory", {"sku": "SKU-MUG"}),
                 ("get_inventory", {"sku": "SKU-MUG"})]
        seen = [outcome(gateway, tool, arguments, "obs-" + str(n))
                for n, (tool, arguments) in enumerate(calls, start=1)]
        self.assertEqual(seen, ["ok", "ok", "ok", "tool_error", "ok"])
        self.assertEqual([r.outcome for r in gateway.records],
                         ["delegated", "delegated", "delegated", "injected_error", "delegated"])
        self.assertEqual([r.sequence for r in gateway.records], [1, 2, 3, 4, 5])
        self.assertEqual([r.observation_id for r in gateway.records],
                         ["obs-1", "obs-2", "obs-3", "obs-4", "obs-5"])
        self.assertEqual([r.matched_faults for r in gateway.records],
                         [((0, 1),), (), (), ((0, 2),), ((0, 3),)])
        self.assertEqual(gateway.records[3].matching_call_number, 2)
        self.assertEqual(gateway.matching_counts, (3,))
        self.assertEqual(harness.calls["get_inventory"], 3)
        self.assertEqual(sum(r.outcome != "delegated" for r in gateway.records), 1)

    def test_empty_match_counts_every_legal_call_of_the_tool(self):
        gateway, harness = self.gateway([fault("get_order", "timeout", 3)])
        calls = [("get_order", {"order_id": "ORD-1001"}),
                 ("get_logistics", {"order_id": "ORD-1001"}),
                 ("get_order", {"order_id": "ORD-2001"}),
                 ("get_order", {"order_id": "ORD-NOPE"}),
                 ("get_order", {"order_id": "ORD-1001"})]
        seen = [outcome(gateway, tool, arguments, "obs-" + str(n))
                for n, (tool, arguments) in enumerate(calls, start=1)]
        self.assertEqual(seen, ["ok", "ok", "empty", "tool_timeout", "ok"])
        self.assertEqual(gateway.matching_counts, (4,))

    def test_multi_key_match_needs_every_key(self):
        gateway, harness = self.gateway(
            [fault("get_order", "error", 1, order_id="ORD-1001")])
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-10011"}, "obs-1"),
                         "empty")
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ord-1001"}, "obs-2"),
                         "empty")
        self.assertEqual(gateway.matching_counts, (0,))
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "obs-3"),
                         "tool_error")

    def test_on_call_beyond_the_run_never_fires(self):
        gateway, harness = self.gateway([fault("get_order", "error", 5)])
        seen = [outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "obs-" + str(n))
                for n in range(1, 5)]
        self.assertEqual(seen, ["ok"] * 4)
        self.assertEqual(harness.calls["get_order"], 4)


# --------------------------------------------------------------------------
# Invalid calls never count
# --------------------------------------------------------------------------


INVALID_GET_ORDER_ARGUMENTS = (
    {},
    {"order": "ORD-1001"},
    {"order_id": "ORD-1001", "extra": "x"},
    {"order_id": "ORD-1001", "customer_id": CUSTOMER},
    {"order_id": 1001},
    {"order_id": "   "},
    {"order_id": "O" * 129},
    ["order_id"],
)


class InvalidArgumentTests(GatewayTestCase):
    def test_invalid_arguments_do_not_consume_the_fault(self):
        gateway, harness = self.gateway([fault("get_order", "error", 1)])
        for arguments in INVALID_GET_ORDER_ARGUMENTS:
            with self.subTest(arguments=repr(arguments)[:40]):
                with self.assertRaises(ValueError):
                    gateway.execute("get_order", arguments, observation_id="obs-bad")
        for tool in ("create_return", "get_orders"):
            with self.assertRaises(ValueError):
                gateway.execute(tool, {"order_id": "ORD-1001"}, observation_id="obs-bad")
        self.assertEqual(gateway.matching_counts, (0,))
        self.assertEqual(gateway.records, ())
        self.assertEqual(harness.calls, Counter())
        result = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
        self.assertEqual(result.error_code, "tool_error")
        self.assertEqual(gateway.records[0].sequence, 1)

    def test_sequence_counts_only_calls_that_reach_the_handler(self):
        gateway, harness = self.gateway()
        gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
        with self.assertRaises(ValueError):
            gateway.execute("get_order", {"order_id": "ORD-1001", "x": "y"},
                            observation_id="obs-2")
        gateway.execute("get_inventory", {"sku": "SKU-MUG"}, observation_id="obs-3")
        self.assertEqual([(r.sequence, r.observation_id) for r in gateway.records],
                         [(1, "obs-1"), (2, "obs-3")])


# --------------------------------------------------------------------------
# Independent counters and overlap
# --------------------------------------------------------------------------


class IndependentCounterTests(GatewayTestCase):
    def test_same_tool_different_match(self):
        gateway, harness = self.gateway([fault("get_inventory", "error", 2, sku="SKU-MUG"),
                                         fault("get_inventory", "timeout", 1, sku="SKU-KETTLE")])
        steps = [("SKU-MUG", "ok", (1, 0)),
                 ("SKU-KETTLE", "tool_timeout", (1, 1)),
                 ("SKU-KETTLE", "ok", (1, 2)),
                 ("SKU-EARBUDS", "ok", (1, 2)),
                 ("SKU-MUG", "tool_error", (2, 2)),
                 ("SKU-MUG", "ok", (3, 2))]
        for number, (sku, expected, counts) in enumerate(steps, start=1):
            with self.subTest(step=number):
                self.assertEqual(outcome(gateway, "get_inventory", {"sku": sku},
                                         "obs-" + str(number)), expected)
                self.assertEqual(gateway.matching_counts, counts)
        fired = [(r.fault_index, r.matching_call_number) for r in gateway.records
                 if r.outcome != "delegated"]
        self.assertEqual(fired, [(1, 1), (0, 2)])

    def test_same_arguments_different_tools(self):
        gateway, harness = self.gateway([fault("get_order", "error", 2, order_id="ORD-1004"),
                                         fault("get_logistics", "timeout", 1, order_id="ORD-1004")])
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1004"}, "o1"), "ok")
        self.assertEqual(outcome(gateway, "get_logistics", {"order_id": "ORD-1004"}, "o2"),
                         "tool_timeout")
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1004"}, "o3"),
                         "tool_error")
        self.assertEqual(outcome(gateway, "get_logistics", {"order_id": "ORD-1004"}, "o4"), "ok")

    def test_a_fired_fault_still_counts_for_the_others(self):
        # Call 1 matches both; only fault 1 is due. Fault 0 still counts it.
        gateway, harness = self.gateway([fault("get_order", "timeout", 2),
                                         fault("get_order", "error", 1, order_id="ORD-1001")])
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "o1"),
                         "tool_error")
        self.assertEqual(gateway.matching_counts, (1, 1))
        self.assertEqual(gateway.records[0].matched_faults, ((0, 1), (1, 1)))
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1002"}, "o2"),
                         "tool_timeout")


class OverlapTests(GatewayTestCase):
    def test_two_faults_due_on_the_first_call(self):
        gateway, harness = self.gateway([fault("get_order", "error", 1),
                                         fault("get_order", "timeout", 1, order_id="ORD-1001")],
                                        trap=True)
        self.assert_fails_closed(gateway, harness, {"order_id": "ORD-1001"})

    def test_overlap_that_only_emerges_at_run_time(self):
        gateway, harness = self.gateway([fault("get_order", "error", 2),
                                         fault("get_order", "malformed", 1, order_id="ORD-1001")])
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1002"}, "o1"), "ok")
        harness.calls.clear()
        harness.trap = True
        self.assert_fails_closed(gateway, harness, {"order_id": "ORD-1001"})

    def test_overlapping_matches_that_are_never_due_together_are_fine(self):
        gateway, harness = self.gateway([fault("get_order", "error", 1),
                                         fault("get_order", "timeout", 2, order_id="ORD-1001")])
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "o1"),
                         "tool_error")
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "o2"),
                         "tool_timeout")


# --------------------------------------------------------------------------
# Per-case state
# --------------------------------------------------------------------------


class PerCaseStateTests(GatewayTestCase):
    def test_each_case_run_starts_from_zero(self):
        faults = [fault("get_order", "error", 1)]
        for run in range(3):
            with self.subTest(run=run):
                gateway, harness = self.gateway(faults)
                self.assertEqual(gateway.matching_counts, (0,))
                self.assertEqual(gateway.records, ())
                self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "o1"),
                                 "tool_error")
                self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "o2"),
                                 "ok")
                self.assertEqual(gateway.records[0].sequence, 1)

    def test_interleaved_case_runs_do_not_share_state(self):
        a, _ = self.gateway([fault("get_order", "error", 2)])
        b, _ = self.gateway([fault("get_order", "error", 2)])
        self.assertEqual(outcome(a, "get_order", {"order_id": "ORD-1001"}, "a1"), "ok")
        self.assertEqual(outcome(b, "get_order", {"order_id": "ORD-1001"}, "b1"), "ok")
        self.assertEqual(outcome(a, "get_order", {"order_id": "ORD-1001"}, "a2"), "tool_error")
        self.assertEqual(outcome(b, "get_order", {"order_id": "ORD-1001"}, "b2"), "tool_error")
        self.assertEqual((a.matching_counts, b.matching_counts), ((2,), (2,)))

    def test_no_module_level_mutable_state(self):
        tree = ast.parse(FAULTS_SOURCE.read_text(encoding="utf-8"))
        self.assertFalse([node for node in ast.walk(tree)
                          if isinstance(node, (ast.Global, ast.Nonlocal))])
        mutable = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                with self.subTest(line=node.lineno):
                    self.assertNotIsInstance(node.value, mutable)
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
                        self.assertNotIsInstance(item.value, mutable)


# --------------------------------------------------------------------------
# A14 / A15 style smoke, logistics and the database
# --------------------------------------------------------------------------


class ArchetypeSmokeTests(GatewayTestCase):
    def test_a14_style_tool_error(self):
        for tool in ("get_order", "get_logistics"):
            with self.subTest(tool=tool):
                gateway, harness = self.gateway([fault(tool, "error", 1, order_id="ORD-1001")],
                                                archetype="A14")
                result = gateway.execute(tool, {"order_id": "ORD-1001"}, observation_id="obs-1")
                self.assert_error(result, "tool_error", "InjectedToolError", "obs-1")
                harness.runtime.assert_database_unchanged()

    def test_a15_style_tool_timeout(self):
        for tool in ("get_order", "get_logistics"):
            with self.subTest(tool=tool):
                gateway, harness = self.gateway([fault(tool, "timeout", 1, order_id="ORD-1001")],
                                                archetype="A15")
                result = gateway.execute(tool, {"order_id": "ORD-1001"}, observation_id="obs-1")
                self.assert_error(result, "tool_timeout", "ToolTimeout", "obs-1")
                self.assertEqual(gateway.records[0].simulated_latency_ms, 30_000)
                harness.runtime.assert_database_unchanged()


class LogisticsFaultTests(GatewayTestCase):
    def category(self, gateway):
        result = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-order")
        (item,) = [e for e in result.evidence if e.metadata["entity"] == "order_item"
                   and e.metadata["record_id"] == "OI-1001-1"
                   and e.metadata["field"] == "category"]
        return item

    def test_error_and_timeout_are_not_observations(self):
        for mode, code in (("error", "tool_error"), ("timeout", "tool_timeout")):
            with self.subTest(mode=mode):
                gateway, harness = self.gateway(
                    [fault("get_logistics", mode, 1, order_id="ORD-1001")])
                category = self.category(gateway)
                result = gateway.execute("get_logistics", {"order_id": "ORD-1001"},
                                         observation_id="obs-logistics")
                self.assertEqual(result.error_code, code)
                with self.assertRaises(IncompleteLogisticsObservation) as caught:
                    complete_delivered_at_evidence(result)
                self.assertIn("an ERROR is not an observation", str(caught.exception))
                with self.assertRaises(IncompleteLogisticsObservation):
                    derive_item_window_from_logistics_result(
                        result, window_policy(), clock=harness.runtime.clock,
                        category=category)
                # The next call is delegated and is a real, complete observation.
                again = gateway.execute("get_logistics", {"order_id": "ORD-1001"},
                                        observation_id="obs-logistics-2")
                self.assertEqual(len(complete_delivered_at_evidence(again)), 1)
                harness.runtime.assert_database_unchanged()

    def test_malformed_yields_no_result_at_all(self):
        gateway, harness = self.gateway([fault("get_logistics", "malformed", 1)])
        category = self.category(gateway)
        derive = mock.MagicMock(side_effect=AssertionError("derived from a fault"))
        with mock.patch.object(rt, "derive_item_window_eligibility", derive):
            with self.assertRaises(ValueError):
                derive_item_window_from_logistics_result(
                    gateway.execute("get_logistics", {"order_id": "ORD-1004"},
                                    observation_id="obs-logistics"),
                    window_policy(), clock=harness.runtime.clock, category=category)
        derive.assert_not_called()
        self.assertEqual(harness.calls["get_logistics"], 0)
        harness.runtime.assert_database_unchanged()


class DatabaseInvariantTests(GatewayTestCase):
    def test_every_path_leaves_the_database_unchanged(self):
        faults = [fault("get_order", "error", 1), fault("get_logistics", "timeout", 1),
                  fault("get_inventory", "malformed", 1)]
        gateway, harness = self.gateway(faults)
        runtime = harness.runtime
        changes = runtime.connection.total_changes

        def check():
            runtime.assert_database_unchanged()
            self.assertEqual(runtime.connection.total_changes, changes)

        self.assertEqual(outcome(gateway, "get_after_sales_case", {"order_id": "ORD-1001"},
                                 "o1"), "ok")
        check()
        self.assertEqual(outcome(gateway, "get_order", {"order_id": "ORD-1001"}, "o2"),
                         "tool_error")
        check()
        self.assertEqual(outcome(gateway, "get_logistics", {"order_id": "ORD-1001"}, "o3"),
                         "tool_timeout")
        check()
        self.assertEqual(outcome(gateway, "get_inventory", {"sku": "SKU-MUG"}, "o4"),
                         "malformed")
        check()
        self.assertEqual([r.outcome for r in gateway.records],
                         ["delegated", "injected_error", "injected_timeout",
                          "injected_malformed"])


# --------------------------------------------------------------------------
# The existing executor keeps every guard on the delegated path
# --------------------------------------------------------------------------


class ExecutorGuardTests(GatewayTestCase):
    def test_closed_arguments_and_identity_smuggling(self):
        gateway, harness = self.gateway()
        for arguments in INVALID_GET_ORDER_ARGUMENTS:
            with self.subTest(arguments=repr(arguments)[:40]):
                with self.assertRaises(ValueError):
                    gateway.execute("get_order", arguments, observation_id="obs-1")
        self.assertEqual(harness.calls, Counter())

    def test_identity_scope_comes_from_the_trusted_context(self):
        gateway, harness = self.gateway()
        other = gateway.execute("get_order", {"order_id": "ORD-2001"}, observation_id="obs-1")
        self.assertIs(other.status, ToolStatus.EMPTY)
        own = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-2")
        self.assertIs(own.status, ToolStatus.OK)
        self.assertNotIn(CUSTOMER, json.dumps(own.to_dict(), ensure_ascii=False))
        for item in own.evidence:
            self.assertEqual(item.metadata[OBSERVATION_ID_KEY], "obs-2")

    def test_side_effect_refused_before_the_boundary(self):
        with mock.patch.object(rt, "check_runtime_registry"):
            gateway, harness = self.gateway(
                [fault("get_inventory", "error", 1)],
                registry_changes={"get_inventory": {"side_effect": True}})
        self.assertIs(gateway._registry.get("get_inventory").side_effect, True)
        with self.assertRaises(SideEffectForbidden):
            gateway.execute("get_inventory", {"sku": "SKU-MUG"}, observation_id="obs-1")
        self.assertEqual(gateway.matching_counts, (0,))
        self.assertEqual(gateway.records, ())
        self.assertEqual(harness.calls, Counter())

    def test_read_only_guard_still_runs(self):
        gateway, harness = self.gateway()
        real = gateway._registry.get("get_inventory").handler

        def writing(context, arguments):
            context.connection.execute("PRAGMA query_only = OFF")
            context.connection.execute("UPDATE inventory SET available_qty = 99")
            return real(context, arguments)

        mirror = ToolRegistry(dataclasses.replace(spec, handler=writing)
                              if spec.name == "get_inventory" else spec
                              for spec in gateway._registry)
        gateway._registry = mirror
        with self.assertRaises(ReadOnlyViolation):
            gateway.execute("get_inventory", {"sku": "SKU-MUG"}, observation_id="obs-1")
        with self.assertRaises(DatabaseChanged):
            harness.runtime.assert_database_unchanged()

    def test_result_contract_and_trace_sanitation(self):
        gateway, harness = self.gateway()
        good = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-0")
        leaky_trace = {**{k: v for k, v in good.trace.items() if k != "observation_id"},
                       "tool": "get_order", "note": "ORD-1001"}
        returns = {
            "not_a_result": ({"status": "ok"}, "expected a ToolResult"),
            "wrong_tool": (ToolResult(tool_name="get_inventory", status=ToolStatus.EMPTY),
                           "tool_name does not match"),
            "value_in_trace": (ToolResult(tool_name="get_order", status=ToolStatus.OK,
                                          evidence=good.evidence, trace=leaky_trace),
                               "argument value"),
            "sql_in_trace": (ToolResult(tool_name="get_order", status=ToolStatus.EMPTY,
                                        trace={"q": "SELECT x FROM orders"}), "SQL"),
        }
        spec = harness.runtime.registry.get("get_order")
        for name, (value, reason) in returns.items():
            with self.subTest(case=name):
                # Returned from behind the gateway boundary, as a real handler's would be.
                fake = dataclasses.replace(spec, handler=lambda c, a, v=value: v)
                gateway._registry = ToolRegistry(
                    dataclasses.replace(s, handler=gateway._wrap(fake))
                    if s.name == "get_order" else s for s in gateway._registry)
                with self.assertRaises(ValueError) as caught:
                    gateway.execute("get_order", {"order_id": "ORD-1001"},
                                    observation_id="obs-1")
                self.assertIn(reason, str(caught.exception))
                self.assertNotIn("ORD-1001", str(caught.exception))
        self.assertEqual([r.outcome for r in gateway.records], ["delegated"] * 5)
        harness.runtime.assert_database_unchanged()


# --------------------------------------------------------------------------
# Mutation guards: plausible wrong gateways the scenarios above must catch
# --------------------------------------------------------------------------


class _FiresEveryMatchingCall(FaultInjectingGateway):
    @staticmethod
    def _is_due(fault, matching_call_number):
        return matching_call_number >= fault.on_call


class _CountsNonMatchingCalls(FaultInjectingGateway):
    def _matching_faults(self, tool_name, arguments):
        return [f for f in self._faults if f.tool == tool_name]


class _CountsBeforeValidation(FaultInjectingGateway):
    """Counts in execute(), on the raw arguments, before the executor checks them."""

    def execute(self, tool_name, arguments, *, observation_id):
        # Matching is right for valid calls; the bug is only *where* it counts.
        self._pending = [f for f in self._faults if f.tool == tool_name and (
            not f.match or (isinstance(arguments, dict) and f.matches(tool_name, arguments)))]
        for f in self._pending:
            self._counts[f.index] += 1
        return super().execute(tool_name, arguments, observation_id=observation_id)

    def _at_boundary(self, tool_name, arguments):
        due = [f for f in self._pending if self._counts[f.index] == f.on_call]
        self._records.append(FaultCallRecord(
            sequence=len(self._records) + 1, tool_name=tool_name,
            observation_id=self._observation_id, outcome="delegated", fault_index=None,
            fault_mode=None, matching_call_number=None, simulated_latency_ms=None,
            matched_faults=()))
        return due[0] if due else None


class _TimeoutAsGenericError(FaultInjectingGateway):
    def _inject(self, fault, tool_name):
        if fault.mode == "timeout":
            raise RuntimeError(tool_name + " timed out")
        return super()._inject(fault, tool_name)


class _TimeoutSleeps(FaultInjectingGateway):
    def _inject(self, fault, tool_name):
        if fault.mode == "timeout":
            time.sleep(0)
        return super()._inject(fault, tool_name)


class _RunsRealHandlerFirst(FaultInjectingGateway):
    def _wrap(self, spec):
        real, name = spec.handler, spec.name

        def handler(context, arguments):
            fault = self._at_boundary(name, arguments)
            result = real(context, arguments)
            return result if fault is None else self._inject(fault, name)
        return handler


class _PostHocMutation(FaultInjectingGateway):
    """Runs the real tool, then rewrites its result into the fault."""

    def execute(self, tool_name, arguments, *, observation_id):
        self._observation_id = observation_id
        try:
            result = fg.execute_tool(self._runtime.registry, self._runtime.context, tool_name,
                                     arguments, observation_id=observation_id)
            fault = self._at_boundary(tool_name, dict(arguments))
        finally:
            self._observation_id = None
        if fault is None:
            return result
        return ToolResult(tool_name=tool_name, status=ToolStatus.ERROR, error_code=(
            "tool_timeout" if fault.mode == "timeout" else "tool_error"),
            error_message=tool_name + " failed", trace={
                "tool": tool_name, "observation_id": observation_id,
                "exception_type": "ToolTimeout" if fault.mode == "timeout"
                else "InjectedToolError"})


class _PicksTheFirstDueFault(FaultInjectingGateway):
    def _select(self, due):
        return due[0] if due else None


class _SharedCounters(FaultInjectingGateway):
    SHARED: list = []  # the deliberate bug: one counter list for every case-run

    def __init__(self, runtime):
        super().__init__(runtime)
        shared = _SharedCounters.SHARED
        shared.extend([0] * (len(self._counts) - len(shared)))
        self._counts = shared


class _Probe(GatewayTestCase):
    """Detached from any test result, so a failing check inside a subTest
    propagates instead of being recorded as a sub-failure."""

    def runTest(self):
        pass


SCENARIOS = (
    OnCallTests.test_nth_matching_call_fires_exactly_once,
    InvalidArgumentTests.test_invalid_arguments_do_not_consume_the_fault,
    FaultModeTests.test_timeout_on_first_matching_call,
    FaultModeTests.test_timeout_never_waits,
    FaultModeTests.test_real_handler_is_never_called_on_injection,
    OverlapTests.test_two_faults_due_on_the_first_call,
    PerCaseStateTests.test_each_case_run_starts_from_zero,
)

# Each wrong implementation, and the one scenario that must catch it on its own.
MUTANTS = (
    (_FiresEveryMatchingCall, OnCallTests.test_nth_matching_call_fires_exactly_once),
    (_CountsNonMatchingCalls, OnCallTests.test_nth_matching_call_fires_exactly_once),
    (_CountsBeforeValidation,
     InvalidArgumentTests.test_invalid_arguments_do_not_consume_the_fault),
    (_TimeoutAsGenericError, FaultModeTests.test_timeout_on_first_matching_call),
    (_TimeoutSleeps, FaultModeTests.test_timeout_never_waits),
    (_RunsRealHandlerFirst, FaultModeTests.test_real_handler_is_never_called_on_injection),
    (_PostHocMutation, FaultModeTests.test_real_handler_is_never_called_on_injection),
    (_PicksTheFirstDueFault, OverlapTests.test_two_faults_due_on_the_first_call),
    (_SharedCounters, PerCaseStateTests.test_each_case_run_starts_from_zero),
)


def run_scenario(gateway_class, scenario):
    """Run one acceptance scenario against `gateway_class`. Raises on deviation."""
    _SharedCounters.SHARED.clear()
    probe = _Probe()
    probe.gateway_class = gateway_class
    try:
        scenario(probe)
    finally:
        probe.doCleanups()


class MutationGuardTests(unittest.TestCase):
    def test_the_real_gateway_passes_every_scenario(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario.__name__):
                run_scenario(FaultInjectingGateway, scenario)

    def test_every_mutant_is_caught_by_its_scenario(self):
        for mutant, scenario in MUTANTS:
            with self.subTest(mutant=mutant.__name__):
                with self.assertRaises((AssertionError, RealHandlerCalled, SleepCalled)):
                    run_scenario(mutant, scenario)


if __name__ == "__main__":
    unittest.main()
