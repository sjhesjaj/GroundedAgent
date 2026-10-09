"""M3 product contracts, scripted providers and isolated temporary databases."""

from __future__ import annotations

import json
import os
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aftersales.action_gateway import ActionGateway
from aftersales_service import agent_core as core
from aftersales_service import customer_wording as wording
from aftersales_service import decision_policy as dp
from aftersales_service import knowledge_base as kb
from aftersales_service.pending_requests import PENDING_TOOL_NAME
from aftersales_service.routes import create_router
from aftersales_service.service import AftersalesService
from llm_provider import LLMResponse
from tests.test_aftersales_service import (
    ProductTestCase, RETURN_ARGS, call, cite_first_source, decision,
)
from tests.test_m3_decision_policy import state


class M3RuntimeTests(ProductTestCase):
    def setUp(self):
        selected = mock.patch.dict(os.environ, {dp.DECISION_POLICY_ENV: dp.POLICY_M3})
        selected.start()
        self.addCleanup(selected.stop)
        super().setUp()
        base = kb.KnowledgeBase(kb.load_corpus())
        knowledge = mock.patch("aftersales_service.conversation.shared_knowledge_base", return_value=base)
        knowledge.start()
        self.addCleanup(knowledge.stop)

    def restart(self):
        self.service.close()
        self.service = AftersalesService(lambda: self.provider, data_dir=self.data_dir)
        self.addCleanup(self.service.close)
        app = FastAPI()
        app.include_router(create_router(self.service))
        self.client = TestClient(app)

    def test_all_smalltalk_templates_precede_provider_creation_and_start_no_run(self):
        session = self.session()
        with mock.patch.object(self.service, "_provider_factory", side_effect=RuntimeError("unavailable")) as factory:
            for text, kind in (("您好！", "greeting"), ("谢谢。", "thanks"), ("再见", "goodbye")):
                reply = self.say(session, text)
                self.assertEqual(reply["reply"], {"kind": kind, "text": wording.FIXED_RESPONSES[kind]})
                self.assertEqual(reply["trace"], {"steps": [], "model_calls": []})
            factory.assert_not_called()
        conversation = self.service._sessions[session]
        self.assertEqual((conversation._runs, conversation._tool_steps, len(conversation._messages)), (0, 0, 3))
        self.restart()
        self.assertEqual(len(self.view(session)["messages"]), 6)

    def test_confirmations_and_mixed_greetings_are_not_filtered(self):
        session = self.session()
        for text in ("好的", "嗯", "可以", "需要", "你好，帮我退货", "谢谢，退款什么时候到"):
            reply = self.say(session, text, decision(call("finish", {"disposition": "refuse"})))
            self.assertEqual(reply["reply"]["kind"], "refuse")
        self.assertEqual(len(self.provider.requests), 6)

    def test_paused_clarification_disables_smalltalk_filter_and_keeps_replay_order(self):
        session = self.session()
        first = self.say(session, "我要退货", decision(call("get_order", {"order_id": "ORD-1001"})),
                         decision(call("ask_user", {"slots": ["order_item"]})))
        self.assertEqual(first["status"], "NEEDS_CLARIFICATION")
        second = self.say(session, "谢谢", decision(call("finish", {"disposition": "refuse"})))
        self.assertEqual(second["reply"]["kind"], "refuse")
        self.assertEqual(self.decisions(), [(1, 6), (2, 5), (3, 4)])
        messages = self.provider.requests[-1]["messages"]
        for index, message in enumerate(messages):
            if message["role"] == "tool":
                self.assertEqual(messages[index - 1]["role"], "assistant")
                self.assertTrue(messages[index - 1].get("tool_calls"))
        self.assertIn("为了继续处理", json.dumps(messages, ensure_ascii=False))

    def test_m3_non_answer_wording_is_fixed(self):
        session = self.session()
        for disposition in ("refuse", "handoff", "boundary"):
            reply = self.say(session, "售后问题", decision(call("finish", {"disposition": disposition})))
            self.assertEqual(reply["reply"]["text"], wording.FIXED_RESPONSES[disposition])
        self.assertEqual(len(self.provider.requests), 3)

    def test_stage6_greeting_still_requires_provider_and_uses_frozen_wording(self):
        with mock.patch.dict(os.environ, {dp.DECISION_POLICY_ENV: dp.POLICY_STAGE6}):
            session = self.session()
            reply = self.say(session, "你好", decision(call("finish", {"disposition": "refuse"})))
            self.assertEqual(reply["reply"]["text"], core.FIXED_RESPONSES["refuse"])
            self.assertNotIn("kind", reply["trace"]["model_calls"][0])

    def test_session_manifest_binds_policy_and_survives_restart(self):
        session = self.session()
        path = self.service._runtime.sessions._path(session)
        manifest = json.loads(path.read_text("utf-8"))
        self.assertEqual(manifest["decision_policy"], "m3")
        self.restart()
        self.assertEqual(self.view(session)["session_id"], session)
        self.assertEqual(self.service._sessions[session].policy, "m3")

    def assert_policy_rejected(self, session, pending="PA-" + "0" * 16):
        base = "/api/aftersales/sessions/" + session
        for response in (self.client.get(base), self.client.post(base + "/messages", json={"text": "你好"}),
                         self.client.post("/api/aftersales/operator/sessions/" + session + "/decision",
                                          json={"pending_action_id": pending, "decision": "APPROVE"})):
            self.assertEqual((response.status_code, response.json()["detail"]),
                             (409, {"code": "policy_version_mismatch"}))

    def test_cached_and_restarted_sessions_reject_config_mismatch_before_calls_or_operator(self):
        session, pending = self.waiting()
        calls = len(self.provider.requests)
        with mock.patch.dict(os.environ, {dp.DECISION_POLICY_ENV: dp.POLICY_STAGE6}):
            self.assert_policy_rejected(session, pending)
            self.restart()
            self.assert_policy_rejected(session, pending)
        self.assertEqual(len(self.provider.requests), calls)
        self.assertEqual(self.view(session)["pending_actions"][0]["status"], "WAITING_APPROVAL")

    def test_unknown_legacy_policy_is_not_guessed(self):
        session = self.session()
        path = self.service._runtime.sessions._path(session)
        manifest = json.loads(path.read_text("utf-8"))
        manifest.pop("decision_policy")
        manifest["schema"] = 1
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.restart()
        self.assert_policy_rejected(session)

    def test_pending_progress_uses_current_gateway_not_historical_completed_exchange(self):
        session, pending = self.waiting()
        reply = self.say(session, "进度怎么样？", decision(call(PENDING_TOOL_NAME, {})),
                         decision(call("finish", {"disposition": "answer"})),
                         cite_first_source("您刚提交的退货申请正在等待审批，目前还没有执行。"))
        self.assertEqual(reply["reply"]["kind"], "answer")
        result = self.observations(session)[-1].result
        requests_data = json.loads(result.evidence[0].content.split("\n", 1)[1])
        self.assertEqual(requests_data, [{"action_type": "create_return", "order_id": "ORD-1001",
                                         "order_item_id": "OI-1001-2", "pending_action_id": pending,
                                         "status": "WAITING_APPROVAL"}])
        self.assertNotIn("AS-1001", result.evidence[0].content)
        self.assertEqual(self.count("action_receipts"), 0)
        calls = reply["trace"]["model_calls"]
        self.assertEqual([item["kind"] for item in calls], ["decision", "decision", "generation"])
        self.assertEqual(sum(item["total_tokens"] for item in calls), 440)
        self.assertTrue(all(item["max_tokens"] > 0 and item["timeout_seconds"] > 0 for item in calls))

    def test_pending_tool_is_session_isolated_and_completed_requests_disappear(self):
        session, pending = self.waiting()
        other = self.session()
        self.say(other, "进度怎么样？", decision(call(PENDING_TOOL_NAME, {})),
                 decision(call("finish", {"disposition": "refuse"})))
        self.assertEqual(self.observations(other)[-1].result.status.value, "empty")
        self.decide(session, pending, "REJECT")
        self.say(session, "进度怎么样？", decision(call(PENDING_TOOL_NAME, {})),
                 decision(call("finish", {"disposition": "refuse"})))
        self.assertEqual(self.observations(session)[-1].result.status.value, "empty")

    def test_current_empty_pending_collection_can_be_answered_and_cited(self):
        session = self.session()
        reply = self.say(session, "进度怎么样？", decision(call(PENDING_TOOL_NAME, {})),
                         decision(call("finish", {"disposition": "answer"})),
                         cite_first_source("本会话目前没有待审批的申请。"))
        self.assertEqual(reply["reply"]["kind"], "answer")
        self.assertEqual(reply["citations"][0]["producer"], PENDING_TOOL_NAME)
        result = self.observations(session)[-1].result
        self.assertEqual((result.status.value, result.evidence), ("empty", ()))
        sources = json.loads(self.provider.requests[-1]["messages"][-1]["content"])["sources"]
        self.assertIn("[]", sources[0]["content"])
        self.assertNotIn("ORD-", sources[0]["content"])
        provenance = self.service._sessions[session]._provenance.entries[-1]
        self.assertEqual(provenance.records, ())
        self.assertEqual(self.count("pending_actions"), 0)

    def test_old_empty_query_is_not_offered_as_a_source_for_a_later_run(self):
        session = self.session()
        self.say(session, "有没有待审批的申请？", decision(call(PENDING_TOOL_NAME, {})),
                 decision(call("finish", {"disposition": "answer"})),
                 cite_first_source("本会话目前没有待审批的申请。"))
        reply = self.say(session, "再说一次？", decision(call("finish", {"disposition": "answer"})),
                         LLMResponse(content=json.dumps({"answer": "尚未查询本轮待审批申请。", "citation_refs": []}),
                                     prompt_tokens=50, completion_tokens=10, latency_seconds=0.1,
                                     provider="deepseek", model="deepseek-chat"))
        sources = json.loads(self.provider.requests[-1]["messages"][-1]["content"])["sources"]
        self.assertEqual((sources, reply["citations"]), ([], []))

    def test_pending_result_cannot_ground_a_new_action(self):
        session, _ = self.waiting()
        # The returned order/item ids occur in the pending result. A different
        # reason creates a new proposal, so an old idempotent replay anchor
        # cannot supply its grounding instead of this run's missing reads.
        args = {**RETURN_ARGS, "reason_code": "quality_issue"}
        with mock.patch.object(ActionGateway, "start_action", side_effect=AssertionError("must not reach gateway")) as gateway:
            reply = self.say(session, "用这个订单商品再提交质量原因的退货", decision(call(PENDING_TOOL_NAME, {})),
                             decision(call("create_return", args)))
            gateway.assert_not_called()
        self.assertEqual(reply["reply"]["kind"], "grounding_rejected")
        provenance = self.service._sessions[session]._provenance.entries[-1]
        self.assertEqual(provenance.tool_name, PENDING_TOOL_NAME)
        self.assertEqual(provenance.records, ())
        self.assertFalse(provenance.usable)
        self.assertEqual(self.count("pending_actions"), 1)

    def test_pending_tool_cannot_select_session_or_customer(self):
        current = state(tools=core.STAGE5_RUNTIME_TOOLS + (kb.KNOWLEDGE_TOOL_NAME, PENDING_TOOL_NAME))
        offered = dp.m3_offered_functions(current)
        for arguments in ({"session_id": "0" * 32}, {"customer_id": "CUST-002"}):
            translation = dp.translate_m3_response(current, offered, (call(PENDING_TOOL_NAME, arguments),))
            self.assertEqual(translation.actions, (core.Finish(disposition="refuse"),))
        self.assertNotIn(PENDING_TOOL_NAME, dp.m3_offered_functions(state()))
        self.assertNotIn(PENDING_TOOL_NAME, dp.m3_offered_functions(
            state(step=core.STAGE6_MAX_STEPS, tools=current.allowed_tools)))

    def test_failed_call_has_trace_and_does_not_move_head_or_record_customer(self):
        session = self.session()
        conversation = self.service._sessions[session]
        head = conversation._head
        with self.assertLogs("aftersales_service.service", level="WARNING"):
            reply = self.say(session, "退款多久到账？", requests.ConnectionError("offline"), expected=503)
        trace = reply["detail"]["trace"]["model_calls"]
        self.assertEqual(len(trace), 1)
        self.assertEqual((trace[0]["kind"], trace[0]["status"]), ("decision", "provider_error"))
        self.assertIsNone(trace[0]["total_tokens"])
        self.assertEqual(conversation._head, head)
        self.assertEqual(self.view(session)["messages"], [])

    def test_invalid_generation_records_usage_and_returns_answer_unavailable(self):
        session = self.session()
        bad = LLMResponse(content="not JSON", prompt_tokens=200, completion_tokens=20,
                          latency_seconds=0.1, provider="deepseek", model="deepseek-chat")
        reply = self.say(session, "我的订单是什么状态？",
                         decision(call("get_order", {"order_id": "ORD-1001"})),
                         decision(call("finish", {"disposition": "answer"})), bad)
        self.assertEqual(reply["reply"]["kind"], "answer_unavailable")
        final = reply["trace"]["model_calls"][-1]
        self.assertEqual((final["kind"], final["status"], final["total_tokens"]),
                         ("generation", "protocol_error", 220))

    def test_failed_generation_keeps_all_call_usage_but_rolls_back_the_turn(self):
        session = self.session()
        head = self.service._sessions[session]._head
        with self.assertLogs("aftersales_service.service", level="WARNING"):
            reply = self.say(session, "订单现在是什么状态？",
                             decision(call("get_order", {"order_id": "ORD-1001"})),
                             decision(call("finish", {"disposition": "answer"})),
                             requests.ConnectionError("offline"), expected=503)
        calls = reply["detail"]["trace"]["model_calls"]
        self.assertEqual([item["kind"] for item in calls], ["decision", "decision", "generation"])
        self.assertEqual([item["total_tokens"] for item in calls], [110, 110, None])
        self.assertEqual(calls[-1]["status"], "provider_error")
        self.assertEqual(self.service._sessions[session]._head, head)
        self.assertEqual(self.view(session)["messages"], [])

    def test_every_call_records_the_configured_transport_timeout(self):
        self.provider.timeout = 35.0
        session = self.session()
        reply = self.say(session, "我的订单是什么状态？",
                         decision(call("get_order", {"order_id": "ORD-1001"})),
                         decision(call("finish", {"disposition": "answer"})),
                         cite_first_source("订单已经签收。"))
        self.assertEqual([item["timeout_seconds"] for item in reply["trace"]["model_calls"]],
                         [35.0, 35.0, 35.0])

    def test_product_trace_keeps_retrieval_mode_and_fallback(self):
        session = self.session()
        reply = self.say(session, "退款多久到账？", decision(call(kb.KNOWLEDGE_TOOL_NAME, {"query": "退款到账时间"})),
                         decision(call("finish", {"disposition": "refuse"})))
        step = reply["trace"]["steps"][0]
        self.assertEqual(step["retrieval_mode"], "bm25")
        self.assertEqual(step["fallback"], "embedder_not_configured")
        source_trace = self.observations(session)[-1].result.trace
        for key in ("relevance_signal", "relevance_scope", "relevance_floor",
                    "relevance_top_score", "eligible_passages", "relevance_filtered_passages"):
            with self.subTest(field=key):
                self.assertIn(key, step)
                self.assertEqual(step[key], source_trace[key])
