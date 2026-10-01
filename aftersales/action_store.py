"""The narrow Stage 6 persistence layer (docs/v2/stage6-design.md §7, §9, §11, §17).

Fixed SQL on the ActionGateway's connection, inside the gateway's transaction:
idempotency replay lookup, the declared business inserts, the receipt insert,
audit appends, and the pending-action records of the approval path (insert,
load, record a decision, move to a terminal state, link the receipt). It makes
no decision and takes no user or model text: every value it writes is a
validated argument, a closed code, a server-owned id, a canonical digest, a
trusted operator decision or the transaction's txn_now.

Every pending UPDATE names its expected status AND expected version, and must
hit exactly one row; there is no unconditional pending UPDATE. Each method
allows only the transitions of §11.2, and the table's CHECK constraints
enforce the same correspondences in the database. It never updates or deletes
a business row, and it never touches inventory, orders, logistics, payments or
refunds.
"""

from __future__ import annotations

from dataclasses import dataclass

from .action_errors import FAILURE_CODES, PendingTransitionConflict
from .actions import (
    ACTION_NAMES,
    REASON_LABELS,
    RESOURCE_AFTER_SALES_CASE,
    RESOURCE_HANDOFF_TICKET,
    ValidatedAction,
)
from .approval import APPROVAL_DECISIONS, APPROVE, APPROVER_REF_PATTERN
from .guard import DENY_REASON_CODES, GUARD_REASON_CODES, GuardDecisionKind
from .guard_snapshot import STALE_REASON_CODES
from .ids import ID_PATTERN, IDEMPOTENCY_KEY_PATTERN, RequestIdentity
from .schema import CaseStatus, CaseType

AUDIT_ACTION_REPLAY_HIT = "action.replay_hit"
AUDIT_GUARD_EVALUATED = "guard.evaluated"
AUDIT_GUARD_FAILED = "guard.failed"
AUDIT_PENDING_CREATED = "action.pending_created"
AUDIT_APPROVAL_RECORDED = "approval.recorded"
AUDIT_APPROVAL_CONFLICT = "approval.conflict"
AUDIT_RESUME_STARTED = "resume.started"
AUDIT_RESUME_VERSION_CHECK = "resume.version_check"
AUDIT_ACTION_EXECUTED = "action.executed"
AUDIT_ACTION_NOT_EXECUTED = "action.not_executed"
AUDIT_TRANSACTION_ROLLED_BACK = "transaction.rolled_back"

AUDIT_EVENT_NAMES = frozenset({
    AUDIT_ACTION_REPLAY_HIT, AUDIT_GUARD_EVALUATED, AUDIT_GUARD_FAILED, AUDIT_PENDING_CREATED,
    AUDIT_APPROVAL_RECORDED, AUDIT_APPROVAL_CONFLICT, AUDIT_RESUME_STARTED,
    AUDIT_RESUME_VERSION_CHECK, AUDIT_ACTION_EXECUTED, AUDIT_ACTION_NOT_EXECUTED,
    AUDIT_TRANSACTION_ROLLED_BACK,
})

PHASE_START = "start"
PHASE_RESUME = "resume"

ANCHOR_RECEIPT = "receipt"
ANCHOR_PENDING = "pending"

VERSION_CHECK_MATCH = "MATCH"
VERSION_CHECK_MISMATCH = "MISMATCH"

APPROVAL_REJECTED = "approval_rejected"

# Pending statuses (§11.1).
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
EXECUTED = "EXECUTED"
STALE = "STALE"
DENIED = "DENIED"
FAILED = "FAILED"
PENDING_STATUSES = (PENDING_APPROVAL, APPROVED, REJECTED, EXECUTED, STALE, DENIED, FAILED)
TERMINAL_STATUSES = frozenset({REJECTED, EXECUTED, STALE, DENIED, FAILED})
# APPROVED -> one of these through transition_terminal (P4, P5, P6); EXECUTED only via mark_executed.
_TERMINAL_CODES = {
    STALE: frozenset(STALE_REASON_CODES),
    DENIED: frozenset(DENY_REASON_CODES),
    FAILED: FAILURE_CODES,
}

