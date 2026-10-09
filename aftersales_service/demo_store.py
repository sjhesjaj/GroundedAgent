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
"""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping

from aftersales.action_db import create_stage6_database
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

DEMO_ID_NAMESPACE = "m0-demo"
DEMO_OPERATOR_REF = "op-demo-1"
DEMO_DATABASE_NAME = "aftersales-demo.db"

if DEMO_OPERATOR_REF not in STAGE6_TRUSTED_OPERATORS:
    raise ImportError("the demo operator must be a registered Stage 6 trusted operator")

# Audit columns a product view may show. Never the idempotency key, persona or args.
AUDIT_VIEW_COLUMNS = ("event_seq", "event_name", "action_name", "pending_action_id",
                      "receipt_id", "phase", "decision", "code", "approver_ref", "at")


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
    """The m3 read side: a ReadSide's five tools plus search_knowledge_base (M3).

    Product-owned and read-only. The knowledge tool is not a CapabilityGate
    tool and not in the frozen registry: it is executed here, over the
    knowledge base, at the store's business time. Every other call goes to
    the ReadSide unchanged.
    """

    def __init__(self, reader: ReadSide, knowledge: KnowledgeBase, business_time: datetime) -> None:
        self._reader = reader
        self._knowledge = knowledge
        self._business_time = business_time
        self.read_tools = reader.read_tools + (KNOWLEDGE_TOOL_NAME,)

    def execute(self, tool_name: str, arguments: Mapping[str, str], *,
                observation_id: str) -> ToolResult:
        if tool_name == KNOWLEDGE_TOOL_NAME:
            return knowledge_tool_result(self._knowledge, arguments, observation_id=observation_id,
                                         as_of=self._business_time)
        return self._reader.execute(tool_name, arguments, observation_id=observation_id)


def seed_demo_database(db_path: Path) -> None:
    """Build a new demo database from the seed files. Refuses an existing file."""
    if Path(db_path).exists():
        raise FileExistsError("a demo database is seeded exactly once")
    create_stage6_database(db_path)


class DemoStore:
    """One mutable demo database plus the server-side configuration around it."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._directory = None
        if db_path is None:
            self._directory = tempfile.TemporaryDirectory(prefix="aftersales-demo-",
                                                          ignore_cleanup_errors=True)
            try:
                self.db_path = Path(self._directory.name) / DEMO_DATABASE_NAME
                seed_demo_database(self.db_path)
            except BaseException:
                self._directory.cleanup()
                raise
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
        )
        self._closed = False

    @property
    def business_time_iso(self) -> str:
        return self.business_time.isoformat()

    def _read_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("the demo store is closed")
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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._directory is not None:
            self._directory.cleanup()
