"""The product demo store: one mutable after-sales database and its trusted configuration.

Composition-root code. Nothing here comes from an evaluation case, label,
oracle or dataset:

    database     a FILE-BACKED Stage 6 database built by the domain's
                 create_stage6_database: aftersales/schema.sql -> demo seed ->
                 action schema -> Stage 6 seed. Mutable, because Stage 6
                 actions execute against it. The service keeps it in the
                 current generation of its data directory (persistence.py),
                 seeded once when that generation is created; without a path
                 the store builds a fresh one in its own temporary directory.
    time         FixedClock(DEMO_VIRTUAL_NOW): the demo business clock. Fixed so
                 that "delivered N days ago" does not drift with the real date.
    identity     server-side DEMO_PERSONAS only (aftersales.demo). A persona is a
                 demo stand-in for a trusted identity, not authentication.
    writes       exactly one ActionGateway (Guard, risk policy s6-risk/1,
                 capability gate, trusted operator registry). The store itself
                 never writes business state; the read side is query_only.
    operator     DEMO_OPERATOR_REF = op-demo-1, a server-side constant from the
                 Stage 6 trusted operator registry. A demo boundary, not
                 authentication.

Reset is deterministic: a new generation is seeded from the same seed files;
the old one is deleted.

Backend: with AFTERSALES_DB_URL unset (the default) the database is the SQLite
file above. With AFTERSALES_DB_URL=postgresql://... it is a PostgreSQL schema
instead (aftersales.action_db.PgDatabase): aftersales_gen_<generation> for a
data-directory generation, a temporary aftersales_demo_<random> schema
(dropped on close) otherwise. Only the business and action tables move;
sessions, checkpoints and traces stay where they are.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Mapping

from aftersales.action_db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    PgDatabase,
    create_stage6_database,
    drop_stage6_database,
    is_postgres,
)
from aftersales.action_gateway import ActionGateway
from aftersales.action_policy import S6_RISK_POLICY
from aftersales.approval import STAGE6_TRUSTED_OPERATORS
from aftersales.capabilities import CapabilityGate, EffectiveCapabilities
from aftersales.clock import FixedClock
from aftersales.context import Persona, TrustedExecutionContext
from aftersales.demo import DEMO_PERSONAS, DEMO_VIRTUAL_NOW
from aftersales.executor import execute_tool
from aftersales.ids import DeterministicIdProvider
from aftersales.policy_catalog import PublishedPolicyCatalog
from aftersales.registry import ToolRegistry, build_runtime_registry
from orchestration.contracts import ToolResult

from .knowledge_base import KNOWLEDGE_TOOL_NAME, KnowledgeBase, knowledge_tool_result
from .pending_requests import PENDING_TOOL_NAME, pending_tool_result

DEMO_ID_NAMESPACE = "m0-demo"
DEMO_OPERATOR_REF = "op-demo-1"
DEMO_DATABASE_NAME = "aftersales-demo.db"
DATABASE_URL_ENV = "AFTERSALES_DB_URL"
GENERATION_SCHEMA_PREFIX = "aftersales_gen_"
TEMPORARY_SCHEMA_PREFIX = "aftersales_demo_"

if DEMO_OPERATOR_REF not in STAGE6_TRUSTED_OPERATORS:
    raise ImportError("the demo operator must be a registered Stage 6 trusted operator")

# Audit columns a product view may show. Never the idempotency key, persona or args.
AUDIT_VIEW_COLUMNS = ("event_seq", "event_name", "action_name", "pending_action_id",
                      "receipt_id", "phase", "decision", "code", "approver_ref", "at")

# Order columns the display views may show (GET /orders, the operator's order
# summary). Never customer_id. Amounts are the stored decimal strings:
#   total_amount  the order's amount (订单金额)
#   unit_price    one unit's price when ordered (下单单价)
#   subtotal      unit_price x quantity (商品小计), computed here, not stored
# Packages belong to an order (one order may ship in several), never to a line.
ORDER_VIEW_COLUMNS = ("order_id", "status", "paid_at", "total_amount")
ITEM_VIEW_COLUMNS = ("order_item_id", "sku", "product_name", "category", "quantity", "unit_price")
PACKAGE_VIEW_COLUMNS = ("tracking_no", "carrier", "status", "shipped_at", "delivered_at",
                        "last_event_at")
_CENT = Decimal("0.01")


def _item_view(row: tuple) -> dict[str, object]:
    item = dict(zip(ITEM_VIEW_COLUMNS, row))
    item["subtotal"] = str((Decimal(item["unit_price"]) * item["quantity"]).quantize(_CENT))
    return item


class ReadSide:
    """The five read tools over a read-only, query_only connection, for one persona.

    Identity and business time come from the trusted context; every call goes
    through the unchanged aftersales.executor.execute_tool.
    """

    def __init__(self, registry: ToolRegistry, context: TrustedExecutionContext,
                 read_tools: tuple[str, ...]) -> None:
        self._registry = registry
        self._context = context
        self.read_tools = tuple(read_tools)
        self._read_tools = frozenset(read_tools)

    def execute(self, tool_name: str, arguments: Mapping[str, str], *,
                observation_id: str) -> ToolResult:
        if tool_name not in self._read_tools:
            raise ValueError("the read tool is not in the effective capabilities")
        return execute_tool(self._registry, self._context, tool_name, arguments,
                            observation_id=observation_id)


class KnowledgeReadSide:
    """The m3 read side: the five business tools and product-owned reads.

    Product-owned and read-only. The knowledge tool is not a CapabilityGate
    tool and not in the frozen registry: it is executed here, over the
    knowledge base, at the store's business time. Pending requests come from a
    trusted session-bound callback. Every business call goes to
    the ReadSide unchanged.
    """

    def __init__(self, reader: ReadSide, knowledge: KnowledgeBase, business_time: datetime,
                 read_pending: Callable[[], list[dict]] | None = None) -> None:
        self._reader = reader
        self._knowledge = knowledge
        self._business_time = business_time
        self._read_pending = read_pending
        self.read_tools = reader.read_tools + (KNOWLEDGE_TOOL_NAME,) + (
            (PENDING_TOOL_NAME,) if read_pending is not None else ())

    def execute(self, tool_name: str, arguments: Mapping[str, str], *,
                observation_id: str) -> ToolResult:
        if tool_name == KNOWLEDGE_TOOL_NAME:
            return knowledge_tool_result(self._knowledge, arguments, observation_id=observation_id,
                                         as_of=self._business_time)
        if tool_name == PENDING_TOOL_NAME and self._read_pending is not None:
            return pending_tool_result(arguments, observation_id=observation_id,
                                       read_pending=self._read_pending,
                                       as_of=self._business_time.isoformat())
        return self._reader.execute(tool_name, arguments, observation_id=observation_id)


def configured_database_url() -> str | None:
    """AFTERSALES_DB_URL, None when unset or empty (= SQLite)."""
    return os.environ.get(DATABASE_URL_ENV, "").strip() or None


def generation_database(directory: Path, generation: str) -> Path | PgDatabase:
    """Where one generation's business database lives on the configured backend."""
    url = configured_database_url()
    if url is None:
        return Path(directory) / DEMO_DATABASE_NAME
    return PgDatabase(url, GENERATION_SCHEMA_PREFIX + generation)


