"""The Stage 6 control contract (docs/v2/stage6-design.md §15.1, §15.2).

Stage 5's protocol is unchanged: `eval_v2.control.require_action` accepts
ToolCall, Clarify and Finish only, so an ActionIntent that reaches the Stage 5
runner is a ControlPolicyContractError. Stage 6 adds one action type and one
state type, here:

    policy.next_action(ActionControlState) -> ToolCall | Clarify | Finish | ActionIntent

ActionIntent
    A proposal, never an execution: an action name and a read-only copy of
    its arguments. It carries no identity, no approval, no server id, no
    handler, no database and no Guard. Whether the proposal is executed,
    parked for approval or denied is decided by the ActionGateway and its
    Guard, after the control loop has ended. The closed argument contract is
    the ActionIntentValidator's (aftersales.actions); the constructor here only
    refuses what no ActionIntent may ever carry.

ActionControlState
    The Stage 5 ControlState fields plus `allowed_actions`, the effective
    actions of this run (from the CapabilityGate). Nothing else: no customer
    id, database, runtime, Guard decision, pending status, case id, scenario,
    expected label or final state, approval state, receipt, or policy build.

STAGE6_MAX_STEPS
    Frozen at 6 before any Stage 6 DEV run: the longest legal flow is
    Clarify(order) -> read -> Clarify(target) -> read -> action, five steps,
    with one spare. Stage 5 keeps max_steps = 5 (a different experiment).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol, Union, runtime_checkable

from aftersales.actions import ACTION_NAMES, FORBIDDEN_ACTION_ARGUMENT_NAMES

from .control import (
    Clarify,
    ControlPolicyContractError,
    ControlState,
    Finish,
    Observation,
    ToolCall,
    UserMessage,
)

STAGE6_MAX_STEPS = 6


@dataclass(frozen=True, kw_only=True)
class ActionIntent:
    """Propose one Stage 6 action. Proposing is not executing."""

    action_name: str
    arguments: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.arguments, Mapping):
            raise ControlPolicyContractError(
                "ActionIntent.arguments must be a mapping, got " + type(self.arguments).__name__)
        # A private, read-only copy: the proposer keeps no handle on it.
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))
        self.check()

    def check(self) -> None:
        if not isinstance(self.action_name, str) or self.action_name not in ACTION_NAMES:
            raise ControlPolicyContractError(
                "ActionIntent.action_name must be one of: " + ", ".join(ACTION_NAMES))
        if not isinstance(self.arguments, MappingProxyType):
            raise ControlPolicyContractError("ActionIntent.arguments must stay read-only")
        for name, value in self.arguments.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ControlPolicyContractError("ActionIntent.arguments must map strings to strings")
            if name in FORBIDDEN_ACTION_ARGUMENT_NAMES:
                # Identity, approval, control and system ids are never arguments.
                raise ControlPolicyContractError(
                    "ActionIntent.arguments may not carry identity, approval or system id fields")


Stage6Action = Union[ToolCall, Clarify, Finish, ActionIntent]
STAGE6_ACTION_TYPES = (ToolCall, Clarify, Finish, ActionIntent)


def require_stage6_action(action: object) -> Stage6Action:
    """The Stage 6 runner's check of whatever a policy returned.

    Exact types only, invariants checked again (see control.require_action).
    """
    if type(action) not in STAGE6_ACTION_TYPES:
        raise ControlPolicyContractError(
            "a Stage 6 control policy must return ToolCall, Clarify, Finish, or ActionIntent, got "
            + type(action).__name__)
    action.check()
    return action


@dataclass(frozen=True, kw_only=True, slots=True)
class ActionControlState:
    """Everything a Stage 6 control policy may see before one decision.

    The Stage 5 ControlState fields, with the same meaning, plus the
    effective actions of this run, in deployment order.
    """

    virtual_now: str
    persona_id: str
    allowed_tools: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    max_steps: int
    step_number: int
    remaining_steps: int
    user_messages: tuple[UserMessage, ...]
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        actions = self.allowed_actions
        if (not isinstance(actions, tuple) or len(set(actions)) != len(actions)
                or not all(isinstance(name, str) and name in ACTION_NAMES for name in actions)):
            raise ValueError("allowed_actions must be a tuple of distinct Stage 6 actions")

    def read_view(self) -> ControlState:
        """The Stage 5 ControlState of the same decision: every field but the actions."""
        return ControlState(
            virtual_now=self.virtual_now,
            persona_id=self.persona_id,
            allowed_tools=self.allowed_tools,
            max_steps=self.max_steps,
            step_number=self.step_number,
            remaining_steps=self.remaining_steps,
            user_messages=self.user_messages,
            observations=self.observations,
        )


@runtime_checkable
class ActionControlPolicy(Protocol):
    """One Stage 6 control decision per call, from the visible state only."""

    def next_action(self, state: ActionControlState) -> Stage6Action:
        ...
