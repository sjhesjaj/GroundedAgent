"""Persistable models for the iterative Wiki lifecycle.

A Wiki build is *derived* knowledge, and what makes it trustworthy over time is
a recorded provenance: which version of which document it was compiled from.
Three ideas carry that:

`SourceSpan`
    One addressable piece of a document, identified by what it says rather than
    by where it sits. Re-uploading a document leaves the unchanged pieces with
    the same id.
`DocumentSnapshot`
    The ordered spans of one document at one version. The version is a digest of
    those spans, so identical content is always the identical version - the
    clock never enters it.
`WikiBuild`
    An immutable set of `WikiPage`s plus the document versions they were
    compiled from. Publishing, archiving and rolling back never rewrite a build;
    they move the pointer and the status kept in the manifest instead. That is
    what makes a rollback a pointer move rather than a restore.

Pages reuse `orchestration.wiki_schema` unchanged: there is exactly one Wiki page
model in this system, and a build holds it rather than a copy of it.

Everything here is pure - no disk, no network, and no clock at import time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from orchestration.wiki_adapter import page_from_json
from orchestration.wiki_schema import WikiPage, validate_collection

SCHEMA_VERSION = "1.0"

DOCUMENT_VERSION_PREFIX = "v-"
DOCUMENT_VERSION_HEX_LENGTH = 12
BUILD_ID_PREFIX = "build-"
BUILD_ID_DIGITS = 4

# A document id becomes a directory name and a version becomes a file name, so
# both are restricted to shapes that cannot escape the repository root. This is
# not type defence: an id like "../.." would write outside the data directory.
DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DOCUMENT_VERSION_PATTERN = re.compile(
    rf"^{DOCUMENT_VERSION_PREFIX}[0-9a-f]{{{DOCUMENT_VERSION_HEX_LENGTH}}}$"
)
BUILD_ID_PATTERN = re.compile(rf"^{BUILD_ID_PREFIX}(\d{{{BUILD_ID_DIGITS},}})$")


def utc_now() -> str:
    """Timestamp format shared with `storage.utc_now`."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Field checks
# --------------------------------------------------------------------------


def _require_text(path: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{path} must not be empty")
    return value


def require_document_id(path: str, value: object) -> str:
    _require_text(path, value)
    assert isinstance(value, str)  # narrowed by _require_text
    if not DOCUMENT_ID_PATTERN.match(value):
        raise ValueError(
            f"{path} must match {DOCUMENT_ID_PATTERN.pattern} so it is safe as a "
            f"directory name, got {value!r}"
        )
    return value


def require_document_version(path: str, value: object) -> str:
    _require_text(path, value)
    assert isinstance(value, str)
    if not DOCUMENT_VERSION_PATTERN.match(value):
        raise ValueError(
            f"{path} must match {DOCUMENT_VERSION_PATTERN.pattern}, got {value!r}"
        )
    return value


def require_build_id(path: str, value: object) -> str:
    _require_text(path, value)
    assert isinstance(value, str)
    if not BUILD_ID_PATTERN.match(value):
        raise ValueError(
            f"{path} must match {BUILD_ID_PATTERN.pattern}, got {value!r}"
        )
    return value


def format_build_id(number: int) -> str:
    """`1` -> `build-0001`. Readable and sorts correctly until 10000 builds."""
    if number < 1:
        raise ValueError(f"build number must be at least 1, got {number}")
    return f"{BUILD_ID_PREFIX}{number:0{BUILD_ID_DIGITS}d}"


def parse_build_number(build_id: str) -> int:
    match = BUILD_ID_PATTERN.match(build_id)
    if match is None:
        raise ValueError(f"{build_id!r} is not a build id")
    return int(match.group(1))


def document_version_for(content_hash: str) -> str:
    """The short, readable version string derived from a document content hash."""
    _require_text("content_hash", content_hash)
    return f"{DOCUMENT_VERSION_PREFIX}{content_hash[:DOCUMENT_VERSION_HEX_LENGTH]}"


# --------------------------------------------------------------------------
# JSON reading helpers
# --------------------------------------------------------------------------


def _require_mapping(raw: object, path: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a JSON object, got {type(raw).__name__}")
    return raw


def _require_field(raw: dict, name: str, path: str) -> object:
    if name not in raw:
        raise ValueError(f"{path}.{name} is required")
    return raw[name]


def _require_str_field(raw: dict, name: str, path: str) -> str:
    return _require_text(f"{path}.{name}", _require_field(raw, name, path))


def _optional_str_field(raw: dict, name: str, path: str) -> str | None:
    value = _require_field(raw, name, path)
    if value is None:
        return None
    return _require_text(f"{path}.{name}", value)


def _require_int_field(raw: dict, name: str, path: str) -> int:
    value = _require_field(raw, name, path)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{path}.{name} must be an integer, got {type(value).__name__}"
        )
    return value


def _require_list_field(raw: dict, name: str, path: str) -> list:
    value = _require_field(raw, name, path)
    if not isinstance(value, list):
        raise ValueError(f"{path}.{name} must be a list, got {type(value).__name__}")
    return value


def _require_schema_version(raw: dict, path: str) -> None:
    version = _require_str_field(raw, "schema_version", path)
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path}.schema_version {version!r} is unsupported; "
            f"expected {SCHEMA_VERSION!r}"
        )


