"""The PostgreSQL backend of the after-sales business + action tables.

Runs only with AFTERSALES_TEST_PG_URL=postgresql://... (docker compose up -d);
otherwise every PostgreSQL test skips. Offline otherwise: no model, no HTTP.

- the schema port: same rows, same constraints, same sort order as SQLite
- the row locks that replace BEGIN IMMEDIATE (aftersales.action_db_pg): the
  PostgreSQL counterparts of the @sqlite_only lock tests, plus races SQLite
  could never have (two writers at once)
- the demo store and data-directory generations on PostgreSQL
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aftersales.action_db import (
    PgDatabase,
    connect_writer,
    create_stage6_database,
    drop_stage6_database,
    is_postgres,
)
from aftersales.action_outcome import ActionStatus
from aftersales.demo import resolve_persona
from tests.stage6_support import (
    ALL_TABLES,
    EXCHANGE_ARGS,
    HANDOFF_ARGS,
    RETURN_ARGS,
    STOCK_TSHIRT_L,
    TEST_PG_URL,
    CountingCatalog,
    CountingClock,
    Hooks,
    SqliteStage6Database,
    Stage6Database,
    approval,
    identity,
    pending_row,
    require_postgres,
    validate,
)
from tests.test_v2_clock import clock_violations, sql_clock_violations

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_FILES = (REPO_ROOT / "aftersales" / "pg" / "schema.sql",
            REPO_ROOT / "aftersales" / "pg" / "action_schema.sql")
SQLITE_FILES = (REPO_ROOT / "aftersales" / "schema.sql",
                REPO_ROOT / "aftersales" / "action_schema.sql")
WAIT_SECONDS = 10


class StaticTests(unittest.TestCase):
    """No server needed."""

    def test_the_postgres_module_and_schema_read_no_clock(self):
        source = (REPO_ROOT / "aftersales" / "action_db_pg.py").read_text(encoding="utf-8")
        self.assertEqual(clock_violations(source), [])
        for path in PG_FILES:
            with self.subTest(path=path.name):
                self.assertEqual(sql_clock_violations(path.read_text(encoding="utf-8")), [])

    def test_the_port_keeps_every_constraint_of_the_sqlite_schema(self):
        for sqlite_path, pg_path in zip(SQLITE_FILES, PG_FILES):
            sqlite_sql = sqlite_path.read_text(encoding="utf-8")
            pg_sql = pg_path.read_text(encoding="utf-8")
            with self.subTest(file=sqlite_path.name):
                for keyword in ("CHECK (", "REFERENCES", "UNIQUE", "PRIMARY KEY", "CREATE TABLE",
                                "NOT NULL", "CREATE UNIQUE INDEX"):
                    self.assertEqual(_code(pg_sql).count(keyword), _code(sqlite_sql).count(keyword),
                                     keyword)
                self.assertNotIn("GLOB", _code(pg_sql))
                self.assertNotIn(" IS '", _code(pg_sql))

    def test_a_pg_database_never_shows_its_url(self):
        target = PgDatabase("postgresql://user:secret@127.0.0.1:1/db", "s6_x")
        self.assertNotIn("secret", repr(target))
        for bad in ("sqlite:///x.db", "aftersales_x"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                PgDatabase(bad, "s6_x")
        for schema in ("S6", "s6-x", "s6; DROP TABLE orders", "", "x" * 64):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                PgDatabase("postgresql://u@h/d", schema)


def _code(sql: str) -> str:
    """SQL without its -- comments."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


class PostgresCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_postgres()

    def setUp(self) -> None:
        self.db = Stage6Database()
        self.assertTrue(is_postgres(self.db.path))
        self.addCleanup(self.db.close)

    def probe(self, sql: str, params: tuple = ()) -> str:
        """Run one statement from another connection that does not wait: locked or granted."""
        import psycopg
        connection = connect_writer(self.db.path, busy_timeout_ms=0)
        try:
            connection.execute("BEGIN")
            connection.execute(sql, params)
            return "granted"
        except psycopg.errors.LockNotAvailable:
            return "locked"
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def waiting_for_a_lock(self) -> bool:
        """Poll until some session of this database waits on a lock."""
        monitor = connect_writer(self.db.path)
        try:
            deadline = time.monotonic() + WAIT_SECONDS
            while time.monotonic() < deadline:
                waiting = monitor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND datname = current_database()").fetchone()[0]
                if waiting:
                    return True
                time.sleep(0.05)
            return False
        finally:
            monitor.close()


