"""Stage 6 database files and connections (docs/v2/stage6-design.md §7.1, §10.1).

Composition-root code, like aftersales/demo.py: the one Stage 6 place that
creates a database file or opens a connection. A Stage 6 database is a
file-backed SQLite database (a restart must be able to resume from it):

    aftersales/schema.sql -> base demo seed -> aftersales/action_schema.sql
    -> system_fixtures/aftersales_stage6_seed.sql

The Stage 4/5 runtime never calls this module; its in-memory, five-table,
query_only database is unchanged.
"""

from __future__ import annotations

import sqlite3
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


def create_stage6_database(path: str | Path) -> Path:
    """Create a fresh Stage 6 database file. Refuses to touch an existing file."""
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


def connect_writer(path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
                   ) -> sqlite3.Connection:
    """The ActionGateway's writer: explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK only."""
    if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be a non-negative integer")
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
