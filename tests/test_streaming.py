import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import rag
from rag import Chunk
from storage import SQLiteStorage


CLIENT_A = "client-a"
CLIENT_B = "client-b"


class FakeOllamaResponse:
    def __init__(self, payloads):
        self.payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=False):
        assert decode_unicode
        return iter(json.dumps(item, ensure_ascii=False) for item in self.payloads)


class OllamaStreamTests(unittest.TestCase):
    def collect(self, payloads):
        with patch.object(rag.requests, "post", return_value=FakeOllamaResponse(payloads)):
            return list(rag.answer_stream("question", [], []))

    def test_streams_visible_content_and_requires_done(self):
        """The visible answer is assembled from the chunks and requires `done`.

        The per-chunk list shape (`["Hello", ", world"]`) was relaxed by main
        agent adjudication in the second acceptance round: the public generator
        must now emit only after the envelope is fully parsed, `done` has
        arrived, and the answer has passed delivery validation, so it cannot
        also hand out partial text as it goes. Everything the assertion was
        protecting is still checked here - the complete body, the requirement
        for a `done` marker (below), and error handling (the tests that follow).
        Chunk-level parsing is still exercised by
        `test_decodes_escaped_content_across_chunks`.
        """
        result = self.collect([
            {"message": {"content": "{\n"}},
            {"message": {"content": ' "answer": "'}},
            {"message": {"content": "Hello"}},
            {"message": {"content": ", world"}},
            {"message": {"content": '"\n}'}},
            {"message": {"content": ""}, "done": True},
        ])
        self.assertEqual("".join(result), "Hello, world")

    def test_decodes_escaped_content_across_chunks(self):
        result = self.collect([
            {"message": {"content": '{"answer":"line 1\\'}},
            {"message": {"content": 'nquote: \\"ok\\""}'}},
            {"message": {"content": ""}, "done": True},
        ])
        self.assertEqual("".join(result), 'line 1\nquote: "ok"')

    def test_raises_for_midstream_error(self):
        with self.assertRaisesRegex(RuntimeError, "model error"):
            self.collect([
                {"message": {"content": '{"answer":"partial'}},
                {"error": "model error"},
            ])

    def test_raises_when_done_marker_is_missing(self):
        with self.assertRaises(RuntimeError):
            self.collect([{"message": {"content": '{"answer":"partial'}}])


class ApiStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_storage = api.storage
        api.storage = SQLiteStorage(Path(self.temp_directory.name) / "test.db")
        with api.state_lock:
            api.chunks.clear()
            api.chunks.append(
                Chunk(text="## Test section\nPolicy content", source="rules.md", index=1)
            )
            api.conversation_locks.clear()
        self.client = TestClient(api.create_app(v1_routes=True))

    def tearDown(self):
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()
        api.storage = self.original_storage
        self.temp_directory.cleanup()

    def chat_payload(self, question, session_id, client_id=CLIENT_A):
        return {
            "question": question,
            "session_id": session_id,
            "client_id": client_id,
        }

    def test_successful_stream_commits_only_its_client_session(self):
        decision = {"type": "direct", "content": "Hello", "seconds": 0.01}
        with patch.object(api, "decide_action", return_value=decision):
            response = self.client.post(
                "/api/chat/stream",
                json=self.chat_payload("hi", "session-a"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: delta", response.text)
        self.assertIn("event: done", response.text)
        self.assertEqual(len(api.storage.get_messages("session-a", CLIENT_A)), 2)
        self.assertIn(
            "session-a",
            {item["id"] for item in api.storage.list_conversations(CLIENT_A)},
        )
        self.assertEqual(api.storage.list_conversations(CLIENT_B), [])

    def test_failed_stream_does_not_commit_partial_history(self):
        decision = {
            "type": "tool",
            "tool": "search_knowledge_base",
            "arguments": {"query": "policy"},
            "seconds": 0.01,
        }

        def broken_answer(*_args):
            yield "partial answer"
            raise RuntimeError("model interrupted")

        with (
            patch.object(api, "decide_action", return_value=decision),
            patch.object(api, "retrieve_fast", return_value=[(api.chunks[0], 1.0)]),
            patch.object(api, "answer_stream", side_effect=broken_answer),
        ):
            response = self.client.post(
                "/api/chat/stream",
                json=self.chat_payload("policy", "session-b"),
            )

        self.assertIn("event: delta", response.text)
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: done", response.text)
        self.assertEqual(api.storage.get_messages("session-b", CLIENT_A), [])

    def test_conversation_endpoints_are_scoped_to_client(self):
        created = self.client.post(
            "/api/conversations",
            json={"title": "Client A conversation", "client_id": CLIENT_A},
        )
        self.assertEqual(created.status_code, 201)
        conversation_id = created.json()["id"]

        own_list = self.client.get("/api/conversations", params={"client_id": CLIENT_A})
        other_list = self.client.get("/api/conversations", params={"client_id": CLIENT_B})
        self.assertEqual([item["id"] for item in own_list.json()["items"]], [conversation_id])
        self.assertEqual(other_list.json()["items"], [])

        other_read = self.client.get(
            f"/api/conversations/{conversation_id}/messages",
            params={"client_id": CLIENT_B},
        )
        other_delete = self.client.delete(
            f"/api/conversations/{conversation_id}",
            params={"client_id": CLIENT_B},
        )
        self.assertEqual(other_read.status_code, 404)
        self.assertEqual(other_delete.status_code, 404)

        own_delete = self.client.delete(
            f"/api/conversations/{conversation_id}",
            params={"client_id": CLIENT_A},
        )
        self.assertEqual(own_delete.status_code, 200)

    def test_chat_rejects_conversation_owned_by_another_client(self):
        api.storage.create_conversation(
            CLIENT_A,
            conversation_id="owned-by-client-a",
        )
        with patch.object(
            api,
            "decide_action",
            return_value={"type": "direct", "content": "should not run", "seconds": 0.01},
        ) as decide:
            response = self.client.post(
                "/api/chat",
                json=self.chat_payload("hi", "owned-by-client-a", CLIENT_B),
            )

        self.assertEqual(response.status_code, 404)
        decide.assert_not_called()
        self.assertEqual(api.storage.get_messages("owned-by-client-a", CLIENT_A), [])


if __name__ == "__main__":
    unittest.main()
