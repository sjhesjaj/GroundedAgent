"""Stage 4.4.2: the deterministic control-layer case runner.

Synthetic cases and the scripted test policy live in this file only. No
dataset, no Planner, no LLM, no scorer, no derived-evidence engine; the sealed
holdout is never read.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import json
import re
import unittest
import uuid
from pathlib import Path
from unittest import mock

import eval_v2
from eval_v2 import control, runner
from eval_v2 import faults as fg
from eval_v2.control import (
    Clarify,
    ControlPolicy,
    ControlPolicyContractError,
    ControlState,
    Finish,
    ToolCall,
    ToolContractFailure,
    ToolObservation,
    UserMessage,
    clarification_slots,
    finish_dispositions,
    require_action,
)
from eval_v2.faults import FaultConfigurationError, FaultInjectingGateway
from eval_v2.runner import (
    CONTROL_RUN_SCHEMA,
    HARD_MAX_STEPS,
    CaseRunRecord,
    ClarificationRecord,
    control_run_sha256,
    observation_id_for,
    text_sha256,
)
from eval_v2.runtime import (
    EXPECTED_TOOL_NAMES,
    DatabaseChanged,
    EvalRuntimeError,
    V2CaseRuntime,
    execute_observation,
)
from orchestration.contracts import ToolResult, ToolStatus

from tests.test_v2_clock import clock_violations
from tests.test_v2_eval_runtime import base_case, inventory_row

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {name: ROOT / "eval_v2" / (name + ".py") for name in ("control", "runner")}
SPEC = ROOT / "eval" / "v2" / "spec"

CASE_ID = "runtime-fixture-1"
SECRET_CASE_ID = "A01-SECRET-EVAL-IDENTITY"
CUSTOMER = "CUST-001"
ORDER = "ORD-1001"
SKU = "SKU-TSHIRT-M"
SENTINEL = "SECRET-USER-TURN-SENTINEL"
# Independently authored evaluation sets (eval/v2), pinned by raw-byte SHA-256.
EXPECTED_DATASET_SHA256 = {
    "dev.json": "dc8e00405afb9ef9e0dd2f14f1fc5b91b1a5f5dd45812fdcb4a798e97a93f5ab",
    "validation.json": "50a0398d8e9f42afa3356cb89179b1b46c0fd00206a58046884ff75776575d2c",
}
LABEL_KEYS = ("expected_capabilities", "expected_evidence", "expected_answerability",
              "expected_action", "expected_final_state", "archetype")


def get_order(order_id=ORDER):
    return ToolCall(tool_name="get_order", arguments={"order_id": order_id})


def get_logistics(order_id=ORDER):
    return ToolCall(tool_name="get_logistics", arguments={"order_id": order_id})


def get_inventory(sku=SKU):
    return ToolCall(tool_name="get_inventory", arguments={"sku": sku})


def fault(tool, mode, on_call=1, **match):
    return {"tool": tool, "match": dict(match), "mode": mode, "on_call": on_call}


def make_case(first="turn one", conditional=(), faults=()):
    """A schema-valid synthetic case. `conditional` is ((slots, text), ...)."""
    case = base_case(faults=list(faults))
    case["user_turns"] = [{"text": first}] + [
        {"on_clarify": list(slots), "text": text} for slots, text in conditional]
    if conditional:
        case["expected_answerability"]["clarify"] = {
            "required": True, "slots": [conditional[0][0][0]]}
    return case


class ScriptedPolicy:
    """Test double: plays a fixed script, one action per decision.

    A script entry is an action, or a callable(state) -> action. Every state
    it receives is kept, so tests can assert what the policy could see.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.states: list[ControlState] = []

    def next_action(self, state):
        self.states.append(state)
        if not self.script:
            raise AssertionError("policy asked for more decisions than scripted")
        entry = self.script.pop(0)
        return entry(state) if callable(entry) else entry


class RepeatingPolicy:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def next_action(self, state):
        self.calls += 1
        return self.action


class PolicyBoom(Exception):
    pass


def run(case, policy, max_steps=8):
    # Looked up at call time, so the mutation guards can swap run_case.
    return runner.run_case(case, policy, max_steps=max_steps)


def tool_observations(record):
    return [o for o in record.observations if type(o) is ToolObservation]


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


class RunnerTestCase(unittest.TestCase):
    pass


# --------------------------------------------------------------------------
# Action contract
# --------------------------------------------------------------------------


