"""The `/api/aftersales/*` HTTP surface (M0). Thin: validation and status codes only.

Every request body is closed (`extra="forbid"`): a browser cannot send a
customer id, an approver, a role, an approval flag or any other field. The
operator decision body names only the pending action being acted on and
APPROVE / REJECT. See docs/v2/m0-a1-aftersales-runtime.md.

Authentication (auth.py): every endpoint needs `Authorization: Bearer <token>`
(401 without a valid one) and the right role (403 otherwise):

    any role    GET  /demo
    customer    POST /sessions, GET /sessions/{id}, POST /sessions/{id}/messages
                - the persona is the token's; another customer's session is 404
    operator    POST /demo/reset, GET /operator/sessions, GET /operator/sessions/{id},
                POST /operator/sessions/{id}/decision - the approver is the token's
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from aftersales.approval import PENDING_ACTION_ID_PATTERN
from aftersales.demo import DEMO_PERSONAS
from auth import CUSTOMER, OPERATOR, AuthUser, get_current_user, require_role

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
    """Optional (the frontend sends no body): the persona is always the token's.

    A body, if sent, must name the caller's own persona; any other persona is
    refused (403), never used. Kept for clients that name it explicitly.
    """
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
    customer = require_role(CUSTOMER)
    operator = require_role(OPERATOR)

    @router.get("/demo")
    def demo_info(user: AuthUser = Depends(get_current_user)) -> dict:
        return service.demo_info()

    @router.post("/demo/reset")
    def reset_demo(user: AuthUser = Depends(operator)) -> dict:
        return service.reset()

    @router.post("/sessions", status_code=201)
    def create_session(body: CreateSessionBody | None = None,
                       user: AuthUser = Depends(customer)) -> dict:
        claimed = None if body is None else body.persona_id
        if claimed is not None and claimed != user.persona_id:
            # An unknown id keeps its old answer; a known one is someone else's persona.
            if claimed not in DEMO_PERSONAS:
                raise _http_error(UnknownPersona("unknown persona"))
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        try:
            return service.create_session(user.persona_id)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str = session_path, user: AuthUser = Depends(customer)) -> dict:
        try:
            return service.session_view(session_id, persona_id=user.persona_id)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.post("/sessions/{session_id}/messages")
    def post_message(body: MessageBody, session_id: str = session_path,
                     user: AuthUser = Depends(customer)) -> dict:
        if not body.text.strip():
            raise HTTPException(status_code=422, detail={"code": "empty_message"})
        try:
            return service.submit(session_id, body.text, persona_id=user.persona_id)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.get("/operator/sessions")
    def operator_sessions(user: AuthUser = Depends(operator)) -> dict:
        return {"items": service.operator_sessions()}

    @router.get("/operator/sessions/{session_id}")
    def operator_session(session_id: str = session_path, user: AuthUser = Depends(operator)) -> dict:
        try:
            return service.session_view(session_id)
        except ConversationError as error:
            raise _http_error(error) from None

    @router.post("/operator/sessions/{session_id}/decision")
    def operator_decision(body: OperatorDecisionBody, session_id: str = session_path,
                          user: AuthUser = Depends(operator)) -> dict:
        try:
            return service.decide(session_id, body.pending_action_id, body.decision,
                                  approver_ref=user.operator_ref)
        except ConversationError as error:
            raise _http_error(error) from None

    return router


default_service = AftersalesService()
router = create_router(default_service)
