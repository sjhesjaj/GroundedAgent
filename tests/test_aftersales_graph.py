"""GroundedAgent V2 M2 (phases 1-2): the product control flow as a LangGraph StateGraph.

Offline, with the scripted model of tests.test_aftersales_service. These tests
pin what the graph must keep from the M0/M1 control loop and what review found
the first graph draft got wrong (docs/v2/m2-session-recovery.md).
"""

from __future__ import annotations

import ast
import json
import unittest
from unittest import mock

import requests

from aftersales.action_gateway import ActionGateway
from aftersales.action_outcome import ActionOutcomeRenderer
from aftersales_service.conversation import Conversation
from aftersales_service.conversation_graph import DURABILITY
from aftersales_service.conversation_state import unpack_state
from tests.test_aftersales_service import (
    RETURN_ARGS, ProductTestCase, call, decision, package_sources, runtime_context,
)


def user_messages(request: dict) -> list[str]:
    return [message["content"] for message in request["messages"] if message["role"] == "user"]


class ClarificationRetryTests(ProductTestCase):
    def test_a_failed_answer_to_a_clarification_leaves_no_trace(self):
        # Review finding: resuming an interrupt stores the answer on the head itself,
        # so a retry from the same head reached the policy with the FAILED answer.
        session_id = self.session()
        asked = self.say(session_id, "我要退货", decision(call("ask_user", {"slots": ["order_id"]})))
        self.assertEqual(asked["status"], "NEEDS_CLARIFICATION")

        with self.assertLogs("aftersales_service.service", level="WARNING"):
            failed = self.say(session_id, "ORD-1001", requests.ConnectionError("down"),
                              expected=503)
        self.assertEqual(failed["detail"], {"code": "llm_unavailable"})
        self.assertEqual(user_messages(self.provider.requests[-1]), ["我要退货", "ORD-1001"])

        start = len(self.provider.requests)
        retried = self.say(session_id, "ORD-3015", decision(call("finish", {"disposition": "refuse"})))
        self.assertEqual(retried["reply"]["kind"], "refuse")
        retry_requests = self.provider.requests[start:]
        self.assertEqual(len(retry_requests), 1)
        self.assertEqual(user_messages(retry_requests[0])[-1], "ORD-3015")
        self.assertEqual(user_messages(retry_requests[0]), ["我要退货", "ORD-3015"])
        # The frozen tool schemas use ORD-1001 as their example order id. They are the
        # same bytes as before the customer ever typed it; nothing else the model
        # receives - the trusted context and the whole conversation - contains it.
        schemas = json.dumps(self.provider.requests[0]["tools"], ensure_ascii=False)
        for request in retry_requests:
            self.assertEqual(json.dumps(request["tools"], ensure_ascii=False), schemas)
            rest = {key: value for key, value in request.items() if key != "tools"}
            self.assertNotIn("ORD-1001", json.dumps(rest, ensure_ascii=False))
        # The answer continued the paused run: step 2 of the same six-step budget.
        self.assertEqual(self.decisions(start), [(2, 5)])

        view = self.view(session_id)
        self.assertNotIn("ORD-1001", json.dumps(view["messages"], ensure_ascii=False))
        self.assertEqual([(entry["role"], entry.get("kind"), entry["text"] if entry["role"] == "customer"
                           else None) for entry in view["messages"]],
                         [("customer", None, "我要退货"), ("assistant", "clarification", None),
                          ("customer", None, "ORD-3015"), ("assistant", "refuse", None)])
        self.assertEqual(view["status"], "OPEN")


class DecisionDuringClarificationTests(ProductTestCase):
    def test_an_operator_decision_leaves_an_open_clarification_answerable(self):
        # Run 1 parks a return at the approval boundary; run 2 waits on a clarification.
        session_id, pending_id = self.waiting()
        asked = self.say(session_id, "另外那件 T 恤我也想换", decision(call("ask_user", {"slots": ["order_id"]})))
        self.assertEqual((asked["status"], asked["pending_action_id"]), ("NEEDS_CLARIFICATION", pending_id))

        approved = self.decide(session_id, pending_id, "APPROVE")
        self.assertEqual(approved["action"]["status"], "EXECUTED")
        self.assertEqual((approved["status"], approved["pending_action_id"]), ("NEEDS_CLARIFICATION", None))

        start = len(self.provider.requests)
        answered = self.say(session_id, "ORD-1004", decision(call("finish", {"disposition": "refuse"})))
        self.assertEqual(answered["reply"]["kind"], "refuse")
        # Run 2 continued with its own step budget; the decision did not end it.
        self.assertEqual(self.decisions(start), [(2, 5)])
        self.assertEqual(runtime_context(self.provider.requests[start])["step_number"], 2)
        self.assertEqual(user_messages(self.provider.requests[start])[-2:],
                         ["另外那件 T 恤我也想换", "ORD-1004"])
        self.assertEqual(answered["status"], "OPEN")
        # The operator outcome stays in the committed conversation after the next turn.
        view = self.view(session_id)
        self.assertEqual([(entry["role"], entry.get("kind")) for entry in view["messages"]],
                         [("customer", None), ("assistant", "action"),
                          ("customer", None), ("assistant", "clarification"),
                          ("assistant", "operator_decision"),
                          ("customer", None), ("assistant", "refuse")])
        self.assertEqual(view["pending_actions"][0]["status"], "EXECUTED")