class ActionContractTests(RunnerTestCase):
    def test_vocabularies_are_the_frozen_spec(self):
        slots = [entry["slot"] for entry in
                 json.loads((SPEC / "slots.json").read_text(encoding="utf-8"))["slots"]]
        schema = json.loads((SPEC / "case.schema.json").read_text(encoding="utf-8"))
        finals = schema["properties"]["expected_answerability"]["properties"]["final"]["enum"]
        self.assertEqual(clarification_slots(), tuple(slots))
        self.assertEqual(clarification_slots(),
                         ("order_id", "order_item", "target_sku", "reason"))
        self.assertEqual(finish_dispositions(), tuple(finals))
        self.assertEqual(finish_dispositions(), ("answer", "refuse", "handoff", "boundary"))

    def test_every_frozen_disposition_finishes(self):
        for disposition in ("answer", "refuse", "handoff", "boundary"):
            with self.subTest(disposition=disposition):
                record = run(make_case(), ScriptedPolicy(Finish(disposition=disposition)))
                self.assertEqual(record.termination, "finished")
                self.assertEqual(record.final_disposition, disposition)
                self.assertEqual(record.control_steps, 1)

    def test_unknown_disposition_is_rejected(self):
        for value in ("maybe", "Answer", "", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(ControlPolicyContractError):
                    Finish(disposition=value)

    def test_forced_unknown_disposition_is_rejected_by_the_runner(self):
        finish = Finish(disposition="answer")
        object.__setattr__(finish, "disposition", "maybe")
        with self.assertRaises(ControlPolicyContractError):
            run(make_case(), ScriptedPolicy(finish))

    def test_clarify_slots_contract(self):
        self.assertEqual(Clarify(slots=("order_id", "reason")).slots, ("order_id", "reason"))
        for slots in ((), ["order_id"], ("order_id", "order_id"), ("colour",),
                      ("reason", "order_id"), (1,), "order_id"):
            with self.subTest(slots=slots):
                with self.assertRaises(ControlPolicyContractError):
                    Clarify(slots=slots)

    def test_tool_call_arguments_are_a_private_read_only_copy(self):
        arguments = {"order_id": ORDER}
        call = ToolCall(tool_name="get_order", arguments=arguments)
        arguments["order_id"] = "ORD-9999"
        self.assertEqual(call.arguments["order_id"], ORDER)
        with self.assertRaises(TypeError):
            call.arguments["order_id"] = "ORD-9999"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            call.tool_name = "get_logistics"
        for bad in (dict(tool_name="", arguments={}), dict(tool_name=None, arguments={}),
                    dict(tool_name="get_order", arguments=[("order_id", ORDER)])):
            with self.subTest(bad=bad):
                with self.assertRaises(ControlPolicyContractError):
                    ToolCall(**bad)

    def test_subclassed_action_is_rejected(self):
        class LooseFinish(Finish):
            def check(self):
                pass
        with self.assertRaises(ControlPolicyContractError):
            require_action(LooseFinish(disposition="answer"))


# --------------------------------------------------------------------------
# Basic control loop
# --------------------------------------------------------------------------


class BasicRunTests(RunnerTestCase):
    def test_tool_then_finish(self):
        policy = ScriptedPolicy(get_order(), Finish(disposition="answer"))
        record = run(make_case(), policy)
        self.assertEqual(record.termination, "finished")
        self.assertEqual(record.final_disposition, "answer")
        self.assertEqual(record.control_steps, 2)
        self.assertEqual(len(record.observations), 1)
        self.assertEqual(len(record.fault_records), 1)
        self.assertTrue(record.database_unchanged)
        self.assertEqual(record.initial_db_sha256, record.final_db_sha256)
        second = policy.states[1]
        self.assertEqual(len(second.observations), 1)
        observation = second.observations[0]
        self.assertIsInstance(observation, ToolObservation)
        self.assertIsInstance(observation.result, ToolResult)
        self.assertIs(observation.result.status, ToolStatus.OK)
        self.assertEqual(observation.tool_name, "get_order")
        self.assertEqual(dict(observation.arguments), {"order_id": ORDER})

    def test_observation_feeds_the_next_tool_arguments(self):
        # Not a Baseline: only proof the runner supports observe -> next tool.
        overlay = {"inventory": {"SKU-RUNNER-PROBE": {"op": "insert", "row": inventory_row(7)}},
                   "order_items": {"OI-1001-1": {"op": "update",
                                                 "set": {"sku": "SKU-RUNNER-PROBE"}}}}
        case = make_case()
        case["initial_state"].update(overlay)

        def inventory_of_first_item(state):
            items = [item for item in state.observations[-1].result.evidence
                     if item.metadata["entity"] == "order_item"
                     and item.metadata["field"] == "sku"]
            first = min(items, key=lambda item: item.metadata["record_id"])
            return get_inventory(first.metadata["value"])

        policy = ScriptedPolicy(get_order(), inventory_of_first_item, Finish(disposition="answer"))
        record = run(case, policy)
        self.assertEqual(record.termination, "finished")
        calls = tool_observations(record)
        self.assertEqual([o.tool_name for o in calls], ["get_order", "get_inventory"])
        self.assertEqual(dict(calls[1].arguments), {"sku": "SKU-RUNNER-PROBE"})
        self.assertEqual(calls[1].result.evidence[0].metadata["value"], 7)
        self.assertEqual(len(policy.states[2].observations), 2)

    def test_policy_must_implement_the_protocol(self):
        self.assertIsInstance(ScriptedPolicy(), ControlPolicy)
        with self.assertRaises(ControlPolicyContractError):
            run(make_case(), object())

    def test_max_steps_must_be_explicit_and_bounded(self):
        with self.assertRaises(TypeError):
            runner.run_case(make_case(), ScriptedPolicy())  # no max_steps
        for bad in (0, -1, HARD_MAX_STEPS + 1, True, 3.0, "3", None):
            with self.subTest(max_steps=bad):
                with self.assertRaises(ValueError):
                    runner.run_case(make_case(), ScriptedPolicy(), max_steps=bad)
        record = run(make_case(), ScriptedPolicy(Finish(disposition="answer")),
                     max_steps=HARD_MAX_STEPS)
        self.assertEqual(record.max_steps, HARD_MAX_STEPS)


# --------------------------------------------------------------------------
# What the policy can see
# --------------------------------------------------------------------------


STATE_FIELDS = ("virtual_now", "persona_id", "allowed_tools", "max_steps",
                "step_number", "remaining_steps", "user_messages", "observations")


class ControlStateTests(RunnerTestCase):
    def first_state(self, case=None):
        policy = ScriptedPolicy(Finish(disposition="answer"))
        run(make_case() if case is None else case, policy, max_steps=5)
        return policy.states[0]

    def test_state_has_exactly_the_visible_fields(self):
        state = self.first_state()
        self.assertEqual(tuple(f.name for f in dataclasses.fields(ControlState)), STATE_FIELDS)
        self.assertFalse(hasattr(state, "case_id"))
        self.assertEqual(state.virtual_now, "2026-11-15T10:00:00+08:00")
        self.assertEqual(state.persona_id, "demo-a")
        self.assertEqual(state.allowed_tools, EXPECTED_TOOL_NAMES)
        self.assertEqual((state.max_steps, state.step_number, state.remaining_steps), (5, 1, 5))
        self.assertEqual(state.user_messages, (UserMessage(turn_index=1, text="turn one"),))
        self.assertEqual(state.observations, ())

    def test_no_label_fixture_fault_or_identity_backdoor(self):
        state = self.first_state(make_case(faults=[fault("get_order", "error")]))
        for name in LABEL_KEYS + ("case_id", "faults", "initial_state", "case", "customer_id",
                                  "runtime",
                                  "registry", "context", "connection", "gateway", "user_turns",
                                  "__dict__"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(state, name))
        self.assertNotIn(CUSTOMER, repr(state))

    def test_state_is_immutable(self):
        state = self.first_state()
        self.assertIsInstance(state.allowed_tools, tuple)
        self.assertIsInstance(state.user_messages, tuple)
        self.assertIsInstance(state.observations, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.user_messages = ()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.user_messages[0].text = "edited"

    def test_allowed_tools_are_not_narrowed_by_expected_capabilities(self):
        case = make_case()
        case["expected_capabilities"] = {"required": ["get_order"], "forbidden": ["get_inventory"]}
        self.assertEqual(self.first_state(case).allowed_tools, EXPECTED_TOOL_NAMES)

    def test_first_state_sees_only_turn_zero(self):
        case = make_case(conditional=((("order_id",), "FUTURE-ANSWER-ORD-1001"),))
        state = self.first_state(case)
        self.assertEqual(len(state.user_messages), 1)
        self.assertEqual(state.user_messages[0].text, "turn one")
        self.assertEqual(state.observations, ())
        self.assertNotIn("FUTURE-ANSWER", repr(state))

    def test_prior_state_is_not_changed_by_later_steps(self):
        policy = ScriptedPolicy(get_order(), get_logistics(), Finish(disposition="answer"))
        run(make_case(), policy)
        self.assertEqual([len(s.observations) for s in policy.states], [0, 1, 2])
        self.assertEqual([s.step_number for s in policy.states], [1, 2, 3])
        self.assertEqual([s.remaining_steps for s in policy.states], [8, 7, 6])

    def test_modifying_an_observed_result_is_caught(self):
        def tamper(state):
            state.observations[0].result.trace["observation_id"] = "forged"
            return Finish(disposition="answer")
        with self.assertRaises(ControlPolicyContractError):
            run(make_case(), ScriptedPolicy(get_order(), tamper))


# --------------------------------------------------------------------------
# Clarification
# --------------------------------------------------------------------------


class ClarificationTests(RunnerTestCase):
    def test_clarification_delivers_the_matching_turn(self):
        case = make_case(first="我想查这个订单", conditional=((("order_id",), ORDER),))
        policy = ScriptedPolicy(Clarify(slots=("order_id",)), get_order(),
                                Finish(disposition="answer"))
        record = run(case, policy)
        self.assertEqual(record.termination, "finished")
        self.assertEqual(policy.states[1].user_messages, (
            UserMessage(turn_index=1, text="我想查这个订单"),
            UserMessage(turn_index=2, text=ORDER)))
        self.assertEqual(record.clarifications, (ClarificationRecord(
            sequence=1, control_step=1, requested_slots=("order_id",),
            matched_turn_index=1, delivered_turn_index=2),))
        self.assertEqual([(m.turn_index, m.case_turn_index) for m in record.user_messages],
                         [(1, 0), (2, 1)])
        self.assertEqual(record.observations[0].turn_index, 2)

    def test_requested_slots_must_be_a_subset(self):
        case = make_case(conditional=((("order_id", "reason"), "ORD-1001 尺码不合适"),))
        record = run(case, ScriptedPolicy(Clarify(slots=("order_id",)),
                                          Finish(disposition="answer")))
        self.assertEqual(record.clarifications[0].matched_turn_index, 1)

        case = make_case(conditional=((("order_id",), ORDER),))
        record = run(case, ScriptedPolicy(Clarify(slots=("order_id", "reason"))))
        self.assertEqual(record.termination, "unanswered_clarification")
        self.assertIsNone(record.final_disposition)
        self.assertIsNone(record.clarifications[0].matched_turn_index)
        self.assertEqual(len(record.user_messages), 1)

    def test_first_matching_turn_in_case_order(self):
        case = make_case(conditional=((("reason",), "尺码不合适"),
                                      (("order_id",), "first order answer"),
                                      (("order_id", "reason"), "second order answer")))
        policy = ScriptedPolicy(Clarify(slots=("order_id",)), Clarify(slots=("order_id",)),
                                Finish(disposition="answer"))
        record = run(case, policy)
        self.assertEqual([c.matched_turn_index for c in record.clarifications], [2, 3])
        self.assertEqual([m.text for m in policy.states[2].user_messages],
                         ["turn one", "first order answer", "second order answer"])
        # The unmatched reason turn never became visible, not even as a gap.
        self.assertEqual([m.turn_index for m in policy.states[2].user_messages], [1, 2, 3])

    def test_conditional_turn_is_consumed_once(self):
        case = make_case(conditional=((("order_id",), ORDER),))
        policy = ScriptedPolicy(Clarify(slots=("order_id",)), Clarify(slots=("order_id",)))
        record = run(case, policy)
        self.assertEqual(record.termination, "unanswered_clarification")
        self.assertEqual(record.control_steps, 2)
        self.assertEqual([c.matched_turn_index for c in record.clarifications], [1, None])
        self.assertEqual(len(record.user_messages), 2)

    def test_over_ask_is_recorded_not_judged(self):
        record = run(make_case(), ScriptedPolicy(Clarify(slots=("order_id",))))
        self.assertEqual(record.termination, "unanswered_clarification")
        self.assertIsNone(record.final_disposition)
        self.assertEqual(record.clarifications, (ClarificationRecord(
            sequence=1, control_step=1, requested_slots=("order_id",),
            matched_turn_index=None, delivered_turn_index=None),))

    def test_clarification_is_structural_not_textual(self):
        # The answer text mentions a slot name; only on_clarify decides.
        case = make_case(conditional=((("reason",), "my order_id is ORD-1001"),))
        record = run(case, ScriptedPolicy(Clarify(slots=("order_id",))))
        self.assertEqual(record.termination, "unanswered_clarification")


# --------------------------------------------------------------------------
# Faults fed back to the policy
# --------------------------------------------------------------------------


class FaultFeedbackTests(RunnerTestCase):
    def test_error_result_is_fed_back_to_policy(self):
        for mode, code in (("error", "tool_error"), ("timeout", "tool_timeout")):
            with self.subTest(mode=mode):
                def refuse_on_error(state):
                    result = state.observations[-1].result
                    self.assertIs(result.status, ToolStatus.ERROR)
                    self.assertEqual(result.error_code, code)
                    return Finish(disposition="refuse")
                policy = ScriptedPolicy(get_order(), refuse_on_error)
                record = run(make_case(faults=[fault("get_order", mode)]), policy)
                self.assertEqual(len(policy.states), 2)
                self.assertEqual(record.termination, "finished")
                self.assertEqual(record.final_disposition, "refuse")
                self.assertEqual(record.fault_records[0].outcome, "injected_" + mode)
                self.assertEqual(record.control_steps, 2)

    def test_malformed_becomes_a_contract_failure(self):
        def refuse_on_failure(state):
            failure = state.observations[-1]
            self.assertIs(type(failure), ToolContractFailure)
            self.assertEqual(failure.kind, "malformed")
            self.assertFalse(hasattr(failure, "result"))
            return Finish(disposition="refuse")
        policy = ScriptedPolicy(get_order(), refuse_on_failure)
        record = run(make_case(faults=[fault("get_order", "malformed")]), policy)
        self.assertEqual(record.termination, "finished")
        self.assertEqual(record.final_disposition, "refuse")
        self.assertEqual(tool_observations(record), [])
        failure = record.observations[0]
        self.assertEqual(failure.observation_id, record.fault_records[0].observation_id)
        self.assertEqual(record.fault_records[0].outcome, "injected_malformed")
        serialized = failure.to_dict()
        self.assertEqual(serialized["type"], "tool_contract_failure")
        self.assertNotIn("result", serialized)
        self.assertNotIn("ToolResult", json.dumps(serialized))

    def test_calls_after_a_malformed_one_continue(self):
        policy = ScriptedPolicy(get_order(), get_order(), Finish(disposition="answer"))
        record = run(make_case(faults=[fault("get_order", "malformed")]), policy)
        self.assertEqual([type(o).__name__ for o in record.observations],
                         ["ToolContractFailure", "ToolObservation"])
        self.assertEqual([r.outcome for r in record.fault_records],
                         ["injected_malformed", "delegated"])

    def test_plain_value_error_is_not_malformed(self):
        def broken_execute(self, tool_name, arguments, *, observation_id):
            raise ValueError("contract bug")
        with mock.patch.object(FaultInjectingGateway, "execute", broken_execute):
            with self.assertRaisesRegex(ValueError, "contract bug"):
                run(make_case(), ScriptedPolicy(get_order()))

    def test_value_error_after_an_earlier_malformed_record_is_not_malformed(self):
        real = FaultInjectingGateway.execute
        calls = []

        def second_call_breaks(self, tool_name, arguments, *, observation_id):
            calls.append(observation_id)
            if len(calls) == 2:
                raise ValueError("contract bug")
            return real(self, tool_name, arguments, observation_id=observation_id)
        with mock.patch.object(FaultInjectingGateway, "execute", second_call_breaks):
            with self.assertRaisesRegex(ValueError, "contract bug"):
                run(make_case(faults=[fault("get_order", "malformed")]),
                    ScriptedPolicy(get_order(), get_order()))

    def test_unknown_tool_and_bad_arguments_raise(self):
        for action in (ToolCall(tool_name="refund_order", arguments={"order_id": ORDER}),
                       ToolCall(tool_name="get_order", arguments={"order": ORDER}),
                       ToolCall(tool_name="get_order", arguments={"order_id": 1001}),
                       ToolCall(tool_name="get_order",
                                arguments={"order_id": ORDER, "customer_id": CUSTOMER})):
            with self.subTest(action=action.tool_name + str(dict(action.arguments))):
                with self.assertRaises(ValueError):
                    run(make_case(), ScriptedPolicy(action))

    def test_fault_configuration_error_propagates(self):
        faults = [fault("get_order", "error"), fault("get_order", "timeout", order_id=ORDER)]
        with self.assertRaises(FaultConfigurationError):
            run(make_case(faults=faults), ScriptedPolicy(get_order()))

    def test_runner_never_sees_fault_declarations(self):
        policy = ScriptedPolicy(get_order(), Finish(disposition="refuse"))
        run(make_case(faults=[fault("get_order", "error", order_id=ORDER)]), policy)
        text = repr(policy.states)
        for word in ("on_call", "injected", "fault"):
            self.assertNotIn(word, text)


# --------------------------------------------------------------------------
# One gateway per case-run
# --------------------------------------------------------------------------


class OneGatewayTests(RunnerTestCase):
    def test_fault_free_case_still_goes_through_the_gateway(self):
        created = []

        class Counting(FaultInjectingGateway):
            def __init__(self, runtime):
                super().__init__(runtime)
                created.append(self)

        policy = ScriptedPolicy(get_order(), get_logistics(), get_inventory(),
                                Finish(disposition="answer"))
        with mock.patch.object(runner, "FaultInjectingGateway", Counting), \
                mock.patch.object(fg, "execute_tool", wraps=fg.execute_tool) as executed, \
                mock.patch("eval_v2.runtime.execute_tool",
                           side_effect=AssertionError("direct path used")):
            record = run(make_case(), policy)
        self.assertEqual(len(created), 1)
        self.assertEqual(executed.call_count, 3)
        self.assertEqual(created[0].records, record.fault_records)
        self.assertEqual([r.outcome for r in record.fault_records], ["delegated"] * 3)
        self.assertEqual([r.observation_id for r in record.fault_records],
                         [o.observation_id for o in record.observations])

    def test_every_run_gets_its_own_gateway(self):
        created = []

        class Counting(FaultInjectingGateway):
            def __init__(self, runtime):
                super().__init__(runtime)
                created.append(runtime)

        with mock.patch.object(runner, "FaultInjectingGateway", Counting):
            for _ in range(2):
                run(make_case(faults=[fault("get_order", "error", on_call=2)]),
                    ScriptedPolicy(get_order(), Finish(disposition="answer")))
        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])

    def test_fault_counters_span_the_whole_case_run(self):
        policy = ScriptedPolicy(get_order(), get_logistics(), get_order(),
                                Finish(disposition="refuse"))
        record = run(make_case(faults=[fault("get_order", "error", on_call=2)]), policy)
        self.assertEqual([r.outcome for r in record.fault_records],
                         ["delegated", "delegated", "injected_error"])


