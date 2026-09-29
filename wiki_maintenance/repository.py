"""File-backed storage for document snapshots and Wiki builds.

Layout under the injected root:

```text
<root>/
├── documents/<document_id>/<version>.json   one document snapshot per version
├── builds/<build_id>.json                   one immutable build per file
├── manifest.json                            every build's lifecycle state
└── current.json                             which build is live
```

The split is the whole point. A build file is written once and never touched
again; `manifest.json` holds the status that changes and `current.json` holds
the pointer that moves. Publishing `build-0002` and rolling back to `build-0001`
therefore rewrite neither build - a rollback restores nothing, it only points
somewhere else, which is why it cannot lose or corrupt what was published.

`manifest.json` and `current.json` are replaced through a temporary file in the
same directory, so an interrupted write leaves the previous state readable
rather than a half-written pointer.

Scope: one process, one writer. There is no lock, no lease and no transaction
across the two files; a concurrent second writer is out of scope for this
milestone and would need real coordination rather than a bigger `try`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Mapping, Sequence

from orchestration.wiki_schema import WikiPage

from .models import (
    BUILD_ID_PATTERN,
    BuildRecord,
    BuildStatus,
    CurrentPointer,
    DocumentSnapshot,
    DocumentVersion,
    SCHEMA_VERSION,
    WikiBuild,
    format_build_id,
    parse_build_number,
    require_build_id,
    require_document_id,
    require_document_version,
    utc_now,
)

DOCUMENTS_DIRECTORY = "documents"
BUILDS_DIRECTORY = "builds"
MANIFEST_FILENAME = "manifest.json"
CURRENT_FILENAME = "current.json"
TEMPORARY_SUFFIX = ".tmp"

# `data/` is the repository's local runtime area and is git-ignored, so builds,
# snapshots and pointers never reach a commit.
DEFAULT_WIKI_DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "wiki"


class WikiRepositoryError(RuntimeError):
    """A Wiki lifecycle operation that cannot be carried out as asked."""


class BuildNotFoundError(WikiRepositoryError):
    """No build with that id is recorded in the manifest."""


class BuildNotPublishableError(WikiRepositoryError):
    """The build exists but must not become the live build."""


def _write_json_atomically(path: Path, payload: object) -> None:
    """Replace `path` in one step, via a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + TEMPORARY_SUFFIX)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _span_identities(snapshot: DocumentSnapshot) -> tuple[tuple[str, str], ...]:
    """What a snapshot actually says, independent of when or as what it arrived."""
    return tuple((span.span_id, span.content_hash) for span in snapshot.spans)


def _read_json(path: Path) -> object:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


