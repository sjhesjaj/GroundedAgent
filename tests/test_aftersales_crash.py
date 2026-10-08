"""GroundedAgent V2 M2 phase 4: real process crashes at each commit point.

Every scenario runs one request in a CHILD process (this module, run with
--crash-worker) that is killed with os._exit(17) at one injection point - no
finally blocks, no checkpoint or session-file write after it, open SQLite
connections and WAL files left as they are. The injection points are patches
in the child only; the product has no test switch. The parent then opens the
same data directory twice in a row (two restarts), lets each recover, and
requires both results to be identical.

Setup that must exist before the crash (a session, a pending action) is made
in process by the parent with the same scripted model, then closed. The child
receives its own scripted model responses as JSON.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from aftersales.action_gateway import ActionGateway
from aftersales_service import persistence
from aftersales_service.conversation import Conversation
from aftersales_service.persistence import SessionFiles
from aftersales_service.service import AftersalesService
from tests.test_aftersales_service import (
    REPO_ROOT,
    RETURN_ARGS,
    SEED_CASES,
    ScriptedProvider,
    call,
    decision,
    runtime_context,
    temporary_data_dir,
)

WORKER_FLAG = "--crash-worker"
CRASH_EXIT = 17
WORKER_TIMEOUT = 60
RETURN_TEXT = "ORD-1001 里那件内衣我不想要了，帮我退货"
RETURN_SCRIPT = (("get_order", {"order_id": "ORD-1001"}), ("create_return", RETURN_ARGS))
CLARIFY_SCRIPT = (("ask_user", {"slots": ["order_id"]}),)
REFUSE_SCRIPT = (("finish", {"disposition": "refuse"}),)
BUSINESS_TABLES = ("pending_actions", "action_receipts", "after_sales_cases")


# --------------------------------------------------------------------------
# The child: one request, killed at one injection point
# --------------------------------------------------------------------------


def _crash(*args, **kwargs):
    os._exit(CRASH_EXIT)


def _after(real, condition=None):
    """Run the real call, then die (when `condition` holds for its arguments)."""
    def patched(*args, **kwargs):
        result = real(*args, **kwargs)
        if condition is None or condition(*args, **kwargs):
            _crash()
        return result
    return patched


def _session_write(*, before: bool, marker: bool):
    """Die before/after the session-file write that sets (marker) or clears the marker."""
    real = SessionFiles.write

    def patched(files, session_id, **fields):
        hit = (fields["inflight"] is not None) == marker
        if hit and before:
            _crash()
        real(files, session_id, **fields)
        if hit and not before:
            _crash()
    return patched


def injection(cut: str) -> list[tuple[object, str, object]]:
    """The patches of one injection point (docs/v2/m2-session-recovery.md, phase 4 crash points)."""
    points = {
        # conversation.py: after the first _invoke returned G, before the marker write
        "before_marker": [(Conversation, "_marker", _crash)],
        # the marker is on disk, the gateway has not run
        "after_marker": [(SessionFiles, "write", _session_write(before=False, marker=True))],
        # Conversation._act, gateway stage: start_action has just returned
        "after_start_action": [(ActionGateway, "start_action", _after(ActionGateway.start_action))],
        # Conversation._commit: before / after the head write of a turn (here: a clarification)
        "before_head_write": [(SessionFiles, "write", _session_write(before=True, marker=False))],
        "after_head_write": [(SessionFiles, "write", _session_write(before=False, marker=False))],
        # Conversation.decide: resume_action returned, the head not yet written
        "after_resume_action": [(ActionGateway, "resume_action", _after(ActionGateway.resume_action))],
        # inside the core's resume_action: T1 committed, T2 not started
        "between_t1_and_t2": [(ActionGateway, "execute_approved", _crash)],
        # persistence.atomic_write_json: the temporary file written, not yet renamed
        "before_replace": [(persistence.os, "replace", _crash)],
    }
    return points[cut]


def run_worker(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    provider = ScriptedProvider()
    provider.responses.extend(decision(call(name, args)) for name, args in spec["script"])
    service = AftersalesService(lambda: provider, data_dir=spec["data_dir"])
    request = spec["request"]
    with ExitStack() as stack:
        for target, attribute, replacement in injection(spec["cut"]):
            stack.enter_context(mock.patch.object(target, attribute, replacement))
        if request["op"] == "say":
            service.submit(request["session_id"], request["text"])
        else:
            service.decide(request["session_id"], request["pending_action_id"], "APPROVE")
    service.close()
    return 0   # the injection point was never reached


# --------------------------------------------------------------------------
# The parent
# --------------------------------------------------------------------------


class CrashTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = temporary_data_dir(self)
        self.work_dir = temporary_data_dir(self)   # the child's spec, outside the data directory
        self.provider = ScriptedProvider()

    # -- services --------------------------------------------------------------

    def open_service(self) -> AftersalesService:
        service = AftersalesService(lambda: self.provider, data_dir=self.data_dir)
        self.addCleanup(service.close)
        return service

    def script(self, *steps) -> None:
        self.provider.responses.extend(decision(call(name, args)) for name, args in steps)

    def new_session(self, *turns) -> str:
        """A session, with its committed turns, made in process before the crash."""
        service = self.open_service()
        session_id = service.create_session("demo-a")["session_id"]
        for text, steps in turns:
            self.script(*steps)
            service.submit(session_id, text)
        service.close()
        return session_id

    def waiting(self) -> tuple[str, str]:
        service = self.open_service()
        session_id = service.create_session("demo-a")["session_id"]
        self.script(*RETURN_SCRIPT)
        pending_id = service.submit(session_id, RETURN_TEXT)["pending_action_id"]
        service.close()
        return session_id, pending_id

    # -- the crash -------------------------------------------------------------

    def crash(self, cut: str, request: dict, script=()) -> None:
        spec = {"data_dir": str(self.data_dir), "cut": cut, "request": request,
                "script": [[name, args] for name, args in script]}
        path = self.work_dir / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "tests.test_aftersales_crash", WORKER_FLAG, str(path)],
            cwd=REPO_ROOT, capture_output=True, timeout=WORKER_TIMEOUT,
            env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(completed.returncode, CRASH_EXIT,
                         completed.stderr.decode("utf-8", "replace")[-3000:])
        self.assert_files_open_after_the_crash()

    def generation_dir(self) -> Path:
        generation = json.loads((self.data_dir / "generation.json").read_text(encoding="utf-8"))
        return self.data_dir / ("gen-" + generation["generation"])

    def assert_files_open_after_the_crash(self) -> None:
        """Windows: the killed process's databases (WAL included) and session files open normally."""
        directory = self.generation_dir()
        for name in ("aftersales-demo.db", "checkpoints.db"):
            connection = sqlite3.connect(str(directory / name))
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",), name)
            finally:
                connection.close()
        for path in (directory / "sessions").glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    # -- after the crash ---------------------------------------------------------

    def session_file(self, session_id: str) -> dict:
        return json.loads((self.generation_dir() / "sessions" / (session_id + ".json"))
                          .read_text(encoding="utf-8"))

    def business(self) -> dict[str, list[tuple]]:
        connection = sqlite3.connect(str(self.generation_dir() / "aftersales-demo.db"))
        try:
            return {table: connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
                    for table in BUSINESS_TABLES}
        finally:
            connection.close()

    def recovered_twice(self, session_id: str, *, scan: bool = False) -> dict:
        """Two restarts: each recovers (by a request, or by the start-up scan alone)."""
        results = []
        for _ in range(2):
            service = self.open_service()
            state = {}
            if scan:
                service.start()   # no request names the session
                state["after_scan"] = (self.session_file(session_id), self.business())
            state["view"] = service.session_view(session_id)
            state["session_file"] = self.session_file(session_id)
            state["business"] = self.business()
            results.append(state)
            service.close()
        self.assertEqual(results[0], results[1])
        return results[0]

    def thread_checkpoints(self, session_id: str) -> list:
        """Every checkpoint of the session's thread, as the killed process left it."""
        service = self.open_service()
        service.start()
        config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
        checkpoints = list(service._runtime.graph.get_state_history(config))
        service.close()
        return checkpoints

    def kinds(self, view: dict) -> list[tuple]:
        return [(entry["role"], entry.get("kind")) for entry in view["messages"]]

    def decisions(self, start: int) -> list[tuple[int, int]]:
        return [(context["step_number"], context["remaining_steps"])
                for context in (runtime_context(request) for request in self.provider.requests[start:]
                                if request["tools"] is not None)]