# --------------------------------------------------------------------------
# Observation ids
# --------------------------------------------------------------------------


class ObservationIdTests(RunnerTestCase):
    def ids(self):
        case = make_case(conditional=((("order_id",), "the second message " + ORDER),))
        policy = ScriptedPolicy(get_order(), get_inventory(), Clarify(slots=("order_id",)),
                                get_logistics(), Finish(disposition="answer"))
        record = run(case, policy)
        return [o.observation_id for o in record.observations], record

    def test_ids_are_structural_and_stable(self):
        first, record = self.ids()
        self.assertEqual(first, ["turn:1:tool:1", "turn:1:tool:2", "turn:2:tool:3"])
        self.assertEqual(self.ids()[0], first)
        self.assertEqual(observation_id_for(2, 3), first[2])
        for observation_id in first:
            self.assertRegex(observation_id, r"^turn:[1-9][0-9]*:tool:[1-9][0-9]*$")
            self.assertIsNone(re.search(r"A[0-9]{2}", observation_id))  # no archetype-like id
            for secret in (CASE_ID, "runtime-fixture", "case", ORDER, SKU, "SKU-MUG", CUSTOMER,
                           "demo-a", "turn one", "second message", "get_"):
                self.assertNotIn(secret, observation_id)
        for observation in record.observations:
            self.assertEqual(observation.result.trace["observation_id"],
                             observation.observation_id)
            for item in observation.result.evidence:
                self.assertEqual(item.metadata["observation_id"], observation.observation_id)



