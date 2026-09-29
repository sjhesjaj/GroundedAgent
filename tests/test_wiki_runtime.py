"""M9C: background Wiki compilation wired into upload and chat.

Every test uses a temporary repository root and a scripted model. Nothing here
reaches Ollama or the network, and nothing writes to the project's `data/`.
"""

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import chat_orchestration
import wiki_runtime
from rag import Chunk
from storage import SQLiteStorage
from tests.test_wiki_compiler import ScriptedModel, batch_reply, dumps
from wiki_maintenance import (
    WikiRepository,
    build_document_snapshot_from_text,
    derive_document_id,
)
from wiki_maintenance.repository import DEFAULT_WIKI_DATA_ROOT
from wiki_runtime import IDLE_STATUS, JobStatus, WikiRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent
WAIT_SECONDS = 10

LEAVE_DOC = """# 员工手册

## 请假制度

正式员工入职满一年后，每年享有 5 天带薪年假。
"""

REMOTE_DOC = """# 远程办公规范

## 远程办公

员工每周最多申请 2 天远程办公。
"""

PLAIN_TEXT_DOC = """员工每周最多申请 2 天远程办公。

远程办公须至少提前一个工作日获得直属主管批准。
"""


def spans_of(filename: str, text: str):
    return build_document_snapshot_from_text(
        document_id=derive_document_id(filename), filename=filename, text=text
    ).spans


def heading_plan(spans):
    """One page per heading. The page count is the planner's to choose."""
    grouped: dict[str, list] = {}
    for span in spans:
        grouped.setdefault(span.heading or "未分类", []).append(span)
    return [
        {
            "topic": topic,
            "existing_page_id": None,
            "source_span_ids": [span.span_id for span in group],
        }
        for topic, group in grouped.items()
    ]


def model_for(*documents):
    """A scripted model that answers from whatever spans the prompt showed it.

    A batch compiles incrementally - the first document is planned against its
    own spans, the second against those plus its own - so a fake that always
    cited every span would cite spans the compiler had not offered yet. Reading
    the prompt keeps the stand-in honest about what it can see.
    """
    spans_by_id = {
        span.span_id: span
        for filename, text in documents
        for span in spans_of(filename, text)
    }

    def visible(text: str):
        return [span for span_id, span in spans_by_id.items() if span_id in text]

    def handler(request):
        if request.stage == "document_decision":
            return dumps(
                {"action": "update", "reason": "公司制度", "supersedes_document_ids": []}
            )
        if request.stage == "topic_plan":
            return dumps({"pages": heading_plan(visible(request.user))})
        if request.stage.startswith("page_compilation:"):

            def page_for(topic, section):
                # Only this page's own section, so the fake cannot cite a
                # sibling page's spans.
                group = visible(section)
                return {
                    "title": topic,
                    "summary": f"{topic}：{group[0].text}",
                    "aliases": [],
                    "claims": [
                        {
                            "text": span.text,
                            "source_span_ids": [span.span_id],
                            "existing_claim_id": None,
                        }
                        for span in group
                    ],
                }

            return batch_reply(request, page_for)
        raise AssertionError(f"unscripted stage {request.stage!r}")

    return ScriptedModel(handler)


class FailingModel:
    """Answers the decision, then produces a plan citing a span that is not there."""

    def __init__(self):
        self.stages = []

    def generate(self, request):
        self.stages.append(request.stage)
        if request.stage == "document_decision":
            return dumps(
                {"action": "update", "reason": "制度", "supersedes_document_ids": []}
            )
        if request.stage == "topic_plan":
            return dumps(
                {
                    "pages": [
                        {
                            "topic": "请假制度",
                            "existing_page_id": None,
                            "source_span_ids": ["span-not-real"],
                        }
                    ]
                }
            )
        raise AssertionError(request.stage)


class IgnoringModel:
    def __init__(self):
        self.stages = []

    def generate(self, request):
        self.stages.append(request.stage)
        return dumps(
            {"action": "ignore", "reason": "与制度无关", "supersedes_document_ids": []}
        )


