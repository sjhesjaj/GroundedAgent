"""One after-sales product conversation: a resumable Stage 6 control loop.

Conversation state is data, not a live object: the delivered customer
messages, the observations of every read tool call, the step counters and the
open control run. A fresh evaluated policy (agent_core.new_control_policy) is
built for every HTTP request and sees only an ActionControlState rebuilt from
that data, so a turn can be rolled back, and the design does not depend on a
policy object surviving between requests.

Control runs
    A control run is one Stage 6 run: at most STAGE6_MAX_STEPS = 6 decisions,
    ending at Finish, at an ActionIntent, or at the step limit. It may span
    several HTTP requests:

    Clarify       the run PAUSES and control returns to HTTP with a fixed
                  clarification text; status NEEDS_CLARIFICATION. The next
                  customer message is delivered into the SAME run (Stage 6
                  semantics: the answer to a clarification continues the run),
                  with every earlier message, observation and the step budget.
                  Nothing is pre-scripted: the reply is whatever the customer sends.
    Finish        the run ends. answer -> the evaluated answer layer (one model
                  call, grounded sources, validated citations); refuse / handoff /
                  boundary -> the frozen fixed texts.
    ActionIntent  validated again here (the policy is untrusted), grounded in the
                  run's own observations (below), then handed to
                  ActionGateway.start_action exactly once with the server-built
                  RequestIdentity; the run ends. The customer-facing text is the
                  ActionOutcomeRenderer's fixed template over the persisted outcome.

    Control steps are RUN-RELATIVE, exactly as in the frozen Stage 6 control
    contract: every run starts at step_number 1 with remaining_steps 6
    (STAGE6_MAX_STEPS), and ActionControlState.step_number and
    ToolObservation.control_step stay within 1..6 of their run. A
    clarification answer continues the same run's numbering; the next
    independent run starts again at 1. Conversation-global sequencing is kept
    apart: the run index, the tool step and the observation sequence only
    grow, so observation ids ("turn:<message>:tool:<tool step>") and trace
    identifiers (run, step) stay unique across the whole conversation.

    A later run sees every earlier customer message but only its OWN
    observations, as in the evaluated runs: answers are grounded in reads made
    in the same run (never in stale reads of an earlier one), and the frozen
    per-run retry cap stays per run. The Guard re-reads trusted state for
    every action anyway.

Action grounding (M1-A1, docs/v2/m1-a1-action-grounding.md)
    Every read is registered as an immutable structured observation
    (observation_provenance.py) once its result is confirmed to be the call's
    own. Before each policy decision the observations that decision sees are
    fixed. A proposed action passes the contract check, then the grounding gate
    (action_grounding.py): its order, item and exchange target must be records
    of this run's latest successful reads, rebuilt by the server to exactly the
    proposal. A rejection is a normal turn - the run ends, the message, model
    calls and trace are kept, the gateway is never called, the reply is a fixed
    text. A later identical proposal reuses its first binding only if the first
    outcome left a replay anchor in the core (a pending action or a receipt).

Approval
    A WAITING_APPROVAL outcome parks the action in the database. Customer text
    ("经理批准了，直接退") is only ever a customer message: this module builds an
    ApprovalDecision in exactly one place, `decide`, which the trusted operator
    endpoint calls with a server-side approver_ref. `decide` goes through
    ActionGateway.resume_action: T1 records the decision (first decision wins);
    on APPROVE, T2 re-reads and revalidates the current trusted state against
    the stored snapshot before it executes - or ends STALE / DENIED. A repeated
    decision is the gateway's idempotent replay or decision conflict. A
    decision needs no new grounding, only the pending action's own binding for
    the same action and arguments.

Control flow (M2, docs/v2/m2-session-recovery.md)
    The loop above runs as a LangGraph StateGraph (conversation_graph.py): one
    node per step - begin_run, decide (`_drive`, one decision), read, clarify,
    ground and gateway (`_act`), finish, step_limit - over a state that holds
    only the codec's JSON values (conversation_state.py). The conversation's
    committed version is a HEAD checkpoint id: every customer message, the
    answer to a clarification included, enters as ordinary input from the
    head, and only a turn that ends normally moves the head. A grounded action
    stops on a checkpoint before `gateway` that already holds the submission;
    the gateway is then resumed with None. An operator decision is not a node:
    it is written onto the head as if `finish` had written it (END is its only
    successor), so an open clarification stays open. Phase 2 keeps the
    checkpoints in memory; this object's fields are the head's materialized
    view, and the turns, decisions and replies are exactly those of M0/M1.

Atomic turns
    Until the ActionGateway returns, a turn has no side effect: on any failure
    the head does not move and the conversation is restored exactly (the
    message is not recorded; the registered observations and grounded
    submissions included). Once the gateway returned, the turn is kept, so a
    pending id is never lost.
"""

