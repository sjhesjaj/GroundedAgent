"""The product control flow as a LangGraph StateGraph (docs/v2/m2-session-recovery.md).

    START -> begin_run -> decide --ToolCall-----> read -> decide
                                 --Clarify------> clarify -> END
                                 --ActionIntent-> ground --rejected--> END
                                                         --grounded--> [stop] gateway -> END
                                 --Finish-------> finish -> END
                                 --step limit---> step_limit -> END

Every customer message - the answer to a clarification included - enters as
ordinary input from the conversation's committed head; `begin_run` either
starts a new run or continues the run that is open on a clarification. There
is no interrupt() / Command(resume=...): resuming an interrupt stores the
answer on the head checkpoint itself, so a retry after a failed answer would
reach the policy with the failed answer. The only static stop is
interrupt_before=["gateway"]: the grounded submission is checkpointed before
the one write path runs, and the gateway is resumed with None, never with
customer data.

Nodes are thin: the Conversation that owns the state does the work, exactly as
its control loop did before M2. Runtime objects (the conversation, the policy,
the guarded provider, the read side) reach nodes through LangGraph's runtime
context and never enter state; state holds only the codec's JSON values
(conversation_state.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from .conversation_state import ConversationState

READ = "read"
CLARIFY = "clarify"
GROUND = "ground"
GATEWAY = "gateway"
FINISH = "finish"
STEP_LIMIT = "step_limit"
DECISION_ROUTES = (READ, CLARIFY, GROUND, FINISH, STEP_LIMIT)
GROUND_ROUTES = (GATEWAY, END)

# Every invocation writes its checkpoints before the next node runs.
DURABILITY = "sync"
# A run is at most six decisions: begin_run + 6 x (decide + read) + decide + step_limit.
RECURSION_LIMIT = 32


def strict_serializer() -> JsonPlusSerializer:
    """The checkpointer's serializer, strict by construction.

    Not LANGGRAPH_STRICT_MSGPACK: that variable is read once, when langgraph is
    first imported, so setting it later is silently ignored. Strict mode does
    not raise in 1.2.14 (a blocked object comes back as a dict); the codec's
    decode is what refuses anything that is not its own JSON.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=None)


@dataclass
class ConversationContext:
    """Runtime context of one invocation. Never checkpointed."""

    conversation: object
    policy: object = None
    provider: object = None
    reader: object = None
    gateway_returned: bool = False   # set once ActionGateway.start_action has returned


def begin_run(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._begin_run(state)


def decide(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._drive(state, runtime.context.policy)


def read(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._read_step(state, runtime.context.reader)


def clarify(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._clarify(state)


def ground(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._act(state, GROUND, runtime.context)


def gateway(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._act(state, GATEWAY, runtime.context)


def finish(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._finish_step(state, runtime.context.provider)


def step_limit(state: ConversationState, runtime: Runtime[ConversationContext]):
    return runtime.context.conversation._step_limit(state)


def route(state: ConversationState) -> str:
    return state["route"]


def build_graph(checkpointer):
    graph = StateGraph(ConversationState, context_schema=ConversationContext)
    for node in (begin_run, decide, read, clarify, ground, gateway, finish, step_limit):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "begin_run")
    graph.add_edge("begin_run", "decide")
    graph.add_conditional_edges("decide", route, list(DECISION_ROUTES))
    graph.add_edge(READ, "decide")
    graph.add_conditional_edges(GROUND, route, list(GROUND_ROUTES))
    for node in (CLARIFY, GATEWAY, FINISH, STEP_LIMIT):
        graph.add_edge(node, END)
    return graph.compile(checkpointer=checkpointer, interrupt_before=[GATEWAY])
