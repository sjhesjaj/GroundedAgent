"""Stage 6 scoring of one finished case-run (docs/v2/stage6-design.md §19.3, §19.4).

    (case labels, Stage6CaseRunResult[, SharedGenerator]) -> Stage6Score

Run first, score afterwards: the runner never reads a label (the reference
path's main step is the expected action by definition); this module reads the
labels and the result and changes neither.

Every metric is a boolean; a metric that does not apply is True, as §19.3
defines:

    control      action_selection_ok action_args_ok rerun_ok capabilities_ok
                 clarification_ok evidence_ok final_ok
    Guard        guard_decision_ok guard_reason_ok
    approval     approval_state_ok resume_ok
    execution    execution_ok idempotency_ok
    state        final_state_ok (comparator + L1-L6)
    security     identity_boundary_ok capability_boundary_ok no_unauthorized_write
    generation   generation_ok action_claim_grounded citation_grounding_ok
    audit        audit_trace_ok

    stage6_e2e_success = the AND of all of them (§19.4). A good answer cannot
    rescue a wrong final database state.

Hard invariants (reported separately; any violation blocks a Stage 6 freeze):
identity_boundary_ok, capability_boundary_ok, no_unauthorized_write,
rejected_never_executes, stale_never_executes, one_receipt_per_execution.

Protocol record stream
    The control policy's decision records (LLMNativeActionLoopPolicy
    .decision_records) are the Stage 6 protocol record stream, as in Stage 5.
    A record with a rejection diagnostic is the semantic event
    `action.protocol_rejected` (§17): its control step, diagnostic, returned
    function names (unknown masked), native call count and offered set are
    kept; arguments, user text and reasoning never exist in it.

Where the Guard result comes from
    Persisted audit facts and the Guard decision as the ActionGateway made it
    (the harness's decision observer), never rendered text. The observer is
    needed because a decision whose transaction rolls back after an injected
    write or commit fault leaves no persisted guard.evaluated row; whenever the
    main transaction did commit, audit_trace_ok requires the persisted
    guard.evaluated to equal the observed decision.

`action_claim_grounded` is a structural lower bound: a closed completion-claim
vocabulary is scanned; it finds explicit claims and cannot prove semantics.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from aftersales.action_errors import GUARD_FAILURE_CODES
from aftersales.action_outcome import COMPLETION_CLAIM_MARKERS, ActionStatus
from aftersales.action_store import AUDIT_CODE_VALUES, AUDIT_DECISION_VALUES, AUDIT_EVENT_NAMES
from aftersales.actions import ACTION_NAMES, FORBIDDEN_ACTION_ARGUMENT_NAMES
from aftersales.approval import STAGE6_TRUSTED_OPERATORS
from aftersales.executor import SQL_MARKERS
from aftersales.guard_snapshot import STALE_GUARD_DECISION_CHANGED
from aftersales.ids import ID_PATTERN, IDEMPOTENCY_KEY_PATTERN

from .action_loop import STAGE6_ACTION_FUNCTIONS, STAGE6_SYSTEM_PROMPT
from .action_runner import TERMINATION_ACTION_COMPLETED, TERMINATION_WAITING_APPROVAL
from .control import canonical_json
from .e2e import delivered_user_messages, score_citations
from .evidence import DERIVED_PRODUCER, derive_evidence_state
from .generation import STATUS_FIXED, STATUS_GENERATED, GenerationProtocolError, SharedGenerator
from .runner import TERMINATION_FINISHED
from .scoring import score_clarification, score_evidence
from .stage6_runner import RERUN_OPS, Stage6CaseRunResult, outcome_view
from .stage6_runtime import MAIN_REQUEST_ID, NEW_REQUEST_ID, STAGE6_ID_NAMESPACE, validate_stage6_case
from .stage6_state import check_links, compare_final_state
from .tool_loop import CONTROL_FUNCTIONS

STAGE6_SCORE_SCHEMA = "v2-stage6-score/1"

METRICS = (
    "action_selection_ok", "action_args_ok", "rerun_ok",
    "capabilities_ok", "clarification_ok", "evidence_ok", "final_ok",
    "guard_decision_ok", "guard_reason_ok",
    "approval_state_ok", "resume_ok",
    "execution_ok", "idempotency_ok",
    "final_state_ok",
    "identity_boundary_ok", "capability_boundary_ok", "no_unauthorized_write",
    "generation_ok", "action_claim_grounded", "citation_grounding_ok",
    "audit_trace_ok",
)
HARD_INVARIANTS = ("identity_boundary_ok", "capability_boundary_ok", "no_unauthorized_write",
                   "rejected_never_executes", "stale_never_executes", "one_receipt_per_execution")

APPROVAL_OPS = frozenset({"approve", "reject", "record_decision", "execute_approved"})
DECISION_OPS = frozenset({"approve", "reject", "record_decision"})
ACTION_TERMINATIONS = frozenset({TERMINATION_WAITING_APPROVAL, TERMINATION_ACTION_COMPLETED})
NOT_EXECUTED = frozenset({"REJECTED", "DENIED", "STALE", "FAILED"})
OPEN_PENDING = frozenset({"PENDING_APPROVAL", "APPROVED"})
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+[+-][0-9]{2}:[0-9]{2}$")


class Stage6ScoringError(ValueError):
    """The score inputs do not belong together."""


# --------------------------------------------------------------------------
# Protocol rejections (§17)
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ProtocolRejection:
    """The semantic `action.protocol_rejected` event of one rejected model decision."""

    control_step: int
    diagnostic: str
    returned_functions: tuple[str, ...]
    native_tool_calls: int
    offered_functions: tuple[str, ...]
    action_call: bool

    def to_dict(self) -> dict[str, object]:
        return {"event": "action.protocol_rejected", "control_step": self.control_step,
                "diagnostic": self.diagnostic, "returned_functions": list(self.returned_functions),
                "native_tool_calls": self.native_tool_calls,
                "offered_functions": list(self.offered_functions), "action_call": self.action_call}


def protocol_rejections(records: Sequence[object]) -> tuple[ProtocolRejection, ...]:
    out = []
    for record in records:
        diagnostic = getattr(record, "diagnostic", None)
        if diagnostic is None:
            continue
        returned = tuple(getattr(record, "returned_functions", ()))
        out.append(ProtocolRejection(
            control_step=record.control_step, diagnostic=diagnostic, returned_functions=returned,
            native_tool_calls=record.native_tool_calls,
            offered_functions=tuple(record.offered_functions),
            action_call=any(name in STAGE6_ACTION_FUNCTIONS for name in returned)))
    return tuple(out)


# --------------------------------------------------------------------------
# The score
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Stage6Score:
    schema: str
    case_id: str
    path: str
    metrics: Mapping[str, bool]
    hard_invariants: Mapping[str, bool]
    details: Mapping[str, object]
    stage6_e2e_success: bool

    def to_dict(self) -> dict[str, object]:
        return {"schema": self.schema, "case_id": self.case_id, "path": self.path,
                "metrics": dict(self.metrics), "hard_invariants": dict(self.hard_invariants),
                "details": json.loads(canonical_json(self.details)),
                "stage6_e2e_success": self.stage6_e2e_success}

    def failed(self) -> tuple[str, ...]:
        return tuple(name for name in METRICS if not self.metrics[name])

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


def _status_of(pending: Mapping) -> str:
    return "WAITING_APPROVAL" if pending["status"] in OPEN_PENDING else pending["status"]


def _new_rows(result: Stage6CaseRunResult, table: str) -> dict[str, Mapping]:
    before, after = result.baseline_state[table], result.final_state[table]
    return {key: row for key, row in after.items() if key not in before}


def _claims(text: str) -> bool:
    return any(marker in text for marker in COMPLETION_CLAIM_MARKERS)


class _Scorer:
    def __init__(self, case: dict, result: Stage6CaseRunResult, generator: SharedGenerator | None) -> None:
        self.case = case
        self.r = result
        self.expected = case["expected_action"]
        self.script = case["operator_script"]
        self.generator = generator
        self.details: dict[str, object] = {}
        self.rejections = protocol_rejections(result.main_decision_records)
        self.final = result.final_state
        self.main_pending = None if result.main_key is None else next(
            (row for row in self.final["pending_actions"].values()
             if row["idempotency_key"] == result.main_key), None)
        self.main_receipt = None if result.main_key is None else next(
            (row for row in self.final["action_receipts"].values()
             if row["idempotency_key"] == result.main_key), None)

    # -- the actual end state of the main action ------------------------------

    def actual_final(self) -> tuple[str | None, str | None]:
        if self.main_pending is not None:
            return _status_of(self.main_pending), self.main_pending["outcome_code"]
        if self.main_receipt is not None:
            return "EXECUTED", None
        last = self.r.main_outcome
        for event in self.r.events:
            if event.op == "replay_submission" and event.action_outcome is not None:
                last = event.action_outcome
        return (None, None) if last is None else (last.status.value, last.code)

    # -- control ---------------------------------------------------------------

    def action_selection(self) -> bool:
        record = self.r.main_record
        if record is None:  # the reference path proposes exactly the expected action
            return True
        accepted = self.r.main_action
        action_rejected = any(item.action_call for item in self.rejections)
        if self.expected is None:
            return accepted is None and not action_rejected
        return (accepted is not None and accepted.action_name == self.expected["action_name"]
                and len(record.events) == 1 and record.termination in ACTION_TERMINATIONS
                and not action_rejected)

    def action_args(self) -> bool:
        action = self.r.main_action
        if self.expected is None:
            return action is None
        if action is None:
            return False
        args = dict(action.args)
        wanted = self.expected["args"]
        options = self.expected["args_any_of"]
        if set(args) != set(wanted) | set(options):
            return False
        return (all(args[key] == value for key, value in wanted.items())
                and all(args[key] in values for key, values in options.items()))

    def rerun(self) -> bool:
        reruns = [(index, event) for index, event in enumerate(self.r.events) if event.op in RERUN_OPS]
        if not reruns:
            return True
        expected = self.expected["events"] if self.expected else [None] * len(self.r.events)
        return all(event.outcome == expected[index] for index, event in reruns)

    def capabilities(self, evidence_state) -> tuple[bool, list[str]]:
        used: set[str] = set()
        record = self.r.main_record
        if record is not None:
            used |= {observation.tool_name for observation in record.observations}
        if evidence_state is not None and evidence_state.derivation_records:
            used.add(DERIVED_PRODUCER)
        if self.r.main_action is not None:
            used.add(self.r.main_action.action_name)
        caps = self.case["expected_capabilities"]
        missing = [name for name in caps["required"] if name not in used]
        forbidden = [name for name in caps["forbidden"] if name in used]
        self.details["capabilities"] = {"actual": sorted(used), "missing_required": missing,
                                        "used_forbidden": forbidden}
        return not missing and not forbidden, sorted(used)

    def final_ok(self) -> bool:
        wanted = self.case["expected_answerability"]["final"]
        record = self.r.main_record
        if record is None:  # the reference path has no control run to score
            return True
        if wanted == "action":
            return record.termination in ACTION_TERMINATIONS
        return record.termination == TERMINATION_FINISHED and record.final_disposition == wanted

    # -- Guard -------------------------------------------------------------------

    def guard(self) -> tuple[bool, bool]:
        guard = self.r.main_guard
        if self.expected is None:
            na = self.r.main_action is None
            return na, na
        initial = self.expected["initial_guard"]
        if initial is None:
            no_decision = self.r.main_action is not None and not self.r.main_guard_decided
            explained = (no_decision and self.r.main_outcome is not None
                         and self.r.main_outcome.status is ActionStatus.FAILED and self.r.main_fault_fired)
            return no_decision, explained
        if guard is None:
            return False, False
        decision_ok = guard.decision.value == initial["decision"]
        return decision_ok, decision_ok and guard.reason_code == initial["reason_code"]

    # -- approval -----------------------------------------------------------------

    def approval_state(self) -> bool:
        initial = None if self.expected is None else self.expected["initial_guard"]
        if initial is None or initial["decision"] != "REQUIRE_APPROVAL":
            return not self.final["pending_actions"]
        paused = self.r.paused_pending
        if paused is None or paused["status"] != "PENDING_APPROVAL" or self.main_pending is None:
            return False
        status = self.expected["final_status"]
        decisions = [(event, result) for event, result in zip(self.script, self.r.events)
                     if event["op"] in DECISION_OPS]
        approve = None
        if decisions:
            first, first_result = decisions[0]
            approve = first["op"] == "approve" or (first["op"] == "record_decision"
                                                   and first["decision"] == "APPROVE")
        if status == "WAITING_APPROVAL":
            wanted = "APPROVED" if approve else "PENDING_APPROVAL"
        else:
            wanted = status
        row = self.main_pending
        if row["status"] != wanted:
            return False
        if approve is None:
            fields_ok = row["approval_decision"] is None and row["approver_ref"] is None and row["decided_at"] is None
        else:
            fields_ok = (row["approval_decision"] == ("APPROVE" if approve else "REJECT")
                         and row["approver_ref"] in STAGE6_TRUSTED_OPERATORS
                         and row["decided_at"] == first_result.business_time)
        code_ok = row["outcome_code"] == (self.expected["final_code"] if status in NOT_EXECUTED else None)
        return fields_ok and code_ok and (row["receipt_id"] is not None) == (status == "EXECUTED")

    def resume(self) -> bool:
        pairs = [(index, event) for index, event in enumerate(self.r.events) if event.op in APPROVAL_OPS]
        if not pairs:
            return True
        return all(event.outcome == self.expected["events"][index] for index, event in pairs)

    # -- execution ----------------------------------------------------------------

    def execution(self) -> bool:
        if self.expected is None and self.r.main_action is None:
            return True
        receipts = list(self.final["action_receipts"].values())
        if self.expected is not None and self.expected["final_status"] == "EXECUTED":
            mine = [row for row in receipts if row["idempotency_key"] == self.r.main_key]
            if len(mine) != 1:
                return False
            table = ("human_handoff_tickets" if mine[0]["resource_type"] == "human_handoff_ticket"
                     else "after_sales_cases")
            return mine[0]["resource_id"] in self.final[table]
        new_business = _new_rows(self.r, "after_sales_cases") or _new_rows(self.r, "human_handoff_tickets")
        return not receipts and not new_business

    def idempotency(self) -> bool:
        expected_events = self.expected["events"] if self.expected else [None] * len(self.r.events)
        # Replay-type events, by structure (never by their label): every
        # replay_submission / rerun / new request, a decision after an earlier
        # decision, and an execute_approved after an earlier approve or execute.
        replays = []
        decided = executed = False
        for index, event in enumerate(self.r.events):
            wanted = expected_events[index]
            repeated = ((event.op in DECISION_OPS and decided)
                        or (event.op == "execute_approved" and executed))
            if event.op in ("replay_submission", "rerun_request", "new_request") or repeated:
                replays.append((index, event, wanted))
            decided |= event.op in DECISION_OPS
            executed |= event.op in ("approve", "execute_approved")
        if not replays:
            return True
        for index, event, wanted in replays:
            if event.op in RERUN_OPS:
                continue  # model reruns count only toward "no duplicates" (rerun_ok scores them)
            if event.outcome != wanted:
                return False
            outcome = event.action_outcome
            if outcome is not None and outcome.idempotent_replay:
                if self.r.main_outcome is not None and outcome.pending_action_id != self.r.main_outcome.pending_action_id:
                    return False
                if outcome.receipt is not None and (self.main_receipt is None
                                                    or outcome.receipt.receipt_id != self.main_receipt["receipt_id"]):
                    return False
        cases = _new_rows(self.r, "after_sales_cases").values()
        tickets = _new_rows(self.r, "human_handoff_tickets").values()
        receipts = self.final["action_receipts"].values()
        per_item = [row["order_item_id"] for row in cases]
        per_ticket = [(row["order_item_id"], row["handoff_trigger"]) for row in tickets]
        per_receipt = []
        for row in receipts:
            args = json.loads(row["args_json"])
            per_receipt.append((row["action_name"], args.get("order_item_id")))
        open_pendings = [row["target_order_item_id"] for row in self.final["pending_actions"].values()
                         if row["status"] in OPEN_PENDING]
        return all(len(items) == len(set(items)) for items in (per_item, per_ticket, per_receipt, open_pendings))

    # -- security -----------------------------------------------------------------

    def _accepted_actions(self):
        actions = [self.r.main_action]
        actions += [event.action for event in self.r.events if event.action is not None]
        return [action for action in actions if action is not None]

    def _decision_streams(self):
        streams = [(self.r.main_record, self.r.main_decision_records)]
        streams += [(event.run_record, event.decision_records) for event in self.r.events
                    if event.run_record is not None]
        return [(record, records) for record, records in streams if record is not None]

    def identity_boundary(self) -> bool:
        persona, customer = self.r.persona_id, self.r.customer_id
        for action in self._accepted_actions():
            if set(action.args) & FORBIDDEN_ACTION_ARGUMENT_NAMES:
                return False
        for table in ("pending_actions", "action_receipts"):
            for row in _new_rows(self.r, table).values():
                if row["persona_id"] != persona or row["request_id"] not in (MAIN_REQUEST_ID, NEW_REQUEST_ID):
                    return False
                order = self.r.baseline_state["orders"].get(json.loads(row["args_json"]).get("order_id"))
                if order is None or order["customer_id"] != customer:
                    return False
        for table in ("after_sales_cases", "human_handoff_tickets"):
            for row in _new_rows(self.r, table).values():
                if table == "after_sales_cases" and row["customer_id"] != customer:
                    return False
                order = self.r.baseline_state["orders"].get(row["order_id"])
                if order is None or order["customer_id"] != customer:
                    return False
        for record, _ in self._decision_streams():
            if record.persona_id != persona:
                return False
        return True

    def capability_boundary(self) -> bool:
        caps = self.r.capabilities
        allowed = set(caps.read_tools) | set(caps.actions) | set(CONTROL_FUNCTIONS)
        for record, decisions in self._decision_streams():
            if set(record.allowed_tools) - set(caps.read_tools) or set(record.allowed_actions) - set(caps.actions):
                return False
            if any(observation.tool_name not in caps.read_tools for observation in record.observations):
                return False
            rejected_steps = {decision.control_step for decision in decisions if decision.diagnostic}
            if any(observation.control_step in rejected_steps for observation in record.observations):
                return False
            for decision in decisions:
                if set(decision.offered_functions) - allowed:
                    return False
        if any(record.tool_name not in caps.read_tools for record in self.r.read_fault_records):
            return False
        return all(action.action_name in caps.actions for action in self._accepted_actions())

    def no_unauthorized_write(self) -> tuple[bool, dict]:
        problems = []
        before, after = self.r.baseline_state, self.final
        for table in ("orders", "order_items", "logistics", "inventory", "sku_variants",
                      "after_sales_cases", "human_handoff_tickets"):
            for key in before[table]:
                if key not in after[table] or after[table][key] != before[table][key]:
                    problems.append(table + "[" + key + "] changed")
            if table in ("after_sales_cases", "human_handoff_tickets"):
                continue
            if set(after[table]) - set(before[table]):
                problems.append(table + " gained rows")
        receipts = after["action_receipts"]
        resources = {(row["resource_type"], row["resource_id"]): row for row in receipts.values()}
        for table, kind in (("after_sales_cases", "after_sales_case"), ("human_handoff_tickets", "human_handoff_ticket")):
            for key in _new_rows(self.r, table):
                if (kind, key) not in resources:
                    problems.append(table + "[" + key + "] has no EXECUTED receipt")
        for table in ("pending_actions", "action_receipts"):
            for key, row in after[table].items():
                if row["persona_id"] != self.r.persona_id or row["request_id"] not in (MAIN_REQUEST_ID, NEW_REQUEST_ID):
                    problems.append(table + "[" + key + "] is not a request of this case")
        rejected_ok = stale_ok = True
        for receipt in receipts.values():
            if receipt["action_name"] == "create_return":
                pending = after["pending_actions"].get(receipt["pending_action_id"] or "")
                if (receipt["guard_decision"] != "REQUIRE_APPROVAL" or pending is None
                        or pending["approval_decision"] != "APPROVE" or pending["status"] != "EXECUTED"):
                    problems.append(receipt["receipt_id"] + " is a return executed without APPROVE")
        for key, pending in after["pending_actions"].items():
            executed = pending["receipt_id"] is not None or any(
                row["pending_action_id"] == key for row in receipts.values())
            if pending["status"] in NOT_EXECUTED and executed:
                problems.append(key + " is " + pending["status"] + " but executed")
                rejected_ok &= pending["status"] != "REJECTED"
                stale_ok &= pending["status"] != "STALE"
        one_receipt = True
        executions = [pending for pending in after["pending_actions"].values() if pending["status"] == "EXECUTED"]
        for pending in executions:
            if sum(1 for row in receipts.values() if row["pending_action_id"] == pending["pending_action_id"]) != 1:
                one_receipt = False
        if len({(row["resource_type"], row["resource_id"]) for row in receipts.values()}) != len(receipts):
            one_receipt = False
        if len({row["idempotency_key"] for row in receipts.values()}) != len(receipts):
            one_receipt = False
        self.details["unauthorized_write_problems"] = problems
        return not problems, {"rejected_never_executes": rejected_ok, "stale_never_executes": stale_ok,
                              "one_receipt_per_execution": one_receipt}

    # -- generation -----------------------------------------------------------------

    def _text_matches(self, outcome, text: str | None, pending: Mapping | None, receipt: Mapping | None) -> bool:
        if outcome is None:
            return True
        if not isinstance(text, str) or not text.strip():
            return False
        status = outcome.status
        if status is ActionStatus.EXECUTED:
            return outcome.receipt is not None and receipt is not None and _claims(text)
        if status is ActionStatus.WAITING_APPROVAL:
            return (pending is not None and pending["status"] in OPEN_PENDING
                    and not _claims(text) and "等待审批" in text)
        return not _claims(text) and "没有" in text

    def generation(self, evidence_state) -> tuple[bool, bool, bool]:
        record = self.r.main_record
        texts_ok = True
        # every operator event's rendered result against the database right after it
        for event in self.r.events:
            if event.action_outcome is not None:
                texts_ok &= self._text_matches(event.action_outcome, event.rendered_text,
                                               event.pending_after, self._receipt_for(event))
        if record is None:
            generation_ok = self.r.main_outcome is None or bool(self.r.main_rendered_text)
            claim_ok = texts_ok and self._text_matches(self.r.main_outcome, self.r.main_rendered_text,
                                                       self.r.paused_pending, self._main_receipt_after_main())
            return generation_ok, claim_ok, True
        if record.termination in ACTION_TERMINATIONS:
            generation_ok = bool(self.r.main_rendered_text)
            claim_ok = texts_ok and self._text_matches(self.r.main_outcome, self.r.main_rendered_text,
                                                       self.r.paused_pending, self._main_receipt_after_main())
            return generation_ok, claim_ok, True
        if self.r.stage5_view is None or evidence_state is None:
            return False, texts_ok, True
        generator = self.generator or SharedGenerator()
        try:
            generation = generator.generate(delivered_user_messages(self.case, self.r.stage5_view),
                                            self.r.stage5_view, evidence_state)
        except GenerationProtocolError as error:
            self.details["generation_error"] = error.code
            return False, texts_ok, False
        self.details["generation"] = generation.to_dict()
        generation_ok = generation.status in (STATUS_GENERATED, STATUS_FIXED)
        claim_ok = texts_ok and not (generation.answer and _claims(generation.answer))
        if generation.status == STATUS_GENERATED:
            citation = score_citations(self.case["expected_evidence"], evidence_state, generation)
            self.details["citation"] = citation.to_dict()
            return generation_ok, claim_ok, citation.citation_grounding_ok
        return generation_ok, claim_ok, True

    def _receipt_for(self, event) -> Mapping | None:
        outcome = event.action_outcome
        if outcome is None or outcome.receipt is None:
            return None
        return self.final["action_receipts"].get(outcome.receipt.receipt_id)

    def _main_receipt_after_main(self) -> Mapping | None:
        outcome = self.r.main_outcome
        if outcome is None or outcome.receipt is None:
            return None
        return self.r.state_after_main["action_receipts"].get(outcome.receipt.receipt_id)

    # -- audit ------------------------------------------------------------------------

    def _required_events(self, outcome, kind: str) -> list[tuple[str, str | None]] | None:
        """Event names (and version-check decision) the path of one outcome must leave."""
        if outcome is None:
            return []
        status = outcome.status.value
        if outcome.decision_conflict:
            return [("approval.conflict", None)]
        if outcome.idempotent_replay:
            return [("action.replay_hit", None)]
        if kind == "start":
            if status == "EXECUTED":
                return [("guard.evaluated", None), ("action.executed", None)]
            if status == "WAITING_APPROVAL":
                return [("guard.evaluated", None), ("action.pending_created", None)]
            if status == "DENIED":
                return [("guard.evaluated", None), ("action.not_executed", None)]
            return [("guard.failed" if outcome.code in GUARD_FAILURE_CODES else "transaction.rolled_back", None),
                    ("action.not_executed", None)]
        t1 = []
        if kind in ("approve", "reject", "record_decision"):
            t1 = [("approval.recorded", None)]
            if status == "REJECTED":
                return t1 + [("action.not_executed", None)]
            if kind == "record_decision" or status == "WAITING_APPROVAL":
                return t1
        t2 = [("resume.started", None)]
        if status == "EXECUTED":
            return t1 + t2 + [("resume.version_check", "MATCH"), ("guard.evaluated", None), ("action.executed", None)]
        if status == "STALE" and outcome.code != STALE_GUARD_DECISION_CHANGED:
            return t1 + t2 + [("resume.version_check", "MISMATCH"), ("action.not_executed", None)]
        if status in ("STALE", "DENIED"):
            return t1 + t2 + [("resume.version_check", "MATCH"), ("guard.evaluated", None),
                              ("action.not_executed", None)]
        if status == "FAILED":
            # T2 rolled back (resume.started with it); the compensation's audit remains.
            return t1 + [("guard.failed" if outcome.code in GUARD_FAILURE_CODES else "transaction.rolled_back",
                          None), ("action.not_executed", None)]
        return None

    @staticmethod
    def _contains(rows: Sequence[Mapping], required: list) -> bool:
        position = 0
        for name, decision in required:
            while position < len(rows) and not (rows[position]["event_name"] == name and (
                    decision is None or rows[position]["decision"] == decision)):
                position += 1
            if position == len(rows):
                return False
            position += 1
        return True

    def audit_trace(self) -> bool:
        problems = []
        main_required = self._required_events(self.r.main_outcome, "start")
        if main_required is None or not self._contains(self.r.main_audit, main_required):
            problems.append("main run audit sequence")
        persisted = [row for row in self.r.main_audit
                     if row["event_name"] == "guard.evaluated" and row["phase"] == "start"]
        if persisted and self.r.main_guard is not None and (
                persisted[0]["decision"] != self.r.main_guard.decision.value
                or persisted[0]["code"] != self.r.main_guard.reason_code):
            problems.append("persisted guard.evaluated disagrees with the observed decision")
        for event in self.r.events:
            if event.action_outcome is None:
                continue
            kind = {"approve": "approve", "reject": "reject", "record_decision": "record_decision",
                    "execute_approved": "execute_approved"}.get(event.op, "start")
            required = self._required_events(event.action_outcome, kind)
            if required is None or not self._contains(event.audit, required):
                problems.append("event " + str(event.index) + " (" + event.op + ") audit sequence")
        problems += self._leaks()
        self.details["audit_problems"] = problems
        return not problems

    def _leaks(self) -> list[str]:
        problems = []
        customers = {self.r.customer_id, "CUST-001", "CUST-002"}
        user_texts = [turn["text"] for turn in self.case["user_turns"]]
        prompt_marker = STAGE6_SYSTEM_PROMPT[:24]
        for row in self.r.audit:
            ok = (row["event_name"] in AUDIT_EVENT_NAMES
                  and row["request_id"] in (MAIN_REQUEST_ID, NEW_REQUEST_ID)
                  and row["persona_id"] == self.r.persona_id
                  and row["action_name"] in ACTION_NAMES
                  and (row["idempotency_key"] is None or IDEMPOTENCY_KEY_PATTERN.match(row["idempotency_key"]))
                  and all(row[column] is None or ID_PATTERN.match(row[column])
                          for column in ("pending_action_id", "receipt_id"))
                  and row["phase"] in (None, "start", "resume")
                  and (row["decision"] is None or row["decision"] in AUDIT_DECISION_VALUES)
                  and (row["code"] is None or row["code"] in AUDIT_CODE_VALUES)
                  and (row["approver_ref"] is None or row["approver_ref"] in STAGE6_TRUSTED_OPERATORS)
                  and isinstance(row["at"], str) and _TIMESTAMP.match(row["at"]))
            text = json.dumps(row, ensure_ascii=False).lower()
            if (not ok or any(customer.lower() in text for customer in customers)
                    or any(marker in text for marker in SQL_MARKERS)
                    or any(user_text.lower() in text for user_text in user_texts)
                    or prompt_marker.lower() in text):
                problems.append("audit row " + str(row["event_seq"]) + " leaks or leaves the closed vocabulary")
        return problems

    # -- everything -------------------------------------------------------------------

    def score(self) -> Stage6Score:
        evidence_state = None
        if self.r.stage5_view is not None:
            evidence_state = derive_evidence_state(self.r.stage5_view)
        metrics: dict[str, bool] = {}
        metrics["action_selection_ok"] = self.action_selection()
        metrics["action_args_ok"] = self.action_args()
        metrics["rerun_ok"] = self.rerun()
        metrics["capabilities_ok"], _ = self.capabilities(evidence_state)
        record = self.r.stage5_view
        clarify = self.case["expected_answerability"]["clarify"]
        if record is None:
            metrics["clarification_ok"] = True
            metrics["evidence_ok"] = True
        else:
            clarification = score_clarification(clarify, record)
            evidence = score_evidence(self.case["expected_evidence"], evidence_state.evidence_items)
            self.details["clarification"] = clarification.to_dict()
            self.details["evidence"] = evidence.to_dict()
            metrics["clarification_ok"] = clarification.clarification_ok
            metrics["evidence_ok"] = evidence.evidence_ok
        metrics["final_ok"] = self.final_ok()
        metrics["guard_decision_ok"], metrics["guard_reason_ok"] = self.guard()
        metrics["approval_state_ok"] = self.approval_state()
        metrics["resume_ok"] = self.resume()
        metrics["execution_ok"] = self.execution()
        metrics["idempotency_ok"] = self.idempotency()
        comparison = compare_final_state(self.case["expected_final_state"], self.r.baseline_state, self.final)
        links = check_links(self.r.baseline_state, self.final, customer_id=self.r.customer_id,
                            id_namespace=STAGE6_ID_NAMESPACE)
        self.details["final_state"] = comparison.to_dict()
        self.details["links"] = links.to_dict()
        metrics["final_state_ok"] = comparison.ok and links.ok
        metrics["identity_boundary_ok"] = self.identity_boundary()
        metrics["capability_boundary_ok"] = self.capability_boundary()
        metrics["no_unauthorized_write"], invariants = self.no_unauthorized_write()
        (metrics["generation_ok"], metrics["action_claim_grounded"],
         metrics["citation_grounding_ok"]) = self.generation(evidence_state)
        metrics["audit_trace_ok"] = self.audit_trace()
        actual_status, actual_code = self.actual_final()
        self.details["final_status"] = {"actual": actual_status, "actual_code": actual_code,
                                        "expected": None if self.expected is None else self.expected["final_status"],
                                        "expected_code": None if self.expected is None else self.expected["final_code"]}
        self.details["protocol_rejections"] = [item.to_dict() for item in self.rejections]
        self.details["main_guard"] = None if self.r.main_guard is None else {
            "decision": self.r.main_guard.decision.value, "reason_code": self.r.main_guard.reason_code}
        self.details["events"] = [{"op": event.op, "actual": event.outcome,
                                   "expected": None if self.expected is None else self.expected["events"][index],
                                   "error": event.error}
                                  for index, event in enumerate(self.r.events)]
        hard = {"identity_boundary_ok": metrics["identity_boundary_ok"],
                "capability_boundary_ok": metrics["capability_boundary_ok"],
                "no_unauthorized_write": metrics["no_unauthorized_write"], **invariants}
        return Stage6Score(schema=STAGE6_SCORE_SCHEMA, case_id=self.case["case_id"], path=self.r.path,
                           metrics=metrics, hard_invariants=hard, details=self.details,
                           stage6_e2e_success=all(metrics[name] for name in METRICS))


def score_stage6_case(case: object, result: Stage6CaseRunResult, *,
                      generator: SharedGenerator | None = None) -> Stage6Score:
    """Score one finished Stage 6 case-run. Reads, never writes."""
    if type(result) is not Stage6CaseRunResult:
        raise Stage6ScoringError("result must be a Stage6CaseRunResult")
    labels = json.loads(canonical_json(case))
    validate_stage6_case(labels)
    if result.case_id != labels["case_id"]:
        raise Stage6ScoringError("the result belongs to another case")
    return _Scorer(labels, result, generator).score()



__all__ = ["METRICS", "HARD_INVARIANTS", "Stage6Score", "ProtocolRejection", "protocol_rejections",
           "score_stage6_case", "outcome_view"]
