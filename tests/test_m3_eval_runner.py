"""The eval_m3 runner itself (Phase 4), offline: scripted models, temporary databases.

These tests check the harness, the scorer and the judge plumbing on the frozen
KB-DEV / Stage 6 subset inputs. Scripted model replies stand in for DeepSeek;
nothing here is a model result.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aftersales_service import knowledge_base as kb
from eval_m3 import judge as judge_module
from eval_m3 import run as cli
from eval_m3 import runner, scoring
from eval_m3.mock_models import MockAgent, MockJudge
from eval_m3.retrieval import rankings, retrieval_recall
from tests.test_aftersales_service import ScriptedProvider, call, decision

EXCHANGE_3001 = {"order_id": "ORD-3001", "order_item_id": "OI-3001-1", "target_sku": "SKU-TSHIRT-M",
                 "reason_code": "size_or_spec_mismatch"}
RETURN_1001 = {"order_id": "ORD-1001", "order_item_id": "OI-1001-1", "reason_code": "no_longer_wanted"}

_KB = None


def knowledge_base():
    global _KB
    if _KB is None:
        _KB = kb.KnowledgeBase(kb.load_corpus())   # BM25 only, no Ollama
    return _KB


def cases_by_id():
    return {case.case_id: case for case in runner.load_kb_dev() + runner.load_stage6_subset()}


class RunnerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = cases_by_id()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="m3-eval-test-", ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def run_scripted(self, case_id, *responses):
        provider = ScriptedProvider()
        provider.responses = list(responses)
        recorded = runner.RecordingProvider(provider, role="agent")
        run = runner.run_case(self.cases[case_id], recorded, data_root=self.root,
                              knowledge_base_factory=knowledge_base)
        return run, provider


class FrozenInputTests(RunnerTestCase):
    def test_suites_load_with_manifest_counts_and_case_settings(self):
        kb_dev = runner.load_kb_dev()
        subset = runner.load_stage6_subset()
        self.assertEqual((len(kb_dev), sum(len(case.turns) for case in kb_dev)), (40, 50))
        self.assertEqual(len(subset), 24)
        manifest = json.loads(runner.SUBSET_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual([case.case_id for case in subset], manifest["case_ids"])
        self.assertEqual(self.cases["s6-dev-007"].virtual_now.isoformat(), "2026-12-10T15:00:00+08:00")
        self.assertEqual(self.cases["s6-dev-016"].action_faults[0]["point"], "policy_catalog")
        self.assertEqual(self.cases["kb-dev-018"].initial_state, self.cases["s6-dev-001"].initial_state)
        self.assertEqual(self.cases["s6-dev-015"].turns[1].on_clarify, ("order_id",))

    def test_changed_inputs_are_refused(self):
        with mock.patch.object(runner, "normalized_sha256", return_value="0" * 64):
            with self.assertRaises(runner.DatasetIntegrityError):
                runner.load_kb_dev()
            with self.assertRaises(runner.DatasetIntegrityError):
                runner.load_stage6_subset()
        with mock.patch.object(runner, "canonical_sha256", return_value="0" * 64):
            with self.assertRaises(runner.DatasetIntegrityError):
                runner.load_stage6_subset()

    def test_the_sealed_holdout_is_never_read(self):
        with self.assertRaises(runner.DatasetIntegrityError):
            runner.load_kb_dev(runner.SEALED_DIRECTORY / "kb-holdout.zip")
        with self.assertRaises(ValueError):
            runner.load_suite("kb-holdout")


class HarnessTests(RunnerTestCase):
    def test_stage6_exchange_runs_on_the_case_state_and_clock_and_scores(self):
        run, provider = self.run_scripted("s6-dev-001", decision(call("get_order", {"order_id": "ORD-3001"})),
                                          decision(call("create_exchange", EXCHANGE_3001)))
        self.assertIsNone(run["infra_error"])
        self.assertIn("ORD-3001", run["baseline_state"]["orders"])
        new_cases = [row for key, row in run["final_state"]["after_sales_cases"].items()
                     if key not in run["baseline_state"]["after_sales_cases"]]
        self.assertEqual([row["created_at"] for row in new_cases], ["2026-11-15T10:00:00+08:00"])
        self.assertIn("2026-11-15T10:00:00+08:00", json.dumps(provider.requests[0]["messages"], ensure_ascii=False))
        self.assertEqual(run["turns"][0]["reads"][0]["tool_name"], "get_order")
        score = scoring.score_case_run(self.cases["s6-dev-001"], run)
        self.assertTrue(all(score["hard_invariants"].values()), score["invariant_problems"])
        self.assertEqual(score["stage6"]["details"]["final_state_problems"], [])
        self.assertTrue(score["passed"], score["stage6"])

    def test_each_case_gets_its_own_database_and_business_time(self):
        run, provider = self.run_scripted("s6-dev-007", decision(call("finish", {"disposition": "refuse"})))
        self.assertIn("ORD-3007", run["baseline_state"]["orders"])
        self.assertNotIn("ORD-3001", run["baseline_state"]["orders"])
        self.assertIn("2026-12-10T15:00:00+08:00", json.dumps(provider.requests[0]["messages"], ensure_ascii=False))
        score = scoring.score_case_run(self.cases["s6-dev-007"], run)
        self.assertFalse(score["passed"])
        self.assertTrue(all(score["hard_invariants"].values()))

    def test_action_faults_reach_the_product_gateway(self):
        run, _ = self.run_scripted("s6-dev-016", decision(call("get_order", {"order_id": "ORD-3016"})),
                                   decision(call("create_exchange", dict(EXCHANGE_3001, order_id="ORD-3016",
                                                                         order_item_id="OI-3016-1"))))
        self.assertEqual((run["turns"][0]["action"]["status"], run["turns"][0]["action"]["code"]),
                         ("FAILED", "policy_unavailable"))
        self.assertTrue(run["action_fault_records"])
        score = scoring.score_case_run(self.cases["s6-dev-016"], run)
        self.assertTrue(score["passed"], score["stage6"])

    def test_a_clarification_answer_is_sent_only_when_it_covers_every_requested_slot(self):
        run, _ = self.run_scripted(
            "s6-dev-015", decision(call("ask_user", {"slots": ["order_id"]})),
            decision(call("get_order", {"order_id": "ORD-3015"})),
            decision(call("create_return", {"order_id": "ORD-3015", "order_item_id": "OI-3015-1",
                                            "reason_code": "no_longer_wanted"})))
        self.assertEqual([turn["delivered"] for turn in run["turns"]], [True, True])
        score = scoring.score_case_run(self.cases["s6-dev-015"], run)
        self.assertTrue(score["stage6"]["clarification_ok"])
        self.assertTrue(score["passed"], score["stage6"])
        run, _ = self.run_scripted("s6-dev-015", decision(call("ask_user", {"slots": ["order_id", "order_item"]})))
        self.assertEqual([turn["delivered"] for turn in run["turns"]], [True, False])

    def test_kb_turn_waiting_approval_with_judge_verdicts(self):
        run, _ = self.run_scripted("kb-dev-017", decision(call("get_order", {"order_id": "ORD-1001"})),
                                   decision(call("create_return", RETURN_1001)))
        case = self.cases["kb-dev-017"]
        verdicts = judge_module.judge_case_run(runner.RecordingProvider(MockJudge(), role="judge"), case, run)
        score = scoring.score_case_run(case, run, verdicts)
        turn = score["turns"][0]
        self.assertTrue(turn["disposition_ok"] and turn["routing"]["ok"] and turn["facts_ok"], turn)
        self.assertTrue(score["passed"])
        request = judge_module.build_request(case, case.turns[0], run["turns"][0], [], run["baseline_state"])
        self.assertEqual(request["turn_trace"]["action"]["status"], "WAITING_APPROVAL")
        self.assertEqual([change["table"] for change in request["state_changes"]], ["pending_actions"])
        self.assertNotIn("expected_tool_route", json.dumps(request, ensure_ascii=False))

    def test_smalltalk_needs_no_model_call(self):
        run, provider = self.run_scripted("kb-dev-031")
        self.assertEqual(provider.requests, [])
        score = scoring.score_case_run(self.cases["kb-dev-031"], run,
                                       judge_module.judge_case_run(MockJudge(), self.cases["kb-dev-031"], run))
        self.assertTrue(score["passed"])

    def test_the_model_never_sees_labels(self):
        agent = ScriptedProvider()
        mock_agent = MockAgent()
        agent.responses = [lambda messages, kwargs: mock_agent.chat(messages, **kwargs)] * 8
        case = self.cases["kb-dev-021"]
        runner.run_case(case, runner.RecordingProvider(agent, role="agent"), data_root=self.root,
                        knowledge_base_factory=knowledge_base)
        sent = json.dumps([request["messages"] for request in agent.requests], ensure_ascii=False)
        self.assertTrue(agent.requests)
        for marker in ("expected_", "must_include", "must_not_include", "allow_history_only", case.case_id,
                       case.turns[0].labels["must_not_include"][0]):
            self.assertNotIn(marker, sent)


class InvariantTests(RunnerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        test = cls("test_untouched_run_holds_every_invariant")
        test.setUp()
        try:
            cls.base, _ = test.run_scripted("kb-dev-017", decision(call("get_order", {"order_id": "ORD-1001"})),
                                            decision(call("create_return", RETURN_1001)))
        finally:
            test.doCleanups()

    def violations(self, change):
        run = copy.deepcopy(self.base)
        change(run)
        invariants, _ = scoring.hard_invariants(run)
        return sorted(name for name, ok in invariants.items() if not ok)

    def test_untouched_run_holds_every_invariant(self):
        self.assertEqual(self.violations(lambda run: None), [])

    def test_each_violation_is_detected(self):
        def changed_order(run):
            run["final_state"]["orders"]["ORD-1001"] = dict(run["final_state"]["orders"]["ORD-1001"], status="x")

        def foreign_pending(run):
            key = next(iter(run["final_state"]["pending_actions"]))
            run["final_state"]["pending_actions"][key] = dict(run["final_state"]["pending_actions"][key],
                                                              persona_id="demo-b")

        def rejected_executed(run):
            key = next(iter(run["final_state"]["pending_actions"]))
            run["final_state"]["pending_actions"][key] = dict(run["final_state"]["pending_actions"][key],
                                                              status="REJECTED", receipt_id="RCPT-X")

        def outside_tool(run):
            run["turns"][0]["trace"]["steps"].append({"kind": "tool_call", "tool_name": "run_sql"})

        def duplicate_receipts(run):
            receipt = {"receipt_id": "R1", "idempotency_key": "k", "request_id": run["request_id"],
                       "persona_id": run["persona_id"], "action_name": "escalate_to_human",
                       "args_json": json.dumps({"order_id": "ORD-1001"}), "resource_type": "human_handoff_ticket",
                       "resource_id": "T1", "pending_action_id": None, "guard_decision": "ALLOW"}
            run["final_state"]["action_receipts"].update({"R1": receipt, "R2": dict(receipt, receipt_id="R2")})

        self.assertIn("no_unauthorized_write", self.violations(changed_order))
        self.assertIn("identity_boundary_ok", self.violations(foreign_pending))
        self.assertIn("rejected_never_executes", self.violations(rejected_executed))
        self.assertEqual(self.violations(outside_tool), ["capability_boundary_ok"])
        self.assertIn("one_receipt_per_execution", self.violations(duplicate_receipts))


class JudgeAndMetricTests(unittest.TestCase):
    REQUEST = {"must_include": [{"id": "I1", "text": "a"}], "must_not_include": [{"id": "N1", "text": "b"}],
               "check_history": True}

    def verdict(self, **override):
        data = {"must_include": [{"id": "I1", "satisfied": True, "evidence": "q"}],
                "must_not_include": [{"id": "N1", "violated": False, "evidence": ""}],
                "window_days": [{"matter": "return_window", "days": 15, "category": "通用", "applies_now": True}],
                "history_reuse": {"facts": [{"fact": "15天", "supported_by_current_reads": False}]}}
        data.update(override)
        return json.dumps(data, ensure_ascii=False)

    def test_verdict_parsing(self):
        parsed = judge_module.parse_verdict(self.verdict(), self.REQUEST)
        self.assertEqual(parsed["must_include"][0]["text"], "a")
        self.assertEqual(parsed["history_reuse"]["facts"][0]["supported_by_current_reads"], False)
        for bad in (self.verdict(must_include=[]), self.verdict(must_not_include=[{"id": "N9", "violated": True}]),
                    self.verdict(window_days=[{"matter": "price", "days": 7}]), "not json"):
            with self.assertRaises(judge_module.JudgeError):
                judge_module.parse_verdict(bad, self.REQUEST)

    def test_judge_retries_once_then_reports_the_error(self):
        provider = ScriptedProvider()
        provider.responses = [decision(), decision()]
        case = cases_by_id()["kb-dev-001"]
        turn = {"question": "q", "reply": {"kind": "answer", "text": "t"}, "state_after": None, "reads": []}
        with mock.patch.object(judge_module, "state_changes", return_value=[]):
            verdict = judge_module.judge_turn(provider, case, case.turns[0], turn, [], {})
        self.assertTrue(verdict["error"].startswith("judge_protocol"))
        self.assertEqual(len(provider.requests), 2)

    def test_window_days_are_compared_with_the_selected_rule(self):
        november, december = datetime.fromisoformat("2026-11-15T10:00:00+08:00"), \
            datetime.fromisoformat("2026-12-02T10:00:00+08:00")
        self.assertEqual(scoring.expected_window_days("return_window", None, november), 15)
        self.assertEqual(scoring.expected_window_days("return_window", None, december), 7)
        self.assertEqual(scoring.expected_window_days("exchange_window", "服装", november), 30)
        self.assertEqual(scoring.expected_window_days("exchange_window", None, november), 15)
        result = scoring.window_consistency([
            {"matter": "return_window", "days": 15, "category": "通用", "applies_now": True},
            {"matter": "return_window", "days": 7, "category": "通用", "applies_now": False}], november)
        self.assertTrue(result["consistent"])
        self.assertEqual(len(result["statements"]), 1)
        self.assertFalse(scoring.window_consistency(
            [{"matter": "return_window", "days": 7, "category": "未指明", "applies_now": True}], november)["consistent"])
        self.assertIsNone(scoring.window_consistency([], november))

    def test_history_only_ratio_counts_follow_up_turns_once(self):
        def score(case_id, facts):
            turn = {"turn": 2, "delivered": True, "routing": {"ok": True, "preferred_used": [], "preferred_total": 0},
                    "disposition_ok": True, "facts_ok": True, "facts": {"missing": [], "violated": []},
                    "citation": None, "window": None, "history": {"checked": True, "facts": facts}, "e2e": True,
                    "resources": {"model_calls": 1, "prompt_tokens": 1, "completion_tokens": 1,
                                  "cache_hit_tokens": 0, "seconds": 1.0}}
            return {"case_id": case_id, "suite": runner.SUITE_KB_DEV, "passed": True, "infra_error": None,
                    "hard_invariants": {name: True for name in scoring.HARD_INVARIANTS},
                    "retrieval_modes": [], "turns": [turn]}
        unsupported = [{"fact": "a", "supported_by_current_reads": False},
                       {"fact": "b", "supported_by_current_reads": False}]
        summary = scoring.summarize([[score("c1", unsupported),
                                      score("c2", [{"fact": "c", "supported_by_current_reads": True}]),
                                      score("c3", [])]])
        history = summary["kb"]["history_only"]
        self.assertEqual((history["numerator"], history["denominator"]), (1, 2))
        self.assertEqual(history["flagged"][0]["case_id"], "c1")
        empty = scoring.summarize([[score("c3", [])]])["kb"]["history_only"]
        self.assertEqual((empty["denominator"], empty["rate"], empty["display"]), (0, None, "N/A"))

    def test_peak_hours_and_cost(self):
        self.assertTrue(cli.is_peak(datetime.fromisoformat("2026-10-09T10:00:00+08:00")))    # Friday
        self.assertFalse(cli.is_peak(datetime.fromisoformat("2026-10-09T13:00:00+08:00")))
        self.assertFalse(cli.is_peak(datetime.fromisoformat("2026-10-10T10:00:00+08:00")))   # Saturday
        call_ = {"at": "2026-10-09T20:00:00+08:00", "prompt_tokens": 1_000_000, "cache_hit_tokens": 0,
                 "cache_miss_tokens": 1_000_000, "completion_tokens": 0}
        self.assertAlmostEqual(cli.call_cost_usd(call_), 0.15)


class RetrievalTests(unittest.TestCase):
    def test_hybrid_ranking_reproduces_the_product_search_order(self):
        class SameVector:
            def embed(self, texts):
                return [[1.0, float(len(text) % 7) + 1.0] for text in texts]

        knowledge = kb.KnowledgeBase(kb.load_corpus(), embedder=SameVector())
        case = cases_by_id()["kb-dev-001"]
        query = case.turns[0].text
        ranked = rankings(knowledge, query, case.virtual_now, SameVector().embed([query])[0])
        product = knowledge.search(query, as_of=case.virtual_now).passages
        self.assertEqual([knowledge.passages[index] for index in ranked["hybrid"][:4]], list(product))
        result = retrieval_recall([case], knowledge)
        self.assertEqual(result["turns"], 1)
        self.assertEqual(set(result["summary"]), {"bm25", "vector", "hybrid"})


class CommandLineTests(unittest.TestCase):
    def test_a_mock_dry_run_writes_records_and_a_summary(self):
        with tempfile.TemporaryDirectory(prefix="m3-eval-cli-", ignore_cleanup_errors=True) as directory, \
                mock.patch("builtins.print"):
            code = cli.main(["--suite", "kb-dev", "--suite", "stage6-subset", "--cases",
                             "kb-dev-001,kb-dev-031,s6-dev-004", "--provider", "mock", "--output", directory])
            self.assertEqual(code, 0)
            summary = json.loads(Path(directory, "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["meta"]["valid"])
            self.assertEqual(set(summary["suites"]), {"kb-dev", "stage6-subset"})
            lines = Path(directory, "run-1", "kb-dev.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line)["case_id"] for line in lines], ["kb-dev-001", "kb-dev-031"])
            self.assertEqual(cli.main(["--rescore", directory]), 0)
            rescored = json.loads(Path(directory, "summary.rescored.json").read_text(encoding="utf-8"))
            for suite in ("kb-dev", "stage6-subset"):
                original, again = summary["suites"][suite], rescored["suites"][suite]
                self.assertEqual({key: again[key] for key in original}, original)


if __name__ == "__main__":
    unittest.main()
