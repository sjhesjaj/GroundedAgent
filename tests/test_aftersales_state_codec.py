"""The M2 checkpointer sees JSON only; every persisted type survives equally."""

from __future__ import annotations

import copy
import dataclasses
import json
import unittest
from enum import Enum
from types import MappingProxyType

from aftersales.action_outcome import ActionStatus
from aftersales.demo import DEMO_PERSONAS
from aftersales.ids import RequestIdentity, idempotency_key
from aftersales_service import agent_core as core
from aftersales_service.action_grounding import GroundedSubmission, SubmissionIndex, ground_action
from aftersales_service import conversation_state
from aftersales_service.conversation import _ControlRun, _Turn
from aftersales_service.conversation_state import (
    SCHEMA_VERSION, StateCodecError, decode, encode, encode_text, pack_state, unpack_state,
)
from aftersales_service.demo_store import DemoStore
from aftersales_service.observation_provenance import ObservationLedger
from orchestration.contracts import (
    DerivedEvidence, Evidence, FreshnessContract, SourceType, ToolResult, ToolStatus,
)
import requests

from tests.test_aftersales_grounding import OBSERVED_AT, outcome, validated
from tests.test_aftersales_service import RETURN_ARGS, ProductTestCase, call, decision


GENERATION = "0123456789abcdef0123456789abcdef"
JSON_NATIVE = (dict, list, str, int, float, bool, type(None))


def assert_json_native(test: unittest.TestCase, value: object, path: str = "state") -> None:
    """Every value is a JSON-native type: no tuple, enum, mapping proxy or project class."""
    test.assertIn(type(value), JSON_NATIVE, path)
    if type(value) is dict:
        for key, item in value.items():
            test.assertIs(type(key), str, path)
            assert_json_native(test, item, path + "." + key)
    elif type(value) is list:
        for index, item in enumerate(value):
            assert_json_native(test, item, path + "[" + str(index) + "]")


