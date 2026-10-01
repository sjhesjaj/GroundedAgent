"""The Stage 6 runner (docs/v2/stage6-design.md §3, §13.1, §15).

A new runner beside the frozen Stage 5 one (`eval_v2.runner`, unchanged and
still read-only). One run:

    trusted run inputs (persona_id, request_id, virtual_now, capabilities)
    -> RequestIdentity, built here - never by the policy or the model
    -> first user message
    -> policy.next_action(ActionControlState), one decision per control step,
       at most STAGE6_MAX_STEPS = 6
       ToolCall      -> the read gateway (five read tools, query_only connection)
       Clarify       -> Stage 5 semantics: the first undelivered conditional
                        turn whose slots cover the request, else
                        unanswered_clarification
       Finish        -> finished (Stage 5 semantics)
       ActionIntent  -> ActionIntentValidator (again: the policy is untrusted)
                        -> ActionGateway.start_action(RequestIdentity,
                           ValidatedAction), synchronously, exactly once
                        -> the run ends: waiting_approval or action_completed
    -> ActionRunRecord

An accepted ActionIntent consumes one control step and ends the control loop:
the policy is never asked for another decision, so it never sees the Guard's
result. The model and the policy never receive a connection, the
ActionGateway, the Guard or the ActionStore; the only writer is the
ActionGateway. Later user text such as "审批通过了" is a user message, never an
approval: this module has no path to record_decision / resume_action /
execute_approved. Approval stays an external trusted operator event.

Terminations
    finished / unanswered_clarification / max_steps_exceeded   Stage 5 semantics
    waiting_approval   the gateway returned WAITING_APPROVAL; the run is paused.
                       Exposed as RunPaused(pending_action_id, action_name,
                       rendered_text) only.
    action_completed   any other gateway outcome: EXECUTED, DENIED, FAILED,
                       or a replayed REJECTED / STALE.

The user-visible text of an action is ActionOutcomeRenderer output: a fixed
template over the persisted outcome, never model text.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from aftersales.action_errors import ActionValidationError, CapabilityConfigurationError
from aftersales.action_gateway import ActionGateway
from aftersales.action_outcome import ActionOutcome, ActionOutcomeRenderer, ActionStatus
from aftersales.actions import ActionIntentValidator, build_action_registry
from aftersales.capabilities import DEPLOYMENT_ACTIONS, DEPLOYMENT_READ_TOOLS, EffectiveCapabilities
from aftersales.clock import FixedClock
from aftersales.context import TrustedExecutionContext
from aftersales.demo import resolve_persona
from aftersales.executor import TRACE_OBSERVATION_ID, execute_tool
from aftersales.ids import RequestIdentity
from aftersales.registry import build_runtime_registry
from orchestration.contracts import ToolResult

from .action_control import (
    STAGE6_MAX_STEPS,
    ActionControlPolicy,
    ActionControlState,
    ActionIntent,
    require_stage6_action,
)
from .control import (
    Clarify,
    ControlPolicyContractError,
    Observation,
    ToolCall,
    ToolObservation,
    UserMessage,
    canonical_json,
    clarification_slots,
)
from .runner import (
    TERMINATION_FINISHED,
    TERMINATION_MAX_STEPS_EXCEEDED,
    TERMINATION_UNANSWERED_CLARIFICATION,
    TERMINATIONS,
    ClarificationRecord,
    UserMessageRecord,
    observation_id_for,
    text_sha256,
)
from .runtime import EvalRuntimeError, check_runtime_registry, parse_virtual_now

STAGE6_RUN_SCHEMA = "v2-stage6-run/1"

TERMINATION_WAITING_APPROVAL = "waiting_approval"
TERMINATION_ACTION_COMPLETED = "action_completed"
STAGE6_TERMINATIONS = TERMINATIONS + (TERMINATION_ACTION_COMPLETED, TERMINATION_WAITING_APPROVAL)

EVENT_ACTION_PROPOSED = "action.proposed"


# --------------------------------------------------------------------------
# The user side of a run
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ConditionalTurn:
    """A user message delivered only in answer to a Clarify covering its slots."""

    on_clarify: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        slots = self.on_clarify
        if (not isinstance(slots, tuple) or not slots or len(set(slots)) != len(slots)
                or not all(isinstance(slot, str) and slot in clarification_slots() for slot in slots)):
            raise ValueError("on_clarify must be a non-empty tuple of distinct frozen slots")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("a conditional turn needs text")


@dataclass(frozen=True, kw_only=True)
class Stage6Conversation:
    """The first user message and the conditional answers, in order."""

    first_turn: str
    conditional_turns: tuple[ConditionalTurn, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.first_turn, str) or not self.first_turn.strip():
            raise ValueError("the first user turn needs text")
        if not isinstance(self.conditional_turns, tuple) or not all(
                type(turn) is ConditionalTurn for turn in self.conditional_turns):
            raise ValueError("conditional_turns must be a tuple of ConditionalTurn")


# --------------------------------------------------------------------------
# The read side
# --------------------------------------------------------------------------


class ReadToolGateway(Protocol):
    """Executes one read tool call and returns its ToolResult."""

    def execute(self, tool_name: str, arguments: Mapping[str, str], *,
                observation_id: str) -> ToolResult:
        ...


class Stage6ReadGateway:
    """The five read tools over a read-only, query_only connection to the Stage 6 database.

    The connection is not the ActionGateway's: even a faulty read tool holds
    no writable handle. Identity and time come from the trusted context, the
    same as in Stage 5; every call goes through the unchanged `execute_tool`.
    """

    def __init__(self, db_path: str | Path, *, persona_id: str, virtual_now: str,
                 read_tools: tuple[str, ...] = DEPLOYMENT_READ_TOOLS) -> None:
        target = Path(db_path)
        if not target.is_file():
            raise FileNotFoundError("the Stage 6 database file does not exist")
        tools = tuple(read_tools)
        if not set(tools) <= set(DEPLOYMENT_READ_TOOLS):
            raise CapabilityConfigurationError("read_tools asks for a tool outside the upper bound")
        registry = build_runtime_registry()
        check_runtime_registry(registry)
        persona = resolve_persona(persona_id)
        clock = FixedClock(parse_virtual_now(virtual_now))
        connection = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise EvalRuntimeError("SQLite refused to enable query_only")
            self._context = TrustedExecutionContext(persona=persona, clock=clock,
                                                    connection=connection)
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._registry = registry
        self._read_tools = frozenset(tools)
        self._persona_id = persona.persona_id
        self._virtual_now = virtual_now

    @property
    def persona_id(self) -> str:
        return self._persona_id

    @property
    def virtual_now(self) -> str:
        return self._virtual_now

    def execute(self, tool_name: str, arguments: Mapping[str, str], *,
                observation_id: str) -> ToolResult:
        if tool_name not in self._read_tools:
            raise ValueError("the read tool is not in this run's effective capabilities")
        return execute_tool(self._registry, self._context, tool_name, arguments,
                            observation_id=observation_id)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Stage6ReadGateway":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --------------------------------------------------------------------------
# Results and the run record
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RunPaused:
    """A run paused on WAITING_APPROVAL: opaque ids and fixed text, nothing else."""

    pending_action_id: str
    action_name: str
    rendered_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "pending_action_id": self.pending_action_id,
            "action_name": self.action_name,
            "rendered_text": self.rendered_text,
        }


@dataclass(frozen=True, kw_only=True)
class ActionCompleted:
    """A run that ended on any gateway outcome other than WAITING_APPROVAL."""

    outcome: ActionOutcome
    rendered_text: str

    def to_dict(self) -> dict[str, object]:
        return {"outcome": self.outcome.to_dict(), "rendered_text": self.rendered_text}


@dataclass(frozen=True, kw_only=True)
class ActionProposedEvent:
    """The run-record event of an accepted proposal. Never the argument values."""

    control_step: int
    action_name: str
    args_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event": EVENT_ACTION_PROPOSED,
            "control_step": self.control_step,
            "action_name": self.action_name,
            "args_sha256": self.args_sha256,
        }


@dataclass(frozen=True, kw_only=True)
class ActionRunRecord:
    """The facts of one Stage 6 run. No label, no score, no user text."""

    schema: str
    persona_id: str
    request_id: str
    virtual_now: str
    allowed_tools: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    max_steps: int
    termination: str
    final_disposition: str | None
    control_steps: int
    user_messages: tuple[UserMessageRecord, ...]
    clarifications: tuple[ClarificationRecord, ...]
    observations: tuple[Observation, ...]
    events: tuple[ActionProposedEvent, ...]
    paused: RunPaused | None
    completed: ActionCompleted | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "persona_id": self.persona_id,
            "request_id": self.request_id,
            "virtual_now": self.virtual_now,
            "allowed_tools": list(self.allowed_tools),
            "allowed_actions": list(self.allowed_actions),
            "max_steps": self.max_steps,
            "termination": self.termination,
            "final_disposition": self.final_disposition,
            "control_steps": self.control_steps,
            "user_messages": [message.to_dict() for message in self.user_messages],
            "clarifications": [record.to_dict() for record in self.clarifications],
            "observations": [observation.to_dict() for observation in self.observations],
            "events": [event.to_dict() for event in self.events],
            "paused": None if self.paused is None else self.paused.to_dict(),
            "completed": None if self.completed is None else self.completed.to_dict(),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


def require_capabilities(capabilities: object) -> EffectiveCapabilities:
    """The run's effective set must lie inside the deployment upper bound.

    EffectiveCapabilities is a plain dataclass, so a hand-built one is checked
    again here, before the policy is ever asked for a decision.
    """
    if type(capabilities) is not EffectiveCapabilities:
        raise CapabilityConfigurationError("capabilities must come from the CapabilityGate")
    for names, bound, what in ((capabilities.read_tools, DEPLOYMENT_READ_TOOLS, "read_tools"),
                               (capabilities.actions, DEPLOYMENT_ACTIONS, "actions")):
        if (not isinstance(names, tuple) or len(set(names)) != len(names)
                or not all(isinstance(name, str) for name in names)
                or not set(names) <= set(bound)):
            raise CapabilityConfigurationError(what + " lies outside the deployment upper bound")
        if names != tuple(name for name in bound if name in names):
            raise CapabilityConfigurationError(what + " must keep the deployment order")
    return capabilities


class _ActionRun:
    """The control loop of one Stage 6 run."""

    def __init__(self, conversation: Stage6Conversation, policy: ActionControlPolicy, *,
                 identity: RequestIdentity, virtual_now: str,
                 capabilities: EffectiveCapabilities, read_gateway: ReadToolGateway,
                 action_gateway: ActionGateway) -> None:
        self._policy = policy
        self._identity = identity
        self._virtual_now = virtual_now
        self._capabilities = capabilities
        self._read_gateway = read_gateway
        self._action_gateway = action_gateway
        self._validator = ActionIntentValidator(build_action_registry(), capabilities.actions)
        self._renderer = ActionOutcomeRenderer()
        self._first_turn = conversation.first_turn
        # (case-order index, slots, text), consumed at most once.
        self._conditional = [(index, frozenset(turn.on_clarify), turn.text)
                             for index, turn in enumerate(conversation.conditional_turns, start=1)]
        self._messages: list[UserMessage] = []
        self._message_records: list[UserMessageRecord] = []
        self._observations: list[Observation] = []
        self._clarifications: list[ClarificationRecord] = []
        self._events: list[ActionProposedEvent] = []
        self._sequence = 0
        self._tool_step = 0
        self._control_steps = 0
        self.termination: str | None = None
        self.final_disposition: str | None = None
        self.paused: RunPaused | None = None
        self.completed: ActionCompleted | None = None

    def drive(self) -> None:
        self._deliver(0, self._first_turn)
        for step in range(1, STAGE6_MAX_STEPS + 1):
            self._control_steps = step
            action = require_stage6_action(self._policy.next_action(self._state(step)))
            if type(action) is ToolCall:
                self._call_tool(step, action)
            elif type(action) is Clarify:
                if not self._clarify(step, action):
                    self.termination = TERMINATION_UNANSWERED_CLARIFICATION
                    return
            elif type(action) is ActionIntent:
                # Terminating: the policy is never asked again in this run.
                self._act(step, action)
                return
            else:
                self.termination = TERMINATION_FINISHED
                self.final_disposition = action.disposition
                return
        self.termination = TERMINATION_MAX_STEPS_EXCEEDED

    def _state(self, step: int) -> ActionControlState:
        return ActionControlState(
            virtual_now=self._virtual_now,
            persona_id=self._identity.persona_id,
            allowed_tools=self._capabilities.read_tools,
            allowed_actions=self._capabilities.actions,
            max_steps=STAGE6_MAX_STEPS,
            step_number=step,
            remaining_steps=STAGE6_MAX_STEPS - step + 1,
            user_messages=tuple(self._messages),
            observations=tuple(self._observations),
        )

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    # -- user turns (Stage 5 semantics) -----------------------------------

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

    # -- reads -------------------------------------------------------------

    def _call_tool(self, step: int, action: ToolCall) -> None:
        if action.tool_name not in self._capabilities.read_tools:
            raise ControlPolicyContractError("a ToolCall named a read tool outside this run's capabilities")
        self._tool_step += 1
        turn_index = len(self._messages)
        observation_id = observation_id_for(turn_index, self._tool_step)
        result = self._read_gateway.execute(action.tool_name, action.arguments,
                                            observation_id=observation_id)
        trace = getattr(result, "trace", None)
        if (type(result) is not ToolResult or result.tool_name != action.tool_name
                or not isinstance(trace, Mapping)
                or trace.get(TRACE_OBSERVATION_ID) != observation_id):
            raise EvalRuntimeError("tool result does not belong to this observation")
        self._observations.append(ToolObservation(
            sequence=self._next_sequence(), control_step=step, turn_index=turn_index,
            tool_step=self._tool_step, observation_id=observation_id,
            tool_name=action.tool_name, arguments=action.arguments, result=result))

    # -- the action --------------------------------------------------------

    def _act(self, step: int, intent: ActionIntent) -> None:
        if intent.action_name not in self._capabilities.actions:
            raise ControlPolicyContractError("an ActionIntent named an action outside this run's capabilities")
        try:
            # The policy is an untrusted proposer: the closed contract is checked here too.
            action = self._validator.validate(intent.action_name, intent.arguments)
        except ActionValidationError as error:
            raise ControlPolicyContractError(
                "an ActionIntent failed the action contract: " + error.diagnostic) from None
        self._events.append(ActionProposedEvent(
            control_step=step, action_name=action.action_name, args_sha256=action.args_sha256))
        # The one write path: trusted identity plus validated action, exactly once.
        outcome = self._action_gateway.start_action(self._identity, action)
        if type(outcome) is not ActionOutcome or outcome.action_name != action.action_name:
            raise EvalRuntimeError("the ActionGateway returned an outcome for another action")
        text = self._renderer.render(outcome)
        if outcome.status is ActionStatus.WAITING_APPROVAL:
            self.termination = TERMINATION_WAITING_APPROVAL
            self.paused = RunPaused(pending_action_id=outcome.pending_action_id,
                                    action_name=outcome.action_name, rendered_text=text)
        else:
            self.termination = TERMINATION_ACTION_COMPLETED
            self.completed = ActionCompleted(outcome=outcome, rendered_text=text)

    # -- the record --------------------------------------------------------

    def record(self) -> ActionRunRecord:
        for observation in self._observations:
            if type(observation) is ToolObservation and not observation.result_unchanged():
                raise ControlPolicyContractError("a tool result was modified after it was observed")
        terminal = self.termination in (TERMINATION_FINISHED, TERMINATION_WAITING_APPROVAL,
                                        TERMINATION_ACTION_COMPLETED)
        steps = len(self._observations) + len(self._clarifications) + (1 if terminal else 0)
        if steps != self._control_steps or self.termination not in STAGE6_TERMINATIONS:
            raise EvalRuntimeError("control steps do not add up to the recorded events")
        if (self.termination == TERMINATION_WAITING_APPROVAL) != (self.paused is not None) or (
                self.termination == TERMINATION_ACTION_COMPLETED) != (self.completed is not None):
            raise EvalRuntimeError("the run result disagrees with its termination")
        return ActionRunRecord(
            schema=STAGE6_RUN_SCHEMA,
            persona_id=self._identity.persona_id,
            request_id=self._identity.request_id,
            virtual_now=self._virtual_now,
            allowed_tools=self._capabilities.read_tools,
            allowed_actions=self._capabilities.actions,
            max_steps=STAGE6_MAX_STEPS,
            termination=self.termination,
            final_disposition=self.final_disposition,
            control_steps=self._control_steps,
            user_messages=tuple(self._message_records),
            clarifications=tuple(self._clarifications),
            observations=tuple(self._observations),
            events=tuple(self._events),
            paused=self.paused,
            completed=self.completed,
        )


def run_action_conversation(conversation: Stage6Conversation, policy: ActionControlPolicy, *,
                            persona_id: str, request_id: str, virtual_now: str,
                            capabilities: EffectiveCapabilities, read_gateway: ReadToolGateway,
                            action_gateway: ActionGateway) -> ActionRunRecord:
    """Run one Stage 6 conversation under one control policy.

    `persona_id`, `request_id`, `virtual_now` and `capabilities` are trusted
    composition-root inputs; the RequestIdentity is built here. Every check
    happens before the policy is asked for its first decision.
    """
    if type(conversation) is not Stage6Conversation:
        raise ValueError("conversation must be a Stage6Conversation")
    if not isinstance(policy, ActionControlPolicy):
        raise ControlPolicyContractError("policy must implement next_action(state)")
    capabilities = require_capabilities(capabilities)
    if type(action_gateway) is not ActionGateway:
        raise ValueError("action_gateway must be the ActionGateway")
    if not callable(getattr(read_gateway, "execute", None)):
        raise ValueError("read_gateway must implement execute(tool_name, arguments, *, observation_id)")
    parse_virtual_now(virtual_now)
    identity = RequestIdentity(persona_id=persona_id, request_id=request_id)
    for attribute, expected in (("persona_id", identity.persona_id), ("virtual_now", virtual_now)):
        actual = getattr(read_gateway, attribute, expected)
        if actual != expected:
            raise ValueError("the read gateway was built for another " + attribute)
    run = _ActionRun(conversation, policy, identity=identity, virtual_now=virtual_now,
                     capabilities=capabilities, read_gateway=read_gateway,
                     action_gateway=action_gateway)
    run.drive()
    return run.record()
