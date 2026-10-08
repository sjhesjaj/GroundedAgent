"""Where a conversation survives a restart (M2 phase 3, docs/v2/m2-session-recovery.md).

    <data dir>/               AFTERSALES_DATA_DIR, default .aftersales-demo/ (git-ignored)
      generation.json         {"generation": "<32 hex>"}                      atomically replaced
      gen-<generation>/
        aftersales-demo.db    the business database, seeded once, when the generation is created
        checkpoints.db        the LangGraph SqliteSaver shared by every conversation of the generation
        sessions/<id>.json    {schema, persona_id, generation, head, inflight}  atomically replaced

A generation is seeded completely before generation.json names it, so a crash
while seeding leaves an unnamed directory, which the next open deletes. Reset
seeds a new generation, switches generation.json, then deletes the old one. A
session file of another generation is not a session of this one: an old
conversation can never resume against a new database.

The session file is a conversation's commit point (Conversation._write_session):
`head` is the committed checkpoint, `inflight` the marker of a submission whose
business write the head may not record yet. Both are always written together,
by one atomic replace. This module writes files only - never SQL - and reads
no clock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from .conversation_graph import build_graph, strict_serializer
from .demo_store import DEMO_DATABASE_NAME, DemoStore, seed_demo_database

logger = logging.getLogger(__name__)

DATA_DIR_ENV = "AFTERSALES_DATA_DIR"
DEFAULT_DATA_DIR = ".aftersales-demo"
GENERATION_FILE = "generation.json"
GENERATION_PREFIX = "gen-"
CHECKPOINTS_NAME = "checkpoints.db"
SESSIONS_DIR = "sessions"
SESSION_SCHEMA = 1
HEX_ID = re.compile(r"^[0-9a-f]{32}$")       # session ids and generation ids
MANIFEST_FIELDS = frozenset({"schema", "persona_id", "generation", "head", "inflight"})
MARKER_FIELDS = frozenset({"checkpoint_id", "idempotency_key", "action_name", "args_sha256"})

# os.replace on Windows fails while another process (an indexer, a virus scan)
# holds the target open: retry with an exponential backoff, 0.3 s in all.
REPLACE_ATTEMPTS = 5
REPLACE_FIRST_DELAY = 0.02
REMOVE_ATTEMPTS = 3


class PersistenceError(RuntimeError):
    """The data directory or a session file is not what this version writes."""


def data_directory(configured: str | Path | None = None) -> Path:
    """The configured directory, else AFTERSALES_DATA_DIR, else .aftersales-demo/."""
    if configured is None:
        configured = os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    return Path(configured).resolve()


def atomic_write_json(path: Path, value: dict) -> None:
    """Replace `path` with a complete document: a reader sees the old or the new one."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        delay = REPLACE_FIRST_DELAY
        for attempt in range(1, REPLACE_ATTEMPTS + 1):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS:
                    raise
                time.sleep(delay)
                delay *= 2
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path) -> dict | None:
    """A JSON object, None when the file does not exist."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise PersistenceError("unreadable file: " + path.name) from error
    if type(value) is not dict:
        raise PersistenceError("not a JSON object: " + path.name)
    return value


class DataDirectory:
    """The generations of one data directory and the pointer to the active one."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.pointer = self.root / GENERATION_FILE

    def directory(self, generation: str) -> Path:
        if not isinstance(generation, str) or not HEX_ID.fullmatch(generation):
            raise PersistenceError("invalid generation id")
        return self.root / (GENERATION_PREFIX + generation)

    def active(self) -> str | None:
        """The generation generation.json names, None for a new data directory."""
        pointer = read_json(self.pointer)
        if pointer is None:
            return None
        if set(pointer) != {"generation"}:
            raise PersistenceError("invalid generation.json")
        generation = pointer["generation"]
        if not self.directory(generation).is_dir():
            raise PersistenceError("the active generation is missing")
        return generation

    def create(self) -> str:
        """A new, fully seeded generation that nothing names yet."""
        generation = secrets.token_hex(16)
        directory = self.directory(generation)
        (directory / SESSIONS_DIR).mkdir(parents=True)
        seed_demo_database(directory / DEMO_DATABASE_NAME)
        return generation

    def activate(self, generation: str) -> None:
        self.directory(generation)
        atomic_write_json(self.pointer, {"generation": generation})

    def remove(self, generation: str) -> None:
        """Delete an inactive generation (best effort: a leftover is removed on the next open)."""
        directory = self.directory(generation)
        for attempt in range(1, REMOVE_ATTEMPTS + 1):
            try:
                shutil.rmtree(directory)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == REMOVE_ATTEMPTS:
                    logger.warning("could not delete the retired generation %s", generation)
                    return
                time.sleep(REPLACE_FIRST_DELAY * attempt)

    def remove_unnamed(self, active: str) -> None:
        """Delete every generation but the active one: retired or never activated."""
        for path in self.root.glob(GENERATION_PREFIX + "*"):
            generation = path.name[len(GENERATION_PREFIX):]
            if path.is_dir() and HEX_ID.fullmatch(generation) and generation != active:
                self.remove(generation)


