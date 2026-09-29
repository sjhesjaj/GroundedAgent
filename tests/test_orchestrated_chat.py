import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import chat_orchestration
import wiki_runtime
from chat_orchestration import (
    MESSAGE_DIRECT,
    MESSAGE_MULTIPLE_SKU,
    MESSAGE_NO_DOCUMENTS,
    MESSAGE_NO_SKU,
    MESSAGE_NO_WIKI,
    MESSAGE_SYSTEM_LIMITED,
    extract_sku,
    extract_skus,
)
from rag import Chunk
from storage import SQLiteStorage

CLIENT_A = "client-a"
CLIENT_B = "client-b"
REPO_ROOT = Path(__file__).resolve().parent.parent

LEAVE_CHUNK = Chunk(
    text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假。",
    source="sample_company_rules.md",
    index=1,
)


class OrchestratedChatTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_storage = api.storage
        api.storage = SQLiteStorage(Path(self.temp_directory.name) / "test.db")
        with api.state_lock:
            api.chunks.clear()
            api.chunks.append(LEAVE_CHUNK)
            api.conversation_locks.clear()
        self.client = TestClient(api.app)
        # These V1 transport/provenance checks explicitly own their old sample;
        # production fallback now uses after-sales pages. Assertions unchanged.
        patch.object(chat_orchestration, "WIKI_PAGES", chat_orchestration.load_wiki_pages()).start()

        # Never reach a model or the network: generation is patched, and so is
        # retrieval, which would otherwise call Ollama for embeddings.
        self.answer = patch.object(
            api, "answer_structured", return_value="年假为 5 天。[来源 1]"
        ).start()
        self.stream = patch.object(
            api, "answer_stream", side_effect=lambda *a, **k: iter(["年假为 5 天。[来源 1]"])
        ).start()
        self.retrieval = patch(
            "orchestration.document_adapter.retrieve_fast",
            side_effect=lambda question, chunks, top_k=4, trace=None: [
                (chunks[0], 3.5)
            ] if chunks else [],
        ).start()
        # The default runtime reads the developer's real data/wiki; a published
        # build there would replace the committed sample and WIKI_PAGES patches.
        # An empty, per-test root means "no build yet", as on a fresh checkout.
        patch.object(
            wiki_runtime, "RUNTIME",
            wiki_runtime.WikiRuntime(root=Path(self.temp_directory.name) / "wiki"),
        ).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()
        api.storage = self.original_storage
        self.temp_directory.cleanup()

    def payload(self, question, session_id, *, mode="orchestrated", client_id=CLIENT_A):
        return {
            "question": question,
            "session_id": session_id,
            "client_id": client_id,
            "mode": mode,
        }

    def ask(self, question, session_id, **kwargs):
        response = self.client.post(
            "/api/chat", json=self.payload(question, session_id, **kwargs)
        )
        return response

    def set_chunks(self, *items):
        with api.state_lock:
            api.chunks.clear()
            api.chunks.extend(items)