# Every value an audit row's `decision` / `code` may hold: closed vocabularies only.
AUDIT_DECISION_VALUES = (
    frozenset(item.value for item in GuardDecisionKind)
    | frozenset({"EXECUTED", "WAITING_APPROVAL", "DENIED", "REJECTED", "STALE", "FAILED"})
    | frozenset(APPROVAL_DECISIONS) | frozenset({VERSION_CHECK_MATCH, VERSION_CHECK_MISMATCH})
)
AUDIT_CODE_VALUES = (
    GUARD_REASON_CODES | FAILURE_CODES | frozenset(STALE_REASON_CODES)
    | frozenset({APPROVAL_REJECTED, ANCHOR_RECEIPT, ANCHOR_PENDING,
                 RESOURCE_AFTER_SALES_CASE, RESOURCE_HANDOFF_TICKET})
)

RECEIPT_RESULT_EXECUTED = "EXECUTED"
OPEN_STATUS = CaseStatus.PENDING.value  # 待处理, for new cases and tickets alike

AUDIT_COLUMNS = (
    "event_name", "request_id", "persona_id", "action_name", "idempotency_key",
    "pending_action_id", "receipt_id", "phase", "decision", "code", "approver_ref", "at",
)
PENDING_COLUMNS = (
    "pending_action_id", "idempotency_key", "request_id", "persona_id", "action_name",
    "args_json", "args_sha256", "target_order_id", "target_order_item_id", "status",
    "guard_decision", "guard_reason_code", "snapshot_json", "snapshot_sha256",
    "action_spec_version", "risk_policy_version", "policy_build_id", "approval_decision",
    "approver_ref", "decided_at", "outcome_code", "receipt_id", "created_at", "updated_at",
    "version",
)
_RECEIPT_COLUMNS = (
    "receipt_id, action_name, resource_type, resource_id, guard_decision,"
    " guard_reason_code, pending_action_id"
)

_SELECT_RECEIPT = "SELECT " + _RECEIPT_COLUMNS + " FROM action_receipts WHERE idempotency_key = ?"
_SELECT_RECEIPT_BY_PENDING = (
    "SELECT " + _RECEIPT_COLUMNS + " FROM action_receipts WHERE pending_action_id = ?")
_SELECT_PENDING_BY_KEY = (
    "SELECT " + ", ".join(PENDING_COLUMNS) + " FROM pending_actions WHERE idempotency_key = ?")
_SELECT_PENDING_BY_ID = (
    "SELECT " + ", ".join(PENDING_COLUMNS) + " FROM pending_actions WHERE pending_action_id = ?")
