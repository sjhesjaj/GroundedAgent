"""The deterministic control-layer case runner (Stage 4.4.2).

One case-run, from a validated case to a raw control-layer record:

    case
    -> fresh V2CaseRuntime (contract validation, fixture, read-only DB)
    -> exactly one FaultInjectingGateway, faults or not
    -> first user turn
    -> policy.next_action(state) -> ToolCall | Clarify | Finish, one per step
    -> every ToolCall through the gateway; its result fed back as an observation
    -> Clarify answered only by a structurally matching conditional user turn
    -> deterministic termination: finished / unanswered_clarification /
       max_steps_exceeded
    -> runtime.assert_database_unchanged(), on every exit path
    -> CaseRunRecord
    -> runtime.close()

The runner records facts only. It does not read the case's expected_* labels,
archetype, or fault declarations; fault semantics belong to the gateway. There
is no derived-evidence engine, Evidence Policy, answer generation, scorer, LLM,
Planner, or dataset here.

Clarification
    Clarify(slots) is answered by the first not-yet-delivered conditional turn
    (user_turns[1:], in case order) whose on_clarify contains every requested
    slot. That turn is delivered once and never again. No match ends the run
    with termination "unanswered_clarification" - a scorable control result,
    not an error. The policy's wording plays no part: there is none.

Observation ids
    Generated here, structurally: case:<case_id>:turn:<TURN>:tool:<STEP>, where
    TURN is the latest delivered user message (from 1) and STEP counts tool
    calls across the whole case-run (from 1). No tool name, argument, user
    text, identity, UUID, random value, or clock reading.

Malformed results
    A malformed fault leaves no ToolResult: the executor raises ValueError. The
    runner turns that into ToolContractFailure(kind="malformed") only when the
    gateway's ledger gained exactly one record for this very observation_id
    with outcome injected_malformed. Every other ValueError - an unknown tool,
    bad arguments, a contract bug - and every FaultConfigurationError is
    re-raised.

Raw record
    `CaseRunRecord.to_dict()` is plain, deterministic JSON data: no durations,
    clock readings, ids beyond the structural ones, or object reprs. User text
    is stored only as its SHA-256; tool results via ToolResult.to_dict(); fault
    calls via FaultCallRecord.to_dict().
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from .control import (
    CONTRACT_FAILURE_MALFORMED,
    Clarify,
    ControlPolicy,
    ControlPolicyContractError,
    ControlState,
    Finish,
    Observation,
    ToolCall,
    ToolContractFailure,
    ToolObservation,
    UserMessage,
    canonical_json,
    require_action,
)
from .faults import (
    OUTCOME_INJECTED_MALFORMED,
    FaultCallRecord,
    FaultConfigurationError,
    FaultInjectingGateway,
)
from .runtime import EvalRuntimeError, V2CaseRuntime

CONTROL_RUN_SCHEMA = "v2-control-run/1"

# A pure safety ceiling. The experimental max_steps is always passed explicitly.
HARD_MAX_STEPS = 64

TERMINATION_FINISHED = "finished"
TERMINATION_UNANSWERED_CLARIFICATION = "unanswered_clarification"
TERMINATION_MAX_STEPS_EXCEEDED = "max_steps_exceeded"
TERMINATIONS = (TERMINATION_FINISHED, TERMINATION_UNANSWERED_CLARIFICATION,
                TERMINATION_MAX_STEPS_EXCEEDED)


def observation_id_for(case_id: str, turn_index: int, tool_step: int) -> str:
    """The structural id of one tool call. Nothing but these three values."""
    return "case:" + case_id + ":turn:" + str(turn_index) + ":tool:" + str(tool_step)


def text_sha256(text: str) -> str:
    """SHA-256 of the exact UTF-8 text. No normalization of any kind."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Raw record
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class UserMessageRecord:
    """A delivered user message, without its text.

    `turn_index` is the delivered index (as the policy saw it, from 1);
    `case_turn_index` is the position in the case's user_turns (from 0).
    """

    turn_index: int
    case_turn_index: int
    text_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_index": self.turn_index,
            "case_turn_index": self.case_turn_index,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True, kw_only=True)
class ClarificationRecord:
    """One Clarify decision and what answered it, if anything.

    `matched_turn_index` is the position of the answering turn in the case's
    user_turns (from 0; a conditional turn is >= 1), or None when nothing
    matched. `delivered_turn_index` is the UserMessage.turn_index it became.
    """

    sequence: int
    control_step: int
    requested_slots: tuple[str, ...]
    matched_turn_index: int | None
    delivered_turn_index: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "control_step": self.control_step,
            "requested_slots": list(self.requested_slots),
            "matched_turn_index": self.matched_turn_index,
            "delivered_turn_index": self.delivered_turn_index,
        }