class RouteScenarioTests(OrchestratedChatTests):
    def test_wiki_only(self):
        body = self.ask("介绍一下请假制度", "s-wiki").json()
        self.assertEqual(body["route"], "wiki_only")
        self.assertEqual(body["steps"], ["wiki_query"])
        self.assertTrue(body["sources"])
        self.assertEqual({s["type"] for s in body["sources"]}, {"wiki"})

    def test_document_only(self):
        body = self.ask("年假最多可以休多少天", "s-doc").json()
        self.assertEqual(body["route"], "document_only")
        self.assertEqual(body["steps"], ["document_search"])
        self.assertEqual({s["type"] for s in body["sources"]}, {"document"})

    def test_wiki_and_document(self):
        body = self.ask("概述请假制度并引用关键条款", "s-wd").json()
        self.assertEqual(body["route"], "wiki_document")
        self.assertEqual(body["steps"], ["wiki_query", "document_search"])
        self.assertEqual({s["type"] for s in body["sources"]}, {"wiki", "document"})

    def test_inventory_system_only(self):
        body = self.ask("帮我查一下 SKU-A100 的库存", "s-sys").json()
        self.assertEqual(body["route"], "system_only")
        self.assertEqual(body["steps"], ["system_query"])
        self.assertEqual({s["type"] for s in body["sources"]}, {"system"})
        self.assertIn("42", body["sources"][0]["content"])

    def test_document_and_inventory(self):
        # Deliberately avoids policy nouns: M2 suppresses the System channel
        # when a question is asking what a rule says.
        body = self.ask(
            "SKU-A100 库存还有多少？依据原文是怎么写的", "s-ds"
        ).json()
        self.assertEqual(body["steps"], ["document_search", "system_query"])
        self.assertEqual({s["type"] for s in body["sources"]}, {"document", "system"})

    def test_sources_carry_provenance_with_stable_keys(self):
        body = self.ask("概述请假制度并引用关键条款", "s-keys").json()
        expected = {
            "rank", "type", "source", "heading", "chunk_index", "locator",
            "score", "content",
        }
        for source in body["sources"]:
            self.assertEqual(set(source), expected)
        by_type = {s["type"]: s for s in body["sources"]}
        self.assertIsNone(by_type["wiki"]["chunk_index"])
        self.assertIsNotNone(by_type["document"]["chunk_index"])

    def test_citations_are_positional(self):
        body = self.ask("概述请假制度并引用关键条款", "s-cite").json()
        self.assertEqual(
            [s["rank"] for s in body["sources"]],
            list(range(1, len(body["sources"]) + 1)),
        )


class FixedAnswerTests(OrchestratedChatTests):
    def test_direct_never_reaches_the_answer_model(self):
        body = self.ask("你好", "s-direct").json()
        self.assertEqual(body["route"], "direct")
        self.assertEqual(body["steps"], [])
        self.assertEqual(body["answer"], MESSAGE_DIRECT)
        self.assertEqual(body["sources"], [])
        self.answer.assert_not_called()
        self.stream.assert_not_called()

    def test_direct_never_reaches_the_answer_model_over_sse(self):
        response = self.client.post(
            "/api/chat/stream", json=self.payload("谢谢！", "s-direct-sse")
        )
        self.assertIn(MESSAGE_DIRECT, response.text)
        self.assertIn("event: done", response.text)
        self.assertNotIn("event: error", response.text)
        self.answer.assert_not_called()
        self.stream.assert_not_called()

    def test_unopened_system_intent_does_not_ask_for_a_sku(self):
        for question in (
            "我的订单现在什么状态",
            "查一下我的审批进度",
            "我的账户余额是多少",
        ):
            with self.subTest(question=question):
                with patch.object(chat_orchestration, "execute_plan") as executor:
                    body = self.ask(question, "s-unopened").json()
                self.assertEqual(body["answer"], MESSAGE_SYSTEM_LIMITED)
                self.assertNotIn("SKU", body["answer"])
                executor.assert_not_called()
                self.answer.assert_not_called()

    def test_inventory_intent_without_a_sku_still_asks_for_one(self):
        for question in ("帮我查一下库存", "sku 的库存还有多少"):
            with self.subTest(question=question):
                body = self.ask(question, "s-inv-nosku").json()
                self.assertEqual(body["answer"], MESSAGE_NO_SKU)

    def test_missing_sku_does_not_execute_system(self):
        with patch.object(chat_orchestration, "execute_plan") as executor:
            body = self.ask("帮我查一下库存", "s-nosku").json()
        self.assertEqual(body["answer"], MESSAGE_NO_SKU)
        self.assertEqual(body["sources"], [])
        executor.assert_not_called()

    def test_two_different_skus_report_the_one_per_request_limit(self):
        """Two valid SKUs is a stated limit, not a missing parameter.

        This previously answered `请提供需要查询的 SKU。`, which told a caller who
        had supplied two usable SKUs that they had supplied none.
        """
        body = self.ask("查一下 SKU-A100 和 SKU-C300 的库存", "s-twosku").json()
        self.assertEqual(body["answer"], MESSAGE_MULTIPLE_SKU)
        self.assertNotEqual(body["answer"], MESSAGE_NO_SKU)

    def test_unknown_sku_refuses_without_calling_the_model(self):
        body = self.ask("帮我查一下 SKU-Z999 的库存", "s-unknown").json()
        self.assertEqual(body["trace"]["outcome"], "refuse")
        self.assertEqual(body["sources"], [])
        self.answer.assert_not_called()

    def test_missing_documents_is_a_fixed_answer_not_409(self):
        self.set_chunks()
        response = self.ask("年假最多可以休多少天", "s-nodoc")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], MESSAGE_NO_DOCUMENTS)
        self.answer.assert_not_called()

    def test_missing_wiki_is_a_fixed_answer(self):
        with patch.object(chat_orchestration, "WIKI_PAGES", ()):
            body = self.ask("介绍一下请假制度", "s-nowiki").json()
        self.assertEqual(body["answer"], MESSAGE_NO_WIKI)


