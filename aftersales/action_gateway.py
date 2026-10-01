"""The ActionGateway: the only component that writes (docs/v2/stage6-design.md §10, §11, §13).

start_action - one transaction, one business instant, one capture (§10.2):

    outside the transaction (pure): the action is in the effective capability
        set, re-validated against the closed contract; the persona resolves;
        the server-owned idempotency key is computed
    BEGIN IMMEDIATE                       (fails -> FAILED transaction_failed,
                                           no Clock read, nothing written)
    txn_now = clock.now()                 exactly once for this transaction
    replay lookup by idempotency key      hit -> audit, COMMIT, stored outcome
    capture = Guard.capture(...)          one reader pass, one catalog.snapshot()
    decision = Guard.decide(...)          pure; same capture, same txn_now
    ALLOW            -> audit, business row, receipt, audit           -> EXECUTED
    REQUIRE_APPROVAL -> audit, pending_actions (P0), audit           -> WAITING_APPROVAL
    DENY             -> audit only                                     -> DENIED
    COMMIT

record_decision (T1) - the trusted operator's decision, first decision wins:

    BEGIN IMMEDIATE -> txn_now once -> load pending
    PENDING_APPROVAL + APPROVE -> APPROVED (P1); + REJECT -> REJECTED (P2)
    any other state: the same decision is a replay, the opposite one a
    decision_conflict; neither changes the pending row
    COMMIT

execute_approved (T2) - restart-safe, from the database alone (§10.3):

    BEGIN IMMEDIATE -> txn_now once -> load pending (PENDING_APPROVAL ->
    NotApproved; terminal -> replay) -> no receipt may exist yet ->
    re-resolve the persona -> rebuild the ValidatedAction (args digest and
    idempotency key must recompute exactly) -> ONE Guard.capture excluding this
    pending -> compare the STORED snapshot with THIS capture's candidate
    (mismatch -> STALE, decide is never called) -> Guard.decide on the SAME
    capture and txn_now -> DENY -> DENIED; ALLOW or another reason -> STALE
    guard_decision_changed; the same REQUIRE_APPROVAL -> business row, receipt,
    pending EXECUTED -> COMMIT

    Nothing is read after the capture. An approval is never a permanent pass.

A failure after BEGIN rolls everything back. A compensating transaction then
records it, reusing txn_now (it never reads the Clock): for start_action an
audit trail only; for T2, once APPROVED was confirmed, also APPROVED -> FAILED
(P6). If the compensation itself fails the pending stays APPROVED, and a later
execute_approved repeats every check. No outcome ever says EXECUTED unless its
receipt committed.

The Guard is handed a TrustedExecutionContext whose clock is FixedClock(txn_now),
so the injected Clock is unreachable from the Guard by construction.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .action_db import DEFAULT_BUSY_TIMEOUT_MS, connect_writer
from .action_errors import (
    FAILURE_ID_GENERATION,
    FAILURE_IDENTITY_UNRESOLVABLE,
    FAILURE_INVARIANT,
    FAILURE_STATE_READ,
    FAILURE_TRANSACTION,
    FAILURE_WRITE,
    ActionCapabilityError,
    ActionContractError,
    ActionValidationError,
    ApprovalInputError,
    GuardFailure,
    NotApproved,
    PendingTransitionConflict,
    SnapshotIntegrityError,
    UnknownPendingAction,
)
from .action_outcome import ActionOutcome, ActionStatus, GuardView, ReceiptView
from .action_policy import S6_RISK_POLICY, ActionRiskPolicy
from .action_store import (
    ANCHOR_PENDING,
    ANCHOR_RECEIPT,
    APPROVAL_REJECTED,
    APPROVED,
    AUDIT_ACTION_EXECUTED,
    AUDIT_ACTION_NOT_EXECUTED,
    AUDIT_ACTION_REPLAY_HIT,
    AUDIT_APPROVAL_CONFLICT,
    AUDIT_APPROVAL_RECORDED,
    AUDIT_GUARD_EVALUATED,
    AUDIT_GUARD_FAILED,
    AUDIT_PENDING_CREATED,
    AUDIT_RESUME_STARTED,
    AUDIT_RESUME_VERSION_CHECK,
    AUDIT_TRANSACTION_ROLLED_BACK,
    DENIED,
    EXECUTED,
    FAILED,
    PENDING_APPROVAL,
    PHASE_RESUME,
    PHASE_START,
    REJECTED,
    STALE,
    TERMINAL_STATUSES,
    VERSION_CHECK_MATCH,
    VERSION_CHECK_MISMATCH,
    ActionStore,
    PendingRecord,
    ReceiptRecord,
)
from .actions import (
    ACTION_SPEC_VERSION,
    CREATE_EXCHANGE,
    CREATE_RETURN,
    ESCALATE_TO_HUMAN,
    RESOURCE_AFTER_SALES_CASE,
    RESOURCE_HANDOFF_TICKET,
    ActionIntentValidator,
    ActionRegistry,
    ValidatedAction,
    build_action_registry,
)
from .approval import (
    APPROVE,
    STAGE6_TRUSTED_OPERATORS,
    ApprovalDecision,
    require_pending_action_id,
    require_trusted_operator,
)
from .capabilities import EffectiveCapabilities
from .clock import Clock, FixedClock, require_aware
from .context import Persona, TrustedExecutionContext
from .demo import resolve_persona
from .guard import Guard, GuardCapture, GuardDecision, GuardDecisionKind, snapshot_document, snapshot_sha256
from .guard_snapshot import STALE_GUARD_DECISION_CHANGED, parse_snapshot_document, stale_reason
from .ids import DeterministicIdProvider, IdKind, IdProvider, RequestIdentity, idempotency_key
from .schema import CaseType

# Deterministic fault points, used by tests to exercise rollback and compensation
# paths. The formal Stage 6 eval fault declarations (action_faults) are a Stage 6.4 concern.
FAULT_BUSINESS_WRITE = "business_write"
FAULT_RECEIPT_WRITE = "receipt_write"
FAULT_COMMIT = "commit"
FAULT_COMPENSATION = "compensation"
FAULT_POINTS = (FAULT_BUSINESS_WRITE, FAULT_RECEIPT_WRITE, FAULT_COMMIT, FAULT_COMPENSATION)


class ActionFaultHooks(Protocol):
    def before(self, point: str) -> None:
        """Called just before a fault point; may raise to simulate a failure."""


class _AttemptFailed(Exception):
    """Internal: an infrastructure failure after BEGIN. Carries a closed code."""

    def __init__(self, code: str, *, from_guard: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.from_guard = from_guard


@dataclasses.dataclass
class _Resume:
    """What T2 has confirmed so far; drives compensation after a rollback."""

    pending_action_id: str
    pending: PendingRecord | None = None
    identity: RequestIdentity | None = None
    approved: bool = False


_CASE_TYPES = {CREATE_RETURN: CaseType.RETURN.value, CREATE_EXCHANGE: CaseType.EXCHANGE.value}


class ActionGateway:
    """The single side-effect entry point. Holds configuration only, no state."""

    def __init__(self, db_path: str | Path, *, clock: Clock, id_provider: IdProvider,
                 catalog: object, capabilities: EffectiveCapabilities,
                 risk_policy: ActionRiskPolicy = S6_RISK_POLICY,
                 action_registry: ActionRegistry | None = None,
                 persona_resolver: Callable[[str], Persona] = resolve_persona,
                 operators: frozenset[str] = STAGE6_TRUSTED_OPERATORS,
                 formal: bool = False, fault_hooks: ActionFaultHooks | None = None,
                 busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        if not isinstance(clock, Clock):
            raise ValueError("clock must implement Clock")
        if not isinstance(id_provider, IdProvider):
            raise ValueError("id_provider must implement IdProvider")
        if not isinstance(formal, bool):
            raise ValueError("formal must be a bool")
        if formal and type(id_provider) is not DeterministicIdProvider:
            raise ValueError("a formal ActionGateway requires the DeterministicIdProvider")
        if not callable(getattr(catalog, "snapshot", None)):
            raise ValueError("catalog must provide snapshot()")
        if not isinstance(capabilities, EffectiveCapabilities):
            raise ValueError("capabilities must be EffectiveCapabilities from the CapabilityGate")
        if not isinstance(risk_policy, ActionRiskPolicy):
            raise ValueError("risk_policy must be an ActionRiskPolicy")
        if not callable(persona_resolver):
            raise ValueError("persona_resolver must be callable")
        if not isinstance(operators, frozenset) or not all(isinstance(op, str) for op in operators):
            raise ValueError("operators must be a frozenset of trusted operator ids")
        registry = build_action_registry() if action_registry is None else action_registry
        self._db_path = Path(db_path)
        self._clock = clock
        self._ids = id_provider
        self._catalog = catalog
        self._capabilities = capabilities
        self._risk = risk_policy
        self._validator = ActionIntentValidator(registry, capabilities.actions)
        self._guard = Guard(risk_policy)
        self._resolve_persona = persona_resolver
        self._operators = operators
        self._hooks = fault_hooks
        self._busy_timeout_ms = busy_timeout_ms

    # ======================================================================
    # start_action (§10.2)
    # ======================================================================

    def start_action(self, identity: RequestIdentity, action: ValidatedAction) -> ActionOutcome:
        """One action attempt: EXECUTED / WAITING_APPROVAL / DENIED / FAILED, or a replay."""
        # S0 - outside any transaction, pure.
        if not isinstance(identity, RequestIdentity):
            raise ValueError("identity must be a trusted RequestIdentity")
        if not isinstance(action, ValidatedAction):
            raise ValueError("action must be a ValidatedAction")
        if action.action_name not in self._capabilities.actions:
            # Before any transaction: no Guard, no Clock, no write.
            raise ActionCapabilityError("the action is not in this run's effective capabilities")
        try:
            revalidated = self._validator.validate(action.action_name, dict(action.args))
        except ActionValidationError as error:
            raise ActionContractError("the action does not satisfy the closed contract: "
                                      + error.diagnostic) from None
        if revalidated != action:
            raise ActionContractError("the action is not in its validated canonical form")
        persona = self._resolve_persona(identity.persona_id)
        if not isinstance(persona, Persona) or persona.persona_id != identity.persona_id:
            raise ValueError("the persona resolver returned another identity")
        key = idempotency_key(identity, action)

        connection = connect_writer(self._db_path, busy_timeout_ms=self._busy_timeout_ms)
        try:
            # S1
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                return self._failed(action.action_name, identity.request_id, FAILURE_TRANSACTION)
            # S2 - the only Clock read of this transaction.
            txn_now = require_aware("clock.now()", self._clock.now())
            try:
                outcome = self._attempt(connection, identity, persona, action, key, txn_now)
                # S7
                self._commit(connection)
                return outcome
            except _AttemptFailed as failure:
                self._rollback(connection)
                self._compensate_start(connection, identity, action, key, failure, txn_now)
                return self._failed(action.action_name, identity.request_id, failure.code)
            except sqlite3.Error:
                # A database error outside the mapped phases: nothing was committed.
                self._rollback(connection)
                failure = _AttemptFailed(FAILURE_TRANSACTION)
                self._compensate_start(connection, identity, action, key, failure, txn_now)
                return self._failed(action.action_name, identity.request_id, FAILURE_TRANSACTION)
            except BaseException:
                self._rollback(connection)
                raise
        finally:
            connection.close()

    def _attempt(self, connection: sqlite3.Connection, identity: RequestIdentity,
                 persona: Persona, action: ValidatedAction, key: str,
                 txn_now: datetime) -> ActionOutcome:
        store = ActionStore(connection)
        at = txn_now.isoformat()
        common = dict(identity=identity, action_name=action.action_name, at=at,
                      idempotency_key=key)

        # S3 - replay lookup, before the Guard (§9.3).
        try:
            receipt = store.find_receipt(key)
            pending = None if receipt is not None else store.find_pending(key)
        except sqlite3.Error:
            raise _AttemptFailed(FAILURE_STATE_READ) from None
        if receipt is not None or pending is not None:
            with _writes():
                return self._start_replay(store, identity, action, receipt, pending, common)

        # S4, S5 - one capture, one pure decision, same txn_now.
        capture, decision = self._capture_and_decide(connection, persona, action, txn_now,
                                                     exclude_pending_id=None)

        guard_view = GuardView(decision=decision.decision.value, reason_code=decision.reason_code)
        # S6 - writes only from here on; nothing is read again.
        with _writes():
            store.append_audit(event_name=AUDIT_GUARD_EVALUATED, phase=PHASE_START,
                               decision=decision.decision.value, code=decision.reason_code,
                               **common)
            if decision.decision is GuardDecisionKind.DENY:
                store.append_audit(event_name=AUDIT_ACTION_NOT_EXECUTED,
                                   decision=ActionStatus.DENIED.value,
                                   code=decision.reason_code, **common)
                return ActionOutcome(status=ActionStatus.DENIED, action_name=action.action_name,
                                     request_id=identity.request_id, guard=guard_view,
                                     code=decision.reason_code)
            if decision.decision is GuardDecisionKind.REQUIRE_APPROVAL:
                return self._create_pending(store, identity, action, key, capture, decision,
                                            guard_view, at, common)
            receipt_view = self._write_execution(store, identity, persona, action, key, capture,
                                                 decision, at, pending_action_id=None)
            store.append_audit(event_name=AUDIT_ACTION_EXECUTED, receipt_id=receipt_view.receipt_id,
                               code=receipt_view.resource_type, **common)
            return ActionOutcome(status=ActionStatus.EXECUTED, action_name=action.action_name,
                                 request_id=identity.request_id, receipt=receipt_view,
                                 guard=guard_view)

    def _create_pending(self, store: ActionStore, identity: RequestIdentity,
                        action: ValidatedAction, key: str, capture: GuardCapture,
                        decision: GuardDecision, guard_view: GuardView, at: str,
                        common: dict) -> ActionOutcome:
        """P0: ∅ -> PENDING_APPROVAL. No business row, no receipt."""
        pending_action_id = self._new_id(IdKind.PENDING_ACTION, key)
        document = snapshot_document(capture.candidate_snapshot, decision)
        store.insert_pending(
            pending_action_id=pending_action_id, key=key, identity=identity, action=action,
            guard_reason_code=decision.reason_code, snapshot_json=document,
            snapshot_sha256=snapshot_sha256(document), action_spec_version=ACTION_SPEC_VERSION,
            risk_policy_version=self._risk.version, policy_build_id=capture.policy.build_id, at=at)
        store.append_audit(event_name=AUDIT_PENDING_CREATED, pending_action_id=pending_action_id,
                           phase=PHASE_START, **common)
        return ActionOutcome(status=ActionStatus.WAITING_APPROVAL, action_name=action.action_name,
                             request_id=identity.request_id, pending_action_id=pending_action_id,
                             guard=guard_view)

    def _start_replay(self, store: ActionStore, identity: RequestIdentity,
                      action: ValidatedAction, receipt: ReceiptRecord | None,
                      pending: PendingRecord | None, common: dict) -> ActionOutcome:
        """The stored outcome of this very key. The Guard is not run again."""
        if receipt is not None:
            if receipt.action_name != action.action_name:
                raise _AttemptFailed(FAILURE_INVARIANT)
            store.append_audit(event_name=AUDIT_ACTION_REPLAY_HIT, phase=PHASE_START,
                               receipt_id=receipt.receipt_id, code=ANCHOR_RECEIPT, **common)
            return ActionOutcome(
                status=ActionStatus.EXECUTED, action_name=action.action_name,
                request_id=identity.request_id, idempotent_replay=True,
                pending_action_id=receipt.pending_action_id,
                receipt=ReceiptView(receipt_id=receipt.receipt_id,
                                    resource_type=receipt.resource_type,
                                    resource_id=receipt.resource_id),
                guard=GuardView(decision=receipt.guard_decision,
                                reason_code=receipt.guard_reason_code))
        if pending.action_name != action.action_name or pending.status == EXECUTED:
            # An EXECUTED pending always has its receipt, which is looked up first.
            raise _AttemptFailed(FAILURE_INVARIANT)
        store.append_audit(event_name=AUDIT_ACTION_REPLAY_HIT, phase=PHASE_START,
                           pending_action_id=pending.pending_action_id, code=ANCHOR_PENDING,
                           **common)
        return self._pending_outcome(pending, None, replay=True)

    # ======================================================================
    # T1 - record_decision (§10.3)
    # ======================================================================

    def record_decision(self, decision: ApprovalDecision) -> ActionOutcome:
        """The trusted operator's decision. Only this path reaches APPROVED or REJECTED."""
        decision = require_trusted_operator(decision, self._operators)
        connection = connect_writer(self._db_path, busy_timeout_ms=self._busy_timeout_ms)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                return self._failed(None, None, FAILURE_TRANSACTION,
                                    pending_action_id=decision.pending_action_id)
            txn_now = require_aware("clock.now()", self._clock.now())
            context: dict = {}
            try:
                outcome = self._record(connection, decision, txn_now, context)
                self._commit(connection)
                return outcome
            except (UnknownPendingAction, ApprovalInputError):
                self._rollback(connection)
                raise
            except (_AttemptFailed, sqlite3.Error) as error:
                # T1 changed nothing that survives: the pending row is untouched.
                self._rollback(connection)
                code = error.code if isinstance(error, _AttemptFailed) else FAILURE_TRANSACTION
                pending = context.get("pending")
                return self._failed(None if pending is None else pending.action_name,
                                    None if pending is None else pending.request_id, code,
                                    pending_action_id=decision.pending_action_id)
            except BaseException:
                self._rollback(connection)
                raise
        finally:
            connection.close()

    def _record(self, connection: sqlite3.Connection, decision: ApprovalDecision,
                txn_now: datetime, context: dict) -> ActionOutcome:
        store = ActionStore(connection)
        at = txn_now.isoformat()
        try:
            pending = store.load_pending(decision.pending_action_id)
        except sqlite3.Error:
            raise _AttemptFailed(FAILURE_STATE_READ) from None
        if pending is None:
            raise UnknownPendingAction("no pending action has this id")
        context["pending"] = pending
        identity = self._pending_identity(pending)
        common = dict(identity=identity, action_name=pending.action_name, at=at,
                      idempotency_key=pending.idempotency_key,
                      pending_action_id=pending.pending_action_id, phase=PHASE_RESUME)

        if pending.status == PENDING_APPROVAL:
            try:
                created = datetime.fromisoformat(pending.created_at)
            except ValueError:
                raise _AttemptFailed(FAILURE_INVARIANT) from None
            if decision.decided_instant < created:
                raise ApprovalInputError("decided_at is earlier than the pending action")
            with _writes():
                store.record_decision(
                    pending_action_id=pending.pending_action_id, expected_version=pending.version,
                    decision=decision.decision, approver_ref=decision.approver_ref,
                    decided_at=decision.decided_at, at=at)
                store.append_audit(event_name=AUDIT_APPROVAL_RECORDED, decision=decision.decision,
                                   approver_ref=decision.approver_ref, **common)
                if decision.decision == APPROVE:
                    recorded = dataclasses.replace(pending, status=APPROVED, version=pending.version + 1)
                else:
                    store.append_audit(event_name=AUDIT_ACTION_NOT_EXECUTED,
                                       decision=ActionStatus.REJECTED.value, code=APPROVAL_REJECTED,
                                       **common)
                    recorded = dataclasses.replace(pending, status=REJECTED,
                                                   outcome_code=APPROVAL_REJECTED,
                                                   version=pending.version + 1)
            return self._pending_outcome(recorded, None)

        # Any later state: the first recorded decision wins (§11.3).
        receipt = self._receipt_for(store, pending)
        with _writes():
            if decision.decision == pending.approval_decision:
                store.append_audit(event_name=AUDIT_ACTION_REPLAY_HIT, code=ANCHOR_PENDING, **common)
                return self._pending_outcome(pending, receipt, replay=True)
            store.append_audit(event_name=AUDIT_APPROVAL_CONFLICT, decision=decision.decision,
                               approver_ref=decision.approver_ref, **common)
            return self._pending_outcome(pending, receipt, conflict=True)

    # ======================================================================
    # T2 - execute_approved (§10.3)
    # ======================================================================

    def execute_approved(self, pending_action_id: str) -> ActionOutcome:
        """Execute an APPROVED pending action from the database alone, or replay its end."""
        pending_action_id = require_pending_action_id(pending_action_id)
        connection = connect_writer(self._db_path, busy_timeout_ms=self._busy_timeout_ms)
        try:
            # U1
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                return self._failed(None, None, FAILURE_TRANSACTION,
                                    pending_action_id=pending_action_id)
            # U2 - the only Clock read of this transaction.
            txn_now = require_aware("clock.now()", self._clock.now())
            resume = _Resume(pending_action_id=pending_action_id)
            try:
                outcome = self._execute_approved(connection, resume, txn_now)
                self._commit(connection)  # U11
                return outcome
            except (UnknownPendingAction, NotApproved):
                self._rollback(connection)
                raise
            except (_AttemptFailed, sqlite3.Error) as error:
                self._rollback(connection)
                failure = error if isinstance(error, _AttemptFailed) else _AttemptFailed(FAILURE_TRANSACTION)
                if resume.approved:
                    self._compensate_resume(connection, resume, failure, txn_now)
                pending = resume.pending
                return self._failed(None if pending is None else pending.action_name,
                                    None if pending is None else pending.request_id,
                                    failure.code, pending_action_id=pending_action_id)
            except BaseException:
                self._rollback(connection)
                raise
        finally:
            connection.close()

    def _execute_approved(self, connection: sqlite3.Connection, resume: _Resume,
                          txn_now: datetime) -> ActionOutcome:
        store = ActionStore(connection)
        at = txn_now.isoformat()
        # U3 - load the pending row; nothing has changed yet if this fails.
        try:
            pending = store.load_pending(resume.pending_action_id)
        except sqlite3.Error:
            raise _AttemptFailed(FAILURE_STATE_READ) from None
        if pending is None:
            raise UnknownPendingAction("no pending action has this id")
        resume.pending = pending
        if pending.status == PENDING_APPROVAL:
            raise NotApproved("no trusted APPROVE has been recorded for this pending action")
        identity = self._pending_identity(pending)
        common = dict(identity=identity, action_name=pending.action_name, at=at,
                      idempotency_key=pending.idempotency_key,
                      pending_action_id=pending.pending_action_id, phase=PHASE_RESUME)
        if pending.status in TERMINAL_STATUSES:
            receipt = self._receipt_for(store, pending)
            with _writes():
                store.append_audit(event_name=AUDIT_ACTION_REPLAY_HIT, code=ANCHOR_PENDING, **common)
            return self._pending_outcome(pending, receipt, replay=True)
        if pending.status != APPROVED:
            raise _AttemptFailed(FAILURE_INVARIANT)
        try:
            existing = store.find_receipt(pending.idempotency_key)
        except sqlite3.Error:
            raise _AttemptFailed(FAILURE_STATE_READ) from None
        # APPROVED is confirmed: from here on a failure ends in P6 (APPROVED -> FAILED).
        resume.identity = identity
        resume.approved = True
        if existing is not None:
            raise _AttemptFailed(FAILURE_INVARIANT)  # an APPROVED pending cannot have a receipt
        # U4
        with _writes():
            store.append_audit(event_name=AUDIT_RESUME_STARTED, **common)
        # U5 - the trusted identity, from server configuration.
        try:
            persona = self._resolve_persona(pending.persona_id)
        except Exception:
            raise _AttemptFailed(FAILURE_IDENTITY_UNRESOLVABLE) from None
        if not isinstance(persona, Persona) or persona.persona_id != pending.persona_id:
            raise _AttemptFailed(FAILURE_IDENTITY_UNRESOLVABLE)
        # U6 - the stored action, exactly.
        action = self._rebuild(pending, identity)
        # U7 - ONE capture: one reader pass, one catalog snapshot.
        capture = self._capture(connection, persona, action, txn_now,
                                exclude_pending_id=pending.pending_action_id)
        # U8 - the stored snapshot against THIS capture's candidate (pure).
        try:
            stored = parse_snapshot_document(pending.snapshot_json,
                                             expected_sha256=pending.snapshot_sha256,
                                             expected_action=pending.action_name)
            self._require_stored_matches_row(stored, pending)
            reason = stale_reason(stored.snapshot, capture.candidate_snapshot)
        except SnapshotIntegrityError:
            raise _AttemptFailed(FAILURE_INVARIANT) from None
        if reason is not None:
            # decide is never called after a mismatch; nothing executes.
            with _writes():
                store.transition_terminal(pending_action_id=pending.pending_action_id,
                                          expected_version=pending.version, status=STALE,
                                          outcome_code=reason, at=at)
                store.append_audit(event_name=AUDIT_RESUME_VERSION_CHECK,
                                   decision=VERSION_CHECK_MISMATCH, code=reason, **common)
                store.append_audit(event_name=AUDIT_ACTION_NOT_EXECUTED,
                                   decision=ActionStatus.STALE.value, code=reason, **common)
            return self._pending_outcome(dataclasses.replace(
                pending, status=STALE, outcome_code=reason, version=pending.version + 1), None)
        # U9 - pure decision on the SAME capture and the SAME txn_now.
        decision = self._decide(action, capture, txn_now)
        with _writes():
            store.append_audit(event_name=AUDIT_RESUME_VERSION_CHECK,
                               decision=VERSION_CHECK_MATCH, **common)
            store.append_audit(event_name=AUDIT_GUARD_EVALUATED, decision=decision.decision.value,
                               code=decision.reason_code, **common)
            if decision.decision is GuardDecisionKind.DENY:
                store.transition_terminal(pending_action_id=pending.pending_action_id,
                                          expected_version=pending.version, status=DENIED,
                                          outcome_code=decision.reason_code, at=at)
                store.append_audit(event_name=AUDIT_ACTION_NOT_EXECUTED,
                                   decision=ActionStatus.DENIED.value, code=decision.reason_code,
                                   **common)
                return self._pending_outcome(dataclasses.replace(
                    pending, status=DENIED, outcome_code=decision.reason_code,
                    version=pending.version + 1), None)
            if (decision.decision is not GuardDecisionKind.REQUIRE_APPROVAL
                    or decision.reason_code != pending.guard_reason_code):
                store.transition_terminal(pending_action_id=pending.pending_action_id,
                                          expected_version=pending.version, status=STALE,
                                          outcome_code=STALE_GUARD_DECISION_CHANGED, at=at)
                store.append_audit(event_name=AUDIT_ACTION_NOT_EXECUTED,
                                   decision=ActionStatus.STALE.value,
                                   code=STALE_GUARD_DECISION_CHANGED, **common)
                return self._pending_outcome(dataclasses.replace(
                    pending, status=STALE, outcome_code=STALE_GUARD_DECISION_CHANGED,
                    version=pending.version + 1), None)
            # U10 - business row, receipt, pending EXECUTED: one transaction.
            receipt_view = self._write_execution(
                store, identity, persona, action, pending.idempotency_key, capture, decision, at,
                pending_action_id=pending.pending_action_id)
            store.mark_executed(pending_action_id=pending.pending_action_id,
                                expected_version=pending.version,
                                receipt_id=receipt_view.receipt_id, at=at)
            store.append_audit(event_name=AUDIT_ACTION_EXECUTED, receipt_id=receipt_view.receipt_id,
                               code=receipt_view.resource_type, **common)
        return ActionOutcome(
            status=ActionStatus.EXECUTED, action_name=pending.action_name,
            request_id=pending.request_id, pending_action_id=pending.pending_action_id,
            receipt=receipt_view,
            guard=GuardView(decision=decision.decision.value, reason_code=decision.reason_code))

    # ======================================================================
    # resume_action / get_outcome (§13.3)
    # ======================================================================

    def resume_action(self, decision: ApprovalDecision) -> ActionOutcome:
        """T1; and, when the recorded decision is APPROVE and still awaits execution, T2."""
        recorded = self.record_decision(decision)
        if (decision.decision == APPROVE and not recorded.decision_conflict
                and recorded.status is ActionStatus.WAITING_APPROVAL and recorded.approval_recorded):
            return self.execute_approved(decision.pending_action_id)
        return recorded

    def get_outcome(self, pending_action_id: str) -> ActionOutcome:
        """Read-only: no Clock, no write, no audit."""
        pending_action_id = require_pending_action_id(pending_action_id)
        connection = connect_writer(self._db_path, busy_timeout_ms=self._busy_timeout_ms)
        try:
            connection.execute("BEGIN")
            try:
                store = ActionStore(connection)
                pending = store.load_pending(pending_action_id)
                if pending is None:
                    raise UnknownPendingAction("no pending action has this id")
                try:
                    return self._pending_outcome(pending, self._receipt_for(store, pending))
                except _AttemptFailed:
                    raise ActionContractError("the stored pending action violates its contract") from None
            finally:
                self._rollback(connection)
        finally:
            connection.close()

    # ======================================================================
    # Shared pieces
    # ======================================================================

    def _capture(self, connection: sqlite3.Connection, persona: Persona, action: ValidatedAction,
                 txn_now: datetime, *, exclude_pending_id: str | None) -> GuardCapture:
        context = TrustedExecutionContext(persona=persona, clock=FixedClock(txn_now),
                                          connection=connection)
        try:
            return self._guard.capture(action, context, self._catalog, txn_now=txn_now,
                                       exclude_pending_id=exclude_pending_id)
        except GuardFailure as failure:
            raise _AttemptFailed(failure.code, from_guard=True) from None

    def _decide(self, action: ValidatedAction, capture: GuardCapture,
                txn_now: datetime) -> GuardDecision:
        try:
            return Guard.decide(action, capture.state, capture.policy, self._risk, txn_now)
        except GuardFailure as failure:
            raise _AttemptFailed(failure.code, from_guard=True) from None

    def _capture_and_decide(self, connection, persona, action, txn_now, *, exclude_pending_id):
        capture = self._capture(connection, persona, action, txn_now,
                                exclude_pending_id=exclude_pending_id)
        return capture, self._decide(action, capture, txn_now)

    def _new_id(self, kind: IdKind, key: str) -> str:
        try:
            return self._ids.new_id(kind, key)
        except Exception:
            raise _AttemptFailed(FAILURE_ID_GENERATION) from None

    def _write_execution(self, store: ActionStore, identity: RequestIdentity, persona: Persona,
                         action: ValidatedAction, key: str, capture: GuardCapture,
                         decision: GuardDecision, at: str, *,
                         pending_action_id: str | None) -> ReceiptView:
        """The declared business row and its receipt. Nothing else is written here."""
        name = action.action_name
        if name == ESCALATE_TO_HUMAN:
            resource_type = RESOURCE_HANDOFF_TICKET
            resource_id = self._new_id(IdKind.HANDOFF_TICKET, key)
        else:
            resource_type = RESOURCE_AFTER_SALES_CASE
            resource_id = self._new_id(IdKind.AFTER_SALES_CASE, key)
        receipt_id = self._new_id(IdKind.RECEIPT, key)
        document = snapshot_document(capture.candidate_snapshot, decision)

        self._fault(FAULT_BUSINESS_WRITE)
        if name == ESCALATE_TO_HUMAN:
            store.insert_handoff_ticket(ticket_id=resource_id, action=action, at=at)
        else:
            store.insert_after_sales_case(case_id=resource_id, action=action,
                                          customer_id=persona.customer_id,
                                          case_type=_CASE_TYPES[name], at=at)
        self._fault(FAULT_RECEIPT_WRITE)
        store.insert_receipt(
            receipt_id=receipt_id, key=key, identity=identity, action=action,
            resource_type=resource_type, resource_id=resource_id,
            pending_action_id=pending_action_id, guard_decision=decision.decision.value,
            guard_reason_code=decision.reason_code, snapshot_json=document,
            snapshot_sha256=snapshot_sha256(document), action_spec_version=ACTION_SPEC_VERSION,
            risk_policy_version=self._risk.version, policy_build_id=capture.policy.build_id,
            executed_at=at)
        return ReceiptView(receipt_id=receipt_id, resource_type=resource_type,
                           resource_id=resource_id)

    def _rebuild(self, pending: PendingRecord, identity: RequestIdentity) -> ValidatedAction:
        """The stored action, re-validated; its digest and key must recompute exactly."""
        try:
            args = json.loads(pending.args_json)
        except (TypeError, ValueError):
            raise _AttemptFailed(FAILURE_INVARIANT) from None
        if not isinstance(args, dict):
            raise _AttemptFailed(FAILURE_INVARIANT)
        try:
            action = self._validator.validate(pending.action_name, args)
        except ActionValidationError:
            raise _AttemptFailed(FAILURE_INVARIANT) from None
        if (action.canonical_args_json != pending.args_json
                or action.args_sha256 != pending.args_sha256
                or action.target_order_id != pending.target_order_id
                or action.target_order_item_id != pending.target_order_item_id
                or idempotency_key(identity, action) != pending.idempotency_key):
            raise _AttemptFailed(FAILURE_INVARIANT)
        return action

    @staticmethod
    def _require_stored_matches_row(stored, pending: PendingRecord) -> None:
        snapshot = stored.snapshot
        if (stored.decision is not GuardDecisionKind.REQUIRE_APPROVAL
                or stored.reason_code != pending.guard_reason_code
                or pending.guard_decision != GuardDecisionKind.REQUIRE_APPROVAL.value
                or snapshot.policy_build_id != pending.policy_build_id
                or snapshot.action_spec_version != pending.action_spec_version
                or snapshot.risk_policy_version != pending.risk_policy_version):
            raise SnapshotIntegrityError("the stored snapshot disagrees with its pending row")

    @staticmethod
    def _pending_identity(pending: PendingRecord) -> RequestIdentity:
        try:
            return RequestIdentity(persona_id=pending.persona_id, request_id=pending.request_id)
        except ValueError:
            raise _AttemptFailed(FAILURE_INVARIANT) from None

    @staticmethod
    def _receipt_for(store: ActionStore, pending: PendingRecord) -> ReceiptRecord | None:
        if pending.status != EXECUTED:
            return None
        try:
            return store.find_receipt_for_pending(pending.pending_action_id)
        except sqlite3.Error:
            raise _AttemptFailed(FAILURE_STATE_READ) from None

    @staticmethod
    def _pending_outcome(pending: PendingRecord, receipt: ReceiptRecord | None, *,
                         replay: bool = False, conflict: bool = False) -> ActionOutcome:
        """The caller-facing view of one pending row (and its receipt, once EXECUTED)."""
        base = dict(action_name=pending.action_name, request_id=pending.request_id,
                    pending_action_id=pending.pending_action_id, idempotent_replay=replay,
                    decision_conflict=conflict)
        approval_guard = GuardView(decision=GuardDecisionKind.REQUIRE_APPROVAL.value,
                                   reason_code=pending.guard_reason_code)
        try:
            if pending.status in (PENDING_APPROVAL, APPROVED):
                return ActionOutcome(status=ActionStatus.WAITING_APPROVAL, guard=approval_guard,
                                     approval_recorded=pending.status == APPROVED, **base)
            if pending.status == EXECUTED:
                if (receipt is None or receipt.receipt_id != pending.receipt_id
                        or receipt.pending_action_id != pending.pending_action_id):
                    raise _AttemptFailed(FAILURE_INVARIANT)
                return ActionOutcome(
                    status=ActionStatus.EXECUTED, guard=GuardView(
                        decision=receipt.guard_decision, reason_code=receipt.guard_reason_code),
                    receipt=ReceiptView(receipt_id=receipt.receipt_id,
                                        resource_type=receipt.resource_type,
                                        resource_id=receipt.resource_id), **base)
            if pending.status == DENIED:
                return ActionOutcome(status=ActionStatus.DENIED, code=pending.outcome_code,
                                     guard=GuardView(decision=GuardDecisionKind.DENY.value,
                                                     reason_code=pending.outcome_code), **base)
            if pending.status in (REJECTED, STALE, FAILED):
                return ActionOutcome(status=ActionStatus(pending.status), code=pending.outcome_code,
                                     guard=approval_guard, **base)
        except ValueError:
            raise _AttemptFailed(FAILURE_INVARIANT) from None
        raise _AttemptFailed(FAILURE_INVARIANT)

    # -- failure handling ------------------------------------------------------------

    def _fault(self, point: str) -> None:
        if self._hooks is not None:
            self._hooks.before(point)

    def _commit(self, connection: sqlite3.Connection) -> None:
        """COMMIT; any failure here means nothing was committed."""
        try:
            self._fault(FAULT_COMMIT)
            connection.execute("COMMIT")
        except Exception:
            raise _AttemptFailed(FAILURE_TRANSACTION) from None

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _compensate_start(self, connection: sqlite3.Connection, identity: RequestIdentity,
                          action: ValidatedAction, key: str, failure: _AttemptFailed,
                          txn_now: datetime) -> None:
        """Best-effort compensating audit. Reuses txn_now; never reads the Clock."""
        at = txn_now.isoformat()
        try:
            self._fault(FAULT_COMPENSATION)
            connection.execute("BEGIN IMMEDIATE")
            store = ActionStore(connection)
            store.append_audit(
                event_name=AUDIT_GUARD_FAILED if failure.from_guard else AUDIT_TRANSACTION_ROLLED_BACK,
                identity=identity, action_name=action.action_name, at=at, idempotency_key=key,
                phase=PHASE_START, code=failure.code)
            store.append_audit(
                event_name=AUDIT_ACTION_NOT_EXECUTED, identity=identity,
                action_name=action.action_name, at=at, idempotency_key=key,
                decision=ActionStatus.FAILED.value, code=failure.code)
            connection.execute("COMMIT")
        except Exception:
            self._rollback(connection)

    def _compensate_resume(self, connection: sqlite3.Connection, resume: _Resume,
                           failure: _AttemptFailed, txn_now: datetime) -> None:
        """P6, best effort: APPROVED -> FAILED plus a safe audit, reusing txn_now.

        If this fails too, the pending stays APPROVED and a later execute_approved
        repeats every check in a new transaction.
        """
        pending, identity = resume.pending, resume.identity
        at = txn_now.isoformat()
        common = dict(identity=identity, action_name=pending.action_name, at=at,
                      idempotency_key=pending.idempotency_key,
                      pending_action_id=pending.pending_action_id, phase=PHASE_RESUME)
        try:
            self._fault(FAULT_COMPENSATION)
            connection.execute("BEGIN IMMEDIATE")
            store = ActionStore(connection)
            store.transition_terminal(pending_action_id=pending.pending_action_id,
                                      expected_version=pending.version, status=FAILED,
                                      outcome_code=failure.code, at=at)
            store.append_audit(
                event_name=AUDIT_GUARD_FAILED if failure.from_guard else AUDIT_TRANSACTION_ROLLED_BACK,
                code=failure.code, **common)
            store.append_audit(event_name=AUDIT_ACTION_NOT_EXECUTED,
                               decision=ActionStatus.FAILED.value, code=failure.code, **common)
            connection.execute("COMMIT")
        except Exception:
            self._rollback(connection)

    @staticmethod
    def _failed(action_name: str | None, request_id: str | None, code: str, *,
                pending_action_id: str | None = None) -> ActionOutcome:
        return ActionOutcome(status=ActionStatus.FAILED, action_name=action_name,
                             request_id=request_id, code=code, pending_action_id=pending_action_id)


class _writes:
    """Map write-phase errors to closed failure codes; _AttemptFailed passes through."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None or isinstance(exc, _AttemptFailed):
            return False
        if isinstance(exc, (PendingTransitionConflict, sqlite3.IntegrityError)):
            # A guarded update or a constraint the Guard should have kept: an invariant broke.
            raise _AttemptFailed(FAILURE_INVARIANT) from None
        if isinstance(exc, Exception):
            raise _AttemptFailed(FAILURE_WRITE) from None
        return False
