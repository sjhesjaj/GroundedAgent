"""JWT login and authorization on /auth/* and /api/aftersales/* (auth.py, routes.py).

Offline: a temporary users table (bcrypt cost 4) and a test secret. Requests
here carry explicit Authorization headers (or none); the scripted flows reuse
the M0 ProductTestCase, whose client acts as each route's legitimate caller.
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import jwt
from fastapi.testclient import TestClient

import auth
from auth import DEMO_USERS, OPERATOR, Authenticator, SeedUser
from tests.auth_support import TEST_JWT_SECRET, TokenFixture
from tests.test_aftersales_service import ProductTestCase, call, decision

SESSION = "0" * 32
PENDING = "PA-0123456789ABCDEF"
PASSWORDS = {user.username: user.password for user in DEMO_USERS}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


class AuthTestCase(ProductTestCase):
    """The M0 product app plus /auth, and a raw client that sends only what a test gives it."""

    def setUp(self) -> None:
        super().setUp()
        app = self.client.app
        app.include_router(auth.create_auth_router())
        self.raw = TestClient(app)

    def login(self, username: str, password: str | None = None):
        return self.raw.post("/auth/login", json={
            "username": username, "password": PASSWORDS[username] if password is None else password})

    def as_(self, username: str) -> dict[str, str]:
        return self.tokens.headers(username)


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


class LoginTests(AuthTestCase):
    def test_every_demo_account_logs_in_and_gets_a_two_hour_hs256_token(self):
        for user in DEMO_USERS:
            with self.subTest(user=user.username):
                response = self.login(user.username)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual((body["token_type"], body["expires_in"]), ("bearer", 7200))
                self.assertEqual(body["user"]["role"], user.role)
                self.assertEqual(body["user"]["persona_id"], user.persona_id)
                self.assertEqual(jwt.get_unverified_header(body["access_token"])["alg"], "HS256")
                claims = jwt.decode(body["access_token"], TEST_JWT_SECRET, algorithms=["HS256"])
                self.assertEqual(set(claims), {"sub", "role", "iat", "exp"})
                self.assertEqual(claims["sub"], str(self.tokens.users.find(user.username).user_id))
                self.assertEqual(claims["role"], user.role)
                self.assertEqual(claims["exp"] - claims["iat"], 7200)
                me = self.raw.get("/auth/me", headers=bearer(body["access_token"]))
                self.assertEqual(me.json(), body["user"])
                self.assertNotIn("CUST-", response.text)

    def test_a_wrong_password_and_an_unknown_user_get_the_same_401(self):
        answers = []
        for username, password in (("customer-a", "wrong"), ("customer-a", PASSWORDS["customer-b"]),
                                   ("nobody", "demo-a-2026"), ("customer-a", "x" * 100)):
            with self.subTest(username=username, password=password):
                response = self.raw.post("/auth/login", json={"username": username, "password": password})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                answers.append(response.json())
        self.assertEqual(answers, [{"detail": {"code": "invalid_credentials"}}] * 4)

    def test_the_login_body_is_closed(self):
        for body in ({"username": "customer-a"}, {"username": "customer-a", "password": "demo-a-2026",
                                                  "role": "operator"}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.raw.post("/auth/login", json=body).status_code, 422)

    def test_passwords_are_stored_as_bcrypt_hashes(self):
        rows = self.tokens.users._connect().execute(
            "SELECT username, password_hash, role, persona_id, operator_ref FROM users ORDER BY id").fetchall()
        self.assertEqual([(row[0], row[2], row[3], row[4]) for row in rows], [
            ("customer-a", "customer", "demo-a", None), ("customer-b", "customer", "demo-b", None),
            ("operator", "operator", None, "op-demo-1")])
        for username, password_hash, *_ in rows:
            self.assertRegex(password_hash, r"^\$2[aby]\$\d\d\$")
            self.assertNotIn(PASSWORDS[username], password_hash)


# --------------------------------------------------------------------------
# 401: no token, a bad token
# --------------------------------------------------------------------------


class AuthenticationTests(AuthTestCase):
    def protected_calls(self):
        """Every operation of the app except POST /auth/login, with valid-looking inputs."""
        bodies = {"/api/aftersales/sessions/{session_id}/messages": {"text": "你好"},
                  "/api/aftersales/operator/sessions/{session_id}/decision":
                      {"pending_action_id": PENDING, "decision": "APPROVE"}}
        calls = []
        for path, operations in self.raw.app.openapi()["paths"].items():
            for method in operations:
                if (method, path) == ("post", "/auth/login"):
                    continue
                calls.append((method.upper(), path.replace("{session_id}", SESSION), bodies.get(path)))
        return calls

    def test_every_route_but_login_needs_a_token(self):
        calls = self.protected_calls()
        self.assertEqual(len(calls), 9)   # 8 under /api/aftersales + GET /auth/me
        for method, path, body in calls:
            with self.subTest(method=method, path=path):
                response = self.raw.request(method, path, json=body)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.json(), {"detail": {"code": "not_authenticated"}})
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.service.operator_sessions(), [])   # nothing was created

    def test_bad_tokens_are_401(self):
        good = self.tokens.token("customer-a")
        claims = jwt.decode(good, TEST_JWT_SECRET, algorithms=["HS256"])
        expired = Authenticator(self.tokens.users, TEST_JWT_SECRET,
                                now=lambda: datetime.now(timezone.utc) - timedelta(hours=3))
        cases = {
            "not a jwt": ("Bearer not-a-token", "invalid_token"),
            "another secret": ("Bearer " + jwt.encode(claims, "another-secret-0123456789abcdef0123",
                                                      algorithm="HS256"), "invalid_token"),
            "alg none": ("Bearer " + jwt.encode(claims, None, algorithm="none"), "invalid_token"),
            "role raised": ("Bearer " + jwt.encode({**claims, "role": "operator"}, TEST_JWT_SECRET,
                                                   algorithm="HS256"), "invalid_token"),
            "no such user": ("Bearer " + jwt.encode({**claims, "sub": "999"}, TEST_JWT_SECRET,
                                                    algorithm="HS256"), "invalid_token"),
            "no expiry": ("Bearer " + jwt.encode({k: v for k, v in claims.items() if k != "exp"},
                                                 TEST_JWT_SECRET, algorithm="HS256"), "invalid_token"),
            "expired": ("Bearer " + expired.issue(self.tokens.users.find("customer-a")), "token_expired"),
            "basic scheme": ("Basic " + good, "not_authenticated"),
            "empty bearer": ("Bearer ", "not_authenticated"),
        }
        for name, (header, code) in cases.items():
            with self.subTest(case=name):
                response = self.raw.get("/api/aftersales/demo", headers={"Authorization": header})
                self.assertEqual((response.status_code, response.json()), (401, {"detail": {"code": code}}))
        self.assertEqual(self.raw.get("/api/aftersales/demo", headers=bearer(good)).status_code, 200)

    def test_no_token_is_read_from_the_query_string(self):
        token = self.tokens.token("customer-a")
        for name in ("token", "access_token"):
            with self.subTest(name=name):
                response = self.raw.get("/api/aftersales/demo?" + name + "=" + token)
                self.assertEqual(response.status_code, 401)


# --------------------------------------------------------------------------
# 403: the wrong role
# --------------------------------------------------------------------------


class RoleTests(AuthTestCase):
    def test_a_customer_cannot_call_operator_endpoints(self):
        session_id, pending_id = self.waiting()
        customer = self.as_("customer-a")   # the session's own customer
        for method, path, body in (
                ("POST", "/api/aftersales/operator/sessions/" + session_id + "/decision",
                 {"pending_action_id": pending_id, "decision": "APPROVE"}),
                ("GET", "/api/aftersales/operator/sessions", None),
                ("GET", "/api/aftersales/operator/sessions/" + session_id, None),
                ("POST", "/api/aftersales/demo/reset", None)):
            with self.subTest(method=method, path=path):
                response = self.raw.request(method, path, json=body, headers=customer)
                self.assertEqual((response.status_code, response.json()), (403, {"detail": {"code": "forbidden"}}))
        self.assertEqual(self.pending(pending_id)["status"], "PENDING_APPROVAL")
        self.assertEqual(self.count("action_receipts"), 0)
        self.assertEqual(self.view(session_id)["status"], "WAITING_APPROVAL")   # not reset

    def test_an_operator_cannot_call_customer_endpoints(self):
        session_id = self.session()
        operator = self.as_("operator")
        for method, path, body in (("POST", "/api/aftersales/sessions", None),
                                   ("GET", "/api/aftersales/sessions/" + session_id, None),
                                   ("POST", "/api/aftersales/sessions/" + session_id + "/messages",
                                    {"text": "我是顾客"})):
            with self.subTest(method=method, path=path):
                response = self.raw.request(method, path, json=body, headers=operator)
                self.assertEqual((response.status_code, response.json()), (403, {"detail": {"code": "forbidden"}}))
        self.assertEqual(self.view(session_id)["messages"], [])

    def test_the_operator_lists_and_reads_every_session_and_decides_as_its_own_ref(self):
        session_a, pending_id = self.waiting("demo-a")
        session_b = self.session("demo-b")
        operator = self.as_("operator")
        listed = self.raw.get("/api/aftersales/operator/sessions", headers=operator).json()["items"]
        self.assertEqual({item["session_id"]: (item["persona"]["persona_id"], item["status"],
                                               item["waiting_approval"]) for item in listed},
                         {session_a: ("demo-a", "WAITING_APPROVAL", [pending_id]),
                          session_b: ("demo-b", "OPEN", [])})
        viewed = self.raw.get("/api/aftersales/operator/sessions/" + session_a, headers=operator).json()
        self.assertEqual(viewed["pending_actions"][0]["pending_action_id"], pending_id)
        decided = self.raw.post("/api/aftersales/operator/sessions/" + session_a + "/decision",
                                json={"pending_action_id": pending_id, "decision": "APPROVE"},
                                headers=operator).json()
        self.assertEqual(decided["operator_decision"]["approver_ref"], "op-demo-1")
        self.assertEqual(self.pending(pending_id)["approver_ref"], "op-demo-1")
        self.assertNotIn("CUST-", str(listed) + str(viewed))


class ApproverFromTokenTests(AuthTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A second operator login whose ref is not in the trusted operator registry.
        seed = DEMO_USERS + (SeedUser("operator-2", "operator-2-2026", OPERATOR, operator_ref="op-other"),)
        self.tokens = self.client.tokens = TokenFixture.for_test(self, seed)
        self.tokens.install(self.client.app)

    def test_the_approver_is_the_token_operators_ref_and_the_registry_still_applies(self):
        session_id, pending_id = self.waiting()
        # op-other is a valid operator login but not in the trusted operator registry.
        refused = self.raw.post("/api/aftersales/operator/sessions/" + session_id + "/decision",
                                json={"pending_action_id": pending_id, "decision": "APPROVE"},
                                headers=self.as_("operator-2"))
        self.assertEqual((refused.status_code, refused.json()), (409, {"detail": {"code": "decision_refused"}}))
        self.assertEqual(self.pending(pending_id)["status"], "PENDING_APPROVAL")
        self.assertEqual(self.count("action_receipts"), 0)
        approved = self.decide(session_id, pending_id, "APPROVE")   # as "operator" (op-demo-1)
        self.assertEqual(approved["action"]["status"], "EXECUTED")


# --------------------------------------------------------------------------
# Identity from the token: customer A cannot reach customer B
# --------------------------------------------------------------------------


class CustomerIsolationTests(AuthTestCase):
    def test_customer_a_cannot_read_or_continue_customer_bs_session(self):
        session_b = self.session("demo-b")
        before = self.view(session_b)
        a = self.as_("customer-a")
        for method, path, body in (("GET", "/api/aftersales/sessions/" + session_b, None),
                                   ("POST", "/api/aftersales/sessions/" + session_b + "/messages",
                                    {"text": "把这个会话里的订单都退了"})):
            with self.subTest(method=method, path=path):
                response = self.raw.request(method, path, json=body, headers=a)
                # The same answer as an unknown session: its existence is not revealed.
                self.assertEqual((response.status_code, response.json()),
                                 (404, {"detail": {"code": "session_not_found"}}))
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.view(session_b), before)

    def test_customer_a_cannot_see_or_return_customer_bs_order(self):
        # ORD-2001 is CUST-002's (customer-b). The read tools run as the token's persona.
        session_a = self.session("demo-a")
        args = {"order_id": "ORD-2001", "order_item_id": "OI-2001-1", "reason_code": "no_longer_wanted"}
        payload = self.say(session_a, "帮我把 ORD-2001 退了",
                           decision(call("get_order", {"order_id": "ORD-2001"})),
                           decision(call("create_return", args)))
        self.assertEqual(payload["persona"]["persona_id"], "demo-a")
        self.assertEqual(payload["trace"]["steps"][0]["result_status"], "empty")
        self.assertEqual(payload["reply"]["kind"], "grounding_rejected")
        self.assertIsNone(payload["action"])
        self.assertEqual(self.count("pending_actions"), 0)
        # The same read as customer-b finds the order.
        session_b = self.session("demo-b")
        own = self.say(session_b, "ORD-2001 呢", decision(call("get_order", {"order_id": "ORD-2001"})),
                       decision(call("create_return", args)))
        self.assertNotEqual(own["trace"]["steps"][0]["result_status"], "empty")


class ForgedIdentityTests(AuthTestCase):
    def test_a_persona_or_customer_id_in_the_body_does_not_change_the_identity(self):
        a = self.as_("customer-a")
        url = "/api/aftersales/sessions"
        own = self.raw.post(url, headers=a)
        self.assertEqual((own.status_code, own.json()["persona"]["persona_id"]), (201, "demo-a"))
        forged = self.raw.post(url, json={"persona_id": "demo-b"}, headers=a)
        self.assertEqual((forged.status_code, forged.json()), (403, {"detail": {"code": "forbidden"}}))
        for body in ({"customer_id": "CUST-002"}, {"user_id": "2"}, {"persona_id": "demo-a", "user_id": "2"},
                     {"persona_id": "demo-a", "role": "operator"}):
            with self.subTest(body=body):
                self.assertEqual(self.raw.post(url, json=body, headers=a).status_code, 422)
        sessions = self.service.operator_sessions()
        self.assertEqual([item["persona"]["persona_id"] for item in sessions], ["demo-a"])

    def test_identity_fields_in_message_and_decision_bodies_are_refused(self):
        session_id, pending_id = self.waiting()
        message = self.raw.post("/api/aftersales/sessions/" + session_id + "/messages",
                                json={"text": "你好", "user_id": "2", "customer_id": "CUST-002"},
                                headers=self.as_("customer-a"))
        self.assertEqual(message.status_code, 422)
        decision_ = self.raw.post("/api/aftersales/operator/sessions/" + session_id + "/decision",
                                  json={"pending_action_id": pending_id, "decision": "APPROVE",
                                        "approver_ref": "op-other"}, headers=self.as_("operator"))
        self.assertEqual(decision_.status_code, 422)
        self.assertEqual(self.pending(pending_id)["status"], "PENDING_APPROVAL")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigurationTests(unittest.TestCase):
    def test_without_jwt_secret_the_dev_default_is_used_with_a_warning(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(auth.JWT_SECRET_ENV, None)
            with self.assertLogs("auth", level="WARNING") as logs:
                self.assertEqual(auth.jwt_secret(), auth.DEV_JWT_SECRET)
        self.assertIn("JWT_SECRET is not set", logs.output[0])

    def test_jwt_secret_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {auth.JWT_SECRET_ENV: "from-env-0123456789abcdef0123456789"}):
            with self.assertNoLogs("auth", level="WARNING"):
                self.assertEqual(auth.jwt_secret(), "from-env-0123456789abcdef0123456789")

    def test_the_users_database_path(self):
        with mock.patch.dict(os.environ, {auth.USERS_DB_ENV: "somewhere/users.db"}):
            self.assertEqual(auth.users_db_path(), Path("somewhere/users.db").resolve())
        with mock.patch.dict(os.environ, {"AFTERSALES_DATA_DIR": "demo-data"}):
            os.environ.pop(auth.USERS_DB_ENV, None)
            self.assertEqual(auth.users_db_path(), Path("demo-data").resolve() / "users.sqlite3")

    def test_the_api_mounts_login_and_the_new_operator_routes(self):
        import api

        paths = set(api.app.openapi()["paths"])
        for path in ("/auth/login", "/auth/me", "/api/aftersales/operator/sessions",
                     "/api/aftersales/operator/sessions/{session_id}"):
            self.assertIn(path, paths)


# --------------------------------------------------------------------------
# The real application (api.app, default configuration): every route
# --------------------------------------------------------------------------

# Every route of the default application that answers without a token, and why.
# (The API process serves no static frontend: Vite serves it in development.)
PUBLIC_ROUTES = {
    ("POST", "/auth/login"): "the login itself: how a token is obtained",
    ("GET", "/api/health"): "process health check: read-only, reports Ollama reachability and the "
                            "V1 index size, no customer or business data",
    ("GET", "/openapi.json"): "FastAPI's schema: route shapes only, no data",
    ("GET", "/docs"): "Swagger UI page over /openapi.json",
    ("GET", "/docs/oauth2-redirect"): "Swagger UI's static OAuth2 helper page",
    ("GET", "/redoc"): "ReDoc page over /openapi.json",
}


def flattened_routes(routes):
    """Every route, included routers expanded (FastAPI 0.14x keeps them as _IncludedRouter)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from flattened_routes(inner.routes)
        else:
            yield route


