"""GroundedAgent V2 M2 phase 3: persistence, the commit protocol and in-process recovery.

Offline, with the scripted model of tests.test_aftersales_service, each test in
its own temporary data directory. A "restart" closes the service and opens a
new one on the same directory: nothing survives but the files. Real process
crashes (os._exit at each commit point) are phase 4.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aftersales.action_gateway import ActionGateway
from aftersales.action_outcome import ActionOutcomeRenderer
from aftersales.approval import ApprovalDecision
from aftersales_service import action_grounding as grounding
from aftersales_service import persistence
from aftersales_service.conversation import Conversation
from aftersales_service.demo_store import DEMO_OPERATOR_REF, seed_demo_database
from aftersales_service.persistence import (
    PersistenceError,
    SessionFiles,
    atomic_write_json,
    data_directory,
)
from aftersales_service.routes import create_router
from aftersales_service.service import AftersalesService
from tests import test_aftersales_golden, test_aftersales_graph
from tests.test_aftersales_service import (
    REPO_ROOT,
    RETURN_ARGS,
    ProductTestCase,
    call,
    decision,
    runtime_context,
    temporary_data_dir,
)

CLARIFY = decision(call("ask_user", {"slots": ["order_id"]}))
REFUSE = decision(call("finish", {"disposition": "refuse"}))
GET_ORDER = decision(call("get_order", {"order_id": "ORD-1001"}))
CREATE_RETURN = decision(call("create_return", RETURN_ARGS))
RETURN_TEXT = "ORD-1001 里那件内衣我不想要了，帮我退货"


def user_messages(request: dict) -> list[str]:
    return [message["content"] for message in request["messages"] if message["role"] == "user"]


class PersistentTestCase(ProductTestCase):
    """ProductTestCase with restarts, session files and the white-box conversation."""

    def setUp(self) -> None:
        super().setUp()
        self.app = self.client.app

    def restart(self) -> None:
        """A new process on the same data directory: nothing survives but the files."""
        self.service.close()
        self.service = AftersalesService(lambda: self.provider, data_dir=self.data_dir)
        self.addCleanup(self.service.close)
        self.app = FastAPI()
        self.app.include_router(create_router(self.service))
        self.client = TestClient(self.app)

    def generation(self) -> str:
        return json.loads((self.data_dir / "generation.json").read_text(encoding="utf-8"))["generation"]

    def session_path(self, session_id: str) -> Path:
        return self.data_dir / ("gen-" + self.generation()) / "sessions" / (session_id + ".json")

    def session_file(self, session_id: str) -> dict:
        return json.loads(self.session_path(session_id).read_text(encoding="utf-8"))

    def conversation(self, session_id: str) -> Conversation:
        return self.service._sessions[session_id]

    def approval(self, pending_action_id: str) -> ApprovalDecision:
        return ApprovalDecision(pending_action_id=pending_action_id, decision="APPROVE",
                                approver_ref=DEMO_OPERATOR_REF,
                                decided_at=self.service.store.clock.now().isoformat())

    def crash_after_start_action(self, session_id: str) -> None:
        """start_action committed, then the turn failed before the head was written."""
        with mock.patch.object(ActionOutcomeRenderer, "render", side_effect=RuntimeError("render")):
            with self.assertRaises(RuntimeError):
                self.request_return(session_id)
        self.assertIsNotNone(self.session_file(session_id)["inflight"])
        self.assertEqual(self.count("pending_actions"), 1)

    def kinds(self, view: dict) -> list[tuple]:
        return [(entry["role"], entry.get("kind")) for entry in view["messages"]]


# --------------------------------------------------------------------------
# Files: the data directory, generations and session files
# --------------------------------------------------------------------------


class SessionFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = temporary_data_dir(self)
        self.files = SessionFiles(self.directory, "a" * 32)

    def write(self, **fields) -> None:
        values = {"persona_id": "demo-a", "head": "head-1", "inflight": None, **fields}
        self.files.write("b" * 32, **values)

    def test_a_session_file_holds_head_and_marker_together(self):
        marker = {"checkpoint_id": "g", "idempotency_key": "k", "action_name": "create_return",
                  "args_sha256": "d"}
        self.write(inflight=marker)
        self.assertEqual(self.files.read("b" * 32),
                         {"schema": 1, "persona_id": "demo-a", "generation": "a" * 32,
                          "head": "head-1", "inflight": marker})
        self.assertEqual(self.files.with_inflight(), ["b" * 32])
        self.write(head="head-2")
        self.assertEqual((self.files.read("b" * 32)["head"], self.files.with_inflight()),
                         ("head-2", []))
        self.assertEqual([path.name for path in self.directory.iterdir()], ["b" * 32 + ".json"])

    def test_invalid_session_files_are_refused(self):
        path = self.directory / ("b" * 32 + ".json")
        for content in ("not json", "[]", json.dumps({"schema": 2}),
                        json.dumps({"schema": 1, "persona_id": "demo-a", "generation": "a" * 32,
                                    "head": "", "inflight": None}),
                        json.dumps({"schema": 1, "persona_id": "demo-a", "generation": "a" * 32,
                                    "head": "h", "inflight": {"checkpoint_id": "g"}})):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(PersistenceError):
                    self.files.read("b" * 32)
        with self.assertRaises(PersistenceError):
            self.write(inflight={"checkpoint_id": "g"})
        with self.assertRaises(PersistenceError):
            self.files.read("../../etc")

    def test_a_session_file_of_another_generation_is_not_a_session(self):
        SessionFiles(self.directory, "c" * 32).write("b" * 32, persona_id="demo-a", head="h",
                                                     inflight=None)
        self.assertIsNone(self.files.read("b" * 32))
        with self.assertRaises(PersistenceError):
            self.files._validated({"schema": 1, "persona_id": "demo-a", "generation": "c" * 32,
                                   "head": "h", "inflight": None})

    def test_a_replace_blocked_by_another_process_is_retried_briefly(self):
        path = self.directory / "x.json"
        atomic_write_json(path, {"value": 1})
        real_replace, failures, delays = os.replace, [2], []

        def blocked_twice(source, target):
            if failures[0]:
                failures[0] -= 1
                raise PermissionError("held by another process")
            return real_replace(source, target)

        with mock.patch.object(persistence.os, "replace", side_effect=blocked_twice), \
                mock.patch.object(persistence.time, "sleep", side_effect=delays.append):
            atomic_write_json(path, {"value": 2})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 2})
        self.assertEqual(delays, [0.02, 0.04])

    def test_a_replace_that_stays_blocked_fails_and_keeps_the_old_document(self):
        path = self.directory / "x.json"
        atomic_write_json(path, {"value": 1})
        delays = []
        with mock.patch.object(persistence.os, "replace", side_effect=PermissionError("held")), \
                mock.patch.object(persistence.time, "sleep", side_effect=delays.append):
            with self.assertRaises(PermissionError):
                atomic_write_json(path, {"value": 2})
        self.assertEqual(len(delays), persistence.REPLACE_ATTEMPTS - 1)
        self.assertLessEqual(sum(delays), 1.0)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 1})
        self.assertEqual([item.name for item in self.directory.iterdir()], ["x.json"])


class DataDirectoryTests(unittest.TestCase):
    def test_the_data_directory_comes_from_the_argument_the_environment_or_the_default(self):
        with mock.patch.dict(os.environ, {"AFTERSALES_DATA_DIR": "configured-dir"}):
            self.assertEqual(data_directory(), Path("configured-dir").resolve())
            self.assertEqual(data_directory("explicit"), Path("explicit").resolve())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(data_directory(), Path(".aftersales-demo").resolve())
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".aftersales-demo/", ignored)

    def test_a_service_touches_no_file_before_it_is_used(self):
        root = temporary_data_dir(self) / "data"
        service = AftersalesService(lambda: None, data_dir=root)
        self.addCleanup(service.close)
        self.assertFalse(root.exists())
        service.start()
        generation = json.loads((root / "generation.json").read_text(encoding="utf-8"))["generation"]
        names = {path.name for path in (root / ("gen-" + generation)).iterdir()}
        # checkpoints.db runs in WAL mode: its -wal / -shm files exist while it is open.
        self.assertEqual(names - {"checkpoints.db-wal", "checkpoints.db-shm"},
                         {"aftersales-demo.db", "checkpoints.db", "sessions"})

    def test_a_generation_is_seeded_once_and_a_never_activated_one_is_removed(self):
        root = temporary_data_dir(self)
        service = AftersalesService(lambda: None, data_dir=root)
        service.start()
        database = service.store.db_path
        service.close()
        stray = root / ("gen-" + "f" * 32)
        stray.mkdir()
        with mock.patch("aftersales_service.persistence.seed_demo_database") as seeded:
            reopened = AftersalesService(lambda: None, data_dir=root)
            self.addCleanup(reopened.close)
            reopened.start()
            self.assertEqual(seeded.call_count, 0)
        self.assertEqual(reopened.store.db_path, database)
        self.assertFalse(stray.exists())
        with self.assertRaises(FileExistsError):
            seed_demo_database(database)


# --------------------------------------------------------------------------
# Restart: a conversation survives (acceptance 1, 2)
# --------------------------------------------------------------------------


class RestartTests(PersistentTestCase):
    def test_a_clarification_survives_a_restart_with_its_step_budget(self):
        session_id = self.session()
        asked = self.say(session_id, "我要退货", CLARIFY)
        self.assertEqual(asked["status"], "NEEDS_CLARIFICATION")
        self.restart()
        self.assertEqual(self.view(session_id)["status"], "NEEDS_CLARIFICATION")
        start = len(self.provider.requests)
        answered = self.say(session_id, "ORD-1001，里面那件内衣，不想要了", GET_ORDER, CREATE_RETURN)
        self.assertEqual(answered["status"], "WAITING_APPROVAL")
        # The same run continued: step 2 and 3 of the same six-step budget.
        self.assertEqual(self.decisions(start), [(2, 5), (3, 4)])
        self.assertEqual(user_messages(self.provider.requests[start]),
                         ["我要退货", "ORD-1001，里面那件内衣，不想要了"])

    def test_a_pending_approval_survives_a_restart_with_its_grounding_binding(self):
        with mock.patch("aftersales_service.conversation.ground_action",
                        wraps=grounding.ground_action) as grounded:
            session_id = self.session()
            payload = self.request_return(session_id)
            pending_id = payload["pending_action_id"]
            binding = next(step["grounding"] for step in payload["trace"]["steps"]
                           if step["kind"] == "action_proposed")
            self.restart()
            self.assertEqual(self.view(session_id)["status"], "WAITING_APPROVAL")
            submission = self.conversation(session_id)._submissions.for_pending(pending_id)
            self.assertEqual({"basis": "observed", **submission.binding.to_dict()}, binding)
            approved = self.decide(session_id, pending_id, "APPROVE")
            self.assertEqual(approved["action"]["status"], "EXECUTED")
            self.assertEqual(grounded.call_count, 1)   # no new grounding for the decision
        self.assertEqual(self.count("action_receipts"), 1)

    def test_every_kind_of_turn_survives_a_restart_exactly(self):
        session_id, pending_id = self.waiting()
        self.say(session_id, "另外那件 T 恤我也想换", CLARIFY)
        self.decide(session_id, pending_id, "APPROVE")
        before = (self.view(session_id), self.conversation(session_id)._snapshot())
        self.restart()
        after = (self.view(session_id), self.conversation(session_id)._snapshot())
        self.assertEqual(after, before)


# --------------------------------------------------------------------------
# Failed turns and event ids (acceptance 5)
# --------------------------------------------------------------------------


class FailedTurnTests(PersistentTestCase):
    def test_a_failed_turn_leaves_no_trace_after_a_restart(self):
        session_id = self.session()
        head = self.session_file(session_id)["head"]
        with self.assertLogs("aftersales_service.service", level="WARNING"):
            self.say(session_id, RETURN_TEXT, GET_ORDER, requests.ConnectionError("down"),
                     expected=503)
        self.assertEqual(self.session_file(session_id)["head"], head)
        self.restart()
        self.assertEqual(self.view(session_id)["messages"], [])
        start = len(self.provider.requests)
        self.request_return(session_id)
        self.assertEqual(runtime_context(self.provider.requests[start])["step_number"], 1)

    def test_a_failed_answer_to_a_clarification_is_retried_with_the_new_answer_after_a_restart(self):
        session_id = self.session()
        self.say(session_id, "我要退货", CLARIFY)
        with self.assertLogs("aftersales_service.service", level="WARNING"):
            self.say(session_id, "ORD-1001", requests.ConnectionError("down"), expected=503)
        self.restart()
        start = len(self.provider.requests)
        self.say(session_id, "ORD-3015", REFUSE)
        self.assertEqual(user_messages(self.provider.requests[start]), ["我要退货", "ORD-3015"])
        self.assertEqual(self.decisions(start), [(2, 5)])
        self.assertNotIn("ORD-1001", json.dumps(self.view(session_id)["messages"], ensure_ascii=False))

    def test_an_operator_outcome_the_head_missed_is_reconciled_once(self):
        session_id, pending_id = self.waiting()
        # The process died right after resume_action: the database has the outcome,
        # the head does not.
        self.service.store.gateway.resume_action(self.approval(pending_id))
        self.restart()
        view = self.view(session_id)
        self.assertEqual((view["status"], view["pending_actions"][0]["status"]), ("OPEN", "EXECUTED"))
        self.assertEqual(self.kinds(view).count(("assistant", "operator_decision")), 1)
        self.assertNotIn("event_id", json.dumps(view["messages"]))
        event_ids = [entry.get("event_id") for entry in self.conversation(session_id)._transcript]
        self.assertEqual([item for item in event_ids if item],
                         ["decision:" + pending_id + ":EXECUTED"])
        # Recovering again appends nothing; a repeated decision is the core's replay.
        self.restart()
        self.assertEqual(self.kinds(self.view(session_id)).count(("assistant", "operator_decision")), 1)
        again = self.decide(session_id, pending_id, "APPROVE")
        self.assertTrue(again["action"]["idempotent_replay"])
        self.assertEqual(self.kinds(self.view(session_id)).count(("assistant", "operator_decision")), 1)
        self.assertEqual(self.count("action_receipts"), 1)

    def test_a_decision_already_recorded_is_not_appended_again_by_reconciliation(self):
        session_id, pending_id = self.waiting()
        self.decide(session_id, pending_id, "REJECT")
        self.restart()
        self.assertEqual(self.kinds(self.view(session_id)).count(("assistant", "operator_decision")), 1)

    def test_an_approval_recorded_but_not_executed_is_finished_by_a_repeated_approve(self):
        session_id, pending_id = self.waiting()
        # The process died between T1 (decision recorded) and T2 (execution).
        self.service.store.gateway.record_decision(self.approval(pending_id))
        self.restart()
        view = self.view(session_id)
        self.assertEqual(view["status"], "WAITING_APPROVAL")
        self.assertEqual((view["pending_actions"][0]["status"],
                          view["pending_actions"][0]["approval_recorded"]), ("WAITING_APPROVAL", True))
        approved = self.decide(session_id, pending_id, "APPROVE")
        self.assertEqual(approved["action"]["status"], "EXECUTED")
        self.assertEqual(self.count("action_receipts"), 1)
        self.assertEqual(self.kinds(self.view(session_id)).count(("assistant", "operator_decision")), 1)


# --------------------------------------------------------------------------
# Reset (acceptance 6)
# --------------------------------------------------------------------------


class ResetTests(PersistentTestCase):
    def test_after_reset_no_old_session_resumes_against_the_new_database(self):
        session_id, pending_id = self.waiting()
        old_generation, old_file = self.generation(), self.session_path(session_id).read_text("utf-8")
        self.post("/api/aftersales/demo/reset")
        self.assertNotEqual(self.generation(), old_generation)
        self.assertFalse((self.data_dir / ("gen-" + old_generation)).exists())
        base = "/api/aftersales/sessions/" + session_id
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(self.client.post(base + "/messages", json={"text": "hi"}).status_code, 404)
        self.assertEqual(self.client.post("/api/aftersales/operator/sessions/" + session_id + "/decision",
                                          json={"pending_action_id": pending_id,
                                                "decision": "APPROVE"}).status_code, 404)
        # Its file copied into the new generation is still not a session of it.
        self.session_path(session_id).write_text(old_file, encoding="utf-8")
        self.assertEqual(self.client.get(base).status_code, 404)
        self.restart()
        self.assertEqual(self.client.get(base).status_code, 404)
        self.assertEqual(self.count("pending_actions"), 0)


# --------------------------------------------------------------------------
# The marker (acceptance 7; 3 in process)
# --------------------------------------------------------------------------


class MarkerTests(PersistentTestCase):
    def test_a_start_action_exception_after_the_marker_write_leaves_no_marker(self):
        session_id = self.session()
        head = self.session_file(session_id)["head"]
        with mock.patch.object(SessionFiles, "write", autospec=True,
                               side_effect=SessionFiles.write) as writes, \
                mock.patch.object(ActionGateway, "start_action", side_effect=ValueError("broken")), \
                self.assertLogs("aftersales_service.service", level="WARNING"):
            failed = self.say(session_id, RETURN_TEXT, GET_ORDER, CREATE_RETURN, expected=500)
        self.assertEqual(failed["detail"], {"code": "agent_internal_error"})
        markers = [item.kwargs["inflight"] for item in writes.call_args_list]
        self.assertEqual([marker is not None for marker in markers], [True, False])
        self.assertEqual(self.session_file(session_id)["head"], head)
        self.assertIsNone(self.session_file(session_id)["inflight"])
        self.assertEqual(self.view(session_id)["messages"], [])
        self.assertEqual(self.request_return(session_id)["status"], "WAITING_APPROVAL")

    def test_an_unresolved_marker_refuses_every_request_until_recovery_succeeds(self):
        session_id = self.session()
        with mock.patch.object(ActionOutcomeRenderer, "render", side_effect=RuntimeError("render")):
            with self.assertRaises(RuntimeError):
                self.request_return(session_id)
            calls = len(self.provider.requests)
            base = "/api/aftersales/sessions/" + session_id
            for response in (self.client.get(base),
                             self.client.post(base + "/messages", json={"text": "还在吗"}),
                             self.client.post("/api/aftersales/operator/sessions/" + session_id
                                              + "/decision", json={"pending_action_id": "PA-" + "0" * 16,
                                                                   "decision": "APPROVE"})):
                with self.subTest(path=str(response.request.url)):
                    self.assertEqual((response.status_code, response.json()["detail"]),
                                     (409, {"code": "recovery_pending"}))
            self.assertEqual(len(self.provider.requests), calls)   # no new turn, no model call
            self.assertIsNotNone(self.session_file(session_id)["inflight"])
        view = self.view(session_id)
        self.assertEqual(self.kinds(view), [("customer", None), ("assistant", "action")])
        self.assertIsNone(self.session_file(session_id)["inflight"])
        self.assertEqual(self.count("pending_actions"), 1)

    def test_recovery_refuses_a_marker_that_does_not_bind_its_checkpoint(self):
        session_id = self.session()
        self.crash_after_start_action(session_id)
        self.restart()
        path, manifest = self.session_path(session_id), self.session_file(session_id)
        for forged in ({**manifest["inflight"], "args_sha256": "0" * 64},
                       {**manifest["inflight"], "checkpoint_id": manifest["head"]}):
            with self.subTest(forged=forged):
                path.write_text(json.dumps({**manifest, "inflight": forged}), encoding="utf-8")
                response = self.client.get("/api/aftersales/sessions/" + session_id)
                self.assertEqual((response.status_code, response.json()["detail"]),
                                 (409, {"code": "recovery_pending"}))
        self.assertEqual(self.count("pending_actions"), 1)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.view(session_id)["status"], "WAITING_APPROVAL")

    def test_the_start_up_scan_records_a_crashed_action_without_any_request(self):
        session_id = self.session()
        self.crash_after_start_action(session_id)
        self.restart()
        calls = len(self.provider.requests)
        with TestClient(self.app):   # application start-up: the router's start hook
            self.assertIsNone(self.session_file(session_id)["inflight"])
            self.assertTrue(self.conversation(session_id)._loaded)
        self.assertEqual(len(self.provider.requests), calls)
        self.restart()
        view = self.view(session_id)
        self.assertEqual(self.kinds(view), [("customer", None), ("assistant", "action")])
        self.assertEqual([event["event_name"] for event in view["audit"]],
                         ["guard.evaluated", "action.pending_created", "action.replay_hit"])

    def test_a_failed_head_write_of_an_action_turn_is_recovered(self):
        session_id = self.session()
        real_write = SessionFiles.write

        def head_write_fails(files, session, **fields):
            if fields["inflight"] is None:
                raise PermissionError("held by another process")
            return real_write(files, session, **fields)

        with mock.patch.object(SessionFiles, "write", autospec=True, side_effect=head_write_fails):
            with self.assertRaises(PermissionError):
                self.request_return(session_id)
        self.assertIsNotNone(self.session_file(session_id)["inflight"])
        view = self.view(session_id)
        self.assertEqual((view["status"], self.kinds(view)),
                         ("WAITING_APPROVAL", [("customer", None), ("assistant", "action")]))
        self.assertEqual(self.count("pending_actions"), 1)

    def test_a_failed_head_write_of_a_plain_turn_is_a_failed_turn(self):
        session_id = self.session()
        head = self.session_file(session_id)["head"]
        with mock.patch.object(SessionFiles, "write", side_effect=PermissionError("held")), \
                self.assertLogs("aftersales_service.service", level="WARNING"):
            failed = self.say(session_id, "我要退货", CLARIFY, expected=500)
        self.assertEqual(failed["detail"], {"code": "agent_internal_error"})
        self.assertEqual(self.session_file(session_id)["head"], head)
        self.assertEqual(self.view(session_id)["messages"], [])
        self.restart()
        self.assertEqual(self.view(session_id)["status"], "OPEN")


# --------------------------------------------------------------------------
# Concurrency and decisions (acceptance 8)
# --------------------------------------------------------------------------


class ConcurrencyTests(PersistentTestCase):
    def test_a_concurrent_turn_and_decision_serialize_and_both_persist(self):
        session_id, pending_id = self.waiting()
        entered, release, results = threading.Event(), threading.Event(), {}

        def slow_model(messages, kwargs):
            entered.set()
            release.wait(10)
            return REFUSE

        self.provider.responses.append(slow_model)
        turn = threading.Thread(target=lambda: results.update(
            turn=self.service.submit(session_id, "还有一个问题")))
        decide = threading.Thread(target=lambda: results.update(
            decision=self.service.decide(session_id, pending_id, "APPROVE")))
        turn.start()
        self.assertTrue(entered.wait(10))
        decide.start()
        decide.join(0.3)
        self.assertTrue(decide.is_alive())   # waits for the session lock
        self.assertEqual(self.pending(pending_id)["status"], "PENDING_APPROVAL")
        release.set()
        turn.join(10)
        decide.join(10)
        self.assertEqual((results["turn"]["reply"]["kind"], results["decision"]["action"]["status"]),
                         ("refuse", "EXECUTED"))
        self.restart()
        self.assertEqual(self.kinds(self.view(session_id)),
                         [("customer", None), ("assistant", "action"), ("customer", None),
                          ("assistant", "refuse"), ("assistant", "operator_decision")])

    def test_a_decision_during_a_clarification_leaves_it_answerable_after_a_restart(self):
        session_id, pending_id = self.waiting()
        self.say(session_id, "另外那件 T 恤我也想换", CLARIFY)
        self.decide(session_id, pending_id, "APPROVE")
        self.restart()
        self.assertEqual(self.view(session_id)["status"], "NEEDS_CLARIFICATION")
        start = len(self.provider.requests)
        self.say(session_id, "ORD-1004", REFUSE)
        self.assertEqual(self.decisions(start), [(2, 5)])


# --------------------------------------------------------------------------
# The head invariant: after every request, the conversation is its head
# --------------------------------------------------------------------------


def head_view(service: AftersalesService, session_id: str) -> Conversation:
    """A new Conversation rebuilt from the head alone (no recovery, no reconciliation)."""
    original = service._sessions[session_id]
    manifest = service._runtime.sessions.read(session_id)
    fresh = Conversation(session_id=session_id, persona=original.persona, runtime=service._runtime)
    fresh._load(fresh._checkpoint(manifest["head"]).values)
    fresh._head = manifest["head"]
    return fresh


class HeadInvariantTests(unittest.TestCase):
    def test_after_every_request_of_every_scenario_the_conversation_equals_its_head(self):
        checked = []
        locked, create = AftersalesService._locked, AftersalesService.create_session

        def verify(service: AftersalesService, session_id: str) -> None:
            conversation = service._sessions.get(session_id)
            if conversation is None or conversation.closed or not conversation._loaded:
                return   # discarded: the next request rebuilds it from the head
            manifest = service._runtime.sessions.read(session_id)
            assert manifest["inflight"] is None, "a request ended with a marker set"
            assert manifest["head"] == conversation._head, "the head on disk is not the object's"
            fresh = head_view(service, session_id)
            assert fresh._snapshot() == conversation._snapshot(), "the object is not its head"
            checked.append(session_id)

        @contextmanager
        def checked_locked(service, session_id):
            with locked(service, session_id) as conversation:
                try:
                    yield conversation
                finally:
                    verify(service, session_id)

        def checked_create(service, persona_id):
            payload = create(service, persona_id)
            verify(service, payload["session_id"])
            return payload

        cases = list(test_aftersales_golden.scripted_cases())
        loader = unittest.TestLoader()
        for module in (test_aftersales_graph,):
            for name in sorted(vars(module)):
                cls = getattr(module, name)
                if isinstance(cls, type) and issubclass(cls, ProductTestCase) and cls.__module__ == module.__name__:
                    cases.extend(loader.loadTestsFromTestCase(cls))
        with mock.patch.object(AftersalesService, "_locked", checked_locked), \
                mock.patch.object(AftersalesService, "create_session", checked_create):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite(cases))
        self.assertTrue(result.wasSuccessful(), "\n".join(
            detail for _, detail in result.failures + result.errors))
        self.assertGreater(len(cases), 45)
        self.assertGreater(len(checked), 150)   # 193 requests at the time of writing


if __name__ == "__main__":
    unittest.main()
