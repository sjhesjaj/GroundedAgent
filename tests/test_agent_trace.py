"""Stage 1 trace tests. Every model call is mocked; nothing reaches the network.

Each scenario asserts against a *fresh* SQLite connection, never the in-memory
recorder: the point is that the persisted rows alone replay the request.
"""

import asyncio
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.concurrency import iterate_in_threadpool

import agent_trace
import api
import chat_orchestration
import llm_provider
import rag
import wiki_runtime
from rag import Chunk
from storage import SQLiteStorage

CLIENT = "client-trace"
LEAVE_CHUNK = Chunk(
    text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假。",
    source="sample_company_rules.md",
    index=1,
)


class FakeResponse:
    def __init__(self, body=None, lines=None):
        self.body, self.lines = body, lines or []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        return None

    def json(self):
        return self.body

    def iter_lines(self, decode_unicode=False):
        return iter(json.dumps(line, ensure_ascii=False) for line in self.lines)


def ollama_answer(answer, prompt_tokens=120, completion_tokens=30):
    return FakeResponse({
        "message": {"role": "assistant", "content": json.dumps({"answer": answer}, ensure_ascii=False)},
        "prompt_eval_count": prompt_tokens, "eval_count": completion_tokens,
        "done": True, "done_reason": "stop",
    })


def ollama_stream(answer, *, fail_after=None):
    text = json.dumps({"answer": answer}, ensure_ascii=False)
    pieces = [text[:8], text[8:16], text[16:]]
    lines = [{"message": {"content": piece}} for piece in pieces]
    if fail_after is not None:
        return FakeResponse(lines=lines[:fail_after] + [{"error": "model crashed"}])
    return FakeResponse(lines=lines + [{"message": {"content": ""}, "done": True,
                                        "prompt_eval_count": 88, "eval_count": 12,
                                        "done_reason": "stop"}])


class TraceTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.original_storage = api.storage
        api.storage = SQLiteStorage(Path(self.directory.name) / "app.db")
        self.addCleanup(setattr, api, "storage", self.original_storage)
        with api.state_lock:
            api.chunks.clear()
            api.chunks.append(LEAVE_CHUNK)
            api.conversation_locks.clear()
        patch.dict(os.environ, {"TRACE_ENABLED": "1"}).start()
        patch.object(wiki_runtime, "RUNTIME",
                     wiki_runtime.WikiRuntime(root=Path(self.directory.name) / "wiki")).start()
        # Retrieval would call Ollama for embeddings; its result is fixed here.
        self.retrieval = patch(
            "orchestration.document_adapter.retrieve_fast",
            side_effect=lambda question, chunks, top_k=4, trace=None: [(chunks[0], 3.5)] if chunks else [],
        ).start()
        llm_provider.reset_provider()
        self.addCleanup(llm_provider.reset_provider)
        self.addCleanup(patch.stopall)
        self.addCleanup(self.clear_state)
        self.client = TestClient(api.create_app(v1_routes=True), raise_server_exceptions=False)

    def clear_state(self):
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()

    # -- helpers ---------------------------------------------------------------------

    def post(self, question, session, *, mode="orchestrated", stream=False):
        url = "/api/chat/stream" if stream else "/api/chat"
        return self.client.post(url, json={"question": question, "session_id": session,
                                           "client_id": CLIENT, "mode": mode})

    def query(self, sql, params=()):
        connection = sqlite3.connect(api.storage.path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
        finally:
            connection.close()

    def run_row(self, run_id):
        rows = self.query("SELECT * FROM trace_runs WHERE run_id = ?", (run_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def run_for_session(self, session):
        rows = self.query("SELECT * FROM trace_runs WHERE session_id = ?", (session,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def spans(self, run_id):
        return self.query("SELECT * FROM trace_spans WHERE run_id = ? ORDER BY seq", (run_id,))

    def top_level(self, run_id):
        """(stage, name) of business spans in execution order."""
        spans = [s for s in self.spans(run_id) if s["parent_span_id"] is None]
        return [(s["stage"], s["name"]) for s in sorted(spans, key=lambda s: (s["offset_ms"], s["seq"]))]

    def children(self, run_id, parent_name):
        spans = self.spans(run_id)
        parent = next(s for s in spans if s["name"] == parent_name)
        return [s for s in spans if s["parent_span_id"] == parent["span_id"]]

    def sse_events(self, response):
        events = []
        for block in response.text.strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines())
            events.append((lines["event"], json.loads(lines["data"])))
        return events


# --------------------------------------------------------------------------
# Acceptance scenarios
# --------------------------------------------------------------------------


class SuccessfulRequestTests(TraceTestCase):
    def test_plain_request_replays_every_stage_from_sqlite(self):
        with patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")):
            response = self.post("年假最多可以休多少天", "s-ok")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"answer", "trace", "sources", "route", "steps"})
        run_id = response.headers["X-Run-Id"]

        run = self.run_row(run_id)
        self.assertEqual(run["status"], "completed")
        self.assertIsNone(run["failed_stage"])
        self.assertEqual((run["kind"], run["entrypoint"], run["mode"], run["streaming"]),
                         ("api", "/api/chat", "orchestrated", 0))
        self.assertEqual((run["session_id"], run["client_id"], run["question"]),
                         ("s-ok", CLIENT, "年假最多可以休多少天"))
        self.assertEqual((run["provider"], run["model"]), ("ollama", "qwen3:4b"))
        self.assertRegex(run["git_commit"] or "", r"^[0-9a-f]{40}$")
        self.assertIsNotNone(run["started_at"])
        self.assertIsNotNone(run["finished_at"])
        self.assertGreater(run["duration_ms"], 0)
        retriever = json.loads(run["retriever_config_json"])
        self.assertEqual(retriever["bm25_fast_path"], {"min_top_score": 3.0, "min_ratio_to_second": 1.5})
        self.assertEqual(retriever["split_text"], {"size": 220, "overlap": 40})
        self.assertEqual((run["llm_calls"], run["prompt_tokens"], run["completion_tokens"]), (1, 120, 30))
        self.assertEqual(run["error_span_count"], 0)
        self.assertEqual(len(json.loads(run["prompt_hashes_json"])), 1)

        self.assertEqual(self.top_level(run_id), [
            ("planner", "plan_request"), ("planner", "availability_check"),
            ("tool_call", "execute_plan"), ("evidence", "evaluate_evidence"),
            ("generation", "answer_structured"), ("commit", "commit_exchange"),
        ])
        spans = {s["name"]: s for s in self.spans(run_id)}
        plan = json.loads(spans["plan_request"]["output_json"])
        self.assertEqual((plan["route"], plan["steps"]), ("document_only", ["document_search"]))

        tool = spans["document_search"]
        self.assertEqual(tool["parent_span_id"], spans["execute_plan"]["span_id"])
        self.assertEqual((tool["stage"], tool["status"]), ("tool_call", "ok"))
        self.assertEqual(json.loads(tool["input_json"])["arguments"],
                         {"question": "年假最多可以休多少天", "top_k": 4, "chunks": 1})
        result = json.loads(tool["output_json"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence"][0]["locator"], "chunk:1")
        self.assertIsNotNone(tool["latency_ms"])

        evidence = json.loads(spans["evaluate_evidence"]["output_json"])
        self.assertEqual(evidence["outcome"], "ready")
        self.assertEqual(len(evidence["usable_evidence"]), 1)

        llm = next(s for s in self.spans(run_id) if s["stage"] == "llm_call")
        generation = next(s for s in self.spans(run_id) if s["stage"] == "generation")
        self.assertEqual(llm["parent_span_id"], generation["span_id"])
        self.assertEqual((llm["name"], llm["provider"], llm["model"]), ("answer_structured", "ollama", "qwen3:4b"))
        self.assertEqual((llm["prompt_tokens"], llm["completion_tokens"]), (120, 30))
        attributes = json.loads(llm["attributes_json"])
        self.assertEqual(attributes["logical_prompt_sha256"], attributes["effective_prompt_sha256"])
        self.assertEqual(attributes["prompt_adaptations"], [])
        self.assertIn(attributes["system_prompt_sha256"], json.loads(run["prompt_hashes_json"]))
        self.assertEqual(json.loads(generation["output_json"]), {"answer": "年假为 5 天。[来源 1]"})

    def test_multi_step_tool_calls_are_recorded_in_order(self):
        question = "请假制度原文怎么写的，另外 SKU-A100 还有多少库存？"
        with patch.object(requests, "post", return_value=ollama_answer("见[来源 1][来源 2]")):
            response = self.post(question, "s-multi")
        self.assertEqual(response.status_code, 200)
        run_id = response.headers["X-Run-Id"]
        tools = sorted(self.children(run_id, "execute_plan"), key=lambda s: s["offset_ms"])
        self.assertEqual([t["name"] for t in tools], ["document_search", "system_query"])
        self.assertTrue(all(t["stage"] == "tool_call" and t["latency_ms"] is not None for t in tools))
        self.assertLessEqual(tools[0]["offset_ms"], tools[1]["offset_ms"])
        system = tools[1]
        self.assertEqual(system["status"], "ok")
        arguments = json.loads(system["input_json"])["arguments"]
        # Business parameters keep their real value; subject_id is absent (None).
        self.assertEqual(arguments["parameters"], {"sku": "sku-a100"})
        self.assertEqual(arguments["operation"], "get_inventory_level")
        self.assertIsNone(arguments["subject_id"])
        self.assertEqual(json.loads(system["output_json"])["evidence"][0]["source_type"], "system")
        plan = json.loads(next(s for s in self.spans(run_id) if s["name"] == "plan_request")["output_json"])
        self.assertEqual(plan["steps"], ["document_search", "system_query"])

    def test_wiki_and_document_route_records_both_tools(self):
        with patch.object(requests, "post", return_value=ollama_answer("见[来源 1]")):
            response = self.post("介绍一下账号与权限，并说明高权限账号多少天复核一次。", "s-wiki")
        run_id = response.headers["X-Run-Id"]
        self.assertEqual(sorted(t["name"] for t in self.children(run_id, "execute_plan")),
                         ["document_search", "wiki_query"])


class ToolFailureTests(TraceTestCase):
    def test_tool_exception_is_an_error_span_in_a_completed_run(self):
        self.retrieval.side_effect = RuntimeError("index offline")
        with patch.object(requests, "post") as post:
            response = self.post("年假最多可以休多少天", "s-toolfail")
        post.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], chat_orchestration.MESSAGE_TOOL_UNAVAILABLE)
        run_id = response.headers["X-Run-Id"]
        run = self.run_row(run_id)
        # The request completed normally (a fixed refusal); the failure is a span fact.
        self.assertEqual(run["status"], "completed")
        self.assertIsNone(run["failed_stage"])
        self.assertEqual(run["error_span_count"], 1)
        tool = next(s for s in self.spans(run_id) if s["name"] == "document_search")
        self.assertEqual((tool["status"], tool["error_type"], tool["error_code"]),
                         ("error", "RuntimeError", "tool_execution_failed"))
        self.assertEqual(tool["error_message"], "document_search failed with RuntimeError")
        evidence = json.loads(next(s for s in self.spans(run_id) if s["stage"] == "evidence")["output_json"])
        self.assertEqual(evidence["outcome"], "refuse")
        self.assertIn("tool_error", evidence["reason_codes"])
        self.assertNotIn(("generation", "answer_structured"), self.top_level(run_id))

    def test_executor_programmer_error_fails_the_run_at_tool_call(self):
        self.retrieval.side_effect = ValueError("bad adapter call")
        response = self.post("年假最多可以休多少天", "s-toolcrash")
        self.assertEqual(response.status_code, 500)
        run = self.run_for_session("s-toolcrash")
        self.assertEqual((run["status"], run["failed_stage"], run["error_type"]),
                         ("failed", "tool_call", "ValueError"))
        failed = next(s for s in self.spans(run["run_id"]) if s["span_id"] == run["failed_span_id"])
        self.assertEqual(failed["name"], "execute_plan")
        self.assertIn("ValueError", failed["error_traceback"])
        self.assertIn("bad adapter call", run["error_message"])


class GenerationFailureTests(TraceTestCase):
    def test_model_failure_fails_the_run_at_generation(self):
        error = requests.ConnectionError("refused; Authorization: Bearer abcdefghijklmnop123")
        with patch.object(requests, "post", side_effect=error):
            response = self.post("年假最多可以休多少天", "s-genfail")
        self.assertEqual(response.status_code, 500)
        run = self.run_for_session("s-genfail")
        self.assertEqual((run["status"], run["failed_stage"], run["error_type"]),
                         ("failed", "generation", "ConnectionError"))
        self.assertNotIn("abcdefghijklmnop123", run["error_message"])
        self.assertIn(agent_trace.REDACTED, run["error_message"])
        spans = self.spans(run["run_id"])
        failed = next(s for s in spans if s["span_id"] == run["failed_span_id"])
        self.assertEqual((failed["stage"], failed["name"], failed["status"]), ("llm_call", "answer_structured", "error"))
        self.assertNotIn("abcdefghijklmnop123", failed["error_traceback"])
        generation = next(s for s in spans if s["stage"] == "generation")
        self.assertEqual((generation["status"], failed["parent_span_id"]), ("error", generation["span_id"]))
        # Everything before generation still completed and is on record.
        self.assertTrue(all(s["status"] == "ok" for s in spans if s["stage"] in ("planner", "evidence")))
        self.assertNotIn("commit", [s["stage"] for s in spans])

    def test_error_handled_inside_a_tool_is_not_blamed_for_a_later_failure(self):
        # Reproduces a real run: the model server is down, so retrieval's rerank
        # fails and rag falls back to the unranked candidates (handled), then
        # generation fails for real. The failure point is generation, not rerank.
        self.retrieval.side_effect = lambda question, chunks, top_k=4, trace=None: rag.rerank(
            question, [(chunks[0], 1.0)], top_k=1)
        with patch.object(requests, "post", side_effect=requests.ConnectionError("model server down")):
            response = self.post("年假最多可以休多少天", "s-handled")
        self.assertEqual(response.status_code, 500)
        run = self.run_for_session("s-handled")
        self.assertEqual((run["failed_stage"], run["error_type"]), ("generation", "ConnectionError"))
        spans = self.spans(run["run_id"])
        failed = next(s for s in spans if s["span_id"] == run["failed_span_id"])
        self.assertEqual((failed["stage"], failed["name"]), ("llm_call", "answer_structured"))
        rerank = next(s for s in spans if s["name"] == "rerank")
        document = next(s for s in spans if s["name"] == "document_search")
        self.assertEqual((rerank["status"], rerank["error_type"]), ("error", "ConnectionError"))
        self.assertEqual(rerank["parent_span_id"], document["span_id"])
        self.assertIn("model server down", rerank["error_message"])
        self.assertEqual(document["status"], "ok")  # the tool itself recovered

    def test_commit_conflict_fails_at_commit(self):
        with patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")), \
                patch.object(api, "commit_exchange", side_effect=HTTPException(409, "知识库已更新")):
            response = self.post("年假最多可以休多少天", "s-commit")
        self.assertEqual(response.status_code, 409)
        run = self.run_row(response.headers["X-Run-Id"])  # error responses carry the id too
        self.assertEqual((run["status"], run["failed_stage"], run["error_type"]),
                         ("failed", "commit", "HTTPException"))


class StreamingTests(TraceTestCase):
    def test_streaming_run_uses_the_same_model(self):
        with patch.object(requests, "post", return_value=ollama_stream("年假为 5 天。[来源 1]")):
            response = self.post("年假最多可以休多少天", "s-stream", stream=True)
        events = self.sse_events(response)
        self.assertEqual(events[-1][0], "done")
        run_id = response.headers["X-Run-Id"]
        run = self.run_row(run_id)
        self.assertEqual((run["status"], run["streaming"], run["entrypoint"]),
                         ("completed", 1, "/api/chat/stream"))
        self.assertEqual((run["prompt_tokens"], run["completion_tokens"]), (88, 12))
        self.assertEqual(self.top_level(run_id), [
            ("planner", "plan_request"), ("planner", "availability_check"),
            ("tool_call", "execute_plan"), ("evidence", "evaluate_evidence"),
            ("generation", "answer_stream"), ("commit", "commit_exchange"),
        ])
        llm = next(s for s in self.spans(run_id) if s["stage"] == "llm_call")
        attributes = json.loads(llm["attributes_json"])
        self.assertTrue(attributes["streaming"])
        self.assertEqual(attributes["delta_count"], 3)
        self.assertIn("first_delta_ms", attributes)
        self.assertEqual(json.loads(llm["output_json"])["content"], json.dumps({"answer": "年假为 5 天。[来源 1]"}, ensure_ascii=False))

    def test_stream_failure_mid_generation_is_finalized(self):
        with patch.object(requests, "post", return_value=ollama_stream("年假为 5 天。", fail_after=2)):
            response = self.post("年假最多可以休多少天", "s-streamfail", stream=True)
        events = self.sse_events(response)
        self.assertEqual(events[-1], ("error", chat_orchestration.STREAM_ERROR_EVENT))
        run = self.run_row(response.headers["X-Run-Id"])
        self.assertEqual((run["status"], run["failed_stage"], run["error_type"]),
                         ("failed", "generation", "RuntimeError"))
        self.assertTrue(json.loads(run["attributes_json"])["sse_error_event"])
        failed = next(s for s in self.spans(run["run_id"]) if s["span_id"] == run["failed_span_id"])
        self.assertEqual((failed["stage"], failed["name"]), ("llm_call", "answer_stream"))
        self.assertIn("model crashed", failed["error_message"])

    def test_legacy_stream_is_traced(self):
        with patch.object(api, "retrieve_fast", return_value=[(LEAVE_CHUNK, 3.5)]), \
                patch.object(requests, "post", return_value=ollama_stream("年假为 5 天。[来源 1]")):
            response = self.post("年假有几天", "s-legacystream", mode="legacy", stream=True)
        run_id = response.headers["X-Run-Id"]
        self.assertEqual(self.run_row(run_id)["status"], "completed")
        self.assertEqual([stage for stage, _ in self.top_level(run_id)],
                         ["router", "tool_call", "evidence", "generation", "commit"])


class LegacyModeTests(TraceTestCase):
    def test_rule_routed_legacy_request(self):
        with patch.object(api, "retrieve_fast", return_value=[(LEAVE_CHUNK, 3.5)]), \
                patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")):
            response = self.post("年假有几天", "s-legacy", mode="legacy")
        self.assertEqual(set(response.json()), {"answer", "trace", "sources"})
        run_id = response.headers["X-Run-Id"]
        self.assertEqual(self.top_level(run_id), [
            ("router", "decide_action"), ("tool_call", "search_knowledge_base"),
            ("evidence", "retrieved_sources"), ("generation", "answer_structured"),
            ("commit", "commit_exchange"),
        ])
        router = next(s for s in self.spans(run_id) if s["name"] == "decide_action")
        self.assertEqual(json.loads(router["output_json"])["tool"], "search_knowledge_base")
        tool = next(s for s in self.spans(run_id) if s["name"] == "search_knowledge_base")
        self.assertEqual(json.loads(tool["input_json"])["arguments"], {"query": "年假有几天"})

    def test_model_routed_legacy_request_records_the_router_llm_call(self):
        router_reply = FakeResponse({"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search_knowledge_base", "arguments": {"query": "天气"}}}]},
            "prompt_eval_count": 50, "eval_count": 5})
        with patch.object(api, "retrieve_fast", return_value=[(LEAVE_CHUNK, 3.5)]), \
                patch.object(requests, "post", side_effect=[router_reply, ollama_answer("资料只涉及制度。[来源 1]")]):
            response = self.post("今天天气怎么样", "s-llmrouter", mode="legacy")
        run_id = response.headers["X-Run-Id"]
        llm = self.children(run_id, "decide_action")
        self.assertEqual([(s["stage"], s["name"]) for s in llm], [("llm_call", "decide_action")])
        call_input = json.loads(llm[0]["input_json"])
        self.assertEqual(call_input["tools"], ["search_knowledge_base", "list_knowledge_sources",
                                               "summarize_knowledge_base"])
        self.assertEqual(json.loads(llm[0]["output_json"])["tool_calls"],
                         [{"name": "search_knowledge_base", "arguments": {"query": "天气"}}])
        self.assertEqual(self.run_row(run_id)["llm_calls"], 2)

    def test_lock_contention_is_recorded_with_the_run_id_header(self):
        lock = api.acquire_conversation("s-busy")
        try:
            response = self.post("年假有几天", "s-busy", mode="legacy")
        finally:
            lock.release()
        self.assertEqual(response.status_code, 409)
        run = self.run_row(response.headers["X-Run-Id"])
        self.assertEqual((run["status"], run["failed_stage"], run["error_type"]),
                         ("failed", "request", "HTTPException"))


