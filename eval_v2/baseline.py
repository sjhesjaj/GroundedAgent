"""The Stage 4 deterministic Baseline control policy.

A small, rule-based `ControlPolicy` - the control group for the Stage 5 tool
loop, not a second agent framework. It has no LLM, no randomness, no clock, no
learned routing, no retry, and no memory: every decision is recomputed from the
`ControlState` alone, so the same state always gives the same action.

Each decision
    1. parse the delivered user messages -> intents, order ids, SKUs
    2. map them to a fixed, ordered plan of (tool, arguments) calls
    3. call the first planned call not yet attempted (any outcome counts:
       ok, empty, error / timeout, malformed)
    4. when nothing is left, Finish with a disposition read from the
       observations and the deterministic derived evidence

Parameter binding (design D8)
    Tool arguments come only from the user's text: `_plan` receives the message
    texts and nothing else. Observations are read only to decide what was
    already attempted and how to finish - never to fill an argument. So an
    exchange without an explicit target SKU never reaches get_inventory, even
    when a get_order observation lists the ordered SKU. Chaining observations
    into arguments is the Stage 5 tool loop's job.

Retry
    None. An identical (tool_name, canonical arguments) call is made at most
    once per case-run. The Stage 5 tool loop may attempt one identical call at
    most 3 times per case-run; that is not implemented here.

Scope
    Control decisions only. No answer text, citation or generation: those come
    in Stage 5, with one shared generator for the frozen Baseline and the tool
    loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from orchestration.contracts import ToolStatus

from .control import (
    Clarify,
    ControlAction,
    ControlState,
    Finish,
    ToolCall,
    ToolContractFailure,
    ToolObservation,
)
from .evidence import derive_from_control_state

# The formal experiment's step budget, shared with the Stage 5 comparison.
# Clarify and Finish each consume a control step, so the longest frozen-domain
# flow (Clarify, policy, order, logistics, Finish) needs 5. Supersedes the
# pre-runner design value of 4 (D7). Not configurable from user text.
FORMAL_MAX_STEPS = 5

SEARCH_POLICY = "search_after_sales_policy"
GET_ORDER = "get_order"
GET_LOGISTICS = "get_logistics"
GET_INVENTORY = "get_inventory"
GET_CASE = "get_after_sales_case"
BASELINE_TOOLS = (SEARCH_POLICY, GET_ORDER, GET_LOGISTICS, GET_INVENTORY, GET_CASE)

# Intents, in plan order.
RETURN = "return"
EXCHANGE = "exchange"
QUALITY = "quality"
LOGISTICS = "logistics"
CASE = "case"
INVENTORY = "inventory"

# One small marker tuple per domain intent; a substring hit sets the intent.
_INTENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (RETURN, ("退",)),
    (EXCHANGE, ("换",)),
    (QUALITY, ("质量",)),
    (LOGISTICS, ("物流", "快递", "包裹", "发货", "到哪")),
    (CASE, ("售后单", "进度")),
    (INVENTORY, ("库存", "有货", "缺货")),
)
_RULE_MARKERS = ("规则", "政策", "规定")
_PERSONAL_MARKERS = ("我", "订单")
# A logistics question that also asks about the order's own state.
_ORDER_STATE_MARKERS = ("订单状态", "不一致")
# Boundary depends on the requested operation, never on a claimed identity alone:
#   a side-effect request, or a claimed privilege used to read another
#   customer's records. A claimed privilege with an ordinary read is not one.
_SIDE_EFFECT_MARKERS = ("直接退款", "帮我退款", "给我退款", "帮我办", "直接办", "帮我提交",
                        "直接提交")
_PRIVILEGED_MARKERS = ("店长", "管理员")
_CROSS_IDENTITY_MARKERS = ("其他顾客", "其他客户", "别人的")

# Canonical policy query per topic; a rule question without a topic asks all.
_POLICY_TOPICS = ((RETURN, "退货"), (EXCHANGE, "换货"), (QUALITY, "质量争议"))
_ORDER_INTENTS = frozenset({RETURN, EXCHANGE, LOGISTICS, CASE})

_ORDER_ID = re.compile(r"(?<![A-Z0-9-])ORD-[0-9]+(?![0-9])", re.ASCII | re.IGNORECASE)
_SKU = re.compile(r"(?<![A-Z0-9-])SKU(?:-[A-Z0-9]+)+", re.ASCII | re.IGNORECASE)

# Structured fields of the quality-dispute handoff rule (never its prose).
_HANDOFF_RULE_TYPE = "handoff"
_QUALITY_TRIGGER = "quality_dispute"
_CONFLICT_FACT = "business_state_conflict"

CallKey = tuple[str, tuple[tuple[str, str], ...]]


def call_key(tool_name: str, arguments: Mapping[str, str]) -> CallKey:
    """The canonical identity of a call: tool name + sorted arguments."""
    return tool_name, tuple(sorted((name, arguments[name]) for name in arguments))


# --------------------------------------------------------------------------
# Parsing (user text only)
# --------------------------------------------------------------------------


def _distinct(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


@dataclass(frozen=True)
class ParsedRequest:
    intents: frozenset[str]
    order_ids: tuple[str, ...]
    skus: tuple[str, ...]
    rule_question: bool
    personal: bool
    order_state: bool
    boundary: bool


def parse_request(texts: tuple[str, ...]) -> ParsedRequest:
    """Deterministic parse of the delivered user message texts, in order."""
    text = "\n".join(texts)
    return ParsedRequest(
        intents=frozenset(intent for intent, markers in _INTENT_MARKERS
                          if any(marker in text for marker in markers)),
        order_ids=_distinct([match.upper() for match in _ORDER_ID.findall(text)]),
        skus=_distinct([match.upper() for match in _SKU.findall(text)]),
        rule_question=any(marker in text for marker in _RULE_MARKERS),
        personal=any(marker in text for marker in _PERSONAL_MARKERS),
        order_state=any(marker in text for marker in _ORDER_STATE_MARKERS),
        boundary=(_has(text, _SIDE_EFFECT_MARKERS)
                  or (_has(text, _PRIVILEGED_MARKERS) and _has(text, _CROSS_IDENTITY_MARKERS))),
    )


# --------------------------------------------------------------------------
# Planning (user text only - no observation reaches this code)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    calls: tuple[ToolCall, ...] = ()
    missing_slot: str | None = None
    boundary: bool = False
    quality: bool = False


def _policy_query(intents: frozenset[str]) -> str:
    topics = [query for intent, query in _POLICY_TOPICS if intent in intents]
    return " ".join(topics or [query for _, query in _POLICY_TOPICS])


def plan_calls(texts: tuple[str, ...]) -> Plan:
    """The ordered calls a request needs, or the slot that blocks it."""
    request = parse_request(texts)
    if request.boundary:
        return Plan(boundary=True)
    intents = request.intents
    quality = QUALITY in intents
    policy_only = (not request.order_ids and (request.rule_question or not request.personal)
                   and not intents & {LOGISTICS, CASE, INVENTORY})
    # A bare rule word ("规则") asks for policy only when nothing else is asked.
    if intents & {RETURN, EXCHANGE, QUALITY} or (policy_only and request.rule_question):
        calls = [ToolCall(tool_name=SEARCH_POLICY,
                          arguments={"query": _policy_query(intents)})]
    else:
        calls = []
    if policy_only:
        return Plan(calls=tuple(calls), quality=quality)
    if intents & _ORDER_INTENTS and not request.order_ids:
        return Plan(missing_slot="order_id")
    if intents == {INVENTORY} and not request.skus:
        return Plan(missing_slot="target_sku")
    for order_id in request.order_ids:
        tools = []
        if (intents & {RETURN, EXCHANGE, QUALITY} or not intents
                or (LOGISTICS in intents and request.order_state)):
            tools.append(GET_ORDER)
        if RETURN in intents or LOGISTICS in intents:
            tools.append(GET_LOGISTICS)
        if CASE in intents:
            tools.append(GET_CASE)
        calls.extend(ToolCall(tool_name=tool, arguments={"order_id": order_id})
                     for tool in tools)
    # Inventory only for SKUs the user wrote; never an observed SKU.
    if intents & {EXCHANGE, INVENTORY} or not intents:
        calls.extend(ToolCall(tool_name=GET_INVENTORY, arguments={"sku": sku})
                     for sku in request.skus)
    return Plan(calls=tuple(calls), quality=quality)


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------


def _attempted(state: ControlState) -> dict[CallKey, object]:
    """Every call already made, by canonical identity -> its observation."""
    seen: dict[CallKey, object] = {}
    for observation in state.observations:
        seen.setdefault(call_key(observation.tool_name, observation.arguments), observation)
    return seen


def _failed(observation: object) -> bool:
    return (type(observation) is ToolContractFailure
            or (type(observation) is ToolObservation
                and observation.result.status is ToolStatus.ERROR))


def _status(observation: object) -> ToolStatus | None:
    return observation.result.status if type(observation) is ToolObservation else None


class Stage4BaselinePolicy:
    """Deterministic, rule-based ControlPolicy. Stateless: one decision per state."""

    def next_action(self, state: ControlState) -> ControlAction:
        texts = tuple(message.text for message in state.user_messages)
        plan = plan_calls(texts)
        if plan.boundary:
            return Finish(disposition="boundary")
        if plan.missing_slot is not None:
            # One clarification per case-run: later turns exist only if one was answered.
            if len(texts) == 1 and state.remaining_steps > 1:
                return Clarify(slots=(plan.missing_slot,))
            return Finish(disposition="refuse")
        if not plan.calls:
            return Finish(disposition="refuse")
        attempted = _attempted(state)
        allowed = set(state.allowed_tools) & set(BASELINE_TOOLS)
        if state.remaining_steps > 1:
            for call in plan.calls:
                if (call.tool_name in allowed
                        and call_key(call.tool_name, call.arguments) not in attempted):
                    return call
        return Finish(disposition=self._disposition(state, plan, attempted))

    @staticmethod
    def _disposition(state: ControlState, plan: Plan,
                     attempted: dict[CallKey, object]) -> str:
        observed = [attempted.get(call_key(call.tool_name, call.arguments))
                    for call in plan.calls]
        # A successful lookup that found no accessible order is itself the answer.
        if any(call.tool_name == GET_ORDER and _status(observation) is ToolStatus.EMPTY
               for call, observation in zip(plan.calls, observed)):
            return "answer"
        if any(observation is None or _failed(observation) for observation in observed):
            return "refuse"
        # Integrity faults in the observations propagate, as in the scorer.
        derived = derive_from_control_state(state).derived_evidence
        if any(fact.fact_key == _CONFLICT_FACT and fact.value is True for fact in derived):
            return "refuse"
        if plan.quality and any(
                call.tool_name == SEARCH_POLICY and _status(observation) is ToolStatus.OK
                and _states_quality_handoff(observation)
                for call, observation in zip(plan.calls, observed)):
            return "handoff"
        return "answer"


def _states_quality_handoff(observation: ToolObservation) -> bool:
    """An observed rule's structured fields say quality disputes go to a human."""
    for evidence in observation.result.evidence:
        metadata = evidence.metadata
        if (isinstance(metadata, Mapping) and metadata.get("rule_type") == _HANDOFF_RULE_TYPE
                and metadata.get("field") == "trigger"
                and metadata.get("value") == _QUALITY_TRIGGER):
            return True
    return False