from __future__ import annotations

import dataclasses
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from aftersales.action_errors import ActionValidationError
from aftersales.action_outcome import ActionOutcome, ActionOutcomeRenderer, ActionStatus
from aftersales.actions import ActionIntentValidator, args_digest, build_action_registry, canonical_args
from aftersales.approval import ApprovalDecision
from aftersales.context import Persona
from aftersales.executor import TRACE_OBSERVATION_ID
from aftersales.ids import RequestIdentity, idempotency_key
from orchestration.contracts import ToolResult

from . import agent_core as core
from .action_grounding import (
    GROUNDING_VERSION,
    GroundedSubmission,
    GroundingBinding,
    GroundingRejected,
    SubmissionIndex,
    ground_action,
)
from .conversation_graph import (
    CLARIFY,
    DURABILITY,
    END,
    FINISH,
    GATEWAY,
    GROUND,
    READ,
    RECURSION_LIMIT,
    STEP_LIMIT,
    ConversationContext,
    build_graph,
    memory_checkpointer,
)
from .conversation_state import encode_text, pack_state, unpack_state
from .demo_store import DemoStore, ReadSide
from .observation_provenance import ObservationLedger

MAX_CUSTOMER_MESSAGES = 40
REQUEST_ID_PREFIX = "conv-"
ANSWER_DISPOSITION = "answer"


class ConversationStatus(str, Enum):
    OPEN = "OPEN"                                # ready for the next customer message
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"  # a control run is paused on a Clarify
    WAITING_APPROVAL = "WAITING_APPROVAL"        # an action waits at the trusted approval boundary


# Reply kinds. The four finish dispositions are reply kinds as well.
REPLY_CLARIFICATION = "clarification"
REPLY_ACTION = "action"
REPLY_STEP_LIMIT = "step_limit"
REPLY_ANSWER_UNAVAILABLE = "answer_unavailable"
REPLY_OPERATOR_DECISION = "operator_decision"
REPLY_GROUNDING_REJECTED = "grounding_rejected"

# How an accepted action is grounded: in this run's observations, or by reusing
# the binding of its first submission, which left a replay anchor in the core.
GROUNDING_OBSERVED = "observed"
GROUNDING_REPLAY_ANCHOR = "replay_anchor"

# Fixed customer-facing texts. Never model text.
CLARIFICATION_PROMPTS = {
    "order_id": "您要办理的订单号",
    "order_item": "订单里具体是哪一件商品",
    "target_sku": "想换成的商品规格（例如尺码）",
    "reason": "申请的原因（例如质量问题、尺码不合适或不想要了）",
}
STEP_LIMIT_TEXT = "这次没能在限定的处理步数内完成，请补充更具体的信息后再试，或联系人工客服。"
ANSWER_UNAVAILABLE_TEXT = "抱歉，暂时无法根据查询结果给出可靠的回答，请稍后再试或联系人工客服。"
GROUNDING_REJECTED_TEXT = ("抱歉，我还没有完成订单商品的核对，暂时不能提交这个申请。"
                           "请确认订单号和要办理的商品，我会先查询核对，再继续为您处理。")

if set(CLARIFICATION_PROMPTS) != set(core.clarification_slots()):
    raise ImportError("every frozen clarification slot needs exactly one customer prompt")