class SwitchAndRobustnessTests(TraceTestCase):
    def test_trace_disabled_writes_nothing_and_changes_nothing(self):
        with patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")):
            traced = self.post("年假最多可以休多少天", "s-on").json()
            with patch.dict(os.environ, {"TRACE_ENABLED": "0"}):
                response = self.post("年假最多可以休多少天", "s-off")
        self.assertNotIn("X-Run-Id", response.headers)
        untraced = response.json()
        for key in ("answer", "sources", "route", "steps"):
            self.assertEqual(untraced[key], traced[key])
        self.assertEqual(set(untraced["trace"]), set(traced["trace"]))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM trace_runs WHERE session_id = 's-off'")[0]["n"], 0)

    def test_unwritable_trace_store_never_fails_the_request(self):
        with patch.object(agent_trace.TraceStore, "insert_run", side_effect=sqlite3.OperationalError("locked")), \
                patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")), \
                self.assertLogs("agent_trace", level="ERROR") as logs:
            response = self.post("年假最多可以休多少天", "s-broken")
        self.assertIn("could not record run start", logs.output[0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "年假为 5 天。[来源 1]")

    def test_failed_finalize_write_never_fails_the_request(self):
        with patch.object(agent_trace.TraceStore, "finalize", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("agent_trace", level="ERROR") as logs:
            with patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")):
                plain = self.post("年假最多可以休多少天", "s-finalize")
            with patch.object(requests, "post", return_value=ollama_stream("年假为 5 天。[来源 1]")):
                streamed = self.post("年假最多可以休多少天", "s-finalize-stream", stream=True)
        self.assertEqual((plain.status_code, plain.json()["answer"]), (200, "年假为 5 天。[来源 1]"))
        self.assertEqual(self.sse_events(streamed)[-1][0], "done")
        self.assertTrue(all("could not finalize" in line for line in logs.output))
        # The start row exists; the run simply never reached its final state.
        self.assertEqual(self.run_row(plain.headers["X-Run-Id"])["status"], "running")

    def test_a_bug_in_trace_recording_never_fails_the_request(self):
        with patch.object(chat_orchestration, "_trace_bundle", side_effect=RuntimeError("recorder bug")), \
                patch.object(agent_trace, "_prompt_facts", side_effect=RuntimeError("recorder bug")), \
                patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")), \
                self.assertLogs("agent_trace", level="ERROR"):
            response = self.post("年假最多可以休多少天", "s-recorderbug")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "年假为 5 天。[来源 1]")
        self.assertEqual(self.run_row(response.headers["X-Run-Id"])["status"], "completed")

    def test_no_active_run_leaves_the_provider_unwrapped(self):
        provider = llm_provider.get_provider()
        self.assertIs(llm_provider.get_provider(), provider)
        self.assertNotIsInstance(provider, agent_trace.TracedProvider)

    def test_cli_replays_a_run_from_the_database(self):
        with patch.object(requests, "post", return_value=ollama_answer("年假为 5 天。[来源 1]")):
            run_id = self.post("年假最多可以休多少天", "s-cli").headers["X-Run-Id"]
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent_trace.main(["--db", str(api.storage.path), "show", run_id])
        self.assertEqual(code, 0)
        text = output.getvalue()
        for fragment in ("completed", "[planner] plan_request", "[tool_call] document_search",
                         "[llm_call] answer_structured", "tokens=120+30", "[commit] commit_exchange"):
            self.assertIn(fragment, text)


class SecretLeakTests(TraceTestCase):
    """End to end: a real DeepSeek-shaped provider with a key, then scan every stored byte."""

    KEY = "sk-livekeyABCDEF1234567890xyz"
    PLAIN_SECRET = "plain-secret-value-9f8e7d"  # no sk- shape: caught only by literal redaction

    def setUp(self):
        super().setUp()
        provider = llm_provider.OpenAICompatibleProvider(
            base_url="https://api.example.test", model="deepseek-flash", api_key=self.KEY)
        patch.object(llm_provider, "_provider", provider).start()
        patch.object(agent_trace, "_known_secrets", (self.PLAIN_SECRET,)).start()

    def stored_text(self) -> str:
        rows = self.query("SELECT * FROM trace_runs") + self.query("SELECT * FROM trace_spans")
        return json.dumps(rows, ensure_ascii=False)

    def assert_no_secrets(self):
        text = self.stored_text()
        for secret in (self.KEY, self.PLAIN_SECRET, "Bearer " + self.KEY[:12]):
            self.assertNotIn(secret, text)
        self.assertIn(agent_trace.REDACTED, text)

    def test_successful_deepseek_request_stores_no_key(self):
        body = {"model": "deepseek-flash", "usage": {"prompt_tokens": 40, "completion_tokens": 9},
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps({"answer": "年假为 5 天。[来源 1]"}, ensure_ascii=False)}}]}
        question = f"年假最多可以休多少天？我的 key 是 {self.KEY}，口令 {self.PLAIN_SECRET}"
        with patch.object(requests, "post", return_value=FakeResponse(body)) as post:
            response = self.post(question, "s-leak-ok")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.KEY, post.call_args.kwargs["headers"]["Authorization"])  # it really was sent
        run = self.run_row(response.headers["X-Run-Id"])
        self.assertEqual((run["provider"], run["model"]), ("deepseek", "deepseek-flash"))
        llm = next(s for s in self.spans(run["run_id"]) if s["stage"] == "llm_call")
        self.assertEqual(json.loads(llm["attributes_json"])["prompt_adaptations"][0]["reason"],
                         "schema_not_supported_by_json_object")
        self.assert_no_secrets()

    def test_provider_error_echoing_the_key_is_redacted(self):
        class Unauthorized(FakeResponse):
            text = '{"error": {"message": "Invalid key sk-livekeyABCDEF1234567890xyz"}}'

            def raise_for_status(self):
                raise requests.HTTPError("401 Client Error: Unauthorized")

        with patch.object(requests, "post", return_value=Unauthorized()):
            response = self.post("年假最多可以休多少天", "s-leak-err")
        self.assertEqual(response.status_code, 500)
        run = self.run_for_session("s-leak-err")
        self.assertEqual((run["failed_stage"], run["error_type"]), ("generation", "HTTPError"))
        self.assertIn("Invalid key", run["error_message"])
        self.assert_no_secrets()


