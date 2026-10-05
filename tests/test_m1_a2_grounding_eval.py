"""M1-A2: the grounding gate around the Stage 6 policy, offline (scripted policies, no API).

Numbered tests follow the M1-A2 brief, section 5. Every database is a frozen
DEV case's own (Stage6CaseRuntime.from_case); no schema is made up here.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import importlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aftersales.action_gateway import ActionGateway
from aftersales.executor import TRACE_OBSERVATION_ID
from aftersales_service.action_grounding import GroundingRejected
from eval_m1 import grounding_policy, run_m1_a2, summarize
from eval_m1.grounding_policy import (
    ATTRIBUTION_FAIL_CLOSED,
    ATTRIBUTION_FALSE,
    ATTRIBUTION_TRUE,
    REJECTION_DISPOSITION,
    GroundingGatedPolicy,
)
from eval_v2.action_control import STAGE6_MAX_STEPS, ActionControlState, ActionIntent
from eval_v2.control import (
    ControlPolicyContractError,
    Finish,
    ToolCall,
    ToolContractFailure,
    ToolObservation,
    UserMessage,
    finish_dispositions,
)
from eval_v2.runner import observation_id_for
from eval_v2.stage6_runner import run_stage6_case
from eval_v2.stage6_runtime import Stage6CaseRuntime
from eval_v2.stage6_scoring import HARD_INVARIANTS

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "eval" / "v2" / "stage6-dev.json"
DEV_SHA256 = "80df024f9fde9dbe6b116ff7b12a2613bdfe8d87d7234b453e8ee3cd287bcbf3"
PRIVATE_TEXT = "private-user-text-never-serialize-this"
_CASES: dict[str, dict] = {}


def dev_case(case_id: str) -> dict:
    if not _CASES:
        raw = DEV.read_bytes()
        if hashlib.sha256(raw).hexdigest() != DEV_SHA256:
            raise AssertionError("the frozen DEV bytes changed")
        _CASES.update({case["case_id"]: case for case in json.loads(raw)})
    return copy.deepcopy(_CASES[case_id])


class ScriptedPolicy:
    """Returns its script, one item per decision; a callable item gets the state."""

    def __init__(self, *actions):
        self.actions = iter(actions)
        self.decision_records = []

    def next_action(self, state):
        action = next(self.actions)
        return action(state) if callable(action) else action


def read_order(order_id="ORD-1001"):
    return ToolCall(tool_name="get_order", arguments={"order_id": order_id})


def read_inventory(sku):
    return ToolCall(tool_name="get_inventory", arguments={"sku": sku})


def return_intent(order_id="ORD-1001", order_item_id="OI-1001-2"):
    return ActionIntent(action_name="create_return", arguments={
        "order_id": order_id, "order_item_id": order_item_id, "reason_code": "size_or_spec_mismatch"})


def expected_intent(case: dict) -> ActionIntent:
    return ActionIntent(action_name=case["expected_action"]["action_name"],
                        arguments=case["expected_action"]["args"])


def state(runtime, *, step=1, observations=()):
    return ActionControlState(
        virtual_now=runtime.virtual_now, persona_id=runtime.persona.persona_id,
        allowed_tools=runtime.capabilities.read_tools, allowed_actions=runtime.capabilities.actions,
        max_steps=STAGE6_MAX_STEPS, step_number=step, remaining_steps=STAGE6_MAX_STEPS - step + 1,
        user_messages=(UserMessage(turn_index=1, text=PRIVATE_TEXT),), observations=tuple(observations))


def observe(runtime, call, *, tool_step=1):
    observation_id = observation_id_for(1, tool_step)
    result = runtime.read_gateway.execute(call.tool_name, call.arguments, observation_id=observation_id)
    return ToolObservation(sequence=tool_step, control_step=tool_step, turn_index=1, tool_step=tool_step,
                           observation_id=observation_id, tool_name=call.tool_name,
                           arguments=call.arguments, result=result)


class _Fixture(unittest.TestCase):
    def runtime(self, case_id="s6-dev-004"):
        runtime = Stage6CaseRuntime.from_case(dev_case(case_id))
        self.addCleanup(runtime.close)
        return runtime

    def wrapper(self, *actions, mode="enforce", case_id="s6-dev-004", policy_run=1):
        return GroundingGatedPolicy(ScriptedPolicy(*actions), mode=mode, case_id=case_id, round=1,
                                    policy_run=policy_run)

    def run_script(self, *actions, mode="enforce", case_id="s6-dev-004", case=None):
        wrappers = []

        def factory():
            wrapper = self.wrapper(*actions, mode=mode, case_id=case_id, policy_run=len(wrappers) + 1)
            wrappers.append(wrapper)
            return wrapper

        run = run_stage6_case(case or dev_case(case_id), factory)
        return run, wrappers[0]

    def decision(self, wrapper, index=0) -> dict:
        return wrapper.grounding_decisions[index].to_dict()


class ShadowAndEnforceTests(_Fixture):
    def test_01_shadow_forwards_an_unread_submission_unchanged_and_records_it(self):
        runtime = self.runtime()
        intent = return_intent()
        wrapper = self.wrapper(intent, mode="shadow")
        self.assertIs(wrapper.next_action(state(runtime)), intent)
        decision = self.decision(wrapper)
        self.assertFalse(decision["grounded"])
        self.assertEqual(decision["code"], "missing_order_observation")
        self.assertEqual(decision["attribution"], ATTRIBUTION_TRUE)
        self.assertEqual(decision["audit"]["reason"], "no_order_read")
        self.assertEqual(decision["supports"], [])
        # Through the frozen runner, the shadow submission reaches the real gateway.
        run, wrapper = self.run_script(return_intent(), mode="shadow")
        self.assertEqual(run.main_outcome.status.value, "WAITING_APPROVAL")
        self.assertEqual(self.decision(wrapper)["code"], "missing_order_observation")

    def test_02_enforce_replaces_it_with_finish_and_nothing_reaches_the_gateway(self):
        original = ActionGateway.start_action
        with mock.patch.object(ActionGateway, "start_action", autospec=True, side_effect=original) as gateway:
            run, wrapper = self.run_script(return_intent())
        gateway.assert_not_called()
        self.assertIsNone(run.main_outcome)
        self.assertIsNone(run.main_action)
        self.assertEqual(run.main_record.termination, "finished")
        self.assertEqual(run.main_record.final_disposition, REJECTION_DISPOSITION)
        self.assertEqual(run.initial_state, run.final_state)   # no business, pending or receipt row
        self.assertEqual(run.main_audit, ())
        self.assertEqual(self.decision(wrapper)["code"], "missing_order_observation")
        runtime = self.runtime()
        returned = self.wrapper(return_intent()).next_action(state(runtime))
        self.assertEqual(returned, Finish(disposition=REJECTION_DISPOSITION))

    def test_rejection_disposition_is_the_frozen_missing_evidence_one(self):
        contract = json.loads((ROOT / "eval/v2/spec/stage6-final-outcomes.json").read_text("utf-8"))
        self.assertEqual(REJECTION_DISPOSITION, "refuse")
        self.assertIn(REJECTION_DISPOSITION, finish_dispositions())
        self.assertIn("必需的动作参数所依赖的证据缺失", contract["definitions"]["refuse"])
        self.assertIn("越过受信身份、权限", contract["definitions"]["boundary"])

    def test_03_a_read_order_then_its_submission_passes_in_both_modes(self):
        for mode in ("shadow", "enforce"):
            with self.subTest(mode=mode):
                run, wrapper = self.run_script(read_order(), return_intent(), mode=mode)
                self.assertEqual(run.main_outcome.status.value, "WAITING_APPROVAL")
                decision = self.decision(wrapper)
                self.assertTrue(decision["grounded"])
                self.assertIsNone(decision["code"])
                self.assertIsNone(decision["attribution"])
                self.assertTrue(decision["audit"]["gate_agrees"])
                self.assertEqual(decision["supports"], ["turn:1:tool:1"])
                self.assertEqual(decision["prior_read_tools"], ["get_order"])
                self.assertEqual(decision["args_sha256"], run.main_action.args_sha256)


class RejectionCodeTests(_Fixture):
    """04: each rule on frozen DEV databases (s6-dev-004 = demo-a's ORD-1001 / ORD-1002)."""

    def assert_rejected(self, wrapper, code, reason):
        decision = self.decision(wrapper)
        self.assertFalse(decision["grounded"])
        self.assertEqual(decision["code"], code)
        self.assertEqual(decision["attribution"], ATTRIBUTION_TRUE)
        self.assertEqual(decision["audit"]["reason"], reason)
        self.assertTrue(decision["audit"]["code_consistent"])
        self.assertTrue(decision["audit"]["gate_agrees"])

    def test_04a_read_a_submit_b(self):
        run, wrapper = self.run_script(read_order(), return_intent("ORD-1002", "OI-1002-1"))
        self.assertIsNone(run.main_outcome)
        self.assert_rejected(wrapper, "missing_order_observation", "no_order_read")

    def test_04b_order_of_a_with_the_item_of_b(self):
        run, wrapper = self.run_script(read_order(), read_order("ORD-1002"), return_intent(order_item_id="OI-1002-1"))
        self.assertIsNone(run.main_outcome)
        self.assert_rejected(wrapper, "target_relation_mismatch", "item_not_in_latest_order_read")
        self.assertEqual(self.decision(wrapper)["audit"]["value_sources"]["order_item_id"],
                         {"in_user_text": False, "in_some_read": True})

    def test_04c_an_item_no_read_returned(self):
        _, wrapper = self.run_script(read_order(), return_intent(order_item_id="OI-NOT-READ"))
        self.assert_rejected(wrapper, "target_not_observed", "item_not_in_latest_order_read")

    def test_04d_a_failed_read(self):
        case = dev_case("s6-dev-004")
        case["initial_state"]["faults"] = [{"tool": "get_order", "match": {"order_id": "ORD-1001"},
                                            "mode": "error", "on_call": 1}]
        run, wrapper = self.run_script(read_order(), return_intent(), case=case)
        self.assertIsNone(run.main_outcome)
        self.assert_rejected(wrapper, "stale_or_failed_observation", "order_read_not_ok")

    def test_04e_an_empty_read(self):
        _, wrapper = self.run_script(read_order("ORD-NOT-EXIST"), return_intent("ORD-NOT-EXIST", "OI-NOT-EXIST"))
        self.assert_rejected(wrapper, "stale_or_failed_observation", "order_read_not_ok")

    def test_04f_another_customers_order_reads_empty(self):
        # s6-dev-025: demo-a asks about demo-b's ORD-2001; the read is empty, so no grounding.
        case = dev_case("s6-dev-025")
        run, wrapper = self.run_script(read_order("ORD-2001"), expected_intent(case), case_id="s6-dev-025")
        self.assertIsNone(run.main_outcome)
        self.assert_rejected(wrapper, "stale_or_failed_observation", "order_read_not_ok")

    def test_04g_a_later_failed_read_is_not_rescued_by_an_earlier_success(self):
        case = dev_case("s6-dev-004")
        case["initial_state"]["faults"] = [{"tool": "get_order", "match": {"order_id": "ORD-1001"},
                                            "mode": "error", "on_call": 2}]
        _, wrapper = self.run_script(read_order(), read_order(), return_intent(), case=case)
        self.assert_rejected(wrapper, "stale_or_failed_observation", "order_read_not_ok")
        self.assertTrue(self.decision(wrapper)["audit"]["earlier_order_read_had_target"])

    def test_05_exchange_needs_a_read_of_the_target_sku(self):
        case = dev_case("s6-dev-001")   # the customer names SKU-TSHIRT-M in the message
        args = case["expected_action"]["args"]
        for mode in ("shadow", "enforce"):
            with self.subTest(mode=mode, inventory=False):
                run, wrapper = self.run_script(read_order(args["order_id"]), expected_intent(case), mode=mode,
                                               case_id="s6-dev-001")
                self.assert_rejected(wrapper, "missing_inventory_observation", "no_inventory_read")
                self.assertEqual(self.decision(wrapper)["audit"]["value_sources"]["target_sku"],
                                 {"in_user_text": True, "in_some_read": False})
                self.assertEqual(run.main_outcome is None, mode == "enforce")
            with self.subTest(mode=mode, inventory=True):
                run, wrapper = self.run_script(read_order(args["order_id"]), read_inventory(args["target_sku"]),
                                               expected_intent(case), mode=mode, case_id="s6-dev-001")
                decision = self.decision(wrapper)
                self.assertTrue(decision["grounded"])
                self.assertEqual(decision["supports"], ["turn:1:tool:1", "turn:1:tool:2"])
                self.assertEqual(run.main_outcome.status.value, "EXECUTED")


class FailClosedTests(_Fixture):
    def test_06_an_observation_that_does_not_pair_is_never_registered(self):
        for mismatch in ("id", "tool", "arguments", "result_tool", "result_id"):
            with self.subTest(mismatch=mismatch):
                runtime = self.runtime()
                call = read_order()
                wrapper = self.wrapper(call, return_intent())
                self.assertIs(wrapper.next_action(state(runtime)), call)
                observation = observe(runtime, call)
                if mismatch == "id":
                    observation = dataclasses.replace(observation, observation_id="turn:1:tool:9")
                elif mismatch == "tool":
                    observation = dataclasses.replace(observation, tool_name="get_inventory")
                elif mismatch == "arguments":
                    observation = dataclasses.replace(observation, arguments={"order_id": "ORD-1002"})
                elif mismatch == "result_tool":
                    observation.result.tool_name = "get_inventory"
                else:
                    observation.result.trace[TRACE_OBSERVATION_ID] = "turn:1:tool:9"
                answer = wrapper.next_action(state(runtime, step=2, observations=(observation,)))
                self.assertEqual(answer, Finish(disposition=REJECTION_DISPOSITION))
                decision = self.decision(wrapper)
                self.assertFalse(decision["grounded"])
                self.assertEqual(decision["attribution"], ATTRIBUTION_FAIL_CLOSED)
                self.assertFalse(decision["audit"]["provenance_complete"])
                self.assertEqual(wrapper._ledger.entries, ())
                self.assertTrue(wrapper.diagnostics)

    def test_06b_an_observation_without_a_forwarded_call_is_never_registered(self):
        runtime = self.runtime()
        wrapper = self.wrapper(return_intent())
        answer = wrapper.next_action(state(runtime, observations=(observe(runtime, read_order()),)))
        self.assertIsInstance(answer, Finish)
        self.assertEqual(self.decision(wrapper)["attribution"], ATTRIBUTION_FAIL_CLOSED)
        self.assertEqual(wrapper.diagnostics[0]["code"], "observation_without_forwarded_call")
        self.assertIsNone(wrapper.diagnostics[0]["observation_id"])

    def test_06c_an_injected_malformed_result_grounds_nothing(self):
        case = dev_case("s6-dev-004")
        case["initial_state"]["faults"] = [{"tool": "get_order", "match": {"order_id": "ORD-1001"},
                                            "mode": "malformed", "on_call": 1}]
        run, wrapper = self.run_script(read_order(), read_order(), return_intent(), case=case)
        self.assertIsInstance(run.main_record.observations[0], ToolContractFailure)
        self.assertIsNone(run.main_outcome)   # fail-closed even though the second read succeeded
        self.assertEqual(self.decision(wrapper)["attribution"], ATTRIBUTION_FAIL_CLOSED)
        self.assertEqual(wrapper.diagnostics[0]["code"], "observation_without_tool_result")

    def test_the_visible_set_is_fixed_before_the_inner_policy_decides(self):
        runtime = self.runtime()
        call = read_order()
        observation = observe(runtime, call)

        def read_appears_after_the_snapshot(current):
            object.__setattr__(current, "observations", (observation,))
            return return_intent()

        wrapper = self.wrapper(call, read_appears_after_the_snapshot)
        wrapper.next_action(state(runtime))
        self.assertIsInstance(wrapper.next_action(state(runtime, step=2)), Finish)
        self.assertEqual(self.decision(wrapper)["code"], "missing_order_observation")

    def test_a_registered_read_no_longer_in_the_state_is_not_visible(self):
        runtime = self.runtime()
        call = read_order()
        observation = observe(runtime, call)
        wrapper = self.wrapper(call, Finish(disposition="answer"), return_intent())
        wrapper.next_action(state(runtime))
        wrapper.next_action(state(runtime, step=2, observations=(observation,)))
        self.assertIsInstance(wrapper.next_action(state(runtime, step=3)), Finish)
        self.assertEqual(self.decision(wrapper)["code"], "missing_order_observation")


class RecordAndContractTests(_Fixture):
    def test_07_records_hold_no_argument_value_user_text_or_customer_id(self):
        exchange = dev_case("s6-dev-001")
        args = exchange["expected_action"]["args"]
        scenarios = [
            ("s6-dev-004", (read_order(), return_intent())),
            ("s6-dev-004", (return_intent(),)),
            ("s6-dev-004", (read_order(), read_order("ORD-1002"), return_intent(order_item_id="OI-1002-1"))),
            ("s6-dev-001", (read_order(args["order_id"]), expected_intent(exchange))),
            ("s6-dev-001", (read_order(args["order_id"]), read_inventory(args["target_sku"]), expected_intent(exchange))),
        ]
        for case_id, script in scenarios:
            for mode in ("shadow", "enforce"):
                with self.subTest(case_id=case_id, mode=mode, steps=len(script)):
                    case = dev_case(case_id)
                    run, wrapper = self.run_script(*script, mode=mode, case_id=case_id)
                    serialized = json.dumps({"decisions": [item.to_dict() for item in wrapper.grounding_decisions],
                                             "diagnostics": wrapper.diagnostics}, ensure_ascii=False)
                    values = {value for item in script for value in item.arguments.values()}
                    for private in (*values, run.customer_id, *(turn["text"] for turn in case["user_turns"]),
                                    "CUST-00"):
                        self.assertNotIn(private, serialized)
        # A scripted state's user text never reaches a record either.
        runtime = self.runtime()
        wrapper = self.wrapper(return_intent())
        wrapper.next_action(state(runtime))
        self.assertNotIn(PRIVATE_TEXT, json.dumps(self.decision(wrapper), ensure_ascii=False))

    def test_an_invalid_intent_is_returned_unchanged_for_the_runner(self):
        runtime = self.runtime()
        invalid = ActionIntent(action_name="create_return", arguments={"order_id": "ORD-1001"})
        for mode in ("shadow", "enforce"):
            with self.subTest(mode=mode):
                wrapper = self.wrapper(invalid, mode=mode)
                self.assertIs(wrapper.next_action(state(runtime)), invalid)
                self.assertEqual(wrapper.grounding_decisions, [])
                with self.assertRaises(ControlPolicyContractError):
                    self.run_script(invalid, mode=mode)

    def test_inner_decision_records_are_the_policys_own(self):
        inner = ScriptedPolicy(Finish(disposition="answer"))
        marker = object()
        inner.decision_records.append(marker)
        wrapper = GroundingGatedPolicy(inner, mode="shadow", case_id="s6-dev-004", round=1)
        self.assertEqual(tuple(wrapper.decision_records), (marker,))

    def test_each_rerun_gets_a_fresh_ledger(self):
        for case_id in ("s6-dev-028", "s6-dev-029"):   # rerun_request, new_request
            case = dev_case(case_id)
            args = case["expected_action"]["args"]
            reads = [read_order(args["order_id"])]
            if "target_sku" in args:
                reads.append(read_inventory(args["target_sku"]))
            for rerun_reads in (False, True):
                with self.subTest(case_id=case_id, rerun_reads=rerun_reads):
                    wrappers = []

                    def factory():
                        script = reads if not wrappers or rerun_reads else []
                        wrapper = self.wrapper(*script, expected_intent(case), case_id=case_id,
                                               policy_run=len(wrappers) + 1)
                        wrappers.append(wrapper)
                        return wrapper

                    run = run_stage6_case(case, factory)
                    self.assertEqual(len(wrappers), 2)
                    self.assertTrue(self.decision(wrappers[0])["grounded"])
                    rerun = self.decision(wrappers[1])
                    self.assertEqual(rerun["policy_run"], 2)
                    self.assertEqual(rerun["grounded"], rerun_reads)
                    self.assertEqual(run.events[0].action_outcome is None, not rerun_reads)

    def test_a_read_schema_example_still_grounds_a_wrong_target(self):
        # s6-dev-015: the customer meant ORD-3015; reading ORD-1001 grounds it (M1-A3, not M1-A1).
        run, wrapper = self.run_script(read_order(), return_intent(order_item_id="OI-1001-1"), case_id="s6-dev-015")
        self.assertTrue(self.decision(wrapper)["grounded"])
        self.assertEqual(run.main_outcome.status.value, "WAITING_APPROVAL")

    def test_a_rejection_the_rules_allow_is_attributed_false(self):
        with mock.patch.object(grounding_policy, "ground_action",
                               side_effect=GroundingRejected("missing_order_observation")):
            run, wrapper = self.run_script(read_order(), return_intent())
        self.assertIsNone(run.main_outcome)
        decision = self.decision(wrapper)
        self.assertEqual(decision["attribution"], ATTRIBUTION_FALSE)
        self.assertFalse(decision["audit"]["gate_agrees"])

    def test_an_admission_the_rules_forbid_is_flagged(self):
        admitted = mock.Mock(supports=())
        with mock.patch.object(grounding_policy, "ground_action", return_value=admitted):
            run, wrapper = self.run_script(read_order(), return_intent("ORD-1002", "OI-1002-1"), mode="shadow")
        self.assertIsNotNone(run.main_outcome)
        decision = self.decision(wrapper)
        self.assertTrue(decision["grounded"])
        self.assertEqual(decision["audit"]["reason"], "no_order_read")
        self.assertFalse(decision["audit"]["gate_agrees"])


# --------------------------------------------------------------------------
# The run script and the summary, offline
# --------------------------------------------------------------------------


def scripted_run_case(scripts):
    """run_case for run_experiment: the frozen runner and scorer with scripted inner policies."""

    def run_case(case, mode, round_number, *, inner_factory, generator_factory):
        made = []

        def scripted():
            script = scripts[case["case_id"]][len(made)]
            made.append(script)
            return ScriptedPolicy(*script)

        return run_m1_a2.run_one(case, mode, round_number, inner_factory=scripted, generator_factory=lambda: None)

    return run_case


def experiment_scripts():
    returned = dev_case("s6-dev-004")["expected_action"]["args"]
    handoff = dev_case("s6-dev-005")
    rerun = dev_case("s6-dev-028")
    rerun_args = rerun["expected_action"]["args"]
    return {
        # read, then submit: grounded under both modes
        "s6-dev-004": [(read_order(returned["order_id"]), expected_intent(dev_case("s6-dev-004")))],
        # r1's pattern: escalate without get_order
        "s6-dev-005": [(expected_intent(handoff),)],
        # main run grounded; the rerun_request submits again without a read
        "s6-dev-028": [(read_order(rerun_args["order_id"]), expected_intent(rerun)), (expected_intent(rerun),)],
    }


EXPERIMENT_CASES = ("s6-dev-004", "s6-dev-005", "s6-dev-028")


def offline_experiment(directory: Path, rounds=1, run_case=None):
    cases = [dev_case(case_id) for case_id in EXPERIMENT_CASES]
    out = Path(directory) / "out"
    context = {"commit": "0" * 40, "scope": summarize.SCOPE}
    with mock.patch("builtins.print"):
        summary = run_m1_a2.run_experiment(
            cases, out, context, rounds=rounds, inner_factory=None, generator_factory=None,
            run_case=run_case or scripted_run_case(experiment_scripts()))
    return out, summary


class ExperimentTests(unittest.TestCase):
    CASES = EXPERIMENT_CASES

    def experiment(self, rounds=1, run_case=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return offline_experiment(directory.name, rounds, run_case)

    def test_end_to_end_counts_costs_and_attributions(self):
        out, summary = self.experiment()
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["stop_required"], summary["integrity_problems"])
        shadow, enforce = (summary["groups"][mode]["rounds"][0] for mode in ("shadow", "enforce"))
        self.assertEqual((shadow["ungrounded_admitted"], enforce["ungrounded_admitted"]), (1, 0))
        self.assertEqual(shadow["ungrounded_admitted_cases"], ["s6-dev-005"])
        self.assertEqual((shadow["ungrounded_admitted_rerun"], enforce["ungrounded_admitted_rerun"]), (1, 0))
        for group in (shadow, enforce):
            self.assertEqual(group["rejections"]["by_code"], {"missing_order_observation": 2})
            self.assertEqual(group["rejections"]["by_attribution"], {"true_rejection": 2})
            self.assertEqual((group["rejections"]["main"], group["rejections"]["rerun"]), (1, 1))
            self.assertEqual(group["hard_invariants"], {name: 3 for name in HARD_INVARIANTS})
        comparison = summary["comparisons"]["rounds"][0]
        self.assertEqual(comparison["completion_cost_cases"], ["s6-dev-005"])
        self.assertEqual(comparison["metrics"]["final_ok"]["cost_cases"], ["s6-dev-005"])
        self.assertIn("s6-dev-028", comparison["metrics"]["stage6_e2e_success"]["cost_cases"])
        self.assertEqual([item["case_id"] for item in summary["rejection_attributions"] if item["mode"] == "enforce"],
                         ["s6-dev-005", "s6-dev-028"])
        # Output layout and hashes.
        self.assertTrue((out / "round-1" / "shadow" / "cases.jsonl").is_file())
        self.assertTrue((out / "round-1" / "enforce" / "meta.json").is_file())
        manifest = json.loads((out / "manifest.json").read_text("utf-8"))
        self.assertIn("summary.json", manifest["files_sha256"])
        self.assertIn("report.md", manifest["files_sha256"])
        report = (out / "report.md").read_text("utf-8")
        self.assertIn(summarize.SCOPE, report)
        self.assertIn("真拦截", report)
        row = json.loads((out / "round-1" / "enforce" / "cases.jsonl").read_text("utf-8").splitlines()[1])
        self.assertEqual(row["run"]["main_record"]["final_disposition"], REJECTION_DISPOSITION)
        self.assertEqual(set(row), {"schema", "case_id", "mode", "round", "started_at", "finished_at", "score",
                                    "run", "grounding_decisions", "diagnostics", "provider_failure", "error", "calls"})

    def test_rounds_run_shadow_then_enforce_and_failures_never_stop_the_plan(self):
        calls = []

        def fake(case, mode, round_number, **_):
            calls.append((round_number, mode, case["case_id"]))
            row = {"schema": run_m1_a2.ROW_SCHEMA, "case_id": case["case_id"], "mode": mode,
                   "round": round_number, "score": None, "run": None, "grounding_decisions": [],
                   "diagnostics": [], "provider_failure": None, "error": None, "calls": {}}
            if case["case_id"] == "s6-dev-004":
                row["provider_failure"] = {"phase": "run", "error_type": "ReadTimeout"}
            else:
                row["error"] = {"phase": "score", "error_type": "KeyError"}
            return row

        _, summary = self.experiment(rounds=2, run_case=fake)
        expected = [(number, mode, case_id) for number in (1, 2) for mode in ("shadow", "enforce")
                    for case_id in self.CASES]
        self.assertEqual(calls, expected)
        self.assertFalse(summary["complete"])
        self.assertEqual(len(summary["provider_failures"]), 4)
        self.assertEqual(summary["stop_reasons"], ["unexpected_error"])

    def test_output_must_be_a_new_or_empty_directory(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        (Path(directory.name) / "left-over").write_text("x", encoding="utf-8")
        with self.assertRaises(run_m1_a2.PreflightError):
            run_m1_a2.run_experiment([], directory.name, {"commit": "0"}, inner_factory=None,
                                     generator_factory=None)


class SummaryStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        out, _ = offline_experiment(directory.name)
        cls.rows = summarize.load_rows(out)

    def summary(self, rows):
        return summarize.summarize_rows(rows, expected_rounds=1, expected_case_ids=EXPERIMENT_CASES)

    def rows_copy(self):
        return copy.deepcopy(self.rows)

    def test_a_false_rejection_requires_a_stop(self):
        rows = self.rows_copy()
        enforce_005 = next(row for row in rows if row["mode"] == "enforce" and row["case_id"] == "s6-dev-005")
        enforce_005["grounding_decisions"][0]["attribution"] = ATTRIBUTION_FALSE
        summary = self.summary(rows)
        self.assertIn("false_rejection", summary["stop_reasons"])
        self.assertEqual(len(summary["false_rejections"]), 1)

    def test_a_failed_hard_invariant_requires_a_stop(self):
        rows = self.rows_copy()
        rows[0]["score"]["hard_invariants"]["no_unauthorized_write"] = False
        self.assertIn("hard_invariant_failure", self.summary(rows)["stop_reasons"])

    def test_an_enforced_ungrounded_admission_requires_a_stop(self):
        rows = self.rows_copy()
        shadow_005 = next(row for row in rows if row["mode"] == "shadow" and row["case_id"] == "s6-dev-005")
        forged = copy.deepcopy(shadow_005)
        forged["mode"] = "enforce"
        for item in forged["grounding_decisions"]:
            item["mode"] = "enforce"
        rows = [row for row in rows if not (row["mode"] == "enforce" and row["case_id"] == "s6-dev-005")] + [forged]
        summary = self.summary(rows)
        self.assertIn({"case_id": "s6-dev-005", "mode": "enforce", "round": 1,
                       "code": "enforce_admitted_ungrounded_action"}, summary["integrity_problems"])

    def test_a_provider_failure_is_listed_without_a_stop(self):
        rows = self.rows_copy()
        rows[0].update(score=None, run=None, grounding_decisions=[],
                       provider_failure={"phase": "run", "error_type": "ConnectionError"})
        summary = self.summary(rows)
        self.assertFalse(summary["stop_required"])
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["provider_failures"][0]["error_type"], "ConnectionError")

    def test_a_missing_or_duplicate_row_is_never_a_success(self):
        rows = self.rows_copy()
        summary = self.summary(rows[1:] + [rows[1]])
        self.assertFalse(summary["complete"])
        self.assertIn("duplicate_row", [item["code"] for item in summary["integrity_problems"]])


class PreflightTests(unittest.TestCase):
    def fake_git(self, **overrides):
        answers = {"status": "", "rev-parse HEAD": "f" * 40, "merge-base": "", "diff": "",
                   "worktree": "worktree " + str(ROOT), "rev-parse --git-common-dir": ".git",
                   "ls-files": "eval/v2/stage6-dev.json"}
        answers.update(overrides)

        def git(root, *args):
            key = " ".join(args[:2]) if args[0] == "rev-parse" else args[0]
            answer = answers[key]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return git

    def temporary(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def preflight(self, out=None, **overrides):
        out = out or self.temporary() / "out"
        with mock.patch.object(run_m1_a2, "git", self.fake_git(**overrides)):
            return run_m1_a2.preflight(out, root=ROOT)

    def test_the_pinned_dev_and_a_clean_tree_pass(self):
        cases, context = self.preflight()
        self.assertEqual([case["case_id"] for case in cases], list(run_m1_a2.DEV_CASE_IDS))
        self.assertEqual(context["commit"], "f" * 40)
        self.assertEqual(context["dataset_sha256"], DEV_SHA256)
        self.assertEqual(context["source_sha256"], {"eval/v2/stage6-dev.json": DEV_SHA256})

    def test_every_precondition_refuses(self):
        refusals = {
            "dirty": dict(status="?? start_demo.bat"),
            "no M1-A1": {"merge-base": subprocess.CalledProcessError(1, "git")},
            "frozen diff": dict(diff="eval_v2/stage6_runner.py"),
        }
        for name, overrides in refusals.items():
            with self.subTest(name), self.assertRaises(run_m1_a2.PreflightError):
                self.preflight(**overrides)
        with self.subTest("inside a checkout"), self.assertRaises(run_m1_a2.PreflightError):
            self.preflight(out=ROOT / "results")
        busy = self.temporary()
        (busy / "x").write_text("x", encoding="utf-8")
        with self.subTest("not empty"), self.assertRaises(run_m1_a2.PreflightError):
            self.preflight(out=busy)
        with self.subTest("dataset bytes"), mock.patch.object(run_m1_a2, "DEV_SHA256", "0" * 64), \
                self.assertRaises(run_m1_a2.PreflightError):
            self.preflight()

    def test_the_formal_provider_is_the_existing_configuration(self):
        from llm_provider import LLMConfig

        formal = LLMConfig(provider="deepseek", base_url="https://api.deepseek.com", model="deepseek-flash",
                           api_key="unit-test-key", timeout=180.0)
        with mock.patch("llm_provider.load_config", return_value=formal) as load, \
                mock.patch("llm_provider.create_provider", return_value="provider") as create:
            provider, public = run_m1_a2.formal_provider()
        load.assert_called_once_with("deepseek")
        create.assert_called_once_with(formal)
        self.assertEqual(provider, "provider")
        self.assertNotIn("unit-test-key", json.dumps(public))
        for change in (dict(model="deepseek-chat"), dict(timeout=60.0), dict(api_key=None)):
            with self.subTest(change=change), \
                    mock.patch("llm_provider.load_config", return_value=dataclasses.replace(formal, **change)), \
                    self.assertRaises(run_m1_a2.PreflightError):
                run_m1_a2.formal_provider()

    def test_the_script_names_no_key(self):
        for path in sorted((ROOT / "eval_m1").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertNotIn("API_KEY", text)
                self.assertNotIn("api_key=", text)
                self.assertNotIn("os.environ", text)


class FrozenBoundaryTests(unittest.TestCase):
    """08: zero diff from main under the frozen and product directories; architecture unchanged."""

    def git(self, *args):
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.decode("utf-8").strip()

    def test_08_frozen_and_product_directories_are_unchanged(self):
        paths = ("eval_v2/", "aftersales/", "eval/v2/", "aftersales_service/")
        self.assertEqual(self.git("diff", "--name-only", "main", "--", *paths), "")
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard", "--", *paths), "")

    def test_08_the_product_architecture_test_is_unchanged_and_passes(self):
        self.assertEqual(self.git("diff", "--name-only", "main", "--", "tests/test_aftersales_service.py"), "")
        module = importlib.import_module("tests.test_aftersales_service")
        test = module.ProductBoundaryTests("test_only_agent_core_imports_eval_v2_and_only_the_reused_modules")
        result = unittest.TestResult()
        test.run(result)
        self.assertEqual((result.testsRun, result.errors, result.failures), (1, [], []))

    def imports(self, path: Path) -> set[str]:
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.add(node.module or "")
        return names

    def test_no_existing_module_imports_eval_m1(self):
        files = self.git("ls-files", "--cached", "--others", "--exclude-standard", "*.py").splitlines()
        for name in files:
            if name.startswith("eval_m1/") or name == "tests/test_m1_a2_grounding_eval.py":
                continue
            with self.subTest(file=name):
                self.assertFalse(any(module.split(".")[0] == "eval_m1" for module in self.imports(ROOT / name)))

    def test_eval_m1_reuses_only_the_two_grounding_modules_of_the_product(self):
        allowed = {"aftersales_service.action_grounding", "aftersales_service.observation_provenance"}
        for path in sorted((ROOT / "eval_m1").glob("*.py")):
            product = {name for name in self.imports(path) if name.split(".")[0] == "aftersales_service"}
            with self.subTest(path.name):
                self.assertLessEqual(product, allowed)


if __name__ == "__main__":
    unittest.main()
