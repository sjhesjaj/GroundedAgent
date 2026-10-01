"""Stage 6.2 approval / resume (docs/v2/stage6-design.md §10.3, §11, §12, §13, §14, §20).

Every test runs on a real file-backed Stage 6 database in a temporary directory.
"""

import ast
import dataclasses
import gc
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import aftersales.action_gateway as gateway_module
from aftersales.action_errors import (
    ActionValidationError,
    ApprovalInputError,
    NotApproved,
    PendingTransitionConflict,
    UnknownPendingAction,
)
from aftersales.action_outcome import (
    COMPLETION_CLAIM_MARKERS,
    ActionOutcome,
    ActionOutcomeRenderer,
    ActionStatus,
)
from aftersales.action_policy import ActionRiskPolicy, S6_RISK_POLICY
from aftersales.action_store import ActionStore
from aftersales.actions import FORBIDDEN_ACTION_ARGUMENT_NAMES, ActionIntentValidator, build_action_registry
from aftersales.approval import ApprovalDecision
from aftersales.capabilities import CapabilityGate
from aftersales.guard import Guard, snapshot_document
from aftersales.guard_snapshot import parse_snapshot_document, stale_reason
from aftersales.ids import DeterministicIdProvider, IdKind, idempotency_key

from tests.stage6_support import (
    LATE_NOW,
    RETURN_ARGS,
    VIRTUAL_NOW,
    CountingCatalog,
    CountingClock,
    Hooks,
    Stage6Database,
    approval,
    audit_trail,
    frozen_snapshot,
    identity,
    insert_case,
    pending_row,
    raiser,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BUSINESS_TABLES = ("orders", "order_items", "logistics", "inventory", "after_sales_cases",
                   "sku_variants", "human_handoff_tickets")
FORBIDDEN_CLAIMS = ("已提交", "已办理", "已退款", "已创建工单", "已转人工")


class ApprovalCase(unittest.TestCase):
    def setUp(self):
        self.db = Stage6Database()
        self.addCleanup(self.db.close)

    def gateway(self, **options):
        return self.db.gateway(**options)

    def start_return(self, request_id="req-1", **options):
        outcome = self.gateway(**options).start_action(identity(request_id), validate("create_return", RETURN_ARGS))
        self.assertIs(outcome.status, ActionStatus.WAITING_APPROVAL)
        return outcome.pending_action_id

    def business(self):
        return {table: self.db.rows("SELECT * FROM " + table + " ORDER BY 1") for table in BUSINESS_TABLES}

    def return_cases(self):
        return self.db.rows("SELECT case_id, order_id, order_item_id, customer_id, type, status, reason,"
                            " created_at, updated_at, version FROM after_sales_cases WHERE case_id LIKE 'AS6-%'")

    def assert_not_executed(self):
        self.assertEqual(self.return_cases(), [])
        self.assertEqual(self.db.count("action_receipts"), 0)
        self.assertEqual(self.db.count("human_handoff_tickets"), 0)


# --------------------------------------------------------------------------
# P0 and the trusted boundary
# --------------------------------------------------------------------------


class PendingCreationTests(ApprovalCase):
    def test_p0_eligible_return_persists_pending_approval(self):
        before = self.business()
        action = validate("create_return", RETURN_ARGS)
        outcome = self.gateway().start_action(identity(), action)
        key = idempotency_key(identity(), action)
        self.assertIs(outcome.status, ActionStatus.WAITING_APPROVAL)
        self.assertFalse(outcome.approval_recorded)
        self.assertEqual((outcome.guard.decision, outcome.guard.reason_code),
                         ("REQUIRE_APPROVAL", "risk_policy_requires_approval"))
        self.assertEqual(outcome.pending_action_id,
                         DeterministicIdProvider("eval").new_id(IdKind.PENDING_ACTION, key))
        row = pending_row(self.db, outcome.pending_action_id)
        snapshot = parse_snapshot_document(row["snapshot_json"], expected_sha256=row["snapshot_sha256"],
                                           expected_action="create_return")
        self.assertEqual(dict(snapshot.snapshot.records), {
            "after_sales_cases": (), "logistics": (("SF1001", 5),),
            "order_items": (("OI-1001-2", 1),), "orders": (("ORD-1001", 4),)})
        expected = {
            "idempotency_key": key, "request_id": "req-1", "persona_id": "demo-a",
            "action_name": "create_return", "args_json": action.canonical_args_json,
            "args_sha256": action.args_sha256, "target_order_id": "ORD-1001",
            "target_order_item_id": "OI-1001-2", "status": "PENDING_APPROVAL",
            "guard_decision": "REQUIRE_APPROVAL", "guard_reason_code": "risk_policy_requires_approval",
            "snapshot_sha256": hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest(),
            "action_spec_version": "s6-actions/1", "risk_policy_version": "s6-risk/1",
            "policy_build_id": "build-0001", "approval_decision": None, "approver_ref": None,
            "decided_at": None, "outcome_code": None, "receipt_id": None,
            "created_at": "2026-11-15T10:00:00+08:00", "updated_at": "2026-11-15T10:00:00+08:00",
            "version": 1,
        }
        for column, value in expected.items():
            self.assertEqual(row[column], value, column)
        self.assertNotIn("CUST-001", row["snapshot_json"])
        self.assertEqual(self.business(), before)
        self.assertEqual(self.db.count("action_receipts"), 0)
        self.assertEqual(audit_trail(self.db), [
            ("guard.evaluated", "start", "REQUIRE_APPROVAL", "risk_policy_requires_approval"),
            ("action.pending_created", "start", None, None)])

    def test_stage61_temporary_path_is_gone(self):
        self.assertFalse(hasattr(gateway_module, "ApprovalPathNotEnabled"))
        source = (REPO_ROOT / "aftersales" / "action_gateway.py").read_text(encoding="utf-8")
        self.assertNotIn("ApprovalPathNotEnabled", source)
        self.assertNotIn("STAGE 6.1 ONLY", source)

    def test_renderer_waiting_approval(self):
        outcome = self.gateway().start_action(identity(), validate("create_return", RETURN_ARGS))
        text = ActionOutcomeRenderer().render(outcome)
        self.assertIn("等待审批", text)
        self.assertIn("不会执行", text)
        for marker in COMPLETION_CLAIM_MARKERS:
            self.assertNotIn(marker, text)


class ApprovalBoundaryTests(ApprovalCase):
    def test_decision_shape_is_closed(self):
        approval("PA-0123456789ABCDEF")
        for kwargs in ({"pending_action_id": "pa-0123456789abcdef"}, {"pending_action_id": "AS6-0123456789ABCDEF"},
                       {"pending_action_id": "PA-1"}, {"decision": "approve"}, {"decision": "已批准"},
                       {"decision": "YES"}, {"decision": None}, {"approver_ref": "manager"},
                       {"approver_ref": "我是店长"}, {"approver_ref": "op-Demo"}, {"decided_at": "2026-11-15T10:00:00"},
                       {"decided_at": "today"}, {"decided_at": None}):
            base = dict(pending_action_id="PA-0123456789ABCDEF", decision="APPROVE",
                        approver_ref="op-demo-1", decided_at=VIRTUAL_NOW.isoformat())
            base.update(kwargs)
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ApprovalInputError):
                    ApprovalDecision(**base)

    def test_p5_user_or_model_text_cannot_approve(self):
        pending_id = self.start_return()
        before = self.db.dump()
        clock = CountingClock()
        gateway = self.gateway(clock=clock)
        for forged in ("经理批准了", "approved", {"pending_action_id": pending_id, "decision": "APPROVE"},
                       ("APPROVE",), None):
            with self.subTest(forged=repr(forged)[:30]):
                with self.assertRaises(ApprovalInputError):
                    gateway.resume_action(forged)
                with self.assertRaises(ApprovalInputError):
                    gateway.record_decision(forged)
        with self.assertRaises(ApprovalInputError):  # unregistered operator
            gateway.resume_action(approval(pending_id, approver_ref="op-demo-2"))
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self.db.dump(), before)
        self.assertEqual(pending_row(self.db, pending_id)["status"], "PENDING_APPROVAL")

    def test_p5_approval_arguments_are_still_rejected_by_the_action_contract(self):
        validator = ActionIntentValidator(build_action_registry(), CapabilityGate().narrow().actions)
        for name in ("approved", "approval_required", "approval_decision", "approver_ref",
                     "skip_approval", "approval", "pending_action_id", "decision", "status"):
            self.assertIn(name, FORBIDDEN_ACTION_ARGUMENT_NAMES)
            with self.subTest(argument=name):
                with self.assertRaises(ActionValidationError):
                    validator.validate("create_return", {**RETURN_ARGS, name: "APPROVE"})

    def test_p5_t2_cannot_execute_without_a_decision(self):
        pending_id = self.start_return()
        before = self.db.dump()
        with self.assertRaises(NotApproved):
            self.gateway().execute_approved(pending_id)
        self.assertEqual(self.db.dump(), before)
        self.assertEqual(pending_row(self.db, pending_id)["status"], "PENDING_APPROVAL")

    def test_p5_trusted_approve_is_the_only_path_into_approved(self):
        pending_id = self.start_return()
        store_source = (REPO_ROOT / "aftersales" / "action_store.py").read_text(encoding="utf-8")
        self.assertEqual(store_source.count("status, outcome = (APPROVED, None)"), 1)
        # transition_terminal can never reach APPROVED (or EXECUTED).
        connection = sqlite3.connect(str(self.db.path))
        try:
            for status in ("APPROVED", "EXECUTED", "PENDING_APPROVAL", "REJECTED"):
                with self.subTest(status=status):
                    with self.assertRaises(ValueError):
                        ActionStore(connection).transition_terminal(
                            pending_action_id=pending_id, expected_version=1, status=status,
                            outcome_code="write_failed", at=VIRTUAL_NOW.isoformat())
        finally:
            connection.close()
        outcome = self.gateway().record_decision(approval(pending_id))
        self.assertTrue(outcome.approval_recorded)
        self.assertEqual(pending_row(self.db, pending_id)["status"], "APPROVED")

    def test_unknown_pending_and_early_decision(self):
        before = self.db.dump()
        with self.assertRaises(UnknownPendingAction):
            self.gateway().record_decision(approval("PA-0123456789ABCDEF"))
        with self.assertRaises(UnknownPendingAction):
            self.gateway().execute_approved("PA-0123456789ABCDEF")
        with self.assertRaises(UnknownPendingAction):
            self.gateway().get_outcome("PA-0123456789ABCDEF")
        self.assertEqual(self.db.dump(), before)
        pending_id = self.start_return()
        with self.assertRaises(ApprovalInputError):
            self.gateway().record_decision(approval(
                pending_id, at=VIRTUAL_NOW.replace(year=2026, month=11, day=14)))
        self.assertEqual(pending_row(self.db, pending_id)["status"], "PENDING_APPROVAL")


