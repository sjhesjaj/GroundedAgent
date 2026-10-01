"""The narrow Stage 6 persistence layer (docs/v2/stage6-design.md §7, §9, §17).

Fixed SQL for exactly four jobs, on the ActionGateway's connection inside the
gateway's transaction: idempotency replay lookup, the declared business
inserts, the receipt insert, and audit appends. It makes no decision and takes
no user or model text: every value it writes is a validated argument, a closed
code, a server-owned id, a canonical digest or the transaction's txn_now.

It never updates or deletes a business row, and it never touches inventory,
orders, logistics, payments or refunds.
"""

from __future__ import annotations

from dataclasses import dataclass

from .action_errors import FAILURE_CODES
from .actions import (
    ACTION_NAMES,
    REASON_LABELS,
    RESOURCE_AFTER_SALES_CASE,
    RESOURCE_HANDOFF_TICKET,
    ValidatedAction,
)
from .guard import GUARD_REASON_CODES, GuardDecisionKind
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

# Every value an audit row's `decision` / `code` may hold: closed vocabularies only.
AUDIT_DECISION_VALUES = frozenset(item.value for item in GuardDecisionKind) | frozenset({
    "EXECUTED", "WAITING_APPROVAL", "DENIED", "REJECTED", "STALE", "FAILED",
})
AUDIT_CODE_VALUES = (
    GUARD_REASON_CODES | FAILURE_CODES
    | frozenset({ANCHOR_RECEIPT, ANCHOR_PENDING, RESOURCE_AFTER_SALES_CASE, RESOURCE_HANDOFF_TICKET})
)

RECEIPT_RESULT_EXECUTED = "EXECUTED"
OPEN_STATUS = CaseStatus.PENDING.value  # 待处理, for new cases and tickets alike

AUDIT_COLUMNS = (
    "event_name", "request_id", "persona_id", "action_name", "idempotency_key",
    "pending_action_id", "receipt_id", "phase", "decision", "code", "approver_ref", "at",
)

_SELECT_RECEIPT = (
    "SELECT receipt_id, action_name, resource_type, resource_id, guard_decision,"
    " guard_reason_code, pending_action_id FROM action_receipts WHERE idempotency_key = ?"
)
_SELECT_PENDING = (
    "SELECT pending_action_id, action_name, status, guard_reason_code, outcome_code, receipt_id"
    " FROM pending_actions WHERE idempotency_key = ?"
)
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
    pending_action_id: str
    action_name: str
    status: str
    guard_reason_code: str
    outcome_code: str | None
    receipt_id: str | None


def _require_id(value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValueError("a Stage 6 id does not match the id format")
    return value


def _require_key(value: str) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_PATTERN.match(value):
        raise ValueError("idempotency key does not match the key format")
    return value


class ActionStore:
    """Replay lookup, business inserts, receipt insert, audit append. Nothing else."""

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

    # -- replay lookup (§9.3) -------------------------------------------------

    def find_receipt(self, key: str) -> ReceiptRecord | None:
        rows = self._rows(_SELECT_RECEIPT, (_require_key(key),))
        if not rows:
            return None
        row = rows[0]
        return ReceiptRecord(receipt_id=row[0], action_name=row[1], resource_type=row[2],
                             resource_id=row[3], guard_decision=row[4],
                             guard_reason_code=row[5], pending_action_id=row[6])

    def find_pending(self, key: str) -> PendingRecord | None:
        rows = self._rows(_SELECT_PENDING, (_require_key(key),))
        if not rows:
            return None
        row = rows[0]
        return PendingRecord(pending_action_id=row[0], action_name=row[1], status=row[2],
                             guard_reason_code=row[3], outcome_code=row[4], receipt_id=row[5])

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
        count = self._execute(_INSERT_RECEIPT, (
            _require_id(receipt_id), _require_key(key), identity.request_id, identity.persona_id,
            action.action_name, action.canonical_args_json, action.args_sha256,
            RECEIPT_RESULT_EXECUTED, resource_type, _require_id(resource_id), pending_action_id,
            guard_decision, guard_reason_code, snapshot_json, snapshot_sha256,
            action_spec_version, risk_policy_version, policy_build_id, executed_at,
        ))
        if count != 1:
            raise RuntimeError("receipt insert did not write exactly one row")

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