def clarification_text(slots: tuple[str, ...]) -> str:
    return "为了继续处理，请告诉我：" + "；".join(CLARIFICATION_PROMPTS[slot] for slot in slots) + "。"


def observation_id(turn_index: int, tool_step: int) -> str:
    """The conversation-local id of one tool call (same shape as the Stage 5/6 runs)."""
    return "turn:" + str(turn_index) + ":tool:" + str(tool_step)


class ConversationError(Exception):
    """A product-level refusal. `code` is closed; the message carries no user data."""

    code = "conversation_error"


class ConversationFull(ConversationError):
    code = "conversation_full"


class PendingActionNotInConversation(ConversationError):
    code = "pending_action_not_found"


class PendingActionNotGrounded(ConversationError):
    """The pending action has no grounding binding for this very action and arguments."""

    code = "pending_action_not_grounded"


class TurnFailed(ConversationError):
    """Nothing was recorded: the conversation is exactly as before the request."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderCallFailed(RuntimeError):
    """The model provider failed (network, HTTP, malformed reply). Not model output."""


class _GuardedProvider:
    """Marks every provider failure as ProviderCallFailed, so a turn can tell an
    outage (retryable) from an integration bug. Forwards calls unchanged."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", None)
        self.model = getattr(inner, "model", None)

    def chat(self, messages, **kwargs):
        try:
            return self._inner.chat(messages, **kwargs)
        except Exception as error:
            raise ProviderCallFailed(type(error).__name__) from error


@dataclass(frozen=True)
class _ControlRun:
    run_index: int                       # conversation-global: 1 for the first run, then 2, ...
    first_observation: int               # index of the run's first observation
    steps_used: int = 0                  # run-relative: decisions made so far (0..STAGE6_MAX_STEPS)
    awaiting_slots: tuple[str, ...] = ()


@dataclass
class _Turn:
    committed: bool = False
    steps: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    reply_kind: str | None = None
    reply_text: str | None = None
    clarification: tuple[str, ...] | None = None
    citations: tuple[dict, ...] = ()
    action: dict | None = None

    def reply(self, kind: str, text: str) -> None:
        self.reply_kind, self.reply_text = kind, text