class Paused:
    """A fault hook that holds a transaction open (locks held, writes done) until released."""

    def __init__(self, point: str) -> None:
        self.point = point
        self.reached = threading.Event()
        self.release = threading.Event()

    def before(self, point: str) -> None:
        if point == self.point:
            self.reached.set()
            if not self.release.wait(WAIT_SECONDS):
                raise AssertionError("never released")


def run_in_thread(function) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def target():
        try:
            box["result"] = function()
        except BaseException as error:  # surfaced by the test
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


class SchemaTests(PostgresCase):
    def test_a_fresh_database_holds_the_same_rows_as_sqlite(self):
        with SqliteStage6Database() as sqlite_db:
            expected = sqlite_db.dump()
        self.assertEqual(self.db.dump(), expected)
        self.assertGreater(sum(len(rows) for rows in expected.values()), 20)

    def test_tables_indexes_and_constraints_exist(self):
        schema = self.db.path.schema
        tables = {row[0] for row in self.db.rows(
            "SELECT tablename FROM pg_tables WHERE schemaname = ?", (schema,))}
        self.assertEqual(tables, set(ALL_TABLES))
        indexes = {row[0] for row in self.db.rows(
            "SELECT indexname FROM pg_indexes WHERE schemaname = ? AND indexname LIKE 's6%'",
            (schema,))}
        self.assertEqual(indexes, {"s6_one_active_case_per_item", "s6_one_open_pending_per_item",
                                   "s6_one_open_ticket_per_item_trigger"})
        checks = self.db.rows(
            "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace"
            " WHERE n.nspname = ? AND c.contype = 'c'", (schema,))[0][0]
        self.assertEqual(checks, sum(_code(path.read_text(encoding="utf-8")).count("CHECK (")
                                     for path in SQLITE_FILES))

    def test_an_approved_row_without_a_decision_is_refused(self):
        # SQLite: `approval_decision IS 'APPROVE'`. A plain `=` would let NULL pass.
        import psycopg
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.db.execute(
                "INSERT INTO pending_actions (pending_action_id, idempotency_key, request_id,"
                " persona_id, action_name, args_json, args_sha256, target_order_id,"
                " target_order_item_id, status, guard_decision, guard_reason_code, snapshot_json,"
                " snapshot_sha256, action_spec_version, risk_policy_version, policy_build_id,"
                " created_at, updated_at, version) VALUES ('PA-1', 'k', 'r', 'p', 'create_return',"
                " '{}', 'h', 'ORD-1001', 'OI-1001-2', 'APPROVED', 'REQUIRE_APPROVAL',"
                " 'risk_policy_requires_approval', '{}', 'h', 'v', 'v', 'b', 't', 't', 1)")

    def test_text_sorts_byte_wise_like_sqlite(self):
        ids = ("AS-10", "AS-2", "AS-B", "AS-a", "AS_1", "AS-1-1", "as-0")
        sql = ("INSERT INTO after_sales_cases (case_id, order_id, order_item_id, customer_id, type,"
               " status, reason, created_at, updated_at, version) VALUES (?, 'ORD-1001', 'OI-1001-2',"
               " 'CUST-001', 'return', '已完成', 'r', 't', 't', 1)")
        query = "SELECT case_id FROM after_sales_cases ORDER BY case_id"
        with SqliteStage6Database() as sqlite_db:
            for case_id in ids:
                sqlite_db.execute(sql, (case_id,))
            expected = sqlite_db.rows(query)
        for case_id in ids:
            self.db.execute(sql, (case_id,))
        self.assertEqual(self.db.rows(query), expected)


