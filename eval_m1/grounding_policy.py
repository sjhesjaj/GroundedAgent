"""The M1-A1 grounding gate composed around a Stage 6 control policy (M1-A2).

    GroundingGatedPolicy(inner, mode="shadow" | "enforce", case_id=..., round=...)

The frozen Stage 6 runner (eval_v2.stage6_runner) never goes through the
product Conversation, so the product gate is composed here, around the
policy, without touching frozen code:

    state --> [fix the visible set] --> inner.next_action(state)
          ToolCall      remembered (tool, arguments, expected observation id),
                        returned unchanged
          ActionIntent  ActionIntentValidator (frozen, as in the product)
                        -> ground_action(action, visible)    (product, unchanged)
                        -> one GroundingDecisionRecord
                        shadow:  returned unchanged, whatever the result
                        enforce: returned if grounded, else Finish(refuse)
          anything else returned unchanged

Provenance. The ledger is fed from state.observations only: an observation is
registered once it pairs with a ToolCall this wrapper forwarded earlier - the
same observation id (eval_v2.runner.observation_id_for of that call's turn and
tool step), tool name, arguments, control step, and a ToolResult whose tool
name and trace observation id agree - as Conversation._read checks it. An
observation that does not pair, or that carries no ToolResult (an injected
malformed result), is never registered and leaves a diagnostic; every later
decision of the run then grounds against an empty set (fail-closed). The
visible set is fixed before the inner policy is asked:
visible_to(run_index=1, observation_ids=every observation of the state). Each
policy instance the runner creates (main run, rerun_request, new_request) is
one run with its own ledger.

Attribution. Every rejection is also checked by an independent re-reading of
the documented rules (m1-grounding/1) over the same paired reads, so a
rejection is never its own proof:

    true_rejection    the rules are not satisfied by this run's reads
    false_rejection   the rules are satisfied, yet ground_action rejected
                      (a defect of M1-A1; the experiment must stop)
    fail_closed       a visible observation's provenance could not be
                      established, so the gate saw no reads (never expected)

The wrapper never reads case labels or an operator script; it sees the state.
A record holds no argument value, no user text and no customer id: argument
values only enter it as booleans ("does this value occur in the user text /
in some read").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from aftersales.action_errors import ActionValidationError
from aftersales.actions import ActionIntentValidator, ValidatedAction, build_action_registry
from aftersales.executor import TRACE_OBSERVATION_ID
from aftersales_service.action_grounding import (
    MISSING_INVENTORY_OBSERVATION,
    MISSING_ORDER_OBSERVATION,
    STALE_OR_FAILED_OBSERVATION,
    TARGET_NOT_OBSERVED,
    TARGET_RECONSTRUCTION_MISMATCH,
    TARGET_RELATION_MISMATCH,
    GroundingRejected,
    ground_action,
)
from aftersales_service.observation_provenance import ObservationLedger, ProvenanceError, VisibleObservations
from eval_v2.action_control import ActionControlPolicy, ActionControlState, ActionIntent, Stage6Action
from eval_v2.control import Finish, ToolCall, ToolContractFailure, ToolObservation
from eval_v2.runner import observation_id_for
from orchestration.contracts import OBSERVATION_ID_KEY, BusinessEvidence, ToolResult, ToolStatus

MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_SHADOW, MODE_ENFORCE)

# The frozen Finish disposition an enforced rejection ends the run with. The
# frozen contract (eval/v2/spec/stage6-final-outcomes.json) defines refuse as
# "a required action argument depends on evidence that is missing or
# unavailable", which is exactly a grounding rejection; boundary is a request
# beyond trusted identity, permission or the system's operations, and its
# fixed text claims the system is read-only. Checked against
# finish_dispositions() when Finish is built.
REJECTION_DISPOSITION = "refuse"

# Each runner-created policy instance is one control run with its own ledger.
LEDGER_RUN_INDEX = 1

ATTRIBUTION_TRUE = "true_rejection"
ATTRIBUTION_FALSE = "false_rejection"
ATTRIBUTION_FAIL_CLOSED = "fail_closed"
ATTRIBUTIONS = (ATTRIBUTION_TRUE, ATTRIBUTION_FALSE, ATTRIBUTION_FAIL_CLOSED)

# Why the independent re-check finds the rules unsatisfied (closed, in rule order).
REASON_NO_ORDER_READ = "no_order_read"
REASON_ORDER_READ_NOT_OK = "order_read_not_ok"
REASON_ITEM_NOT_IN_LATEST_ORDER_READ = "item_not_in_latest_order_read"
REASON_ITEM_RELATION_MISMATCH = "item_relation_mismatch"
REASON_NO_INVENTORY_READ = "no_inventory_read"
REASON_INVENTORY_READ_NOT_OK = "inventory_read_not_ok"
REASON_UNCOVERED_ARGUMENT = "uncovered_argument"
AUDIT_REASONS = (REASON_NO_ORDER_READ, REASON_ORDER_READ_NOT_OK, REASON_ITEM_NOT_IN_LATEST_ORDER_READ,
                 REASON_ITEM_RELATION_MISMATCH, REASON_NO_INVENTORY_READ, REASON_INVENTORY_READ_NOT_OK,
                 REASON_UNCOVERED_ARGUMENT)
# The gate codes each audit reason is consistent with (M1-A1 doc, section 2).
REASON_CODES = {
    REASON_NO_ORDER_READ: {MISSING_ORDER_OBSERVATION},
    REASON_ORDER_READ_NOT_OK: {STALE_OR_FAILED_OBSERVATION},
    REASON_ITEM_NOT_IN_LATEST_ORDER_READ: {TARGET_NOT_OBSERVED, STALE_OR_FAILED_OBSERVATION,
                                           TARGET_RELATION_MISMATCH},
    REASON_ITEM_RELATION_MISMATCH: {TARGET_RELATION_MISMATCH},
    REASON_NO_INVENTORY_READ: {MISSING_INVENTORY_OBSERVATION},
    REASON_INVENTORY_READ_NOT_OK: {STALE_OR_FAILED_OBSERVATION},
    REASON_UNCOVERED_ARGUMENT: {TARGET_RECONSTRUCTION_MISMATCH},
}

# Wrapper diagnostics (closed). Ids or names an unpaired observation claims are never echoed.
DIAGNOSTIC_UNEXPECTED_OBSERVATION_TYPE = "unexpected_observation_type"
DIAGNOSTIC_DUPLICATE_OBSERVATION_ID = "duplicate_observation_id"
DIAGNOSTIC_NO_FORWARDED_CALL = "observation_without_forwarded_call"
DIAGNOSTIC_CALL_MISMATCH = "observation_call_mismatch"
DIAGNOSTIC_NO_TOOL_RESULT = "observation_without_tool_result"
DIAGNOSTIC_RESULT_MISMATCH = "tool_result_call_mismatch"
DIAGNOSTIC_REGISTERED_CHANGED = "registered_observation_changed"
DIAGNOSTIC_REGISTRATION_FAILED = "observation_registration_failed"
DIAGNOSTIC_VISIBLE_SET_FAILED = "visible_set_unavailable"
DIAGNOSTIC_ACTION_INVALID = "action_contract_failed"

GET_ORDER, GET_INVENTORY = "get_order", "get_inventory"
ORDER_ID, ORDER_ITEM_ID, TARGET_SKU = "order_id", "order_item_id", "target_sku"
COVERED_ARGUMENTS = frozenset({ORDER_ID, ORDER_ITEM_ID, TARGET_SKU, "reason_code", "handoff_trigger"})
# argument -> the entity whose record_id it names
TARGET_ENTITIES = {ORDER_ID: "order", ORDER_ITEM_ID: "order_item", TARGET_SKU: "inventory"}


@dataclass(frozen=True, kw_only=True)
class GroundingDecisionRecord:
    """One grounding decision on one proposed action. Ids, codes and booleans only."""

    case_id: str
    round: int
    mode: str
    policy_run: int                    # 1 = the main run; 2.. = runner-created reruns, in order
    step: int
    action_name: str
    args_sha256: str
    grounded: bool
    code: str | None
    supports: tuple[str, ...]          # observation ids of the binding, in argument order
    prior_read_tools: tuple[str, ...]  # tool names of the run's observations before this decision
    attribution: str | None            # None when grounded
    audit: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {"case_id": self.case_id, "round": self.round, "mode": self.mode,
                "policy_run": self.policy_run, "step": self.step, "action_name": self.action_name,
                "args_sha256": self.args_sha256, "grounded": self.grounded, "code": self.code,
                "supports": list(self.supports), "prior_read_tools": list(self.prior_read_tools),
                "attribution": self.attribution, "audit": _plain(self.audit)}


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class _ForwardedCall:
    call: ToolCall
    step: int
    turn_index: int
    tool_step: int
    observation_id: str


@dataclass(frozen=True)
class _ReadView:
    """The audit's own structured view of one paired read. Never serialized as such."""

    observation_id: str
    tool_name: str
    arguments: tuple[tuple[str, str], ...]
    status: str                        # ok / empty / error, or "malformed" (no ToolResult)
    well_formed: bool
    records: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...]   # entity, record_id, relations

    @property
    def usable(self) -> bool:
        return self.status == ToolStatus.OK.value and self.well_formed

    def argument(self, name: str) -> str | None:
        return dict(self.arguments).get(name)

    def record(self, entity: str, record_id: str) -> dict[str, str] | None:
        for kind, identifier, relations in self.records:
            if kind == entity and identifier == record_id:
                return dict(relations)
        return None


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_view(forwarded: _ForwardedCall, result: ToolResult | None) -> _ReadView:
    """Re-read a result's structured evidence independently of the product ledger."""
    call = forwarded.call
    records: dict[tuple[str, str], tuple] = {}
    well_formed = result is not None
    for item in (() if result is None else result.evidence):
        metadata = getattr(item, "metadata", None)
        relations = getattr(item, "relations", None)
        if (type(item) is not BusinessEvidence or not isinstance(metadata, Mapping)
                or not _nonblank(metadata.get("entity")) or not _nonblank(metadata.get("record_id"))
                or metadata.get("tool") != call.tool_name
                or metadata.get(OBSERVATION_ID_KEY) != forwarded.observation_id
                or not isinstance(relations, Mapping)
                or not all(_nonblank(key) and _nonblank(value) for key, value in relations.items())):
            well_formed = False
            break
        entry = (metadata["entity"], metadata["record_id"], item.state_version,
                 tuple(sorted(relations.items())))
        if records.setdefault((entry[0], entry[1]), entry) != entry:
            well_formed = False
            break
    return _ReadView(
        observation_id=forwarded.observation_id, tool_name=call.tool_name,
        arguments=tuple(sorted(call.arguments.items())),
        status="malformed" if result is None else ToolStatus(result.status).value,
        well_formed=well_formed,
        records=tuple((entity, record_id, relations) for entity, record_id, _, relations in records.values())
        if well_formed else ())


