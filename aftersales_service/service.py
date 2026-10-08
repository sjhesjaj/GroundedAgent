"""The after-sales demo service: conversations that survive a restart.

    one data directory (AFTERSALES_DATA_DIR, default .aftersales-demo/) whose
        active generation holds the demo business database, the checkpoints
        of every conversation and their session files (persistence.py). It is
        opened once, lazily on first use or by `start` at application start-up;
        opening it runs the start-up scan, which recovers every session whose
        file holds an in-flight marker, even one no customer comes back to.
        Reset switches to a new generation.
    sessions: session_id -> Conversation, the conversations in use, each bound
        for life to one server-side persona (DEMO_PERSONAS). A session that is
        not in memory is rebuilt from its session file - and recovered - by
        the first request that names it. The session id is server-generated
        and unguessable; it is the only handle a browser holds.

The browser chooses a persona id from the server-side list - a demo stand-in
for a login, not authentication - and never supplies a customer id, an
approver, a role or an approval. Whoever holds a session id may act as that
session's persona and, through the operator endpoint, as the demo operator
op-demo-1 for that session's pending actions: a demo boundary, to be replaced
by real authentication before any non-local deployment.

Concurrency: a single process. One lock per conversation serializes its turns,
decisions, recovery and session-file writes; a registry lock guards the
session map and the generation. Reset takes the registry lock, then every
conversation's lock (waiting for in-flight work), closes them all and switches
the generation. A request never takes the registry lock while it holds a
conversation lock, so the order is fixed and cannot deadlock.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from aftersales.action_errors import ApprovalInputError, UnknownPendingAction
from aftersales.demo import DEMO_PERSONAS

from .conversation import (
    Conversation,
    ConversationError,
    PendingActionNotInConversation,
    RecoveryPending,
    TurnFailed,
)
from .demo_store import DemoStore
from .persistence import HEX_ID, DataDirectory, Generation, PersistenceError, data_directory

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
                 max_sessions: int = MAX_SESSIONS, data_dir: str | Path | None = None) -> None:
        self._provider_factory = default_provider if provider_factory is None else provider_factory
        self._max_sessions = max_sessions
        self._configured_dir = data_dir   # resolved (and created) when first opened
        self._lock = threading.Lock()
        self._data: DataDirectory | None = None
        self._runtime: Generation | None = None
        self._sessions: dict[str, Conversation] = {}

    # -- the data directory -------------------------------------------------

    def _require_runtime(self) -> Generation:
        """Caller holds the registry lock. Opens the active generation once."""
        if self._runtime is None:
            data = DataDirectory(data_directory(self._configured_dir))
            data.root.mkdir(parents=True, exist_ok=True)
            generation = data.active()
            if generation is None:
                generation = data.create()
                data.activate(generation)
            data.remove_unnamed(generation)
            self._data, self._runtime = data, Generation(data, generation)
            self._recover_inflight_sessions()
        return self._runtime

    def _recover_inflight_sessions(self) -> None:
        """The start-up scan. Caller holds the registry lock (then each conversation's)."""
        for session_id in self._runtime.sessions.with_inflight():
            try:
                conversation = self._cached(session_id)
                if conversation is None:
                    continue
                with conversation.lock:
                    conversation.ensure_loaded()
            except RecoveryPending:
                logger.warning("start-up recovery failed for a session; it answers recovery_pending")

    def start(self) -> None:
        """Open the data directory now and run the start-up scan. Idempotent."""
        with self._lock:
            self._require_runtime()

    @property
    def store(self) -> DemoStore:
        with self._lock:
            return self._require_runtime().store

    def _info(self, runtime: Generation) -> dict[str, object]:
        store = runtime.store
        return {
            "business_time": store.business_time_iso,
            "personas": [{"persona_id": persona.persona_id, "display_name": persona.display_name}
                         for persona in store.personas.values()],
            "operator": {"approver_ref": store.operator_ref, "notice": DEMO_BOUNDARY_NOTICE},
            "active_sessions": runtime.sessions.count(),
        }

    def demo_info(self) -> dict[str, object]:
        with self._lock:
            return self._info(self._require_runtime())

    def reset(self) -> dict[str, object]:
        """Drop every conversation and switch to a new generation, seeded from the seed files."""
        with self._lock:
            old = self._require_runtime()
            sessions = list(self._sessions.values())
            for conversation in sessions:
                conversation.lock.acquire()
            try:
                # Seeded completely before generation.json names it.
                generation = self._data.create()
                fresh = Generation(self._data, generation)
                try:
                    self._data.activate(generation)
                except BaseException:
                    fresh.close()
                    self._data.remove(generation)
                    raise
                for conversation in sessions:
                    conversation.closed = True
                self._runtime, self._sessions = fresh, {}
                old.close()
                self._data.remove(old.generation)
            finally:
                for conversation in sessions:
                    conversation.lock.release()
            return self._info(self._runtime)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            for conversation in sessions:
                conversation.lock.acquire()
            try:
                for conversation in sessions:
                    conversation.closed = True
                self._sessions = {}
                if self._runtime is not None:
                    self._runtime.close()
                    self._runtime = None
            finally:
                for conversation in sessions:
                    conversation.lock.release()

    # -- sessions ------------------------------------------------------------

    def create_session(self, persona_id: str) -> dict[str, object]:
        persona = DEMO_PERSONAS.get(persona_id) if isinstance(persona_id, str) else None
        if persona is None:
            raise UnknownPersona("unknown persona")
        with self._lock:
            runtime = self._require_runtime()
            if runtime.sessions.count() >= self._max_sessions:
                raise TooManySessions("too many demo sessions; reset the demo")
            session_id = uuid.uuid4().hex
            conversation = Conversation.create(session_id=session_id, persona=persona,
                                               runtime=runtime)
            self._sessions[session_id] = conversation
        with conversation.lock:
            return conversation.view()

    def _cached(self, session_id: str) -> Conversation | None:
        """The conversation of this generation's session, not yet loaded if new to memory.

        Caller holds the registry lock.
        """
        conversation = self._sessions.get(session_id)
        if conversation is None:
            runtime = self._require_runtime()
            try:
                manifest = runtime.sessions.read(session_id)
            except PersistenceError:
                raise RecoveryPending("the session file cannot be read") from None
            if manifest is None:
                return None
            persona = DEMO_PERSONAS.get(manifest["persona_id"])
            if persona is None:
                raise RecoveryPending("the session file names an unknown persona")
            conversation = Conversation(session_id=session_id, persona=persona, runtime=runtime)
            self._sessions[session_id] = conversation
        return conversation

    @contextmanager
    def _locked(self, session_id: str) -> Iterator[Conversation]:
        if not isinstance(session_id, str) or not HEX_ID.fullmatch(session_id):
            raise SessionNotFound("no such session")
        with self._lock:
            conversation = self._cached(session_id)
        if conversation is None:
            raise SessionNotFound("no such session")
        with conversation.lock:
            if conversation.closed:
                raise SessionNotFound("the session was reset")
            # Rebuilt from its session file and recovered before anything touches it.
            conversation.ensure_loaded()
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
