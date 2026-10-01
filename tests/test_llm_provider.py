"""LLMProvider unit tests. Every HTTP call is mocked; nothing touches the network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import llm_provider
import rag
from llm_provider import (
    LLMConfigError,
    OllamaProvider,
    OpenAICompatibleProvider,
    load_config,
    split_think,
)
from rag import Chunk


class FakeResponse:
    def __init__(self, body=None, *, lines=None, status=200, text=""):
        self.body = body
        self.lines = lines or []
        self.status = status
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Client Error")

    def json(self):
        return self.body

    def iter_lines(self, decode_unicode=False):
        assert decode_unicode
        return iter(self.lines)


def ollama_body(content, **extra):
    message = {"role": "assistant", "content": content}
    message.update(extra.pop("message_extra", {}))
    return {"message": message, "prompt_eval_count": 11, "eval_count": 7, "done": True,
            "done_reason": "stop", **extra}


def deepseek_body(content, **message_extra):
    return {
        "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content, **message_extra}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }


MESSAGES = [{"role": "system", "content": "你是助手。"}, {"role": "user", "content": "年假几天？"}]
SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


def deepseek():
    return OpenAICompatibleProvider(base_url="https://api.example.test", model="deepseek-flash",
                                    api_key="sk-test-secret-value")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dotenv = Path(self.directory.name) / ".env"

    def write_dotenv(self, text):
        self.dotenv.write_text(text, encoding="utf-8")

    def test_defaults_to_local_qwen_without_any_configuration(self):
        config = load_config(environ={}, dotenv_path=None)
        self.assertEqual((config.provider, config.base_url, config.model),
                         ("ollama", "http://localhost:11434", "qwen3:4b"))
        self.assertIsNone(config.api_key)

    def test_deepseek_settings_come_from_dotenv(self):
        self.write_dotenv(
            "# comment\nLLM_PROVIDER=deepseek\nDEEPSEEK_BASE_URL=https://api.deepseek.com/\n"
            'DEEPSEEK_MODEL="deepseek-flash"\nexport DEEPSEEK_API_KEY=sk-from-file\n'
        )
        config = load_config(environ={}, dotenv_path=self.dotenv)
        self.assertEqual(config.provider, "deepseek")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.model, "deepseek-flash")
        self.assertEqual(config.api_key, "sk-from-file")

    def test_environment_overrides_dotenv_for_non_secret_settings(self):
        self.write_dotenv("LLM_PROVIDER=deepseek\nOLLAMA_CHAT_MODEL=from-file\n")
        config = load_config(environ={"LLM_PROVIDER": "ollama", "OLLAMA_CHAT_MODEL": "from-env"},
                             dotenv_path=self.dotenv)
        self.assertEqual((config.provider, config.model), ("ollama", "from-env"))

    def test_api_key_is_read_from_dotenv_only(self):
        self.write_dotenv("DEEPSEEK_BASE_URL=https://api.deepseek.com\nDEEPSEEK_MODEL=deepseek-flash\n")
        with self.assertRaisesRegex(LLMConfigError, "DEEPSEEK_API_KEY"):
            load_config("deepseek", environ={"DEEPSEEK_API_KEY": "sk-env"}, dotenv_path=self.dotenv)

    def test_deepseek_has_no_hard_coded_defaults(self):
        with self.assertRaises(LLMConfigError) as caught:
            load_config("deepseek", environ={}, dotenv_path=None)
        for name in ("DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY"):
            self.assertIn(name, str(caught.exception))

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(LLMConfigError):
            load_config(environ={"LLM_PROVIDER": "gpt"}, dotenv_path=None)

    def test_empty_llm_dotenv_disables_the_file(self):
        self.write_dotenv("OLLAMA_CHAT_MODEL=from-file\n")
        self.assertEqual(load_config(environ={"LLM_DOTENV": ""}).model, "qwen3:4b")
        self.assertEqual(load_config(environ={"LLM_DOTENV": str(self.dotenv)}).model, "from-file")

    def test_api_key_never_appears_in_repr_or_public_dict(self):
        self.write_dotenv("DEEPSEEK_BASE_URL=u\nDEEPSEEK_MODEL=m\nDEEPSEEK_API_KEY=sk-very-secret\n")
        config = load_config("deepseek", environ={}, dotenv_path=self.dotenv)
        provider = llm_provider.create_provider(config)
        for text in (repr(config), repr(provider), json.dumps(config.public_dict())):
            self.assertNotIn("sk-very-secret", text)
        self.assertTrue(config.public_dict()["api_key_set"])


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        llm_provider.reset_provider()
        self.addCleanup(llm_provider.reset_provider)

    def test_get_provider_caches_until_reset(self):
        with patch.dict(os.environ, {"LLM_DOTENV": "", "LLM_PROVIDER": "ollama", "OLLAMA_CHAT_MODEL": "a"}):
            first = llm_provider.get_provider()
            self.assertIs(llm_provider.get_provider(), first)
        with patch.dict(os.environ, {"LLM_DOTENV": "", "LLM_PROVIDER": "ollama", "OLLAMA_CHAT_MODEL": "b"}):
            self.assertEqual(llm_provider.get_provider().model, "a")  # stale until reset
            llm_provider.reset_provider()
            self.assertEqual(llm_provider.get_provider().model, "b")

    def test_test_suite_runs_without_dotenv(self):
        # tests/__init__.py pins this so a developer's .env cannot leak into unit tests.
        self.assertEqual(os.environ.get("LLM_DOTENV"), "")
        self.assertEqual(llm_provider.get_provider().name, "ollama")


# --------------------------------------------------------------------------
# Think-content normalisation
# --------------------------------------------------------------------------


class SplitThinkTests(unittest.TestCase):
    def test_plain_content_is_untouched(self):
        self.assertEqual(split_think(' {"answer": "5天"} '), (' {"answer": "5天"} ', None))

    def test_answer_follows_the_last_closing_tag(self):
        self.assertEqual(split_think("<think>想一想</think>\n5天"), ("\n5天", "想一想"))

    def test_closing_tag_without_opening_tag(self):
        self.assertEqual(split_think("先分析</think>5天"), ("5天", "先分析"))

    def test_unterminated_think_block_is_removed(self):
        content, reasoning = split_think("5天<think>还没想完")
        self.assertEqual(content, "5天")
        self.assertEqual(reasoning, "还没想完")


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------


class OllamaProviderTests(unittest.TestCase):
    def call(self, body, **kwargs):
        with patch.object(llm_provider.requests, "post", return_value=FakeResponse(body)) as post:
            result = OllamaProvider().chat(MESSAGES, **kwargs)
        return result, post

    def test_request_bodies_match_the_pre_provider_calls(self):
        cases = [
            ({}, {"model": "qwen3:4b", "messages": MESSAGES, "stream": False, "think": False}),
            ({"response_format": "json"},
             {"model": "qwen3:4b", "messages": MESSAGES, "stream": False, "think": False, "format": "json"}),
            ({"response_format": SCHEMA, "temperature": 0},
             {"model": "qwen3:4b", "messages": MESSAGES, "stream": False, "think": False,
              "format": SCHEMA, "options": {"temperature": 0}}),
            ({"tools": [{"type": "function"}]},
             {"model": "qwen3:4b", "messages": MESSAGES, "tools": [{"type": "function"}],
              "stream": False, "think": False}),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                _, post = self.call(ollama_body("ok"), **kwargs)
                self.assertEqual(post.call_args.args, ("http://localhost:11434/api/chat",))
                self.assertEqual(post.call_args.kwargs["json"], expected)
                self.assertEqual(list(post.call_args.kwargs["json"]), list(expected))
                self.assertEqual(post.call_args.kwargs["timeout"], 180.0)

    def test_returns_content_usage_and_latency(self):
        result, _ = self.call(ollama_body("5天"))
        self.assertEqual(result.content, "5天")
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (11, 7))
        self.assertGreaterEqual(result.latency_seconds, 0.0)
        self.assertEqual((result.provider, result.model, result.finish_reason), ("ollama", "qwen3:4b", "stop"))

    def test_thinking_never_reaches_content(self):
        result, _ = self.call(ollama_body("<think>inline</think>5天", message_extra={"thinking": "field"}))
        self.assertEqual(result.content, "5天")
        self.assertEqual(result.reasoning, "field\ninline")

    def test_tool_calls_are_normalised(self):
        body = ollama_body("", message_extra={"tool_calls": [
            {"function": {"name": "search_knowledge_base", "arguments": {"query": "年假"}}}]})
        result, _ = self.call(body)
        self.assertEqual(result.tool_calls[0].name, "search_knowledge_base")
        self.assertEqual(result.tool_calls[0].arguments, {"query": "年假"})
        # Ollama sends parsed arguments and may omit an id.
        self.assertIsNone(result.tool_calls[0].id)
        self.assertIsNone(result.tool_calls[0].raw_arguments)

    def test_ollama_tool_call_id_is_kept_when_supplied(self):
        body = ollama_body("", message_extra={"tool_calls": [
            {"id": "call-o1", "function": {"name": "get_order", "arguments": {"order_id": "ORD-1001"}}}]})
        result, _ = self.call(body)
        self.assertEqual(result.tool_calls[0].id, "call-o1")
        self.assertEqual(result.tool_calls[0].arguments, {"order_id": "ORD-1001"})

    def test_http_errors_propagate_as_request_exceptions(self):
        with patch.object(llm_provider.requests, "post", return_value=FakeResponse(status=500)):
            with self.assertRaises(requests.RequestException):
                OllamaProvider().chat(MESSAGES)

    def stream(self, lines, **kwargs):
        encoded = [json.dumps(line, ensure_ascii=False) for line in lines]
        with patch.object(llm_provider.requests, "post", return_value=FakeResponse(lines=encoded)) as post:
            stream = OllamaProvider().chat_stream(MESSAGES, **kwargs)
            deltas = list(stream)
        return deltas, stream, post

    def test_stream_yields_content_and_reports_usage(self):
        deltas, stream, post = self.stream([
            {"message": {"thinking": "hmm"}},
            {"message": {"content": "5"}},
            {"message": {"content": "天"}},
            {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 2},
        ], response_format=SCHEMA, temperature=0)
        self.assertEqual(deltas, ["5", "天"])
        self.assertEqual((stream.response.prompt_tokens, stream.response.completion_tokens), (3, 2))
        self.assertEqual(stream.response.reasoning, "hmm")
        self.assertEqual(post.call_args.kwargs["json"], {
            "model": "qwen3:4b", "messages": MESSAGES, "stream": True, "think": False,
            "format": SCHEMA, "options": {"temperature": 0}})
        self.assertEqual(post.call_args.kwargs["timeout"], (10.0, 180.0))
        self.assertTrue(post.call_args.kwargs["stream"])

    def test_stream_raises_on_error_and_on_missing_done(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.stream([{"message": {"content": "x"}}, {"error": "boom"}])
        with self.assertRaisesRegex(RuntimeError, "意外中断"):
            self.stream([{"message": {"content": "x"}}])


# --------------------------------------------------------------------------
# DeepSeek (OpenAI-compatible)
# --------------------------------------------------------------------------


class DeepSeekProviderTests(unittest.TestCase):
    def call(self, body, messages=MESSAGES, **kwargs):
        with patch.object(llm_provider.requests, "post", return_value=FakeResponse(body)) as post:
            result = deepseek().chat(messages, **kwargs)
        return result, post

    def test_request_targets_chat_completions_with_thinking_disabled(self):
        _, post = self.call(deepseek_body("ok"), temperature=0)
        self.assertEqual(post.call_args.args, ("https://api.example.test/chat/completions",))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)
        self.assertFalse(body["stream"])
        self.assertNotIn("response_format", body)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test-secret-value")

    def test_temperature_is_omitted_when_not_requested(self):
        _, post = self.call(deepseek_body("ok"))
        self.assertNotIn("temperature", post.call_args.kwargs["json"])

    def test_schema_becomes_json_object_plus_recorded_instruction(self):
        original = json.loads(json.dumps(MESSAGES))
        result, post = self.call(deepseek_body('{"answer": "5天"}'), response_format=SCHEMA)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertTrue(body["messages"][0]["content"].startswith("你是助手。\n\n"))
        self.assertIn("JSON Schema", body["messages"][0]["content"])
        self.assertIn('"required":["answer"]', body["messages"][0]["content"])
        self.assertEqual(body["messages"][1:], MESSAGES[1:])
        self.assertEqual(MESSAGES, original, "the caller's logical messages must not be mutated")
        self.assertEqual(len(result.prompt_adaptations), 1)
        adaptation = result.prompt_adaptations[0]
        self.assertEqual(adaptation["reason"], "schema_not_supported_by_json_object")
        self.assertEqual(adaptation["position"], "appended_to_messages[0]")
        self.assertIn(adaptation["text"], body["messages"][0]["content"])

    def test_bare_json_request_is_adapted_only_when_prompt_lacks_json(self):
        with_word = [{"role": "system", "content": '只输出JSON {"ranking":[]}'}, MESSAGES[1]]
        result, post = self.call(deepseek_body('{"ranking":[1]}'), messages=with_word, response_format="json")
        self.assertEqual(post.call_args.kwargs["json"]["messages"], with_word)
        self.assertEqual(result.prompt_adaptations, ())
        result, post = self.call(deepseek_body("{}"), response_format="json")
        self.assertEqual(result.prompt_adaptations[0]["reason"], "json_object_requires_word_json")

    def test_instruction_becomes_a_system_message_when_none_exists(self):
        result, post = self.call(deepseek_body("{}"), messages=[MESSAGES[1]], response_format=SCHEMA)
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1], MESSAGES[1])
        self.assertEqual(result.prompt_adaptations[0]["position"], "new_system_message")

    def test_reasoning_content_is_separated_from_content(self):
        result, _ = self.call(deepseek_body("5天", reasoning_content="让我想想"))
        self.assertEqual(result.content, "5天")
        self.assertEqual(result.reasoning, "让我想想")
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (20, 5))
        self.assertEqual((result.provider, result.finish_reason), ("deepseek", "stop"))

    def test_tool_call_arguments_are_parsed(self):
        body = deepseek_body(None, tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"query": "年假"}'}},
            {"id": "c2", "type": "function", "function": {"name": "broken", "arguments": "{not json"}},
        ])
        result, _ = self.call(body, tools=[{"type": "function"}])
        self.assertEqual(result.content, "")
        self.assertEqual(result.tool_calls[0].arguments, {"query": "年假"})
        self.assertEqual(result.tool_calls[1].arguments, {})
        # The unparsed original survives even when parsing fails.
        self.assertEqual(result.tool_calls[1].raw_arguments, "{not json")
        self.assertEqual([call.id for call in result.tool_calls], ["c1", "c2"])

    def test_native_tool_call_identity_is_preserved(self):
        raw = '{"order_id":"ORD-1001"}'
        body = deepseek_body(None, tool_calls=[
            {"id": "call-real-123", "type": "function",
             "function": {"name": "get_order", "arguments": raw}}])
        result, _ = self.call(body, tools=[{"type": "function"}])
        call = result.tool_calls[0]
        self.assertEqual(call.id, "call-real-123")
        self.assertEqual(call.name, "get_order")
        self.assertEqual(call.arguments, {"order_id": "ORD-1001"})
        self.assertEqual(call.raw_arguments, raw)

    def test_tool_call_stays_backward_compatible(self):
        call = llm_provider.ToolCall(name="get_order", arguments={"order_id": "ORD-1001"})
        self.assertEqual((call.id, call.raw_arguments), (None, None))
        self.assertEqual(call, llm_provider.ToolCall("get_order", {"order_id": "ORD-1001"}))

    def test_http_error_carries_body_but_never_the_key(self):
        failing = FakeResponse(status=401, text='{"error":{"message":"Authentication Fails"}}')
        with patch.object(llm_provider.requests, "post", return_value=failing):
            with self.assertRaises(requests.HTTPError) as caught:
                deepseek().chat(MESSAGES)
        self.assertIn("Authentication Fails", str(caught.exception))
        self.assertNotIn("sk-test-secret-value", str(caught.exception))

    def stream(self, lines):
        with patch.object(llm_provider.requests, "post", return_value=FakeResponse(lines=lines)) as post:
            stream = deepseek().chat_stream(MESSAGES, response_format=SCHEMA, temperature=0)
            deltas = list(stream)
        return deltas, stream, post

    def test_stream_parses_sse_and_final_usage(self):
        chunk = lambda delta, finish=None: "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False)
        deltas, stream, post = self.stream([
            chunk({"role": "assistant", "content": ""}),
            chunk({"reasoning_content": "想"}),
            "",
            chunk({"content": '{"answer":"5'}),
            chunk({"content": '天"}'}, "stop"),
            "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}),
            "data: [DONE]",
        ])
        self.assertEqual(deltas, ['{"answer":"5', '天"}'])
        self.assertEqual((stream.response.prompt_tokens, stream.response.completion_tokens), (9, 4))
        self.assertEqual(stream.response.reasoning, "想")
        self.assertEqual(stream.response.finish_reason, "stop")
        self.assertEqual(post.call_args.kwargs["json"]["stream_options"], {"include_usage": True})

    def test_stream_without_done_marker_raises(self):
        with self.assertRaisesRegex(RuntimeError, "意外中断"):
            self.stream(["data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]})])


# --------------------------------------------------------------------------
# Business layer sees one format
# --------------------------------------------------------------------------


class BusinessLayerTests(unittest.TestCase):
    RESULTS = [(Chunk(text="## 年假\n员工享有5天年假", source="rules.md", index=1), 1.0)]

    def setUp(self):
        llm_provider.reset_provider()
        self.addCleanup(llm_provider.reset_provider)

    def test_answer_structured_works_through_deepseek(self):
        with patch.object(llm_provider, "get_provider", return_value=deepseek()), \
                patch.object(llm_provider.requests, "post",
                             return_value=FakeResponse(deepseek_body('{"answer": "年假为5天。[来源1]"}'))) as post:
            reply = rag.answer_structured("年假几天？", self.RESULTS, [])
        self.assertEqual(reply, "年假为5天。[来源1]")
        self.assertEqual(post.call_args.kwargs["json"]["response_format"], {"type": "json_object"})

    def test_rag_extraction_sees_the_raw_model_text(self):
        # Main's extract_answer_text parses the JSON envelope *before* stripping
        # think shells, so a literal "<think>" inside an answer must reach it intact.
        answer = "年假为5天。标签写作<think>，不是思考内容。[来源1]"
        body = ollama_body(json.dumps({"answer": answer}, ensure_ascii=False))
        with patch.object(llm_provider.requests, "post", return_value=FakeResponse(body)):
            response = OllamaProvider().chat(MESSAGES, response_format=SCHEMA)
            self.assertEqual(json.loads(response.raw_content)["answer"], answer)
            self.assertNotEqual(response.content, response.raw_content)  # content keeps the old semantics
            reply = rag.answer_structured("年假几天？", self.RESULTS, [])
        self.assertIn("<think>", reply)

    def test_answer_strips_qwen_think_through_the_provider(self):
        with patch.object(llm_provider.requests, "post",
                          return_value=FakeResponse(ollama_body("<think>分析</think>\n年假为5天。[来源1]"))):
            self.assertEqual(rag.answer("年假几天？", self.RESULTS, []), "年假为5天。[来源1]")


if __name__ == "__main__":
    unittest.main()