def _audit(action: ValidatedAction, reads: tuple[_ReadView, ...], texts: tuple[str, ...],
           provenance_complete: bool) -> dict[str, object]:
    """Re-check m1-grounding/1 over the paired reads; keep only booleans and closed strings."""
    args = dict(action.args)
    order_id, item_id = args[ORDER_ID], args[ORDER_ITEM_ID]
    order_reads = [read for read in reads if read.tool_name == GET_ORDER and read.argument(ORDER_ID) == order_id]

    def order_and_item(read: _ReadView) -> bool:
        if not read.usable or read.record("order", order_id) is None:
            return False
        item = read.record("order_item", item_id)
        return item is not None and item.get(ORDER_ID) == order_id

    reason = None
    if not order_reads:
        reason = REASON_NO_ORDER_READ
    else:
        latest = order_reads[-1]
        item = latest.record("order_item", item_id)
        if not latest.usable or latest.record("order", order_id) is None:
            reason = REASON_ORDER_READ_NOT_OK
        elif item is None:
            reason = REASON_ITEM_NOT_IN_LATEST_ORDER_READ
        elif item.get(ORDER_ID) != order_id:
            reason = REASON_ITEM_RELATION_MISMATCH
    exchange = TARGET_SKU in args
    inventory_reads = [read for read in reads
                       if exchange and read.tool_name == GET_INVENTORY and read.argument("sku") == args[TARGET_SKU]]
    if reason is None and exchange:
        if not inventory_reads:
            reason = REASON_NO_INVENTORY_READ
        elif not inventory_reads[-1].usable or inventory_reads[-1].record("inventory", args[TARGET_SKU]) is None:
            reason = REASON_INVENTORY_READ_NOT_OK
    if reason is None and set(args) - COVERED_ARGUMENTS:
        reason = REASON_UNCOVERED_ARGUMENT
    sources = {}
    for name, entity in TARGET_ENTITIES.items():
        if name in args:
            value = args[name]
            sources[name] = {"in_user_text": any(value in text for text in texts),
                             "in_some_read": any(read.record(entity, value) is not None for read in reads)}
    return {
        "rule_version": "m1-grounding/1",
        "provenance_complete": provenance_complete,
        "rule_satisfied": reason is None,
        "reason": reason,
        "order_reads": len(order_reads),
        "earlier_order_read_had_target": any(order_and_item(read) for read in order_reads[:-1]),
        "inventory_required": exchange,
        "inventory_reads": len(inventory_reads),
        "value_sources": sources,
        "reads": [{"observation_id": read.observation_id, "tool_name": read.tool_name,
                   "status": read.status, "well_formed": read.well_formed} for read in reads],
    }