class EmptyKnowledgeBaseTests(OrchestratedChatTests):
    def test_inventory_works_without_any_documents(self):
        self.set_chunks()
        response = self.ask("帮我查一下 SKU-A100 的库存", "s-empty-sys")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["route"], "system_only")
        self.assertEqual({s["type"] for s in body["sources"]}, {"system"})
        # Persisted, so the conversation was created despite an empty index.
        self.assertEqual(len(api.storage.get_messages("s-empty-sys", CLIENT_A)), 2)

    def test_legacy_still_returns_409_without_documents(self):
        self.set_chunks()
        response = self.ask("年假有多少天", "s-legacy-409", mode="legacy")
        self.assertEqual(response.status_code, 409)

    def test_foreign_conversation_is_rejected_even_without_documents(self):
        api.storage.create_conversation(CLIENT_A, conversation_id="owned-by-a")
        self.set_chunks()
        response = self.ask(
            "帮我查一下 SKU-A100 的库存", "owned-by-a", client_id=CLIENT_B
        )
        self.assertEqual(response.status_code, 404)


class LegacyCompatibilityTests(OrchestratedChatTests):
    def test_legacy_mode_is_unchanged(self):
        decision = {"type": "direct", "content": "你好", "seconds": 0.01}
        with patch.object(api, "decide_action", return_value=decision) as decide:
            response = self.client.post(
                "/api/chat",
                json={
                    "question": "你好",
                    "session_id": "s-legacy",
                    "client_id": CLIENT_A,
                },
            )
        body = response.json()
        decide.assert_called_once()
        self.assertEqual(body["answer"], "你好")
        self.assertEqual(set(body), {"answer", "trace", "sources"})

    def test_orchestrated_mode_does_not_use_decide_action(self):
        with patch.object(api, "decide_action") as decide:
            self.ask("年假最多可以休多少天", "s-nodecide")
        decide.assert_not_called()