class ConversationStateCodecTests(unittest.TestCase):
    def setUp(self):
        self.store = DemoStore()
        self.addCleanup(self.store.close)
        self.ledger = ObservationLedger("codec-session")
        with self.store.read_side(DEMO_PERSONAS["demo-a"]) as reader:
            self.result = reader.execute("get_order", {"order_id": "ORD-1001"},
                                         observation_id="turn:1:tool:1")
        self.registered = self.ledger.register(
            run_index=1, observation_id="turn:1:tool:1", tool_name="get_order",
            arguments={"order_id": "ORD-1001"}, result=self.result)
        self.visible = self.ledger.visible_to(run_index=1, observation_ids=["turn:1:tool:1"])
        self.action = validated("create_return", RETURN_ARGS)
        self.binding = ground_action(self.action, self.visible)
        self.waiting = outcome(ActionStatus.WAITING_APPROVAL, pending="PA-0123456789ABCDEF")
        self.submission = GroundedSubmission(
            key=idempotency_key(RequestIdentity(persona_id="demo-a", request_id="conv-codec"),
                                self.action), action_name=self.action.action_name,
            args_sha256=self.action.args_sha256, binding=self.binding,
            first_run_index=1, first_outcome=self.waiting)
        self.observation = core.ToolObservation(
            sequence=1, control_step=1, turn_index=1, tool_step=1,
            observation_id="turn:1:tool:1", tool_name="get_order",
            arguments={"order_id": "ORD-1001"}, result=self.result)
        self.index = SubmissionIndex()
        self.index.record(self.submission)

    def round_trip(self, value):
        encoded = encode(value)
        # An actual JSON round trip proves there are no opaque msgpack objects,
        # tuples, mapping proxies, enums or classes left in checkpoint state.
        wire = json.loads(json.dumps(encoded, ensure_ascii=False, allow_nan=False))
        restored = decode(wire)
        self.assertEqual(restored, value)
        self.assertIs(type(restored), type(value))
        self.assertEqual(encode(restored), encoded)
        return restored

    def snapshot(self):
        return ([core.UserMessage(turn_index=1, text="我要退货")], [self.observation],
                [{"role": "customer", "text": "我要退货"}], 1, 1, 1,
                _ControlRun(1, 0, steps_used=2, awaiting_slots=("reason_code",)),
                {"PA-0123456789ABCDEF": {"action_name": "create_return", "arguments": RETURN_ARGS}},
                ["PA-0123456789ABCDEF"], self.ledger.snapshot(), self.index.snapshot())

    def test_every_control_and_turn_record_round_trips(self):
        values = [core.UserMessage(turn_index=1, text="你好"), self.observation,
                  core.ToolCall(tool_name="get_order", arguments={"order_id": "ORD-1001"}),
                  core.Clarify(slots=("order_id",)), core.Finish(disposition="refuse"),
                  core.ActionIntent(action_name="create_return", arguments=RETURN_ARGS),
                  _ControlRun(1, 0, steps_used=1, awaiting_slots=("order_id",)),
                  _Turn(committed=True, steps=[{"run": 1, "step": 1}],
                        model_calls=[{"latency_seconds": 0.1}], reply_kind="action",
                        reply_text="等待审批", clarification=("order_id",),
                        citations=({"ref": "source-1"},), action=self.waiting.to_dict())]
        for value in values:
            with self.subTest(type=type(value).__name__):
                self.round_trip(value)
        restored = self.round_trip(self.observation)
        self.assertIs(type(restored.arguments), MappingProxyType)
        self.assertTrue(restored.result_unchanged())
        self.assertEqual(restored.to_dict(), self.observation.to_dict())

    def test_every_evidence_and_tool_status_round_trips(self):
        evidence = Evidence(content="规则", source_type=SourceType.DOCUMENT,
                            source="policy", authority=90, confidence=0.75,
                            metadata={"nested": [True, None, {"x": 1}]})
        derived = DerivedEvidence(content="已签收", source_type=SourceType.DERIVED,
                                  source="rule", authority=100, locator="order:1#delivered",
                                  observed_at=OBSERVED_AT, fact_key="delivered", subject="order:1",
                                  value=True, details={"days": 2}, input_refs=("ev-input",),
                                  policy_refs=("policy-1",), derivation_id="rule/1")
        values = [evidence, self.result.evidence[0], derived, self.result,
                  ToolResult(tool_name="get_order", status=ToolStatus.EMPTY),
                  ToolResult(tool_name="get_order", status=ToolStatus.ERROR,
                             error_code="unavailable", error_message="source unavailable")]
        for value in values:
            with self.subTest(type=type(value).__name__):
                self.round_trip(value)
        for enum in (SourceType, FreshnessContract, ToolStatus, ActionStatus):
            for value in enum:
                with self.subTest(enum=enum.__name__, value=value.value):
                    self.assertIs(self.round_trip(value), value)

    def test_every_grounding_record_and_snapshot_round_trips(self):
        values = [self.registered.records[0], self.registered, self.visible,
                  self.binding.supports[0], self.binding, self.submission,
                  self.ledger.snapshot(), self.index.snapshot()]
        for value in values:
            with self.subTest(type=type(value).__name__):
                self.round_trip(value)
        ledger = ObservationLedger("codec-session")
        ledger.restore(self.round_trip(self.ledger.snapshot()))
        self.assertEqual(ledger.entries, self.ledger.entries)
        index = SubmissionIndex()
        index.restore(self.round_trip(self.index.snapshot()))
        self.assertEqual(index.anchored(self.submission.key), self.submission)
        self.assertEqual(index.for_pending(self.waiting.pending_action_id), self.submission)

    def test_every_outcome_and_replay_status_round_trips(self):
        values = [self.waiting,
                  outcome(ActionStatus.EXECUTED, receipt=True),
                  outcome(ActionStatus.EXECUTED, pending="PA-0123456789ABCDEF", receipt=True),
                  outcome(ActionStatus.REJECTED, pending="PA-0123456789ABCDEF", code="approval_rejected"),
                  outcome(ActionStatus.STALE, pending="PA-0123456789ABCDEF", code="record_version_changed"),
                  outcome(ActionStatus.DENIED, code="inventory_unavailable"),
                  outcome(ActionStatus.FAILED, code="transaction_failed")]
        for value in values:
            with self.subTest(status=value.status):
                self.round_trip(value)
                self.round_trip(dataclasses.replace(value, idempotent_replay=True))
                if value.receipt:
                    self.round_trip(value.receipt)
                if value.guard:
                    self.round_trip(value.guard)

    def test_every_registered_type_has_a_round_trip_sample(self):
        samples = {
            "Evidence": Evidence(content="规则", source_type=SourceType.DOCUMENT, source="policy",
                                 authority=90),
            "BusinessEvidence": self.result.evidence[0],
            "DerivedEvidence": DerivedEvidence(
                content="已签收", source_type=SourceType.DERIVED, source="rule", authority=100,
                locator="order:1#delivered", observed_at=OBSERVED_AT, fact_key="delivered",
                subject="order:1", value=True, details={"days": 2}, input_refs=("ev-input",),
                policy_refs=("policy-1",), derivation_id="rule/1"),
            "ToolResult": self.result, "SourceType": SourceType.BUSINESS,
            "FreshnessContract": next(iter(FreshnessContract)), "ToolStatus": ToolStatus.OK,
            "ActionStatus": ActionStatus.WAITING_APPROVAL,
            "UserMessage": core.UserMessage(turn_index=1, text="你好"),
            "ToolObservation": self.observation,
            "ToolCall": core.ToolCall(tool_name="get_order", arguments={"order_id": "ORD-1001"}),
            "Clarify": core.Clarify(slots=("order_id",)), "Finish": core.Finish(disposition="answer"),
            "ActionIntent": core.ActionIntent(action_name="create_return", arguments=RETURN_ARGS),
            "_ControlRun": _ControlRun(1, 0), "_Turn": _Turn(),
            "ObservedRecord": self.registered.records[0], "ObservationRecord": self.registered,
            "VisibleObservations": self.visible, "ArgumentSupport": self.binding.supports[0],
            "GroundingBinding": self.binding, "GroundedSubmission": self.submission,
            "GuardView": self.waiting.guard,
            "ReceiptView": outcome(ActionStatus.EXECUTED, receipt=True).receipt,
            "ActionOutcome": self.waiting,
        }
        self.assertEqual(set(samples), set(conversation_state._types()))
        for name, value in samples.items():
            with self.subTest(type=name):
                self.assertEqual(type(value).__name__, name)
                self.round_trip(value)

    def test_full_state_contains_only_json_and_preserves_every_snapshot_component(self):
        snapshot = self.snapshot()
        turn = _Turn(clarification=("order_id",), reply_kind="clarification", reply_text="订单号？")
        submission = {"action_name": "create_return", "arguments": RETURN_ARGS,
                      "args_sha256": self.action.args_sha256, "key": self.submission.key,
                      "binding": self.binding, "basis": "observed", "run_index": 1}
        state = pack_state(snapshot, generation=GENERATION, turn=turn, route="clarify",
                           decision=core.Clarify(slots=("order_id",)), visible=self.visible,
                           submission=submission, customer_text="我要退货")
        self.assertEqual(state["schema"], SCHEMA_VERSION)
        assert_json_native(self, state)
        wire = json.loads(json.dumps(state, ensure_ascii=False, allow_nan=False))
        decoded = unpack_state(wire)
        self.assertEqual(decoded["snapshot"], snapshot)
        self.assertEqual(decoded["turn"], turn)
        self.assertEqual(decoded["submission"], submission)
        self.assertEqual(decoded["visible"], self.visible)
        self.assertEqual(decoded["decision"], core.Clarify(slots=("order_id",)))
        self.assertEqual((decoded["route"], decoded["customer_text"]), ("clarify", "我要退货"))

    def test_every_step_field_is_written_every_time(self):
        # No value of an earlier step can survive into a later one by accident.
        state = pack_state(self.snapshot(), generation=GENERATION)
        self.assertEqual(set(state), {"schema", "generation", "snapshot", "turn", "route",
                                      "decision", "visible", "submission", "customer_text"})
        decoded = unpack_state(state)
        self.assertEqual({key: value for key, value in decoded.items()
                          if key not in ("snapshot", "generation")},
                         dict.fromkeys(("turn", "route", "decision", "visible", "submission",
                                        "customer_text")))
        self.assertEqual(decoded["generation"], GENERATION)

    def test_empty_state_round_trips(self):
        snapshot = ([], [], [], 0, 0, 0, None, {}, [], (), {})
        self.assertEqual(unpack_state(pack_state(snapshot, generation=GENERATION))["snapshot"], snapshot)

    def test_container_tags_cannot_collide_with_user_data(self):
        value = {"type": "_Turn", "fields": {"__class__": "os.system"},
                 "tuple": ("x", {"items": [1, None, False, 0.1]}),
                 "proxy": MappingProxyType({"key": (1, 2)})}
        restored = self.round_trip(value)
        self.assertIs(type(restored["tuple"]), tuple)
        self.assertIs(type(restored["proxy"]), MappingProxyType)
        with self.assertRaises(TypeError):
            restored["proxy"]["key"] = ()

    def test_unknown_versions_missing_fields_and_extra_fields_are_refused(self):
        state = pack_state(self.snapshot(), generation=GENERATION)
        for version in (0, 2, "1", True, None):
            with self.subTest(version=version), self.assertRaises(StateCodecError):
                unpack_state({**state, "schema": version})
        for key in state:
            invalid = {name: value for name, value in state.items() if name != key}
            with self.subTest(missing=key), self.assertRaises(StateCodecError):
                unpack_state(invalid)
        with self.assertRaises(StateCodecError):
            unpack_state({**state, "future": None})
        with self.assertRaises(TypeError):
            pack_state(self.snapshot(), generation=GENERATION, future=None)
        with self.assertRaises(StateCodecError):
            pack_state((), generation=GENERATION)
        for field, value in (("route", 1), ("customer_text", ["x"])):
            with self.subTest(field=field), self.assertRaises(StateCodecError):
                unpack_state({**state, field: value})
        with self.assertRaises(StateCodecError):
            pack_state(self.snapshot(), generation=GENERATION, route=1)
        with self.assertRaises(StateCodecError):
            encode_text(None)
        for generation in ("", None, 1):
            with self.subTest(generation=generation), self.assertRaises(StateCodecError):
                unpack_state({**state, "generation": generation})
            with self.subTest(generation=generation), self.assertRaises(StateCodecError):
                pack_state(self.snapshot(), generation=generation)

    def test_arbitrary_types_and_non_json_values_are_refused(self):
        @dataclasses.dataclass
        class Unknown:
            value: str

        class UnknownEnum(str, Enum):
            VALUE = "value"

        for value in (Unknown("x"), UnknownEnum.VALUE, object(), b"data", {1: "x"},
                      {"set"}, float("nan"), float("inf"), float("-inf")):
            with self.subTest(type=type(value).__name__), self.assertRaises(StateCodecError):
                encode(value)

    def test_unknown_tags_and_malformed_records_are_refused(self):
        encoded = encode(core.UserMessage(turn_index=1, text="hello"))
        malformed = [{"type": "os.system", "fields": {}},
                     {"type": "tuple", "items": {}},
                     {"type": "dict", "items": {1: "value"}},
                     {"type": "SourceType", "value": "future"},
                     {"type": "SourceType", "value": "business", "extra": 1},
                     {"type": "tuple", "items": [], "extra": 1},
                     {"type": "UserMessage", "fields": {"turn_index": 1}},
                     {"type": "UserMessage", "fields": {**encoded["fields"], "extra": 1}},
                     {**encoded, "extra": 1}, object(), (1, 2)]
        for value in malformed:
            with self.subTest(value=repr(value)), self.assertRaises(StateCodecError):
                decode(value)

    def test_a_constructor_that_changes_persisted_data_is_refused(self):
        data = encode(self.registered)
        # ObservationRecord converts records to tuple. A list wire representation
        # cannot be silently accepted because that would not re-encode equally.
        data["fields"]["records"] = data["fields"]["records"]["items"]
        with self.assertRaises(StateCodecError):
            decode(data)

    def test_mutated_observation_snapshot_is_refused(self):
        self.observation.result.trace["changed"] = True
        with self.assertRaises(StateCodecError):
            encode(self.observation)

    def test_decoded_state_has_no_mutable_alias_to_original(self):
        restored = self.round_trip(self.snapshot())
        restored[0].append(core.UserMessage(turn_index=2, text="different"))
        restored[2][0]["text"] = "different"
        restored[1][0].result.trace["extra"] = True
        self.assertEqual(len(self.snapshot()[0]), 1)
        self.assertEqual(self.snapshot()[2][0]["text"], "我要退货")
        self.assertNotIn("extra", self.result.trace)


