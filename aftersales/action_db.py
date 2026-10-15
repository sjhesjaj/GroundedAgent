"""Stage 6 database files and connections (docs/v2/stage6-design.md §7.1, §10.1).

Composition-root code, like aftersales/demo.py: the one Stage 6 place that
creates a database file or opens a connection. A Stage 6 database is a
file-backed SQLite database (a restart must be able to resume from it):

    aftersales/schema.sql -> base demo seed -> aftersales/action_schema.sql
    -> system_fixtures/aftersales_stage6_seed.sql

The Stage 4/5 runtime never calls this module; its in-memory, five-table,
query_only database is unchanged.

A Stage 6 database may instead live on PostgreSQL: a PgDatabase target names
one schema of one server database, and every function below hands it to
aftersales/action_db_pg.py (imported only then). Which target is used is the
composition root's choice; nothing here reads the environment.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .demo import DEMO_SEED_PATH
from .schema import SCHEMA_PATH

ACTION_SCHEMA_PATH = Path(__file__).resolve().parent / "action_schema.sql"
STAGE6_SEED_PATH = (
    Path(__file__).resolve().parent.parent / "system_fixtures" / "aftersales_stage6_seed.sql"
)

STAGE6_ACTION_TABLES = (
    "sku_variants", "human_handoff_tickets", "pending_actions", "action_receipts",
    "action_audit_events",
)

DEFAULT_BUSY_TIMEOUT_MS = 5000

PG_URL_PREFIXES = ("postgresql://", "postgres://")
PG_SCHEMA_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class PgDatabase:
    """A Stage 6 database on PostgreSQL: one schema of the server database `url` names.

    The schema plays the part of the SQLite file: created fresh and seeded once
    by create_stage6_database, then opened by name. The url may hold a
    password, so it never appears in a repr.
    """

    url: str = field(repr=False)
    schema: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith(PG_URL_PREFIXES):
            raise ValueError("a PostgreSQL url must start with postgresql://")
        if not isinstance(self.schema, str) or not PG_SCHEMA_PATTERN.match(self.schema):
            raise ValueError("a PostgreSQL schema name must match " + PG_SCHEMA_PATTERN.pattern)


def is_postgres(target: object) -> bool:
    return isinstance(target, PgDatabase)


def database_errors() -> tuple[type[BaseException], ...]:
    """sqlite3.Error, plus psycopg.Error once the PostgreSQL backend is loaded."""
    psycopg = sys.modules.get("psycopg")
    return (sqlite3.Error,) if psycopg is None else (sqlite3.Error, psycopg.Error)


def integrity_errors() -> tuple[type[BaseException], ...]:
    """A violated key or CHECK constraint, on either backend."""
    psycopg = sys.modules.get("psycopg")
    return ((sqlite3.IntegrityError,) if psycopg is None
            else (sqlite3.IntegrityError, psycopg.IntegrityError))


def drop_stage6_database(target: PgDatabase) -> None:
    """PostgreSQL only: drop the schema. (A SQLite database goes with its directory.)"""
    from .action_db_pg import drop_database
    drop_database(target)


def create_stage6_database(path: str | Path | PgDatabase) -> Path | PgDatabase:
    """Create a fresh Stage 6 database file. Refuses to touch an existing file."""
    if is_postgres(path):
        from .action_db_pg import create_database
        return create_database(path)
    target = Path(path)
    if target.exists():
        raise FileExistsError("a Stage 6 database must be created fresh")
    connection = sqlite3.connect(str(target), isolation_level=None)
    try:
        if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
            raise RuntimeError("SQLite refused WAL journal mode")
        connection.execute("PRAGMA foreign_keys = ON")
        for script in (SCHEMA_PATH, DEMO_SEED_PATH, ACTION_SCHEMA_PATH, STAGE6_SEED_PATH):
            connection.executescript(script.read_text(encoding="utf-8"))
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Stage 6 seed violates foreign keys")
    finally:
        connection.close()
    return target


def connect_writer(path: str | Path | PgDatabase, *,
                   busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """The ActionGateway's writer: explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK only.

    On PostgreSQL the gateway's own BEGIN plus row locks replace BEGIN IMMEDIATE
    (action_gateway._begin); busy_timeout_ms becomes the lock_timeout.
    """
    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be a non-negative integer")
    if is_postgres(path):
        from .action_db_pg import connect
        return connect(path, lock_timeout_ms=busy_timeout_ms)
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError("the Stage 6 database file does not exist")
    connection = sqlite3.connect(str(target), isolation_level=None, timeout=busy_timeout_ms / 1000)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = " + str(busy_timeout_ms))
    except BaseException:
        connection.close()
        raise
    return connection