class SessionFiles:
    """sessions/<id>.json of one generation: each conversation's head and in-flight marker."""

    def __init__(self, directory: Path, generation: str) -> None:
        self.directory = directory
        self.generation = generation

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not HEX_ID.fullmatch(session_id):
            raise PersistenceError("invalid session id")
        return self.directory / (session_id + ".json")

    def _validated(self, manifest: dict) -> dict:
        if set(manifest) != MANIFEST_FIELDS or type(manifest["schema"]) is not int \
                or manifest["schema"] != SESSION_SCHEMA:
            raise PersistenceError("unsupported session file")
        if manifest["generation"] != self.generation:
            raise PersistenceError("a session file of another generation")
        persona_id, head, marker = manifest["persona_id"], manifest["head"], manifest["inflight"]
        if not isinstance(persona_id, str) or not persona_id or not isinstance(head, str) or not head:
            raise PersistenceError("a session file names its persona and its head")
        if marker is not None and (type(marker) is not dict or set(marker) != MARKER_FIELDS or any(
                not isinstance(value, str) or not value for value in marker.values())):
            raise PersistenceError("invalid in-flight marker")
        return manifest

    def read(self, session_id: str) -> dict | None:
        """This generation's session file, None if there is none."""
        manifest = read_json(self._path(session_id))
        if manifest is None:
            return None
        generation = manifest.get("generation")
        if isinstance(generation, str) and HEX_ID.fullmatch(generation) and generation != self.generation:
            # A file copied from another generation is not a session of this one.
            return None
        return self._validated(manifest)

    def write(self, session_id: str, *, persona_id: str, head: str,
              inflight: dict | None) -> None:
        """Commit head and marker together, in one atomic replace."""
        manifest = {"schema": SESSION_SCHEMA, "persona_id": persona_id,
                    "generation": self.generation, "head": head,
                    "inflight": None if inflight is None else dict(inflight)}
        atomic_write_json(self._path(session_id), self._validated(manifest))

    def session_ids(self) -> list[str]:
        return sorted(path.stem for path in self.directory.glob("*.json")
                      if HEX_ID.fullmatch(path.stem))

    def count(self) -> int:
        return len(self.session_ids())

    def with_inflight(self) -> list[str]:
        """Sessions whose file holds an unresolved marker (unreadable files included)."""
        found = []
        for session_id in self.session_ids():
            try:
                manifest = self.read(session_id)
            except PersistenceError:
                found.append(session_id)
                continue
            if manifest is not None and manifest["inflight"] is not None:
                found.append(session_id)
        return found


class Generation:
    """One open generation: its business store, its checkpointer and graph, its session files.

    One SqliteSaver (one connection, its own lock) and one compiled graph serve
    every conversation of the generation; a conversation is the graph thread
    named by its session id.
    """

    def __init__(self, data: DataDirectory, generation: str) -> None:
        self.generation = generation
        directory = data.directory(generation)
        self.directory = directory
        self.store = DemoStore(directory / DEMO_DATABASE_NAME)
        self._connection = None
        try:
            self._connection = sqlite3.connect(str(directory / CHECKPOINTS_NAME),
                                               check_same_thread=False)
            self.checkpointer = SqliteSaver(self._connection, serde=strict_serializer())
            self.checkpointer.setup()
            self.graph = build_graph(self.checkpointer)
        except BaseException:
            if self._connection is not None:
                self._connection.close()
            self.store.close()
            raise
        self.sessions = SessionFiles(directory / SESSIONS_DIR, generation)
        self.checkpoints_path = directory / CHECKPOINTS_NAME

    def close(self) -> None:
        self._connection.close()
        self.store.close()
