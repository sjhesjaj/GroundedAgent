"""The reused Stage 6 agent core: the ONE product module that imports eval_v2.

The control protocol, the LLM-native action loop policy and the answer
generation layer were built and formally evaluated under `eval_v2/`. They are
label-free agent logic, so the product composes them exactly as evaluated
instead of copying them:

    eval_v2.control         ToolCall / Clarify / Finish / UserMessage / ToolObservation
    eval_v2.action_control  ActionIntent, ActionControlState, require_stage6_action,
                            STAGE6_MAX_STEPS
    eval_v2.action_loop     LLMNativeActionLoopPolicy - the untrusted proposer
                            (prompt, native schemas, fail-closed translation)
    eval_v2.tool_loop       ToolLoopProtocolError (an integration error type)
    eval_v2.evidence        derive_from_control_state - label-free evidence derivation
    eval_v2.generation      the answer layer's pure functions (sources, prompt,
                            protocol, fixed non-answer texts)

M3 (docs/v2/m3-policy-rag.md): the product decision policy m3-decision/1
(decision_policy.py) composes the public action_loop / tool_loop helpers
re-exported below the main imports. It replaces the prompt, the offered
functions, the schemas, the translation and the earlier replies it shows;
everything else is the evaluated loop.

Dependency strategy (M0). Import-only reuse, confined to this module and
pinned by an AST allowlist test. Moving these modules out of eval_v2 would
rewrite frozen Stage 5/6 files whose bytes are pinned by tests and would make
the product policy differ from the evaluated one; duplicating them would be
~1,500 lines that drift. The cost is a product -> eval_v2 import (and the
eval_v2 package initializer importing the harness); extracting a neutral agent
package is a later step.

Never used here - evaluation harness only: eval_v2.action_runner (pre-scripted
conditional user turns, one synchronous run), eval_v2.stage6_runner /
stage6_runtime / stage6_state / stage6_scoring / stage6_oracle (case runtime,
operator_script, fault injection, reruns, scoring), eval_v2.faults / dataset /
runner / runtime / scoring / baseline / e2e, and every formal=True mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from eval_v2.action_control import (
    STAGE6_MAX_STEPS,
    ActionControlState,
    ActionIntent,
    require_stage6_action,
)
from eval_v2.action_loop import LLMNativeActionLoopPolicy
from eval_v2.control import (
    Clarify,
    ControlPolicyContractError,
    Finish,
    ToolCall,
    ToolObservation,
    UserMessage,
    clarification_slots,
)
from eval_v2.evidence import EvalEvidenceError, derive_from_control_state
from eval_v2.generation import (
    FIXED_RESPONSES,
    GENERATION_SCHEMA,
    GENERATION_MAX_TOKENS,
    GENERATION_TEMPERATURE,
    GenerationInputError,
    GenerationProtocolError,
    answer_response_schema,
    build_messages as build_generation_messages,
    build_sources,
    parse_answer,
)
from eval_v2.tool_loop import ToolLoopProtocolError

# M3: the evaluated loop's public helpers, for the product decision policy
# m3-decision/1 (decision_policy.py). It composes them; it never copies the
# loop or subclasses the evaluated policy.
from eval_v2.action_loop import (
    DIAG_ACTION_NOT_SINGLE_CALL,
    STAGE6_ACTION_FUNCTIONS,
    STAGE6_SYSTEM_PROMPT,
    ActionLoopDecisionRecord,
    ActionTranslation,
    NativeActionCall,
    build_action_messages,
    stage6_offered_functions,
    stage6_tool_schemas,
    translate_action_response,
)
from eval_v2.tool_loop import (
    CONTROL_FUNCTIONS,
    DIAG_BATCH_EXCEEDS_STEP_BUDGET,
    DIAG_FUNCTION_NOT_OFFERED,
    DIAG_IDENTITY_ARGUMENT,
    DIAG_INVALID_ARGUMENTS,
    DIAG_MULTIPLE_TOOL_CALLS,
    DIAG_NO_TOOL_CALL,
    DIAG_RETRY_CAP_EXCEEDED,
    DIAG_TOOL_NOT_ALLOWED,
    DIAG_UNKNOWN_FUNCTION,
    RETRY_CAP,
    STAGE5_RUNTIME_TOOLS,
    TOOL_LOOP_MAX_TOKENS,
    TOOL_LOOP_TEMPERATURE,
    UNKNOWN_FUNCTION_NAME,
    NativeCallEnvelope,
    call_key,
    envelope_matches,
    prior_attempts,
    runtime_tool_specs,
)

__all__ = [
    "FIXED_RESPONSES",
    "GENERATION_SCHEMA",
    "STAGE6_MAX_STEPS",
    "ActionControlState",
    "ActionIntent",
    "AnswerUnavailable",
    "Clarify",
    "ControlPolicyContractError",
    "Finish",
    "GeneratedAnswer",
    "ToolCall",
    "ToolLoopProtocolError",
    "ToolObservation",
    "UserMessage",
    "clarification_slots",
    "generate_answer",
    "new_control_policy",
    "require_stage6_action",
]


def new_control_policy(provider: object) -> LLMNativeActionLoopPolicy:
    """A fresh evaluated Stage 6 policy for one HTTP request.

    Never formal: a formal policy runs on DeepSeek only and refuses to replay
    an observation it did not produce itself, which cannot hold once a
    conversation pauses between requests. Observations from earlier requests
    are replayed with the policy's own synthetic call ids instead.
    """
    return LLMNativeActionLoopPolicy(provider, formal=False)


class AnswerUnavailable(RuntimeError):
    """The answer layer could not produce a grounded answer. `code` is closed."""

    def __init__(self, code: str) -> None:
        super().__init__("answer unavailable: " + code)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class GeneratedAnswer:
    text: str
    citations: tuple[dict[str, object], ...]
    provider: str | None
    model: str | None


def generate_answer(
    provider: object,
    state: ActionControlState,
    *,
    message_builder: Callable[[ActionControlState, Sequence[object]],
                              list[dict[str, object]]] | None = None,
) -> GeneratedAnswer:
    """The evaluated answer layer for a Finish("answer"), over this conversation's state.

    Sources are the label-free evidence derived from the observations; the
    reply must follow the frozen JSON answer protocol with citations drawn only
    from those sources. A protocol or evidence failure is AnswerUnavailable;
    provider / network errors propagate unchanged (an outage is not an answer).
    M3 may inject only message construction; the default uses the frozen
    messages and the rest of the generation flow is shared by both paths.
    """
    try:
        evidence = derive_from_control_state(state.read_view())
        sources = build_sources(evidence)
    except (EvalEvidenceError, GenerationInputError):
        raise AnswerUnavailable("evidence_unavailable") from None
    messages = (build_generation_messages(state.user_messages, state.virtual_now, sources)
                if message_builder is None else message_builder(state, sources))
    response = provider.chat(
        messages,
        response_format=answer_response_schema(),
        temperature=GENERATION_TEMPERATURE,
        max_tokens=GENERATION_MAX_TOKENS,
    )
    try:
        text, refs = parse_answer(getattr(response, "content", None),
                                  [source.ref for source in sources])
    except GenerationProtocolError as error:
        raise AnswerUnavailable(error.code) from None
    by_ref = {source.ref: source for source in sources}
    citations = tuple({"ref": ref, "producer": by_ref[ref].producer,
                       "source_type": by_ref[ref].source_type, "locator": by_ref[ref].locator}
                      for ref in refs)
    model = getattr(response, "model", None)
    return GeneratedAnswer(text=text, citations=citations,
                           provider=getattr(provider, "name", None),
                           model=model if isinstance(model, str) else None)