# --------------------------------------------------------------------------
# T1, T2 and the state machine
# --------------------------------------------------------------------------


class RecordDecisionTests(ApprovalCase):
    def test_p1_approve(self):
        pending_id = self.start_return()
        clock = CountingClock()
        outcome = self.gateway(clock=clock).record_decision(approval(pending_id))
        self.assertEqual(clock.calls, 1)  # P-12 T1
        self.assertIs(outcome.status, ActionStatus.WAITING_APPROVAL)
        self.assertTrue(outcome.approval_recorded)
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["approval_decision"], row["approver_ref"], row["decided_at"],
                          row["outcome_code"], row["receipt_id"], row["updated_at"], row["version"]),
                         ("APPROVED", "APPROVE", "op-demo-1", "2026-11-15T10:00:00+08:00", None, None,
                          "2026-11-15T10:00:00+08:00", 2))
        self.assertEqual(audit_trail(self.db, pending_id)[-1], ("approval.recorded", "resume", "APPROVE", None))
        self.assert_not_executed()

    def test_p2_reject(self):
        pending_id = self.start_return()
        outcome = self.gateway().record_decision(approval(pending_id, "REJECT"))
        self.assertIs(outcome.status, ActionStatus.REJECTED)
        self.assertEqual(outcome.code, "approval_rejected")
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["approval_decision"], row["outcome_code"], row["version"]),
                         ("REJECTED", "REJECT", "approval_rejected", 2))
        self.assertEqual(audit_trail(self.db, pending_id)[-2:], [
            ("approval.recorded", "resume", "REJECT", None),
            ("action.not_executed", "resume", "REJECTED", "approval_rejected")])

    def test_first_decision_wins(self):
        gateway = self.gateway(operators=frozenset({"op-demo-1", "op-demo-2"}))
        approved = self.start_return(request_id="req-a")
        gateway.record_decision(approval(approved))
        replay = gateway.record_decision(approval(approved, approver_ref="op-demo-2"))
        self.assertTrue(replay.idempotent_replay)
        self.assertFalse(replay.decision_conflict)
        conflict = gateway.record_decision(approval(approved, "REJECT"))
        self.assertTrue(conflict.decision_conflict)
        self.assertIs(conflict.status, ActionStatus.WAITING_APPROVAL)
        row = pending_row(self.db, approved)
        self.assertEqual((row["status"], row["approval_decision"], row["approver_ref"], row["version"]),
                         ("APPROVED", "APPROVE", "op-demo-1", 2))  # first metadata kept
        self.assertEqual(audit_trail(self.db, approved)[-1], ("approval.conflict", "resume", "REJECT", None))

    def test_rejected_then_approve_is_a_conflict(self):
        pending_id = self.start_return()
        self.gateway().resume_action(approval(pending_id, "REJECT"))
        outcome = self.gateway().resume_action(approval(pending_id))
        self.assertIs(outcome.status, ActionStatus.REJECTED)
        self.assertTrue(outcome.decision_conflict)
        self.assertEqual(pending_row(self.db, pending_id)["status"], "REJECTED")
        self.assert_not_executed()