class RuntimeTestCase(unittest.TestCase):
    """A runtime rooted in a temporary directory."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name) / "wiki"
        self.addCleanup(self.temp_directory.cleanup)

    def runtime(self, model) -> WikiRuntime:
        return WikiRuntime(hold_as_draft=False, root=self.root, model=model)

    def run_job(self, runtime, documents):
        job_id = runtime.submit(documents)
        self.assertTrue(runtime.wait(job_id, WAIT_SECONDS), "job did not finish")
        return runtime.status(job_id)


# --------------------------------------------------------------------------
# Job lifecycle
# --------------------------------------------------------------------------


class JobLifecycleTests(RuntimeTestCase):
    def test_a_successful_job_publishes_and_reports_its_stages(self):
        model = model_for(("rules.md", LEAVE_DOC))
        runtime = self.runtime(model)

        status = self.run_job(runtime, [("rules.md", LEAVE_DOC)])

        self.assertEqual(status["status"], JobStatus.PUBLISHED.value)
        self.assertEqual(status["files"], ["rules.md"])
        self.assertEqual(status["completed_documents"], 1)
        self.assertEqual(status["total_documents"], 1)
        self.assertIsNone(status["error"])
        self.assertEqual(status["current_build_id"], "build-0001")
        self.assertIsNone(status["stage"], "a finished job reports no stage")

        # Two model calls for the whole corpus. The model decides the structure;
        # the program writes the pages, so `page_compilation` never runs.
        self.assertEqual(model.stages, ["document_decision", "topic_plan"])

    def test_stage_is_visible_while_the_job_runs(self):
        seen = []
        release = threading.Event()
        inner = model_for(("rules.md", LEAVE_DOC))

        class Blocking:
            def generate(self, request):
                if request.stage == "topic_plan":
                    seen.append(runtime.status(job_id)["stage"])
                    release.wait(WAIT_SECONDS)
                return inner.generate(request)

        runtime = self.runtime(Blocking())
        job_id = runtime.submit([("rules.md", LEAVE_DOC)])
        try:
            for _ in range(200):
                if seen:
                    break
                time.sleep(0.01)
            self.assertEqual(seen, ["topic_plan"])
            self.assertEqual(runtime.status(job_id)["status"], JobStatus.RUNNING.value)
        finally:
            release.set()
        self.assertTrue(runtime.wait(job_id, WAIT_SECONDS))

    def test_an_ignored_document_publishes_nothing(self):
        runtime = self.runtime(IgnoringModel())

        status = self.run_job(runtime, [("notes.md", LEAVE_DOC)])

        self.assertEqual(status["status"], JobStatus.IGNORED.value)
        self.assertIsNone(status["current_build_id"])
        self.assertIsNone(runtime.published_pages())

    def test_a_failed_compile_leaves_the_published_build_in_place(self):
        published = self.run_job(
            self.runtime(model_for(("rules.md", LEAVE_DOC))),
            [("rules.md", LEAVE_DOC)],
        )
        self.assertEqual(published["current_build_id"], "build-0001")
        live_before = WikiRepository(self.root).load_current_build()

        failing = self.runtime(FailingModel())
        status = self.run_job(failing, [("remote.md", REMOTE_DOC)])

        self.assertEqual(status["status"], JobStatus.FAILED.value)
        self.assertIn("span-not-real", status["error"])
        self.assertEqual(status["current_build_id"], "build-0001")
        self.assertEqual(failing.published_pages(), live_before.pages)

    def test_a_headingless_text_document_compiles(self):
        runtime = self.runtime(model_for(("notes.txt", PLAIN_TEXT_DOC)))

        status = self.run_job(runtime, [("notes.txt", PLAIN_TEXT_DOC)])

        self.assertEqual(status["status"], JobStatus.PUBLISHED.value)
        pages = runtime.published_pages()
        locators = [claim.locator for page in pages for claim in page.claims]
        self.assertTrue(locators)
        self.assertTrue(all(locator.startswith("span:") for locator in locators))

    def test_status_for_an_unknown_or_absent_job(self):
        runtime = self.runtime(model_for(("rules.md", LEAVE_DOC)))
        idle = runtime.status()
        self.assertIsNone(idle["job_id"])
        self.assertEqual(idle["status"], IDLE_STATUS)
        self.assertEqual(idle["total_documents"], 0)
        self.assertIsNone(idle["current_build_id"])
        self.assertEqual(runtime.status("wiki-job-9999")["status"], IDLE_STATUS)


# --------------------------------------------------------------------------
# Batches and ordering
# --------------------------------------------------------------------------


class BatchTests(RuntimeTestCase):
    def test_a_batch_publishes_only_the_final_build(self):
        documents = [("rules.md", LEAVE_DOC), ("remote.md", REMOTE_DOC)]
        runtime = self.runtime(model_for(*documents))

        status = self.run_job(runtime, documents)

        self.assertEqual(status["status"], JobStatus.PUBLISHED.value)
        self.assertEqual(status["completed_documents"], 2)
        repository = WikiRepository(self.root)
        # Two drafts were built; only the last one is live.
        self.assertEqual(
            [record.build_id for record in repository.list_builds()],
            ["build-0001", "build-0002"],
        )
        self.assertEqual(repository.get_current_build_id(), "build-0002")
        # The second draft was built on the first, so the batch reads as one edit.
        self.assertEqual(repository.load_build("build-0002").base_build_id, "build-0001")
        # And both documents are in the live build's provenance.
        self.assertEqual(
            sorted(repository.load_current_build().document_version_map),
            sorted(derive_document_id(name) for name, _ in documents),
        )

    def test_a_batch_that_fails_halfway_publishes_nothing(self):
        documents = [("rules.md", LEAVE_DOC), ("remote.md", REMOTE_DOC)]
        good = model_for(*documents)

        class FailsOnSecond:
            def __init__(self):
                self.documents_seen = 0

            def generate(self, request):
                if request.stage == "document_decision":
                    self.documents_seen += 1
                if self.documents_seen == 2 and request.stage == "topic_plan":
                    return dumps(
                        {
                            "pages": [
                                {
                                    "topic": "远程办公",
                                    "existing_page_id": None,
                                    "source_span_ids": ["span-not-real"],
                                }
                            ]
                        }
                    )
                return good.generate(request)

        runtime = self.runtime(FailsOnSecond())
        status = self.run_job(runtime, documents)

        self.assertEqual(status["status"], JobStatus.FAILED.value)
        self.assertIsNone(status["current_build_id"])
        self.assertIsNone(runtime.published_pages())
        # The first document's draft exists but was never published.
        repository = WikiRepository(self.root)
        self.assertEqual(
            [record.status.value for record in repository.list_builds()], ["draft"]
        )

    def test_jobs_run_strictly_one_at_a_time(self):
        concurrent = []
        running = threading.Lock()
        inner = model_for(("rules.md", LEAVE_DOC), ("remote.md", REMOTE_DOC))

        class Serial:
            def generate(self, request):
                if request.stage == "document_decision":
                    if not running.acquire(blocking=False):
                        concurrent.append(request.stage)
                    else:
                        time.sleep(0.05)
                        running.release()
                return inner.generate(request)

        runtime = self.runtime(Serial())
        first = runtime.submit([("rules.md", LEAVE_DOC)])
        second = runtime.submit([("remote.md", REMOTE_DOC)])

        # FIFO: the second job is still queued while the first one runs.
        self.assertIn(
            runtime.status(second)["status"],
            (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
        )
        self.assertTrue(runtime.wait(first, WAIT_SECONDS))
        self.assertTrue(runtime.wait(second, WAIT_SECONDS))

        self.assertEqual(concurrent, [], "two jobs compiled at the same time")
        self.assertEqual(runtime.status(first)["status"], JobStatus.PUBLISHED.value)
        self.assertEqual(runtime.status(second)["status"], JobStatus.PUBLISHED.value)
        # The second job built on the first job's published build.
        repository = WikiRepository(self.root)
        self.assertEqual(repository.get_current_build_id(), "build-0002")
        self.assertEqual(repository.load_build("build-0002").base_build_id, "build-0001")


# --------------------------------------------------------------------------
# Invalidation
# --------------------------------------------------------------------------


class InvalidationTests(RuntimeTestCase):
    def test_a_queued_job_never_publishes_after_a_clear(self):
        release = threading.Event()
        started = threading.Event()
        inner = model_for(("rules.md", LEAVE_DOC), ("remote.md", REMOTE_DOC))

        class Blocking:
            def generate(self, request):
                if request.stage == "document_decision":
                    started.set()
                    release.wait(WAIT_SECONDS)
                return inner.generate(request)

        runtime = self.runtime(Blocking())
        first = runtime.submit([("rules.md", LEAVE_DOC)])
        second = runtime.submit([("remote.md", REMOTE_DOC)])
        self.assertTrue(started.wait(WAIT_SECONDS))

        runtime.clear()
        release.set()

        self.assertTrue(runtime.wait(first, WAIT_SECONDS))
        self.assertTrue(runtime.wait(second, WAIT_SECONDS))
        self.assertEqual(runtime.status(first)["status"], JobStatus.CANCELLED.value)
        self.assertEqual(runtime.status(second)["status"], JobStatus.CANCELLED.value)
        self.assertIsNone(runtime.current_build_id())
        self.assertIsNone(runtime.published_pages())

    def test_clearing_retracts_the_live_build_but_keeps_the_files(self):
        documents = [("rules.md", LEAVE_DOC)]
        runtime = self.runtime(model_for(*documents))
        self.run_job(runtime, documents)
        self.assertEqual(runtime.current_build_id(), "build-0001")
        build_bytes = (self.root / "builds" / "build-0001.json").read_bytes()

        retracted = runtime.clear()

        self.assertEqual(retracted, "build-0001")
        # The Wiki stops answering ...
        self.assertIsNone(runtime.current_build_id())
        self.assertIsNone(runtime.published_pages())
        self.assertFalse((self.root / "current.json").exists())
        # ... but nothing was destroyed.
        repository = WikiRepository(self.root)
        self.assertEqual(
            [record.build_id for record in repository.list_builds()], ["build-0001"]
        )
        self.assertEqual(
            (self.root / "builds" / "build-0001.json").read_bytes(), build_bytes
        )
        self.assertTrue(repository.list_document_ids())
        self.assertEqual(
            repository.get_build_record("build-0001").status.value, "archived"
        )
        # A finished job is history, not pending work.
        self.assertEqual(runtime.status()["status"], JobStatus.PUBLISHED.value)

    def test_a_retracted_build_does_not_come_back_for_a_fresh_runtime(self):
        documents = [("rules.md", LEAVE_DOC)]
        first = self.runtime(model_for(*documents))
        self.run_job(first, documents)
        first.clear()

        # A new process reading the same root must not resurrect it: the
        # retraction is on disk, not just in this runtime's cache.
        second = self.runtime(model_for(*documents))
        self.assertIsNone(second.current_build_id())
        self.assertIsNone(second.published_pages())
        self.assertIsNone(WikiRepository(self.root).load_current_build())

        with patch.object(wiki_runtime, "RUNTIME", second):
            self.assertEqual(
                chat_orchestration.current_wiki_pages(), chat_orchestration.WIKI_PAGES
            )

    def test_a_retracted_build_can_still_be_published_again(self):
        documents = [("rules.md", LEAVE_DOC)]
        runtime = self.runtime(model_for(*documents))
        self.run_job(runtime, documents)
        runtime.clear()

        WikiRepository(self.root).publish("build-0001")

        self.assertEqual(runtime.current_build_id(), "build-0001")

    def test_clearing_while_a_job_runs_leaves_nothing_published(self):
        """The clear lands between the last document and the publish."""
        published = threading.Event()
        at_last_stage = threading.Event()
        release = threading.Event()
        inner = model_for(("rules.md", LEAVE_DOC))

        class Blocking:
            def generate(self, request):
                answer = inner.generate(request)
                # The last model call before the build is written.
                if request.stage == "topic_plan":
                    at_last_stage.set()
                    release.wait(WAIT_SECONDS)
                return answer

        runtime = self.runtime(Blocking())
        job_id = runtime.submit([("rules.md", LEAVE_DOC)])
        self.assertTrue(at_last_stage.wait(WAIT_SECONDS))

        runtime.clear()
        release.set()
        self.assertTrue(runtime.wait(job_id, WAIT_SECONDS))
        published.set()

        self.assertEqual(runtime.status(job_id)["status"], JobStatus.CANCELLED.value)
        self.assertIsNone(runtime.current_build_id())
        self.assertIsNone(runtime.published_pages())
        self.assertFalse((self.root / "current.json").exists())


# --------------------------------------------------------------------------
# The live Wiki the chat path reads
# --------------------------------------------------------------------------


class LiveWikiTests(RuntimeTestCase):
    def test_the_static_wiki_answers_before_any_build_exists(self):
        runtime = self.runtime(model_for(("rules.md", LEAVE_DOC)))
        with patch.object(wiki_runtime, "RUNTIME", runtime):
            pages = chat_orchestration.current_wiki_pages()
        self.assertEqual(pages, chat_orchestration.WIKI_PAGES)
        self.assertTrue(pages)
        self.assertFalse(self.root.exists(), "reading the pointer must not create it")

    def test_a_published_build_replaces_the_static_wiki_without_a_restart(self):
        runtime = self.runtime(model_for(("rules.md", LEAVE_DOC)))
        with patch.object(wiki_runtime, "RUNTIME", runtime):
            before = chat_orchestration.current_wiki_pages()
            self.run_job(runtime, [("rules.md", LEAVE_DOC)])
            after = chat_orchestration.current_wiki_pages()

        self.assertEqual(before, chat_orchestration.WIKI_PAGES)
        self.assertNotEqual(after, before)
        self.assertEqual([page.title for page in after], ["请假制度"])

    def test_publishing_again_is_picked_up(self):
        documents = [("rules.md", LEAVE_DOC)]
        runtime = self.runtime(model_for(*documents, ("remote.md", REMOTE_DOC)))
        self.run_job(runtime, documents)
        first = runtime.published_pages()

        self.run_job(runtime, [("remote.md", REMOTE_DOC)])
        second = runtime.published_pages()

        self.assertNotEqual(first, second)
        self.assertEqual(
            sorted(page.title for page in second), ["请假制度", "远程办公"]
        )
        self.assertEqual(
            sorted(claim.source for page in second for claim in page.claims),
            ["remote.md", "rules.md"],
        )


# --------------------------------------------------------------------------
# API wiring
# --------------------------------------------------------------------------


class UploadEndpointTests(RuntimeTestCase):
    def setUp(self):
        super().setUp()
        self.original_storage = api.storage
        api.storage = SQLiteStorage(Path(self.temp_directory.name) / "test.db")
        self.addCleanup(lambda: setattr(api, "storage", self.original_storage))
        with api.state_lock:
            api.chunks.clear()
        self.client = TestClient(api.app)

        # Indexing must not call Ollama for embeddings.
        self.build_index = patch.object(
            api, "build_index", side_effect=lambda parsed, stats: parsed
        ).start()
        self.addCleanup(patch.stopall)

    def upload(self, *documents):
        files = [
            ("files", (name, io.BytesIO(text.encode("utf-8")), "text/markdown"))
            for name, text in documents
        ]
        return self.client.post("/api/knowledge/upload", files=files)

    def test_upload_returns_before_the_model_runs(self):
        release = threading.Event()
        entered = threading.Event()
        inner = model_for(("rules.md", LEAVE_DOC))

        class Blocking:
            def generate(self, request):
                entered.set()
                release.wait(WAIT_SECONDS)
                return inner.generate(request)

        runtime = self.runtime(Blocking())
        with patch.object(wiki_runtime, "RUNTIME", runtime):
            response = self.upload(("rules.md", LEAVE_DOC))
            body = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["files"], ["rules.md"])
            job_id = body["wiki_job_id"]
            self.assertTrue(job_id)

            # The upload returned while the compiler is still blocked.
            self.assertTrue(entered.wait(WAIT_SECONDS))
            status = self.client.get("/api/wiki/status").json()
            self.assertEqual(status["job_id"], job_id)
            self.assertEqual(status["status"], JobStatus.RUNNING.value)

            release.set()
            self.assertTrue(runtime.wait(job_id, WAIT_SECONDS))
            final = self.client.get(f"/api/wiki/status?job_id={job_id}").json()

        self.assertEqual(final["status"], JobStatus.PUBLISHED.value)
        self.assertEqual(final["current_build_id"], "build-0001")

    def test_the_status_endpoint_reports_every_documented_field(self):
        runtime = self.runtime(model_for(("rules.md", LEAVE_DOC)))
        with patch.object(wiki_runtime, "RUNTIME", runtime):
            job_id = self.upload(("rules.md", LEAVE_DOC)).json()["wiki_job_id"]
            self.assertTrue(runtime.wait(job_id, WAIT_SECONDS))
            body = self.client.get("/api/wiki/status").json()

        for field in (
            "job_id",
            "status",
            "stage",
            "files",
            "completed_documents",
            "total_documents",
            "current_build_id",
            "error",
        ):
            self.assertIn(field, body)
        self.assertEqual(body["files"], ["rules.md"])
        self.assertEqual(body["total_documents"], 1)

    def test_clearing_the_knowledge_base_voids_a_pending_job(self):
        release = threading.Event()
        entered = threading.Event()
        inner = model_for(("rules.md", LEAVE_DOC))

        class Blocking:
            def generate(self, request):
                entered.set()
                release.wait(WAIT_SECONDS)
                return inner.generate(request)

        runtime = self.runtime(Blocking())
        with patch.object(wiki_runtime, "RUNTIME", runtime):
            job_id = self.upload(("rules.md", LEAVE_DOC)).json()["wiki_job_id"]
            self.assertTrue(entered.wait(WAIT_SECONDS))

            self.assertEqual(self.client.delete("/api/knowledge").status_code, 200)
            release.set()
            self.assertTrue(runtime.wait(job_id, WAIT_SECONDS))
            body = self.client.get("/api/wiki/status").json()

        self.assertEqual(body["status"], JobStatus.CANCELLED.value)
        self.assertIsNone(body["current_build_id"])
        self.assertIsNone(runtime.published_pages())

    def test_a_multi_file_upload_is_one_job(self):
        documents = [("rules.md", LEAVE_DOC), ("remote.md", REMOTE_DOC)]
        runtime = self.runtime(model_for(*documents))
        with patch.object(wiki_runtime, "RUNTIME", runtime):
            body = self.upload(*documents).json()
            job_id = body["wiki_job_id"]
            self.assertTrue(runtime.wait(job_id, WAIT_SECONDS))
            status = self.client.get("/api/wiki/status").json()

        self.assertEqual(status["files"], ["rules.md", "remote.md"])
        self.assertEqual(status["total_documents"], 2)
        self.assertEqual(status["status"], JobStatus.PUBLISHED.value)


class ImportPurityTests(unittest.TestCase):
    def test_constructing_a_runtime_and_reading_it_creates_nothing(self):
        """The lazy repository is what keeps a chat request from creating the
        data directory just by asking which build is live."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "never-created"
            runtime = WikiRuntime(hold_as_draft=False, root=root, model=None)

            self.assertIsNone(runtime.current_build_id())
            self.assertIsNone(runtime.published_pages())
            self.assertIsNone(runtime.status()["current_build_id"])
            self.assertFalse(root.exists())

    def test_the_module_runtime_is_the_one_the_app_uses_and_this_suite_does_not(self):
        self.assertEqual(wiki_runtime.RUNTIME.root, DEFAULT_WIKI_DATA_ROOT)
        # Every test here patches RUNTIME with a temp-rooted one, so the real
        # runtime must never have been handed a job.
        self.assertIsNone(wiki_runtime.RUNTIME.status()["job_id"])


if __name__ == "__main__":
    unittest.main()
