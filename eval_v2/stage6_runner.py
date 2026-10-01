"""One Stage 6 case-run: main run, trusted operator script, final state (design §19.2).

    run_stage6_case(case, policy_factory)        the control-policy path
    run_stage6_reference(case)                   the oracle / reference path

Steps of one case-run (no database is ever reused across case-runs):

     1  fresh file-backed Stage 6 database          Stage6CaseRuntime.from_case
     2  initial_state patches                       (trusted harness, BEGIN IMMEDIATE)
     3  read runtime + one FaultInjectingGateway
     4  CapabilityGate().narrow()
     5  ActionGateway
     6  trusted RequestIdentity(persona, "req-1")
     7  main run: run_action_conversation (policy path) or a direct
        start_action of the expected action (reference path)
     8  run / protocol / action facts captured
     9  operator_script, event by event
    10  final database F
    11  baseline B in a separate database (stage6_state.build_baseline)
    12  scoring happens afterwards (eval_v2.stage6_scoring), from this result only

The operator_script is trusted harness input. It is never put into a
ActionControlState, a prompt or a tool observation; the policy factory
receives nothing from the case.

Operator events
    approve / reject      resume_action(APPROVE / REJECT) on the pending id
                          taken from the main run's result only; approver
                          op-demo-1; decided_at = the current business time
    record_decision       T1 only;  execute_approved: T2 only
    mutate                trusted business write in BEGIN IMMEDIATE (the version grows)
    advance_clock         later operations use a FixedClock at the new instant
    restart               every connection closed, every system object dropped,
                          rebuilt from the database file + static config
    replay_submission     the main run's accepted ValidatedAction, the same
                          RequestIdentity, straight to start_action (no model):
                          system idempotency
    rerun_request /       a fresh policy instance runs the user script again with
    new_request           request_id req-1 / req-2 (model stability, rerun_ok)

An event that cannot be performed (no pending id, no accepted action) or that
the gateway refuses (NotApproved, UnknownPendingAction, ApprovalInputError)
records {"status": null} with the error class name; it never stops the run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Mapping

from aftersales.action_errors import ApprovalInputError, NotApproved, UnknownPendingAction
from aftersales.action_outcome import ActionOutcome, ActionOutcomeRenderer
from aftersales.actions import ActionIntentValidator, ValidatedAction, build_action_registry
from aftersales.approval import APPROVE, REJECT, ApprovalDecision
from aftersales.guard import GuardDecision
from aftersales.ids import idempotency_key

from .action_control import STAGE6_MAX_STEPS, ActionControlPolicy
from .action_runner import (
    ActionRunRecord,
    ConditionalTurn,
    Stage6Conversation,
    run_action_conversation,
)
from .control import canonical_json
from .faults import FaultCallRecord
from .runner import CONTROL_RUN_SCHEMA, CaseRunRecord
from .stage6_runtime import (
    MAIN_REQUEST_ID,
    NEW_REQUEST_ID,
    OPERATOR_REF,
    ActionFaultRecord,
    Stage6CaseRuntime,
)
from .stage6_state import build_baseline, state_sha256

PATH_POLICY = "policy"
PATH_REFERENCE = "reference"
RERUN_OPS = frozenset({"rerun_request", "new_request"})

PolicyFactory = Callable[[], ActionControlPolicy]


def outcome_view(outcome: ActionOutcome | None) -> dict | None:
    """The scored shape of one action result: status, code and the two flags."""
    if outcome is None:
        return {"status": None}
    return {"status": outcome.status.value, "code": outcome.code,
            "idempotent_replay": outcome.idempotent_replay,
            "decision_conflict": outcome.decision_conflict}


@dataclass(frozen=True, kw_only=True)
class EventResult:
    """What one operator event did. `outcome` is None for mutate / advance_clock / restart."""

    index: int
    op: str
    outcome: dict | None
    action_outcome: ActionOutcome | None
    rendered_text: str | None
    error: str | None
    business_time: str
    audit: tuple[dict, ...]
    pending_after: dict | None
    receipt_after: dict | None
    run_record: ActionRunRecord | None = None
    decision_records: tuple = ()
    action: ValidatedAction | None = None
    request_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index, "op": self.op, "outcome": self.outcome,
            "rendered_text": self.rendered_text, "error": self.error,
            "business_time": self.business_time,
            "audit_events": [row["event_name"] for row in self.audit],
            "run_record": None if self.run_record is None else self.run_record.to_dict(),
            "decision_records": [record.to_dict() for record in self.decision_records],
        }


@dataclass(frozen=True, kw_only=True)
class Stage6CaseRunResult:
    """Every fact a Stage 6 score needs. Built before any label is read (except the
    reference path, whose main step is the expected action itself)."""

    path: str
    case_id: str
    persona_id: str
    customer_id: str
    capabilities: object
    main_record: ActionRunRecord | None
    main_decision_records: tuple
    main_action: ValidatedAction | None
    main_key: str | None
    main_outcome: ActionOutcome | None
    main_rendered_text: str | None
    main_guard: GuardDecision | None
    main_guard_decided: bool
    main_fault_fired: bool
    main_audit: tuple[dict, ...]
    paused_pending: dict | None
    state_after_main: dict
    events: tuple[EventResult, ...]
    read_fault_records: tuple[FaultCallRecord, ...]
    main_read_fault_records: tuple[FaultCallRecord, ...]
    action_fault_records: tuple[ActionFaultRecord, ...]
    initial_state: dict
    final_state: dict
    baseline_state: dict
    audit: tuple[dict, ...]
    restarts: int
    gateways_built: int
    stage5_view: CaseRunRecord | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, object]:
        """Plain data for result files. No argument values, user text or reasoning."""
        return {
            "path": self.path,
            "case_id": self.case_id,
            "main_record": None if self.main_record is None else self.main_record.to_dict(),
            "protocol_decision_records": [record.to_dict() for record in self.main_decision_records],
            "main_action": None if self.main_action is None else {
                "action_name": self.main_action.action_name, "args_sha256": self.main_action.args_sha256},
            "main_outcome": None if self.main_outcome is None else self.main_outcome.to_dict(),
            "main_rendered_text": self.main_rendered_text,
            "main_guard": None if self.main_guard is None else {
                "decision": self.main_guard.decision.value, "reason_code": self.main_guard.reason_code},
            "events": [event.to_dict() for event in self.events],
            "read_fault_records": [record.to_dict() for record in self.read_fault_records],
            "action_fault_records": [record.to_dict() for record in self.action_fault_records],
            "final_state_sha256": state_sha256(self.final_state),
            "baseline_state_sha256": state_sha256(self.baseline_state),
            "restarts": self.restarts,
        }


def conversation_of(case: Mapping) -> Stage6Conversation:
    turns = case["user_turns"]
    return Stage6Conversation(
        first_turn=turns[0]["text"],
        conditional_turns=tuple(ConditionalTurn(on_clarify=tuple(turn["on_clarify"]), text=turn["text"])
                                for turn in turns[1:]))


def reference_action(case: Mapping) -> ValidatedAction | None:
    """The expected action's semantic args (first args_any_of value), validated."""
    expected = case["expected_action"]
    if expected is None:
        return None
    args = dict(expected["args"])
    for name, values in expected["args_any_of"].items():
        args[name] = values[0]
    validator = ActionIntentValidator(build_action_registry(), (expected["action_name"],))
    return validator.validate(expected["action_name"], args)