def route_table(app) -> set[tuple[str, str]]:
    table = set()
    for route in flattened_routes(app.routes):
        path, methods = getattr(route, "path", None), getattr(route, "methods", None)
        if path is None or methods is None:
            raise AssertionError("a route the walk cannot read: " + type(route).__name__)
        table.update((method, path) for method in methods - {"HEAD"})
    return table


def openapi_operations(app) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
            for method in operations}


class FullApplicationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        import api

        self.api = api
        self.assertFalse(api.v1_routes_enabled(),
                         "run the tests with ENABLE_V1_ROUTES unset: they check the default application")
        tokens = TokenFixture.for_test(self)
        api.app.dependency_overrides[auth.get_authenticator] = lambda: tokens.authenticator
        self.addCleanup(api.app.dependency_overrides.pop, auth.get_authenticator, None)
        self.client = TestClient(api.app)   # no start-up: nothing opens the demo data directory

    def test_every_route_but_the_whitelist_answers_401_without_a_token(self):
        table = route_table(self.api.app)
        # The walk sees everything OpenAPI lists, plus routes kept out of the schema.
        self.assertLessEqual(openapi_operations(self.api.app), table)
        self.assertLessEqual(set(PUBLIC_ROUTES), table, "a stale whitelist entry")
        protected = sorted(table - set(PUBLIC_ROUTES))
        self.assertIn(("POST", "/api/aftersales/operator/sessions/{session_id}/decision"), protected)
        self.assertIn(("POST", "/api/aftersales/demo/reset"), protected)
        for method, path in protected:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, re.sub(r"\{[^}]+\}", SESSION, path))
                self.assertEqual((response.status_code, response.json()),
                                 (401, {"detail": {"code": "not_authenticated"}}))

    def test_v1_routes_do_not_exist_by_default(self):
        v1 = route_table(self.api.v1_router)
        self.assertIn(("POST", "/api/knowledge/upload"), v1)
        self.assertIn(("DELETE", "/api/knowledge"), v1)
        self.assertFalse(v1 & route_table(self.api.app))
        self.assertFalse({path for _, path in v1} & set(self.api.app.openapi()["paths"]))
        for method, path in sorted(v1):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, re.sub(r"\{[^}]+\}", "x", path))
                self.assertEqual(response.status_code, 404)
        upload = self.client.post("/api/knowledge/upload",
                                  files={"files": ("rules.md", b"# rules", "text/markdown")})
        self.assertEqual(upload.status_code, 404)
        self.assertEqual(self.client.delete("/api/knowledge").status_code, 404)

    def test_enable_v1_routes_mounts_them_and_only_then(self):
        v1_paths = {path for _, path in route_table(self.api.v1_router)}
        for value, mounted in (("", False), ("0", False), ("false", False), ("1", True),
                               ("true", True), ("ON", True)):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {self.api.V1_ROUTES_ENV: value}):
                    paths = set(self.api.create_app().openapi()["paths"])
                self.assertEqual(v1_paths <= paths, mounted)
                self.assertFalse(not mounted and v1_paths & paths)
        self.assertLessEqual(v1_paths, set(self.api.create_app(v1_routes=True).openapi()["paths"]))


if __name__ == "__main__":
    unittest.main()
