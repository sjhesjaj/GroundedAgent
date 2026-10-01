"""Stage 6.1 ActionGateway: writes, idempotency, transactions, failures (design §9, §10.2, §16-§18)."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from aftersales.action_db import connect_writer, create_stage6_database
from aftersales.action_errors import ActionCapabilityError, ActionContractError
from aftersales.action_gateway import ActionGateway, ApprovalPathNotEnabled
from aftersales.action_outcome import (
    COMPLETION_CLAIM_MARKERS,
    DENIED_EXPLANATIONS,
    ActionOutcome,
    ActionOutcomeRenderer,
    ActionStatus,
    GuardView,
    ReceiptView,
)
from aftersales.action_store import AUDIT_CODE_VALUES, AUDIT_DECISION_VALUES, AUDIT_EVENT_NAMES
from aftersales.actions import ValidatedAction, canonical_args, args_digest
from aftersales.capabilities import CapabilityGate
from aftersales.executor import SQL_MARKERS
from aftersales.guard import DENY_REASON_CODES
from aftersales.ids import ID_PATTERN, UuidIdProvider, idempotency_key
from aftersales.policy import PolicyRuleType

from tests.stage6_support import (
    EXCHANGE_ARGS,
    HANDOFF_ARGS,
    RETURN_ARGS,
    STOCK_TSHIRT_L,
    CountingCatalog,
    CountingClock,
    Hooks,
    Stage6Database,
    identity,
    insert_ticket,
    raiser,
    snapshot_without,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BUSINESS_TABLES = ("orders", "order_items", "logistics", "inventory", "after_sales_cases",
                   "sku_variants", "human_handoff_tickets")


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self.db = Stage6Database()
        self.addCleanup(self.db.close)
        self.db.execute(STOCK_TSHIRT_L)

    def start(self, name, args, *, request_id="req-1", persona_id="demo-a", **gateway):
        return self.db.gateway(**gateway).start_action(
            identity(request_id, persona_id), validate(name, args))

    def new_cases(self):
        return self.db.rows("SELECT case_id, order_id, order_item_id, customer_id, type, status,"
                            " reason, created_at, updated_at, version FROM after_sales_cases"
                            " WHERE case_id LIKE 'AS6-%'")


class WriteAndIdempotencyTests(GatewayCase):
    def test_a_eligible_exchange_writes_one_case_and_one_receipt(self):
        inventory_before = self.db.rows("SELECT * FROM inventory ORDER BY sku")
        orders_before = self.db.rows("SELECT * FROM orders ORDER BY order_id")
        logistics_before = self.db.rows("SELECT * FROM logistics ORDER BY tracking_no")
        outcome = self.start("create_exchange", EXCHANGE_ARGS)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertFalse(outcome.idempotent_replay)
        self.assertEqual((outcome.guard.decision, outcome.guard.reason_code), ("ALLOW", "risk_policy_allows"))
        cases = self.new_cases()
        self.assertEqual(len(cases), 1)
        case_id = cases[0][0]
        self.assertEqual(cases[0][1:], ("ORD-1001", "OI-1001-1", "CUST-001", "exchange", "待处理",
                                        "尺码或规格不合适", "2026-11-15T10:00:00+08:00",
                                        "2026-11-15T10:00:00+08:00", 1))
        self.assertEqual(outcome.receipt.resource_id, case_id)
        self.assertEqual(outcome.receipt.resource_type, "after_sales_case")
        receipts = self.db.rows("SELECT receipt_id, request_id, persona_id, action_name, args_json,"
                                " result_status, resource_type, resource_id, pending_action_id,"
                                " guard_decision, guard_reason_code, action_spec_version,"
                                " risk_policy_version, policy_build_id, executed_at FROM action_receipts")
        self.assertEqual(receipts, [(outcome.receipt.receipt_id, "req-1", "demo-a", "create_exchange",
                                     canonical_args(EXCHANGE_ARGS), "EXECUTED", "after_sales_case",
                                     case_id, None, "ALLOW", "risk_policy_allows", "s6-actions/1",
                                     "s6-risk/1", "build-0001", "2026-11-15T10:00:00+08:00")])
        # No inventory reservation, no order or logistics mutation.
        self.assertEqual(self.db.rows("SELECT * FROM inventory ORDER BY sku"), inventory_before)
        self.assertEqual(self.db.rows("SELECT * FROM orders ORDER BY order_id"), orders_before)
        self.assertEqual(self.db.rows("SELECT * FROM logistics ORDER BY tracking_no"), logistics_before)
        self.assertEqual(self.db.count("pending_actions"), 0)

    def test_receipt_snapshot_is_the_closed_s6_guard_snapshot(self):
        outcome = self.start("create_exchange", EXCHANGE_ARGS)
        document, digest = self.db.rows("SELECT snapshot_json, snapshot_sha256 FROM action_receipts")[0]
        import hashlib
        self.assertEqual(digest, hashlib.sha256(document.encode("utf-8")).hexdigest())
        snapshot = json.loads(document)
        self.assertEqual(set(snapshot), {"schema", "action_name", "evaluated_at", "records",
                                         "policy_build_id", "action_spec_version",
                                         "risk_policy_version", "decision"})
        self.assertEqual(snapshot["records"], {
            "after_sales_cases": {"AS-1001": 3}, "inventory": {"SKU-TSHIRT-L": 13},
            "logistics": {"SF1001": 5}, "order_items": {"OI-1001-1": 1},
            "orders": {"ORD-1001": 4}, "sku_variants": {"SKU-TSHIRT-L": 1, "SKU-TSHIRT-M": 1}})
        self.assertNotIn("CUST-001", document)
        self.assertNotIn("服装", document)  # category never enters persisted facts
        self.assertIs(outcome.status, ActionStatus.EXECUTED)

    def test_b_exact_replay_returns_the_same_receipt(self):
        first = self.start("create_exchange", EXCHANGE_ARGS)
        catalog = CountingCatalog()
        second = self.start("create_exchange", EXCHANGE_ARGS, catalog=catalog)
        self.assertIs(second.status, ActionStatus.EXECUTED)
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(second.receipt, first.receipt)
        self.assertEqual(catalog.calls, 0)  # the Guard is not run again for a replay
        self.assertEqual(len(self.new_cases()), 1)
        self.assertEqual(self.db.count("action_receipts"), 1)
        replays = self.db.rows("SELECT code, receipt_id FROM action_audit_events"
                               " WHERE event_name = 'action.replay_hit'")
        self.assertEqual(replays, [("receipt", first.receipt.receipt_id)])

    def test_c_new_request_for_the_same_item_is_denied(self):
        self.start("create_exchange", EXCHANGE_ARGS)
        outcome = self.start("create_exchange", EXCHANGE_ARGS, request_id="req-2")
        self.assertIs(outcome.status, ActionStatus.DENIED)
        self.assertEqual(outcome.code, "active_after_sales_case_exists")
        self.assertFalse(outcome.idempotent_replay)
        self.assertEqual(len(self.new_cases()), 1)
        self.assertEqual(self.db.count("action_receipts"), 1)

    def test_d_eligible_handoff_writes_one_ticket_and_one_receipt(self):
        outcome = self.start("escalate_to_human", HANDOFF_ARGS)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        tickets = self.db.rows("SELECT * FROM human_handoff_tickets")
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0][1:], ("ORD-1001", "OI-1001-1", "quality_dispute", "待处理",
                                          "2026-11-15T10:00:00+08:00", "2026-11-15T10:00:00+08:00", 1))
        self.assertEqual(outcome.receipt.resource_id, tickets[0][0])
        self.assertEqual(outcome.receipt.resource_type, "human_handoff_ticket")
        self.assertRegex(tickets[0][0], ID_PATTERN)
        self.assertEqual(self.db.count("action_receipts"), 1)
        self.assertEqual(len(self.new_cases()), 0)

    def test_e_duplicate_open_handoff_is_denied(self):
        self.start("escalate_to_human", HANDOFF_ARGS)
        outcome = self.start("escalate_to_human", HANDOFF_ARGS, request_id="req-2")
        self.assertIs(outcome.status, ActionStatus.DENIED)
        self.assertEqual(outcome.code, "handoff_ticket_exists")
        self.assertEqual(self.db.count("human_handoff_tickets"), 1)
        self.assertEqual(self.db.count("action_receipts"), 1)

    def test_f_eligible_return_is_stage61_approval_path_not_enabled_with_zero_writes(self):
        before = self.db.dump()
        clock, catalog = CountingClock(), CountingCatalog()
        with self.assertRaises(ApprovalPathNotEnabled):
            self.start("create_return", RETURN_ARGS, clock=clock, catalog=catalog)
        self.assertEqual(self.db.dump(), before)  # including zero audit rows
        self.assertEqual(self.db.count("action_audit_events"), 0)
        self.assertEqual(self.db.count("pending_actions"), 0)
        self.assertEqual((clock.calls, catalog.calls), (1, 1))

    def test_denied_writes_only_audit(self):
        before = {table: self.db.rows("SELECT * FROM " + table + " ORDER BY 1") for table in BUSINESS_TABLES}
        outcome = self.start("create_exchange", {**EXCHANGE_ARGS, "target_sku": "SKU-MUG"})
        self.assertIs(outcome.status, ActionStatus.DENIED)
        self.assertEqual(outcome.code, "exchange_target_incompatible")
        self.assertIsNone(outcome.receipt)
        after = {table: self.db.rows("SELECT * FROM " + table + " ORDER BY 1") for table in BUSINESS_TABLES}
        self.assertEqual(after, before)
        self.assertEqual(self.db.count("action_receipts"), 0)
        self.assertEqual(self.db.rows("SELECT event_name, phase, decision, code FROM action_audit_events"
                                      " ORDER BY event_seq"), [
            ("guard.evaluated", "start", "DENY", "exchange_target_incompatible"),
            ("action.not_executed", None, "DENIED", "exchange_target_incompatible")])

    def test_a_denied_request_is_not_an_anchor(self):
        first = self.start("create_exchange", {**EXCHANGE_ARGS, "target_sku": "SKU-MUG"})
        again = self.start("create_exchange", {**EXCHANGE_ARGS, "target_sku": "SKU-MUG"})
        self.assertEqual((first.status, again.status), (ActionStatus.DENIED, ActionStatus.DENIED))
        self.assertFalse(again.idempotent_replay)  # re-evaluated, since nothing was written


class TransactionTests(GatewayCase):
    def test_p12_clock_is_read_exactly_once_per_attempt(self):
        paths = {
            "allow": ("create_exchange", EXCHANGE_ARGS, {}),
            "deny": ("create_exchange", {**EXCHANGE_ARGS, "target_sku": "SKU-MUG"}, {}),
            "replay": ("create_exchange", EXCHANGE_ARGS, {}),
            "policy_unavailable": ("escalate_to_human", HANDOFF_ARGS,
                                   {"catalog": CountingCatalog(error=RuntimeError("down"))}),
            "write failure": ("escalate_to_human", {**HANDOFF_ARGS, "order_item_id": "OI-1001-2"},
                              {"fault_hooks": Hooks(business_write=raiser(sqlite3.OperationalError("x")))}),
            "commit failure": ("escalate_to_human", {**HANDOFF_ARGS, "order_item_id": "OI-1001-2"},
                               {"fault_hooks": Hooks(commit=raiser(sqlite3.OperationalError("x")))}),
        }
        for label, (name, args, options) in paths.items():
            with self.subTest(path=label):
                clock = CountingClock()
                self.start(name, args, clock=clock, **options)
                self.assertEqual(clock.calls, 1)

    def test_p12_begin_failure_reads_no_clock_and_writes_nothing(self):
        before = self.db.dump()
        blocker = connect_writer(self.db.path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            clock = CountingClock()
            outcome = self.start("create_exchange", EXCHANGE_ARGS, clock=clock, busy_timeout_ms=0)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertIs(outcome.status, ActionStatus.FAILED)
        self.assertEqual(outcome.code, "transaction_failed")
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self.db.dump(), before)

    def test_p13_one_policy_snapshot_per_evaluated_attempt(self):
        for label, name, args in (("allow", "create_exchange", EXCHANGE_ARGS),
                                  ("deny", "create_exchange", {**EXCHANGE_ARGS, "target_sku": "SKU-MUG"}),
                                  ("handoff", "escalate_to_human", HANDOFF_ARGS)):
            with self.subTest(path=label):
                catalog = CountingCatalog()
                self.start(name, args, catalog=catalog, request_id="req-" + label)
                self.assertEqual(catalog.calls, 1)
        catalog = CountingCatalog()
        with self.assertRaises(ApprovalPathNotEnabled):
            self.start("create_return", RETURN_ARGS, catalog=catalog)
        self.assertEqual(catalog.calls, 1)
        failing = CountingCatalog(error=RuntimeError("down"))
        outcome = self.start("create_exchange", EXCHANGE_ARGS, catalog=failing, request_id="req-down")
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "policy_unavailable"))
        self.assertEqual(failing.calls, 1)

    def test_guard_runs_after_begin_immediate(self):
        seen = []

        class ObservingCatalog(CountingCatalog):
            def snapshot(inner):
                probe = sqlite3.connect(str(self.db.path), timeout=0)
                try:
                    probe.execute("BEGIN IMMEDIATE")
                    seen.append("unlocked")
                    probe.execute("ROLLBACK")
                except sqlite3.OperationalError as error:
                    seen.append(str(error))
                finally:
                    probe.close()
                return super().snapshot()

        self.start("create_exchange", EXCHANGE_ARGS, catalog=ObservingCatalog())
        self.assertEqual(seen, ["database is locked"])

    def test_toctou_second_writer_is_locked_between_capture_and_write(self):
        results = []

        def second_writer():
            other = sqlite3.connect(str(self.db.path), isolation_level=None, timeout=0)
            try:
                other.execute("PRAGMA busy_timeout = 0")
                other.execute("BEGIN IMMEDIATE")
                other.execute("UPDATE inventory SET available_qty = 0, version = 99"
                              " WHERE sku = 'SKU-TSHIRT-L'")
                other.execute("COMMIT")
                results.append("wrote")
            except sqlite3.OperationalError as error:
                results.append(str(error))
            finally:
                other.close()

        hooks = Hooks(business_write=second_writer)
        outcome = self.start("create_exchange", EXCHANGE_ARGS, fault_hooks=hooks)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(results, ["database is locked"])
        # The state the Guard read is the state the write committed against.
        self.assertEqual(self.db.rows("SELECT available_qty, version FROM inventory"
                                      " WHERE sku = 'SKU-TSHIRT-L'"), [(5, 13)])
        # After the commit, the second writer can write.
        second_writer()
        self.assertEqual(results[-1], "wrote")


class FailureTests(GatewayCase):
    def assert_failed_cleanly(self, outcome, code, before):
        self.assertIs(outcome.status, ActionStatus.FAILED)
        self.assertEqual(outcome.code, code)
        self.assertIsNone(outcome.receipt)
        after = self.db.dump()
        for table in BUSINESS_TABLES + ("action_receipts", "pending_actions"):
            self.assertEqual(after[table], before[table], table)
        audit = self.db.rows("SELECT event_name, request_id, persona_id, action_name, idempotency_key,"
                             " pending_action_id, receipt_id, phase, decision, code, approver_ref, at"
                             " FROM action_audit_events ORDER BY event_seq")
        self.assertEqual([(row[0], row[8], row[9]) for row in audit][-1],
                         ("action.not_executed", "FAILED", code))
        self.assert_safe_audit(audit)

    def assert_safe_audit(self, audit):
        for row in audit:
            event, request_id, persona_id, action, key, pending, receipt, phase, decision, code, approver, at = row
            self.assertIn(event, AUDIT_EVENT_NAMES)
            self.assertIn(decision, AUDIT_DECISION_VALUES | {None})
            self.assertIn(code, AUDIT_CODE_VALUES | {None})
            self.assertIsNone(pending)
            self.assertIsNone(approver)
            self.assertTrue(receipt is None or ID_PATTERN.match(receipt))
            rendered = " ".join(str(value) for value in row).lower()
            for secret in ("cust-001", "cust-002", "operationalerror", "traceback", "boom"):
                self.assertNotIn(secret, rendered)
            for marker in SQL_MARKERS:
                self.assertNotIn(marker, rendered)

    def test_business_write_failure_rolls_back_everything(self):
        before = self.db.dump()
        outcome = self.start("create_exchange", EXCHANGE_ARGS,
                             fault_hooks=Hooks(business_write=raiser(sqlite3.OperationalError("boom"))))
        self.assert_failed_cleanly(outcome, "write_failed", before)

    def test_receipt_write_failure_rolls_back_the_business_row(self):
        before = self.db.dump()
        hooks = Hooks(receipt_write=raiser(sqlite3.OperationalError("boom")))
        outcome = self.start("create_exchange", EXCHANGE_ARGS, fault_hooks=hooks)
        self.assertEqual(hooks.seen, ["business_write", "receipt_write"])
        self.assert_failed_cleanly(outcome, "write_failed", before)
        self.assertEqual(len(self.new_cases()), 0)

    def test_commit_failure_never_reports_executed(self):
        before = self.db.dump()
        hooks = Hooks(commit=raiser(sqlite3.OperationalError("boom")))
        outcome = self.start("escalate_to_human", HANDOFF_ARGS, fault_hooks=hooks)
        self.assert_failed_cleanly(outcome, "transaction_failed", before)
        self.assertEqual(self.db.count("human_handoff_tickets"), 0)
        # The same request can be retried: a failure is not an idempotency anchor.
        retry = self.start("escalate_to_human", HANDOFF_ARGS)
        self.assertIs(retry.status, ActionStatus.EXECUTED)
        self.assertFalse(retry.idempotent_replay)

    def test_guard_failures_are_failed_never_allow_or_deny(self):
        cases = (
            ("policy_unavailable", {"catalog": CountingCatalog(error=RuntimeError("boom"))}, None),
            ("state_version_missing", {}, "UPDATE orders SET version = 0 WHERE order_id = 'ORD-1001'"),
            ("state_malformed", {}, "UPDATE logistics SET delivered_at = 'boom' WHERE tracking_no = 'SF1001'"),
        )
        for code, options, sql in cases:
            with self.subTest(code=code):
                with Stage6Database() as db:
                    db.execute(STOCK_TSHIRT_L)
                    if sql:
                        db.execute(sql, ignore_checks=True)
                    before = db.dump()
                    outcome = db.gateway(**options).start_action(identity(), validate("create_exchange", EXCHANGE_ARGS))
                    self.assertIs(outcome.status, ActionStatus.FAILED)
                    self.assertEqual(outcome.code, code)
                    self.assertIsNone(outcome.guard)
                    after = db.dump()
                    for table in BUSINESS_TABLES + ("action_receipts",):
                        self.assertEqual(after[table], before[table])
                    self.assertEqual(db.rows("SELECT event_name, code FROM action_audit_events ORDER BY event_seq"),
                                     [("guard.failed", code), ("action.not_executed", code)])

    def test_failed_reads_are_not_empty(self):
        self.db.execute("DROP TABLE sku_variants")
        outcome = self.start("create_exchange", EXCHANGE_ARGS)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "state_read_failed"))


class BoundaryTests(GatewayCase):
    def test_p7_capability_violation_happens_before_any_transaction(self):
        before = self.db.dump()
        clock, catalog = CountingClock(), CountingCatalog()
        gateway = self.db.gateway(clock=clock, catalog=catalog,
                                  capabilities=CapabilityGate().narrow(actions=("escalate_to_human",)))
        with self.assertRaises(ActionCapabilityError):
            gateway.start_action(identity(), validate("create_exchange", EXCHANGE_ARGS))
        self.assertEqual((clock.calls, catalog.calls), (0, 0))
        self.assertEqual(self.db.dump(), before)

    def test_p6_smuggled_identity_is_rejected_before_the_database(self):
        missing = Path(tempfile.gettempdir()) / "stage6-never-created.db"
        self.assertFalse(missing.exists())
        gateway = ActionGateway(missing, clock=CountingClock(), id_provider=self.db.gateway()._ids,
                                catalog=CountingCatalog(), capabilities=CapabilityGate().narrow())
        for smuggled in ({"customer_id": "CUST-002"}, {"approved": "true"}, {"case_id": "AS6-X"}):
            args = {**RETURN_ARGS, **smuggled}
            canonical = canonical_args(args)
            forged = ValidatedAction(action_name="create_return", args=args,
                                     canonical_args_json=canonical, args_sha256=args_digest(canonical),
                                     target_order_id="ORD-1001", target_order_item_id="OI-1001-2")
            with self.subTest(smuggled=smuggled):
                with self.assertRaises(ActionContractError):
                    gateway.start_action(identity(), forged)
        self.assertFalse(missing.exists())

    def test_p6_writes_use_the_trusted_customer_only(self):
        outcome = self.start("create_exchange", {**EXCHANGE_ARGS, "order_id": "ORD-2001",
                                                 "order_item_id": "OI-2001-2", "target_sku": "SKU-SOCKS"})
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.DENIED, "order_not_accessible"))
        foreign = self.start("escalate_to_human", {**HANDOFF_ARGS, "order_id": "ORD-2001",
                                                   "order_item_id": "OI-2001-1"}, request_id="req-2")
        missing = self.start("escalate_to_human", {**HANDOFF_ARGS, "order_id": "ORD-9999",
                                                   "order_item_id": "OI-2001-1"}, request_id="req-2")
        self.assertEqual((foreign.code, missing.code), ("order_not_accessible", "order_not_accessible"))
        renderer = ActionOutcomeRenderer()
        self.assertEqual(renderer.render(foreign), renderer.render(missing))
        own = self.start("escalate_to_human", {**HANDOFF_ARGS, "order_id": "ORD-2001",
                                               "order_item_id": "OI-2001-1"}, persona_id="demo-b",
                         request_id="req-3")
        self.assertIs(own.status, ActionStatus.EXECUTED)
        self.assertEqual(self.db.rows("SELECT persona_id FROM action_receipts"), [("demo-b",)])

    def test_formal_gateway_requires_deterministic_ids(self):
        with self.assertRaises(ValueError):
            self.db.gateway(id_provider=UuidIdProvider(), formal=True)
        gateway = self.db.gateway(id_provider=UuidIdProvider(), formal=False)
        outcome = gateway.start_action(identity(), validate("escalate_to_human", HANDOFF_ARGS))
        self.assertRegex(outcome.receipt.resource_id, ID_PATTERN)

    def test_ids_are_derived_from_the_server_key(self):
        action = validate("create_exchange", EXCHANGE_ARGS)
        outcome = self.start("create_exchange", EXCHANGE_ARGS)
        key = idempotency_key(identity(), action)
        ids = self.db.gateway()._ids
        from aftersales.ids import IdKind
        self.assertEqual(outcome.receipt.receipt_id, ids.new_id(IdKind.RECEIPT, key))
        self.assertEqual(outcome.receipt.resource_id, ids.new_id(IdKind.AFTER_SALES_CASE, key))
        self.assertEqual(self.db.rows("SELECT idempotency_key FROM action_receipts"), [(key,)])

    def test_p8_cross_process_determinism(self):
        local = self.start("create_exchange", EXCHANGE_ARGS)
        local_rows = self.db.rows("SELECT receipt_id, resource_id, snapshot_json FROM action_receipts")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            script = Path(tmp) / "child.py"
            output = Path(tmp) / "child.json"
            script.write_text(textwrap.dedent(f"""
                import json, sqlite3, sys
                from pathlib import Path
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from tests.stage6_support import Stage6Database, STOCK_TSHIRT_L, EXCHANGE_ARGS, identity, validate
                with Stage6Database() as db:
                    db.execute(STOCK_TSHIRT_L)
                    outcome = db.gateway().start_action(identity(), validate("create_exchange", EXCHANGE_ARGS))
                    rows = db.rows("SELECT receipt_id, resource_id, snapshot_json FROM action_receipts")
                Path({str(output)!r}).write_text(json.dumps({{"outcome": outcome.to_dict(),
                    "rows": rows}}, ensure_ascii=False), encoding="utf-8")
            """), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(script)], cwd=str(REPO_ROOT),
                                       capture_output=True, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
            child = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(child["outcome"], local.to_dict())
        self.assertEqual([tuple(row) for row in child["rows"]], local_rows)


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.renderer = ActionOutcomeRenderer()

    def outcome(self, **kwargs):
        defaults = dict(action_name="create_exchange", request_id="req-1")
        defaults.update(kwargs)
        return ActionOutcome(**defaults)

    def test_only_executed_claims_submission(self):
        executed = self.outcome(status=ActionStatus.EXECUTED,
                                receipt=ReceiptView(receipt_id="RC-0123456789ABCDEF",
                                                    resource_type="after_sales_case",
                                                    resource_id="AS6-0123456789ABCDEF"),
                                guard=GuardView(decision="ALLOW", reason_code="risk_policy_allows"))
        text = self.renderer.render(executed)
        self.assertIn("已提交换货申请", text)
        self.assertIn("AS6-0123456789ABCDEF", text)
        ticket = self.outcome(action_name="escalate_to_human", status=ActionStatus.EXECUTED,
                              receipt=ReceiptView(receipt_id="RC-0123456789ABCDEF",
                                                  resource_type="human_handoff_ticket",
                                                  resource_id="HT-0123456789ABCDEF"),
                              guard=GuardView(decision="ALLOW", reason_code="risk_policy_allows"))
        self.assertIn("已创建人工客服工单", self.renderer.render(ticket))

    def test_denied_and_failed_never_claim_execution(self):
        for action in ("create_return", "create_exchange", "escalate_to_human"):
            for code in DENY_REASON_CODES:
                with self.subTest(action=action, code=code):
                    text = self.renderer.render(self.outcome(
                        action_name=action, status=ActionStatus.DENIED, code=code,
                        guard=GuardView(decision="DENY", reason_code=code)))
                    self.assertIn("没有", text)
                    self.assertNotIn(code, text)
                    self.assertNotIn("_", text)
                    for marker in COMPLETION_CLAIM_MARKERS:
                        self.assertNotIn(marker, text)
            failed = self.renderer.render(self.outcome(action_name=action, status=ActionStatus.FAILED,
                                                       code="write_failed"))
            self.assertIn("没有提交", failed)
            self.assertNotIn("write_failed", failed)
            for marker in COMPLETION_CLAIM_MARKERS:
                self.assertNotIn(marker, failed)

    def test_every_deny_code_has_a_rendering(self):
        self.assertEqual(set(DENIED_EXPLANATIONS), set(DENY_REASON_CODES))

    def test_outcome_contract(self):
        with self.assertRaises(ValueError):
            self.outcome(status=ActionStatus.EXECUTED)  # no receipt
        with self.assertRaises(ValueError):
            self.outcome(status=ActionStatus.FAILED, code="sqlite exploded")
        with self.assertRaises(ValueError):
            self.outcome(status=ActionStatus.DENIED, code="nope",
                         guard=GuardView(decision="DENY", reason_code="return_window_closed"))
        with self.assertRaises(ValueError):
            self.outcome(action_name="refund_money", status=ActionStatus.FAILED, code="write_failed")
        self.assertNotIn("customer_id", self.outcome(status=ActionStatus.FAILED, code="write_failed").to_dict())


class StaticBoundaryTests(unittest.TestCase):
    @staticmethod
    def sql_statements(path):
        """Non-docstring string constants that start like an SQL statement."""
        import ast
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        statements = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                head = node.value.strip().upper()
                if head.split(" ", 1)[0] in {"SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN",
                                             "COMMIT", "ROLLBACK", "PRAGMA", "DROP", "CREATE"}:
                    statements.append(head)
        return statements

    def test_only_the_gateway_layer_writes(self):
        writes = ("INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "DROP", "CREATE", "PRAGMA")
        for name in ("guard.py", "guard_state.py", "actions.py", "action_policy.py", "capabilities.py",
                     "ids.py", "action_outcome.py", "action_errors.py"):
            with self.subTest(module=name):
                for statement in self.sql_statements(REPO_ROOT / "aftersales" / name):
                    self.assertNotIn(statement.split(" ", 1)[0], writes)
        guard_sql = self.sql_statements(REPO_ROOT / "aftersales" / "guard_state.py")
        self.assertEqual(len(guard_sql), 8)
        self.assertTrue(all(statement.startswith("SELECT") for statement in guard_sql))
        store_sql = self.sql_statements(REPO_ROOT / "aftersales" / "action_store.py")
        self.assertEqual(sorted(statement.split(" ", 1)[0] for statement in store_sql),
                         ["INSERT", "INSERT", "INSERT", "INSERT", "SELECT", "SELECT"])
        for statement in store_sql:
            self.assertNotIn("UPDATE ", statement)
            self.assertNotIn("DELETE ", statement)
            self.assertNotIn("INVENTORY", statement)

    def test_stage61_approval_branch_is_visibly_temporary(self):
        source = (REPO_ROOT / "aftersales" / "action_gateway.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("STAGE 6.1 ONLY"), 3)
        self.assertIn("delete in Stage 6.2", source)


if __name__ == "__main__":
    unittest.main()