# --------------------------------------------------------------------------
# Source spans and document snapshots
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SourceSpan:
    """One addressable piece of a source document at one document version.

    `span_id` is a digest of the document id, the heading and the normalized
    text; `ordinal` records reading order and is deliberately not part of it, so
    inserting text does not renumber - and so re-identify - everything below.
    """

    span_id: str
    document_id: str
    document_version: str
    ordinal: int
    source: str
    heading: str | None
    text: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_text("SourceSpan.span_id", self.span_id)
        require_document_id("SourceSpan.document_id", self.document_id)
        require_document_version("SourceSpan.document_version", self.document_version)
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("SourceSpan.ordinal must be an integer")
        if self.ordinal < 1:
            raise ValueError("SourceSpan.ordinal must be at least 1")
        _require_text("SourceSpan.source", self.source)
        if self.heading is not None:
            _require_text("SourceSpan.heading", self.heading)
        _require_text("SourceSpan.text", self.text)
        _require_text("SourceSpan.content_hash", self.content_hash)

    @property
    def locator(self) -> str | None:
        """The `section:` locator a compiled claim would cite, when known.

        Same shape `orchestration.wiki_schema.require_locator` enforces, so a
        span can be quoted straight into a `WikiClaim` once M9B compiles pages.
        """
        return None if self.heading is None else f"section:{self.heading}"

    def to_dict(self) -> dict[str, object]:
        return {
            "span_id": self.span_id,
            "document_id": self.document_id,
            "document_version": self.document_version,
            "ordinal": self.ordinal,
            "source": self.source,
            "heading": self.heading,
            "text": self.text,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, raw: object, path: str = "span") -> SourceSpan:
        data = _require_mapping(raw, path)
        return cls(
            span_id=_require_str_field(data, "span_id", path),
            document_id=_require_str_field(data, "document_id", path),
            document_version=_require_str_field(data, "document_version", path),
            ordinal=_require_int_field(data, "ordinal", path),
            source=_require_str_field(data, "source", path),
            heading=_optional_str_field(data, "heading", path),
            text=_require_str_field(data, "text", path),
            content_hash=_require_str_field(data, "content_hash", path),
        )