class Conversation:
    """One customer conversation, bound for life to one server-side persona."""

    def __init__(self, *, session_id: str, persona: Persona, store: DemoStore) -> None:
        if not isinstance(persona, Persona):
            raise ValueError("persona must be a server-side Persona")
        self.session_id = session_id
        self.persona = persona
        self.lock = threading.Lock()
        self.closed = False
        self._store = store
        # Server-built: one logical request per conversation (the idempotency scope).
        self._identity = RequestIdentity(persona_id=persona.persona_id,
                                         request_id=REQUEST_ID_PREFIX + session_id)
        self._validator = ActionIntentValidator(build_action_registry(), store.capabilities.actions)
        self._renderer = ActionOutcomeRenderer()
        self._messages: list[core.UserMessage] = []
        self._observations: list[core.ToolObservation] = []
        self._transcript: list[dict] = []
        self._runs = 0         # conversation-global sequencing: runs started,
        self._tool_steps = 0   # tool calls made (observation ids),
        self._sequence = 0     # observation sequence numbers.
        self._run: _ControlRun | None = None
        self._actions: dict[str, dict] = {}   # pending_action_id -> {action_name, arguments}
        self._open_pending: list[str] = []
        self._provenance = ObservationLedger(session_id)   # every registered read (M1-A1)
        self._submissions = SubmissionIndex()              # idempotency key -> GroundedSubmission
        # M2: the control flow graph and the committed head. Even an empty
        # conversation has a head, so nothing but a head is ever read as it.
        self._graph = build_graph(memory_checkpointer())
        self._head: str | None = None
        self._save()

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @property
    def request_id(self) -> str:
        return self._identity.request_id

    @property
    def status(self) -> ConversationStatus:
        if self._run is not None:
            return ConversationStatus.NEEDS_CLARIFICATION
        if self._open_pending:
            return ConversationStatus.WAITING_APPROVAL
        return ConversationStatus.OPEN

    def persona_view(self) -> dict[str, str]:
        # Display only: never the customer id.
        return {"persona_id": self.persona.persona_id, "display_name": self.persona.display_name}

    def _common(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "persona": self.persona_view(),
            "business_time": self._store.business_time_iso,
            "status": self.status.value,
            "pending_action_id": self._open_pending[-1] if self._open_pending else None,
        }

    def view(self) -> dict[str, object]:
        pending = []
        for pending_action_id, proposal in self._actions.items():
            outcome = self._store.gateway.get_outcome(pending_action_id)
            pending.append(self._action_view(outcome, proposal))
        return {**self._common(), "messages": [dict(entry) for entry in self._transcript],
                "pending_actions": pending, "audit": self._store.audit(self.request_id)}

    @staticmethod
    def _action_view(outcome: ActionOutcome, proposal: Mapping[str, object]) -> dict[str, object]:
        data = outcome.to_dict()
        data.pop("request_id")
        if data["action_name"] is None:
            data["action_name"] = proposal["action_name"]
        data["arguments"] = dict(proposal["arguments"])
        return data

    def _response(self, turn: _Turn, *, trace: bool) -> dict[str, object]:
        return {
            **self._common(),
            "reply": {"kind": turn.reply_kind, "text": turn.reply_text},
            "clarification": (None if turn.clarification is None
                              else {"slots": list(turn.clarification)}),
            "citations": [dict(item) for item in turn.citations],
            "action": turn.action,
            "trace": ({"steps": turn.steps, "model_calls": turn.model_calls} if trace else None),
            "audit": self._store.audit(self.request_id),
        }

    # ------------------------------------------------------------------
    # One customer message
    # ------------------------------------------------------------------

    def _snapshot(self) -> tuple:
        return (list(self._messages), list(self._observations), list(self._transcript),
                self._runs, self._tool_steps, self._sequence, self._run,
                dict(self._actions), list(self._open_pending),
                self._provenance.snapshot(), self._submissions.snapshot())

    def _restore(self, saved: tuple) -> None:
        (self._messages, self._observations, self._transcript, self._runs, self._tool_steps,
         self._sequence, self._run, self._actions, self._open_pending,
         provenance, submissions) = saved
        self._provenance.restore(provenance)
        self._submissions.restore(submissions)

    def submit(self, text: str, provider: object) -> dict[str, object]:
        """Deliver one customer message and run the control loop until it pauses or ends."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a customer message needs text")
        if len(self._messages) >= MAX_CUSTOMER_MESSAGES:
            raise ConversationFull("the conversation is full; reset the demo")
        saved = self._snapshot()
        guarded = _GuardedProvider(provider)
        gateway = ConversationContext(self)   # the gateway step needs no policy and no provider
        try:
            with self._store.read_side(self.persona) as reader:
                context = ConversationContext(self, core.new_control_policy(guarded), guarded, reader)
                # Ordinary input from the head - also the answer to a clarification.
                checkpoint = self._invoke({"customer_text": encode_text(text)}, self._head, context)
                if checkpoint.next == (GATEWAY,):
                    # The grounded submission is checkpointed; only the gateway runs from here.
                    checkpoint = self._invoke(None, checkpoint.config["configurable"]["checkpoint_id"],
                                              gateway)
            if checkpoint.next:
                raise RuntimeError("the control flow stopped before the turn ended")
            turn = self._commit(checkpoint)
        except BaseException as error:
            if gateway.gateway_returned:
                # The database may have changed: the turn is kept as far as it got, as the head.
                self._save()
                raise
            self._restore(saved)
            if isinstance(error, ProviderCallFailed):
                raise TurnFailed("llm_unavailable") from error
            if isinstance(error, Exception):
                raise TurnFailed("agent_internal_error") from error
            raise
        return self._response(turn, trace=True)

    def _assistant_entry(self, turn: _Turn) -> dict[str, object]:
        entry: dict[str, object] = {"role": "assistant", "kind": turn.reply_kind,
                                    "text": turn.reply_text}
        if turn.action is not None:
            entry["action_status"] = turn.action["status"]
            entry["pending_action_id"] = turn.action["pending_action_id"]
        return entry

    # ------------------------------------------------------------------
    # The graph: head, checkpoints and the state of one node
    # ------------------------------------------------------------------

    def _config(self, checkpoint_id: str | None) -> dict[str, object]:
        configurable = {"thread_id": self.session_id, "checkpoint_ns": ""}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable, "recursion_limit": RECURSION_LIMIT}

    def _invoke(self, value, checkpoint_id: str, context: ConversationContext):
        """Run the graph from one checkpoint; the last checkpoint THIS invocation wrote.

        Never "the newest checkpoint of the thread": a failed turn leaves an
        orphan branch that is never read.
        """
        last = None
        for event in self._graph.stream(value, self._config(checkpoint_id), context=context,
                                        durability=DURABILITY, stream_mode="checkpoints"):
            last = event["config"]["configurable"]["checkpoint_id"]
        if last is None:
            raise RuntimeError("the graph wrote no checkpoint")
        return self._graph.get_state(self._config(last))

    def _commit(self, checkpoint) -> _Turn:
        """A turn that ended normally: its last checkpoint becomes the head."""
        turn = self._load(checkpoint.values)["turn"]
        self._head = checkpoint.config["configurable"]["checkpoint_id"]
        return turn

    def _save(self) -> None:
        """Write this conversation onto the head as if `finish` had written it, and move the head.

        END is `finish`'s only successor, so the stored run state - an open
        clarification included - is kept as it is.
        """
        config = self._graph.update_state(self._config(self._head), self._pack(None),
                                          as_node=FINISH)
        self._head = config["configurable"]["checkpoint_id"]

    def _load(self, state) -> dict[str, object]:
        """Make this object the materialized view of `state`; its decoded fields."""
        decoded = unpack_state(state)
        self._restore(decoded["snapshot"])
        return decoded

    def _pack(self, turn: _Turn | None, **fields) -> dict[str, object]:
        return pack_state(self._snapshot(), turn=turn, **fields)

    def _end_turn(self, turn: _Turn, **fields) -> dict[str, object]:
        self._transcript.append(self._assistant_entry(turn))
        return self._pack(turn, **fields)

    # ------------------------------------------------------------------
    # The graph's nodes (conversation_graph.py)
    # ------------------------------------------------------------------

    def _begin_run(self, state) -> dict[str, object]:
        text = self._load(state)["customer_text"]
        if text is None:
            raise RuntimeError("a turn begins with a customer message")
        self._messages.append(core.UserMessage(turn_index=len(self._messages) + 1, text=text))
        self._transcript.append({"role": "customer", "text": text})
        if self._run is None:
            # A new, independent control run: its steps start again at 1.
            self._runs += 1
            self._run = _ControlRun(run_index=self._runs,
                                    first_observation=len(self._observations))
        else:
            # The answer to a clarification continues the paused run.
            self._run = dataclasses.replace(self._run, awaiting_slots=())
        return self._pack(_Turn())

    def _control_state(self, run: _ControlRun):
        capabilities = self._store.capabilities
        step = run.steps_used
        return core.ActionControlState(
            virtual_now=self._store.business_time_iso,
            persona_id=self.persona.persona_id,
            allowed_tools=capabilities.read_tools,
            allowed_actions=capabilities.actions,
            max_steps=core.STAGE6_MAX_STEPS,
            step_number=step,
            remaining_steps=core.STAGE6_MAX_STEPS - step + 1,
            user_messages=tuple(self._messages),
            observations=tuple(self._observations[run.first_observation:]),
        )

    def _drive(self, node_state, policy) -> dict[str, object]:
        """The decide node: one control decision of the open run, or the step limit."""
        turn = self._load(node_state)["turn"]
        run = self._run
        if run.steps_used >= core.STAGE6_MAX_STEPS:
            return self._pack(turn, route=STEP_LIMIT)
        # Run-relative, as in the frozen contract: 1..STAGE6_MAX_STEPS per run.
        step = run.steps_used + 1
        run = dataclasses.replace(run, steps_used=step)
        self._run = run
        state = self._control_state(run)
        # Fixed before the model is asked: an action this decision proposes
        # can be grounded in exactly these registered reads, never in a later one.
        visible = self._provenance.visible_to(
            run_index=run.run_index,
            observation_ids=[item.observation_id for item in state.observations])
        seen = len(policy.decision_records)
        action = core.require_stage6_action(policy.next_action(state))
        records = policy.decision_records[seen:]
        turn.model_calls.extend({"run": run.run_index, **record.to_dict()} for record in records)
        kind = type(action)
        if kind is core.ToolCall:
            return self._pack(turn, route=READ, decision=action)
        if kind is core.Clarify:
            return self._pack(turn, route=CLARIFY, decision=action)
        if kind is core.ActionIntent:
            return self._pack(turn, route=GROUND, decision=action, visible=visible)
        turn.steps.append({"run": run.run_index, "step": step, "kind": "finish",
                           "disposition": action.disposition,
                           "diagnostic": records[-1].diagnostic if records else None})
        return self._pack(turn, route=FINISH, decision=action)

    def _read_step(self, state, reader: ReadSide) -> dict[str, object]:
        decoded = self._load(state)
        turn = decoded["turn"]
        self._read(self._run.run_index, self._run.steps_used, decoded["decision"], reader, turn)
        return self._pack(turn)

    def _clarify(self, state) -> dict[str, object]:
        """The run PAUSES: it stays open, with its slots, in the state the head will hold."""
        decoded = self._load(state)
        turn, action, run = decoded["turn"], decoded["decision"], self._run
        self._run = dataclasses.replace(run, awaiting_slots=action.slots)
        turn.steps.append({"run": run.run_index, "step": run.steps_used, "kind": "clarify",
                           "slots": list(action.slots)})
        turn.clarification = action.slots
        turn.reply(REPLY_CLARIFICATION, clarification_text(action.slots))
        return self._end_turn(turn)

    def _finish_step(self, state, provider) -> dict[str, object]:
        decoded = self._load(state)
        turn, run = decoded["turn"], self._run
        self._run = None
        self._finish(decoded["decision"].disposition, provider, self._control_state(run), turn)
        return self._end_turn(turn)

    def _step_limit(self, state) -> dict[str, object]:
        turn = self._load(state)["turn"]
        run, self._run = self._run, None
        turn.steps.append({"run": run.run_index, "step": run.steps_used, "kind": "step_limit"})
        turn.reply(REPLY_STEP_LIMIT, STEP_LIMIT_TEXT)
        return self._end_turn(turn)

    def _read(self, run_index: int, step: int, action, reader: ReadSide, turn: _Turn) -> None:
        if action.tool_name not in self._store.capabilities.read_tools:
            raise core.ControlPolicyContractError("a ToolCall named a tool outside the capabilities")
        self._tool_steps += 1
        turn_index = len(self._messages)
        call_id = observation_id(turn_index, self._tool_steps)
        result = reader.execute(action.tool_name, action.arguments, observation_id=call_id)
        trace = getattr(result, "trace", None)
        if (type(result) is not ToolResult or result.tool_name != action.tool_name
                or not isinstance(trace, Mapping) or trace.get(TRACE_OBSERVATION_ID) != call_id):
            raise RuntimeError("the tool result does not belong to this call")
        # The only place a read becomes provenance: the server's own, confirmed call.
        self._provenance.register(run_index=run_index, observation_id=call_id,
                                  tool_name=action.tool_name, arguments=action.arguments,
                                  result=result)
        self._sequence += 1
        self._observations.append(core.ToolObservation(
            sequence=self._sequence, control_step=step, turn_index=turn_index,
            tool_step=self._tool_steps, observation_id=call_id, tool_name=action.tool_name,
            arguments=action.arguments, result=result))
        turn.steps.append({"run": run_index, "step": step, "kind": "tool_call",
                           "tool_name": action.tool_name,
                           "arguments": dict(action.arguments), "result_status": result.status.value,
                           "observation_id": call_id})

    def _finish(self, disposition: str, provider, state, turn: _Turn) -> None:
        if disposition != ANSWER_DISPOSITION:
            turn.reply(disposition, core.FIXED_RESPONSES[disposition])
            return
        try:
            answer = core.generate_answer(provider, state)
        except core.AnswerUnavailable as error:
            turn.steps[-1]["answer_error"] = error.code
            turn.reply(REPLY_ANSWER_UNAVAILABLE, ANSWER_UNAVAILABLE_TEXT)
            return
        turn.citations = answer.citations
        turn.reply(ANSWER_DISPOSITION, answer.text)

    def _act(self, state, stage: str, context: ConversationContext) -> dict[str, object]:
        """The ground node, then - from the next checkpoint - the gateway node.

        ground   the policy is an untrusted proposer: the closed contract, then the
                 grounding gate (or the binding of a first submission that left a
                 replay anchor). A grounded action becomes the SUBMISSION in state;
                 the graph stops on that checkpoint before the gateway runs.
        gateway  rebuilds the ValidatedAction from the checkpointed submission and
                 hands it to ActionGateway.start_action exactly once. It needs
                 neither the policy nor the provider.
        One function, so that the grounding gate stays visibly the only way to
        the one write path.
        """
        decoded = self._load(state)
        turn = decoded["turn"]
        if stage == GROUND:
            intent, visible = decoded["decision"], decoded["visible"]
            run_index, step = self._run.run_index, self._run.steps_used
            if intent.action_name not in self._store.capabilities.actions:
                raise core.ControlPolicyContractError("an ActionIntent named an action outside the capabilities")
            try:
                # The policy is an untrusted proposer: the closed contract is checked here too.
                action = self._validator.validate(intent.action_name, intent.arguments)
            except ActionValidationError as error:
                raise core.ControlPolicyContractError(
                    "an ActionIntent failed the action contract: " + error.diagnostic) from None
            self._run = None
            proposed = {"run": run_index, "step": step, "kind": "action_proposed",
                        "action_name": action.action_name, "args_sha256": action.args_sha256}
            turn.steps.append(proposed)
            # The grounding gate, deterministic, before the only write path.
            key = idempotency_key(self._identity, action)
            anchored = self._submissions.anchored(key)
            if anchored is not None:
                # The first submission left a replay anchor in the core: reuse its binding,
                # and the gateway returns the core's own idempotent replay.
                if not anchored.binds(action.action_name, action.args_sha256):
                    raise RuntimeError("a replay anchor is bound to another action")
                binding, basis = anchored.binding, GROUNDING_REPLAY_ANCHOR
            else:
                try:
                    binding, basis = ground_action(action, visible), GROUNDING_OBSERVED
                except GroundingRejected as rejection:
                    # A normal product outcome: the run ends, nothing reaches the gateway.
                    turn.steps.append({"run": run_index, "step": step, "kind": "grounding_rejected",
                                       "action_name": action.action_name,
                                       "args_sha256": action.args_sha256, "code": rejection.code,
                                       "grounding_version": GROUNDING_VERSION})
                    turn.reply(REPLY_GROUNDING_REJECTED, GROUNDING_REJECTED_TEXT)
                    return self._end_turn(turn, route=END)
            proposed["grounding"] = {"basis": basis, **binding.to_dict()}
            submission = {"action_name": action.action_name, "arguments": action.args_object(),
                          "args_sha256": action.args_sha256, "key": key, "binding": binding,
                          "basis": basis, "run_index": run_index}
            return self._pack(turn, route=GATEWAY, submission=submission)
        if stage != GATEWAY:
            raise ValueError("an action is grounded, then submitted")
        submission = decoded["submission"]
        action = self._submitted_action(submission)
        # The one write path: the server-built identity plus the validated action, once.
        outcome = self._store.gateway.start_action(self._identity, action)
        # From here the database may have changed: this turn is kept whatever happens next.
        context.gateway_returned = True
        if type(outcome) is not ActionOutcome or outcome.action_name != action.action_name:
            raise RuntimeError("the ActionGateway returned an outcome for another action")
        if submission["basis"] == GROUNDING_OBSERVED:
            # A new submission - the first, or one after a DENIED / FAILED first outcome.
            self._submissions.record(GroundedSubmission(
                key=submission["key"], action_name=action.action_name,
                args_sha256=action.args_sha256, binding=submission["binding"],
                first_run_index=submission["run_index"], first_outcome=outcome))
        proposal = {"action_name": action.action_name, "arguments": action.args_object()}
        if outcome.pending_action_id is not None:
            self._actions.setdefault(outcome.pending_action_id, proposal)
            self._track(outcome.pending_action_id, outcome)
        turn.action = self._action_view(outcome, proposal)
        turn.reply(REPLY_ACTION, self._renderer.render(outcome))
        return self._end_turn(turn)

    def _submitted_action(self, submission: object):
        """The ValidatedAction of a checkpointed submission, re-derived and re-checked."""
        if type(submission) is not dict:
            raise RuntimeError("the gateway runs only on a grounded submission")
        action = self._validator.validate(submission["action_name"], submission["arguments"])
        binding = submission["binding"]
        if (action.args_sha256 != submission["args_sha256"]
                or idempotency_key(self._identity, action) != submission["key"]
                or submission["basis"] not in (GROUNDING_OBSERVED, GROUNDING_REPLAY_ANCHOR)
                or type(binding) is not GroundingBinding
                or not binding.matches(action.action_name, action.args_sha256)):
            raise RuntimeError("the checkpointed submission does not bind this action")
        return action

    def _track(self, pending_action_id: str, current: ActionOutcome) -> None:
        """Keep the open-pending list in step with the persisted pending state."""
        if pending_action_id in self._open_pending:
            self._open_pending.remove(pending_action_id)
        if current.status is ActionStatus.WAITING_APPROVAL:
            self._open_pending.append(pending_action_id)

    # ------------------------------------------------------------------
    # The trusted operator decision
    # ------------------------------------------------------------------

    def decide(self, pending_action_id: str, decision: str) -> dict[str, object]:
        """The trusted operator's APPROVE / REJECT on a pending action of THIS conversation.

        The only place in the product that builds an ApprovalDecision. The
        approver is the server-side demo operator, the decision time is the
        business clock; neither comes from the request.
        """
        proposal = self._actions.get(pending_action_id)
        if proposal is None:
            raise PendingActionNotInConversation("this conversation has no such pending action")
        # No new grounding for a decision: only the pending action's own binding,
        # for exactly this action and these arguments.
        submission = self._submissions.for_pending(pending_action_id)
        if submission is None or not submission.binds(
                proposal["action_name"], args_digest(canonical_args(proposal["arguments"]))):
            raise PendingActionNotGrounded("the pending action has no matching grounding binding")
        approval = ApprovalDecision(
            pending_action_id=pending_action_id,
            decision=decision,
            approver_ref=self._store.operator_ref,
            decided_at=self._store.clock.now().isoformat(),
        )
        # T1, then (APPROVE, freshly recorded) T2: revalidate against current trusted state.
        outcome = self._store.gateway.resume_action(approval)
        self._track(pending_action_id, self._store.gateway.get_outcome(pending_action_id))
        turn = _Turn(committed=True)
        turn.action = self._action_view(outcome, proposal)
        turn.reply(REPLY_OPERATOR_DECISION, self._renderer.render(outcome))
        if not outcome.idempotent_replay and not outcome.decision_conflict:
            # A first decision is news for the customer; a replay or conflict is not.
            self._transcript.append(self._assistant_entry(turn))
        # Not a graph node: written onto the head, leaving an open clarification open.
        self._save()
        return {**self._response(turn, trace=False),
                "operator_decision": {"pending_action_id": pending_action_id,
                                      "decision": decision,
                                      "approver_ref": self._store.operator_ref}}