class ExecuteApprovedTests(ApprovalCase):
    def test_p3_approved_return_executes_once(self):
        pending_id = self.start_return()
        inventory_before = self.db.rows("SELECT * FROM inventory ORDER BY sku")
        orders_before = self.db.rows("SELECT * FROM orders ORDER BY order_id")
        logistics_before = self.db.rows("SELECT * FROM logistics ORDER BY tracking_no")
        self.gateway().record_decision(approval(pending_id))
        clock, catalog = CountingClock(), CountingCatalog()
        outcome = self.gateway(clock=clock, catalog=catalog).execute_approved(pending_id)
        self.assertEqual((clock.calls, catalog.calls), (1, 1))  # P-12 T2, P-14
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(outcome.pending_action_id, pending_id)
        cases = self.return_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0][1:], ("ORD-1001", "OI-1001-2", "CUST-001", "return", "待处理",
                                        "不想要了（无理由退货）", "2026-11-15T10:00:00+08:00",
                                        "2026-11-15T10:00:00+08:00", 1))
        receipts = self.db.rows("SELECT receipt_id, resource_type, resource_id, pending_action_id,"
                                " guard_decision, guard_reason_code FROM action_receipts")
        self.assertEqual(receipts, [(outcome.receipt.receipt_id, "after_sales_case", cases[0][0], pending_id,
                                     "REQUIRE_APPROVAL", "risk_policy_requires_approval")])
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["receipt_id"], row["outcome_code"], row["version"]),
                         ("EXECUTED", outcome.receipt.receipt_id, None, 3))
        self.assertEqual(self.db.rows("SELECT * FROM inventory ORDER BY sku"), inventory_before)
        self.assertEqual(self.db.rows("SELECT * FROM orders ORDER BY order_id"), orders_before)
        self.assertEqual(self.db.rows("SELECT * FROM logistics ORDER BY tracking_no"), logistics_before)
        self.assertEqual(audit_trail(self.db, pending_id)[-4:], [
            ("resume.started", "resume", None, None),
            ("resume.version_check", "resume", "MATCH", None),
            ("guard.evaluated", "resume", "REQUIRE_APPROVAL", "risk_policy_requires_approval"),
            ("action.executed", "resume", None, "after_sales_case")])
        text = ActionOutcomeRenderer().render(outcome)
        self.assertIn("已提交退货申请", text)
        self.assertIn(cases[0][0], text)

    def test_time_only_change_is_denied_not_stale(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        outcome = self.gateway(clock=CountingClock(LATE_NOW)).execute_approved(pending_id)
        self.assertIs(outcome.status, ActionStatus.DENIED)
        self.assertEqual(outcome.code, "return_window_closed")
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["outcome_code"]), ("DENIED", "return_window_closed"))
        self.assertEqual(audit_trail(self.db, pending_id)[-3:], [
            ("resume.version_check", "resume", "MATCH", None),
            ("guard.evaluated", "resume", "DENY", "return_window_closed"),
            ("action.not_executed", "resume", "DENIED", "return_window_closed")])
        self.assert_not_executed()
        text = ActionOutcomeRenderer().render(outcome)
        self.assertIn("已超过退货时限", text)
        self.assertIn("没有提交", text)

    def test_policy_build_change_is_stale_policy_changed(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        republished = dataclasses.replace(frozen_snapshot(), build_id="build-0002")
        catalog = CountingCatalog(republished)
        outcome = self.gateway(catalog=catalog).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.STALE, "policy_changed"))
        self.assertEqual(catalog.calls, 1)
        self.assert_not_executed()

    def test_risk_policy_version_change_is_stale_action_policy_changed(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        stricter = ActionRiskPolicy(version="s6-risk/2", dispositions=dict(S6_RISK_POLICY.dispositions))
        outcome = self.gateway(risk_policy=stricter).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.STALE, "action_policy_changed"))
        self.assert_not_executed()

    def test_action_spec_version_change_is_stale_action_policy_changed(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        row = pending_row(self.db, pending_id)
        document = json.loads(row["snapshot_json"])
        document["action_spec_version"] = "s6-actions/0"
        from aftersales.policy_source import canonical
        rewritten = canonical(document)
        self.db.execute("UPDATE pending_actions SET snapshot_json = ?, snapshot_sha256 = ?,"
                        " action_spec_version = 's6-actions/0' WHERE pending_action_id = ?",
                        (rewritten, hashlib.sha256(rewritten.encode("utf-8")).hexdigest(), pending_id))
        outcome = self.gateway().execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.STALE, "action_policy_changed"))
        self.assert_not_executed()

    def test_changed_guard_decision_is_stale(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        misconfigured = ActionRiskPolicy(version="s6-risk/1", dispositions={
            **S6_RISK_POLICY.dispositions, "create_return": "ALLOW"})
        outcome = self.gateway(risk_policy=misconfigured).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.STALE, "guard_decision_changed"))
        self.assert_not_executed()

    def test_tampered_persisted_state_fails_closed(self):
        tamperings = {
            "snapshot without digest": ("UPDATE pending_actions SET snapshot_json = replace(snapshot_json,"
                                        " '\"ORD-1001\":4', '\"ORD-1001\":5')"),
            "args": ("UPDATE pending_actions SET args_json = replace(args_json, 'no_longer_wanted',"
                     " 'size_or_spec_mismatch')"),
            "digest": "UPDATE pending_actions SET args_sha256 = '0000'",
            "key": "UPDATE pending_actions SET idempotency_key = 's6k1-' || substr(idempotency_key, 7) || '0'",
        }
        for label, sql in tamperings.items():
            with self.subTest(tampering=label):
                with Stage6Database() as db:
                    pending_id = db.gateway().start_action(identity(), validate("create_return", RETURN_ARGS)).pending_action_id
                    db.gateway().record_decision(approval(pending_id))
                    db.execute(sql)
                    outcome = db.gateway().execute_approved(pending_id)
                    self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "invariant_violation"))
                    self.assertEqual(pending_row(db, pending_id)["status"], "FAILED")
                    self.assertEqual(db.count("action_receipts"), 0)

    def test_snapshot_parser_is_closed(self):
        pending_id = self.start_return()
        row = pending_row(self.db, pending_id)
        document, digest = row["snapshot_json"], row["snapshot_sha256"]
        parse_snapshot_document(document, expected_sha256=digest, expected_action="create_return")
        data = json.loads(document)
        from aftersales.action_errors import SnapshotIntegrityError
        from aftersales.policy_source import canonical
        variants = {
            "extra key": {**data, "note": "x"},
            "schema": {**data, "schema": "s6-guard-snapshot/2"},
            "action": {**data, "action_name": "create_exchange"},
            "tables": {**data, "records": {**data["records"], "inventory": {}}},
            "version type": {**data, "records": {**data["records"], "orders": {"ORD-1001": "4"}}},
            "bool version": {**data, "records": {**data["records"], "orders": {"ORD-1001": True}}},
            "decision": {**data, "decision": {**data["decision"], "reason_code": "risk_policy_allows"}},
            "facts": {**data, "decision": {**data["decision"], "facts": {
                **data["decision"]["facts"], "category": "服装"}}},
            "evaluated_at": {**data, "evaluated_at": "2026-11-15T10:00:00"},
        }
        for label, variant in variants.items():
            text = canonical(variant)
            with self.subTest(variant=label):
                with self.assertRaises(SnapshotIntegrityError):
                    parse_snapshot_document(text, expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
                                            expected_action="create_return")
        with self.assertRaises(SnapshotIntegrityError):
            parse_snapshot_document(document, expected_sha256="0" * 64, expected_action="create_return")
        pretty = json.dumps(data, indent=1, ensure_ascii=False)
        with self.assertRaises(SnapshotIntegrityError):
            parse_snapshot_document(pretty, expected_sha256=hashlib.sha256(pretty.encode()).hexdigest(),
                                    expected_action="create_return")


