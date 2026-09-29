"""M9F: a document the Wiki superseded must also leave the retrieval index.

The Wiki decides that `new-policy.md` replaces `old-policy.md` and drops the old
document from the build. Retrieval used to keep it, so `document_search` could
still quote a rule the Wiki had already retired - the two channels disagreeing
about what the company's policy is, which is the one failure a reader cannot
detect.

Retirement is deliberately narrow. Only documents an *actually published* batch
reported as superseded are retired, so a Wiki that ignored a document, or failed
to compile, never costs the user their uploaded text.

Scripted models throughout. No Ollama, no network, no writes outside temp dirs.
"""

import io
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import api
import wiki_runtime
from storage import SQLiteStorage
from tests.test_wiki_compiler import ScriptedModel, dumps
from tests.test_wiki_runtime import WAIT_SECONDS, spans_of
from wiki_maintenance import derive_document_id
from wiki_runtime import JobStatus, WikiRuntime

OLD_FILE = "old-policy.md"
NEW_FILE = "new-policy.md"
UNRELATED_FILE = "remote.md"

OLD_DOC = """# 旧版制度

## 请假制度

正式员工每年享有 5 天带薪年假。
"""

NEW_DOC = """# 新版制度

## 请假制度

正式员工每年享有 15 天带薪年假。
"""

UNRELATED_DOC = """# 远程办公规范

## 远程办公

员工每周最多申请 2 天远程办公。
"""

OLD_FACT = "享有 5 天带薪年假"
NEW_FACT = "享有 15 天带薪年假"


def planning_model(all_documents, *, supersedes=None, fail_plan_for=None):
    """Decides, plans, and optionally declares a cross-file supersede.

    `supersedes` maps a document_id to the document_ids its decision retires.
    `fail_plan_for` makes the plan stage unusable for one document, so a batch
    can be made to fail after an earlier document already succeeded.
    """
    spans_by_id = {
        span.span_id: span
        for filename, text in all_documents
        for span in spans_of(filename, text)
    }
    supersedes = supersedes or {}

    def visible(text):
        return [span for span_id, span in spans_by_id.items() if span_id in text]

    def handler(request):
        if request.stage == "document_decision":
            document_id = re.search(r"document_id：(\S+)", request.user).group(1)
            return dumps(
                {
                    "action": "update",
                    "reason": "新版制度",
                    "supersedes_document_ids": supersedes.get(document_id, []),
                }
            )
        if request.stage == "topic_plan":
            spans = visible(request.user)
            if fail_plan_for and any(
                span.source == fail_plan_for for span in spans
            ):
                return "这不是 JSON"
            grouped: dict[str, list] = {}
            for span in spans:
                grouped.setdefault(span.heading or "未分类", []).append(span)
            return dumps(
                {
                    "pages": [
                        {
                            "topic": f"{topic}｜{group[0].source}",
                            "existing_page_id": None,
                            "source_span_ids": [s.span_id for s in group],
                        }
                        for topic, group in grouped.items()
                    ]
                }
            )
        raise AssertionError(f"unexpected stage {request.stage!r}")

    return ScriptedModel(handler)


class SupersedeFixture(unittest.TestCase):
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
            api.document_upload_versions.clear()
        self.client = TestClient(api.app)

        self.embedded: list[list[str]] = []

        def fake_build_index(parsed, stats=None):
            self.embedded.append([chunk.source for chunk in parsed])
            for chunk in parsed:
                chunk.embedding = [float(len(chunk.text))]
            if stats is not None:
                stats.update({"chunks": len(parsed)})
            return parsed

        patch.object(api, "build_index", side_effect=fake_build_index).start()
        self.addCleanup(patch.stopall)

        self.submitted: list[str] = []
        self.addCleanup(self._drain)

    def _drain(self):
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return
        for job_id in self.submitted:
            runtime.wait(job_id, WAIT_SECONDS)

    def use_runtime(self, model):
        self.runtime = WikiRuntime(hold_as_draft=False, root=self.root / "wiki", model=model)
        patch.object(wiki_runtime, "RUNTIME", self.runtime).start()
        return self.runtime

    def upload(self, *documents, wait=True):
        files = [
            ("files", (name, io.BytesIO(text.encode("utf-8")), "text/markdown"))
            for name, text in documents
        ]
        response = self.client.post("/api/knowledge/upload", files=files)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        job_id = body.get("wiki_job_id")
        if job_id:
            self.submitted.append(job_id)
            if wait:
                self.assertTrue(self.runtime.wait(job_id, WAIT_SECONDS), "job hung")
        return body

    # -- observations -------------------------------------------------

    def rag_sources(self):
        with api.state_lock:
            return {chunk.source for chunk in api.chunks}

    def rag_text(self):
        with api.state_lock:
            return "\n".join(chunk.text for chunk in api.chunks)

    def stored_text(self):
        return "\n".join(chunk.text for chunk in api.storage.load_chunks())

    def wiki_text(self):
        pages = self.runtime.published_pages() or ()
        return "\n".join(c.text for p in pages for c in p.claims)