class _CaseRun:
    def __init__(self, case: dict, runtime: Stage6CaseRuntime, path: str,
                 policy_factory: PolicyFactory | None) -> None:
        self.case = case
        self.runtime = runtime
        self.path = path
        self.policy_factory = policy_factory
        self.renderer = ActionOutcomeRenderer()
        self.main_action: ValidatedAction | None = None
        self.main_outcome: ActionOutcome | None = None

    # -- helpers -----------------------------------------------------------

    def _audit_since(self, position: int) -> tuple[dict, ...]:
        return self.runtime.audit()[position:]

    def _main_rows(self) -> tuple[dict | None, dict | None]:
        if self.main_action is None:
            return None, None
        key = idempotency_key(self.runtime.identity(MAIN_REQUEST_ID), self.main_action)
        state = self.runtime.state()
        pending = next((row for row in state["pending_actions"].values()
                        if row["idempotency_key"] == key), None)
        receipt = next((row for row in state["action_receipts"].values()
                        if row["idempotency_key"] == key), None)
        return pending, receipt

    def _render(self, outcome: ActionOutcome | None) -> str | None:
        return None if outcome is None else self.renderer.render(outcome)

    def _conversation(self, request_id: str):
        """One policy run (main or rerun); returns (record, decision records, outcome)."""
        policy = self.policy_factory()
        record = run_action_conversation(
            conversation_of(self.case), policy,
            persona_id=self.runtime.persona.persona_id, request_id=request_id,
            virtual_now=self.runtime.virtual_now, capabilities=self.runtime.capabilities,
            read_gateway=self.runtime.read_gateway, action_gateway=self.runtime.gateway)
        decisions = tuple(getattr(policy, "decision_records", ()))
        return record, decisions, record.outcome

    # -- the main step -----------------------------------------------------

    def main(self) -> dict:
        audit_before = len(self.runtime.audit())
        faults_before = len(self.runtime.injector.records)
        decisions_before = len(self.runtime.injector.decisions)
        reads_before = len(self.runtime.read_gateway.records)
        record, decisions, outcome = None, (), None
        if self.path == PATH_POLICY:
            record, decisions, outcome = self._conversation(MAIN_REQUEST_ID)
            self.main_action = record.accepted_action
        else:
            self.main_action = reference_action(self.case)
            if self.main_action is not None:
                outcome = self.runtime.gateway.start_action(self.runtime.identity(MAIN_REQUEST_ID),
                                                            self.main_action)
        self.main_outcome = outcome
        observed = self.runtime.injector.decisions[decisions_before:]
        pending, _ = self._main_rows()
        return dict(
            main_record=record, main_decision_records=decisions,
            main_outcome=outcome,
            main_rendered_text=(None if outcome is None else
                                (record.paused.rendered_text if record is not None and record.paused
                                 else record.completed.rendered_text if record is not None
                                 else self._render(outcome))),
            main_guard=observed[0] if observed else None,
            main_guard_decided=bool(observed),
            main_fault_fired=self.runtime.injector.fired_since(faults_before),
            main_audit=self._audit_since(audit_before),
            paused_pending=pending,
            state_after_main=self.runtime.state(),
            main_read_fault_records=tuple(self.runtime.read_gateway.records[reads_before:]),
        )

    # -- operator events ---------------------------------------------------

    def event(self, index: int, event: Mapping) -> EventResult:
        op = event["op"]
        audit_before = len(self.runtime.audit())
        outcome: ActionOutcome | None = None
        performed = op in ("mutate", "advance_clock", "restart")
        error = None
        run_record, decisions, action, request_id = None, (), None, None
        pending_id = None if self.main_outcome is None else self.main_outcome.pending_action_id
        try:
            if op == "mutate":
                self.runtime.mutate(event)
            elif op == "advance_clock":
                self.runtime.advance_clock(event["virtual_now"])
            elif op == "restart":
                self.runtime.restart()
            elif op in ("approve", "reject", "record_decision"):
                decision = {"approve": APPROVE, "reject": REJECT}.get(op, event.get("decision"))
                if pending_id is not None:
                    approval = ApprovalDecision(pending_action_id=pending_id, decision=decision,
                                                approver_ref=OPERATOR_REF,
                                                decided_at=self.runtime.virtual_now)
                    gateway = self.runtime.gateway
                    outcome = (gateway.record_decision(approval) if op == "record_decision"
                               else gateway.resume_action(approval))
                    performed = True
            elif op == "execute_approved":
                if pending_id is not None:
                    outcome = self.runtime.gateway.execute_approved(pending_id)
                    performed = True
            elif op == "replay_submission":
                if self.main_action is not None:
                    request_id = MAIN_REQUEST_ID
                    action = self.main_action
                    outcome = self.runtime.gateway.start_action(self.runtime.identity(MAIN_REQUEST_ID),
                                                                self.main_action)
                    performed = True
            elif op in ("rerun_request", "new_request"):
                request_id = MAIN_REQUEST_ID if op == "rerun_request" else NEW_REQUEST_ID
                if self.path == PATH_POLICY:
                    run_record, decisions, outcome = self._conversation(request_id)
                    action = run_record.accepted_action
                else:
                    action = reference_action(self.case)
                    if action is not None:
                        outcome = self.runtime.gateway.start_action(self.runtime.identity(request_id),
                                                                    action)
                performed = True
            else:
                raise ValueError("unknown operator event")
        except (NotApproved, UnknownPendingAction, ApprovalInputError) as caught:
            error = type(caught).__name__
            performed = False
        if op in ("mutate", "advance_clock", "restart"):
            view = None
        elif not performed:
            view = {"status": None}
        else:
            view = outcome_view(outcome)
        pending, receipt = self._main_rows()
        return EventResult(index=index, op=op, outcome=view, action_outcome=outcome,
                           rendered_text=self._render(outcome), error=error,
                           business_time=self.runtime.virtual_now,
                           audit=self._audit_since(audit_before), pending_after=pending,
                           receipt_after=receipt, run_record=run_record, decision_records=decisions,
                           action=action, request_id=request_id)