class FailureSemanticsTests(ApprovalCase):
    def approved(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        return pending_id

    def test_p6_failure_after_approved_moves_to_failed(self):
        pending_id = self.approved()
        clock = CountingClock()
        hooks = Hooks(business_write=raiser(sqlite3.OperationalError("boom")))
        outcome = self.gateway(clock=clock, fault_hooks=hooks).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "write_failed"))
        self.assertEqual(clock.calls, 1)  # the compensation reuses txn_now
        self.assertEqual(hooks.seen, ["business_write", "compensation"])
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["outcome_code"], row["receipt_id"]), ("FAILED", "write_failed", None))
        self.assert_not_executed()
        self.assertEqual(audit_trail(self.db, pending_id)[-2:], [
            ("transaction.rolled_back", "resume", None, "write_failed"),
            ("action.not_executed", "resume", "FAILED", "write_failed")])
        # Terminal: a retry replays FAILED, nothing executes.
        again = self.gateway().execute_approved(pending_id)
        self.assertEqual((again.status, again.code, again.idempotent_replay), (ActionStatus.FAILED, "write_failed", True))
        self.assert_not_executed()

    def test_failed_compensation_leaves_approved_and_retry_is_safe(self):
        pending_id = self.approved()
        hooks = Hooks(receipt_write=raiser(sqlite3.OperationalError("boom")),
                      compensation=raiser(sqlite3.OperationalError("boom")))
        outcome = self.gateway(fault_hooks=hooks).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "write_failed"))
        self.assertEqual(pending_row(self.db, pending_id)["status"], "APPROVED")
        self.assert_not_executed()
        retry = self.gateway().execute_approved(pending_id)
        self.assertIs(retry.status, ActionStatus.EXECUTED)
        self.assertEqual(len(self.return_cases()), 1)
        self.assertEqual(self.db.count("action_receipts"), 1)

    def test_commit_failure_never_reports_executed(self):
        pending_id = self.approved()
        outcome = self.gateway(fault_hooks=Hooks(commit=raiser(sqlite3.OperationalError("boom")))).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "transaction_failed"))
        self.assertEqual(pending_row(self.db, pending_id)["status"], "FAILED")
        self.assert_not_executed()

    def test_t2_begin_failure_reads_no_clock_and_keeps_approved(self):
        pending_id = self.approved()
        before = self.db.dump()
        blocker = sqlite3.connect(str(self.db.path), isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            clock = CountingClock()
            outcome = self.gateway(clock=clock, busy_timeout_ms=0).execute_approved(pending_id)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertEqual((outcome.status, outcome.code, outcome.action_name), (ActionStatus.FAILED, "transaction_failed", None))
        self.assertEqual(outcome.pending_action_id, pending_id)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(self.db.dump(), before)
        self.assertEqual(ActionOutcomeRenderer().render(outcome),
                         "系统暂时无法完成该操作，申请没有提交。请稍后再试，或联系人工客服。")

    def test_identity_unresolvable(self):
        pending_id = self.approved()

        def unknown(persona_id):
            raise ValueError("unknown persona")

        outcome = self.gateway(persona_resolver=unknown).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "identity_unresolvable"))
        self.assertEqual(pending_row(self.db, pending_id)["status"], "FAILED")

    def test_guard_failure_on_resume(self):
        pending_id = self.approved()
        outcome = self.gateway(catalog=CountingCatalog(error=RuntimeError("down"))).execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.FAILED, "policy_unavailable"))
        self.assertEqual(audit_trail(self.db, pending_id)[-2:], [
            ("guard.failed", "resume", None, "policy_unavailable"),
            ("action.not_executed", "resume", "FAILED", "policy_unavailable")])