class LockTests(PostgresCase):
    """Counterparts of the @sqlite_only BEGIN IMMEDIATE tests."""

    def gateway(self, **options):
        return self.db.gateway(**options)

    def approved(self) -> str:
        pending_id = self.gateway().start_action(identity(), validate("create_return", RETURN_ARGS)
                                                 ).pending_action_id
        self.assertIsNotNone(pending_id)
        recorded = self.gateway().record_decision(approval(pending_id))
        self.assertTrue(recorded.approval_recorded)
        return pending_id

    def hold(self, sql: str, params: tuple = ()):
        """Another connection that takes a lock and keeps it until the cleanup."""
        blocker = connect_writer(self.db.path)
        blocker.execute("BEGIN")
        blocker.execute(sql, params)

        def release():
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")
            blocker.close()
        self.addCleanup(release)
        return release

    def test_a_locked_order_fails_start_before_the_clock_and_writes_nothing(self):
        self.db.execute(STOCK_TSHIRT_L)
        for label, sql in (("order", "SELECT 1 FROM orders WHERE order_id = 'ORD-1001' FOR UPDATE"),
                           ("item", "SELECT 1 FROM order_items WHERE order_item_id = 'OI-1001-1'"
                                    " FOR SHARE")):
            with self.subTest(locked=label):
                before = self.db.dump()
                release = self.hold(sql)
                clock = CountingClock()
                outcome = self.gateway(clock=clock, busy_timeout_ms=0).start_action(
                    identity(), validate("create_exchange", EXCHANGE_ARGS))
                release()
                self.assertEqual((outcome.status, outcome.code),
                                 (ActionStatus.FAILED, "transaction_failed"))
                self.assertEqual(clock.calls, 0)
                self.assertEqual(self.db.dump(), before)

    def test_a_locked_pending_fails_t1_and_t2_before_the_clock(self):
        pending_id = self.approved()
        before = self.db.dump()
        release = self.hold("SELECT 1 FROM pending_actions WHERE pending_action_id = ? FOR SHARE",
                            (pending_id,))
        clock = CountingClock()
        executed = self.gateway(clock=clock, busy_timeout_ms=0).execute_approved(pending_id)
        decided = self.gateway(clock=clock, busy_timeout_ms=0).record_decision(approval(pending_id))
        release()
        for outcome in (executed, decided):
            self.assertEqual((outcome.status, outcome.code, outcome.pending_action_id),
                             (ActionStatus.FAILED, "transaction_failed", pending_id))
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self.db.dump(), before)
        self.assertEqual(pending_row(self.db, pending_id)["status"], "APPROVED")

    def test_the_guard_reads_while_its_rows_are_locked(self):
        self.db.execute(STOCK_TSHIRT_L)
        seen = {}
        probe = self.probe

        class ObservingCatalog(CountingCatalog):
            def snapshot(inner):
                seen.update({
                    "order item": probe("SELECT 1 FROM order_items WHERE order_item_id = 'OI-1001-1'"
                                        " FOR UPDATE"),
                    "order": probe("UPDATE orders SET version = version WHERE order_id = 'ORD-1001'"),
                    "package": probe("UPDATE logistics SET version = version WHERE tracking_no = 'SF1001'"),
                    "target inventory": probe("UPDATE inventory SET available_qty = 0"
                                              " WHERE sku = 'SKU-TSHIRT-L'"),
                    "variant": probe("UPDATE sku_variants SET version = version WHERE sku = 'SKU-TSHIRT-M'"),
                    "new package": probe(
                        "INSERT INTO logistics (tracking_no, order_id, carrier, status, shipped_at,"
                        " last_event_at, updated_at, version) VALUES ('SFNEW', 'ORD-1001', 'c',"
                        " '运输中', 't', 't', 't', 1)"),
                    "new case": probe(
                        "INSERT INTO after_sales_cases (case_id, order_id, order_item_id, customer_id,"
                        " type, status, reason, created_at, updated_at, version) VALUES ('AS-NEW',"
                        " 'ORD-1001', 'OI-1001-1', 'CUST-001', 'return', '已完成', 'r', 't', 't', 1)"),
                    "another order": probe("SELECT 1 FROM order_items WHERE order_item_id = 'OI-2001-1'"
                                           " FOR UPDATE"),
                    "another sku": probe("UPDATE inventory SET version = version WHERE sku = 'SKU-MUG'"),
                })
                return super().snapshot()

        outcome = self.gateway(catalog=ObservingCatalog()).start_action(
            identity(), validate("create_exchange", EXCHANGE_ARGS))
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(seen, {
            "order item": "locked", "order": "locked", "package": "locked",
            "target inventory": "locked", "variant": "locked", "new package": "locked",
            "new case": "locked", "another order": "granted", "another sku": "granted",
        })

    def test_toctou_second_writer_is_locked_between_capture_and_write(self):
        self.db.execute(STOCK_TSHIRT_L)
        results = []
        hooks = Hooks(business_write=lambda: results.append(self.probe(
            "UPDATE inventory SET available_qty = 0, version = 99 WHERE sku = 'SKU-TSHIRT-L'")))
        outcome = self.gateway(fault_hooks=hooks).start_action(
            identity(), validate("create_exchange", EXCHANGE_ARGS))
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(results, ["locked"])
        self.assertEqual(self.db.rows("SELECT available_qty, version FROM inventory"
                                      " WHERE sku = 'SKU-TSHIRT-L'"), [(5, 13)])

    def test_restart_across_processes(self):
        pending_id = self.approved()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            script, result = Path(tmp) / "resume.py", Path(tmp) / "result.json"
            script.write_text(textwrap.dedent(f"""
                import json, sys
                from pathlib import Path
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from aftersales.action_db import PgDatabase
                from tests.stage6_support import Stage6Database
                db = Stage6Database.__new__(Stage6Database)
                db.path = PgDatabase({TEST_PG_URL!r}, {self.db.path.schema!r})
                outcome = db.gateway().execute_approved({pending_id!r})
                Path({str(result)!r}).write_text(json.dumps(outcome.to_dict()), encoding="utf-8")
            """), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(script)], cwd=str(REPO_ROOT),
                                       capture_output=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
            outcome = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(outcome["status"], "EXECUTED")
        self.assertEqual(self.db.rows("SELECT status, receipt_id FROM pending_actions"),
                         [("EXECUTED", outcome["receipt"]["receipt_id"])])
        self.assertEqual(self.db.count("action_receipts"), 1)


