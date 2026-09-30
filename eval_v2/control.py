"""The control-policy contract of a V2 case-run (Stage 4.4.2).

One protocol for every agent control policy the V2 eval compares - the Stage 4
deterministic Baseline and the Stage 5 LLM-native tool loop alike:

    policy.next_action(state) -> ToolCall | Clarify | Finish

one decision at a time. The runner (`eval_v2.runner`) owns everything else:
the case runtime, the single fault gateway, the user turns, the step budget.
So the only experimental variable between two policies is the policy.

What a policy sees
    `ControlState` holds what is really visible at run time and nothing more:
    the virtual instant, the persona id, the tool names, the step budget, the
    user messages actually delivered so far, and the observations of the tool
    calls made so far. Never the case id (eval bookkeeping, not business
    meaning - it stays in the raw run record only), the case dict, its
    expected_* labels, its archetype, its fixture or fault declarations, the
    trusted customer id, or the runtime / connection / registry. A conditional
    user turn is not in the state until a clarification has delivered it.

What a policy returns
    Frozen actions, checked when built and again by the runner:

    - ToolCall(tool_name, arguments)  arguments are copied and read-only; the
                                      executor, not this module, decides whether
                                      the tool and its arguments are valid
    - Clarify(slots)                  a non-empty tuple of distinct slots from
                                      the frozen eval/v2/spec/slots.json, in
                                      that file's order
    - Finish(disposition)             one of the frozen final vocabulary:
                                      answer, refuse, handoff, boundary

    `Finish.disposition` is what the policy decided. It is never compared with
    the case's expected label here.

The closed vocabularies are read from the frozen spec files, lazily and
read-only, and cross-checked against the frozen case schema; nothing here keeps
a second hand-maintained copy. Nothing here reads a clock, generates an id, or
draws a random number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol, Union, runtime_checkable

from orchestration.contracts import ToolResult

SPEC_DIR = Path(__file__).resolve().parent.parent / "eval" / "v2" / "spec"
SLOTS_PATH = SPEC_DIR / "slots.json"
CASE_SCHEMA_PATH = SPEC_DIR / "case.schema.json"
FINAL_OUTCOMES_PATH = SPEC_DIR / "final-outcomes.json"

# The only contract-failure kind a tool call can produce today.
CONTRACT_FAILURE_MALFORMED = "malformed"
TOOL_CONTRACT_FAILURE_KINDS = (CONTRACT_FAILURE_MALFORMED,)


class ControlPolicyContractError(RuntimeError):
    """A control policy returned something outside the action contract.

    A programmer / policy error, never an observation: it ends the case-run.
    """


class ControlVocabularyDrift(RuntimeError):
    """The frozen spec files disagree about a closed vocabulary."""


# --------------------------------------------------------------------------
# Closed vocabularies (frozen spec, read-only)
# --------------------------------------------------------------------------


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def clarification_slots() -> tuple[str, ...]:
    """The frozen clarification slots, in slots.json order."""
    slots = tuple(entry["slot"] for entry in _load(SLOTS_PATH)["slots"])
    schema_slots = tuple(_load(CASE_SCHEMA_PATH)["$defs"]["slot"]["enum"])
    if not slots or len(set(slots)) != len(slots) or slots != schema_slots:
        raise ControlVocabularyDrift("slots.json disagrees with the case schema slot enum")
    return slots


@lru_cache(maxsize=1)
def finish_dispositions() -> tuple[str, ...]:
    """The frozen expected_answerability.final vocabulary, in schema order."""
    schema = _load(CASE_SCHEMA_PATH)
    finals = tuple(schema["properties"]["expected_answerability"]
                   ["properties"]["final"]["enum"])
    defined = tuple(_load(FINAL_OUTCOMES_PATH)["definitions"])
    if not finals or len(set(finals)) != len(finals) or set(finals) != set(defined):
        raise ControlVocabularyDrift("final-outcomes.json disagrees with the case schema")
    return finals


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


def _read_only_copy(arguments: Mapping) -> Mapping:
    return MappingProxyType(dict(arguments))


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    """Call one tool. Validity of the tool and its arguments is the executor's."""

    tool_name: str
    arguments: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):
            raise ControlPolicyContractError(
                "ToolCall.arguments must be a mapping, got " + type(self.arguments).__name__)
        # A private, read-only copy: the policy keeps no handle on it.
        object.__setattr__(self, "arguments", _read_only_copy(self.arguments))
        self.check()

    def check(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ControlPolicyContractError("ToolCall.tool_name must be a non-empty string")
        if not isinstance(self.arguments, MappingProxyType):
            raise ControlPolicyContractError("ToolCall.arguments must stay read-only")


@dataclass(frozen=True, kw_only=True)
class Clarify:
    """Ask the user for the named slots. Structured: there is no question text."""

    slots: tuple[str, ...]

    def __post_init__(self) -> None:
        self.check()

    def check(self) -> None:
        slots = self.slots
        if not isinstance(slots, tuple) or not slots:
            raise ControlPolicyContractError("Clarify.slots must be a non-empty tuple")
        vocabulary = clarification_slots()
        for slot in slots:
            if not isinstance(slot, str) or slot not in vocabulary:
                # The value is policy output, not echoed.
                raise ControlPolicyContractError(
                    "Clarify.slots may only name: " + ", ".join(vocabulary))
        if len(set(slots)) != len(slots):
            raise ControlPolicyContractError("Clarify.slots must be distinct")
        if slots != tuple(slot for slot in vocabulary if slot in slots):
            raise ControlPolicyContractError(
                "Clarify.slots must follow the slots.json order: " + ", ".join(vocabulary))


@dataclass(frozen=True, kw_only=True)
class Finish:
    """Stop, with the policy's own disposition. No answer text at this stage."""

    disposition: str

    def __post_init__(self) -> None:
        self.check()

    def check(self) -> None:
        dispositions = finish_dispositions()
        if not isinstance(self.disposition, str) or self.disposition not in dispositions:
            raise ControlPolicyContractError(
                "Finish.disposition must be one of: " + ", ".join(dispositions))


ControlAction = Union[ToolCall, Clarify, Finish]
ACTION_TYPES = (ToolCall, Clarify, Finish)


def require_action(action: object) -> ControlAction:
    """The runner's check of whatever a policy returned.

    Exact types only (a subclass could override `check`), and the invariants
    are checked again: a frozen dataclass can still be forced with
    object.__setattr__.
    """
    if type(action) not in ACTION_TYPES:
        raise ControlPolicyContractError(
            "a control policy must return ToolCall, Clarify, or Finish, got "
            + type(action).__name__)
    action.check()
    return action


# --------------------------------------------------------------------------
# What the policy sees
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class UserMessage:
    """One user message actually delivered to the policy.

    `turn_index` counts delivered messages from 1. It says nothing about the
    case's user_turns list, so an undelivered conditional turn leaves no gap.
    """

    turn_index: int
    text: str


def _arguments_dict(arguments: Mapping[str, str]) -> dict[str, str]:
    return {name: arguments[name] for name in sorted(arguments)}


@dataclass(frozen=True, kw_only=True)
class ToolObservation:
    """A tool call that returned a ToolResult (ok, empty, or error).

    `result` is the executor's own ToolResult object, never a copy or a
    summary. Its serialized form is captured when the observation is made, so
    the run record cannot be changed afterwards through the (mutable) result.
    """

    sequence: int
    control_step: int
    turn_index: int
    tool_step: int
    observation_id: str
    tool_name: str
    arguments: Mapping[str, str]
    result: ToolResult
    _result_dict: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.result, ToolResult):
            raise TypeError("ToolObservation.result must be a ToolResult")
        object.__setattr__(self, "arguments", _read_only_copy(self.arguments))
        object.__setattr__(self, "_result_dict", json.loads(canonical_json(self.result.to_dict())))

    def result_unchanged(self) -> bool:
        return canonical_json(self.result.to_dict()) == canonical_json(self._result_dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "tool_observation",
            "sequence": self.sequence,
            "control_step": self.control_step,
            "turn_index": self.turn_index,
            "tool_step": self.tool_step,
            "observation_id": self.observation_id,
            "tool_name": self.tool_name,
            "arguments": _arguments_dict(self.arguments),
            "result": json.loads(canonical_json(self._result_dict)),
        }


