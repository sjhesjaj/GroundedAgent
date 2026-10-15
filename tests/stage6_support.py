"""Shared fixtures for the Stage 6.1 action-core tests. Offline.

Every Stage 6 database is a file in a fresh temporary directory outside the
repository (tests/ must never hold a *.db file), closed and deleted afterwards.

With AFTERSALES_TEST_PG_URL=postgresql://... set, every Stage6Database is a fresh
PostgreSQL schema instead (dropped afterwards), so the same tests run against
the PostgreSQL backend. If that server cannot be reached the tests skip. Tests
of a SQLite-only mechanism are marked @sqlite_only; PostgreSQL counterparts
live in tests/test_aftersales_postgres.py.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import sqlite3
import tempfile
import unittest
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from aftersales.action_db import (
    PgDatabase,
    connect_writer,
    create_stage6_database,
    drop_stage6_database,
    integrity_errors,
)
from aftersales.action_gateway import ActionGateway
from aftersales.action_policy import S6_RISK_POLICY
from aftersales.actions import ActionIntentValidator, ValidatedAction, build_action_registry
from aftersales.capabilities import CapabilityGate
from aftersales.clock import BUSINESS_TIMEZONE, FixedClock
from aftersales.context import TrustedExecutionContext
from aftersales.demo import resolve_persona
from aftersales.guard import Guard
from aftersales.ids import DeterministicIdProvider, RequestIdentity
from aftersales.policy import PolicyRecord, PolicyRuleType
from aftersales.policy_catalog import CatalogSnapshot, PublishedPolicyCatalog

TEST_PG_URL_ENV = "AFTERSALES_TEST_PG_URL"
TEST_PG_URL = os.environ.get(TEST_PG_URL_ENV, "").strip() or None
ON_POSTGRES = TEST_PG_URL is not None


def sqlite_only(reason: str):
    """Skip on the PostgreSQL run: the test exercises a SQLite-only mechanism."""
    return unittest.skipIf(ON_POSTGRES, "SQLite-only mechanism: " + reason)


@lru_cache(maxsize=1)
def postgres_unavailable() -> str | None:
    """Why the PostgreSQL tests cannot run (checked once per process), None if they can."""
    if TEST_PG_URL is None:
        return TEST_PG_URL_ENV + " is not set"
    try:
        import psycopg
    except ImportError:
        return "psycopg is not installed"
    try:
        psycopg.connect(TEST_PG_URL, connect_timeout=3).close()
    except psycopg.Error as error:
        return "PostgreSQL is not reachable: " + type(error).__name__
    return None


def require_postgres() -> None:
    reason = postgres_unavailable()
    if reason is not None:
        raise unittest.SkipTest(reason)


VIRTUAL_NOW = datetime(2026, 11, 15, 10, 0, tzinfo=BUSINESS_TIMEZONE)
LATE_NOW = datetime(2026, 12, 20, 10, 0, tzinfo=BUSINESS_TIMEZONE)

ALL_TABLES = (
    "orders", "order_items", "logistics", "inventory", "after_sales_cases",
    "sku_variants", "human_handoff_tickets", "pending_actions", "action_receipts",
    "action_audit_events",
)

EXCHANGE_ARGS = {"order_id": "ORD-1001", "order_item_id": "OI-1001-1",
                 "target_sku": "SKU-TSHIRT-L", "reason_code": "size_or_spec_mismatch"}
RETURN_ARGS = {"order_id": "ORD-1001", "order_item_id": "OI-1001-2",
               "reason_code": "no_longer_wanted"}
HANDOFF_ARGS = {"order_id": "ORD-1001", "order_item_id": "OI-1001-1",
                "handoff_trigger": "quality_dispute"}

STOCK_TSHIRT_L = ("UPDATE inventory SET available_qty = 5, version = 13,"
                  " updated_at = '2026-11-14T20:00:00+08:00' WHERE sku = 'SKU-TSHIRT-L'")


def identity(request_id: str = "req-1", persona_id: str = "demo-a") -> RequestIdentity:
    return RequestIdentity(persona_id=persona_id, request_id=request_id)


def validate(action_name: str, args: dict) -> ValidatedAction:
    caps = CapabilityGate().narrow()
    return ActionIntentValidator(build_action_registry(), caps.actions).validate(action_name, args)


@lru_cache(maxsize=1)
def frozen_snapshot() -> CatalogSnapshot:
    """The published build-0001 snapshot (read once per test process)."""
    return PublishedPolicyCatalog().snapshot()


def snapshot_with(records) -> CatalogSnapshot:
    return dataclasses.replace(frozen_snapshot(), records=tuple(records))


def snapshot_without(rule_type: PolicyRuleType) -> CatalogSnapshot:
    return snapshot_with(r for r in frozen_snapshot().records if r.rule_type is not rule_type)


def extra_policy(rule_type: PolicyRuleType, params: dict, *, priority: int,
                 policy_id: str = "test-extra", scope: tuple = ()) -> PolicyRecord:
    return PolicyRecord(
        policy_id=policy_id, version="1", title="测试规则", rule_type=rule_type, scope=scope,
        params=params, effective_from="2026-01-01T00:00:00+08:00", effective_to=None,
        source_doc="test-extra.md", locator="section:test", build_id=frozen_snapshot().build_id,
        priority=priority,
    )


class CountingClock(FixedClock):
    """A FixedClock that counts every read."""

    def __init__(self, instant: datetime = VIRTUAL_NOW) -> None:
        super().__init__(instant)
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return super().now()


class RaisingClock(FixedClock):
    """A Clock that must never be read."""

    def __init__(self) -> None:
        super().__init__(VIRTUAL_NOW)

    def now(self) -> datetime:
        raise AssertionError("this Clock must not be read")


class CountingCatalog:
    """snapshot() counter around a fixed CatalogSnapshot (or a failure)."""

    def __init__(self, snapshot: CatalogSnapshot | None = None, *, error: Exception | None = None) -> None:
        self._snapshot = frozen_snapshot() if snapshot is None else snapshot
        self._error = error
        self.calls = 0

    def snapshot(self) -> CatalogSnapshot:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


class Hooks:
    """Fault hooks: callables per point, run just before that point."""

    def __init__(self, **actions) -> None:
        self.actions = actions
        self.seen: list[str] = []

    def before(self, point: str) -> None:
        self.seen.append(point)
        action = self.actions.get(point)
        if action is not None:
            action()


def raiser(exception: Exception):
    def action():
        raise exception
    return action


class Stage6Database:
    """One fresh Stage 6 database: a file in its own temporary directory, or
    (AFTERSALES_TEST_PG_URL set) a fresh PostgreSQL schema."""

    follows_test_backend = True

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.directory = Path(self._dir.name)  # scratch space on either backend
        self.postgres = ON_POSTGRES and self.follows_test_backend
        if self.postgres:
            reason = postgres_unavailable()
            if reason is not None:
                self._dir.cleanup()
                raise unittest.SkipTest(reason)
            self.path = PgDatabase(TEST_PG_URL, "s6test_" + secrets.token_hex(8))
        else:
            self.path = self.directory / "stage6.db"
        try:
            create_stage6_database(self.path)
        except BaseException:
            self._dir.cleanup()
            raise

    @property
    def begin_sql(self) -> str:
        """What the Guard tests open their read transaction with (see action_gateway._begin)."""
        return "BEGIN" if self.postgres else "BEGIN IMMEDIATE"

    def connect(self):
        """An autocommit connection to this database (sqlite3, or the PostgreSQL wrapper)."""
        if self.postgres:
            return connect_writer(self.path)
        return sqlite3.connect(str(self.path), isolation_level=None)

    def close(self) -> None:
        if self.postgres:
            drop_stage6_database(self.path)
        self._dir.cleanup()

    def __enter__(self) -> "Stage6Database":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def execute(self, sql: str, params: tuple = (), *, ignore_checks: bool = False) -> None:
        if self.postgres:
            self._execute_postgres(sql, params, ignore_checks=ignore_checks)
            return
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if ignore_checks:
                connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(sql, params)
            connection.execute("COMMIT")
        finally:
            connection.close()

    def _execute_postgres(self, sql: str, params: tuple, *, ignore_checks: bool) -> None:
        import psycopg
        connection = connect_writer(self.path)
        try:
            connection.execute("BEGIN")
            try:
                connection.execute(sql, params)
            except psycopg.errors.CheckViolation:
                if ignore_checks:
                    # PostgreSQL cannot switch CHECK constraints off for one write,
                    # so a row only a CHECK-less SQLite write can plant is not plantable.
                    raise unittest.SkipTest("PostgreSQL cannot plant a CHECK-violating row") from None
                raise
            connection.execute("COMMIT")
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        connection = connect_writer(self.path) if self.postgres else sqlite3.connect(str(self.path))
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def count(self, table: str) -> int:
        return self.rows("SELECT COUNT(*) FROM " + table)[0][0]

    def dump(self) -> dict[str, list[tuple]]:
        return {table: self.rows("SELECT * FROM " + table + " ORDER BY 1") for table in ALL_TABLES}

    def gateway(self, **overrides) -> ActionGateway:
        options = dict(
            clock=FixedClock(VIRTUAL_NOW),
            id_provider=DeterministicIdProvider("eval"),
            catalog=CountingCatalog(),
            capabilities=CapabilityGate().narrow(),
            risk_policy=S6_RISK_POLICY,
            formal=True,
        )
        options.update(overrides)
        return ActionGateway(self.path, **options)

    def evaluate(self, action: ValidatedAction, *, persona_id: str = "demo-a",
                 now: datetime = VIRTUAL_NOW, snapshot: CatalogSnapshot | None = None,
                 exclude_pending_id: str | None = None):
        """Guard.capture + Guard.decide inside a BEGIN IMMEDIATE that is rolled back."""
        connection = connect_writer(self.path)
        try:
            connection.execute(self.begin_sql)
            context = TrustedExecutionContext(persona=resolve_persona(persona_id),
                                              clock=FixedClock(now), connection=connection)
            capture = Guard(S6_RISK_POLICY).capture(
                action, context, CountingCatalog(snapshot), txn_now=now,
                exclude_pending_id=exclude_pending_id)
            decision = Guard.decide(action, capture.state, capture.policy, S6_RISK_POLICY, now)
            return capture, decision
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()


class SqliteStage6Database(Stage6Database):
    """Always a SQLite file, whatever AFTERSALES_TEST_PG_URL says: for the eval_v2
    tests, whose runtime is not migrated and opens the database by file path."""

    follows_test_backend = False


def insert_case(db: Stage6Database, case_id: str, order_item_id: str, *, case_type: str,
                status: str, order_id: str = "ORD-1001", customer_id: str = "CUST-001") -> None:
    db.execute(
        "INSERT INTO after_sales_cases (case_id, order_id, order_item_id, customer_id, type,"
        " status, reason, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (case_id, order_id, order_item_id, customer_id, case_type, status, "测试",
         "2026-11-10T10:00:00+08:00", "2026-11-10T10:00:00+08:00"))


def insert_pending(db: Stage6Database, order_item_id: str, *, order_id: str = "ORD-1001",
                   status: str = "PENDING_APPROVAL") -> None:
    key = "s6k1-" + "0" * 64
    db.execute(
        "INSERT INTO pending_actions (pending_action_id, idempotency_key, request_id, persona_id,"
        " action_name, args_json, args_sha256, target_order_id, target_order_item_id, status,"
        " guard_decision, guard_reason_code, snapshot_json, snapshot_sha256, action_spec_version,"
        " risk_policy_version, policy_build_id, created_at, updated_at, version)"
        " VALUES (?, ?, 'req-x', 'demo-a', 'create_return', '{}', ?, ?, ?, ?, 'REQUIRE_APPROVAL',"
        " 'risk_policy_requires_approval', '{}', ?, 's6-actions/1', 's6-risk/1', 'build-0001',"
        " '2026-11-14T10:00:00+08:00', '2026-11-14T10:00:00+08:00', 1)",
        ("PA-0000000000000001", key, "0" * 64, order_id, order_item_id, status, "0" * 64))


def insert_ticket(db: Stage6Database, ticket_id: str, order_item_id: str, *, status: str,
                  order_id: str = "ORD-1001") -> None:
    db.execute(
        "INSERT INTO human_handoff_tickets (ticket_id, order_id, order_item_id, handoff_trigger,"
        " status, created_at, updated_at, version) VALUES (?, ?, ?, 'quality_dispute', ?, ?, ?, 1)",
        (ticket_id, order_id, order_item_id, status, "2026-11-10T10:00:00+08:00",
         "2026-11-10T10:00:00+08:00"))


# --------------------------------------------------------------------------
# Stage 6.2: approval / resume
# --------------------------------------------------------------------------

OPERATOR = "op-demo-1"


def approval(pending_action_id: str, decision: str = "APPROVE", *, at: datetime = VIRTUAL_NOW,
             approver_ref: str = OPERATOR):
    from aftersales.approval import ApprovalDecision
    return ApprovalDecision(pending_action_id=pending_action_id, decision=decision,
                            approver_ref=approver_ref, decided_at=at.isoformat())


def pending_row(db: Stage6Database, pending_action_id: str) -> dict:
    from aftersales.action_store import PENDING_COLUMNS
    rows = db.rows("SELECT " + ", ".join(PENDING_COLUMNS)
                   + " FROM pending_actions WHERE pending_action_id = ?", (pending_action_id,))
    assert len(rows) == 1
    return dict(zip(PENDING_COLUMNS, rows[0]))


def audit_trail(db: Stage6Database, pending_action_id: str | None = None) -> list[tuple]:
    if pending_action_id is None:
        return db.rows("SELECT event_name, phase, decision, code FROM action_audit_events"
                       " ORDER BY event_seq")
    return db.rows("SELECT event_name, phase, decision, code FROM action_audit_events"
                   " WHERE pending_action_id = ? ORDER BY event_seq", (pending_action_id,))