# --------------------------------------------------------------------------
# Eval case identity never reaches the policy
# --------------------------------------------------------------------------


def with_case_id(case, case_id):
    case = copy.deepcopy(case)
    case["case_id"] = case_id
    return case


def policy_view(state):
    """Everything a policy can read from one state, as plain data - read from the
    live objects it holds (ToolResult, trace, every evidence item), not from the
    run record's snapshot."""
    view = {name: list(value) if isinstance(value, tuple) else value
            for name, value in ((f.name, getattr(state, f.name))
                                for f in dataclasses.fields(ControlState))
            if name not in ("user_messages", "observations")}
    view["user_messages"] = [[m.turn_index, m.text] for m in state.user_messages]
    observations = []
    for observation in state.observations:
        entry = {f.name: getattr(observation, f.name)
                 for f in dataclasses.fields(observation)
                 if f.name not in ("arguments", "result", "_result_dict")}
        entry["type"] = type(observation).__name__
        entry["arguments"] = dict(observation.arguments)
        if type(observation) is ToolObservation:
            entry["result"] = observation.result.to_dict()
            entry["trace"] = dict(observation.result.trace)
            entry["evidence"] = [item.to_dict() for item in observation.result.evidence]
        observations.append(entry)
    view["observations"] = observations
    return view