def _stage5_view(case: Mapping, record: ActionRunRecord, read_faults: tuple[FaultCallRecord, ...],
                 initial_sha: str, final_sha: str) -> CaseRunRecord:
    """The main run's read / control facts as the frozen Stage 5 record type.

    Only so the unchanged Stage 5 evidence, generation and citation evaluators
    can be reused. The termination string is the Stage 6 one.
    """
    return CaseRunRecord(
        schema=CONTROL_RUN_SCHEMA, case_id=case["case_id"], virtual_now=record.virtual_now,
        persona_id=record.persona_id, allowed_tools=record.allowed_tools, max_steps=STAGE6_MAX_STEPS,
        termination=record.termination, final_disposition=record.final_disposition,
        control_steps=record.control_steps, user_messages=record.user_messages,
        clarifications=record.clarifications, observations=record.observations,
        fault_records=read_faults, initial_db_sha256=initial_sha, final_db_sha256=final_sha,
        database_unchanged=initial_sha == final_sha)


def _run(case: object, path: str, policy_factory: PolicyFactory | None) -> Stage6CaseRunResult:
    case = json.loads(canonical_json(case))  # a private copy; the caller's case is untouched
    runtime = Stage6CaseRuntime.from_case(case)
    try:
        initial_state = runtime.state()
        run = _CaseRun(case, runtime, path, policy_factory)
        main = run.main()
        events = tuple(run.event(index, event) for index, event in enumerate(case["operator_script"]))
        final_state = runtime.state()
        audit = runtime.audit()
        baseline = build_baseline(case, runtime.directory)
        main_key = (None if run.main_action is None
                    else idempotency_key(runtime.identity(MAIN_REQUEST_ID), run.main_action))
        view = None
        if main["main_record"] is not None:
            view = _stage5_view(case, main["main_record"], main["main_read_fault_records"],
                                state_sha256(initial_state), state_sha256(main["state_after_main"]))
        return Stage6CaseRunResult(
            path=path, case_id=case["case_id"], persona_id=runtime.persona.persona_id,
            customer_id=runtime.customer_id, capabilities=runtime.capabilities,
            main_action=run.main_action, main_key=main_key, events=events,
            read_fault_records=tuple(runtime.read_gateway.records),
            action_fault_records=tuple(runtime.injector.records),
            initial_state=initial_state, final_state=final_state, baseline_state=baseline,
            audit=audit, restarts=runtime.restarts, gateways_built=runtime.gateways_built,
            stage5_view=view, **main)
    finally:
        runtime.close()


def run_stage6_case(case: object, policy_factory: PolicyFactory) -> Stage6CaseRunResult:
    """The control-policy path. A fresh policy instance per conversation."""
    if not callable(policy_factory):
        raise TypeError("policy_factory must return a fresh control policy per call")
    return _run(case, PATH_POLICY, policy_factory)


def run_stage6_reference(case: object) -> Stage6CaseRunResult:
    """The oracle / reference path: the expected action straight to start_action. No model."""
    return _run(case, PATH_REFERENCE, None)

