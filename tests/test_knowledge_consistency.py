import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import storage as storage_module
from rag import EMBED_MODEL, Chunk
from storage import SQLiteStorage


CLIENT_ID = "consistency-client"


class StorageKnowledgeAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "storage.db"
        self.storage = SQLiteStorage(self.database_path)
        self.old_chunks = [
            Chunk(
                text="Old policy",
                source="old.md",
                index=1,
                embedding=[0.1, 0.2],
            )
        ]
        self.storage.replace_chunks(self.old_chunks)
        self.conversation = self.storage.create_conversation(
            CLIENT_ID,
            conversation_id="existing-conversation",
        )
        self.storage.commit_exchange(
            self.conversation["id"],
            CLIENT_ID,
            "Old question",
            "Old answer",
        )

    def tearDown(self):
        self.temp_directory.cleanup()

    def install_conversation_delete_failure(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_conversation_delete
                BEFORE DELETE ON conversations
                BEGIN
                    SELECT RAISE(ABORT, 'forced conversation delete failure');
                END;
                """
            )
            connection.commit()

    def metadata(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return dict(connection.execute("SELECT key, value FROM app_meta"))

    def message_count(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def assert_old_state_is_intact(self):
        loaded = self.storage.load_chunks()
        self.assertEqual(
            [(chunk.text, chunk.source, chunk.index, chunk.embedding) for chunk in loaded],
            [("Old policy", "old.md", 1, [0.1, 0.2])],
        )
        self.assertEqual(
            [item["content"] for item in self.storage.get_messages(
                self.conversation["id"], CLIENT_ID
            )],
            ["Old question", "Old answer"],
        )
        self.assertEqual(self.metadata()["embedding_model"], EMBED_MODEL)

    def test_replace_knowledge_replaces_chunks_and_clears_history_together(self):
        replacement = [
            Chunk(
                text="New policy",
                source="new.md",
                index=1,
                embedding=[0.8, 0.9],
            )
        ]

        self.storage.replace_knowledge(replacement)

        loaded = self.storage.load_chunks()
        self.assertEqual(
            [(chunk.text, chunk.source, chunk.index, chunk.embedding) for chunk in loaded],
            [("New policy", "new.md", 1, [0.8, 0.9])],
        )
        self.assertEqual(self.storage.list_conversations(CLIENT_ID), [])
        self.assertEqual(self.message_count(), 0)

    def test_replace_knowledge_rolls_back_chunks_metadata_and_history_on_failure(self):
        self.install_conversation_delete_failure()
        replacement = [
            Chunk(
                text="New policy",
                source="new.md",
                index=1,
                embedding=[0.8, 0.9],
            )
        ]

        with patch.object(storage_module, "EMBED_MODEL", "replacement-model"):
            with self.assertRaisesRegex(
                sqlite3.DatabaseError,
                "forced conversation delete failure",
            ):
                self.storage.replace_knowledge(replacement)

        self.assert_old_state_is_intact()

    def test_clear_knowledge_clears_chunks_metadata_and_history_together(self):
        self.storage.clear_knowledge()

        self.assertEqual(self.storage.load_chunks(), [])
        self.assertEqual(self.metadata(), {})
        self.assertEqual(self.storage.list_conversations(CLIENT_ID), [])
        self.assertEqual(self.message_count(), 0)

    def test_clear_knowledge_rolls_back_everything_on_failure(self):
        self.install_conversation_delete_failure()

        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "forced conversation delete failure",
        ):
            self.storage.clear_knowledge()

        self.assert_old_state_is_intact()


class ApiKnowledgeVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_storage = api.storage
        self.original_chunks = list(api.chunks)
        self.original_version = api.knowledge_version
        self.original_conversation_locks = dict(api.conversation_locks)
        with api.state_lock:
            api.storage = SQLiteStorage(Path(self.temp_directory.name) / "api.db")
            api.chunks[:] = [
                Chunk(
                    text="## Policy\nCurrent policy content",
                    source="current.md",
                    index=1,
                )
            ]
            api.knowledge_version = 100
            api.conversation_locks.clear()
        self.client = TestClient(api.create_app(v1_routes=True))

    def tearDown(self):
        self.client.close()
        with api.state_lock:
            api.storage = self.original_storage
            api.chunks[:] = self.original_chunks
            api.knowledge_version = self.original_version
            api.conversation_locks.clear()
            api.conversation_locks.update(self.original_conversation_locks)
        self.temp_directory.cleanup()

    @staticmethod
    def payload(session_id):
        return {
            "question": "What is the current policy?",
            "session_id": session_id,
            "client_id": CLIENT_ID,
        }

    @staticmethod
    def search_decision():
        return {
            "type": "tool",
            "tool": "search_knowledge_base",
            "arguments": {"query": "current policy"},
            "seconds": 0.01,
        }

    @staticmethod
    def advance_knowledge_version():
        with api.state_lock:
            api.knowledge_version += 1

    def test_non_streaming_answer_is_not_persisted_after_knowledge_changes(self):
        def stale_answer(*_args):
            self.advance_knowledge_version()
            return "Answer generated from the old snapshot"

        with (
            patch.object(api, "decide_action", return_value=self.search_decision()),
            patch.object(api, "retrieve_fast", return_value=[(api.chunks[0], 1.0)]),
            patch.object(api, "answer_structured", side_effect=stale_answer),
        ):
            response = self.client.post(
                "/api/chat",
                json=self.payload("stale-non-stream"),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            api.storage.get_messages("stale-non-stream", CLIENT_ID),
            [],
        )

    def test_streaming_answer_emits_error_and_is_not_persisted_after_knowledge_changes(self):
        def stale_stream(*_args):
            yield "Answer generated "
            self.advance_knowledge_version()
            yield "from the old snapshot"

        with (
            patch.object(api, "decide_action", return_value=self.search_decision()),
            patch.object(api, "retrieve_fast", return_value=[(api.chunks[0], 1.0)]),
            patch.object(api, "answer_stream", side_effect=stale_stream),
        ):
            response = self.client.post(
                "/api/chat/stream",
                json=self.payload("stale-stream"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: delta", response.text)
        self.assertIn("event: error", response.text)
        self.assertNotIn("event: done", response.text)
        self.assertEqual(api.storage.get_messages("stale-stream", CLIENT_ID), [])


if __name__ == "__main__":
    unittest.main()
