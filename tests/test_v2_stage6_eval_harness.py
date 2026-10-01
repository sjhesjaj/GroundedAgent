"""Stage 6.4A: the case-run harness, operator events, restart and fault injection (design §19.2)."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from aftersales.action_db import create_stage6_database
from aftersales.action_gateway import ActionGateway
from aftersales.guard_state import GUARD_READS, READ_MALFORMED, GuardStateReader
from eval_v2 import faults as faults_module
from eval_v2.action_control import ActionControlState
from eval_v2.faults import FaultInjectingGateway
from eval_v2.runtime import EvalCaseInvalid, V2CaseRuntime
from eval_v2.stage6_runner import run_stage6_case, run_stage6_reference
from eval_v2.stage6_runtime import (
    ActionFaultConfigurationError,
    ActionFaultInjector,
    Stage6CaseRuntime,
    Stage6ReadRuntime,
)

from tests import stage6_eval_support as support
from tests.stage6_support import (
    EXCHANGE_ARGS as GATEWAY_EXCHANGE_ARGS,
    HANDOFF_ARGS as GATEWAY_HANDOFF_ARGS,
    RETURN_ARGS as GATEWAY_RETURN_ARGS,
    STOCK_TSHIRT_L,
    Stage6Database,
    approval,
    identity,
    validate,
)
from tests.test_v2_eval_runtime import base_case


class RuntimeTests(unittest.TestCase):
    def test_each_case_run_gets_a_fresh_file_database_that_is_removed(self):
        runtime = Stage6CaseRuntime.from_case(support.exchange_case())
        path = runtime.db_path
        self.assertTrue(path.is_file())
        self.assertEqual(path.suffix, ".db")
        other = Stage6CaseRuntime.from_case(support.exchange_case())
        self.assertNotEqual(other.db_path, path)
        runtime.close()
        other.close()
        self.assertFalse(path.exists())

    def test_an_invalid_case_never_starts(self):
        case = support.exchange_case()
        case["expected_answerability"]["final"] = "answer"
        with self.assertRaises(EvalCaseInvalid):
            Stage6CaseRuntime.from_case(case)

    def test_read_side_is_query_only(self):
        with Stage6CaseRuntime.from_case(support.exchange_case()) as runtime:
            connection = runtime.read_runtime.context.connection
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.Error):
                connection.execute("DELETE FROM orders")

    def test_restart_rebuilds_every_system_object(self):
        with Stage6CaseRuntime.from_case(support.waiting_return_case()) as runtime:
            gateway, context = runtime.gateway, runtime.read_runtime.context
            read_gateway, injector = runtime.read_gateway, runtime.injector
            runtime.restart()
            self.assertIsNot(runtime.gateway, gateway)
            self.assertIsNot(runtime.read_runtime.context, context)
            self.assertIsNot(runtime.read_runtime.context.connection, context.connection)
            with self.assertRaises(sqlite3.ProgrammingError):  # the old connection is closed
                context.connection.execute("SELECT 1")
            # harness instrumentation spans the whole case-run
            self.assertIs(runtime.read_gateway, read_gateway)
            self.assertIs(runtime.injector, injector)
            self.assertEqual((runtime.restarts, runtime.gateways_built), (1, 2))

    def test_advance_clock_gives_new_objects_at_the_new_instant(self):
        with Stage6CaseRuntime.from_case(support.waiting_return_case()) as runtime:
            gateway = runtime.gateway
            runtime.advance_clock(support.LATE)
            self.assertIsNot(runtime.gateway, gateway)
            self.assertEqual(runtime.read_runtime.context.clock.now().isoformat(), support.LATE)
            with self.assertRaises(Exception):
                runtime.advance_clock(support.NOW)


class ReadRuntimeProtocolTests(unittest.TestCase):
    def test_both_runtimes_satisfy_the_protocol(self):
        for owner in (V2CaseRuntime, Stage6ReadRuntime):
            for name in faults_module.READ_RUNTIME_MEMBERS:
                self.assertTrue(hasattr(owner, name), (owner.__name__, name))
        with self.assertRaises(ValueError):
            FaultInjectingGateway(object())

    def test_stage5_runtime_still_drives_the_gateway(self):
        runtime = V2CaseRuntime.from_case(base_case())
        try:
            gateway = FaultInjectingGateway(runtime)
            result = gateway.execute("get_order", {"order_id": "ORD-1001"}, observation_id="obs-1")
            self.assertEqual(result.status.value, "ok")
        finally:
            runtime.close()

    def test_stage6_read_faults_use_the_same_semantics(self):
        case = support.read_fault_consult()
        case["initial_state"]["faults"].append({"tool": "get_logistics", "match": {}, "mode": "malformed",
                                                "on_call": 1})
        factory = support.scripted([
            support.reply(support.native("get_order", {"order_id": "ORD-1001"}, "c1")),
            support.reply(support.native("get_logistics", {"order_id": "ORD-1001"}, "c2")),
            support.reply(support.native("finish", {"disposition": "refuse"}, "c3"))])
        result = run_stage6_case(case, factory)
        observations = result.main_record.observations
        self.assertEqual(type(observations[0]).__name__, "ToolObservation")
        self.assertEqual(observations[0].result.status.value, "error")
        self.assertEqual(type(observations[1]).__name__, "ToolContractFailure")
        self.assertEqual([record.outcome for record in result.read_fault_records],
                         ["injected_error", "injected_malformed"])


class ActionFaultTests(unittest.TestCase):
    def test_declarations_are_checked(self):
        for bad in ([{"point": "refund", "mode": "error", "on_call": 1}],
                    [{"point": "commit", "read": "order", "mode": "error", "on_call": 1}],
                    [{"point": "commit", "mode": "malformed", "on_call": 1}],
                    [{"point": "commit", "mode": "error", "on_call": 0}]):
            with self.assertRaises(ActionFaultConfigurationError):
                ActionFaultInjector(bad)
        injector = ActionFaultInjector([{"point": "commit", "mode": "error", "on_call": 1},
                                        {"point": "commit", "mode": "error", "on_call": 1}])
        with self.assertRaises(ActionFaultConfigurationError):
            injector.before("commit")

    def test_counters_span_start_and_resume(self):
        # guard_read "order" #1 is the start's read; #2 is the resume's.
        result = run_stage6_reference(support.resume_fault_case("guard_read", on_call=2, read="order",
                                                                code="state_read_failed"))
        self.assertEqual(result.main_outcome.status.value, "WAITING_APPROVAL")
        self.assertEqual(result.events[0].outcome["code"], "state_read_failed")
        fired = [record for record in result.action_fault_records if record.outcome != "delegated"]
        self.assertEqual([(record.point, record.read) for record in fired], [("guard_read", "order")])
        self.assertEqual(sum(1 for record in result.action_fault_records
                             if record.point == "guard_read" and record.read == "order"), 2)

    def test_commit_fault_then_replay_reuses_the_counter(self):
        result = run_stage6_reference(support.commit_fault_then_replay())
        self.assertEqual(result.main_outcome.code, "transaction_failed")
        self.assertEqual(result.events[0].outcome["status"], "EXECUTED")
        self.assertEqual([record.outcome for record in result.action_fault_records
                          if record.point == "commit"], ["injected_error", "delegated"])

    def test_malformed_feeds_the_real_decoders(self):
        seen = []
        real = GuardStateReader._read

        def spy(self, *args, **kwargs):
            try:
                return real(self, *args, **kwargs)
            except Exception as error:
                seen.append(type(error).__name__ + ":" + getattr(error, "code", ""))
                raise

        with mock.patch.object(GuardStateReader, "_read", spy):
            result = run_stage6_reference(support.guard_read_case("malformed"))
        self.assertEqual(result.main_outcome.code, "state_malformed")
        self.assertEqual(seen, ["GuardFailure:state_malformed"])  # raised inside the reader's decoding
        self.assertFalse(result.main_guard_decided)

    def test_no_production_result_is_rewritten(self):
        # A fault replaces the boundary call; nothing runs first and is then edited.
        result = run_stage6_reference(support.write_fault_case("business_write"))
        self.assertEqual(result.final_state["after_sales_cases"], result.baseline_state["after_sales_cases"])
        self.assertTrue(result.main_guard_decided)
        self.assertEqual(result.main_guard.decision.value, "ALLOW")


class InertHookTests(unittest.TestCase):
    """The 6.4 hooks change nothing when absent, and an inert hook changes nothing either."""

    ACTIONS = (("create_exchange", GATEWAY_EXCHANGE_ARGS), ("create_return", GATEWAY_RETURN_ARGS),
               ("escalate_to_human", GATEWAY_HANDOFF_ARGS),
               ("create_return", {**GATEWAY_RETURN_ARGS, "order_id": "ORD-2001", "order_item_id": "OI-2001-2"}))

    class Inert:
        def __init__(self):
            self.reads = []
            self.decisions = []

        def before_read(self, read):
            self.reads.append(read)
            return None

        def observe(self, decision):
            self.decisions.append(decision)

    def run_all(self, **hooks):
        with Stage6Database() as db:
            db.execute(STOCK_TSHIRT_L)
            gateway = db.gateway(**hooks)
            outcomes = [gateway.start_action(identity("req-" + str(i)), validate(name, args)).to_dict()
                        for i, (name, args) in enumerate(self.ACTIONS)]
            pid = outcomes[1]["pending_action_id"]
            outcomes.append(gateway.resume_action(approval(pid)).to_dict())
            return outcomes, db.dump()

    def test_inert_hooks_are_byte_identical(self):
        plain = self.run_all()
        inert = self.Inert()
        hooked = self.run_all(guard_read_hook=inert, decision_observer=inert.observe)
        self.assertEqual(hooked, plain)
        self.assertTrue(set(inert.reads) <= set(GUARD_READS))
        self.assertEqual(inert.reads[:2], ["order", "order_item"])
        self.assertEqual(len(inert.decisions), 5)  # four starts and one resume

    def test_reader_without_hook_is_the_default(self):
        reader = GuardStateReader()
        self.assertIsNone(reader._read_hook)
        with self.assertRaises(ValueError):
            GuardStateReader(read_hook=object())
        self.assertEqual(READ_MALFORMED, "malformed")


class OperatorEventTests(unittest.TestCase):
    def test_pending_id_comes_only_from_the_main_run(self):
        case = support.approved_return_case()
        self.assertNotIn("PA-", str(case["operator_script"]))
        result = run_stage6_reference(case)
        self.assertEqual(result.events[0].action_outcome.pending_action_id, result.main_outcome.pending_action_id)

    def test_approval_uses_the_trusted_operator_and_business_time(self):
        result = run_stage6_reference(support.guard_denies_on_resume())
        pending = next(iter(result.final_state["pending_actions"].values()))
        self.assertEqual((pending["approver_ref"], pending["decided_at"]), ("op-demo-1", support.LATE))

    def test_event_without_a_pending_is_recorded_not_raised(self):
        case = support.approved_return_case()
        factory = support.scripted([support.reply(support.native("finish", {"disposition": "answer"}, "c1"))])
        result = run_stage6_case(case, factory)
        self.assertEqual(result.events[0].outcome, {"status": None})

    def test_replay_submission_uses_the_same_identity_and_action(self):
        result = run_stage6_reference(support.a21a())
        self.assertTrue(all(event.action is result.main_action for event in result.events))
        self.assertTrue(all(event.request_id == "req-1" for event in result.events))
        self.assertTrue(all(event.action_outcome.idempotent_replay for event in result.events))

    def test_rerun_uses_a_fresh_policy_and_new_request_uses_req2(self):
        conversation = [support.action_reply("create_exchange", support.EXCHANGE_ARGS)]
        factory = support.scripted(conversation, conversation)
        result = run_stage6_case(support.a21b(), factory)
        self.assertEqual(len(factory.providers), 2)
        self.assertIsNot(factory.providers[0], factory.providers[1])
        self.assertEqual(result.events[0].run_record.request_id, "req-1")
        factory = support.scripted(conversation, conversation)
        result = run_stage6_case(support.a21d(), factory)
        self.assertEqual(result.events[0].run_record.request_id, "req-2")

    def test_operator_script_never_reaches_the_policy(self):
        seen = []

        class Watching:
            def __init__(self, inner):
                self.inner = inner
                self.decision_records = ()

            def next_action(self, state):
                seen.append(state)
                action = self.inner.next_action(state)
                self.decision_records = self.inner.decision_records
                return action

        base = support.scripted([support.action_reply("create_return", support.RETURN_ARGS)],
                                [support.action_reply("create_return", support.RETURN_ARGS)])
        case = support.a22d()
        case["operator_script"].append({"op": "rerun_request"})
        case["expected_action"]["events"].append(support.event("STALE", "record_version_changed", replay=True))
        run_stage6_case(case, lambda: Watching(base()))
        self.assertTrue(seen)
        for state in seen:
            self.assertIs(type(state), ActionControlState)
            text = repr(state)
            for leaked in ("operator", "mutate", "approve", "record_decision", "restart", "PA-", "op-demo-1"):
                self.assertNotIn(leaked, text)

    def test_mutate_is_trusted_and_in_baseline(self):
        result = run_stage6_reference(support.a22a())
        self.assertEqual(result.final_state["order_items"]["OI-1001-2"]["version"], 2)
        self.assertEqual(result.baseline_state["order_items"]["OI-1001-2"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