# --------------------------------------------------------------------------
# Recorder units
# --------------------------------------------------------------------------


class SanitizerTests(unittest.TestCase):
    def test_sensitive_keys_are_redacted_and_token_counts_kept(self):
        clean = agent_trace.sanitize({
            "api_key": "k1", "Authorization": "Bearer x", "password": "p", "client_secret": "s",
            "access-token": "t", "subject_id": "u-1", "headers": {"cookie": "c"},
            "prompt_tokens": 10, "completion_tokens": 3, "max_tokens": 50, "sku": "sku-a100",
            "empty_subject": {"subject_id": None},
        })
        for key in ("api_key", "Authorization", "password", "client_secret", "access-token", "subject_id"):
            self.assertEqual(clean[key], agent_trace.REDACTED, key)
        self.assertEqual(clean["headers"]["cookie"], agent_trace.REDACTED)
        self.assertEqual((clean["prompt_tokens"], clean["completion_tokens"], clean["max_tokens"]), (10, 3, 50))
        self.assertEqual(clean["sku"], "sku-a100")
        self.assertIsNone(clean["empty_subject"]["subject_id"])

    def test_secret_looking_values_are_redacted_inside_text(self):
        text = agent_trace.sanitize("key sk-abcdefghijklmnopqrstuv and Bearer abc.def-ghi_jkl here")
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", text)
        self.assertNotIn("abc.def-ghi_jkl", text)

    def test_truncation_bounds_text_and_lists(self):
        clean = agent_trace.sanitize({"text": "x" * 50, "items": list(range(10))}, max_text=10, max_items=3)
        self.assertTrue(clean["text"].startswith("x" * 10) and "truncated 40 chars" in clean["text"])
        self.assertEqual(clean["items"][:3], [0, 1, 2])
        self.assertIn("7 more items", clean["items"][3])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "traces.sqlite"
        patch.dict(os.environ, {"TRACE_ENABLED": "1"}).start()
        self.addCleanup(patch.stopall)

    def new_run(self, **kwargs):
        return agent_trace.start_run(self.db, kind="eval", entrypoint="test", question="q", **kwargs)

    def test_api_runs_truncate_and_eval_runs_keep_full_text(self):
        for truncate, expect_full in ((True, False), (False, True)):
            run = self.new_run(truncate=truncate)
            with run:
                with agent_trace.span("tool_call", "document_search") as span:
                    span.output = {"content": "长" * 6000}
            stored = agent_trace.load_trace(self.db, run.run_id)["spans"][0]["output"]["content"]
            self.assertEqual(len(stored) == 6000, expect_full, truncate)

    def test_finish_is_idempotent_and_nested_failure_names_the_outer_stage(self):
        run = self.new_run()
        with self.assertRaises(KeyError):
            with run:
                with agent_trace.span("generation", "answer_structured"):
                    with agent_trace.span("llm_call", "answer_structured"):
                        raise KeyError("boom")
        run.finish()
        trace = agent_trace.load_trace(self.db, run.run_id)
        self.assertEqual((trace["run"]["status"], trace["run"]["failed_stage"]), ("failed", "generation"))
        failed = next(s for s in trace["spans"] if s["span_id"] == trace["run"]["failed_span_id"])
        self.assertEqual(failed["stage"], "llm_call")
        self.assertEqual(len(trace["spans"]), 2)

    def test_failure_point_is_matched_by_exception_identity(self):
        run = self.new_run()
        with self.assertRaises(ConnectionError):
            with run:
                with agent_trace.span("tool_call", "document_search"):
                    try:
                        with agent_trace.span("llm_call", "rerank"):
                            raise ConnectionError("handled")
                    except ConnectionError:
                        pass
                with agent_trace.span("generation", "answer_structured"):
                    with agent_trace.span("llm_call", "answer_structured"):
                        raise ConnectionError("fatal")
        trace = agent_trace.load_trace(self.db, run.run_id)
        spans = {(s["stage"], s["name"]): s for s in trace["spans"]}
        self.assertEqual(trace["run"]["failed_stage"], "generation")
        self.assertEqual(trace["run"]["failed_span_id"], spans[("llm_call", "answer_structured")]["span_id"])
        self.assertEqual(trace["run"]["error_message"], "fatal")
        self.assertEqual(spans[("llm_call", "rerank")]["error_message"], "handled")
        # The enclosing generation span records the type, not a duplicate traceback.
        self.assertEqual(spans[("generation", "answer_structured")]["error_type"], "ConnectionError")
        self.assertIsNone(spans[("generation", "answer_structured")]["error_traceback"])

    def test_streaming_generator_keeps_its_run_across_threadpool_steps(self):
        run = self.new_run(streaming=True)

        def generate():
            for index in range(3):
                with agent_trace.span("generation", f"step{index}"):
                    pass
                yield index

        async def consume():
            return [item async for item in iterate_in_threadpool(run.iterate(generate()))]

        self.assertEqual(asyncio.run(consume()), [0, 1, 2])
        trace = agent_trace.load_trace(self.db, run.run_id)
        self.assertEqual(trace["run"]["status"], "completed")
        self.assertEqual([s["name"] for s in trace["spans"]], ["step0", "step1", "step2"])

    def test_stream_closed_early_is_a_failed_run(self):
        run = self.new_run(streaming=True)

        def generate():
            with agent_trace.span("generation", "answer_stream"):
                yield 1
                yield 2

        iterator = run.iterate(generate())
        next(iterator)
        iterator.close()
        trace = agent_trace.load_trace(self.db, run.run_id)
        self.assertEqual((trace["run"]["status"], trace["run"]["error_type"], trace["run"]["failed_stage"]),
                         ("failed", "ClientDisconnected", "generation"))

    def test_disabled_tracing_returns_a_null_run(self):
        with patch.dict(os.environ, {"TRACE_ENABLED": "off"}):
            run = self.new_run()
        self.assertIsNone(run.run_id)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
