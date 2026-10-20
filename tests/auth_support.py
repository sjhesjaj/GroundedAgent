"""Token fixtures for the /api/aftersales/* tests.

Real JWTs from the real Authenticator over a temporary users table (bcrypt
cost 4, test secret), minted directly: no login request, so the M2 golden
recording (tests/test_aftersales_golden.py) sees exactly the HTTP traffic it
always did. Authentication itself is never relaxed - a request without one of
these tokens gets 401, as in production (tests/test_aftersales_auth.py).

`TokenFixture.client(app)` is a TestClient that acts as the legitimate caller
of each route unless the call passes its own Authorization header:

    /api/aftersales/operator/*, POST /demo/reset   the operator
    /api/aftersales/sessions/{id}...                that session's customer
                                                    (learned from its 201; else customer-a)
    POST /api/aftersales/sessions                   the customer of the body's persona_id
                                                    (else customer-a)
    anything else                                   customer-a
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import DEMO_USERS, OPERATOR, Authenticator, SeedUser, UserStore, get_authenticator

TEST_JWT_SECRET = "tests-only-jwt-secret-0123456789abcdef"
_SESSION_PATH = re.compile(r"^/api/aftersales/sessions/([^/?]+)")
_CUSTOMER_OF = {user.persona_id: user.username for user in DEMO_USERS if user.persona_id}
OPERATOR_USERNAME = next(user.username for user in DEMO_USERS if user.role == OPERATOR)
DEFAULT_CUSTOMER = _CUSTOMER_OF["demo-a"]


class TokenFixture:
    def __init__(self, directory: Path, seed: tuple[SeedUser, ...] = DEMO_USERS) -> None:
        self.users = UserStore(Path(directory) / "users.sqlite3", seed=seed, rounds=4)
        self.authenticator = Authenticator(self.users, TEST_JWT_SECRET)
        self.owners: dict[str, str] = {}   # session_id -> persona_id

    @classmethod
    def for_test(cls, test: unittest.TestCase, seed: tuple[SeedUser, ...] = DEMO_USERS) -> "TokenFixture":
        directory = Path(tempfile.mkdtemp(prefix="aftersales-auth-test-"))
        test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return cls(directory, seed)

    def token(self, username: str) -> str:
        return self.authenticator.issue(self.users.find(username))

    def headers(self, username: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token(username)}

    def customer(self, persona_id: str = "demo-a") -> dict[str, str]:
        return self.headers(_CUSTOMER_OF[persona_id])

    def operator(self) -> dict[str, str]:
        return self.headers(OPERATOR_USERNAME)

    def install(self, app: FastAPI) -> FastAPI:
        app.dependency_overrides[get_authenticator] = lambda: self.authenticator
        return app

    def client(self, app: FastAPI) -> "ActingClient":
        return ActingClient(self.install(app), self)

    # -- the acting client's choice -------------------------------------------

    def caller_for(self, method: str, path: str, body: object) -> str:
        if path.startswith("/api/aftersales/operator/") or path == "/api/aftersales/demo/reset":
            return OPERATOR_USERNAME
        match = _SESSION_PATH.match(path)
        if match:
            return _CUSTOMER_OF.get(self.owners.get(match.group(1)), DEFAULT_CUSTOMER)
        if path == "/api/aftersales/sessions" and isinstance(body, dict):
            return _CUSTOMER_OF.get(body.get("persona_id"), DEFAULT_CUSTOMER)
        return DEFAULT_CUSTOMER

    def learn(self, method: str, path: str, response) -> None:
        if method.upper() == "POST" and path == "/api/aftersales/sessions" and response.status_code == 201:
            payload = response.json()
            self.owners[payload["session_id"]] = payload["persona"]["persona_id"]


class ActingClient(TestClient):
    def __init__(self, app: FastAPI, tokens: TokenFixture) -> None:
        super().__init__(app)
        self.tokens = tokens

    def request(self, method, url, *args, **kwargs):
        path = str(url).removeprefix(str(self.base_url))
        headers = dict(kwargs.get("headers") or {})
        if not any(name.lower() == "authorization" for name in headers):
            headers.update(self.tokens.headers(self.tokens.caller_for(method, path, kwargs.get("json"))))
        kwargs["headers"] = headers
        response = super().request(method, url, *args, **kwargs)
        self.tokens.learn(method, path, response)
        return response