_INSERT_CASE = (
    "INSERT INTO after_sales_cases (case_id, order_id, order_item_id, customer_id, type,"
    " status, reason, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
)
_INSERT_TICKET = (
    "INSERT INTO human_handoff_tickets (ticket_id, order_id, order_item_id, handoff_trigger,"
    " status, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, 1)"
)
_INSERT_RECEIPT = (
    "INSERT INTO action_receipts (receipt_id, idempotency_key, request_id, persona_id,"
    " action_name, args_json, args_sha256, result_status, resource_type, resource_id,"
    " pending_action_id, guard_decision, guard_reason_code, snapshot_json, snapshot_sha256,"
    " action_spec_version, risk_policy_version, policy_build_id, executed_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_PENDING = (
    "INSERT INTO pending_actions (pending_action_id, idempotency_key, request_id, persona_id,"
    " action_name, args_json, args_sha256, target_order_id, target_order_item_id, status,"
    " guard_decision, guard_reason_code, snapshot_json, snapshot_sha256, action_spec_version,"
    " risk_policy_version, policy_build_id, approval_decision, approver_ref, decided_at,"
    " outcome_code, receipt_id, created_at, updated_at, version)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_APPROVAL', 'REQUIRE_APPROVAL', ?, ?, ?, ?, ?, ?,"
    " NULL, NULL, NULL, NULL, NULL, ?, ?, 1)"
)
# P1 / P2: PENDING_APPROVAL -> APPROVED | REJECTED.
_UPDATE_RECORD_DECISION = (
    "UPDATE pending_actions SET status = ?, approval_decision = ?, approver_ref = ?,"
    " decided_at = ?, outcome_code = ?, updated_at = ?, version = version + 1"
    " WHERE pending_action_id = ? AND status = 'PENDING_APPROVAL' AND version = ?"
)
# P4 / P5 / P6: APPROVED -> STALE | DENIED | FAILED.
_UPDATE_TERMINAL = (
    "UPDATE pending_actions SET status = ?, outcome_code = ?, updated_at = ?,"
    " version = version + 1 WHERE pending_action_id = ? AND status = 'APPROVED' AND version = ?"
)
# P3: APPROVED -> EXECUTED, linked to its receipt in the same statement.
_UPDATE_EXECUTED = (
    "UPDATE pending_actions SET status = 'EXECUTED', receipt_id = ?, updated_at = ?,"
    " version = version + 1 WHERE pending_action_id = ? AND status = 'APPROVED' AND version = ?"
)
_INSERT_AUDIT = (
    "INSERT INTO action_audit_events (" + ", ".join(AUDIT_COLUMNS) + ")"
    " VALUES (" + ", ".join("?" for _ in AUDIT_COLUMNS) + ")"
)


@dataclass(frozen=True, kw_only=True)
class ReceiptRecord:
    receipt_id: str
    action_name: str
    resource_type: str
    resource_id: str
    guard_decision: str
    guard_reason_code: str
    pending_action_id: str | None


@dataclass(frozen=True, kw_only=True)
class PendingRecord:
    """One pending_actions row, every column, as stored."""

    pending_action_id: str
    idempotency_key: str
    request_id: str
    persona_id: str
    action_name: str
    args_json: str
    args_sha256: str
    target_order_id: str
    target_order_item_id: str
    status: str
    guard_decision: str
    guard_reason_code: str
    snapshot_json: str
    snapshot_sha256: str
    action_spec_version: str
    risk_policy_version: str
    policy_build_id: str
    approval_decision: str | None
    approver_ref: str | None
    decided_at: str | None
    outcome_code: str | None
    receipt_id: str | None
    created_at: str
    updated_at: str
    version: int


