"""GroundedAgent V2 product runtime (M0): the after-sales customer-service agent.

A thin composition around the frozen Stage 6 domain (aftersales/) and the
evaluated Stage 6 agent core (see agent_core.py for the dependency strategy):

    demo_store.py     mutable demo database + trusted server-side configuration
    conversation.py   one resumable conversation: control loop, clarification
                      pause/resume, the single action write path, operator decision
    observation_provenance.py   M1-A1: registered reads as immutable structured
                      observations; the set each decision can see
    action_grounding.py         M1-A1: the grounding gate before the write path
                      and the grounded-submission (replay) index
    service.py        sessions, locks, reset
    routes.py         the /api/aftersales/* HTTP surface

Nothing here is evaluation code: no case, label, oracle, dataset, operator
script or fault injection. Re-exports only; no work at import time.
"""

from __future__ import annotations

from .conversation import Conversation, ConversationStatus
from .demo_store import DEMO_OPERATOR_REF, DemoStore
from .service import AftersalesService

__all__ = [
    "DEMO_OPERATOR_REF",
    "AftersalesService",
    "Conversation",
    "ConversationStatus",
    "DemoStore",
]
