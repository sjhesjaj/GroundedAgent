"""The ActionGateway: the only component that writes (docs/v2/stage6-design.md §10.2).

Stage 6.1 implements `start_action` only. One transaction, one business
instant, one capture:

    outside the transaction (pure): the action is in the effective capability
        set, re-validated against the closed contract; the persona resolves;
        the server-owned idempotency key is computed
    BEGIN IMMEDIATE                       (fails -> FAILED transaction_failed,
                                           no Clock read, nothing written)
    txn_now = clock.now()                 exactly once for this transaction
    replay lookup by idempotency key      hit -> audit, COMMIT, stored outcome
    capture = Guard.capture(...)          one reader pass, one catalog.snapshot()
    decision = Guard.decide(...)          pure; same capture, same txn_now
    ALLOW  -> audit, business row, receipt, audit
    DENY   -> audit only
    REQUIRE_APPROVAL -> STAGE 6.1 ONLY: ROLLBACK, zero writes,
                        raise ApprovalPathNotEnabled
    COMMIT

Nothing is read between capture and the writes. A failure after BEGIN rolls
everything back; a compensating transaction then records the failure in the
audit trail, reusing txn_now (it never reads the Clock). No outcome ever says
EXECUTED unless its receipt committed.

The Guard is handed a TrustedExecutionContext whose clock is FixedClock(txn_now),
so the injected Clock is unreachable from the Guard by construction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .action_db import DEFAULT_BUSY_TIMEOUT_MS, connect_writer
from .action_errors import (
    FAILURE_ID_GENERATION,
    FAILURE_INVARIANT,
    FAILURE_STATE_READ,
    FAILURE_TRANSACTION,
    FAILURE_WRITE,
    ActionCapabilityError,
    ActionContractError,
    ActionValidationError,
    GuardFailure,
)
from .action_outcome import ActionOutcome, ActionStatus, GuardView, ReceiptView
from .action_policy import S6_RISK_POLICY, ActionRiskPolicy
from .action_store import (
    ANCHOR_PENDING,
    ANCHOR_RECEIPT,
    AUDIT_ACTION_EXECUTED,
    AUDIT_ACTION_NOT_EXECUTED,
    AUDIT_ACTION_REPLAY_HIT,
    AUDIT_GUARD_EVALUATED,
    AUDIT_GUARD_FAILED,
    AUDIT_TRANSACTION_ROLLED_BACK,
    PHASE_START,
    ActionStore,
    PendingRecord,
    ReceiptRecord,
)
from .actions import (
    ACTION_SPEC_VERSION,
    CREATE_EXCHANGE,
    CREATE_RETURN,
    ESCALATE_TO_HUMAN,
    ActionIntentValidator,
    ActionRegistry,
    ValidatedAction,
    build_action_registry,
)
from .capabilities import EffectiveCapabilities
from .clock import Clock, FixedClock, require_aware
from .context import Persona, TrustedExecutionContext
from .demo import resolve_persona
from .guard import Guard, GuardDecisionKind, snapshot_document, snapshot_sha256
from .ids import DeterministicIdProvider, IdKind, IdProvider, RequestIdentity, idempotency_key
from .schema import CaseType


# ==========================================================================
# STAGE 6.1 ONLY - delete in Stage 6.2 (docs/v2/stage6-design.md §10.2, S6-D38)
# ==========================================================================
class ApprovalPathNotEnabled(RuntimeError):
    """STAGE 6.1 ONLY. Raised when the Guard returns REQUIRE_APPROVAL.

    Stage 6.1 has no pending-action path: the transaction is rolled back with
    zero writes (no pending row, no audit row, no compensating transaction).
    Stage 6.2 deletes this class and its branch and enables the normal
    REQUIRE_APPROVAL -> audit + pending path. This is an implementation-stage
    exception, never a user-visible outcome.
    """


# Deterministic fault points, used by tests to exercise rollback paths. The
# formal Stage 6 eval fault declarations (action_faults) are a Stage 6.4 concern.
FAULT_BUSINESS_WRITE = "business_write"
FAULT_RECEIPT_WRITE = "receipt_write"
FAULT_COMMIT = "commit"
FAULT_POINTS = (FAULT_BUSINESS_WRITE, FAULT_RECEIPT_WRITE, FAULT_COMMIT)


class ActionFaultHooks(Protocol):
    def before(self, point: str) -> None:
        """Called just before a fault point; may raise to simulate a failure."""


class _AttemptFailed(Exception):
    """Internal: an infrastructure failure after BEGIN. Carries a closed code."""

    def __init__(self, code: str, *, from_guard: bool) -> None:
        super().__init__(code)
        self.code = code
        self.from_guard = from_guard


_CASE_TYPES = {CREATE_RETURN: CaseType.RETURN.value, CREATE_EXCHANGE: CaseType.EXCHANGE.value}


class ActionGateway:
    """The single side-effect entry point. Holds configuration only, no state."""

    def __init__(self, db_path: str | Path, *, clock: Clock, id_provider: IdProvider,
                 catalog: object, capabilities: EffectiveCapabilities,
                 risk_policy: ActionRiskPolicy = S6_RISK_POLICY,
                 action_registry: ActionRegistry | None = None,
                 persona_resolver: Callable[[str], Persona] = resolve_persona,
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
        self._hooks = fault_hooks
        self._busy_timeout_ms = busy_timeout_ms

    # -- public API ------------------------------------------------------------

    def start_action(self, identity: RequestIdentity, action: ValidatedAction) -> ActionOutcome:
        """One action attempt. EXECUTED / DENIED / FAILED (or a replay)."""
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
                return self._failed(identity, action, FAILURE_TRANSACTION)
            # S2 - the only Clock read of this transaction.
            txn_now = require_aware("clock.now()", self._clock.now())
            try:
                outcome = self._attempt(connection, identity, persona, action, key, txn_now)
            except ApprovalPathNotEnabled:
                # STAGE 6.1 ONLY: zero writes, no compensating transaction.
                self._rollback(connection)
                raise
            except _AttemptFailed as failure:
                self._rollback(connection)
                self._record_failure(connection, identity, action, key, failure, txn_now)
                return self._failed(identity, action, failure.code)
            except BaseException:
                self._rollback(connection)
                raise
            # S7
            try:
                self._fault(FAULT_COMMIT)
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                failure = _AttemptFailed(FAILURE_TRANSACTION, from_guard=False)
                self._record_failure(connection, identity, action, key, failure, txn_now)
                return self._failed(identity, action, FAILURE_TRANSACTION)
            return outcome
        finally:
            connection.close()

    # -- the transaction body ------------------------------------------------------

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
            raise _AttemptFailed(FAILURE_STATE_READ, from_guard=False) from None
        if receipt is not None or pending is not None:
            try:
                return self._replay(store, identity, action, receipt, pending, common)
            except (sqlite3.Error, ValueError, RuntimeError):
                raise _AttemptFailed(FAILURE_WRITE, from_guard=False) from None

        # S4, S5 - one capture, one pure decision, same txn_now.
        context = TrustedExecutionContext(persona=persona, clock=FixedClock(txn_now),
                                          connection=connection)
        try:
            capture = self._guard.capture(action, context, self._catalog, txn_now=txn_now,
                                          exclude_pending_id=None)
            decision = Guard.decide(action, capture.state, capture.policy, self._risk, txn_now)
        except GuardFailure as failure:
            raise _AttemptFailed(failure.code, from_guard=True) from None

        if decision.decision is GuardDecisionKind.REQUIRE_APPROVAL:
            # ==================================================================
            # STAGE 6.1 ONLY - delete in Stage 6.2 (§10.2 S6b, S6-D38).
            # No pending row, no audit row: the caller rolls back with zero writes.
            # ==================================================================
            raise ApprovalPathNotEnabled(
                "Stage 6.1 has no approval path: REQUIRE_APPROVAL is not executed")

        guard_view = GuardView(decision=decision.decision.value, reason_code=decision.reason_code)
        # S6 - writes only from here on; nothing is read again.
        try:
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
            if decision.decision is not GuardDecisionKind.ALLOW:
                raise _AttemptFailed(FAILURE_INVARIANT, from_guard=False)
            return self._execute(store, identity, persona, action, key, capture, decision,
                                 guard_view, at, common)
        except _AttemptFailed:
            raise
        except sqlite3.IntegrityError:
            # A UNIQUE / CHECK violation the Guard should have prevented.
            raise _AttemptFailed(FAILURE_INVARIANT, from_guard=False) from None
        except Exception:
            raise _AttemptFailed(FAILURE_WRITE, from_guard=False) from None

    def _execute(self, store: ActionStore, identity: RequestIdentity, persona: Persona,
                 action: ValidatedAction, key: str, capture, decision, guard_view: GuardView,
                 at: str, common: dict) -> ActionOutcome:
        name = action.action_name
        try:
            if name == ESCALATE_TO_HUMAN:
                resource_type = "human_handoff_ticket"
                resource_id = self._ids.new_id(IdKind.HANDOFF_TICKET, key)
            else:
                resource_type = "after_sales_case"
                resource_id = self._ids.new_id(IdKind.AFTER_SALES_CASE, key)
            receipt_id = self._ids.new_id(IdKind.RECEIPT, key)
        except Exception:
            raise _AttemptFailed(FAILURE_ID_GENERATION, from_guard=False) from None
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
            resource_type=resource_type, resource_id=resource_id, pending_action_id=None,
            guard_decision=decision.decision.value, guard_reason_code=decision.reason_code,
            snapshot_json=document, snapshot_sha256=snapshot_sha256(document),
            action_spec_version=ACTION_SPEC_VERSION, risk_policy_version=self._risk.version,
            policy_build_id=capture.policy.build_id, executed_at=at)
        store.append_audit(event_name=AUDIT_ACTION_EXECUTED, receipt_id=receipt_id,
                           code=resource_type, **common)
        return ActionOutcome(
            status=ActionStatus.EXECUTED, action_name=name, request_id=identity.request_id,
            receipt=ReceiptView(receipt_id=receipt_id, resource_type=resource_type,
                                resource_id=resource_id),
            guard=guard_view)

    def _replay(self, store: ActionStore, identity: RequestIdentity, action: ValidatedAction,
                receipt: ReceiptRecord | None, pending: PendingRecord | None,
                common: dict) -> ActionOutcome:
        """The stored outcome of this very key. The Guard is not run again."""
        if receipt is not None:
            if receipt.action_name != action.action_name:
                raise _AttemptFailed(FAILURE_INVARIANT, from_guard=False)
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
        # A pending row for this key (created from Stage 6.2 on): its stored status.
        if pending.action_name != action.action_name:
            raise _AttemptFailed(FAILURE_INVARIANT, from_guard=False)
        store.append_audit(event_name=AUDIT_ACTION_REPLAY_HIT, phase=PHASE_START,
                           pending_action_id=pending.pending_action_id, code=ANCHOR_PENDING,
                           **common)
        guard = GuardView(decision=GuardDecisionKind.REQUIRE_APPROVAL.value,
                          reason_code=pending.guard_reason_code)
        base = dict(action_name=action.action_name, request_id=identity.request_id,
                    idempotent_replay=True, pending_action_id=pending.pending_action_id)
        if pending.status in ("PENDING_APPROVAL", "APPROVED"):
            return ActionOutcome(status=ActionStatus.WAITING_APPROVAL,
                                 approval_recorded=pending.status == "APPROVED", guard=guard, **base)
        if pending.status == "DENIED":
            return ActionOutcome(status=ActionStatus.DENIED, code=pending.outcome_code,
                                 guard=GuardView(decision=GuardDecisionKind.DENY.value,
                                                 reason_code=pending.outcome_code), **base)
        if pending.status in ("REJECTED", "STALE", "FAILED"):
            return ActionOutcome(status=ActionStatus(pending.status), code=pending.outcome_code,
                                 guard=guard, **base)
        # EXECUTED pending rows always have a receipt, which is looked up first.
        raise _AttemptFailed(FAILURE_INVARIANT, from_guard=False)

    # -- failure handling ------------------------------------------------------------

    def _fault(self, point: str) -> None:
        if self._hooks is not None:
            self._hooks.before(point)

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _record_failure(self, connection: sqlite3.Connection, identity: RequestIdentity,
                        action: ValidatedAction, key: str, failure: _AttemptFailed,
                        txn_now: datetime) -> None:
        """Best-effort compensating audit. Reuses txn_now; never reads the Clock."""
        at = txn_now.isoformat()
        try:
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

    @staticmethod
    def _failed(identity: RequestIdentity, action: ValidatedAction, code: str) -> ActionOutcome:
        return ActionOutcome(status=ActionStatus.FAILED, action_name=action.action_name,
                             request_id=identity.request_id, code=code)
