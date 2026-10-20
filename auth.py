"""Login and bearer-token authentication for /api/aftersales/* (minimal JWT, /auth/*).

    users        a small SQLite table of its own (AFTERSALES_USERS_DB, default
                 <data dir>/users.sqlite3), bcrypt password hashes, seeded with
                 the demo accounts on first open. It is not part of a demo
                 generation: /demo/reset does not touch it.
    identity     a customer account names its server-side persona (aftersales/demo.py),
                 the persona names the customer id the Guard checks; an operator
                 account names its approver ref in the trusted operator registry.
                 Both come from the users row the token's `sub` names - never
                 from a request body.
    token        POST /auth/login -> HS256 access token {sub=user id, role, iat, exp},
                 valid for 2 hours. No refresh, no revocation: logout is the
                 browser dropping its token.
    secret       JWT_SECRET; without it a development default, with a warning in
                 the start-up log.

FastAPI dependencies: `get_current_user` (401 without a valid token) and
`require_role(role)` (403 for another role). Both resolve the Authenticator
through `get_authenticator`, which tests override.

Outside aftersales_service/ on purpose: that package reads no system clock and
writes no SQL (its boundary tests), while token expiry is wall-clock time and
the users table is this module's own.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from aftersales.demo import DEMO_PERSONAS
from aftersales_service.demo_store import DEMO_OPERATOR_REF
from aftersales_service.persistence import data_directory

logger = logging.getLogger(__name__)

JWT_SECRET_ENV = "JWT_SECRET"
USERS_DB_ENV = "AFTERSALES_USERS_DB"
USERS_DB_NAME = "users.sqlite3"
# Development only: anyone who reads this repository can mint tokens with it.
DEV_JWT_SECRET = "groundedagent-dev-only-jwt-secret-do-not-deploy"
JWT_ALGORITHM = "HS256"
TOKEN_TTL = timedelta(hours=2)
BCRYPT_ROUNDS = 12
# bcrypt only reads the first 72 bytes; bcrypt 5 refuses longer input.
BCRYPT_MAX_BYTES = 72

CUSTOMER = "customer"
OPERATOR = "operator"
ROLES = (CUSTOMER, OPERATOR)


@dataclass(frozen=True)
class SeedUser:
    username: str
    password: str
    role: str
    persona_id: str | None = None
    operator_ref: str | None = None


# The demo accounts. Passwords are documented in README (local demo only).
# Customers match the demo order data: demo-a = CUST-001, demo-b = CUST-002.
DEMO_USERS = (
    SeedUser("customer-a", "demo-a-2026", CUSTOMER, persona_id="demo-a"),
    SeedUser("customer-b", "demo-b-2026", CUSTOMER, persona_id="demo-b"),
    SeedUser("operator", "operator-2026", OPERATOR, operator_ref=DEMO_OPERATOR_REF),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('customer', 'operator')),
    -- the domain identity behind the login: a customer's persona, an operator's approver ref
    persona_id    TEXT    UNIQUE,
    operator_ref  TEXT    UNIQUE,
    CHECK ((role = 'customer') = (persona_id IS NOT NULL)),
    CHECK ((role = 'operator') = (operator_ref IS NOT NULL))
)
"""


@dataclass(frozen=True)
class AuthUser:
    """The authenticated caller, built from the users row a valid token names."""

    user_id: int
    username: str
    role: str
    persona_id: str | None
    operator_ref: str | None

    @property
    def display_name(self) -> str:
        if self.role == CUSTOMER:
            return DEMO_PERSONAS[self.persona_id].display_name
        return "售后操作员 " + self.operator_ref

    def view(self) -> dict[str, object]:
        # Display only: never the customer id.
        return {"username": self.username, "role": self.role, "display_name": self.display_name,
                "persona_id": self.persona_id}


class AuthError(Exception):
    code = "invalid_token"


class TokenExpired(AuthError):
    code = "token_expired"


class InvalidCredentials(AuthError):
    code = "invalid_credentials"


def _hash(password: str, rounds: int) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds)).decode("ascii")


class UserStore:
    """The users table. One short-lived connection per call."""

    def __init__(self, path: str | Path, *, seed: tuple[SeedUser, ...] = DEMO_USERS,
                 rounds: int = BCRYPT_ROUNDS) -> None:
        self.path = Path(path)
        self._rounds = rounds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(SCHEMA)
            for user in seed:
                if user.persona_id is not None and user.persona_id not in DEMO_PERSONAS:
                    raise ValueError("a seed customer must name a demo persona")
                exists = connection.execute("SELECT 1 FROM users WHERE username = ?",
                                            (user.username,)).fetchone()
                if exists is None:
                    connection.execute(
                        "INSERT INTO users (username, password_hash, role, persona_id, operator_ref)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (user.username, _hash(user.password, rounds), user.role,
                         user.persona_id, user.operator_ref))
        # Checked when the username is unknown, so both failures cost one bcrypt check.
        self._dummy_hash = _hash("not-a-password", rounds).encode("ascii")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _row(self, where: str, value: object) -> tuple | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT id, username, role, persona_id, operator_ref, password_hash FROM users WHERE "
                + where + " = ?", (value,)).fetchone()

    @staticmethod
    def _user(row: tuple) -> AuthUser:
        return AuthUser(user_id=row[0], username=row[1], role=row[2], persona_id=row[3],
                        operator_ref=row[4])

    def get(self, user_id: int) -> AuthUser | None:
        row = self._row("id", user_id)
        return None if row is None else self._user(row)

    def find(self, username: str) -> AuthUser | None:
        row = self._row("username", username)
        return None if row is None else self._user(row)

    def check_password(self, username: str, password: str) -> AuthUser | None:
        row = self._row("username", username)
        candidate = password.encode("utf-8")
        if len(candidate) > BCRYPT_MAX_BYTES:
            candidate = b""   # never a valid password; still costs one check
        stored = self._dummy_hash if row is None else row[5].encode("ascii")
        if not bcrypt.checkpw(candidate, stored) or row is None:
            return None
        return self._user(row)