class CheckpointContentTests(ProductTestCase):
    """What the checkpointer actually holds for a real conversation through the graph."""

    def test_every_checkpoint_holds_only_json_native_values_the_codec_accepts(self):
        # read, ground, gateway, clarify, an operator decision, finish - and a failed turn.
        session_id, pending_id = self.waiting()
        self.say(session_id, "另外那件 T 恤我也想换", decision(call("ask_user", {"slots": ["order_id"]})))
        self.decide(session_id, pending_id, "APPROVE")
        with self.assertLogs("aftersales_service.service", level="WARNING"):
            self.say(session_id, "ORD-1004", requests.ConnectionError("down"), expected=503)
        self.say(session_id, "ORD-1004", decision(call("finish", {"disposition": "refuse"})))
        conversation = self.service._sessions[session_id]
        history = list(conversation._graph.get_state_history(conversation._config(None)))
        self.assertGreater(len(history), 10)
        for checkpoint in history:
            with self.subTest(checkpoint=checkpoint.config["configurable"]["checkpoint_id"],
                              source=checkpoint.metadata.get("source")):
                assert_json_native(self, checkpoint.values)
                unpack_state(checkpoint.values)
        # The head is exactly the conversation this object serves.
        head = conversation._graph.get_state(conversation._config(conversation._head))
        self.assertEqual(unpack_state(head.values)["snapshot"], conversation._snapshot())
        self.assertEqual(head.next, ())

    def test_a_project_object_that_bypasses_the_codec_never_comes_back_as_itself(self):
        # The checkpointer's serializer is strict, but in langgraph 1.2.14 a blocked
        # object comes back as a plain dict (with a log line); the codec refuses it.
        conversation = self.service._sessions[self.session()]
        smuggled = dict(conversation._pack(None), turn=_Turn(reply_kind="action"))
        config = conversation._graph.update_state(conversation._config(conversation._head),
                                                  smuggled, as_node="finish")
        with self.assertLogs("langgraph.checkpoint.serde.jsonplus", level="WARNING"):
            values = conversation._graph.get_state(config).values
        self.assertIs(type(values["turn"]), dict)
        with self.assertRaises(StateCodecError):
            unpack_state(values)


if __name__ == "__main__":
    unittest.main()