class WikiRepository:
    """Document snapshots and Wiki builds under one root directory.

    The root is injected so tests, a demo and production can never share one.
    """

    def __init__(self, root: str | Path = DEFAULT_WIKI_DATA_ROOT) -> None:
        self.root = Path(root)
        self.documents_directory = self.root / DOCUMENTS_DIRECTORY
        self.builds_directory = self.root / BUILDS_DIRECTORY
        self.manifest_path = self.root / MANIFEST_FILENAME
        self.current_path = self.root / CURRENT_FILENAME
        self.documents_directory.mkdir(parents=True, exist_ok=True)
        self.builds_directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Document snapshots
    # ------------------------------------------------------------------

    def snapshot_path(self, document_id: str, version: str) -> Path:
        require_document_id("document_id", document_id)
        require_document_version("version", version)
        return self.documents_directory / document_id / f"{version}.json"

    def save_document_snapshot(self, snapshot: DocumentSnapshot) -> Path:
        """Write one document version once, and never again.

        A version names the *content*, not the upload: `filename`, `source` and
        `created_at` are outside it on purpose, so re-uploading the same bytes
        under a new name produces the same version. Writing anyway would let the
        second upload silently rewrite when the content was first seen and what
        it was called - the provenance a snapshot exists to hold. So an existing
        snapshot of the same content is a true no-op: nothing is written and the
        first save's metadata stands.

        The short 12-hex `version` in the path is not proof of sameness; the full
        `content_hash` and the span identities are compared before trusting it.
        Anything else already at that path is a conflict and is refused rather
        than overwritten.
        """
        if not isinstance(snapshot, DocumentSnapshot):
            raise ValueError(
                f"snapshot must be a DocumentSnapshot, got {type(snapshot).__name__}"
            )
        path = self.snapshot_path(snapshot.document_id, snapshot.version)
        if path.exists():
            self._require_same_content(path, snapshot)
            return path
        _write_json_atomically(path, snapshot.to_dict())
        return path

    def _require_same_content(self, path: Path, snapshot: DocumentSnapshot) -> None:
        """Refuse to touch `path` unless it already holds this exact content."""
        try:
            existing = DocumentSnapshot.from_dict(_read_json(path), str(path))
        except (OSError, ValueError) as exc:
            raise WikiRepositoryError(
                f"{path} already exists but cannot be read as a snapshot, so it "
                f"must not be overwritten: {exc}"
            ) from exc
        if existing.content_hash != snapshot.content_hash:
            raise WikiRepositoryError(
                f"{path} already holds content_hash {existing.content_hash!r}, "
                f"not {snapshot.content_hash!r}; two different documents share "
                "the short version and neither may overwrite the other"
            )
        if _span_identities(existing) != _span_identities(snapshot):
            raise WikiRepositoryError(
                f"{path} declares the same content_hash but different spans; "
                "refusing to overwrite"
            )

    def load_document_snapshot(
        self, document_id: str, version: str
    ) -> DocumentSnapshot:
        path = self.snapshot_path(document_id, version)
        if not path.exists():
            raise WikiRepositoryError(
                f"no snapshot for document {document_id!r} at version {version!r}"
            )
        return DocumentSnapshot.from_dict(_read_json(path), str(path))

    def list_document_versions(self, document_id: str) -> tuple[str, ...]:
        require_document_id("document_id", document_id)
        directory = self.documents_directory / document_id
        if not directory.is_dir():
            return ()
        return tuple(sorted(path.stem for path in directory.glob("*.json")))

    def list_document_ids(self) -> tuple[str, ...]:
        if not self.documents_directory.is_dir():
            return ()
        return tuple(
            sorted(
                path.name
                for path in self.documents_directory.iterdir()
                if path.is_dir()
            )
        )

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _load_records(self) -> dict[str, BuildRecord]:
        if not self.manifest_path.exists():
            return {}
        raw = _read_json(self.manifest_path)
        if not isinstance(raw, dict):
            raise ValueError(f"{self.manifest_path} must be a JSON object")
        version = raw.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"{self.manifest_path} schema_version {version!r} is unsupported; "
                f"expected {SCHEMA_VERSION!r}"
            )
        entries = raw.get("builds")
        if not isinstance(entries, list):
            raise ValueError(f"{self.manifest_path}.builds must be a list")
        records: dict[str, BuildRecord] = {}
        for index, entry in enumerate(entries):
            record = BuildRecord.from_dict(entry, f"{self.manifest_path}.builds[{index}]")
            records[record.build_id] = record
        return records

    def _save_records(self, records: Mapping[str, BuildRecord]) -> None:
        ordered = sorted(records.values(), key=lambda record: parse_build_number(record.build_id))
        _write_json_atomically(
            self.manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "builds": [record.to_dict() for record in ordered],
            },
        )

    def list_builds(self) -> tuple[BuildRecord, ...]:
        """Every recorded build, oldest first."""
        records = self._load_records()
        return tuple(
            sorted(records.values(), key=lambda record: parse_build_number(record.build_id))
        )

    def get_build_record(self, build_id: str) -> BuildRecord:
        require_build_id("build_id", build_id)
        records = self._load_records()
        if build_id not in records:
            raise BuildNotFoundError(f"build {build_id!r} is not in the manifest")
        return records[build_id]

    # ------------------------------------------------------------------
    # Builds
    # ------------------------------------------------------------------

    def build_path(self, build_id: str) -> Path:
        require_build_id("build_id", build_id)
        return self.builds_directory / f"{build_id}.json"

    def _next_build_id(self, records: Mapping[str, BuildRecord]) -> str:
        """One past the highest number the manifest *or* the builds directory
        knows about, so an orphaned file from an interrupted create is never
        overwritten."""
        numbers = [parse_build_number(build_id) for build_id in records]
        for path in self.builds_directory.glob("*.json"):
            match = BUILD_ID_PATTERN.match(path.stem)
            if match is not None:
                numbers.append(int(match.group(1)))
        return format_build_id(max(numbers, default=0) + 1)

    def create_build(
        self,
        pages: Sequence[WikiPage],
        *,
        document_versions: Mapping[str, str] | Sequence[DocumentVersion] = (),
        base_build_id: str | None = None,
        created_at: str | None = None,
        provenance: Mapping[str, str] | None = None,
    ) -> WikiBuild:
        """Write a new draft build and record it.

        Every check runs before anything is written - the page collection, the
        base build, and each declared document version - so a build that fails
        any of them leaves no file and no manifest entry behind.
        """
        records = self._load_records()
        if base_build_id is not None:
            require_build_id("base_build_id", base_build_id)
            if base_build_id not in records:
                raise BuildNotFoundError(
                    f"base build {base_build_id!r} is not in the manifest"
                )

        timestamp = created_at if created_at is not None else utc_now()
        # Constructing the build is pure: it normalizes the provenance and runs
        # the page collection check without touching the disk.
        build = WikiBuild(
            build_id=self._next_build_id(records),
            base_build_id=base_build_id,
            created_at=timestamp,
            document_versions=document_versions,
            pages=pages,
            provenance={} if provenance is None else provenance,
        )
        self._require_resolvable_provenance(build)
        _write_json_atomically(self.build_path(build.build_id), build.to_dict())
        records[build.build_id] = BuildRecord(
            build_id=build.build_id,
            status=BuildStatus.DRAFT,
            base_build_id=build.base_build_id,
            created_at=build.created_at,
            updated_at=timestamp,
            page_count=len(build.pages),
        )
        self._save_records(records)
        return build

    def _require_resolvable_provenance(self, build: WikiBuild) -> None:
        """Every document version a build cites must load from this repository.

        A build says which document versions it was compiled from; if one of
        them was never saved, that sentence is not true and the trail from a
        claim back to its source is already broken at creation. An empty
        `document_versions` claims nothing and is therefore fine - that is what
        the static sample Wiki bootstrap uses.
        """
        for entry in build.document_versions:
            try:
                snapshot = self.load_document_snapshot(entry.document_id, entry.version)
            except (WikiRepositoryError, OSError, ValueError) as exc:
                raise WikiRepositoryError(
                    f"build cites document {entry.document_id!r} at version "
                    f"{entry.version!r}, which has no readable snapshot: {exc}"
                ) from exc
            if (
                snapshot.document_id != entry.document_id
                or snapshot.version != entry.version
            ):
                raise WikiRepositoryError(
                    f"snapshot at {entry.document_id!r}/{entry.version!r} declares "
                    f"{snapshot.document_id!r}/{snapshot.version!r} instead"
                )

    def load_build(self, build_id: str) -> WikiBuild:
        path = self.build_path(build_id)
        if not path.exists():
            raise BuildNotFoundError(f"build file for {build_id!r} is missing")
        build = WikiBuild.from_dict(_read_json(path), str(path))
        if build.build_id != build_id:
            raise ValueError(
                f"{path} declares build_id {build.build_id!r}, expected {build_id!r}"
            )
        return build

    def mark_failed(self, build_id: str, reason: str) -> BuildRecord:
        """Record that a build must never be published.

        The live build is refused: a build that is currently serving readers has
        demonstrably not failed, and marking it so would leave the manifest
        contradicting `current.json`.
        """
        records = self._load_records()
        if build_id not in records:
            raise BuildNotFoundError(f"build {build_id!r} is not in the manifest")
        if build_id == self.get_current_build_id():
            raise BuildNotPublishableError(
                f"build {build_id!r} is the current published build and cannot be "
                "marked failed; publish or roll back to another build first"
            )
        record = records[build_id]
        updated = BuildRecord(
            build_id=record.build_id,
            status=BuildStatus.FAILED,
            base_build_id=record.base_build_id,
            created_at=record.created_at,
            updated_at=utc_now(),
            page_count=record.page_count,
            note=reason,
        )
        records[build_id] = updated
        self._save_records(records)
        return updated

    # ------------------------------------------------------------------
    # Current pointer
    # ------------------------------------------------------------------

    def get_current_build_id(self) -> str | None:
        """The live build id, or `None` when nothing has been published yet."""
        pointer = self.load_current_pointer()
        return None if pointer is None else pointer.build_id

    def load_current_pointer(self) -> CurrentPointer | None:
        if not self.current_path.exists():
            return None
        return CurrentPointer.from_dict(
            _read_json(self.current_path), str(self.current_path)
        )

    def load_current_build(self) -> WikiBuild | None:
        """The live build, or `None` when nothing has been published yet.

        A pointer at a build whose file is missing is an error, not an empty
        state: silently answering `None` would hide a lost build behind the same
        answer as a fresh repository.
        """
        build_id = self.get_current_build_id()
        if build_id is None:
            return None
        return self.load_build(build_id)

    def _switch_current(self, build_id: str, *, require_archived: bool) -> BuildRecord:
        records = self._load_records()
        if build_id not in records:
            raise BuildNotFoundError(f"build {build_id!r} is not in the manifest")
        record = records[build_id]
        if record.status is BuildStatus.FAILED:
            note = f": {record.note}" if record.note else ""
            raise BuildNotPublishableError(
                f"build {build_id!r} is marked failed and cannot be published{note}"
            )
        if require_archived and record.status is not BuildStatus.ARCHIVED:
            raise BuildNotPublishableError(
                f"build {build_id!r} has status {record.status.value!r}; rollback "
                "targets a previously published build"
            )
        # Loading proves the build file is present and parseable before the
        # pointer moves, so publishing can never point at something unreadable.
        try:
            build = self.load_build(build_id)
        except (OSError, ValueError) as exc:
            raise BuildNotPublishableError(
                f"build {build_id!r} cannot be loaded and must not be published: {exc}"
            ) from exc

        timestamp = utc_now()
        for other_id, other in list(records.items()):
            if other_id != build_id and other.status is BuildStatus.PUBLISHED:
                records[other_id] = _with_status(other, BuildStatus.ARCHIVED, timestamp)
        published = _with_status(record, BuildStatus.PUBLISHED, timestamp)
        records[build_id] = published

        # Manifest first, pointer second. Each write is atomic but the pair is
        # not, and this is the order whose half-done state is the safe one: if
        # the pointer never lands, readers keep serving the build they were
        # already serving and the manifest disagrees visibly. The reverse would
        # switch what readers see while the manifest still called it a draft.
        self._save_records(records)
        _write_json_atomically(
            self.current_path,
            CurrentPointer(build_id=build.build_id, published_at=timestamp).to_dict(),
        )
        return published

    def publish(self, build_id: str) -> BuildRecord:
        """Make `build_id` the live build; the previous one becomes archived."""
        return self._switch_current(build_id, require_archived=False)

    def retract_current(self) -> str | None:
        """Take the live build offline, deleting nothing.

        Returns the build id that was retracted, or `None` if nothing was live.
        Builds and snapshots stay on disk: the history is still true, it is only
        the claim "this is what the Wiki says now" that is being withdrawn. A
        retracted build can be published again with `publish`.

        The pointer goes first here, the opposite of `_switch_current`. The goal
        of a publish is to serve something, so its safe half-done state is
        "still serving the old build"; the goal of a retraction is to stop
        serving, so its safe half-done state is "already stopped".
        """
        build_id = self.get_current_build_id()
        if self.current_path.exists():
            self.current_path.unlink()

        records = self._load_records()
        timestamp = utc_now()
        retracted = {
            other_id: _with_status(other, BuildStatus.ARCHIVED, timestamp)
            for other_id, other in records.items()
            if other.status is BuildStatus.PUBLISHED
        }
        if retracted:
            self._save_records({**records, **retracted})
        return build_id

    def rollback(self, build_id: str) -> BuildRecord:
        """Return to a previously published build.

        Restricted to an archived build on purpose: rolling *back* to something
        that was never live is just a publish, and saying so keeps the two
        intentions distinguishable in a log.
        """
        return self._switch_current(build_id, require_archived=True)


def _with_status(
    record: BuildRecord, status: BuildStatus, timestamp: str
) -> BuildRecord:
    return BuildRecord(
        build_id=record.build_id,
        status=status,
        base_build_id=record.base_build_id,
        created_at=record.created_at,
        updated_at=timestamp,
        page_count=record.page_count,
        note=record.note,
    )
