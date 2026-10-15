"""PostgreSQL Stage 6 databases (the after-sales business + action tables only).

Composition-root code next to action_db.py, which imports this module only for
a PgDatabase target. A Stage 6 database on PostgreSQL is one schema of one
server database: created fresh and seeded once, like the SQLite file, then
opened by name through the connection's search_path:

    aftersales/pg/schema.sql -> base demo seed -> aftersales/pg/action_schema.sql
    -> system_fixtures/aftersales_stage6_seed.sql

The two seed files are the SQLite ones, loaded unchanged (plain INSERT ... VALUES).

PgConnection is the part of the sqlite3 connection API the Stage 6 code uses:
execute / cursor / in_transaction / total_changes / close, with sqlite-style
`?` placeholders. It runs psycopg in autocommit mode, so that - as with
sqlite3 isolation_level=None - a transaction exists only between the caller's
own BEGIN and COMMIT / ROLLBACK. Which BEGIN, and which row locks follow it,
is the ActionGateway's decision (action_gateway._begin), not this module's.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import pq
from psycopg import sql as pg_sql

from .action_db import STAGE6_SEED_PATH, PgDatabase
from .demo import DEMO_SEED_PATH

PG_SCHEMA_DIR = Path(__file__).resolve().parent / "pg"
PG_SCHEMA_PATH = PG_SCHEMA_DIR / "schema.sql"
PG_ACTION_SCHEMA_PATH = PG_SCHEMA_DIR / "action_schema.sql"

CONNECT_TIMEOUT_SECONDS = 5
_WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})


def _open(target: PgDatabase, *, options: str = "") -> psycopg.Connection:
    # ClientCursor binds parameters client-side, like sqlite3: an untyped
    # placeholder such as R8's `? IS NULL` needs no server-side type inference.
    return psycopg.connect(target.url, autocommit=True, cursor_factory=psycopg.ClientCursor,
                           client_encoding="UTF8", connect_timeout=CONNECT_TIMEOUT_SECONDS,
                           options=options)


def _schema_exists(raw: psycopg.Connection, schema: str) -> bool:
    return raw.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is not None


def database_exists(target: PgDatabase) -> bool:
    raw = _open(target)
    try:
        return _schema_exists(raw, target.schema)
    finally:
        raw.close()


def create_database(target: PgDatabase) -> PgDatabase:
    """Create and seed a fresh schema in one transaction. Refuses an existing one."""
    raw = _open(target)
    try:
        with raw.transaction():
            if _schema_exists(raw, target.schema):
                raise FileExistsError("a Stage 6 database must be created fresh")
            identifier = pg_sql.Identifier(target.schema)
            raw.execute(pg_sql.SQL("CREATE SCHEMA {}").format(identifier))
            raw.execute(pg_sql.SQL("SET LOCAL search_path TO {}").format(identifier))
            for script in (PG_SCHEMA_PATH, DEMO_SEED_PATH, PG_ACTION_SCHEMA_PATH, STAGE6_SEED_PATH):
                # No parameters: the whole file goes as one simple query.
                raw.execute(script.read_text(encoding="utf-8"))
        # Foreign keys are checked per statement as the seed loads (SQLite needs
        # a separate PRAGMA foreign_key_check for the same guarantee).
    finally:
        raw.close()
    return target


def drop_database(target: PgDatabase) -> None:
    raw = _open(target)
    try:
        raw.execute(pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            pg_sql.Identifier(target.schema)))
    finally:
        raw.close()


def connect(target: PgDatabase, *, lock_timeout_ms: int, read_only: bool = False) -> "PgConnection":
    """A connection whose unqualified table names resolve to the target schema.

    lock_timeout plays the part of SQLite's busy_timeout: a row lock not granted
    in time raises (LockNotAvailable) instead of waiting forever. 0 means "do
    not wait" in SQLite but "wait forever" in PostgreSQL, so it becomes 1 ms.
    read_only plays the part of PRAGMA query_only.
    """
    options = "-c search_path=" + target.schema + " -c lock_timeout=" + str(max(lock_timeout_ms, 1))
    if read_only:
        options += " -c default_transaction_read_only=on"
    raw = _open(target, options=options)
    try:
        if not _schema_exists(raw, target.schema):
            raise FileNotFoundError("the Stage 6 database schema does not exist")
    except BaseException:
        raw.close()
        raise
    return PgConnection(raw)


# --------------------------------------------------------------------------
# Row locks: what stands in for SQLite's BEGIN IMMEDIATE (action_gateway)
# --------------------------------------------------------------------------
#
# BEGIN IMMEDIATE makes a gateway transaction the database's only writer, so
# nothing the Guard reads can change between its capture and the COMMIT. On
# PostgreSQL the same holds for the rows involved:
#
# lock_anchor / lock_pending - in action_gateway._begin, right after BEGIN,
# before the Clock read (a lock not granted in time fails like a busy SQLite
# database, transaction_failed, nothing read):
#   the order and the order item   FOR UPDATE   two transactions on one order run one after the
#                                               other; and since a foreign-key check takes KEY
#                                               SHARE on the parent row, no writer can insert a
#                                               new package, case or ticket for them meanwhile
#                                               (no phantom rows in R3 / R4 / R7)
#   the pending row (T1, T2, P6)   FOR UPDATE   one decision or execution at a time
#
# lock_guard_reads - in action_gateway._capture, just before the Guard's own
# reads, as the first of them (a failure is a failed Guard read):
#   every other row the Guard reads FOR SHARE   packages, the item's cases and tickets, its other
#   (R3-R8)                                     open pendings, the variant rows: no writer can
#                                               update them meanwhile
#   the target SKU's inventory     FOR UPDATE   an executed exchange may take units from it
#                                               (C-D1); two FOR SHARE holders upgrading to write
#                                               would deadlock, so exchanges into one SKU queue
#
# Order is always: order, order item, pending row, then the Guard's other rows.
# The only gateway transactions that lock a row exclusively without first
# holding its order (T1, P6: the pending row alone) wait for nothing else, and a
# transaction holding an inventory row waits only for shared or T1-held rows,
# so the locks cannot form a cycle. The Guard's SELECTs are unchanged and run after the
# locks; under READ COMMITTED each one sees everything committed before it.

LOCK_ORDER = "SELECT order_id FROM orders WHERE order_id = ? FOR UPDATE"
LOCK_ORDER_ITEM = "SELECT order_item_id FROM order_items WHERE order_item_id = ? FOR UPDATE"
LOCK_PACKAGES = "SELECT tracking_no FROM logistics WHERE order_id = ? FOR SHARE"
LOCK_ITEM_CASES = "SELECT case_id FROM after_sales_cases WHERE order_item_id = ? FOR SHARE"
LOCK_ITEM_TICKETS = "SELECT ticket_id FROM human_handoff_tickets WHERE order_item_id = ? FOR SHARE"
LOCK_OTHER_PENDINGS = (
    "SELECT pending_action_id FROM pending_actions WHERE target_order_item_id = ?"
    " AND status IN ('PENDING_APPROVAL', 'APPROVED')"
    " AND (? IS NULL OR pending_action_id <> ?) FOR SHARE")
LOCK_TARGET_INVENTORY = "SELECT sku FROM inventory WHERE sku = ? FOR UPDATE"
LOCK_VARIANTS = (
    "SELECT sku FROM sku_variants"
    " WHERE sku IN ((SELECT sku FROM order_items WHERE order_item_id = ?), ?) FOR SHARE")
LOCK_PENDING = "SELECT pending_action_id FROM pending_actions WHERE pending_action_id = ? FOR UPDATE"
# The target columns of a pending row never change after its insert: reading
# them before any lock is safe, and it is what lets T2 lock in the same order.
PENDING_TARGET = (
    "SELECT target_order_id, target_order_item_id FROM pending_actions"
    " WHERE pending_action_id = ?")


def pending_target(connection: "PgConnection", pending_action_id: str) -> tuple[str, str] | None:
    """T2: the stored action's (order_id, order_item_id), read before any lock."""
    rows = connection.execute(PENDING_TARGET, (pending_action_id,)).fetchall()
    return tuple(rows[0]) if rows else None