def identity_script():
    return (get_order(), Clarify(slots=("order_id",)), get_logistics(), get_inventory(),
            Finish(disposition="refuse"))


def identity_case(case_id):
    return with_case_id(make_case(conditional=((("order_id",), ORDER),),
                                  faults=[fault("get_logistics", "malformed"),
                                          fault("get_inventory", "error")]), case_id)


class CaseIdentityTests(RunnerTestCase):
    def test_case_id_never_reaches_the_policy(self):
        policy = ScriptedPolicy(*identity_script())
        record = run(identity_case(SECRET_CASE_ID), policy)
        self.assertEqual(record.termination, "finished")
        first = policy.states[0]
        self.assertFalse(hasattr(first, "case_id"))
        self.assertNotIn(SECRET_CASE_ID, repr(first))
        # The last state holds every observation kind: ok, contract failure, error.
        self.assertEqual([type(o).__name__ for o in policy.states[-1].observations],
                         ["ToolObservation", "ToolContractFailure", "ToolObservation"])
        self.assertIs(policy.states[-1].observations[-1].result.status, ToolStatus.ERROR)
        for state in policy.states:
            self.assertFalse(hasattr(state, "case_id"))
            for text in (repr(state), json.dumps(policy_view(state), ensure_ascii=False)):
                for secret in (SECRET_CASE_ID, "SECRET-EVAL", "A01"):
                    self.assertNotIn(secret, text)
            for observation in state.observations:
                self.assertNotIn(SECRET_CASE_ID, observation.observation_id)
                if type(observation) is ToolObservation:
                    self.assertNotIn(SECRET_CASE_ID, repr(observation.result.trace))
                    for item in observation.result.evidence:
                        self.assertNotIn(SECRET_CASE_ID,
                                         json.dumps(item.to_dict(), ensure_ascii=False))
        # The raw record keeps the case identity.
        self.assertEqual(record.case_id, SECRET_CASE_ID)
        self.assertEqual(record.to_dict()["case_id"], SECRET_CASE_ID)

    def test_policy_visible_states_do_not_depend_on_case_id(self):
        plain, secret = ScriptedPolicy(*identity_script()), ScriptedPolicy(*identity_script())
        plain_record = run(identity_case(CASE_ID), plain)
        secret_record = run(identity_case(SECRET_CASE_ID), secret)
        self.assertEqual(len(plain.states), 5)
        self.assertEqual([policy_view(s) for s in plain.states],
                         [policy_view(s) for s in secret.states])
        self.assertEqual(plain.states, secret.states)
        # Only the raw record's own identity differs, and it must stay.
        plain_dict, secret_dict = plain_record.to_dict(), secret_record.to_dict()
        self.assertEqual((plain_dict.pop("case_id"), secret_dict.pop("case_id")),
                         (CASE_ID, SECRET_CASE_ID))
        self.assertEqual(plain_dict, secret_dict)
        self.assertNotEqual(control_run_sha256(plain_record), control_run_sha256(secret_record))


# --------------------------------------------------------------------------
# Max steps
# --------------------------------------------------------------------------


class MaxStepsTests(RunnerTestCase):
    def test_max_steps_stops_exactly(self):
        policy = RepeatingPolicy(get_order())
        record = run(make_case(), policy, max_steps=3)
        self.assertEqual(policy.calls, 3)
        self.assertEqual(record.termination, "max_steps_exceeded")
        self.assertIsNone(record.final_disposition)
        self.assertEqual(record.control_steps, 3)
        self.assertEqual(len(record.observations), 3)

    def test_finish_on_the_last_step_is_finished(self):
        policy = ScriptedPolicy(get_order(), get_order(), Finish(disposition="answer"))
        record = run(make_case(), policy, max_steps=3)
        self.assertEqual(record.termination, "finished")

    def test_answered_clarification_on_the_last_step_runs_out(self):
        case = make_case(conditional=((("order_id",), ORDER),))
        record = run(case, ScriptedPolicy(Clarify(slots=("order_id",))), max_steps=1)
        self.assertEqual(record.termination, "max_steps_exceeded")
        self.assertEqual(len(record.user_messages), 2)


# --------------------------------------------------------------------------
# Policy return contract
# --------------------------------------------------------------------------


class PolicyReturnTests(RunnerTestCase):
    def test_non_actions_are_rejected(self):
        for value in (None, {"tool_name": "get_order"}, "finish", 1, object(),
                      ("get_order", {"order_id": ORDER})):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ControlPolicyContractError):
                    run(make_case(), ScriptedPolicy(lambda state, v=value: v))

    def test_policy_exception_propagates(self):
        def boom(state):
            raise PolicyBoom()
        with self.assertRaises(PolicyBoom):
            run(make_case(), ScriptedPolicy(boom))


# --------------------------------------------------------------------------
# Raw record
# --------------------------------------------------------------------------


def label_twin(case):
    """Same runtime-visible case, different scorer labels."""
    twin = json.loads(json.dumps(case))
    twin["archetype"] = "A01"
    twin["expected_capabilities"] = {"required": ["get_order"], "forbidden": ["get_inventory"]}
    twin["expected_answerability"]["final"] = "refuse"
    return twin


def full_script():
    return (get_order(), get_logistics(), Clarify(slots=("order_id",)), get_inventory(),
            Finish(disposition="answer"))


def full_case():
    return make_case(first=SENTINEL + " 我想退货",
                     conditional=((("order_id",), SENTINEL + "-CLARIFIED " + ORDER),),
                     faults=[fault("get_logistics", "timeout")])