class CommitPointCrashTests(CrashTestCase):
    def test_a_killed_after_g_before_the_marker_the_branch_is_discarded(self):
        session_id = self.new_session()
        head = self.session_file(session_id)["head"]
        self.crash("before_marker", {"op": "say", "session_id": session_id, "text": RETURN_TEXT},
                   RETURN_SCRIPT)
        # Killed after G: the thread holds a checkpoint stopped before the gateway, no marker.
        self.assertIn(("gateway",), [item.next for item in self.thread_checkpoints(session_id)])
        self.assertEqual((self.session_file(session_id)["head"], self.session_file(session_id)["inflight"]),
                         (head, None))
        state = self.recovered_twice(session_id)
        self.assertEqual((state["session_file"]["head"], state["session_file"]["inflight"]), (head, None))
        self.assertEqual(state["view"]["messages"], [])
        self.assertEqual(state["business"]["pending_actions"], [])
        self.assertEqual(len(state["business"]["after_sales_cases"]), SEED_CASES)
        # The next message is processed normally, from step 1 of a new run.
        service = self.open_service()
        self.script(*RETURN_SCRIPT)
        start = len(self.provider.requests)
        self.assertEqual(service.submit(session_id, RETURN_TEXT)["status"], "WAITING_APPROVAL")
        self.assertEqual(self.decisions(start), [(1, 6), (2, 5)])

    def test_b_killed_after_the_marker_before_the_gateway_recovery_submits_it_once(self):
        session_id = self.new_session()
        self.crash("after_marker", {"op": "say", "session_id": session_id, "text": RETURN_TEXT},
                   RETURN_SCRIPT)
        self.assertIsNotNone(self.session_file(session_id)["inflight"])
        self.assertEqual(self.business()["pending_actions"], [])   # the gateway never ran
        state = self.recovered_twice(session_id)
        self.assertIsNone(state["session_file"]["inflight"])
        self.assertEqual(len(state["business"]["pending_actions"]), 1)
        self.assertEqual(self.kinds(state["view"]), [("customer", None), ("assistant", "action")])
        self.assertEqual(state["view"]["status"], "WAITING_APPROVAL")
        self.assertEqual([event["event_name"] for event in state["view"]["audit"]],
                         ["guard.evaluated", "action.pending_created"])

    def test_c_killed_after_start_action_recovery_replays_one_business_write(self):
        session_id = self.new_session()
        self.crash("after_start_action", {"op": "say", "session_id": session_id, "text": RETURN_TEXT},
                   RETURN_SCRIPT)
        before = self.business()
        self.assertEqual(len(before["pending_actions"]), 1)   # committed before the kill
        state = self.recovered_twice(session_id)
        # Acceptance 3: exactly one business write (business tables, not audit rows).
        self.assertEqual(state["business"], before)
        self.assertEqual(len(state["business"]["after_sales_cases"]), SEED_CASES)
        self.assertEqual(state["business"]["action_receipts"], [])
        pending_id = state["business"]["pending_actions"][0][0]
        self.assertEqual(self.kinds(state["view"]), [("customer", None), ("assistant", "action")])
        self.assertEqual((state["view"]["status"], state["view"]["pending_action_id"]),
                         ("WAITING_APPROVAL", pending_id))
        self.assertIn("action.replay_hit", [event["event_name"] for event in state["view"]["audit"]])

    def test_d_killed_after_start_action_the_start_up_scan_alone_recovers_it(self):
        session_id = self.new_session()
        self.crash("after_start_action", {"op": "say", "session_id": session_id, "text": RETURN_TEXT},
                   RETURN_SCRIPT)
        before = self.business()
        state = self.recovered_twice(session_id, scan=True)
        scanned_file, scanned_business = state["after_scan"]
        self.assertIsNone(scanned_file["inflight"])
        self.assertEqual(scanned_business, before)
        self.assertEqual(self.kinds(state["view"]), [("customer", None), ("assistant", "action")])
        # The operator can approve it.
        service = self.open_service()
        pending_id = state["view"]["pending_action_id"]
        approved = service.decide(session_id, pending_id, "APPROVE")
        self.assertEqual(approved["action"]["status"], "EXECUTED")
        business = self.business()
        self.assertEqual((len(business["action_receipts"]), len(business["after_sales_cases"])),
                         (1, SEED_CASES + 1))

    def test_e1_killed_before_the_clarifications_head_write_the_turn_never_happened(self):
        session_id = self.new_session()
        head = self.session_file(session_id)["head"]
        self.crash("before_head_write", {"op": "say", "session_id": session_id, "text": "我要退货"},
                   CLARIFY_SCRIPT)
        # Killed after the clarification's checkpoints, before its head write.
        self.assertIn("clarify", [item.values.get("route") for item in self.thread_checkpoints(session_id)])
        self.assertEqual(self.session_file(session_id)["head"], head)
        state = self.recovered_twice(session_id)
        self.assertEqual(state["session_file"]["head"], head)
        self.assertEqual((state["view"]["status"], state["view"]["messages"]), ("OPEN", []))
        service = self.open_service()
        self.script(*CLARIFY_SCRIPT)
        start = len(self.provider.requests)
        service.submit(session_id, "我要退货")
        self.assertEqual(self.decisions(start), [(1, 6)])   # a new run: the question was never seen

    def test_e2_killed_after_the_clarifications_head_write_the_answer_continues_the_run(self):
        session_id = self.new_session()
        head = self.session_file(session_id)["head"]
        self.crash("after_head_write", {"op": "say", "session_id": session_id, "text": "我要退货"},
                   CLARIFY_SCRIPT)
        self.assertNotEqual(self.session_file(session_id)["head"], head)   # the head was written
        state = self.recovered_twice(session_id)
        self.assertEqual(state["view"]["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(self.kinds(state["view"]), [("customer", None), ("assistant", "clarification")])
        service = self.open_service()
        self.script(*RETURN_SCRIPT)
        start = len(self.provider.requests)
        answered = service.submit(session_id, "ORD-1001，里面那件内衣，不想要了")
        self.assertEqual(answered["status"], "WAITING_APPROVAL")
        self.assertEqual(self.decisions(start), [(2, 5), (3, 4)])

    def test_f_killed_after_resume_action_the_outcome_is_reconciled_once(self):
        session_id, pending_id = self.waiting()
        self.crash("after_resume_action",
                   {"op": "decide", "session_id": session_id, "pending_action_id": pending_id})
        self.assertEqual(len(self.business()["action_receipts"]), 1)
        state = self.recovered_twice(session_id)
        self.assertEqual((state["view"]["status"], state["view"]["pending_actions"][0]["status"]),
                         ("OPEN", "EXECUTED"))
        self.assertEqual(self.kinds(state["view"]).count(("assistant", "operator_decision")), 1)
        service = self.open_service()
        again = service.decide(session_id, pending_id, "APPROVE")
        self.assertTrue(again["action"]["idempotent_replay"])
        self.assertEqual(len(self.business()["action_receipts"]), 1)
        self.assertEqual(self.kinds(service.session_view(session_id)).count(
            ("assistant", "operator_decision")), 1)

    def test_g_killed_between_t1_and_t2_a_repeated_approve_executes_once(self):
        session_id, pending_id = self.waiting()
        self.crash("between_t1_and_t2",
                   {"op": "decide", "session_id": session_id, "pending_action_id": pending_id})
        self.assertEqual(self.business()["action_receipts"], [])
        state = self.recovered_twice(session_id)
        pending = state["view"]["pending_actions"][0]
        self.assertEqual((state["view"]["status"], pending["status"], pending["approval_recorded"]),
                         ("WAITING_APPROVAL", "WAITING_APPROVAL", True))
        service = self.open_service()
        approved = service.decide(session_id, pending_id, "APPROVE")
        self.assertEqual(approved["action"]["status"], "EXECUTED")
        business = self.business()
        self.assertEqual((len(business["action_receipts"]), len(business["after_sales_cases"])),
                         (1, SEED_CASES + 1))
        self.assertEqual(self.kinds(service.session_view(session_id)).count(
            ("assistant", "operator_decision")), 1)

    def test_h_killed_before_os_replace_the_previous_session_file_stands(self):
        session_id = self.new_session(("我要退货", CLARIFY_SCRIPT))
        committed = self.session_file(session_id)
        self.crash("before_replace", {"op": "say", "session_id": session_id, "text": "ORD-1004"},
                   REFUSE_SCRIPT)
        sessions = self.generation_dir() / "sessions"
        self.assertTrue(list(sessions.glob("*.tmp")))   # the new version was never renamed
        self.assertEqual(self.session_file(session_id), committed)
        state = self.recovered_twice(session_id)
        self.assertEqual(state["session_file"], committed)
        self.assertEqual(self.kinds(state["view"]), [("customer", None), ("assistant", "clarification")])
        service = self.open_service()
        self.script(*REFUSE_SCRIPT)
        start = len(self.provider.requests)
        service.submit(session_id, "ORD-1004")
        self.assertEqual(self.decisions(start), [(2, 5)])


if __name__ == "__main__":
    if sys.argv[1:2] == [WORKER_FLAG]:
        sys.exit(run_worker(sys.argv[2]))
    unittest.main()
