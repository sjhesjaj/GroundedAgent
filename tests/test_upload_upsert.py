"""M9D-A: uploading upserts by document identity instead of replacing everything.

Before this, uploading B after A left the Wiki holding A+B while retrieval held
only B - so the Wiki could summarise a policy whose source text
`document_search` could no longer produce. These tests pin the two back
together.

Scripted model, temporary repository, temporary database. No Ollama, no network,
and nothing written to the project's `data/`.
"""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import wiki_runtime
from storage import SQLiteStorage
from tests.test_wiki_runtime import WAIT_SECONDS, model_for
from wiki_maintenance import derive_document_id
from wiki_runtime import JobStatus, WikiRuntime

LEAVE_FILE = "leave.md"
REMOTE_FILE = "remote.md"

DOC_A = """# 员工手册

## 请假制度

正式员工入职满一年后，每年享有 5 天带薪年假。
"""

DOC_A_V2 = """# 员工手册

## 请假制度

正式员工入职满一年后，每年享有 8 天带薪年假。
"""

DOC_B = """# 远程办公规范

## 远程办公

员工每周最多申请 2 天远程办公。
"""

CLIENT = "client-upsert"


class UploadFixture(unittest.TestCase):
    """Shared setup. Holds no tests, so subclasses do not re-run each other's."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)

        self.original_storage = api.storage
        api.storage = SQLiteStorage(self.root / "test.db")
        self.addCleanup(lambda: setattr(api, "storage", self.original_storage))
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()
        self.client = TestClient(api.app)

        # Records exactly which documents were handed to the embedder.
        self.embedded: list[list[str]] = []

        def fake_build_index(parsed, stats=None):
            self.embedded.append([chunk.source for chunk in parsed])
            for chunk in parsed:
                chunk.embedding = [float(len(chunk.text))]
            if stats is not None:
                stats.update(
                    {
                        "chunks": len(parsed),
                        "cache_hits": 0,
                        "cache_misses": len(parsed),
                        "index_seconds": 0.0,
                    }
                )
            return parsed

        patch.object(api, "build_index", side_effect=fake_build_index).start()
        self.addCleanup(patch.stopall)

        self.runtime = WikiRuntime(hold_as_draft=False,
            root=self.root / "wiki",
            model=model_for(
                (LEAVE_FILE, DOC_A),
                (LEAVE_FILE, DOC_A_V2),
                (REMOTE_FILE, DOC_B),
            ),
        )
        patch.object(wiki_runtime, "RUNTIME", self.runtime).start()

        # The compile thread writes into the temp root. Draining before the
        # directory is removed keeps teardown from racing a job still on disk.
        self.submitted: list[str] = []
        self.addCleanup(self._drain_wiki_jobs)

    def _drain_wiki_jobs(self):
        for job_id in self.submitted:
            self.runtime.wait(job_id, WAIT_SECONDS)

    def _record_job(self, body: dict) -> dict:
        job_id = body.get("wiki_job_id")
        if job_id:
            self.submitted.append(job_id)
        return body

    # ------------------------------------------------------------------

    def upload(self, *documents, wait: bool = True) -> dict:
        files = [
            ("files", (name, io.BytesIO(text.encode("utf-8")), "text/markdown"))
            for name, text in documents
        ]
        response = self.client.post("/api/knowledge/upload", files=files)
        self.assertEqual(response.status_code, 200, response.text)
        body = self._record_job(response.json())
        if wait:
            self.assertTrue(
                self.runtime.wait(body["wiki_job_id"], WAIT_SECONDS),
                "wiki job did not finish",
            )
        return body

    def rag_sources(self) -> set[str]:
        with api.state_lock:
            return {chunk.source for chunk in api.chunks}

    def rag_text(self) -> str:
        with api.state_lock:
            return "\n".join(chunk.text for chunk in api.chunks)

    def wiki_claims(self) -> list[str]:
        pages = self.runtime.published_pages() or ()
        return [claim.text for page in pages for claim in page.claims]

    def wiki_sources(self) -> set[str]:
        pages = self.runtime.published_pages() or ()
        return {claim.source for page in pages for claim in page.claims}


class UploadUpsertTests(UploadFixture):
    def test_a_second_upload_adds_to_retrieval_instead_of_replacing_it(self):
        self.upload((LEAVE_FILE, DOC_A))
        self.assertEqual(self.rag_sources(), {LEAVE_FILE})

        body = self.upload((REMOTE_FILE, DOC_B))

        # Retrieval now holds both documents ...
        self.assertEqual(self.rag_sources(), {LEAVE_FILE, REMOTE_FILE})
        self.assertIn("5 天带薪年假", self.rag_text())
        self.assertIn("2 天远程办公", self.rag_text())
        # ... and so does the Wiki, which is the point.
        self.assertEqual(self.wiki_sources(), {LEAVE_FILE, REMOTE_FILE})
        self.assertEqual(
            sorted(page.title for page in self.runtime.published_pages()),
            ["请假制度", "远程办公"],
        )
        self.assertIn("5 天带薪年假", " ".join(self.wiki_claims()))
        self.assertIn("2 天远程办公", " ".join(self.wiki_claims()))
        # `chunks` is the whole base; `uploaded_chunks` is this upload's share.
        self.assertEqual(body["chunks"], len(api.chunks))
        self.assertEqual(body["uploaded_chunks"], 1)
        self.assertEqual(body["files"], [REMOTE_FILE])

    def test_re_uploading_one_document_replaces_only_that_document(self):
        self.upload((LEAVE_FILE, DOC_A))
        self.upload((REMOTE_FILE, DOC_B))

        self.upload((LEAVE_FILE, DOC_A_V2))

        text = self.rag_text()
        self.assertNotIn("5 天带薪年假", text, "the old version must be gone")
        self.assertIn("8 天带薪年假", text, "the new version must be present")
        self.assertIn("2 天远程办公", text, "the untouched document must survive")
        self.assertEqual(self.rag_sources(), {LEAVE_FILE, REMOTE_FILE})

        claims = " ".join(self.wiki_claims())
        self.assertIn("8 天", claims)
        self.assertNotIn("5 天", claims)
        self.assertEqual(self.wiki_sources(), {LEAVE_FILE, REMOTE_FILE})

    def test_a_kept_document_is_never_re_embedded(self):
        self.upload((LEAVE_FILE, DOC_A))
        with api.state_lock:
            kept_embedding = next(
                chunk.embedding for chunk in api.chunks if chunk.source == LEAVE_FILE
            )

        self.upload((REMOTE_FILE, DOC_B))

        # The embedder only ever saw each document when it was uploaded.
        self.assertEqual(self.embedded, [[LEAVE_FILE], [REMOTE_FILE]])
        with api.state_lock:
            still_kept = next(
                chunk.embedding for chunk in api.chunks if chunk.source == LEAVE_FILE
            )
        self.assertEqual(still_kept, kept_embedding)

        # Re-uploading the same document does re-embed it, and only it.
        self.upload((LEAVE_FILE, DOC_A_V2))
        self.assertEqual(
            self.embedded, [[LEAVE_FILE], [REMOTE_FILE], [LEAVE_FILE]]
        )

    def test_chunk_positions_are_renumbered_across_the_merged_base(self):
        self.upload((LEAVE_FILE, DOC_A))
        self.upload((REMOTE_FILE, DOC_B))
        with api.state_lock:
            positions = [chunk.index for chunk in api.chunks]
        self.assertEqual(positions, list(range(1, len(positions) + 1)))

        # And the renumbering survived the round trip through storage.
        reloaded = api.storage.load_chunks()
        self.assertEqual(
            [chunk.index for chunk in reloaded], list(range(1, len(reloaded) + 1))
        )
        self.assertEqual(
            {chunk.source for chunk in reloaded}, {LEAVE_FILE, REMOTE_FILE}
        )

    def test_uploading_both_documents_in_one_batch_still_works(self):
        body = self.upload((LEAVE_FILE, DOC_A), (REMOTE_FILE, DOC_B))

        self.assertEqual(body["files"], [LEAVE_FILE, REMOTE_FILE])
        self.assertEqual(self.rag_sources(), {LEAVE_FILE, REMOTE_FILE})
        self.assertEqual(body["chunks"], len(api.chunks))
        self.assertEqual(body["uploaded_chunks"], len(api.chunks))
        self.assertEqual(self.embedded, [[LEAVE_FILE, REMOTE_FILE]])
        self.assertEqual(self.wiki_sources(), {LEAVE_FILE, REMOTE_FILE})

    def test_clearing_empties_retrieval_and_retracts_the_wiki(self):
        self.upload((LEAVE_FILE, DOC_A))
        self.upload((REMOTE_FILE, DOC_B))
        self.assertIsNotNone(self.runtime.current_build_id())

        self.assertEqual(self.client.delete("/api/knowledge").status_code, 200)

        with api.state_lock:
            self.assertEqual(api.chunks, [])
        self.assertEqual(api.storage.load_chunks(), [])
        self.assertIsNone(self.runtime.current_build_id())
        self.assertIsNone(self.runtime.published_pages())

    def test_uploading_still_clears_old_conversations(self):
        created = self.client.post(
            "/api/conversations", json={"title": "旧对话", "client_id": CLIENT}
        )
        self.assertEqual(created.status_code, 201)
        self.upload((LEAVE_FILE, DOC_A))

        listed = self.client.get(f"/api/conversations?client_id={CLIENT}").json()
        self.assertEqual(listed["items"], [])

    def test_document_identity_comes_from_the_wiki(self):
        """RAG and the Wiki must agree on what a re-upload replaces."""
        self.upload((LEAVE_FILE, DOC_A))
        self.upload((LEAVE_FILE, DOC_A_V2))

        with api.state_lock:
            document_ids = {derive_document_id(c.source) for c in api.chunks}
        self.assertEqual(document_ids, {derive_document_id(LEAVE_FILE)})
        self.assertEqual(self.wiki_sources(), {LEAVE_FILE})
        # One document in, one document version recorded by the build.
        build = self.runtime._get_repository().load_current_build()
        self.assertEqual(
            list(build.document_version_map), [derive_document_id(LEAVE_FILE)]
        )

    def test_a_failed_wiki_job_does_not_roll_back_retrieval(self):
        """Retrieval and the Wiki are updated independently on purpose: a model
        failure must not cost the user the upload they already paid for."""
        self.upload((LEAVE_FILE, DOC_A))

        broken = WikiRuntime(hold_as_draft=False, root=self.root / "wiki", model=_UnusableModel())
        with patch.object(wiki_runtime, "RUNTIME", broken):
            body = self.upload((REMOTE_FILE, DOC_B), wait=False)
            self.assertTrue(broken.wait(body["wiki_job_id"], WAIT_SECONDS))
            self.assertEqual(
                broken.status(body["wiki_job_id"])["status"], JobStatus.FAILED.value
            )

        self.assertEqual(self.rag_sources(), {LEAVE_FILE, REMOTE_FILE})


class DuplicateDocumentInOneBatchTests(UploadFixture):
    """One batch may name a document once. Retrieval would otherwise keep both
    versions while the Wiki compiled only one."""

    def post(self, *documents):
        files = [
            ("files", (name, io.BytesIO(text.encode("utf-8")), "text/markdown"))
            for name, text in documents
        ]
        response = self.client.post("/api/knowledge/upload", files=files)
        if response.status_code == 200:
            self._record_job(response.json())
        return response

    def test_the_same_filename_twice_is_rejected(self):
        for label, second in (
            ("different content", DOC_A_V2),
            ("identical content", DOC_A),
        ):
            with self.subTest(second=label):
                response = self.post((LEAVE_FILE, DOC_A), (LEAVE_FILE, second))

                self.assertEqual(response.status_code, 400)
                detail = response.json()["detail"]
                self.assertIn("同一批次不能包含重复文档", detail)
                self.assertIn(LEAVE_FILE, detail)

    def test_a_rejected_batch_changes_nothing(self):
        self.upload((LEAVE_FILE, DOC_A))
        self.client.post(
            "/api/conversations", json={"title": "保留我", "client_id": CLIENT}
        )
        with api.state_lock:
            before_chunks = [(c.source, c.text, c.index) for c in api.chunks]
            before_version = api.knowledge_version
        before_stored = [(c.source, c.text) for c in api.storage.load_chunks()]
        before_conversations = self.client.get(
            f"/api/conversations?client_id={CLIENT}"
        ).json()["items"]
        before_embedded = list(self.embedded)
        before_job = self.runtime.status()["job_id"]

        with patch.object(
            self.runtime, "submit", side_effect=AssertionError("must not queue")
        ) as submit:
            response = self.post((REMOTE_FILE, DOC_B), (REMOTE_FILE, DOC_B))

        self.assertEqual(response.status_code, 400)
        submit.assert_not_called()
        # Nothing was embedded, stored, or queued.
        self.assertEqual(self.embedded, before_embedded)
        with api.state_lock:
            self.assertEqual(
                [(c.source, c.text, c.index) for c in api.chunks], before_chunks
            )
            self.assertEqual(api.knowledge_version, before_version)
        self.assertEqual(
            [(c.source, c.text) for c in api.storage.load_chunks()], before_stored
        )
        self.assertEqual(
            self.client.get(f"/api/conversations?client_id={CLIENT}").json()["items"],
            before_conversations,
        )
        self.assertTrue(before_conversations, "the fixture needs a conversation")
        self.assertEqual(self.runtime.status()["job_id"], before_job)

    def test_two_different_documents_in_one_batch_are_still_accepted(self):
        response = self.post((LEAVE_FILE, DOC_A), (REMOTE_FILE, DOC_B))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rag_sources(), {LEAVE_FILE, REMOTE_FILE})


class _UnusableModel:
    def generate(self, request):
        return "not json at all"


if __name__ == "__main__":
    unittest.main()