class LegalTransitionTests(ApprovalCase):
    def test_every_legal_transition(self):
        # P0, P1, P3
        executed = self.start_return(request_id="req-p3")
        self.assertEqual(pending_row(self.db, executed)["status"], "PENDING_APPROVAL")
        self.gateway().record_decision(approval(executed))
        self.assertEqual(pending_row(self.db, executed)["status"], "APPROVED")
        self.gateway().execute_approved(executed)
        self.assertEqual(pending_row(self.db, executed)["status"], "EXECUTED")
        # P2, P4, P5, P6, each on its own item / database.
        cases = {
            "REJECTED": lambda db, pid: db.gateway().record_decision(approval(pid, "REJECT")),
            "STALE": lambda db, pid: (db.gateway().record_decision(approval(pid)),
                                      db.execute("UPDATE orders SET version = 5 WHERE order_id = 'ORD-1001'"),
                                      db.gateway().execute_approved(pid)),
            "DENIED": lambda db, pid: (db.gateway().record_decision(approval(pid)),
                                       db.gateway(clock=CountingClock(LATE_NOW)).execute_approved(pid)),
            "FAILED": lambda db, pid: (db.gateway().record_decision(approval(pid)),
                                       db.gateway(fault_hooks=Hooks(business_write=raiser(
                                           sqlite3.OperationalError("x")))).execute_approved(pid)),
        }
        for status, run in cases.items():
            with self.subTest(status=status):
                with Stage6Database() as db:
                    pid = db.gateway().start_action(identity(), validate("create_return", RETURN_ARGS)).pending_action_id
                    run(db, pid)
                    self.assertEqual(pending_row(db, pid)["status"], status)

    def test_illegal_transitions_are_refused(self):
        pending_id = self.start_return()
        connection = sqlite3.connect(str(self.db.path), isolation_level=None)
        try:
            store = ActionStore(connection)
            at = VIRTUAL_NOW.isoformat()
            # PENDING_APPROVAL cannot go straight to EXECUTED / STALE / DENIED / FAILED.
            with self.assertRaises(PendingTransitionConflict):
                store.mark_executed(pending_action_id=pending_id, expected_version=1,
                                    receipt_id="RC-0123456789ABCDEF", at=at)
            for status, code in (("STALE", "record_version_changed"), ("DENIED", "return_window_closed"),
                                 ("FAILED", "write_failed")):
                with self.subTest(status=status):
                    with self.assertRaises(PendingTransitionConflict):
                        store.transition_terminal(pending_action_id=pending_id, expected_version=1,
                                                  status=status, outcome_code=code, at=at)
            # A stale version is refused.
            with self.assertRaises(PendingTransitionConflict):
                store.record_decision(pending_action_id=pending_id, expected_version=7, decision="APPROVE",
                                      approver_ref="op-demo-1", decided_at=at, at=at)
            # Codes must belong to their terminal status.
            with self.assertRaises(ValueError):
                store.transition_terminal(pending_action_id=pending_id, expected_version=1, status="STALE",
                                          outcome_code="return_window_closed", at=at)
            store.record_decision(pending_action_id=pending_id, expected_version=1, decision="REJECT",
                                  approver_ref="op-demo-1", decided_at=at, at=at)
            # Terminal REJECTED is immutable.
            with self.assertRaises(PendingTransitionConflict):
                store.record_decision(pending_action_id=pending_id, expected_version=2, decision="APPROVE",
                                      approver_ref="op-demo-1", decided_at=at, at=at)
            with self.assertRaises(PendingTransitionConflict):
                store.transition_terminal(pending_action_id=pending_id, expected_version=2, status="FAILED",
                                          outcome_code="write_failed", at=at)
        finally:
            connection.close()
        self.assertEqual(pending_row(self.db, pending_id)["status"], "REJECTED")


class DatabaseConstraintTests(ApprovalCase):
    INSERT = ("INSERT INTO pending_actions (pending_action_id, idempotency_key, request_id, persona_id,"
              " action_name, args_json, args_sha256, target_order_id, target_order_item_id, status,"
              " guard_decision, guard_reason_code, snapshot_json, snapshot_sha256, action_spec_version,"
              " risk_policy_version, policy_build_id, approval_decision, approver_ref, decided_at,"
              " outcome_code, receipt_id, created_at, updated_at, version) VALUES"
              " (?, ?, 'r', 'demo-a', 'create_return', '{}', 'h', 'ORD-1001', ?, ?, 'REQUIRE_APPROVAL',"
              " 'risk_policy_requires_approval', '{}', 'h', 'v', 'v', 'b', ?, ?, ?, ?, ?, 't', 't', 1)")

    def insert(self, db, *, pid="PA-1", key="k1", item="OI-1001-2", status="PENDING_APPROVAL",
               decision=None, approver=None, decided=None, outcome=None, receipt=None):
        db.execute(self.INSERT, (pid, key, item, status, decision, approver, decided, outcome, receipt))

    def test_check_constraints(self):
        violations = {
            "EXECUTED without receipt": dict(status="EXECUTED", decision="APPROVE", approver="op", decided="t"),
            "REJECTED with APPROVE": dict(status="REJECTED", decision="APPROVE", approver="op", decided="t",
                                          outcome="approval_rejected"),
            "APPROVED with REJECT": dict(status="APPROVED", decision="REJECT", approver="op", decided="t"),
            "APPROVED without decision": dict(status="APPROVED"),
            "PENDING with approval fields": dict(decision="APPROVE", approver="op", decided="t"),
            "half-filled approval": dict(status="APPROVED", decision="APPROVE"),
            "STALE without code": dict(status="STALE", decision="APPROVE", approver="op", decided="t"),
            "PENDING with code": dict(outcome="write_failed"),
            "unknown status": dict(status="DONE"),
        }
        for label, kwargs in violations.items():
            with self.subTest(violation=label):
                with Stage6Database() as db:
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert(db, **kwargs)

    def test_unique_constraints(self):
        self.insert(self.db)
        with self.assertRaises(sqlite3.IntegrityError):  # one idempotency key
            self.insert(self.db, pid="PA-2", item="OI-1001-1")
        with self.assertRaises(sqlite3.IntegrityError):  # one open pending per item
            self.insert(self.db, pid="PA-2", key="k2")
        self.insert(self.db, pid="PA-3", key="k3", status="REJECTED", decision="REJECT", approver="op",
                    decided="t", outcome="approval_rejected")  # terminal rows do not count
        receipt = ("INSERT INTO action_receipts (receipt_id, idempotency_key, request_id, persona_id,"
                   " action_name, args_json, args_sha256, result_status, resource_type, resource_id,"
                   " pending_action_id, guard_decision, guard_reason_code, snapshot_json, snapshot_sha256,"
                   " action_spec_version, risk_policy_version, policy_build_id, executed_at) VALUES"
                   " (?, ?, 'r', 'p', 'create_return', '{}', 'h', 'EXECUTED', 'after_sales_case', ?, 'PA-1',"
                   " 'REQUIRE_APPROVAL', 'risk_policy_requires_approval', '{}', 'h', 'v', 'v', 'b', 't')")
        self.db.execute(receipt, ("RC-1", "rk1", "AS6-1"))
        with self.assertRaises(sqlite3.IntegrityError):  # one receipt per pending
            self.db.execute(receipt, ("RC-2", "rk2", "AS6-2"))
        with self.assertRaises(sqlite3.IntegrityError):  # one receipt per key
            self.db.execute(receipt.replace("'PA-1'", "NULL").replace("'REQUIRE_APPROVAL'", "'ALLOW'"),
                            ("RC-3", "rk1", "AS6-3"))
        with self.assertRaises(sqlite3.IntegrityError):  # a receipt's pending must exist
            self.db.execute(receipt.replace("'PA-1'", "'PA-404'"), ("RC-4", "rk4", "AS6-4"))