def _require_id(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValueError("a Stage 6 id does not match the id format")
    return value


def _require_key(value: str) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_PATTERN.match(value):
        raise ValueError("idempotency key does not match the key format")
    return value


def _require_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected version must be a positive integer")
    return value


class ActionStore:
    """Replay lookup, business inserts, receipts, audit, pending records. Nothing else."""

    def __init__(self, connection: object) -> None:
        if connection is None or not callable(getattr(connection, "execute", None)):
            raise ValueError("connection must be an open database connection")
        self._connection = connection

    def _rows(self, sql: str, bindings: tuple) -> list[tuple]:
        cursor = self._connection.cursor()
        try:
            cursor.row_factory = None
            cursor.execute(sql, bindings)
            return cursor.fetchall()
        finally:
            cursor.close()

    def _execute(self, sql: str, bindings: tuple) -> int:
        cursor = self._connection.cursor()
        try:
            cursor.execute(sql, bindings)
            return cursor.rowcount
        finally:
            cursor.close()

    def _guarded(self, sql: str, bindings: tuple) -> None:
        if self._execute(sql, bindings) != 1:
            raise PendingTransitionConflict("a guarded pending update did not hit exactly one row")

    # -- replay lookup (§9.3) -------------------------------------------------

    @staticmethod
    def _receipt(row: tuple) -> ReceiptRecord:
        return ReceiptRecord(receipt_id=row[0], action_name=row[1], resource_type=row[2],
                             resource_id=row[3], guard_decision=row[4],
                             guard_reason_code=row[5], pending_action_id=row[6])

    def find_receipt(self, key: str) -> ReceiptRecord | None:
        rows = self._rows(_SELECT_RECEIPT, (_require_key(key),))
        return self._receipt(rows[0]) if rows else None

    def find_receipt_for_pending(self, pending_action_id: str) -> ReceiptRecord | None:
        rows = self._rows(_SELECT_RECEIPT_BY_PENDING, (_require_id(pending_action_id),))
        return self._receipt(rows[0]) if rows else None

    def find_pending(self, key: str) -> PendingRecord | None:
        rows = self._rows(_SELECT_PENDING_BY_KEY, (_require_key(key),))
        return PendingRecord(**dict(zip(PENDING_COLUMNS, rows[0]))) if rows else None

    def load_pending(self, pending_action_id: str) -> PendingRecord | None:
        rows = self._rows(_SELECT_PENDING_BY_ID, (_require_id(pending_action_id),))
        return PendingRecord(**dict(zip(PENDING_COLUMNS, rows[0]))) if rows else None

    # -- business inserts (§2) ------------------------------------------------

    def insert_after_sales_case(self, *, case_id: str, action: ValidatedAction, customer_id: str,
                                case_type: str, at: str) -> None:
        if case_type not in (CaseType.RETURN.value, CaseType.EXCHANGE.value):
            raise ValueError("case_type must be return or exchange")
        reason = REASON_LABELS[action.args["reason_code"]]
        count = self._execute(_INSERT_CASE, (
            _require_id(case_id), action.target_order_id, action.target_order_item_id,
            customer_id, case_type, OPEN_STATUS, reason, at, at,
        ))
        if count != 1:
            raise RuntimeError("after-sales case insert did not write exactly one row")

    def insert_handoff_ticket(self, *, ticket_id: str, action: ValidatedAction, at: str) -> None:
        count = self._execute(_INSERT_TICKET, (
            _require_id(ticket_id), action.target_order_id, action.target_order_item_id,
            action.args["handoff_trigger"], OPEN_STATUS, at, at,
        ))
        if count != 1:
            raise RuntimeError("handoff ticket insert did not write exactly one row")

    # -- receipt (§7.2) -------------------------------------------------------

    def insert_receipt(self, *, receipt_id: str, key: str, identity: RequestIdentity,
                       action: ValidatedAction, resource_type: str, resource_id: str,
                       pending_action_id: str | None, guard_decision: str,
                       guard_reason_code: str, snapshot_json: str, snapshot_sha256: str,
                       action_spec_version: str, risk_policy_version: str,
                       policy_build_id: str, executed_at: str) -> None:
        if pending_action_id is not None:
            _require_id(pending_action_id)
        count = self._execute(_INSERT_RECEIPT, (
            _require_id(receipt_id), _require_key(key), identity.request_id, identity.persona_id,
            action.action_name, action.canonical_args_json, action.args_sha256,
            RECEIPT_RESULT_EXECUTED, resource_type, _require_id(resource_id), pending_action_id,
            guard_decision, guard_reason_code, snapshot_json, snapshot_sha256,
            action_spec_version, risk_policy_version, policy_build_id, executed_at,
        ))
        if count != 1:
            raise RuntimeError("receipt insert did not write exactly one row")

    # -- pending actions (§7.2, §11.2) -----------------------------------------

    def insert_pending(self, *, pending_action_id: str, key: str, identity: RequestIdentity,
                       action: ValidatedAction, guard_reason_code: str, snapshot_json: str,
                       snapshot_sha256: str, action_spec_version: str, risk_policy_version: str,
                       policy_build_id: str, at: str) -> None:
        """P0: a new PENDING_APPROVAL row, version 1, no approval fields, no receipt."""
        count = self._execute(_INSERT_PENDING, (
            _require_id(pending_action_id), _require_key(key), identity.request_id,
            identity.persona_id, action.action_name, action.canonical_args_json,
            action.args_sha256, action.target_order_id, action.target_order_item_id,
            guard_reason_code, snapshot_json, snapshot_sha256, action_spec_version,
            risk_policy_version, policy_build_id, at, at,
        ))
        if count != 1:
            raise RuntimeError("pending insert did not write exactly one row")

    def record_decision(self, *, pending_action_id: str, expected_version: int, decision: str,
                        approver_ref: str, decided_at: str, at: str) -> None:
        """P1 / P2: PENDING_APPROVAL -> APPROVED (APPROVE) or REJECTED (REJECT)."""
        if decision not in APPROVAL_DECISIONS:
            raise ValueError("decision must be APPROVE or REJECT")
        if not isinstance(approver_ref, str) or not APPROVER_REF_PATTERN.match(approver_ref):
            raise ValueError("approver_ref does not match the operator id format")
        status, outcome = (APPROVED, None) if decision == APPROVE else (REJECTED, APPROVAL_REJECTED)
        self._guarded(_UPDATE_RECORD_DECISION, (
            status, decision, approver_ref, decided_at, outcome, at,
            _require_id(pending_action_id), _require_version(expected_version),
        ))

    def transition_terminal(self, *, pending_action_id: str, expected_version: int,
                            status: str, outcome_code: str, at: str) -> None:
        """P4 / P5 / P6: APPROVED -> STALE, DENIED or FAILED, with its closed code."""
        if status not in _TERMINAL_CODES:
            raise ValueError("transition_terminal only reaches STALE, DENIED or FAILED")
        if outcome_code not in _TERMINAL_CODES[status]:
            raise ValueError("outcome_code does not belong to the terminal status")
        self._guarded(_UPDATE_TERMINAL, (
            status, outcome_code, at, _require_id(pending_action_id),
            _require_version(expected_version),
        ))

    def mark_executed(self, *, pending_action_id: str, expected_version: int, receipt_id: str,
                      at: str) -> None:
        """P3: APPROVED -> EXECUTED, linked to the receipt written in this transaction."""
        self._guarded(_UPDATE_EXECUTED, (
            _require_id(receipt_id), at, _require_id(pending_action_id),
            _require_version(expected_version),
        ))

    # -- audit (§17) ------------------------------------------------------------

    def append_audit(self, *, event_name: str, identity: RequestIdentity, action_name: str,
                     at: str, idempotency_key: str | None = None,
                     pending_action_id: str | None = None, receipt_id: str | None = None,
                     phase: str | None = None, decision: str | None = None,
                     code: str | None = None, approver_ref: str | None = None) -> None:
        """Names, codes, ids and business time only. Anything else is refused."""
        if event_name not in AUDIT_EVENT_NAMES:
            raise ValueError("unknown audit event name")
        if action_name not in ACTION_NAMES:
            raise ValueError("audit action_name is not a Stage 6 action")
        if phase not in (None, PHASE_START, PHASE_RESUME):
            raise ValueError("audit phase is not start or resume")
        if decision is not None and decision not in AUDIT_DECISION_VALUES:
            raise ValueError("audit decision is not a closed decision value")
        if code is not None and code not in AUDIT_CODE_VALUES:
            raise ValueError("audit code is not a closed code")
        if approver_ref is not None and not APPROVER_REF_PATTERN.match(approver_ref):
            raise ValueError("audit approver_ref is not an operator id")
        if idempotency_key is not None:
            _require_key(idempotency_key)
        for value in (pending_action_id, receipt_id):
            if value is not None:
                _require_id(value)
        count = self._execute(_INSERT_AUDIT, (
            event_name, identity.request_id, identity.persona_id, action_name, idempotency_key,
            pending_action_id, receipt_id, phase, decision, code, approver_ref, at,
        ))
        if count != 1:
            raise RuntimeError("audit insert did not write exactly one row")