class Authenticator:
    def __init__(self, users: UserStore, secret: str, *, ttl: timedelta = TOKEN_TTL,
                 now: Callable[[], datetime] | None = None) -> None:
        if not secret:
            raise ValueError("the JWT secret must not be empty")
        self.users = users
        self._secret = secret
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(timezone.utc))

    def issue(self, user: AuthUser) -> str:
        issued = self._now()
        payload = {"sub": str(user.user_id), "role": user.role,
                   "iat": issued, "exp": issued + self._ttl}
        return jwt.encode(payload, self._secret, algorithm=JWT_ALGORITHM)

    def login(self, username: str, password: str) -> dict[str, object]:
        user = self.users.check_password(username, password)
        if user is None:
            # One answer for an unknown user and a wrong password.
            raise InvalidCredentials("invalid username or password")
        return {"access_token": self.issue(user), "token_type": "bearer",
                "expires_in": int(self._ttl.total_seconds()), "user": user.view()}

    def verify(self, token: str) -> AuthUser:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[JWT_ALGORITHM],
                                 options={"require": ["sub", "role", "iat", "exp"]})
        except jwt.ExpiredSignatureError:
            raise TokenExpired("the token has expired") from None
        except jwt.InvalidTokenError:
            raise AuthError("invalid token") from None
        subject = payload["sub"]
        user = self.users.get(int(subject)) if isinstance(subject, str) and subject.isdigit() else None
        # The account must still exist with the role the token was issued for.
        if user is None or payload["role"] != user.role:
            raise AuthError("the token names no current user")
        return user


def jwt_secret() -> str:
    secret = os.environ.get(JWT_SECRET_ENV, "")
    if secret:
        return secret
    logger.warning("%s is not set: using the built-in development secret. Anyone with this "
                   "repository can forge tokens; set %s before any non-local deployment.",
                   JWT_SECRET_ENV, JWT_SECRET_ENV)
    return DEV_JWT_SECRET


def users_db_path() -> Path:
    configured = os.environ.get(USERS_DB_ENV)
    return Path(configured).resolve() if configured else data_directory() / USERS_DB_NAME


_default: Authenticator | None = None
_default_lock = threading.Lock()


def default_authenticator() -> Authenticator:
    """The process's authenticator, built (users seeded, secret read) on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Authenticator(UserStore(users_db_path()), jwt_secret())
        return _default


# -- FastAPI dependencies ---------------------------------------------------


def get_authenticator() -> Authenticator:
    """Overridden in tests (app.dependency_overrides)."""
    return default_authenticator()


def _unauthorized(code: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": code},
                         headers={"WWW-Authenticate": "Bearer"})


def get_current_user(request: Request,
                     authenticator: Authenticator = Depends(get_authenticator)) -> AuthUser:
    """The caller named by `Authorization: Bearer <token>`; 401 otherwise.

    The header only: the after-sales frontend calls every endpoint with fetch
    (there is no EventSource / SSE endpoint under /api/aftersales), so no token
    is accepted from a query string, where it would end up in access logs.
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("not_authenticated")
    try:
        return authenticator.verify(token.strip())
    except AuthError as error:
        raise _unauthorized(error.code) from None


def require_role(role: str) -> Callable[..., AuthUser]:
    """A dependency: the current user, who must have `role` (403 otherwise)."""
    if role not in ROLES:
        raise ValueError("unknown role")

    def dependency(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role != role:
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        return user

    return dependency


# -- /auth --------------------------------------------------------------------


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


def create_auth_router(*, on_startup: list[Callable[[], object]] | None = None) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"], on_startup=on_startup)

    @router.post("/login")
    def login(body: LoginBody, authenticator: Authenticator = Depends(get_authenticator)) -> dict:
        try:
            return authenticator.login(body.username, body.password)
        except InvalidCredentials as error:
            raise _unauthorized(error.code) from None

    @router.get("/me")
    def me(user: AuthUser = Depends(get_current_user)) -> dict:
        return user.view()

    return router


# api.py's router: the start-up hook seeds the users and logs the secret warning.
router = create_auth_router(on_startup=[default_authenticator])