class RawRecordTests(RunnerTestCase):
    def test_record_is_deterministic(self):
        first = run(full_case(), ScriptedPolicy(*full_script()))
        second = run(full_case(), ScriptedPolicy(*full_script()))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(control_run_sha256(first), control_run_sha256(second))
        self.assertEqual(first.sha256(), control_run_sha256(first))
        self.assertEqual(len(control_run_sha256(first)), 64)
        self.assertNotEqual(control_run_sha256(first), control_run_sha256(
            run(full_case(), ScriptedPolicy(get_order(), Finish(disposition="answer")))))

    def test_record_is_plain_json(self):
        record = run(full_case(), ScriptedPolicy(*full_script()))
        payload = record.to_dict()
        text = json.dumps(payload, ensure_ascii=False)  # no default=
        self.assertEqual(json.loads(text), payload)
        self.assertEqual(record.canonical_json(), json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        self.assertEqual(list(payload), [
            "schema", "case_id", "virtual_now", "persona_id", "allowed_tools", "max_steps",
            "termination", "final_disposition", "control_steps", "user_messages",
            "clarifications", "observations", "fault_records", "initial_db_sha256",
            "final_db_sha256", "database_unchanged"])
        self.assertEqual(payload["schema"], CONTROL_RUN_SCHEMA)
        self.assertEqual(payload["schema"], "v2-control-run/1")
        self.assertEqual(payload["termination"], "finished")
        self.assertTrue(payload["database_unchanged"])
        self.assertEqual([r["outcome"] for r in payload["fault_records"]],
                         ["delegated", "injected_timeout", "delegated"])
        first = payload["observations"][0]
        self.assertEqual(first["type"], "tool_observation")
        self.assertEqual(first["arguments"], {"order_id": ORDER})
        self.assertEqual(first["result"], record.observations[0].result.to_dict())

    def test_record_has_no_wall_clock_or_process_data(self):
        text = run(full_case(), ScriptedPolicy(*full_script())).canonical_json()
        for marker in ("duration", "elapsed", "perf_counter", "timestamp", "0x", " object at "):
            self.assertNotIn(marker, text)

    def test_record_has_no_label_keys(self):
        payload = run(full_case(), ScriptedPolicy(*full_script())).to_dict()
        keys = set(all_keys(payload))
        for key in LABEL_KEYS + ("faults", "initial_state", "customer_id", "holdout"):
            self.assertNotIn(key, keys)
        self.assertNotIn(CUSTOMER, json.dumps(payload, ensure_ascii=False))

    def test_labels_do_not_change_the_record(self):
        record = run(full_case(), ScriptedPolicy(*full_script()))
        twin = run(label_twin(full_case()), ScriptedPolicy(*full_script()))
        self.assertEqual(control_run_sha256(record), control_run_sha256(twin))

    def test_user_text_is_hashed_not_stored(self):
        case = full_case()
        policy = ScriptedPolicy(*full_script())
        record = run(case, policy)
        self.assertTrue(any(SENTINEL in m.text for m in policy.states[-1].user_messages))
        text = json.dumps(record.to_dict(), ensure_ascii=False)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("我想退货", text)
        self.assertEqual([m["text_sha256"] for m in record.to_dict()["user_messages"]],
                         [text_sha256(turn["text"]) for turn in case["user_turns"]])
        self.assertEqual(text_sha256("é"), text_sha256("é"))
        self.assertNotEqual(text_sha256("é"), text_sha256("é"))  # no normalization

    def test_record_is_immutable(self):
        record = run(make_case(), ScriptedPolicy(Finish(disposition="answer")))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.termination = "max_steps_exceeded"
        self.assertIsInstance(record.observations, tuple)
        self.assertIsInstance(record.fault_records, tuple)


# --------------------------------------------------------------------------
# Runtime lifecycle and the database invariant
# --------------------------------------------------------------------------


class LifecycleTests(RunnerTestCase):
    def counted_run(self, case, policy, *, invariant_error=None, max_steps=8):
        real_assert = V2CaseRuntime.assert_database_unchanged
        real_close = V2CaseRuntime.close
        self.events = []

        def assert_unchanged(runtime):
            self.events.append(("assert", runtime.closed))
            if invariant_error is not None:
                raise invariant_error
            return real_assert(runtime)

        def close(runtime):
            self.events.append(("close", runtime.closed))
            return real_close(runtime)

        with mock.patch.object(V2CaseRuntime, "assert_database_unchanged", assert_unchanged), \
                mock.patch.object(V2CaseRuntime, "close", close):
            return run(case, policy, max_steps=max_steps)

    def assert_invariant_then_close(self):
        self.assertEqual(self.events, [("assert", False), ("close", False)])

    def test_every_exit_path_checks_then_closes(self):
        def boom(state):
            raise PolicyBoom()

        def broken_execute(self, tool_name, arguments, *, observation_id):
            raise RuntimeError("gateway broke")

        paths = {
            "finish": (make_case(), lambda: ScriptedPolicy(get_order(),
                                                           Finish(disposition="answer")), None),
            "unanswered": (make_case(), lambda: ScriptedPolicy(Clarify(slots=("reason",))), None),
            "max_steps": (make_case(), lambda: RepeatingPolicy(get_order()), None),
            "error": (make_case(faults=[fault("get_order", "error")]),
                      lambda: ScriptedPolicy(get_order(), Finish(disposition="refuse")), None),
            "timeout": (make_case(faults=[fault("get_order", "timeout")]),
                        lambda: ScriptedPolicy(get_order(), Finish(disposition="refuse")), None),
            "malformed": (make_case(faults=[fault("get_order", "malformed")]),
                          lambda: ScriptedPolicy(get_order(), Finish(disposition="refuse")),
                          None),
            "unknown_tool": (make_case(), lambda: ScriptedPolicy(
                ToolCall(tool_name="refund_order", arguments={})), ValueError),
            "policy_raises": (make_case(), lambda: ScriptedPolicy(boom), PolicyBoom),
            "bad_return": (make_case(), lambda: ScriptedPolicy(lambda s: None),
                           ControlPolicyContractError),
            "fault_config": (make_case(faults=[fault("get_order", "error"),
                                               fault("get_order", "timeout", order_id=ORDER)]),
                             lambda: ScriptedPolicy(get_order()), FaultConfigurationError),
        }
        for name, (case, make_policy, raised) in paths.items():
            with self.subTest(path=name):
                if raised is None:
                    record = self.counted_run(case, make_policy())
                    self.assertTrue(record.database_unchanged)
                else:
                    with self.assertRaises(raised):
                        self.counted_run(case, make_policy())
                self.assert_invariant_then_close()
        with self.subTest(path="gateway_raises"):
            with mock.patch.object(FaultInjectingGateway, "execute", broken_execute):
                with self.assertRaisesRegex(RuntimeError, "gateway broke"):
                    self.counted_run(make_case(), ScriptedPolicy(get_order()))
            self.assert_invariant_then_close()
        with self.subTest(path="keyboard_interrupt"):
            def interrupt(state):
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                self.counted_run(make_case(), ScriptedPolicy(interrupt))
            self.assert_invariant_then_close()

    def test_database_change_outranks_the_primary_error(self):
        def boom(state):
            raise PolicyBoom()
        with self.assertRaises(DatabaseChanged) as caught:
            self.counted_run(make_case(), ScriptedPolicy(boom),
                             invariant_error=DatabaseChanged("changed"))
        self.assertIsInstance(caught.exception.__cause__, PolicyBoom)
        self.assert_invariant_then_close()

    def test_database_change_on_a_normal_finish_raises(self):
        with self.assertRaises(DatabaseChanged) as caught:
            self.counted_run(make_case(), ScriptedPolicy(Finish(disposition="answer")),
                             invariant_error=DatabaseChanged("changed"))
        self.assertIsNone(caught.exception.__cause__)
        self.assert_invariant_then_close()

    def test_final_hash_is_read_before_close(self):
        record = run(make_case(), ScriptedPolicy(get_order(), Finish(disposition="answer")))
        self.assertEqual(record.final_db_sha256, record.initial_db_sha256)
        runtime = V2CaseRuntime.from_case(make_case())
        self.addCleanup(runtime.close)
        self.assertEqual(record.initial_db_sha256, runtime.initial_db_sha256)

    def test_invalid_case_opens_nothing(self):
        case = make_case()
        del case["archetype"]
        with mock.patch.object(runner, "FaultInjectingGateway") as gateway:
            with self.assertRaises(EvalRuntimeError):
                run(case, ScriptedPolicy())
        gateway.assert_not_called()


# --------------------------------------------------------------------------
# Static boundaries
# --------------------------------------------------------------------------


def code_literals(tree):
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def imported_modules(tree):
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names]
    return modules