class CommitTests(ProductTestCase):
    def invocations(self):
        """Spy on Conversation._invoke: (input, context, checkpoint it returned) per call."""
        seen = []
        original = Conversation._invoke

        def spy(conversation, value, checkpoint_id, context):
            checkpoint = original(conversation, value, checkpoint_id, context)
            seen.append((value, context, checkpoint))
            return checkpoint

        patcher = mock.patch.object(Conversation, "_invoke", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_an_action_turn_stops_before_the_gateway_with_its_submission_checkpointed(self):
        session_id = self.session()
        seen = self.invocations()
        payload = self.request_return(session_id)
        self.assertEqual(payload["status"], "WAITING_APPROVAL")
        (first, policy_context, stopped), (resumed, gateway_context, ended) = seen
        self.assertEqual(first, {"customer_text": "ORD-1001 里那件内衣我不想要了，帮我退货"})
        self.assertEqual(stopped.next, ("gateway",))
        submission = unpack_state(stopped.values)["submission"]
        self.assertEqual((submission["action_name"], submission["arguments"], submission["basis"]),
                         ("create_return", RETURN_ARGS, "observed"))
        self.assertEqual(submission["binding"].args_sha256, submission["args_sha256"])
        # The gateway is resumed with None, from that very checkpoint, without the model.
        self.assertIsNone(resumed)
        self.assertIsNotNone(policy_context.policy)
        self.assertEqual((gateway_context.policy, gateway_context.provider, gateway_context.reader),
                         (None, None, None))
        self.assertTrue(gateway_context.gateway_returned)
        self.assertEqual(ended.next, ())
        conversation = self.service._sessions[session_id]
        self.assertEqual(conversation._head, ended.config["configurable"]["checkpoint_id"])

    def test_a_failed_turn_never_moves_the_head(self):
        session_id = self.session()
        conversation = self.service._sessions[session_id]
        head = conversation._head
        with self.assertLogs("aftersales_service.service", level="WARNING"):
            self.say(session_id, "ORD-1001 里那件内衣我不想要了",
                     decision(call("get_order", {"order_id": "ORD-1001"})),
                     requests.ConnectionError("down"), expected=503)
        self.assertEqual(conversation._head, head)
        # Its orphan branch exists - it is the thread's newest checkpoint - and is never read.
        newest = conversation._graph.get_state(conversation._config(None))
        self.assertNotEqual(newest.config["configurable"]["checkpoint_id"], head)
        self.assertEqual(self.view(session_id)["messages"], [])

    def test_the_graph_has_one_static_stop_and_every_run_is_durable(self):
        conversation = self.service._sessions[self.session()]
        self.assertEqual(list(conversation._graph.interrupt_before_nodes), ["gateway"])
        self.assertEqual(DURABILITY, "sync")
        for name, tree in package_sources().items():
            for node in ast.walk(tree):
                with self.subTest(module=name, line=getattr(node, "lineno", None)):
                    if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("langgraph"):
                        self.assertFalse({alias.name for alias in node.names} & {"interrupt", "Command"})
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in (
                            "invoke", "stream", "ainvoke", "astream"):
                        self.assertIn("durability", [keyword.arg for keyword in node.keywords])
                    # Strictness comes from the serializer argument, never the environment.
                    if isinstance(node, ast.Constant):
                        self.assertNotEqual(node.value, "LANGGRAPH_STRICT_MSGPACK")


class GatewayReturnedTests(ProductTestCase):
    def test_a_failure_after_the_gateway_returned_is_recovered_from_the_marker(self):
        # Once start_action returned, the error surfaces, the in-flight marker stays and
        # the object is discarded; the next request rebuilds the conversation and runs
        # ONLY the gateway again: the core's idempotent replay. The turn is recorded in
        # full, the business write happened once, the pending id is never lost.
        session_id = self.session()
        started = mock.patch.object(ActionGateway, "start_action", autospec=True,
                                    side_effect=ActionGateway.start_action)
        with started as spy, mock.patch.object(ActionOutcomeRenderer, "render",
                                                side_effect=RuntimeError("render")):
            with self.assertRaises(RuntimeError):
                self.request_return(session_id)
            self.assertEqual(spy.call_count, 1)
            marker = self.session_file(session_id)["inflight"]
            self.assertEqual(marker["action_name"], "create_return")
        requests_before = len(self.provider.requests)
        with mock.patch.object(ActionGateway, "start_action", autospec=True,
                               side_effect=ActionGateway.start_action) as spy:
            view = self.view(session_id)
            self.assertEqual(spy.call_count, 1)
            self.assertTrue(spy.call_args.args[0] is self.service.store.gateway)
        self.assertEqual(len(self.provider.requests), requests_before)   # no model call
        self.assertIsNone(self.session_file(session_id)["inflight"])
        self.assertEqual(view["status"], "WAITING_APPROVAL")
        self.assertEqual(self.count("pending_actions"), 1)
        self.assertEqual([(entry["role"], entry.get("kind"), entry.get("action_status"))
                          for entry in view["messages"]],
                         [("customer", None, None), ("assistant", "action", "WAITING_APPROVAL")])
        self.assertEqual([event["event_name"] for event in view["audit"]],
                         ["guard.evaluated", "action.pending_created", "action.replay_hit"])
        # The next turn starts from that conversation, in a new run.
        start = len(self.provider.requests)
        self.say(session_id, "好的", decision(call("finish", {"disposition": "refuse"})))
        self.assertEqual(user_messages(self.provider.requests[start]),
                         ["ORD-1001 里那件内衣我不想要了，帮我退货", "好的"])
        self.assertEqual(self.decisions(start), [(1, 6)])
        approved = self.decide(session_id, view["pending_action_id"], "APPROVE")
        self.assertEqual(approved["action"]["status"], "EXECUTED")
        self.assertEqual(self.count("action_receipts"), 1)

    def session_file(self, session_id: str) -> dict:
        generation = json.loads((self.data_dir / "generation.json").read_text(encoding="utf-8"))
        path = self.data_dir / ("gen-" + generation["generation"]) / "sessions" / (session_id + ".json")
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
