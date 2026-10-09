"""The `/api/aftersales/*` HTTP surface (M0). Thin: validation and status codes only.

Every request body is closed (`extra="forbid"`): a browser cannot send a
customer id, an approver, a role, an approval flag or any other field. The
operator decision body names only the pending action being acted on and
APPROVE / REJECT; the approver is server-side. See docs/v2/m0-a1-aftersales-runtime.md.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from aftersales.approval import PENDING_ACTION_ID_PATTERN

from .conversation import (
    ConversationError,
    ConversationFull,
    PendingActionNotGrounded,
    PendingActionNotInConversation,
    PolicyVersionMismatch,
    RecoveryPending,
    TurnFailed,
)
from .service import (
    AftersalesService,
    DecisionRefused,
    SessionNotFound,
    TooManySessions,
    UnknownPersona,
)

SESSION_ID_PATTERN = r"^[0-9a-f]{32}$"

_STATUS_CODES = {
    SessionNotFound: 404,
    PendingActionNotInConversation: 404,
    UnknownPersona: 422,
    ConversationFull: 409,
    DecisionRefused: 409,
    PendingActionNotGrounded: 409,
    PolicyVersionMismatch: 409,
    RecoveryPending: 409,
    TooManySessions: 429,
}
_TURN_STATUS_CODES = {"llm_unavailable": 503, "agent_internal_error": 500}


class _ClosedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSessionBody(_ClosedBody):
    persona_id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9-]+$")


class MessageBody(_ClosedBody):
    text: str = Field(min_length=1, max_length=2000)


class OperatorDecisionBody(_ClosedBody):
    pending_action_id: str = Field(pattern=PENDING_ACTION_ID_PATTERN.pattern)
    decision: Literal["APPROVE", "REJECT"]


def _http_error(error: ConversationError) -> HTTPException:
    if isinstance(error, TurnFailed):
        status = _TURN_STATUS_CODES.get(error.code, 500)
    else:
        status = next((code for kind, code in _STATUS_CODES.items() if isinstance(error, kind)), 400)
    detail = {"code": error.code}
    if isinstance(error, TurnFailed) and error.trace is not None:
        detail["trace"] = error.trace
    return HTTPException(status_code=status, detail=detail)


def create_router(service: AftersalesService) -> APIRouter:
    # At application start-up: open the data directory and recover every session
    # with an in-flight marker (idempotent; without it, the first request does it).
    router = APIRouter(prefix="/api/aftersales", tags=["aftersales"], on_startup=[service.start])
    session_path = Path(pattern=SESSION_ID_PATTERN)

    @router.get("/demo")
    def demo_info() -> dict:
        return service.demo_info()

    @router.post("/demo/reset")
    def reset_demo() -> dict:
        return service.reset()

    @router.post("/sessions", status_code=201)
    def create_session(body: CreateSessionBody) -> dict:
        try:
            return service.create_session(body.persona_id)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str = session_path) -> dict:
        try:
            return service.session_view(session_id)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.post("/sessions/{session_id}/messages")
    def post_message(body: MessageBody, session_id: str = session_path) -> dict:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail={"code": "empty_message"})
        try:
            return service.submit(session_id, body.text)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.post("/operator/sessions/{session_id}/decision")
    def operator_decision(body: OperatorDecisionBody, session_id: str = session_path) -> dict:
        try:
            return service.decide(session_id, body.pending_action_id, body.decision)
        except ConversationError as error:
            raise _http_error(error) from None

    return router


default_service = AftersalesService()
router = create_router(default_service)