# --------------------------------------------------------------------------
# A21-c, A22, A23, replay
# --------------------------------------------------------------------------


class A21DuplicateApprovalTests(ApprovalCase):
    def test_a21c_duplicate_submission_approval_and_resume(self):
        gateway = self.gateway()
        action = validate("create_return", RETURN_ARGS)
        first = gateway.start_action(identity(), action)
        catalog = CountingCatalog()
        replayed = self.gateway(catalog=catalog).start_action(identity(), action)
        self.assertEqual((replayed.status, replayed.pending_action_id, replayed.idempotent_replay),
                         (ActionStatus.WAITING_APPROVAL, first.pending_action_id, True))
        self.assertEqual(catalog.calls, 0)  # no new Guard execution for a replay
        pid = first.pending_action_id
        executed = gateway.resume_action(approval(pid))
        self.assertIs(executed.status, ActionStatus.EXECUTED)
        again = gateway.resume_action(approval(pid))
        self.assertEqual((again.status, again.receipt, again.idempotent_replay),
                         (ActionStatus.EXECUTED, executed.receipt, True))
        resumed = gateway.execute_approved(pid)
        self.assertEqual((resumed.status, resumed.receipt, resumed.idempotent_replay),
                         (ActionStatus.EXECUTED, executed.receipt, True))
        submitted = gateway.start_action(identity(), action)
        self.assertEqual((submitted.status, submitted.receipt, submitted.idempotent_replay),
                         (ActionStatus.EXECUTED, executed.receipt, True))
        conflict = gateway.resume_action(approval(pid, "REJECT"))
        self.assertTrue(conflict.decision_conflict)
        self.assertEqual(conflict.receipt, executed.receipt)
        self.assertEqual(self.db.count("pending_actions"), 1)
        self.assertEqual(len(self.return_cases()), 1)
        self.assertEqual(self.db.count("action_receipts"), 1)
        self.assertEqual(gateway.get_outcome(pid).receipt, executed.receipt)

    def test_replay_returns_every_stored_status(self):
        expectations = {}
        flows = {
            "PENDING_APPROVAL": lambda db, pid: None,
            "APPROVED": lambda db, pid: db.gateway().record_decision(approval(pid)),
            "EXECUTED": lambda db, pid: db.gateway().resume_action(approval(pid)),
            "REJECTED": lambda db, pid: db.gateway().resume_action(approval(pid, "REJECT")),
            "STALE": lambda db, pid: (db.gateway().record_decision(approval(pid)),
                                      db.execute("UPDATE logistics SET version = 6 WHERE tracking_no = 'SF1001'"),
                                      db.gateway().execute_approved(pid)),
            "DENIED": lambda db, pid: (db.gateway().record_decision(approval(pid)),
                                       db.gateway(clock=CountingClock(LATE_NOW)).execute_approved(pid)),
            "FAILED": lambda db, pid: (db.gateway().record_decision(approval(pid)),
                                       db.gateway(fault_hooks=Hooks(business_write=raiser(
                                           sqlite3.OperationalError("x")))).execute_approved(pid)),
        }
        expected_status = {"PENDING_APPROVAL": ActionStatus.WAITING_APPROVAL,
                           "APPROVED": ActionStatus.WAITING_APPROVAL, "EXECUTED": ActionStatus.EXECUTED,
                           "REJECTED": ActionStatus.REJECTED, "STALE": ActionStatus.STALE,
                           "DENIED": ActionStatus.DENIED, "FAILED": ActionStatus.FAILED}
        for name, flow in flows.items():
            with self.subTest(stored=name):
                with Stage6Database() as db:
                    action = validate("create_return", RETURN_ARGS)
                    pid = db.gateway().start_action(identity(), action).pending_action_id
                    flow(db, pid)
                    stored = db.gateway().get_outcome(pid)
                    catalog = CountingCatalog()
                    replay = db.gateway(catalog=catalog).start_action(identity(), action)
                    self.assertEqual(catalog.calls, 0)
                    self.assertIs(replay.status, expected_status[name])
                    self.assertTrue(replay.idempotent_replay)
                    self.assertEqual(replay.pending_action_id, pid)
                    self.assertEqual((replay.code, replay.receipt), (stored.code, stored.receipt))
                    self.assertEqual(replay.approval_recorded, name == "APPROVED")
                    expectations[name] = replay.status
        self.assertEqual(len(expectations), 7)


class A22StateChangeTests(ApprovalCase):
    def assert_stale(self, outcome, pending_id, reason):
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.STALE, reason))
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["outcome_code"], row["approval_decision"]), ("STALE", reason, "APPROVE"))
        self.assert_not_executed()
        self.assertEqual(audit_trail(self.db, pending_id)[-2:], [
            ("resume.version_check", "resume", "MISMATCH", reason),
            ("action.not_executed", "resume", "STALE", reason)])
        self.assertNotIn(("guard.evaluated", "resume"),
                         [row[:2] for row in audit_trail(self.db, pending_id)])
        text = ActionOutcomeRenderer().render(outcome)
        self.assertIn("没有执行", text)

    def test_a22a_version_change_before_approval(self):
        pending_id = self.start_return()
        self.db.execute("UPDATE order_items SET version = 2, updated_at = '2026-11-15T09:00:00+08:00'"
                        " WHERE order_item_id = 'OI-1001-2'")
        self.assert_stale(self.gateway().resume_action(approval(pending_id)), pending_id, "record_version_changed")

    def test_a22b_change_after_approval(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        self.db.execute("UPDATE logistics SET version = 6, updated_at = '2026-11-15T09:00:00+08:00'"
                        " WHERE tracking_no = 'SF1001'")
        self.assert_stale(self.gateway().execute_approved(pending_id), pending_id, "record_version_changed")

    def test_a22c_record_set_change(self):
        pending_id = self.start_return()
        insert_case(self.db, "AS-T9", "OI-1001-2", case_type="exchange", status="已完成")
        self.assert_stale(self.gateway().resume_action(approval(pending_id)), pending_id, "record_set_changed")

    def test_a22d_restart_between_change_and_execution(self):
        pending_id = self.start_return()
        gateway = self.gateway()
        gateway.record_decision(approval(pending_id))
        self.db.execute("UPDATE orders SET version = 5, updated_at = '2026-11-15T09:00:00+08:00'"
                        " WHERE order_id = 'ORD-1001'")
        del gateway
        gc.collect()
        fresh = self.db.gateway()  # nothing carried over but the file and the configuration
        self.assert_stale(fresh.execute_approved(pending_id), pending_id, "record_version_changed")

    def test_unrelated_changes_are_not_stale(self):
        pending_id = self.start_return()
        self.db.execute("UPDATE orders SET version = 9 WHERE order_id = 'ORD-2001'")
        self.db.execute("UPDATE inventory SET version = 99 WHERE sku = 'SKU-MUG'")
        self.db.execute("UPDATE order_items SET version = 7 WHERE order_item_id = 'OI-1001-1'")
        outcome = self.gateway().resume_action(approval(pending_id))
        self.assertIs(outcome.status, ActionStatus.EXECUTED)


class A23RejectionTests(ApprovalCase):
    def test_a23_rejection_never_executes(self):
        pending_id = self.start_return()
        outcome = self.gateway().resume_action(approval(pending_id, "REJECT"))
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.REJECTED, "approval_rejected"))
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["outcome_code"], row["receipt_id"]), ("REJECTED", "approval_rejected", None))
        self.assert_not_executed()
        text = ActionOutcomeRenderer().render(outcome)
        self.assertIn("没有执行", text)
        self.assertIn("未通过人工审批", text)
        self.assertIn("人工客服", text)
        for claim in FORBIDDEN_CLAIMS + COMPLETION_CLAIM_MARKERS:
            self.assertNotIn(claim, text)
        # T2 never runs for REJECTED; a later APPROVE is a conflict; replay creates nothing.
        replay = self.gateway().execute_approved(pending_id)
        self.assertEqual((replay.status, replay.idempotent_replay), (ActionStatus.REJECTED, True))
        conflict = self.gateway().resume_action(approval(pending_id))
        self.assertTrue(conflict.decision_conflict)
        resubmitted = self.gateway().start_action(identity(), validate("create_return", RETURN_ARGS))
        self.assertEqual((resubmitted.status, resubmitted.pending_action_id), (ActionStatus.REJECTED, pending_id))
        self.assertEqual(self.db.count("pending_actions"), 1)
        self.assert_not_executed()