class StaticBoundaryTests(unittest.TestCase):
    def trees(self):
        return {name: (path.read_text(encoding="utf-8"),
                       ast.parse(path.read_text(encoding="utf-8")))
                for name, path in SOURCES.items()}

    def test_no_clock_randomness_or_ids(self):
        for name, (source, tree) in self.trees().items():
            with self.subTest(module=name):
                self.assertEqual(clock_violations(source), [])
                roots = {module.split(".")[0] for module in imported_modules(tree)}
                self.assertFalse(roots & {"uuid", "random", "time", "secrets", "datetime"})
                for marker in ("datetime.now", "utcnow", "date.today", "time.time",
                               "localtime", "perf_counter", "uuid", "random"):
                    self.assertNotIn(marker, "\n".join(code_literals(tree)))

    def test_no_llm_planner_or_network_imports(self):
        forbidden = {"llm_provider", "requests", "openai", "ollama", "agent", "httpx",
                     "urllib", "socket", "chat_orchestration", "diagnostic_eval", "eval_env",
                     "agent_trace", "tools"}
        for name, (_, tree) in self.trees().items():
            with self.subTest(module=name):
                modules = imported_modules(tree)
                self.assertFalse({m.split(".")[0] for m in modules} & forbidden)
                self.assertFalse([m for m in modules if m.startswith("orchestration.")
                                  and m != "orchestration.contracts"])

    def test_runner_has_one_tool_path_and_reads_no_labels(self):
        source, tree = self.trees()["runner"]
        self.assertNotIn("execute_observation", source)
        self.assertNotIn("execute_tool", source)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertNotIn("faults", attributes)
        # control.py reads the final vocabulary from the frozen *schema*'s
        # expected_answerability.final enum; that one schema key is the only
        # label name either module may spell, and never on a case.
        schema_keys = {"control": {"expected_answerability"}, "runner": set()}
        for name, (_, module_tree) in self.trees().items():
            literals = [text for text in code_literals(module_tree)
                        if text not in schema_keys[name]]
            for word in ("expected_", "archetype", "initial_state", "faults", "customer_id",
                         "holdout", "unseal", "receipt"):
                with self.subTest(module=name, word=word):
                    self.assertFalse([text for text in literals if word in text])
        _, control_tree = self.trees()["control"]
        subscripts = [node for node in ast.walk(control_tree) if isinstance(node, ast.Subscript)
                      and isinstance(node.slice, ast.Constant)
                      and node.slice.value == "expected_answerability"]
        self.assertEqual(len(subscripts), 1)
        self.assertEqual(ast.unparse(subscripts[0].value), "schema['properties']")

    def test_case_id_is_not_part_of_observation_ids(self):
        self.assertEqual(tuple(inspect.signature(observation_id_for).parameters),
                         ("turn_index", "tool_step"))
        self.assertEqual(observation_id_for(1, 1), "turn:1:tool:1")
        _, tree = self.trees()["runner"]
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "observation_id_for")
        self.assertNotIn("case", ast.unparse(function.body[-1]))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "observation_id_for"]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("case", ast.unparse(calls[0]))
        # The runner reads the case id for the raw record only.
        readers = {function.name for function in ast.walk(tree)
                   if isinstance(function, ast.FunctionDef)
                   for node in ast.walk(function)
                   if isinstance(node, ast.Attribute) and node.attr == "_case_id"}
        self.assertEqual(readers, {"__init__", "record"})
        control_source, _ = self.trees()["control"]
        self.assertNotIn("case_id", control_source)

    def test_package_exports(self):
        for name in ("ControlPolicy", "ControlState", "ToolCall", "Clarify", "Finish",
                     "ControlPolicyContractError", "CaseRunRecord", "ToolObservation",
                     "ToolContractFailure", "ClarificationRecord", "run_case",
                     "control_run_sha256", "UserMessage"):
            with self.subTest(name=name):
                self.assertIn(name, eval_v2.__all__)
                source = control if hasattr(control, name) else runner
                self.assertIs(getattr(eval_v2, name), getattr(source, name))

    def test_evaluation_datasets_match_authored_bytes(self):
        # The formal datasets exist and stay byte-identical to the independently
        # authored artifacts: raw bytes, no JSON parsing, no line-ending changes.
        for name, expected in EXPECTED_DATASET_SHA256.items():
            with self.subTest(dataset=name):
                path = ROOT / "eval" / "v2" / name
                self.assertTrue(path.is_file())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)


# --------------------------------------------------------------------------
# Mutation guards: plausible wrong runners the scenarios above must catch
# --------------------------------------------------------------------------