def seed_demo_database(db_path: Path | PgDatabase) -> None:
    """Build a new demo database from the seed files. Refuses an existing file."""
    if is_postgres(db_path):
        create_stage6_database(db_path)  # refuses an existing schema
        return
    if Path(db_path).exists():
        raise FileExistsError("a demo database is seeded exactly once")
    create_stage6_database(db_path)


class DemoStore:
    """One mutable demo database plus the server-side configuration around it."""

    def __init__(self, db_path: Path | PgDatabase | None = None) -> None:
        self._directory = None
        self._temporary_schema = False
        url = configured_database_url() if db_path is None else None
        if db_path is None and url is not None:
            self.db_path = PgDatabase(url, TEMPORARY_SCHEMA_PREFIX + secrets.token_hex(8))
            seed_demo_database(self.db_path)
            self._temporary_schema = True
        elif db_path is None:
            self._directory = tempfile.TemporaryDirectory(prefix="aftersales-demo-",
                                                          ignore_cleanup_errors=True)
            try:
                self.db_path = Path(self._directory.name) / DEMO_DATABASE_NAME
                seed_demo_database(self.db_path)
            except BaseException:
                self._directory.cleanup()
                raise
        elif is_postgres(db_path):
            from aftersales.action_db_pg import database_exists
            self.db_path = db_path
            if not database_exists(db_path):
                raise FileNotFoundError(
                    "the demo database of this generation is missing on PostgreSQL; a data"
                    " directory seeded without AFTERSALES_DB_URL needs its own AFTERSALES_DATA_DIR")
        else:
            # An existing, already seeded database: opening never seeds again.
            self.db_path = Path(db_path)
            if not self.db_path.is_file():
                raise FileNotFoundError("the demo database of this generation is missing")
        self.business_time = DEMO_VIRTUAL_NOW
        self.clock = FixedClock(DEMO_VIRTUAL_NOW)
        self.capabilities: EffectiveCapabilities = CapabilityGate().narrow()
        self.operator_ref = DEMO_OPERATOR_REF
        self.personas = DEMO_PERSONAS
        self._registry = build_runtime_registry()
        self.gateway = ActionGateway(
            self.db_path,
            clock=self.clock,
            id_provider=DeterministicIdProvider(DEMO_ID_NAMESPACE),
            catalog=PublishedPolicyCatalog(),
            capabilities=self.capabilities,
            risk_policy=S6_RISK_POLICY,
            operators=STAGE6_TRUSTED_OPERATORS,
            # C-D1 (docs/v2/exchange-stock-deduction.md): the product takes an
            # executed exchange's units from stock; evaluations keep S6-D10.
            deduct_exchange_stock=True,
        )
        self._closed = False

    @property
    def business_time_iso(self) -> str:
        return self.business_time.isoformat()

    def _read_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("the demo store is closed")
        if is_postgres(self.db_path):
            # default_transaction_read_only plays the part of PRAGMA query_only.
            from aftersales.action_db_pg import connect
            connection = connect(self.db_path, lock_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS,
                                 read_only=True)
            try:
                if connection.execute("SHOW default_transaction_read_only").fetchone()[0] != "on":
                    raise RuntimeError("PostgreSQL refused a read-only session")
            except BaseException:
                connection.close()
                raise
            return connection
        connection = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise RuntimeError("SQLite refused to enable query_only")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def read_side(self, persona: Persona) -> Iterator[ReadSide]:
        """A read-only tool executor for one request, closed afterwards."""
        connection = self._read_connection()
        try:
            context = TrustedExecutionContext(persona=persona, clock=self.clock,
                                              connection=connection)
            yield ReadSide(self._registry, context, self.capabilities.read_tools)
        finally:
            connection.close()

    def audit(self, request_id: str) -> list[dict[str, object]]:
        """The audit trail of one conversation's request id, oldest first. Read-only."""
        connection = self._read_connection()
        try:
            rows = connection.execute(
                "SELECT " + ", ".join(AUDIT_VIEW_COLUMNS)
                + " FROM action_audit_events WHERE request_id = ? ORDER BY event_seq",
                (request_id,)).fetchall()
        finally:
            connection.close()
        return [dict(zip(AUDIT_VIEW_COLUMNS, row)) for row in rows]

    def customer_orders(self, customer_id: str) -> list[dict[str, object]]:
        """Every order of one customer, highest order number first, each with its lines
        and its packages. Read-only; nothing of another customer's."""
        connection = self._read_connection()
        try:
            orders = connection.execute(
                "SELECT " + ", ".join(ORDER_VIEW_COLUMNS)
                + " FROM orders WHERE customer_id = ? ORDER BY order_id DESC", (customer_id,)).fetchall()
            items = connection.execute(
                "SELECT " + ", ".join("i." + column for column in ("order_id",) + ITEM_VIEW_COLUMNS)
                + " FROM order_items AS i JOIN orders AS o ON o.order_id = i.order_id"
                " WHERE o.customer_id = ? ORDER BY i.order_item_id", (customer_id,)).fetchall()
            packages = connection.execute(
                "SELECT " + ", ".join("l." + column for column in ("order_id",) + PACKAGE_VIEW_COLUMNS)
                + " FROM logistics AS l JOIN orders AS o ON o.order_id = l.order_id"
                " WHERE o.customer_id = ? ORDER BY l.shipped_at, l.tracking_no", (customer_id,)).fetchall()
        finally:
            connection.close()
        views = {row[0]: {**dict(zip(ORDER_VIEW_COLUMNS, row)), "items": [], "packages": []}
                 for row in orders}
        for order_id, *row in items:
            views[order_id]["items"].append(_item_view(tuple(row)))
        for order_id, *row in packages:
            views[order_id]["packages"].append(dict(zip(PACKAGE_VIEW_COLUMNS, row)))
        return list(views.values())

    def order_line(self, customer_id: str, order_id: object,
                   order_item_id: object) -> dict[str, object]:
        """One line of one customer's order: {"order": ..., "item": ...}. Read-only.

        "order" is that order with its packages, None unless it is this
        customer's; "item" is the line, None unless it is in that order.
        """
        order = item = None
        connection = self._read_connection()
        try:
            if isinstance(order_id, str):
                row = connection.execute(
                    "SELECT " + ", ".join(ORDER_VIEW_COLUMNS)
                    + " FROM orders WHERE order_id = ? AND customer_id = ?",
                    (order_id, customer_id)).fetchone()
                if row is not None:
                    packages = connection.execute(
                        "SELECT " + ", ".join(PACKAGE_VIEW_COLUMNS)
                        + " FROM logistics WHERE order_id = ? ORDER BY shipped_at, tracking_no",
                        (order_id,)).fetchall()
                    order = {**dict(zip(ORDER_VIEW_COLUMNS, row)),
                             "packages": [dict(zip(PACKAGE_VIEW_COLUMNS, package)) for package in packages]}
            if order is not None and isinstance(order_item_id, str):
                row = connection.execute(
                    "SELECT " + ", ".join(ITEM_VIEW_COLUMNS)
                    + " FROM order_items WHERE order_item_id = ? AND order_id = ?",
                    (order_item_id, order_id)).fetchone()
                item = None if row is None else _item_view(row)
        finally:
            connection.close()
        return {"order": order, "item": item}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._directory is not None:
            self._directory.cleanup()
        if self._temporary_schema:
            drop_stage6_database(self.db_path)
