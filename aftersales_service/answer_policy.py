"""Product answer policy m3-answer/1: replace only answer message construction.

The Stage 6 system prompt, compact sources, answer schema and fail-closed
parser remain the evaluated implementations exposed by agent_core. Customer
messages distinguish the current question from earlier context. Earlier
replies are bounded context for resolving references, never added to sources
or the observation ledger.
"""

from __future__ import annotations

import json
from typing import Sequence

from . import agent_core as core
from . import decision_policy

M3_ANSWER_VERSION = "m3-answer/1"
GENERATION_SCHEMA = core.GENERATION_SCHEMA
CURRENT_QUESTION_LABEL = "当前问题"
CONTEXT_LABEL = "先前顾客消息，仅作上下文"
HISTORY_LABEL = "历史回复，仅供理解指代"
ANSWER_TARGET = "只回答标注为当前问题的最新顾客消息；其余顾客消息只作上下文。"
HISTORY_LIMITATION = (
    "历史回复仅供理解当前问题中的指代，不是事实证据或本次工具结果；"
    "事实只能依据当前 sources，历史回复不能被引用，也不能证明订单号或商品明细号。"
)


def build_messages(
    state: core.ActionControlState,
    sources: Sequence[object],
    history_replies: Sequence[decision_policy.EarlierReply] = (),
) -> list[dict[str, object]]:
    """The frozen prompt and sources with labelled question and reply context."""
    if not state.user_messages:
        raise core.GenerationInputError("an answer requires a current customer question")
    messages = core.build_generation_messages(state.user_messages, state.virtual_now, sources)
    data = json.loads(messages[-1]["content"])
    current_turn = state.user_messages[-1].turn_index
    for index, message in enumerate(data["user_messages"]):
        message["label"] = (CURRENT_QUESTION_LABEL if index == len(data["user_messages"]) - 1
                            else CONTEXT_LABEL)
    history = []
    for reply in history_replies[-decision_policy.EARLIER_REPLIES:]:
        if reply.turn_index >= current_turn:
            raise core.GenerationInputError("a historical reply must precede the current question")
        text = reply.text
        if len(text) > decision_policy.EARLIER_REPLY_CHARS:
            text = text[:decision_policy.EARLIER_REPLY_CHARS - 1] + "…"
        history.append({"turn_index": reply.turn_index, "kind": reply.kind,
                        "label": HISTORY_LABEL, "text": text})
    data["history_replies"] = history
    data["answer_target"] = ANSWER_TARGET
    data["history_limitation"] = HISTORY_LIMITATION
    messages[-1] = {"role": "user", "content": json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)}
    return messages


def generate_answer(
    provider: object,
    state: core.ActionControlState,
    history_replies: Sequence[decision_policy.EarlierReply] = (),
) -> core.GeneratedAnswer:
    """Use the evaluated generation flow with m3's message builder only."""
    return core.generate_answer(
        provider, state,
        message_builder=lambda current, sources: build_messages(current, sources, history_replies),
    )