class _KeepsRuntime(runner._CaseRun):
    def __init__(self, runtime, gateway, policy, max_steps):
        super().__init__(runtime, gateway, policy, max_steps)
        self._runtime = runtime


class _BypassesGatewayWhenFaultFree(_KeepsRuntime):
    def _call_tool(self, step, action):
        if self._runtime.faults:
            return super()._call_tool(step, action)
        self._tool_step += 1
        turn = len(self._messages)
        observation_id = runner.observation_id_for(turn, self._tool_step)
        result = execute_observation(self._runtime, action.tool_name, action.arguments,
                                     observation_id=observation_id)
        self._observations.append(ToolObservation(
            sequence=self._next_sequence(), control_step=step, turn_index=turn,
            tool_step=self._tool_step, observation_id=observation_id,
            tool_name=action.tool_name, arguments=action.arguments, result=result))


class _GatewayPerCall(_KeepsRuntime):
    def _call_tool(self, step, action):
        self._gateway = runner.FaultInjectingGateway(self._runtime)
        return super()._call_tool(step, action)


class _FutureTurnsVisible(runner._CaseRun):
    def _state(self, step):
        state = super()._state(step)
        future = tuple(UserMessage(turn_index=len(state.user_messages) + i + 1, text=text)
                       for i, (_, _, text) in enumerate(self._conditional))
        return dataclasses.replace(state, user_messages=state.user_messages + future)


class _ConditionalReused(runner._CaseRun):
    def _clarify(self, step, action):
        saved = list(self._conditional)
        try:
            return super()._clarify(step, action)
        finally:
            self._conditional = saved


class _ErrorTerminates(runner._CaseRun):
    def _call_tool(self, step, action):
        super()._call_tool(step, action)
        last = self._observations[-1]
        if type(last) is ToolObservation and last.result.status is ToolStatus.ERROR:
            raise _Stop()

    def drive(self):
        try:
            super().drive()
        except _Stop:
            self.termination = "max_steps_exceeded"


class _Stop(Exception):
    pass


class _CaseIdInObservationIds(runner._CaseRun):
    def _call_tool(self, step, action):
        def case_scoped(turn_index, tool_step):
            return ("case:" + self._case_id + ":turn:" + str(turn_index)
                    + ":tool:" + str(tool_step))
        with mock.patch.object(runner, "observation_id_for", case_scoped):
            return super()._call_tool(step, action)


class _SwallowsEveryValueError(runner._CaseRun):
    def _injected_malformed(self, before, observation_id):
        return True


class _OneExtraStep(runner._CaseRun):
    def drive(self):
        self._max_steps += 1
        super().drive()
        self._max_steps -= 1


def _run_case_without_exception_invariant(case, policy, *, max_steps):
    runtime = V2CaseRuntime.from_case(case)
    try:
        gateway = FaultInjectingGateway(runtime)
        case_run = runner._CaseRun(runtime, gateway, policy, max_steps)
        case_run.drive()
        runtime.assert_database_unchanged()
        return case_run.record(initial_db_sha256=runtime.initial_db_sha256,
                               final_db_sha256=runtime.database_sha256())
    finally:
        runtime.close()


def _to_dict_with_labels(self):
    payload = _REAL_TO_DICT(self)
    payload["expected_answerability"] = {"final": "answer"}
    return payload


_REAL_TO_DICT = CaseRunRecord.to_dict


def _uuid_observation_id(turn_index, tool_step):
    return "obs-" + uuid.uuid4().hex


def _patch_case_run(mutant):
    return mock.patch.object(runner, "_CaseRun", mutant)


MUTANTS = (
    ("bypasses_gateway_when_fault_free", lambda: _patch_case_run(_BypassesGatewayWhenFaultFree),
     OneGatewayTests.test_fault_free_case_still_goes_through_the_gateway),
    ("gateway_per_tool_call", lambda: _patch_case_run(_GatewayPerCall),
     BasicRunTests.test_observation_feeds_the_next_tool_arguments),
    ("uuid_observation_ids",
     lambda: mock.patch.object(runner, "observation_id_for", _uuid_observation_id),
     ObservationIdTests.test_ids_are_structural_and_stable),
    ("case_id_in_observation_ids", lambda: _patch_case_run(_CaseIdInObservationIds),
     CaseIdentityTests.test_case_id_never_reaches_the_policy),
    ("future_turns_visible", lambda: _patch_case_run(_FutureTurnsVisible),
     ControlStateTests.test_first_state_sees_only_turn_zero),
    ("conditional_turn_reused", lambda: _patch_case_run(_ConditionalReused),
     ClarificationTests.test_conditional_turn_is_consumed_once),
    ("error_terminates", lambda: _patch_case_run(_ErrorTerminates),
     FaultFeedbackTests.test_error_result_is_fed_back_to_policy),
    ("every_value_error_is_malformed", lambda: _patch_case_run(_SwallowsEveryValueError),
     FaultFeedbackTests.test_plain_value_error_is_not_malformed),
    ("one_extra_step", lambda: _patch_case_run(_OneExtraStep),
     MaxStepsTests.test_max_steps_stops_exactly),
    ("no_invariant_on_exception",
     lambda: mock.patch.object(runner, "run_case", _run_case_without_exception_invariant),
     LifecycleTests.test_database_change_outranks_the_primary_error),
    ("record_has_labels",
     lambda: mock.patch.object(CaseRunRecord, "to_dict", _to_dict_with_labels),
     RawRecordTests.test_record_has_no_label_keys),
    ("record_has_user_text",
     lambda: mock.patch.object(runner, "text_sha256", lambda text: text),
     RawRecordTests.test_user_text_is_hashed_not_stored),
)


def run_scenario(patcher, scenario):
    """Run one acceptance scenario, detached from any test result, so a failing
    check (even inside a subTest) propagates instead of being recorded."""
    owner = globals()[scenario.__qualname__.split(".")[0]]
    probe = owner(scenario.__name__)
    try:
        if patcher is None:
            scenario(probe)
        else:
            with patcher():
                scenario(probe)
    finally:
        probe.doCleanups()


class MutationGuardTests(unittest.TestCase):
    def test_the_real_runner_passes_every_scenario(self):
        for name, _, scenario in MUTANTS:
            with self.subTest(mutant=name):
                run_scenario(None, scenario)

    def test_every_mutant_is_caught_by_its_scenario(self):
        for name, patcher, scenario in MUTANTS:
            with self.subTest(mutant=name):
                with self.assertRaises(Exception):
                    run_scenario(patcher, scenario)


if __name__ == "__main__":
    unittest.main()