class CrossDocumentSupersedeTests(SupersedeFixture):
    def test_a_superseded_document_leaves_both_the_wiki_and_retrieval(self):
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        self.use_runtime(
            planning_model(
                [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
                supersedes={new_id: [old_id]},
            )
        )
        self.upload((OLD_FILE, OLD_DOC))
        self.assertIn(OLD_FACT, self.rag_text())

        self.upload((NEW_FILE, NEW_DOC))

        # The Wiki says only the new rule ...
        self.assertIn(NEW_FACT, self.wiki_text())
        self.assertNotIn(OLD_FACT, self.wiki_text())
        # ... and so does retrieval, in memory and on disk.
        self.assertNotIn(OLD_FACT, self.rag_text())
        self.assertIn(NEW_FACT, self.rag_text())
        self.assertEqual(self.rag_sources(), {NEW_FILE})
        self.assertNotIn(OLD_FACT, self.stored_text())

        status = self.client.get("/api/wiki/status").json()
        self.assertEqual(status["status"], JobStatus.PUBLISHED.value)
        self.assertEqual(status["superseded_document_ids"], [old_id])
        self.assertEqual(status["retired_document_ids"], [old_id])

    def test_retirement_survives_a_restart(self):
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        self.use_runtime(
            planning_model(
                [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
                supersedes={new_id: [old_id]},
            )
        )
        self.upload((OLD_FILE, OLD_DOC))
        self.upload((NEW_FILE, NEW_DOC))

        # A restart reloads the index from storage.
        reloaded = api.storage.load_chunks()
        self.assertNotIn(OLD_FACT, "\n".join(c.text for c in reloaded))
        self.assertIn(NEW_FACT, "\n".join(c.text for c in reloaded))
        self.assertEqual({c.source for c in reloaded}, {NEW_FILE})
        self.assertEqual([c.index for c in reloaded], list(range(1, len(reloaded) + 1)))

    def test_unrelated_documents_are_kept_and_never_re_embedded(self):
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        self.use_runtime(
            planning_model(
                [
                    (OLD_FILE, OLD_DOC),
                    (NEW_FILE, NEW_DOC),
                    (UNRELATED_FILE, UNRELATED_DOC),
                ],
                supersedes={new_id: [old_id]},
            )
        )
        self.upload((OLD_FILE, OLD_DOC))
        self.upload((UNRELATED_FILE, UNRELATED_DOC))
        with api.state_lock:
            kept_embedding = next(
                c.embedding for c in api.chunks if c.source == UNRELATED_FILE
            )

        self.upload((NEW_FILE, NEW_DOC))

        self.assertEqual(self.rag_sources(), {UNRELATED_FILE, NEW_FILE})
        self.assertIn("2 天远程办公", self.rag_text())
        # The embedder only ever saw each document at its own upload.
        self.assertEqual(
            self.embedded, [[OLD_FILE], [UNRELATED_FILE], [NEW_FILE]]
        )
        with api.state_lock:
            still = next(c.embedding for c in api.chunks if c.source == UNRELATED_FILE)
        self.assertEqual(still, kept_embedding)


class RetirementIsNarrowTests(SupersedeFixture):
    def test_an_ignored_document_is_not_retired_from_retrieval(self):
        """The Wiki declining a document says nothing about its source text."""
        ignoring = ScriptedModel(
            lambda request: dumps(
                {"action": "ignore", "reason": "与制度无关", "supersedes_document_ids": []}
            )
        )
        self.use_runtime(ignoring)
        self.upload((OLD_FILE, OLD_DOC))
        self.upload((UNRELATED_FILE, UNRELATED_DOC))

        status = self.client.get("/api/wiki/status").json()
        self.assertEqual(status["status"], JobStatus.IGNORED.value)
        # Both documents are still searchable even though the Wiki has nothing.
        self.assertEqual(self.rag_sources(), {OLD_FILE, UNRELATED_FILE})
        self.assertIn(OLD_FACT, self.rag_text())
        self.assertEqual(status["retired_document_ids"], [])

    def test_a_failed_compile_retires_nothing(self):
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        self.use_runtime(
            planning_model(
                [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
                supersedes={new_id: [old_id]},
                fail_plan_for=NEW_FILE,
            )
        )
        self.upload((OLD_FILE, OLD_DOC))
        published_before = self.runtime.current_build_id()

        self.upload((NEW_FILE, NEW_DOC))

        status = self.client.get("/api/wiki/status").json()
        self.assertEqual(status["status"], JobStatus.FAILED.value)
        self.assertEqual(status["retired_document_ids"], [])
        # The upload is not rolled back, and the old document stays searchable.
        self.assertEqual(self.rag_sources(), {OLD_FILE, NEW_FILE})
        self.assertIn(OLD_FACT, self.rag_text())
        self.assertIn(NEW_FACT, self.rag_text())
        # The previously published Wiki is untouched.
        self.assertEqual(self.runtime.current_build_id(), published_before)

    def test_a_batch_that_fails_after_a_supersede_retires_nothing(self):
        """The supersede was decided, but the batch never published, so the
        retirement it implied must not happen either."""
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        unrelated_id = derive_document_id(UNRELATED_FILE)
        self.use_runtime(
            planning_model(
                [
                    (OLD_FILE, OLD_DOC),
                    (NEW_FILE, NEW_DOC),
                    (UNRELATED_FILE, UNRELATED_DOC),
                ],
                supersedes={new_id: [old_id]},
                fail_plan_for=UNRELATED_FILE,
            )
        )
        self.upload((OLD_FILE, OLD_DOC))

        # One batch: the supersede succeeds, then the last document fails.
        self.upload((NEW_FILE, NEW_DOC), (UNRELATED_FILE, UNRELATED_DOC))

        status = self.client.get("/api/wiki/status").json()
        self.assertEqual(status["status"], JobStatus.FAILED.value)
        self.assertEqual(status["retired_document_ids"], [])
        self.assertIn(OLD_FACT, self.rag_text())
        self.assertEqual(
            self.rag_sources(), {OLD_FILE, NEW_FILE, UNRELATED_FILE}
        )
        self.assertNotIn(unrelated_id, status["superseded_document_ids"])


class RetirementCannotOutrunTheUserTests(SupersedeFixture):
    def test_a_document_re_uploaded_after_the_job_started_is_not_retired(self):
        """An old job must not delete a newer upload of the same document."""
        import threading

        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        release = threading.Event()
        reached = threading.Event()
        inner = planning_model(
            [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
            supersedes={new_id: [old_id]},
        )

        class Blocking:
            def generate(self, request):
                answer = inner.generate(request)
                # Only stall the job that carries the supersede; the first
                # upload's job must be allowed to finish normally.
                if request.stage == "topic_plan" and NEW_FILE in request.user:
                    reached.set()
                    release.wait(WAIT_SECONDS)
                return answer

        self.use_runtime(Blocking())
        self.upload((OLD_FILE, OLD_DOC))

        body = self.upload((NEW_FILE, NEW_DOC), wait=False)
        self.assertTrue(reached.wait(WAIT_SECONDS))

        # While the job is mid-flight the user re-uploads the old document.
        revived = OLD_DOC.replace("5 天", "7 天")
        self.upload((OLD_FILE, revived), wait=False)

        release.set()
        self.assertTrue(self.runtime.wait(body["wiki_job_id"], WAIT_SECONDS))

        # The supersede was decided, but the re-upload is newer, so it stands.
        self.assertIn(OLD_FILE, self.rag_sources())
        self.assertIn("享有 7 天带薪年假", self.rag_text())
        status = self.client.get(f"/api/wiki/status?job_id={body['wiki_job_id']}").json()
        self.assertEqual(status["superseded_document_ids"], [old_id])
        self.assertEqual(status["retired_document_ids"], [])

    def test_clearing_cancels_a_job_before_it_can_retire_anything(self):
        import threading

        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        release = threading.Event()
        reached = threading.Event()
        inner = planning_model(
            [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
            supersedes={new_id: [old_id]},
        )

        class Blocking:
            def generate(self, request):
                answer = inner.generate(request)
                # Only stall the job that carries the supersede; the first
                # upload's job must be allowed to finish normally.
                if request.stage == "topic_plan" and NEW_FILE in request.user:
                    reached.set()
                    release.wait(WAIT_SECONDS)
                return answer

        self.use_runtime(Blocking())
        self.upload((OLD_FILE, OLD_DOC))
        body = self.upload((NEW_FILE, NEW_DOC), wait=False)
        self.assertTrue(reached.wait(WAIT_SECONDS))

        self.assertEqual(self.client.delete("/api/knowledge").status_code, 200)
        release.set()
        self.assertTrue(self.runtime.wait(body["wiki_job_id"], WAIT_SECONDS))

        status = self.client.get(f"/api/wiki/status?job_id={body['wiki_job_id']}").json()
        self.assertEqual(status["status"], JobStatus.CANCELLED.value)
        self.assertEqual(status["retired_document_ids"], [])
        self.assertIsNone(self.runtime.current_build_id())
        with api.state_lock:
            self.assertEqual(api.chunks, [])


class ReAddedInTheSameBatchTests(SupersedeFixture):
    def test_a_document_the_final_build_still_cites_is_not_retired(self):
        """The batch supersedes old-policy, then re-adds a newer old-policy.

        The running union of supersede decisions still names it; the build that
        actually went live cites it. The build wins.
        """
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        revived = OLD_DOC.replace("5 天", "9 天")
        self.use_runtime(
            planning_model(
                [(OLD_FILE, OLD_DOC), (OLD_FILE, revived), (NEW_FILE, NEW_DOC)],
                supersedes={new_id: [old_id]},
            )
        )
        self.upload((OLD_FILE, OLD_DOC))

        # One batch, in this order: the supersede, then the document's return.
        self.upload((NEW_FILE, NEW_DOC), (OLD_FILE, revived))

        status = self.client.get("/api/wiki/status").json()
        self.assertEqual(status["status"], JobStatus.PUBLISHED.value)
        self.assertEqual(status["superseded_document_ids"], [old_id])
        self.assertEqual(
            status["retired_document_ids"], [], "the final build still cites it"
        )

        # The live build cites both documents ...
        live = self.runtime._get_repository().load_current_build()
        self.assertEqual(sorted(live.document_version_map), sorted([old_id, new_id]))
        # ... and so does retrieval, with the new text, in memory and on disk.
        self.assertEqual(self.rag_sources(), {OLD_FILE, NEW_FILE})
        self.assertIn("享有 9 天带薪年假", self.rag_text())
        self.assertIn("享有 9 天带薪年假", self.stored_text())
        self.assertNotIn(OLD_FACT, self.rag_text())


class UploadAndRetirementAreSerialisedTests(SupersedeFixture):
    def test_an_upload_in_flight_cannot_resurrect_a_retired_document(self):
        """Retirement must not land between an upload's snapshot and its commit.

        The upload copies the surviving chunks, embeds outside the state lock,
        then commits. A retirement slipping into that window would be undone by
        the commit writing the stale snapshot back.
        """
        import threading

        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        plan_reached = threading.Event()
        release_plan = threading.Event()
        embedding_reached = threading.Event()
        release_embedding = threading.Event()

        inner = planning_model(
            [
                (OLD_FILE, OLD_DOC),
                (NEW_FILE, NEW_DOC),
                (UNRELATED_FILE, UNRELATED_DOC),
            ],
            supersedes={new_id: [old_id]},
        )

        class Blocking:
            def generate(self, request):
                answer = inner.generate(request)
                if request.stage == "topic_plan" and NEW_FILE in request.user:
                    plan_reached.set()
                    release_plan.wait(WAIT_SECONDS)
                return answer

        self.use_runtime(Blocking())
        self.upload((OLD_FILE, OLD_DOC))

        # Signals the moment the retirement is at the door, so the upload can be
        # held open across the retirement's attempt rather than merely near it.
        retire_attempted = threading.Event()
        real_retire = api.retire_superseded_documents

        def probing_retire(ids, version):
            retire_attempted.set()
            return real_retire(ids, version)

        patch.object(api, "retire_superseded_documents", probing_retire).start()

        # The superseding batch is submitted and then parks inside the model,
        # so its upload has already returned and released the mutation lock.
        body = self.upload((NEW_FILE, NEW_DOC), wait=False)
        self.assertTrue(plan_reached.wait(WAIT_SECONDS), "wiki job never parked")

        original_build_index = api.build_index

        def blocking_build_index(parsed, stats=None):
            # Park the unrelated upload between its snapshot of the surviving
            # chunks and its commit - exactly the window a retirement could
            # otherwise slip into and be written straight back out of.
            if any(chunk.source == UNRELATED_FILE for chunk in parsed):
                embedding_reached.set()
                release_embedding.wait(WAIT_SECONDS)
            return original_build_index(parsed, stats)

        errors: list[BaseException] = []

        def upload_unrelated():
            try:
                files = [
                    (
                        "files",
                        (
                            UNRELATED_FILE,
                            io.BytesIO(UNRELATED_DOC.encode("utf-8")),
                            "text/markdown",
                        ),
                    )
                ]
                response = self.client.post("/api/knowledge/upload", files=files)
                if response.status_code == 200:
                    self.submitted.append(response.json()["wiki_job_id"])
                else:
                    errors.append(AssertionError(response.text))
            except BaseException as exc:  # surfaced after the join
                errors.append(exc)

        with patch.object(api, "build_index", side_effect=blocking_build_index):
            uploader = threading.Thread(target=upload_unrelated)
            uploader.start()
            self.assertTrue(embedding_reached.wait(WAIT_SECONDS), "upload never parked")

            # Let the Wiki job finish and wait until its retirement is actually
            # trying to write, with the upload still parked. Its write must
            # queue behind that upload rather than land inside it.
            release_plan.set()
            self.assertTrue(
                retire_attempted.wait(WAIT_SECONDS), "retirement never attempted"
            )
            # Give an unserialised retirement time to get all the way through,
            # so this test fails loudly if the mutation lock is ever dropped.
            time.sleep(0.2)

            release_embedding.set()
            uploader.join(WAIT_SECONDS)
            self.assertEqual(errors, [])

        self.assertTrue(self.runtime.wait(body["wiki_job_id"], WAIT_SECONDS))
        self._drain()

        # Whatever the interleaving, the old document is gone everywhere.
        self.assertNotIn(OLD_FACT, self.rag_text())
        self.assertNotIn(OLD_FACT, self.stored_text())
        self.assertNotIn(OLD_FILE, self.rag_sources())
        self.assertNotIn(OLD_FACT, self.wiki_text())
        # The unrelated upload still landed.
        self.assertIn(UNRELATED_FILE, self.rag_sources())
        self.assertIn("2 天远程办公", self.rag_text())


class RetirementFailureIsCompensatedTests(SupersedeFixture):
    def _arrange(self):
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        self.use_runtime(
            planning_model(
                [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
                supersedes={new_id: [old_id]},
            )
        )
        self.upload((OLD_FILE, OLD_DOC))
        return old_id, new_id

    def test_a_failed_write_rolls_the_wiki_back_and_fails_the_job(self):
        old_id, _ = self._arrange()
        published_before = self.runtime.current_build_id()
        self.assertIsNotNone(published_before)
        wiki_before = self.wiki_text()

        with patch.object(
            api.storage, "replace_chunks", side_effect=OSError("disk gone")
        ):
            body = self.upload((NEW_FILE, NEW_DOC))

        status = self.client.get(f"/api/wiki/status?job_id={body['wiki_job_id']}").json()
        self.assertEqual(status["status"], JobStatus.FAILED.value)
        self.assertIn("disk gone", status["error"])
        self.assertEqual(status["retired_document_ids"], [])

        # The Wiki went back to what it was before the batch ...
        self.assertEqual(self.runtime.current_build_id(), published_before)
        self.assertEqual(self.wiki_text(), wiki_before)
        self.assertIn(OLD_FACT, self.wiki_text())
        # ... and retrieval still holds everything, so the two agree again.
        self.assertIn(OLD_FACT, self.rag_text())
        self.assertIn(OLD_FACT, self.stored_text())

    def test_the_first_ever_build_is_retracted_rather_than_rolled_back(self):
        """With no previous build to return to, compensation takes the pointer
        down instead of rolling back.

        One batch, in order: old-policy compiles into the first draft, which is
        what makes it an active document by the time new-policy declares it
        superseded. So the supersede is legal, the batch publishes, and the
        failing write then has a first-ever build to undo.
        """
        old_id, new_id = derive_document_id(OLD_FILE), derive_document_id(NEW_FILE)
        self.use_runtime(
            planning_model(
                [(OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC)],
                supersedes={new_id: [old_id]},
            )
        )
        self.assertIsNone(
            self.runtime.current_build_id(), "the repository starts with no build"
        )

        repository = self.runtime._get_repository()
        retract = Mock(wraps=repository.retract_current)
        rollback = Mock(wraps=repository.rollback)
        restore = Mock(wraps=self.runtime._restore_previous_build)
        write = Mock(side_effect=OSError("disk gone"))

        with patch.object(repository, "retract_current", retract), patch.object(
            repository, "rollback", rollback
        ), patch.object(self.runtime, "_restore_previous_build", restore), patch.object(
            api.storage, "replace_chunks", write
        ):
            body = self.upload((OLD_FILE, OLD_DOC), (NEW_FILE, NEW_DOC))

        # The retirement was attempted once, and compensating took the pointer
        # down rather than rolling back to a build that never existed.
        self.assertEqual(write.call_count, 1)
        self.assertEqual(restore.call_count, 1)
        self.assertEqual(retract.call_count, 1)
        self.assertEqual(rollback.call_count, 0)

        status = self.client.get(f"/api/wiki/status?job_id={body['wiki_job_id']}").json()
        self.assertEqual(status["status"], JobStatus.FAILED.value)
        self.assertIn("disk gone", status["error"])
        self.assertIsNone(status["published_build_id"])
        self.assertIsNone(status["current_build_id"])
        self.assertEqual(status["retired_document_ids"], [])
        self.assertEqual(status["superseded_document_ids"], [old_id])

        self.assertIsNone(self.runtime.current_build_id())
        self.assertIsNone(self.runtime.published_pages())

        # The upload itself is not rolled back: both documents stay searchable.
        self.assertEqual(self.rag_sources(), {OLD_FILE, NEW_FILE})
        for fact in (OLD_FACT, NEW_FACT):
            self.assertIn(fact, self.rag_text())
            self.assertIn(fact, self.stored_text())

    def test_a_clear_before_compensation_is_not_undone(self):
        """Compensation must not restore a Wiki the user just cleared.

        The clear lands after the failed write released the locks and before
        compensation runs - the only window where the two can actually race,
        since a retirement and a clear are serialised on the same lock.
        """
        import threading

        old_id, _ = self._arrange()
        cleared = threading.Event()
        original_restore = WikiRuntime._restore_previous_build

        def clear_then_restore(runtime, *args, **kwargs):
            self.assertEqual(self.client.delete("/api/knowledge").status_code, 200)
            cleared.set()
            return original_restore(runtime, *args, **kwargs)

        with patch.object(
            api.storage, "replace_chunks", side_effect=OSError("disk gone")
        ), patch.object(WikiRuntime, "_restore_previous_build", clear_then_restore):
            body = self.upload((NEW_FILE, NEW_DOC))

        self.assertTrue(cleared.is_set())
        status = self.client.get(f"/api/wiki/status?job_id={body['wiki_job_id']}").json()
        self.assertIn(
            status["status"], (JobStatus.FAILED.value, JobStatus.CANCELLED.value)
        )
        # The clear stands: no build is live and retrieval is empty.
        self.assertIsNone(self.runtime.current_build_id())
        self.assertIsNone(self.runtime.published_pages())
        with api.state_lock:
            self.assertEqual(api.chunks, [])


if __name__ == "__main__":
    unittest.main()