class RaceTests(PostgresCase):
    """Two writers at once: what BEGIN IMMEDIATE serialised, the row locks serialise."""

    def test_the_same_request_twice_at_once_is_one_pending_and_one_replay(self):
        paused = Paused("commit")
        action = validate("create_return", RETURN_ARGS)
        first, first_box = run_in_thread(
            lambda: self.db.gateway(fault_hooks=paused).start_action(identity(), action))
        self.assertTrue(paused.reached.wait(WAIT_SECONDS))
        second, second_box = run_in_thread(lambda: self.db.gateway().start_action(identity(), action))
        self.assertTrue(self.waiting_for_a_lock())  # the second waits for the first's order lock
        paused.release.set()
        first.join(WAIT_SECONDS)
        second.join(WAIT_SECONDS)
        self.assertNotIn("error", first_box)
        self.assertNotIn("error", second_box)
        one, two = first_box["result"], second_box["result"]
        self.assertEqual((one.status, one.idempotent_replay), (ActionStatus.WAITING_APPROVAL, False))
        self.assertEqual((two.status, two.idempotent_replay), (ActionStatus.WAITING_APPROVAL, True))
        self.assertEqual(two.pending_action_id, one.pending_action_id)
        self.assertEqual(self.db.count("pending_actions"), 1)

    def test_approve_and_reject_at_once_the_first_decision_wins(self):
        pending_id = self.db.gateway().start_action(
            identity(), validate("create_return", RETURN_ARGS)).pending_action_id
        paused = Paused("commit")
        first, first_box = run_in_thread(
            lambda: self.db.gateway(fault_hooks=paused).record_decision(approval(pending_id)))
        self.assertTrue(paused.reached.wait(WAIT_SECONDS))
        second, second_box = run_in_thread(
            lambda: self.db.gateway().record_decision(approval(pending_id, "REJECT")))
        self.assertTrue(self.waiting_for_a_lock())  # the second waits for the pending row
        paused.release.set()
        first.join(WAIT_SECONDS)
        second.join(WAIT_SECONDS)
        self.assertNotIn("error", first_box)
        self.assertNotIn("error", second_box)
        approved, rejected = first_box["result"], second_box["result"]
        self.assertTrue(approved.approval_recorded)
        self.assertFalse(approved.decision_conflict)
        # Not FAILED: the second decision read the first one's committed row.
        self.assertTrue(rejected.decision_conflict)
        self.assertIs(rejected.status, ActionStatus.WAITING_APPROVAL)
        self.assertEqual(pending_row(self.db, pending_id)["status"], "APPROVED")
        self.assertEqual(self.db.rows(
            "SELECT event_name FROM action_audit_events WHERE pending_action_id = ?"
            " AND event_name LIKE 'approval.%' ORDER BY event_seq", (pending_id,)),
            [("approval.recorded",), ("approval.conflict",)])

    def test_another_order_is_not_blocked(self):
        paused = Paused("commit")
        first, first_box = run_in_thread(lambda: self.db.gateway(fault_hooks=paused).start_action(
            identity(), validate("create_return", RETURN_ARGS)))
        self.assertTrue(paused.reached.wait(WAIT_SECONDS))
        other = self.db.gateway(busy_timeout_ms=0).start_action(
            identity("req-b", "demo-b"),
            validate("escalate_to_human", {**HANDOFF_ARGS, "order_id": "ORD-2001",
                                           "order_item_id": "OI-2001-1"}))
        self.assertTrue(first.is_alive())  # the first transaction is still open
        paused.release.set()
        first.join(WAIT_SECONDS)
        self.assertIs(other.status, ActionStatus.EXECUTED)
        self.assertIs(first_box["result"].status, ActionStatus.WAITING_APPROVAL)