def lock_anchor(connection: "PgConnection", order_id: str, order_item_id: str) -> None:
    """The order, then the order item, FOR UPDATE until the transaction ends."""
    connection.execute(LOCK_ORDER, (order_id,))
    connection.execute(LOCK_ORDER_ITEM, (order_item_id,))


def lock_pending(connection: "PgConnection", pending_action_id: str) -> None:
    connection.execute(LOCK_PENDING, (pending_action_id,))


def lock_guard_reads(connection: "PgConnection", *, order_id: str, order_item_id: str,
                     target_sku: str | None, exclude_pending_id: str | None) -> None:
    """Every other row the Guard reads for this target (a superset), FOR SHARE."""
    connection.execute(LOCK_PACKAGES, (order_id,))
    connection.execute(LOCK_ITEM_CASES, (order_item_id,))
    connection.execute(LOCK_ITEM_TICKETS, (order_item_id,))
    connection.execute(LOCK_OTHER_PENDINGS, (order_item_id, exclude_pending_id, exclude_pending_id))
    if target_sku is not None:
        connection.execute(LOCK_TARGET_INVENTORY, (target_sku,))
        connection.execute(LOCK_VARIANTS, (order_item_id, target_sku))


class PgCursor:
    """sqlite3.Cursor-shaped: execute with `?` placeholders, tuples out."""

    def __init__(self, connection: "PgConnection", raw: psycopg.ClientCursor) -> None:
        self._connection = connection
        self._raw = raw
        self.row_factory = None  # accepted and ignored: rows are always plain tuples

    def execute(self, sql: str, parameters=()) -> "PgCursor":
        parameters = tuple(parameters)
        if parameters:
            # `?` -> `%s`; a literal `%` must be doubled once placeholders are in play.
            self._raw.execute(sql.replace("%", "%%").replace("?", "%s"), parameters)
        else:
            self._raw.execute(sql)
        status = self._raw.statusmessage or ""
        verb = status.split(" ", 1)[0]
        if verb in _WRITE_VERBS and self._raw.rowcount > 0:
            self._connection._changes += self._raw.rowcount
        if sql.strip().upper() == "COMMIT" and verb != "COMMIT":
            # PostgreSQL answers COMMIT of a failed transaction with ROLLBACK and
            # no error; sqlite3 would have raised. Never report it as committed.
            raise psycopg.OperationalError("COMMIT rolled back a failed transaction")
        return self

    def fetchall(self) -> list[tuple]:
        return [] if self._raw.description is None else list(self._raw.fetchall())

    def fetchone(self) -> tuple | None:
        return None if self._raw.description is None else self._raw.fetchone()

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    def close(self) -> None:
        self._raw.close()


class PgConnection:
    """sqlite3.Connection-shaped wrapper over one autocommit psycopg connection."""

    def __init__(self, raw: psycopg.Connection) -> None:
        self._raw = raw
        self._changes = 0

    def cursor(self) -> PgCursor:
        return PgCursor(self, self._raw.cursor())

    def execute(self, sql: str, parameters=()) -> PgCursor:
        return self.cursor().execute(sql, parameters)

    @property
    def in_transaction(self) -> bool:
        return self._raw.info.transaction_status in (
            pq.TransactionStatus.ACTIVE, pq.TransactionStatus.INTRANS,
            pq.TransactionStatus.INERROR)

    @property
    def total_changes(self) -> int:
        """Rows written through this connection, like sqlite3's counter (the
        read tools' executor compares it before and after every read)."""
        if self._raw.closed:
            raise psycopg.InterfaceError("the connection is closed")
        return self._changes

    def close(self) -> None:
        self._raw.close()