@dataclass(frozen=True, kw_only=True)
class DocumentSnapshot:
    """One document's ordered spans at one content-determined version.

    `created_at` records when the snapshot was taken and is excluded from the
    version on purpose: uploading the same file twice must not look like an
    edit.
    """

    document_id: str
    filename: str
    version: str
    content_hash: str
    spans: tuple[SourceSpan, ...] = ()
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_document_id("DocumentSnapshot.document_id", self.document_id)
        _require_text("DocumentSnapshot.filename", self.filename)
        require_document_version("DocumentSnapshot.version", self.version)
        _require_text("DocumentSnapshot.content_hash", self.content_hash)
        _require_text("DocumentSnapshot.created_at", self.created_at)

        object.__setattr__(self, "spans", tuple(self.spans))
        expected_version = document_version_for(self.content_hash)
        if self.version != expected_version:
            raise ValueError(
                f"DocumentSnapshot.version {self.version!r} does not match its "
                f"content_hash, expected {expected_version!r}"
            )
        for index, span in enumerate(self.spans):
            if span.document_id != self.document_id:
                raise ValueError(
                    f"DocumentSnapshot.spans[{index}].document_id "
                    f"{span.document_id!r} does not match the snapshot's "
                    f"{self.document_id!r}"
                )
            if span.document_version != self.version:
                raise ValueError(
                    f"DocumentSnapshot.spans[{index}].document_version "
                    f"{span.document_version!r} does not match the snapshot's "
                    f"{self.version!r}"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "document_id": self.document_id,
            "filename": self.filename,
            "version": self.version,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "spans": [span.to_dict() for span in self.spans],
        }

    @classmethod
    def from_dict(cls, raw: object, path: str = "snapshot") -> DocumentSnapshot:
        data = _require_mapping(raw, path)
        _require_schema_version(data, path)
        spans = tuple(
            SourceSpan.from_dict(span, f"{path}.spans[{index}]")
            for index, span in enumerate(_require_list_field(data, "spans", path))
        )
        return cls(
            document_id=_require_str_field(data, "document_id", path),
            filename=_require_str_field(data, "filename", path),
            version=_require_str_field(data, "version", path),
            content_hash=_require_str_field(data, "content_hash", path),
            created_at=_require_str_field(data, "created_at", path),
            spans=spans,
        )


# --------------------------------------------------------------------------
# Builds
# --------------------------------------------------------------------------


class BuildStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class DocumentVersion:
    """Which version of one document a build was compiled from."""

    document_id: str
    version: str

    def __post_init__(self) -> None:
        require_document_id("DocumentVersion.document_id", self.document_id)
        require_document_version("DocumentVersion.version", self.version)

    def to_dict(self) -> dict[str, object]:
        return {"document_id": self.document_id, "version": self.version}

    @classmethod
    def from_dict(cls, raw: object, path: str = "document_version") -> DocumentVersion:
        data = _require_mapping(raw, path)
        return cls(
            document_id=_require_str_field(data, "document_id", path),
            version=_require_str_field(data, "version", path),
        )


def normalize_document_versions(
    versions: Mapping[str, str] | Sequence[DocumentVersion],
) -> tuple[DocumentVersion, ...]:
    """Sorted by `document_id`, so a build's provenance serializes identically
    however the caller happened to order it."""
    if isinstance(versions, Mapping):
        entries = [
            DocumentVersion(document_id=document_id, version=version)
            for document_id, version in versions.items()
        ]
    else:
        entries = list(versions)

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, DocumentVersion):
            raise ValueError(
                f"document_versions entries must be DocumentVersion, "
                f"got {type(entry).__name__}"
            )
        if entry.document_id in seen:
            raise ValueError(
                f"document_versions has two entries for {entry.document_id!r}"
            )
        seen.add(entry.document_id)
    return tuple(sorted(entries, key=lambda entry: entry.document_id))


