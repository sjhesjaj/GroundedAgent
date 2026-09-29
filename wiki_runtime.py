"""Background Wiki compilation, and the live Wiki the chat path reads.

Two jobs, both owned here so `api.py` gains only wiring:

**A single-worker FIFO queue.** One upload becomes one job; one thread runs one
job at a time. The point is not throughput but the opposite: a 4B model
compiling two documents at once on a laptop makes both slow and neither
finishes sooner, so the queue is deliberately serial.

**The published-build pointer that the chat path reads per request.** Publishing
a build must take effect without restarting the API, so `current.json` is
consulted on each read and the pages are cached against the build id it names.

A batch defaults to hold-as-draft for explicit review. Each document compiles
onto the preceding draft. Legacy callers can explicitly set hold_as_draft=False
for the existing all-or-nothing publish/retirement path. A failed batch leaves
the live pointer intact in either mode. After-sales policy operations use
aftersales.policy_lifecycle and the same repository's publish/rollback methods.

Job state lives in memory. It is lost on restart, which is acceptable for a
progress indicator and is not acceptable for the builds themselves - those are
on disk, which is why a lost job never loses compiled work.

Nothing here runs at import: no thread starts and no directory is created until
a job is submitted.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from queue import Queue

from orchestration.wiki_schema import WikiPage
from wiki_maintenance import (
    DocumentAction,
    WikiMaintainer,
    WikiRepository,
    build_document_snapshot_from_text,
    derive_document_id,
)
from wiki_maintenance.compiler import ModelRequest, WikiModel
from wiki_maintenance.repository import CURRENT_FILENAME, DEFAULT_WIKI_DATA_ROOT


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DRAFT = "draft"
    IGNORED = "ignored"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Reported when no job has ever been submitted. Not a job state.
IDLE_STATUS = "idle"

_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)

#: Retires the source text of documents a published build superseded.
#: Takes the document ids and the knowledge version the job compiled from, and
#: returns the ids it actually removed - fewer, if a newer upload overtook it.
RetireDocuments = Callable[[Sequence[str], "int | None"], "Sequence[str]"]


@dataclass
class WikiJob:
    """One upload batch, compiled in upload order."""

    job_id: str
    documents: tuple[tuple[str, str], ...]
    epoch: int
    status: JobStatus = JobStatus.QUEUED
    stage: str | None = None
    completed_documents: int = 0
    error: str | None = None
    published_build_id: str | None = None
    draft_build_id: str | None = None
    #: The knowledge-base version this batch was compiled from. A document
    #: uploaded again after that version is newer than anything this job knows,
    #: and must not be retired by it.
    knowledge_version: int | None = None
    retire_documents: RetireDocuments | None = None
    #: Every supersede the batch decided, and what retrieval actually retired.
    #: The second is smaller when the final build still cites a document, when a
    #: re-upload overtook the job, or when nothing published at all.
    superseded_document_ids: tuple[str, ...] = ()
    retired_document_ids: tuple[str, ...] = ()
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.documents)

    @property
    def total_documents(self) -> int:
        return len(self.documents)


class _ProgressModel:
    """Reports each stage as the compiler asks for it.

    The compiler already names its own stages - `document_decision`,
    `topic_plan`, `page_compilation:<topic>` - so progress is read off the
    requests it makes rather than threaded back through its signatures.
    """

    def __init__(self, inner: WikiModel, report) -> None:
        self._inner = inner
        self._report = report

    def generate(self, request: ModelRequest) -> str:
        self._report(request.stage)
        return self._inner.generate(request)


class WikiRuntime:
    """Owns the compile queue, the job log, and the live-build cache."""

    def __init__(
        self,
        *,
        root: str | Path = DEFAULT_WIKI_DATA_ROOT,
        model: WikiModel | None = None,
        hold_as_draft: bool = True,
    ) -> None:
        self.root = Path(root)
        self._model = model
        if type(hold_as_draft) is not bool:
            raise ValueError("hold_as_draft must be a bool")
        self.hold_as_draft = hold_as_draft
        self._queue: Queue[str] = Queue()
        self._lock = threading.RLock()
        self._jobs: dict[str, WikiJob] = {}
        self._order: list[str] = []
        self._epoch = 0
        self._sequence = 0
        self._worker: threading.Thread | None = None
        self._repository: WikiRepository | None = None
        self._cached_build_id: str | None = None
        self._cached_pages: tuple[WikiPage, ...] | None = None

    # ------------------------------------------------------------------
    # Lazy resources
    # ------------------------------------------------------------------

    def _get_model(self) -> WikiModel:
        if self._model is None:
            # Imported here so choosing a backend is an act, not an import.
            from wiki_maintenance.ollama_compiler import OllamaWikiModel

            self._model = OllamaWikiModel()
        return self._model

    def _get_repository(self) -> WikiRepository:
        """Constructing this creates the data directory, so it waits until
        there is actually something to store."""
        if self._repository is None:
            self._repository = WikiRepository(self.root)
        return self._repository

    def _has_published_build(self) -> bool:
        """A pure existence check: reading the live pointer must not create the
        directory tree it lives in."""
        return (self.root / CURRENT_FILENAME).exists()

    # ------------------------------------------------------------------
    # The live Wiki
    # ------------------------------------------------------------------

    def published_pages(self) -> tuple[WikiPage, ...] | None:
        """Pages of the published build, or `None` when there is no build yet.

        `None` means "fall back to the committed sample Wiki"; the caller owns
        that fallback. An unreadable build is reported the same way, matching
        how a malformed static Wiki file is already handled: the service keeps
        answering from what it can read rather than failing every request.
        """
        if not self._has_published_build():
            return None
        try:
            repository = self._get_repository()
            build_id = repository.get_current_build_id()
            if build_id is None:
                return None
            with self._lock:
                if build_id == self._cached_build_id:
                    return self._cached_pages
            pages = repository.load_build(build_id).pages
        except (OSError, ValueError, RuntimeError):
            return None
        with self._lock:
            self._cached_build_id = build_id
            self._cached_pages = pages
        return pages

    def current_build_id(self) -> str | None:
        if not self._has_published_build():
            return None
        try:
            return self._get_repository().get_current_build_id()
        except (OSError, ValueError, RuntimeError):
            return None

    def _forget_cached_pages(self) -> None:
        with self._lock:
            self._cached_build_id = None
            self._cached_pages = None

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def submit(
        self,
        documents: Sequence[tuple[str, str]],
        *,
        knowledge_version: int | None = None,
        retire_documents: RetireDocuments | None = None,
    ) -> str:
        """Queue one upload batch. Returns immediately with the job id.

        `knowledge_version` stamps the batch with the state of the retrieval
        index it was compiled from, so a retirement it decides cannot delete a
        document uploaded after it started.
        """
        prepared = tuple((str(name), str(text)) for name, text in documents)
        if not prepared:
            raise ValueError("a Wiki job needs at least one document")
        with self._lock:
            self._sequence += 1
            job = WikiJob(
                job_id=f"wiki-job-{self._sequence:04d}",
                documents=prepared,
                epoch=self._epoch,
                knowledge_version=knowledge_version,
                retire_documents=retire_documents,
            )
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._start_worker()
        self._queue.put(job.job_id)
        return job.job_id

    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._work, name="wiki-compiler", daemon=True
            )
            self._worker.start()

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs[job_id]
            try:
                self._run_job(job)
            except Exception as exc:  # one bad job must not stop the queue
                with self._lock:
                    if job.status not in (JobStatus.CANCELLED,):
                        job.status = JobStatus.FAILED
                        job.stage = None
                        job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.done.set()
                self._queue.task_done()

    def _run_job(self, job: WikiJob) -> None:
        if self._is_stale(job):
            self._cancel(job)
            return
        with self._lock:
            job.status = JobStatus.RUNNING
            job.error = None

        repository = self._get_repository()
        model = _ProgressModel(
            self._get_model(), lambda stage: self._set_stage(job, stage)
        )
        maintainer = WikiMaintainer(repository, model)

        starting_build_id = repository.get_current_build_id()
        base_build_id = starting_build_id
        superseded: list[str] = []
        for index, (filename, text) in enumerate(job.documents, start=1):
            if self._is_stale(job):
                self._cancel(job)
                return
            snapshot = build_document_snapshot_from_text(
                document_id=derive_document_id(filename),
                filename=filename,
                text=text,
            )
            repository.save_document_snapshot(snapshot)
            outcome = maintainer.ingest(snapshot, base_build_id=base_build_id)
            if outcome.action is DocumentAction.UPDATE:
                # The next document compiles onto this draft, so a batch reads
                # as one edit rather than as N competing rewrites of the Wiki.
                base_build_id = outcome.build.build_id
            # Collected, not acted on: a supersede only takes effect if the
            # batch it belongs to reaches publish.
            superseded.extend(outcome.superseded_document_ids)
            with self._lock:
                job.completed_documents = index

        # Checked and published under the lock so a clear arriving right now
        # cannot slip between the two and publish a Wiki nobody wants.
        with self._lock:
            job.superseded_document_ids = tuple(sorted(set(superseded)))
            if self._is_stale(job):
                self._cancel(job)
                return
            if base_build_id is None or base_build_id == starting_build_id:
                job.status = JobStatus.IGNORED
                job.stage = None
                return
            job.draft_build_id = base_build_id
            if self.hold_as_draft:
                job.status = JobStatus.DRAFT
                job.stage = None
                return
            repository.publish(base_build_id)
            job.stage = None
            job.published_build_id = base_build_id
            # Deliberately still RUNNING: the batch is not done until retrieval
            # agrees with it, and reporting `published` early would tell a
            # poller the two stores are in step before they are.
        self._forget_cached_pages()

        try:
            self._retire_superseded(job, repository, base_build_id)
        except Exception:
            # Retrieval could not be brought into step, so the Wiki must not
            # stay ahead of it. Undo this job's publish and fail the job.
            self._restore_previous_build(job, repository, base_build_id, starting_build_id)
            raise

        with self._lock:
            if not self._is_stale(job):
                job.status = JobStatus.PUBLISHED

    def _retirement_candidates(
        self, repository: WikiRepository, build_id: str, superseded: Sequence[str]
    ) -> tuple[str, ...]:
        """Superseded documents the published build no longer cites.

        A batch can supersede a document and then re-add a newer version of it -
        the same file uploaded again later in the same batch. The running union
        of supersede decisions still names it, but the build that actually went
        live cites it, so it is emphatically not retired. The final build is the
        authority; the decisions along the way are not.
        """
        still_cited = set(repository.load_build(build_id).document_version_map)
        return tuple(sorted(set(superseded) - still_cited))

    def _retire_superseded(
        self, job: WikiJob, repository: WikiRepository, build_id: str
    ) -> None:
        """Take the superseded documents out of retrieval.

        Runs after the build is live but before the job reports success, so a
        failure here can still be undone. The callback takes the API's upload
        lock and then its state lock; this is called holding no runtime lock, so
        there is one lock order in the process rather than two.
        """
        candidates = self._retirement_candidates(
            repository, build_id, job.superseded_document_ids
        )
        if not candidates or job.retire_documents is None:
            return
        if self._is_stale(job):
            return
        retired = job.retire_documents(candidates, job.knowledge_version)
        with self._lock:
            job.retired_document_ids = tuple(retired)

    def _restore_previous_build(
        self,
        job: WikiJob,
        repository: WikiRepository,
        published_build_id: str,
        previous_build_id: str | None,
    ) -> None:
        """Put the live pointer back where this job found it.

        Only when the pointer is still the one this job set. A clear, or a later
        job's publish, means the current Wiki is somebody else's decision and
        restoring an older build over it would undo a change the user asked for.
        """
        with self._lock:
            if self._is_stale(job):
                return
            if repository.get_current_build_id() != published_build_id:
                return
            if previous_build_id is None:
                repository.retract_current()
            else:
                repository.rollback(previous_build_id)
            job.published_build_id = None
        self._forget_cached_pages()

    def _is_stale(self, job: WikiJob) -> bool:
        with self._lock:
            return job.epoch != self._epoch

    def _cancel(self, job: WikiJob) -> None:
        with self._lock:
            job.status = JobStatus.CANCELLED
            job.stage = None

    def _set_stage(self, job: WikiJob, stage: str) -> None:
        with self._lock:
            job.stage = stage

    def clear(self) -> str | None:
        """Withdraw the Wiki when the knowledge base is cleared.

        Three things, all of them necessary for "cleared" to mean cleared:

        - every queued and running job is voided, so work compiled from
          documents that no longer exist can never publish, and a job waiting in
          the queue cannot wake up later and overwrite the Wiki;
        - the live build is retracted, because a Wiki derived from deleted
          documents must stop answering rather than outlive its sources;
        - the page cache is dropped, so the next request re-reads the pointer.

        Snapshots and builds stay on disk - the history is still true. Returns
        the build id that was retracted, or `None`.

        All of it happens under the lock that `_run_job` takes to publish, so a
        job cannot slip a publish in between the cancel and the retraction.
        """
        with self._lock:
            self._epoch += 1
            for job in self._jobs.values():
                if job.status in _ACTIVE_STATUSES:
                    job.status = JobStatus.CANCELLED
                    job.stage = None
            retracted = (
                self._get_repository().retract_current()
                if self._has_published_build()
                else None
            )
            self._cached_build_id = None
            self._cached_pages = None
        return retracted

    def wait(self, job_id: str, timeout: float | None = None) -> bool:
        """Block until a job finishes. For tests and shutdown, not for requests."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job.done.wait(timeout)

    def status(self, job_id: str | None = None) -> dict:
        """The latest job, or a named one. Never raises for an unknown id."""
        with self._lock:
            if job_id is None and self._order:
                job_id = self._order[-1]
            job = self._jobs.get(job_id) if job_id is not None else None
            snapshot = (
                None
                if job is None
                else {
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "stage": job.stage,
                    "files": list(job.files),
                    "completed_documents": job.completed_documents,
                    "total_documents": job.total_documents,
                    "error": job.error,
                    "published_build_id": job.published_build_id,
                    "draft_build_id": job.draft_build_id,
                    "superseded_document_ids": list(job.superseded_document_ids),
                    "retired_document_ids": list(job.retired_document_ids),
                }
            )
        # Read outside the lock: it touches the filesystem.
        current_build_id = self.current_build_id()
        if snapshot is None:
            return {
                "job_id": None,
                "status": IDLE_STATUS,
                "stage": None,
                "files": [],
                "completed_documents": 0,
                "total_documents": 0,
                "current_build_id": current_build_id,
                "error": None,
                "published_build_id": None,
                "draft_build_id": None,
                "superseded_document_ids": [],
                "retired_document_ids": [],
            }
        return {**snapshot, "current_build_id": current_build_id}


#: The process-wide runtime. Tests replace this with one rooted in a temp dir.
RUNTIME = WikiRuntime()
