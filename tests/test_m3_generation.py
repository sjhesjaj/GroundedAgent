"""M3 answer monitoring and transport limits, without live providers."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from aftersales_service import agent_core as core
from aftersales_service import answer_policy
from aftersales_service import decision_policy
from aftersales_service.pending_requests import PENDING_TOOL_NAME, pending_tool_result
from llm_provider import LLMResponse, OllamaProvider, OpenAICompatibleProvider
from orchestration.contracts import ToolResult, ToolStatus, evidence_ref
from tests.test_m3_decision_policy import KB_ARGS, knowledge_observation, state


class M3GenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = state(observations=(knowledge_observation(1, KB_ARGS),))
        self.sources = core.build_sources(core.derive_from_control_state(self.state.read_view()))
        self.ref = self.sources[0].ref
        self.response = LLMResponse(
            content=json.dumps({"answer": "非质量原因退货的寄回运费由您承担。",
                                "citation_refs": [self.ref]}, ensure_ascii=False),
            prompt_tokens=321, completion_tokens=45, latency_seconds=0.25,
            provider="deepseek", model="deepseek-flash",
        )
        self.provider = SimpleNamespace(name="deepseek", model="deepseek-flash", timeout=180.0,
                                        chat=mock.Mock(return_value=self.response))

    def test_answer_preserves_usage_and_emits_one_generation_call(self):
        calls = []
        result = answer_policy.generate_answer(self.provider, self.state, on_model_call=calls.append)
        self.assertEqual((result.prompt_tokens, result.completion_tokens, result.latency_seconds),
                         (321, 45, 0.25))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], {
            "kind": "generation", "provider": "deepseek", "model_requested": "deepseek-flash",
            "model_reported": "deepseek-flash", "prompt_tokens": 321, "completion_tokens": 45,
            "total_tokens": 366, "latency_seconds": 0.25, "max_tokens": 1024,
            "timeout_seconds": 180.0, "status": "success", "diagnostic": None,
        })

    def test_invalid_citation_still_records_the_billed_generation_call(self):
        self.provider.chat.return_value = dataclasses.replace(
            self.response, content=json.dumps({"answer": "不允许引用历史。", "citation_refs": ["history:1"]}))
        calls = []
        with self.assertRaises(core.AnswerUnavailable) as error:
            answer_policy.generate_answer(self.provider, self.state, on_model_call=calls.append)
        self.assertEqual(error.exception.code, "unknown_citation_ref")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "protocol_error")
        self.assertEqual(calls[0]["diagnostic"], "unknown_citation_ref")
        self.assertEqual((calls[0]["prompt_tokens"], calls[0]["completion_tokens"]), (321, 45))

    def test_network_failure_propagates_without_inventing_usage_or_leaking_error_text(self):
        failure = ConnectionError("private URL and credentials must not be traced")
        self.provider.chat.side_effect = failure
        calls = []
        with self.assertRaises(ConnectionError) as error:
            answer_policy.generate_answer(self.provider, self.state, on_model_call=calls.append)
        self.assertIs(error.exception, failure)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "provider_error")
        self.assertEqual(calls[0]["diagnostic"], "ConnectionError")
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "latency_seconds"):
            self.assertIsNone(calls[0][key])
        self.assertNotIn("credentials", json.dumps(calls))
        self.assertEqual(len(self.state.observations), 1)

    def test_m3_kb_citation_uses_only_verified_source_metadata(self):
        result = answer_policy.generate_answer(self.provider, self.state)
        evidence = core.derive_from_control_state(self.state.read_view()).evidence_items[0].evidence
        citation = result.citations[0]
        self.assertEqual(citation["doc_id"], evidence.metadata["doc_id"])
        self.assertEqual(citation["title"], evidence.metadata["title"])
        self.assertEqual(citation["section"], evidence.metadata["section"])
        self.assertEqual(citation["version"], evidence.version)
        self.assertEqual(citation["ref"], self.ref)
        self.assertNotIn("content", citation)
        self.assertNotIn("metadata", citation)

    def test_stage6_keeps_its_citation_shape_and_does_not_emit_new_trace(self):
        calls = []
        self.provider.timeout = float("nan")
        result = core.generate_answer(self.provider, self.state, on_model_call=calls.append)
        self.assertEqual(set(result.citations[0]), {"ref", "producer", "source_type", "locator"})
        self.assertEqual(calls, [])
        self.assertEqual(self.provider.chat.call_args.kwargs["max_tokens"], 1024)

    def test_invalid_transport_timeout_stops_before_a_model_call(self):
        for timeout in (None, True, False, 0, -1, float("nan"), float("inf"), "180"):
            with self.subTest(timeout=timeout):
                self.provider.timeout = timeout
                calls = []
                with self.assertRaises(ValueError):
                    answer_policy.generate_answer(self.provider, self.state, on_model_call=calls.append)
                self.provider.chat.assert_not_called()
                self.assertEqual(calls, [])

    def test_timeout_validation_follows_the_product_guard_to_the_transport(self):
        wrapped = SimpleNamespace(_inner=SimpleNamespace(_inner=self.provider))
        self.provider.timeout = 13.5
        self.assertEqual(core.model_timeout_seconds(wrapped), 13.5)
        self.provider.timeout = float("inf")
        with self.assertRaises(ValueError):
            core.model_timeout_seconds(wrapped)

    def test_scripted_providers_without_transport_configuration_use_existing_default(self):
        self.assertEqual(core.model_timeout_seconds(SimpleNamespace(chat=lambda: None)), 180.0)
        self.assertEqual(core.model_timeout_seconds(mock.Mock()), 180.0)

    def test_timeout_wrapper_cycle_is_rejected(self):
        wrapped = SimpleNamespace()
        wrapped._inner = wrapped
        with self.assertRaises(ValueError):
            core.model_timeout_seconds(wrapped)

    def test_unknown_or_invalid_provider_usage_is_not_reported_as_zero(self):
        self.provider.chat.return_value = dataclasses.replace(
            self.response, prompt_tokens=None, completion_tokens=True, latency_seconds=float("nan"))
        calls = []
        answer_policy.generate_answer(self.provider, self.state, on_model_call=calls.append)
        self.assertIsNone(calls[0]["prompt_tokens"])
        self.assertIsNone(calls[0]["completion_tokens"])
        self.assertIsNone(calls[0]["total_tokens"])
        self.assertIsNone(calls[0]["latency_seconds"])

    def test_deepseek_http_request_keeps_token_limit_and_real_transport_timeout(self):
        provider = OpenAICompatibleProvider(base_url="https://offline.example.invalid", model="deepseek-flash",
                                             api_key="offline-test-key", timeout=23.5)
        response = mock.Mock()
        response.json.return_value = {
            "model": "deepseek-flash", "usage": {"prompt_tokens": 321, "completion_tokens": 45},
            "choices": [{"message": {"content": self.response.content}, "finish_reason": "stop"}],
        }
        with mock.patch("llm_provider.requests.post", return_value=response) as post:
            answer_policy.generate_answer(provider, self.state)
        self.assertEqual(post.call_args.kwargs["timeout"], 23.5)
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 1024)
        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "disabled"})

    def test_ollama_http_request_keeps_token_limit_and_real_transport_timeout(self):
        response = mock.Mock()
        response.json.return_value = {"message": {"content": self.response.content},
                                      "prompt_eval_count": 321, "eval_count": 45}
        with mock.patch("llm_provider.requests.post", return_value=response) as post:
            answer_policy.generate_answer(OllamaProvider(timeout=19.0), self.state)
        self.assertEqual(post.call_args.kwargs["timeout"], 19.0)
        self.assertEqual(post.call_args.kwargs["json"]["options"]["num_predict"], 1024)
        self.assertIs(post.call_args.kwargs["json"]["think"], False)

    def _pending_observation(self, sequence=1, error=False, pending=None, turn_index=1):
        observation_id = "turn:" + str(turn_index) + ":tool:" + str(sequence)
        result = (ToolResult(tool_name=PENDING_TOOL_NAME, status=ToolStatus.ERROR,
                             error_code="pending_read_failed", error_message="offline scripted failure",
                             trace={"observation_id": observation_id}) if error else
                  pending_tool_result({}, observation_id=observation_id, read_pending=lambda: pending or [],
                                      as_of=self.state.virtual_now))
        return core.ToolObservation(
            sequence=sequence, control_step=sequence, turn_index=turn_index, tool_step=sequence,
            observation_id=observation_id, tool_name=PENDING_TOOL_NAME, arguments={}, result=result)

    def _echo_source_answer(self, messages, **kwargs):
        data = json.loads(messages[-1]["content"])
        return dataclasses.replace(self.response, content=json.dumps({
            "answer": "本会话当前没有待审批申请。", "citation_refs": [source["ref"] for source in data["sources"]]}))

    def test_current_empty_pending_read_is_citable_without_changing_the_read(self):
        observation = self._pending_observation()
        current = state(observations=(observation,), tools=core.STAGE5_RUNTIME_TOOLS + (PENDING_TOOL_NAME,))
        self.provider.chat.side_effect = self._echo_source_answer
        result = answer_policy.generate_answer(self.provider, current)
        data = json.loads(self.provider.chat.call_args.args[0][-1]["content"])
        self.assertEqual(len(data["sources"]), 1)
        self.assertEqual(data["sources"][0]["producer"], PENDING_TOOL_NAME)
        self.assertIn('{"pending_requests":[]}', data["sources"][0]["content"])
        self.assertEqual(result.citations[0]["ref"], data["sources"][0]["ref"])
        self.assertIs(observation.result.status, ToolStatus.EMPTY)
        self.assertEqual(observation.result.evidence, ())
        self.assertTrue(observation.result_unchanged())
        self.assertNotIn("order_id", json.dumps(data["sources"]))
        self.assertNotIn("pending_action_id", json.dumps(data["sources"]))

    def test_latest_pending_error_does_not_reuse_an_earlier_empty_read(self):
        current = state(observations=(self._pending_observation(), self._pending_observation(2, error=True)),
                        tools=core.STAGE5_RUNTIME_TOOLS + (PENDING_TOOL_NAME,))
        self.provider.chat.side_effect = self._echo_source_answer
        answer_policy.generate_answer(self.provider, current)
        data = json.loads(self.provider.chat.call_args.args[0][-1]["content"])
        self.assertEqual(data["sources"], [])

    def test_a_historical_empty_reply_without_a_current_read_does_not_create_a_source(self):
        current = state(messages=("查一下申请", "现在进度怎么样？"), observations=())
        history = (decision_policy.EarlierReply(1, "answer", "本会话没有待审批申请。"),)
        self.provider.chat.side_effect = self._echo_source_answer
        answer_policy.generate_answer(self.provider, current, history)
        data = json.loads(self.provider.chat.call_args.args[0][-1]["content"])
        self.assertEqual(data["sources"], [])
        self.assertEqual(len(data["history_replies"]), 1)

    def test_stage6_does_not_adapt_an_empty_pending_observation(self):
        current = state(observations=(self._pending_observation(),),
                        tools=core.STAGE5_RUNTIME_TOOLS + (PENDING_TOOL_NAME,))
        self.provider.chat.side_effect = self._echo_source_answer
        core.generate_answer(self.provider, current)
        data = json.loads(self.provider.chat.call_args.args[0][-1]["content"])
        self.assertEqual(data["sources"], [])

    def _pending_request(self, pending_id="PA-FIRST"):
        return {"pending_action_id": pending_id, "action_type": "create_return",
                "order_id": "ORD-1001", "order_item_id": "OI-1001-2", "status": "WAITING_APPROVAL"}

    def _answer_sources(self, observations):
        current = state(messages=("申请进度怎么样？", "审批后再查一次"), observations=observations,
                        tools=core.STAGE5_RUNTIME_TOOLS + (PENDING_TOOL_NAME,))
        self.provider.chat.side_effect = self._echo_source_answer
        answer_policy.generate_answer(self.provider, current)
        return json.loads(self.provider.chat.call_args.args[0][-1]["content"])["sources"]

    def test_pending_ok_then_empty_removes_the_preapproval_source(self):
        old = self._pending_observation(pending=[self._pending_request()])
        latest = self._pending_observation(2, turn_index=2)
        sources = self._answer_sources((old, latest))
        self.assertEqual(len(sources), 1)
        self.assertIn('{"pending_requests":[]}', sources[0]["content"])
        self.assertNotIn("WAITING_APPROVAL", json.dumps(sources))
        self.assertNotIn(evidence_ref(old.result.evidence[0]), [source["ref"] for source in sources])
        self.assertTrue(old.result_unchanged())
        self.assertTrue(latest.result_unchanged())

    def test_pending_ok_then_error_removes_old_pending_but_preserves_other_sources(self):
        old = self._pending_observation(pending=[self._pending_request()])
        latest = self._pending_observation(2, error=True, turn_index=2)
        knowledge = knowledge_observation(3, KB_ARGS)
        sources = self._answer_sources((old, latest, knowledge))
        self.assertTrue(sources)
        self.assertTrue(all(source["producer"] == "search_knowledge_base" for source in sources))
        self.assertNotIn("WAITING_APPROVAL", json.dumps(sources))
        self.assertTrue(old.result_unchanged())
        self.assertTrue(latest.result_unchanged())

    def test_pending_ok_then_ok_uses_only_the_latest_result(self):
        old = self._pending_observation(pending=[self._pending_request()])
        latest = self._pending_observation(2, pending=[self._pending_request("PA-LATEST")], turn_index=2)
        sources = self._answer_sources((old, latest))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["ref"], evidence_ref(latest.result.evidence[0]))
        self.assertIn("PA-LATEST", sources[0]["content"])
        self.assertNotIn("PA-FIRST", json.dumps(sources))
        self.assertTrue(old.result_unchanged())
        self.assertTrue(latest.result_unchanged())

    def test_parse_rejects_a_superseded_pending_source_citation(self):
        old = self._pending_observation(pending=[self._pending_request()])
        latest = self._pending_observation(2, turn_index=2)
        current = state(observations=(old, latest), tools=core.STAGE5_RUNTIME_TOOLS + (PENDING_TOOL_NAME,))
        self.provider.chat.return_value = dataclasses.replace(self.response, content=json.dumps({
            "answer": "申请仍在等待审批。", "citation_refs": [evidence_ref(old.result.evidence[0])]}))
        with self.assertRaises(core.AnswerUnavailable) as error:
            answer_policy.generate_answer(self.provider, current)
        self.assertEqual(error.exception.code, "unknown_citation_ref")


if __name__ == "__main__":
    unittest.main()