# --------------------------------------------------------------------------
# Capture consistency: P-12, P-14, P-15, P-17
# --------------------------------------------------------------------------


class CaptureConsistencyTests(ApprovalCase):
    def approved(self):
        pending_id = self.start_return()
        self.gateway().record_decision(approval(pending_id))
        return pending_id

    def test_p12_clock_once_per_transaction(self):
        for label, prepare, call in (
                ("T1", lambda: self.start_return("req-t1"),
                 lambda gw, pid: gw.record_decision(approval(pid))),
                ("T2 execute", self.approved, lambda gw, pid: gw.execute_approved(pid)),
                ("T2 replay", lambda: (lambda p: (self.gateway().execute_approved(p), p)[1])(self.approved()),
                 lambda gw, pid: gw.execute_approved(pid)),
                ("get_outcome", lambda: self.start_return("req-g"), lambda gw, pid: gw.get_outcome(pid))):
            with self.subTest(transaction=label):
                with Stage6Database() as db:
                    self.db, saved = db, self.db
                    try:
                        pid = prepare()
                        clock = CountingClock()
                        call(self.gateway(clock=clock), pid)
                        self.assertEqual(clock.calls, 0 if label == "get_outcome" else 1)
                    finally:
                        self.db = saved

    def test_p14_one_catalog_snapshot_per_resume(self):
        pending_id = self.approved()

        class SecondCallRepublishes(CountingCatalog):
            def snapshot(inner):
                snapshot = super().snapshot()
                return snapshot if inner.calls == 1 else dataclasses.replace(snapshot, build_id="build-0002")

        catalog = SecondCallRepublishes()
        outcome = self.gateway(catalog=catalog).execute_approved(pending_id)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(catalog.calls, 1)

    def test_p15_comparison_and_decision_use_the_same_capture(self):
        pending_id = self.approved()
        seen = {}
        real_capture, real_decide, real_stale = Guard.capture, Guard.decide, stale_reason

        def capture(self_, *args, **kwargs):
            seen["capture"] = real_capture(self_, *args, **kwargs)
            return seen["capture"]

        def compare(stored, candidate):
            seen["compared"] = candidate
            return real_stale(stored, candidate)

        def decide(action, state, policy, risk, txn_now):
            seen["decide"] = (state, policy, txn_now)
            return real_decide(action, state, policy, risk, txn_now)

        catalog = CountingCatalog()
        with mock.patch.object(Guard, "capture", capture), \
                mock.patch.object(Guard, "decide", staticmethod(decide)), \
                mock.patch.object(gateway_module, "stale_reason", compare):
            outcome = self.gateway(catalog=catalog).execute_approved(pending_id)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        captured = seen["capture"]
        self.assertIs(seen["compared"], captured.candidate_snapshot)
        state, policy, txn_now = seen["decide"]
        self.assertIs(state, captured.state)
        self.assertIs(policy, captured.policy)
        self.assertIs(policy, catalog.snapshot())  # the one CatalogSnapshot acquired
        self.assertEqual(txn_now, captured.txn_now)

    def test_p17_nothing_is_read_after_the_comparison(self):
        pending_id = self.approved()
        events = []
        real_connect = sqlite3.connect
        real_capture, real_decide = Guard.capture, Guard.decide

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(lambda sql: events.append(("sql", sql.strip().split(" ", 1)[0].upper())))
            return connection

        def capture(self_, *args, **kwargs):
            result = real_capture(self_, *args, **kwargs)
            events.append(("capture_done", None))
            return result

        def decide(*args, **kwargs):
            events.append(("decide", None))
            return real_decide(*args, **kwargs)

        class MarkingClock(CountingClock):
            def now(inner):
                events.append(("clock", None))
                return super().now()

        class MarkingCatalog(CountingCatalog):
            def snapshot(inner):
                events.append(("catalog", None))
                return super().snapshot()

        with mock.patch("sqlite3.connect", traced_connect), \
                mock.patch.object(Guard, "capture", capture), \
                mock.patch.object(Guard, "decide", staticmethod(decide)):
            outcome = self.gateway(clock=MarkingClock(), catalog=MarkingCatalog()).execute_approved(pending_id)
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds.count("clock"), 1)
        self.assertEqual(kinds.count("catalog"), 1)
        done, decided = kinds.index("capture_done"), kinds.index("decide")
        self.assertLess(done, decided)
        self.assertEqual(events[done + 1:decided], [])  # the comparison happens here, reading nothing
        after = events[decided + 1:]
        self.assertNotIn(("clock", None), after)
        self.assertNotIn(("catalog", None), after)
        statements = [verb for kind, verb in after if kind == "sql"]
        self.assertTrue(statements)
        self.assertEqual(statements[-1], "COMMIT")
        self.assertTrue(set(statements) <= {"INSERT", "UPDATE", "COMMIT"}, statements)
        self.assertGreaterEqual(statements.count("UPDATE"), 1)