@dataclass(frozen=True, kw_only=True)
class CaseRunRecord:
    """The raw control-layer facts of one case-run. No label, no score."""

    schema: str
    case_id: str
    virtual_now: str
    persona_id: str
    allowed_tools: tuple[str, ...]
    max_steps: int
    termination: str
    final_disposition: str | None
    control_steps: int
    user_messages: tuple[UserMessageRecord, ...]
    clarifications: tuple[ClarificationRecord, ...]
    observations: tuple[Observation, ...]
    fault_records: tuple[FaultCallRecord, ...]
    initial_db_sha256: str
    final_db_sha256: str
    database_unchanged: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "case_id": self.case_id,
            "virtual_now": self.virtual_now,
            "persona_id": self.persona_id,
            "allowed_tools": list(self.allowed_tools),
            "max_steps": self.max_steps,
            "termination": self.termination,
            "final_disposition": self.final_disposition,
            "control_steps": self.control_steps,
            "user_messages": [message.to_dict() for message in self.user_messages],
            "clarifications": [record.to_dict() for record in self.clarifications],
            "observations": [observation.to_dict() for observation in self.observations],
            "fault_records": [record.to_dict() for record in self.fault_records],
            "initial_db_sha256": self.initial_db_sha256,
            "final_db_sha256": self.final_db_sha256,
            "database_unchanged": self.database_unchanged,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def control_run_sha256(record: CaseRunRecord) -> str:
    """The deterministic hash of a raw control-layer record."""
    if not isinstance(record, CaseRunRecord):
        raise ValueError("record must be a CaseRunRecord, got " + type(record).__name__)
    return record.sha256()


# --------------------------------------------------------------------------
# One case-run
# --------------------------------------------------------------------------


def _require_max_steps(max_steps: object) -> int:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise ValueError("max_steps must be an integer")
    if not 1 <= max_steps <= HARD_MAX_STEPS:
        raise ValueError("max_steps must be between 1 and " + str(HARD_MAX_STEPS))
    return max_steps