class GroundingGatedPolicy:
    """A Stage 6 control policy: the inner policy plus the M1-A1 grounding gate.

    `decision_records` is the inner policy's own record stream, unchanged, so
    the frozen runner and scorer see exactly the model calls that were made.
    The grounding records are `grounding_decisions`; wrapper diagnostics are
    `diagnostics`.
    """

    def __init__(self, inner: ActionControlPolicy, *, mode: str, case_id: str, round: int,
                 policy_run: int = 1) -> None:
        if mode not in MODES:
            raise ValueError("mode must be one of: " + ", ".join(MODES))
        if not isinstance(inner, ActionControlPolicy):
            raise TypeError("inner must implement next_action(state)")
        if not _nonblank(case_id):
            raise ValueError("case_id must be a non-empty string")
        if any(type(value) is not int or value < 1 for value in (round, policy_run)):
            raise ValueError("round and policy_run must be positive integers")
        self.inner = inner
        self.mode = mode
        self.case_id = case_id
        self.round = round
        self.policy_run = policy_run
        self.grounding_decisions: list[GroundingDecisionRecord] = []
        self.diagnostics: list[dict[str, object]] = []
        self._ledger = ObservationLedger("m1-a2:" + case_id + ":" + str(policy_run))
        self._forwarded: dict[str, _ForwardedCall] = {}
        self._views: dict[str, _ReadView] = {}
        self._tool_steps = 0
        self._diagnosed: set[tuple] = set()

    @property
    def decision_records(self):
        return getattr(self.inner, "decision_records", ())

    def _diagnose(self, code: str, step: int, forwarded: _ForwardedCall | None = None) -> None:
        observation_id = None if forwarded is None else forwarded.observation_id
        key = (code, observation_id)
        if key not in self._diagnosed:
            self._diagnosed.add(key)
            self.diagnostics.append({"case_id": self.case_id, "round": self.round, "mode": self.mode,
                                     "policy_run": self.policy_run, "step": step, "code": code,
                                     "observation_id": observation_id})

    def _pair(self, observation: object, step: int) -> tuple[_ForwardedCall | None, ToolResult | None, bool]:
        """(the forwarded call, its ToolResult, paired) for one observation of the state."""
        if type(observation) not in (ToolObservation, ToolContractFailure):
            self._diagnose(DIAGNOSTIC_UNEXPECTED_OBSERVATION_TYPE, step)
            return None, None, False
        forwarded = self._forwarded.get(observation.observation_id)
        if forwarded is None:
            self._diagnose(DIAGNOSTIC_NO_FORWARDED_CALL, step)
            return None, None, False
        if (observation.tool_name != forwarded.call.tool_name
                or dict(observation.arguments) != dict(forwarded.call.arguments)
                or observation.control_step != forwarded.step
                or observation.turn_index != forwarded.turn_index
                or observation.tool_step != forwarded.tool_step):
            self._diagnose(DIAGNOSTIC_CALL_MISMATCH, step, forwarded)
            return forwarded, None, False
        if type(observation) is ToolContractFailure:
            self._diagnose(DIAGNOSTIC_NO_TOOL_RESULT, step, forwarded)
            return forwarded, None, False
        result = observation.result
        trace = getattr(result, "trace", None)
        if (type(result) is not ToolResult or result.tool_name != forwarded.call.tool_name
                or not isinstance(trace, Mapping)
                or trace.get(TRACE_OBSERVATION_ID) != forwarded.observation_id):
            self._diagnose(DIAGNOSTIC_RESULT_MISMATCH, step, forwarded)
            return forwarded, None, False
        return forwarded, result, True

    def _fix_visible(self, state: ActionControlState) -> tuple[VisibleObservations, tuple[_ReadView, ...], bool]:
        """Register the newly paired reads, then fix what this decision can see."""
        step = state.step_number
        complete = True
        views: list[_ReadView] = []
        ids = [getattr(observation, "observation_id", None) for observation in state.observations]
        if len(set(ids)) != len(ids):
            self._diagnose(DIAGNOSTIC_DUPLICATE_OBSERVATION_ID, step)
            complete = False
        for observation in state.observations:
            forwarded, result, paired = self._pair(observation, step)
            if forwarded is not None and type(observation) is ToolContractFailure and not paired:
                views.append(_read_view(forwarded, None))   # audit only: a read without a result
            if not paired:
                complete = False
                continue
            view = _read_view(forwarded, result)
            views.append(view)
            known = self._views.get(forwarded.observation_id)
            if known is not None:
                if known != view:
                    self._diagnose(DIAGNOSTIC_REGISTERED_CHANGED, step, forwarded)
                    complete = False
                continue
            try:
                self._ledger.register(run_index=LEDGER_RUN_INDEX, observation_id=forwarded.observation_id,
                                      tool_name=forwarded.call.tool_name, arguments=forwarded.call.arguments,
                                      result=result)
            except ProvenanceError:
                self._diagnose(DIAGNOSTIC_REGISTRATION_FAILED, step, forwarded)
                complete = False
                continue
            self._views[forwarded.observation_id] = view
        if complete:
            try:
                return self._ledger.visible_to(run_index=LEDGER_RUN_INDEX, observation_ids=ids), tuple(views), True
            except (ProvenanceError, TypeError):
                self._diagnose(DIAGNOSTIC_VISIBLE_SET_FAILED, step)
        # Fail-closed: an observation without established provenance hides every read.
        return self._ledger.visible_to(run_index=LEDGER_RUN_INDEX, observation_ids=()), tuple(views), False

    def next_action(self, state: ActionControlState) -> Stage6Action:
        # Fixed before the inner policy is asked: reads made later can never ground this decision.
        visible, views, complete = self._fix_visible(state)
        texts = tuple(message.text for message in state.user_messages)
        prior_reads = tuple(observation.tool_name for observation in state.observations)
        action = self.inner.next_action(state)
        if type(action) is ToolCall:
            self._tool_steps += 1
            turn_index = len(state.user_messages)
            observation_id = observation_id_for(turn_index, self._tool_steps)
            self._forwarded[observation_id] = _ForwardedCall(
                call=action, step=state.step_number, turn_index=turn_index,
                tool_step=self._tool_steps, observation_id=observation_id)
            return action
        if type(action) is not ActionIntent:
            return action
        validator = ActionIntentValidator(build_action_registry(), state.allowed_actions)
        try:
            validated = validator.validate(action.action_name, action.arguments)
        except ActionValidationError:
            # The frozen runner handles an invalid intent exactly as without the wrapper.
            self._diagnose(DIAGNOSTIC_ACTION_INVALID, state.step_number)
            return action
        audit = _audit(validated, views, texts, complete)
        grounded, code, supports = True, None, ()
        try:
            binding = ground_action(validated, visible)
            supports = tuple(dict.fromkeys(item.observation_id for item in binding.supports))
        except GroundingRejected as rejection:
            grounded, code = False, rejection.code
        # The independent re-check must agree with the gate both ways: a rejection the
        # rules allow is a false rejection, an admission the rules forbid is a defect too.
        audit["gate_agrees"] = None if not complete else grounded == audit["rule_satisfied"]
        attribution = None
        if not grounded:
            attribution = (ATTRIBUTION_FAIL_CLOSED if not complete
                           else ATTRIBUTION_FALSE if audit["rule_satisfied"] else ATTRIBUTION_TRUE)
            audit["code_consistent"] = (attribution != ATTRIBUTION_TRUE
                                        or code in REASON_CODES[audit["reason"]])
        self.grounding_decisions.append(GroundingDecisionRecord(
            case_id=self.case_id, round=self.round, mode=self.mode, policy_run=self.policy_run,
            step=state.step_number, action_name=validated.action_name,
            args_sha256=validated.args_sha256, grounded=grounded, code=code, supports=supports,
            prior_read_tools=prior_reads, attribution=attribution, audit=audit))
        if self.mode == MODE_ENFORCE and not grounded:
            return Finish(disposition=REJECTION_DISPOSITION)
        return action