@dataclass(frozen=True, kw_only=True)
class WikiBuild:
    """An immutable set of compiled pages plus the provenance behind them.

    A build's *content* never changes after creation. Its lifecycle - draft,
    published, archived, failed - lives in the repository manifest, so
    publishing or rolling back can never rewrite what was published.
    """

    build_id: str
    base_build_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    document_versions: tuple[DocumentVersion, ...] = ()
    pages: tuple[WikiPage, ...] = ()
    # Compiler/parser identity, not business fields. Optional for V1 builds.
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_build_id("WikiBuild.build_id", self.build_id)
        if not isinstance(self.provenance, Mapping) or not all(
            isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
            for key, value in self.provenance.items()
        ):
            raise ValueError("WikiBuild.provenance must map non-empty strings to strings")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        if self.base_build_id is not None:
            require_build_id("WikiBuild.base_build_id", self.base_build_id)
        if self.base_build_id == self.build_id:
            raise ValueError("WikiBuild.base_build_id must not be the build itself")
        _require_text("WikiBuild.created_at", self.created_at)
        object.__setattr__(
            self,
            "document_versions",
            normalize_document_versions(self.document_versions),
        )
        # The one Wiki collection check in the system, reused rather than
        # reimplemented: unique page ids, globally unique claim ids, non-empty.
        object.__setattr__(
            self, "pages", validate_collection(self.pages, path="WikiBuild.pages")
        )

    @property
    def document_version_map(self) -> dict[str, str]:
        return {entry.document_id: entry.version for entry in self.document_versions}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "build_id": self.build_id,
            "base_build_id": self.base_build_id,
            "created_at": self.created_at,
            "document_versions": [
                entry.to_dict() for entry in self.document_versions
            ],
            "pages": [page.to_dict() for page in self.pages],
            **({"provenance": dict(self.provenance)} if self.provenance else {}),
        }

    @classmethod
    def from_dict(cls, raw: object, path: str = "build") -> WikiBuild:
        data = _require_mapping(raw, path)
        _require_schema_version(data, path)
        document_versions = tuple(
            DocumentVersion.from_dict(entry, f"{path}.document_versions[{index}]")
            for index, entry in enumerate(
                _require_list_field(data, "document_versions", path)
            )
        )
        # Pages are parsed by the Wiki adapter's own reader, so a build file and
        # `wiki_pages/*.json` can never drift into two page formats.
        pages = tuple(
            page_from_json(page, f"{path}.pages[{index}]")
            for index, page in enumerate(_require_list_field(data, "pages", path))
        )
        return cls(
            build_id=_require_str_field(data, "build_id", path),
            base_build_id=_optional_str_field(data, "base_build_id", path),
            created_at=_require_str_field(data, "created_at", path),
            document_versions=document_versions,
            pages=pages,
            provenance=data.get("provenance", {}),
        )


@dataclass(frozen=True, kw_only=True)
class BuildRecord:
    """A build's lifecycle state, kept beside the build rather than inside it."""

    build_id: str
    status: BuildStatus
    base_build_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    page_count: int = 0
    note: str | None = None

    def __post_init__(self) -> None:
        require_build_id("BuildRecord.build_id", self.build_id)
        if not isinstance(self.status, BuildStatus):
            raise ValueError(
                f"BuildRecord.status must be a BuildStatus, "
                f"got {type(self.status).__name__}"
            )
        if self.base_build_id is not None:
            require_build_id("BuildRecord.base_build_id", self.base_build_id)
        _require_text("BuildRecord.created_at", self.created_at)
        _require_text("BuildRecord.updated_at", self.updated_at)
        if isinstance(self.page_count, bool) or not isinstance(self.page_count, int):
            raise ValueError("BuildRecord.page_count must be an integer")
        if self.page_count < 0:
            raise ValueError("BuildRecord.page_count must not be negative")
        if self.note is not None:
            _require_text("BuildRecord.note", self.note)

    def to_dict(self) -> dict[str, object]:
        return {
            "build_id": self.build_id,
            "status": self.status.value,
            "base_build_id": self.base_build_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "page_count": self.page_count,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: object, path: str = "record") -> BuildRecord:
        data = _require_mapping(raw, path)
        status_value = _require_str_field(data, "status", path)
        try:
            status = BuildStatus(status_value)
        except ValueError as exc:
            raise ValueError(
                f"{path}.status {status_value!r} is not a known build status"
            ) from exc
        return cls(
            build_id=_require_str_field(data, "build_id", path),
            status=status,
            base_build_id=_optional_str_field(data, "base_build_id", path),
            created_at=_require_str_field(data, "created_at", path),
            updated_at=_require_str_field(data, "updated_at", path),
            page_count=_require_int_field(data, "page_count", path),
            note=_optional_str_field(data, "note", path),
        )


@dataclass(frozen=True, kw_only=True)
class CurrentPointer:
    """Which build is live. The minimum needed to answer that question."""

    build_id: str
    published_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_build_id("CurrentPointer.build_id", self.build_id)
        _require_text("CurrentPointer.published_at", self.published_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "build_id": self.build_id,
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, raw: object, path: str = "current") -> CurrentPointer:
        data = _require_mapping(raw, path)
        _require_schema_version(data, path)
        return cls(
            build_id=_require_str_field(data, "build_id", path),
            published_at=_require_str_field(data, "published_at", path),
        )