class ServiceTests(PostgresCase):
    """The demo store and the data-directory generations with AFTERSALES_DB_URL set."""

    def setUp(self) -> None:  # no Stage6Database: the service creates its own schemas
        patcher = mock.patch.dict("os.environ", {"AFTERSALES_DB_URL": TEST_PG_URL})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_demo_store_runs_on_a_temporary_schema(self):
        import psycopg
        from aftersales.action_db_pg import database_exists
        from aftersales_service.demo_store import DemoStore
        from orchestration.contracts import ToolStatus
        store = DemoStore()
        try:
            self.assertTrue(is_postgres(store.db_path))
            self.assertTrue(database_exists(store.db_path))
            persona = resolve_persona("demo-a")
            with store.read_side(persona) as reader:
                result = reader.execute("get_order", {"order_id": "ORD-1001"}, observation_id="t:1")
            self.assertIsNot(result.status, ToolStatus.ERROR)
            outcome = store.gateway.start_action(identity("conv-pg"), validate("create_return", RETURN_ARGS))
            self.assertIs(outcome.status, ActionStatus.WAITING_APPROVAL)
            self.assertIn("action.pending_created",
                          [event["event_name"] for event in store.audit("conv-pg")])
            connection = store._read_connection()
            try:
                with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
                    connection.execute("UPDATE inventory SET version = version")
            finally:
                connection.close()
        finally:
            store.close()
        self.assertFalse(database_exists(store.db_path))

    def test_a_generation_is_a_schema_created_and_dropped_with_it(self):
        from aftersales.action_db_pg import database_exists
        from aftersales_service.demo_store import generation_database
        from aftersales_service.persistence import DataDirectory, Generation
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            data = DataDirectory(Path(tmp))
            generation = data.create()
            target = generation_database(data.directory(generation), generation)
            self.assertEqual(target.schema, "aftersales_gen_" + generation)
            self.assertTrue(database_exists(target))
            self.assertFalse((data.directory(generation) / "aftersales-demo.db").exists())
            runtime = Generation(data, generation)
            try:
                self.assertEqual(runtime.store.db_path, target)
            finally:
                runtime.close()
            data.remove(generation)
            self.assertFalse(database_exists(target))
            self.assertFalse(data.directory(generation).exists())


if __name__ == "__main__":
    unittest.main()
