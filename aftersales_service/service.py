"""The after-sales demo service: conversations over one shared demo store.

    one DemoStore per process (built lazily on first use, rebuilt on reset)
        a mutable demo database + the trusted configuration around it
    sessions: session_id -> Conversation, each bound for life to one
        server-side persona (DEMO_PERSONAS). The session id is server-generated
        and unguessable; it is the only handle a browser holds.

The browser chooses a persona id from the server-side list - a demo stand-in
for a login, not authentication - and never supplies a customer id, an
approver, a role or an approval. Whoever holds a session id may act as that
session's persona and, through the operator endpoint, as the demo operator
op-demo-1 for that session's pending actions: a demo boundary, to be replaced
by real authentication before any non-local deployment.

Concurrency: one lock per conversation serializes its turns; a registry lock
guards the session map and the store. Reset takes the registry lock, then
every conversation's lock (waiting for in-flight turns), closes them all and
swaps in a fresh store. A turn never takes the registry lock while it holds a
conversation lock, so the order is fixed and cannot deadlock.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator

from aftersales.action_errors import ApprovalInputError, UnknownPendingAction
from aftersales.demo import DEMO_PERSONAS

from .conversation import (
    Conversation,
    ConversationError,
    PendingActionNotInConversation,
    TurnFailed,
)
from .demo_store import DemoStore

logger = logging.getLogger(__name__)

MAX_SESSIONS = 64

DEMO_BOUNDARY_NOTICE = (
    "Demo boundary, not authentication: personas are server-side demo stand-ins and the "
    "operator decision endpoint acts as the server-side demo operator.")


class SessionNotFound(ConversationError):
    code = "session_not_found"


class UnknownPersona(ConversationError):
    code = "unknown_persona"


class TooManySessions(ConversationError):
    code = "too_many_sessions"


class DecisionRefused(ConversationError):
    code = "decision_refused"


def default_provider() -> object:
    """The configured chat provider (llm_provider), resolved per request."""
    import llm_provider

    return llm_provider.get_provider()


class AftersalesService:
    def __init__(self, provider_factory: Callable[[], object] | None = None, *,
                 max_sessions: int = MAX_SESSIONS) -> None:
        self._provider_factory = default_provider if provider_factory is None else provider_factory
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._store: DemoStore | None = None
        self._sessions: dict[str, Conversation] = {}

    # -- store ---------------------------------------------------------------

    def _require_store(self) -> DemoStore:
        """Caller holds the registry lock."""
        if self._store is None:
            self._store = DemoStore()
        return self._store

    @property
    def store(self) -> DemoStore:
        with self._lock:
            return self._require_store()

    def _info(self, store: DemoStore) -> dict[str, object]:
        return {
            "business_time": store.business_time_iso,
            "personas": [{"persona_id": persona.persona_id, "display_name": persona.display_name}
                         for persona in store.personas.values()],
            "operator": {"approver_ref": store.operator_ref, "notice": DEMO_BOUNDARY_NOTICE},
            "active_sessions": len(self._sessions),
        }

    def demo_info(self) -> dict[str, object]:
        with self._lock:
            return self._info(self._require_store())

    def reset(self) -> dict[str, object]:
        """Drop every conversation and rebuild the demo database from the seed."""
        with self._lock:
            sessions = list(self._sessions.values())
            for conversation in sessions:
                conversation.lock.acquire()
            try:
                for conversation in sessions:
                    conversation.closed = True
                old, self._store, self._sessions = self._store, DemoStore(), {}
            finally:
                for conversation in sessions:
                    conversation.lock.release()
            if old is not None:
                old.close()
            return self._info(self._store)

    def close(self) -> None:
        with self._lock:
            for conversation in self._sessions.values():
                conversation.closed = True
            self._sessions = {}
            if self._store is not None:
                self._store.close()
                self._store = None

    # -- sessions ------------------------------------------------------------

    def create_session(self, persona_id: str) -> dict[str, object]:
        persona = DEMO_PERSONAS.get(persona_id) if isinstance(persona_id, str) else None
        if persona is None:
            raise UnknownPersona("unknown persona")
        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise TooManySessions("too many demo sessions; reset the demo")
            session_id = uuid.uuid4().hex
            conversation = Conversation(session_id=session_id, persona=persona,
                                        store=self._require_store())
            self._sessions[session_id] = conversation
        with conversation.lock:
            return conversation.view()

    @contextmanager
    def _locked(self, session_id: str) -> Iterator[Conversation]:
        with self._lock:
            conversation = self._sessions.get(session_id)
        if conversation is None:
            raise SessionNotFound("no such session")
        with conversation.lock:
            if conversation.closed:
                raise SessionNotFound("the session was reset")
            yield conversation

    def session_view(self, session_id: str) -> dict[str, object]:
        with self._locked(session_id) as conversation:
            return conversation.view()

    def submit(self, session_id: str, text: str) -> dict[str, object]:
        with self._locked(session_id) as conversation:
            try:
                provider = self._provider_factory()
            except Exception:
                logger.exception("after-sales turn: the chat provider is not available")
                raise TurnFailed("llm_unavailable") from None
            try:
                return conversation.submit(text, provider)
            except TurnFailed as error:
                logger.warning("after-sales turn failed (%s); nothing was recorded", error.code,
                               exc_info=error.__cause__)
                raise

    def decide(self, session_id: str, pending_action_id: str, decision: str) -> dict[str, object]:
        """The trusted operator decision, for a pending action of this session only."""
        with self._locked(session_id) as conversation:
            try:
                return conversation.decide(pending_action_id, decision)
            except UnknownPendingAction:
                raise PendingActionNotInConversation("no such pending action") from None
            except ApprovalInputError:
                raise DecisionRefused("the decision was refused") from None