class _CaseRun:
    """The control loop of one case-run. Owns no lifecycle; run_case does."""

    def __init__(self, runtime: V2CaseRuntime, gateway: FaultInjectingGateway,
                 policy: ControlPolicy, max_steps: int) -> None:
        case = runtime.case  # the runtime's validated private snapshot, copied
        self._gateway = gateway
        self._policy = policy
        self._max_steps = max_steps
        self._case_id = runtime.case_id
        self._virtual_now = case["virtual_now"]  # the authored instant the FixedClock holds
        self._persona_id = runtime.context.persona.persona_id
        self._allowed_tools = tuple(runtime.registry.names())
        self._first_turn = case["user_turns"][0]["text"]
        # (case_turn_index, slots, text), in case order; consumed at most once.
        self._conditional = [
            (index, frozenset(turn["on_clarify"]), turn["text"])
            for index, turn in enumerate(case["user_turns"][1:], start=1)
        ]
        self._messages: list[UserMessage] = []
        self._message_records: list[UserMessageRecord] = []
        self._observations: list[Observation] = []
        self._clarifications: list[ClarificationRecord] = []
        self._sequence = 0
        self._tool_step = 0
        self._control_steps = 0
        self.termination: str | None = None
        self.final_disposition: str | None = None

    # -- the loop ----------------------------------------------------------

    def drive(self) -> None:
        self._deliver(0, self._first_turn)
        for step in range(1, self._max_steps + 1):
            self._control_steps = step
            action = require_action(self._policy.next_action(self._state(step)))
            if type(action) is ToolCall:
                self._call_tool(step, action)
            elif type(action) is Clarify:
                if not self._clarify(step, action):
                    self.termination = TERMINATION_UNANSWERED_CLARIFICATION
                    return
            else:
                self.termination = TERMINATION_FINISHED
                self.final_disposition = action.disposition
                return
        self.termination = TERMINATION_MAX_STEPS_EXCEEDED

    def _state(self, step: int) -> ControlState:
        return ControlState(
            case_id=self._case_id,
            virtual_now=self._virtual_now,
            persona_id=self._persona_id,
            allowed_tools=self._allowed_tools,
            max_steps=self._max_steps,
            step_number=step,
            remaining_steps=self._max_steps - step + 1,
            user_messages=tuple(self._messages),
            observations=tuple(self._observations),
        )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    # -- user turns --------------------------------------------------------

    def _deliver(self, case_turn_index: int, text: str) -> int:
        turn_index = len(self._messages) + 1
        self._messages.append(UserMessage(turn_index=turn_index, text=text))
        self._message_records.append(UserMessageRecord(
            turn_index=turn_index, case_turn_index=case_turn_index,
            text_sha256=text_sha256(text)))
        return turn_index

    def _clarify(self, step: int, action: Clarify) -> bool:
        requested = frozenset(action.slots)
        match = next((entry for entry in self._conditional if requested <= entry[1]), None)
        delivered = None
        if match is not None:
            self._conditional.remove(match)
            delivered = self._deliver(match[0], match[2])
        self._clarifications.append(ClarificationRecord(
            sequence=self._next_sequence(),
            control_step=step,
            requested_slots=action.slots,
            matched_turn_index=None if match is None else match[0],
            delivered_turn_index=delivered,
        ))
        return match is not None

    # -- tools -------------------------------------------------------------

    def _call_tool(self, step: int, action: ToolCall) -> None:
        self._tool_step += 1
        turn_index = len(self._messages)
        observation_id = observation_id_for(self._case_id, turn_index, self._tool_step)
        before = len(self._gateway.records)
        common = dict(control_step=step, turn_index=turn_index, tool_step=self._tool_step,
                      observation_id=observation_id, tool_name=action.tool_name,
                      arguments=action.arguments)
        try:
            result = self._gateway.execute(action.tool_name, action.arguments,
                                           observation_id=observation_id)
        except FaultConfigurationError:
            raise
        except ValueError:
            if not self._injected_malformed(before, observation_id):
                raise
            self._observations.append(ToolContractFailure(
                sequence=self._next_sequence(), kind=CONTRACT_FAILURE_MALFORMED, **common))
            return
        self._require_ledgered(before, observation_id, action, result)
        self._observations.append(ToolObservation(
            sequence=self._next_sequence(), result=result, **common))

    def _new_records(self, before: int) -> tuple[FaultCallRecord, ...]:
        return self._gateway.records[before:]

    def _injected_malformed(self, before: int, observation_id: str) -> bool:
        new = self._new_records(before)
        return (len(new) == 1 and new[0].observation_id == observation_id
                and new[0].outcome == OUTCOME_INJECTED_MALFORMED)

    def _require_ledgered(self, before: int, observation_id: str, action: ToolCall,
                          result: object) -> None:
        """A returned result must be exactly one gateway call of this observation."""
        new = self._new_records(before)
        if (len(new) != 1 or new[0].observation_id != observation_id
                or new[0].tool_name != action.tool_name
                or new[0].outcome == OUTCOME_INJECTED_MALFORMED):
            raise EvalRuntimeError("tool result did not come from one gateway call")
        trace = getattr(result, "trace", None)
        if (getattr(result, "tool_name", None) != action.tool_name
                or not isinstance(trace, Mapping)
                or trace.get("observation_id") != observation_id):
            raise EvalRuntimeError("tool result does not belong to this observation")

    # -- the record --------------------------------------------------------

    def record(self, *, initial_db_sha256: str, final_db_sha256: str) -> CaseRunRecord:
        for observation in self._observations:
            if type(observation) is ToolObservation and not observation.result_unchanged():
                raise ControlPolicyContractError("a tool result was modified after it was observed")
        steps = (sum(type(o) is ToolObservation or type(o) is ToolContractFailure
                     for o in self._observations)
                 + len(self._clarifications)
                 + (1 if self.termination == TERMINATION_FINISHED else 0))
        if steps != self._control_steps or self.termination not in TERMINATIONS:
            raise EvalRuntimeError("control steps do not add up to the recorded events")
        return CaseRunRecord(
            schema=CONTROL_RUN_SCHEMA,
            case_id=self._case_id,
            virtual_now=self._virtual_now,
            persona_id=self._persona_id,
            allowed_tools=self._allowed_tools,
            max_steps=self._max_steps,
            termination=self.termination,
            final_disposition=self.final_disposition,
            control_steps=self._control_steps,
            user_messages=tuple(self._message_records),
            clarifications=tuple(self._clarifications),
            observations=tuple(self._observations),
            fault_records=self._gateway.records,
            initial_db_sha256=initial_db_sha256,
            final_db_sha256=final_db_sha256,
            database_unchanged=final_db_sha256 == initial_db_sha256,
        )


def run_case(case: object, policy: ControlPolicy, *, max_steps: int) -> CaseRunRecord:
    """Run one case under one control policy and return its raw record.

    The database invariant is checked before the runtime closes on every exit
    path. If it fails while another exception is already propagating, the
    invariant failure is raised, chained from the original one.
    """
    max_steps = _require_max_steps(max_steps)
    if not isinstance(policy, ControlPolicy):
        raise ControlPolicyContractError("policy must implement next_action(state)")
    runtime = V2CaseRuntime.from_case(case)
    try:
        try:
            # Exactly one gateway per case-run; every tool call goes through it.
            gateway = FaultInjectingGateway(runtime)
            run = _CaseRun(runtime, gateway, policy, max_steps)
            run.drive()
        except BaseException as primary:
            try:
                runtime.assert_database_unchanged()
            except BaseException as invariant_error:
                raise invariant_error from primary
            raise
        runtime.assert_database_unchanged()
        return run.record(initial_db_sha256=runtime.initial_db_sha256,
                          final_db_sha256=runtime.database_sha256())
    finally:
        runtime.close()