class StreamTests(OrchestratedChatTests):
    def stream_text(self, question, session_id, **kwargs):
        response = self.client.post(
            "/api/chat/stream", json=self.payload(question, session_id, **kwargs)
        )
        return response.text

    def test_stream_and_json_agree(self):
        body = self.ask("概述请假制度并引用关键条款", "s-json").json()
        text = self.stream_text("概述请假制度并引用关键条款", "s-stream")

        done = json.loads(
            [line for line in text.splitlines() if line.startswith("data: ")][-1][6:]
        )
        sources_line = next(
            line for line in text.splitlines()
            if line.startswith("data: ") and '"sources"' in line
        )
        streamed_sources = json.loads(sources_line[6:])["sources"]

        self.assertEqual(done["trace"]["route"], body["route"])
        self.assertEqual(done["trace"]["steps"], body["steps"])
        self.assertEqual(streamed_sources, body["sources"])

    def test_refusal_is_a_delta_not_an_error(self):
        text = self.stream_text("帮我查一下库存", "s-refuse-stream")
        self.assertIn("event: delta", text)
        self.assertIn("event: done", text)
        self.assertNotIn("event: error", text)
        self.assertIn(MESSAGE_NO_SKU, text)

    def test_stream_failure_returns_a_fixed_payload(self):
        secret = "stream-secret-4417"

        def boom(*_args, **_kwargs):
            raise RuntimeError(secret)
            yield  # pragma: no cover - generator marker

        with patch.object(api, "answer_stream", side_effect=boom):
            text = self.stream_text("年假最多可以休多少天", "s-boom")

        self.assertIn("ORCHESTRATED_STREAM_ERROR", text)
        self.assertIn("回答生成失败，请稍后重试。", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("event: done", text)
        self.assertEqual(api.storage.get_messages("s-boom", CLIENT_A), [])

        # The lock was released, so the session is usable again.
        follow_up = self.ask("年假最多可以休多少天", "s-boom")
        self.assertEqual(follow_up.status_code, 200)

    def test_legacy_stream_failure_keeps_its_message(self):
        decision = {
            "type": "tool",
            "tool": "search_knowledge_base",
            "arguments": {"query": "policy"},
            "seconds": 0.01,
        }

        def boom(*_args):
            yield "partial"
            raise RuntimeError("legacy-secret")

        with (
            patch.object(api, "decide_action", return_value=decision),
            patch.object(api, "retrieve_fast", return_value=[(LEAVE_CHUNK, 1.0)]),
            patch.object(api, "answer_stream", side_effect=boom),
        ):
            response = self.client.post(
                "/api/chat/stream",
                json={
                    "question": "policy",
                    "session_id": "s-legacy-boom",
                    "client_id": CLIENT_A,
                },
            )
        self.assertIn("AGENT_STREAM_ERROR", response.text)


class ConcurrencyAndVersionTests(OrchestratedChatTests):
    def test_conversation_lock_rejects_a_concurrent_request(self):
        lock = api.acquire_conversation("s-locked")
        try:
            response = self.ask("年假最多可以休多少天", "s-locked")
            self.assertEqual(response.status_code, 409)
        finally:
            lock.release()

    def test_stale_knowledge_version_is_rejected_and_persists_nothing(self):
        def bump(*_args, **_kwargs):
            with api.state_lock:
                api.knowledge_version += 1
            return "年假为 5 天。"

        with patch.object(api, "answer_structured", side_effect=bump):
            response = self.ask("年假最多可以休多少天", "s-stale")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(api.storage.get_messages("s-stale", CLIENT_A), [])


class SkuExtractionTests(unittest.TestCase):
    def test_accepted_forms_normalize(self):
        for text in ("SKU-A100", "sku a100", "sku_a100", "查询 SKU-A100 的库存", "skuA100"):
            with self.subTest(text=text):
                self.assertEqual(extract_sku(text), "sku-a100")

    def test_rejected_forms(self):
        for text in ("没有编号", "sku-a1000", "xsku-a100", "SKU-A100 和 SKU-C300"):
            with self.subTest(text=text):
                self.assertIsNone(extract_sku(text))


class IsolationTests(OrchestratedChatTests):
    def test_no_new_database_file_is_created(self):
        self.ask("帮我查一下 SKU-A100 的库存", "s-nodb")
        for directory in (REPO_ROOT, REPO_ROOT / "system_fixtures"):
            with self.subTest(directory=directory.name):
                self.assertEqual(list(directory.glob("*.db")), [])

    def test_demo_data_is_never_written(self):
        fixture = REPO_ROOT / "system_fixtures" / "sample_business_system.sql"
        before_bytes = fixture.read_bytes()
        with chat_orchestration.demo_system_connection() as connection:
            before = connection.execute("SELECT * FROM inventory ORDER BY 1").fetchall()
            # Baseline after the fixture's own INSERTs, so only query-time
            # writes would move it.
            baseline_changes = connection.total_changes
            connection.execute("SELECT * FROM inventory WHERE sku = ?", ("sku-a100",))
            self.assertEqual(connection.total_changes, baseline_changes)

        self.ask("帮我查一下 SKU-A100 的库存", "s-nowrite")

        with chat_orchestration.demo_system_connection() as connection:
            after = connection.execute("SELECT * FROM inventory ORDER BY 1").fetchall()
        self.assertEqual(before, after)
        self.assertEqual(fixture.read_bytes(), before_bytes)

    def test_trace_exposes_no_internal_detail(self):
        body = self.ask("帮我查一下 SKU-A100 的库存", "s-trace").json()
        trace = body["trace"]
        allowed = {
            "mode", "route", "steps", "outcome", "reason_codes",
            "executor_total_seconds", "answer_seconds", "total_seconds",
        }
        self.assertTrue(set(trace) <= allowed, set(trace) - allowed)
        rendered = json.dumps(trace, ensure_ascii=False)
        for forbidden in ("SELECT", "executor_tool_traces", "system_parameter_names"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