# --------------------------------------------------------------------------
# P-9 and the cross-process restart
# --------------------------------------------------------------------------


class ResumeIndependenceTests(unittest.TestCase):
    RESUME_MODULES = ("action_gateway.py", "action_store.py", "approval.py", "guard.py",
                      "guard_snapshot.py", "guard_state.py", "actions.py", "action_db.py",
                      "action_outcome.py", "action_policy.py", "capabilities.py", "ids.py",
                      "action_errors.py")

    def test_p9_resume_modules_import_no_model_conversation_or_policy_code(self):
        banned = ("eval_v2", "llm_provider", "orchestration.planner", "rag", "agent", "openai", "requests")
        for name in self.RESUME_MODULES:
            tree = ast.parse((REPO_ROOT / "aftersales" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    with self.subTest(module=name, imports=module):
                        self.assertFalse(any(module == b or module.startswith(b + ".") for b in banned))

    def test_restart_across_processes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            base = Path(tmp)
            db_path, handoff = base / "stage6.db", base / "pending.txt"
            result = base / "result.json"
            common = f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from datetime import datetime
                from pathlib import Path
                from aftersales.action_gateway import ActionGateway
                from aftersales.capabilities import CapabilityGate
                from aftersales.clock import BUSINESS_TIMEZONE, FixedClock
                from aftersales.ids import DeterministicIdProvider
                from aftersales.policy_catalog import PublishedPolicyCatalog
                gateway = ActionGateway({str(db_path)!r},
                    clock=FixedClock(datetime(2026, 11, 15, 10, tzinfo=BUSINESS_TIMEZONE)),
                    id_provider=DeterministicIdProvider("eval"), catalog=PublishedPolicyCatalog(),
                    capabilities=CapabilityGate().narrow(), formal=True)
            """
            process_a = textwrap.dedent(common) + textwrap.dedent(f"""
                from aftersales.action_db import create_stage6_database
                from aftersales.actions import ActionIntentValidator, build_action_registry
                from aftersales.approval import ApprovalDecision
                from aftersales.ids import RequestIdentity
                create_stage6_database({str(db_path)!r})
                action = ActionIntentValidator(build_action_registry(), CapabilityGate().narrow().actions).validate(
                    "create_return", {{"order_id": "ORD-1001", "order_item_id": "OI-1001-2",
                                       "reason_code": "no_longer_wanted"}})
                waiting = gateway.start_action(RequestIdentity(persona_id="demo-a", request_id="req-1"), action)
                assert waiting.status.value == "WAITING_APPROVAL", waiting
                recorded = gateway.record_decision(ApprovalDecision(
                    pending_action_id=waiting.pending_action_id, decision="APPROVE",
                    approver_ref="op-demo-1", decided_at="2026-11-15T10:00:00+08:00"))
                assert recorded.approval_recorded, recorded
                Path({str(handoff)!r}).write_text(waiting.pending_action_id, encoding="utf-8")
            """)
            process_b = textwrap.dedent(common) + textwrap.dedent(f"""
                import json, os, socket
                pending_id = Path({str(handoff)!r}).read_text(encoding="utf-8")
                root = os.path.normcase({str(REPO_ROOT)!r})
                watched = tuple(os.path.join(root, part) for part in (
                    "llm_provider.py", os.path.join("orchestration", "planner.py"), "eval_v2"))
                control = os.path.join(root, "aftersales", "guard.py")
                called, guard_ran = set(), []

                def profiler(frame, event, arg):
                    if event == "call":
                        filename = os.path.normcase(frame.f_code.co_filename)
                        if filename.startswith(watched):
                            called.add(filename)
                        elif filename == control:
                            guard_ran.append(frame.f_code.co_name)

                def no_network(*args, **kwargs):
                    raise RuntimeError("resume must not touch the network")

                socket.socket.connect = no_network
                socket.create_connection = no_network
                sys.setprofile(profiler)
                try:
                    outcome = gateway.execute_approved(pending_id)
                finally:
                    sys.setprofile(None)
                Path({str(result)!r}).write_text(json.dumps({{"outcome": outcome.to_dict(),
                    "pending_id": pending_id, "called": sorted(called), "guard_ran": sorted(set(guard_ran)),
                    "eval_modules": sorted(m for m in sys.modules if m.startswith("eval_v2"))}}),
                    encoding="utf-8")
            """)
            for label, source in (("A", process_a), ("B", process_b)):
                script = base / ("process_" + label + ".py")
                script.write_text(source, encoding="utf-8")
                completed = subprocess.run([sys.executable, str(script)], cwd=str(base),
                                           capture_output=True, timeout=180)
                self.assertEqual(completed.returncode, 0,
                                 label + ": " + completed.stderr.decode("utf-8", "replace"))
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["outcome"]["status"], "EXECUTED")
            self.assertEqual(payload["outcome"]["pending_action_id"], payload["pending_id"])
            # No model, planner or eval code ran during the resume, and no eval module was loaded.
            # (llm_provider / orchestration.planner are imported, unused, by orchestration/__init__.)
            self.assertEqual(payload["called"], [])
            self.assertIn("capture", payload["guard_ran"])  # the profiler does see repository code
            self.assertIn("decide", payload["guard_ran"])
            self.assertEqual(payload["eval_modules"], [])
            connection = sqlite3.connect(str(db_path))
            try:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM after_sales_cases WHERE case_id LIKE 'AS6-%' AND type = 'return'"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM action_receipts").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT status, receipt_id FROM pending_actions").fetchall(),
                                 [("EXECUTED", payload["outcome"]["receipt"]["receipt_id"])])
            finally:
                connection.close()


class RendererTests(unittest.TestCase):
    def test_approval_path_renderings(self):
        renderer = ActionOutcomeRenderer()
        base = dict(action_name="create_return", request_id="req-1", pending_action_id="PA-0123456789ABCDEF")
        stale = renderer.render(ActionOutcome(status=ActionStatus.STALE, code="record_version_changed", **base))
        self.assertIn("没有执行", stale)
        rejected = renderer.render(ActionOutcome(status=ActionStatus.REJECTED, code="approval_rejected", **base))
        for text in (stale, rejected):
            for claim in FORBIDDEN_CLAIMS + COMPLETION_CLAIM_MARKERS:
                self.assertNotIn(claim, text)
        with self.assertRaises(ValueError):
            ActionOutcome(status=ActionStatus.WAITING_APPROVAL, action_name="create_return", request_id="r")
        with self.assertRaises(ValueError):
            ActionOutcome(status=ActionStatus.DENIED, action_name=None, request_id=None,
                          pending_action_id="PA-0123456789ABCDEF", code="return_window_closed")


if __name__ == "__main__":
    unittest.main()