@dataclass(frozen=True, kw_only=True)
class ToolContractFailure:
    """A tool call that produced no ToolResult at all (an injected malformed result).

    Fixed taxonomy only: no exception message, no traceback, and no fabricated
    ToolResult.
    """

    sequence: int
    control_step: int
    turn_index: int
    tool_step: int
    observation_id: str
    tool_name: str
    arguments: Mapping[str, str]
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in TOOL_CONTRACT_FAILURE_KINDS:
            raise ValueError("ToolContractFailure.kind must be one of: "
                             + ", ".join(TOOL_CONTRACT_FAILURE_KINDS))
        object.__setattr__(self, "arguments", _read_only_copy(self.arguments))

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "tool_contract_failure",
            "sequence": self.sequence,
            "control_step": self.control_step,
            "turn_index": self.turn_index,
            "tool_step": self.tool_step,
            "observation_id": self.observation_id,
            "tool_name": self.tool_name,
            "arguments": _arguments_dict(self.arguments),
            "kind": self.kind,
        }


Observation = Union[ToolObservation, ToolContractFailure]


@dataclass(frozen=True, kw_only=True, slots=True)
class ControlState:
    """Everything a control policy may see before one decision. Nothing else.

    `step_number` is the 1-based number of the decision being asked for;
    `remaining_steps` counts the decisions left including this one, so the
    last allowed decision sees 1.
    """

    virtual_now: str
    persona_id: str
    allowed_tools: tuple[str, ...]
    max_steps: int
    step_number: int
    remaining_steps: int
    user_messages: tuple[UserMessage, ...]
    observations: tuple[Observation, ...]


@runtime_checkable
class ControlPolicy(Protocol):
    """One control decision per call, from the visible state only."""

    def next_action(self, state: ControlState) -> ControlAction:
        ...


# --------------------------------------------------------------------------
# Canonical JSON (shared with the run record)
# --------------------------------------------------------------------------


def canonical_json(value: object) -> str:
    """Strict canonical JSON: plain data only, sorted keys, no NaN, no default=."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
