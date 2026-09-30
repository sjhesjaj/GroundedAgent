"""Stage 4 Eval Completion: evaluate_case, the dataset runner, and mutation guards.

Synthetic in-test cases only: no eval/v2 dev or validation dataset exists, and
the sealed holdout is never read.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from aftersales.derived import derive_window_eligibility
from aftersales.policy_catalog import PublishedPolicyCatalog
from eval_v2 import dataset, scoring
from eval_v2 import evidence as ev
from eval_v2.control import (
    Clarify,
    ControlPolicyContractError,
    Finish,
    canonical_json,
)
from eval_v2.dataset import (
    DATASET_RUN_SCHEMA,
    DatasetRunError,
    EvaluatedCase,
    dataset_run_sha256,
)
from eval_v2.evidence import EvidenceState
from eval_v2.runtime import complete_delivered_at_evidence
from orchestration.contracts import ToolResult, ToolStatus

from tests import test_v2_eval_evidence as evidence_tests
from tests import test_v2_eval_scoring as scoring_tests
from tests.test_v2_eval_evidence import (
    get_inventory,
    get_logistics,
    get_order,
    search,
    synthetic_case,
)
from tests.test_v2_eval_scoring import (
    A01_LABELS,
    RecordingPolicy,
    derived,
    labelled_case,
    labels,
)

ROOT = Path(__file__).resolve().parent.parent
DATASET_SOURCE = ROOT / "eval_v2" / "dataset.py"

# Plans keyed by the (business) first user message, which the policy may see.
PLANS = {
    "plan-a01": [search("退货 服装"), get_order(), get_logistics(), Finish(disposition="answer")],
    "plan-a06": [get_inventory("SKU-TSHIRT-L"), Finish(disposition="answer")],
    "plan-clarify": [Clarify(slots=("target_sku",))],
    "plan-loop": [get_inventory("SKU-MUG")] * 10,
}


class PlanPolicy:
    """Deterministic and stateless: the first user message picks the plan, the
    step number the action."""

    def next_action(self, state):
        return PLANS[state.user_messages[0].text][state.step_number - 1]


def plan_case(case_id, plan, label_values):
    case = labelled_case({}, label_values, case_id=case_id)
    case["user_turns"] = [{"text": plan}]
    return case


def mixed_cases():
    clarify = plan_case("c-clarify", "plan-clarify",
                        labels(clarify={"required": True, "slots": ["order_id"]}))
    clarify["user_turns"].append({"on_clarify": ["order_id"], "text": "ORD-1001"})
    return [
        plan_case("c-a01", "plan-a01", A01_LABELS),
        plan_case("c-a06", "plan-a06", labels(
            all_of=[derived("inventory_available", "SKU-TSHIRT-L", value=False)],
            required=["get_inventory"], final="refuse")),
        clarify,
        plan_case("c-loop", "plan-loop", labels(required=["get_inventory"])),
    ]


def run_mixed(max_steps=4):
    return dataset.run_dataset(mixed_cases(), policy_factory=PlanPolicy, max_steps=max_steps)


# --------------------------------------------------------------------------
# evaluate_case
# --------------------------------------------------------------------------


class EvaluateCaseTests(unittest.TestCase):
    def test_run_then_enrich_then_score(self):
        order = []

        def spy(name, real):
            def wrapper(*args, **kwargs):
                order.append(name)
                return real(*args, **kwargs)
            return wrapper

        with mock.patch.object(dataset, "run_case", spy("run", dataset.run_case)), \
                mock.patch.object(dataset, "derive_evidence_state",
                                  spy("derive", dataset.derive_evidence_state)), \
                mock.patch.object(dataset, "score_case", spy("score", dataset.score_case)):
            evaluated = dataset.evaluate_case(mixed_cases()[0], PlanPolicy(), max_steps=8)
        self.assertEqual(order, ["run", "derive", "score"])
        self.assertIsInstance(evaluated, EvaluatedCase)
        self.assertEqual(evaluated.evidence_state.control_run_sha256,
                         evaluated.control_record.sha256())
        self.assertTrue(evaluated.score.control_success)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            evaluated.score = None

    def test_evaluate_case_never_lets_labels_reach_the_policy(self):
        left = labelled_case({}, A01_LABELS, case_id="leak-a", archetype="A01")
        right = labelled_case({}, labels(final="handoff", forbidden_caps=["get_order"]),
                              case_id="leak-b", archetype="A20")
        policies = RecordingPolicy(), RecordingPolicy()
        results = [dataset.evaluate_case(case, policy, max_steps=8)
                   for case, policy in zip((left, right), policies)]
        self.assertEqual(policies[0].seen, policies[1].seen)
        self.assertNotEqual(results[0].score.canonical_json(), results[1].score.canonical_json())

    def test_enrichment_is_label_free_end_to_end(self):
        left = labelled_case({}, A01_LABELS, case_id="same-input")
        right = labelled_case({}, labels(all_of=[derived("inventory_available")]),
                              case_id="same-input", archetype="A06")
        states = [dataset.evaluate_case(case, RecordingPolicy(), max_steps=8).evidence_state
                  for case in (left, right)]
        self.assertEqual(states[0].canonical_json(), states[1].canonical_json())
        self.assertTrue(states[0].derived_evidence)


# --------------------------------------------------------------------------
# run_dataset
# --------------------------------------------------------------------------


class DatasetRunTests(unittest.TestCase):
    def test_authored_order_and_shape(self):
        result = run_mixed()
        self.assertEqual(result.schema, DATASET_RUN_SCHEMA)
        self.assertEqual(result.case_count, 4)
        self.assertEqual([r.case_id for r in result.case_results],
                         ["c-a01", "c-a06", "c-clarify", "c-loop"])
        reversed_run = dataset.run_dataset(list(reversed(mixed_cases())),
                                           policy_factory=PlanPolicy, max_steps=4)
        self.assertEqual([r.case_id for r in reversed_run.case_results],
                         ["c-loop", "c-clarify", "c-a06", "c-a01"])
        for entry in result.case_results:
            self.assertEqual(set(entry.to_dict()), {"case_id", "control_run_sha256",
                                                    "evidence_state_sha256", "score"})
        self.assertEqual(set(result.to_dict()), {"schema", "max_steps", "case_count",
                                                 "case_results", "summary"})

    def test_summary_is_control_layer_only(self):
        summary = run_mixed().summary
        self.assertEqual(summary["case_count"], 4)
        self.assertEqual((summary["control_success_count"], summary["control_success_rate"]),
                         (1, 0.25))
        self.assertEqual(summary["final_ok_count"], 1)
        self.assertEqual(summary["evidence_ok_count"], 4)
        self.assertEqual(summary["clarification_ok_count"], 3)
        self.assertEqual(summary["capabilities_ok_count"], 4)
        self.assertEqual(summary["db_ok_count"], 4)
        self.assertEqual(summary["termination_counts"], {
            "finished": 2, "unanswered_clarification": 1, "max_steps_exceeded": 1})
        self.assertEqual(summary["average_control_steps"], (4 + 2 + 1 + 4) / 4)
        for word in ("factual", "citation", "cost", "latency", "duration", "answer_"):
            self.assertFalse([key for key in summary if word in key])

    def test_deterministic_repeat(self):
        first, second = run_mixed(), run_mixed()
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(dataset_run_sha256(first), dataset_run_sha256(second))
        self.assertEqual([r.control_run_sha256 for r in first.case_results],
                         [r.control_run_sha256 for r in second.case_results])
        self.assertEqual([r.evidence_state_sha256 for r in first.case_results],
                         [r.evidence_state_sha256 for r in second.case_results])

    def test_no_user_text_is_copied(self):
        payload = run_mixed().canonical_json()
        for plan in PLANS:
            self.assertNotIn(plan, payload)
        self.assertNotIn("user_turns", payload)

    def test_factory_is_called_once_per_case_without_arguments(self):
        calls = []

        def factory(*args, **kwargs):
            calls.append((args, kwargs))
            return PlanPolicy()

        dataset.run_dataset(mixed_cases(), policy_factory=factory, max_steps=4)
        self.assertEqual(calls, [((), {})] * 4)

    def test_reused_policy_instance_fails_loudly(self):
        shared = PlanPolicy()
        with self.assertRaises(DatasetRunError):
            dataset.run_dataset(mixed_cases(), policy_factory=lambda: shared, max_steps=4)

    def test_factory_contract(self):
        with self.assertRaises(ControlPolicyContractError):
            dataset.run_dataset(mixed_cases(), policy_factory=object, max_steps=4)
        with self.assertRaises(DatasetRunError):
            dataset.run_dataset(mixed_cases(), policy_factory=None, max_steps=4)
        with self.assertRaises(DatasetRunError):
            dataset.run_dataset([], policy_factory=PlanPolicy, max_steps=4)
        cases = mixed_cases()
        with self.assertRaises(DatasetRunError):
            dataset.run_dataset([cases[0], copy.deepcopy(cases[0])], policy_factory=PlanPolicy,
                                max_steps=4)

    def test_policy_factory_never_receives_a_case(self):
        tree = ast.parse(DATASET_SOURCE.read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "policy_factory"]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0].args, calls[0].keywords), ([], []))
        run_dataset = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                           and node.name == "run_dataset")
        self.assertEqual([arg.arg for arg in run_dataset.args.args], ["cases"])
        self.assertEqual([arg.arg for arg in run_dataset.args.kwonlyargs],
                         ["policy_factory", "max_steps"])


# --------------------------------------------------------------------------
# Mutation guards
# --------------------------------------------------------------------------


def _labels_drive_derivation(case, policy, *, max_steps):
    record = dataset.run_case(case, policy, max_steps=max_steps)
    state = dataset.derive_evidence_state(record)
    wanted = {spec.get("field") for spec in case["expected_evidence"]["all_of"]}
    kept = tuple(fact for fact in state.derived_evidence if fact.fact_key in wanted)
    state = dataclasses.replace(state, derived_evidence=kept)
    return EvaluatedCase(control_record=record, evidence_state=state,
                         score=dataset.score_case(case, record, state))


def _labels_reach_the_policy(case, policy, *, max_steps):
    hinted = copy.deepcopy(case)
    hinted["user_turns"][0]["text"] += " " + case["expected_answerability"]["final"]
    record = dataset.run_case(hinted, policy, max_steps=max_steps)
    state = dataset.derive_evidence_state(record)
    return EvaluatedCase(control_record=record, evidence_state=state,
                         score=dataset.score_case(case, record, state))


_REAL_ENRICH = ev._enrich


def _enrich_rereads_database(*args, **kwargs):
    sqlite3.connect(":memory:").close()
    return _REAL_ENRICH(*args, **kwargs)


def _item_window_picks_the_delivered_package(result, policy, *, clock, category):
    delivered = [item for item in complete_delivered_at_evidence(result)
                 if item.metadata["value"] is not None]
    return derive_window_eligibility(delivered[0], policy, clock=clock, category=category)


class _Everything(frozenset):
    def __contains__(self, item):
        return True


def _catalog_policies_unobserved():
    def observed(seen):
        records = PublishedPolicyCatalog().snapshot().records
        return {ev.policy_ref(r): r for r in records if r.rule_type in ev.WINDOW_RULE_TYPES}

    def selected(seen, policies):
        return {ref: _Everything() for ref in policies}

    patcher = mock.patch.multiple(ev, _observed_policies=observed, _selected_categories=selected)
    return patcher


_REAL_COLLECT = ev._collect


def _malformed_becomes_error(observations):
    seen, failures = _REAL_COLLECT(observations)
    for failure in failures:
        seen.append(ev._Seen(failure.observation_id, failure.tool_name, dict(failure.arguments),
                             ToolResult(tool_name=failure.tool_name, status=ToolStatus.ERROR,
                                        error_code="malformed", error_message="malformed")))
    return seen, []


_REAL_SCORE_EVIDENCE = scoring.score_evidence


def _any_of_global_or(expected, items):
    score = _REAL_SCORE_EVIDENCE(expected, items)
    any_hit = any(scoring.matching_refs(spec, items)
                  for group in expected["any_of"] for spec in group)
    ok = not score.missing_all_of and (any_hit or not expected["any_of"])
    return dataclasses.replace(score, evidence_ok=ok)


def _forbidden_fails_evidence(expected, items):
    score = _REAL_SCORE_EVIDENCE(expected, items)
    return dataclasses.replace(score, evidence_ok=score.evidence_ok
                               and not score.forbidden_evidence_present)


_REAL_RUN_DATASET = dataset.run_dataset


def _factory_gets_the_case(cases, *, policy_factory, max_steps):
    return _REAL_RUN_DATASET(cases, policy_factory=lambda: policy_factory(cases[0]),
                             max_steps=max_steps)


def _factory_called_once(cases, *, policy_factory, max_steps):
    policy = policy_factory()
    evaluations = [dataset.evaluate_case(case, policy, max_steps=max_steps) for case in cases]
    return evaluations


def _reuse_allowed(cases, *, policy_factory, max_steps):
    evaluations = [dataset.evaluate_case(case, policy_factory(), max_steps=max_steps)
                   for case in cases]
    return evaluations


def _scope_free_rules_widen(category, selected, policies):
    return sorted(ref for ref, targets in selected.items()
                  if category in targets or not policies[ref].scope)


def _general_joins_explicit(category, selected, policies):
    return sorted(ref for ref, targets in selected.items()
                  if category in targets or (None in targets and not policies[ref].scope))


def _patch(owner, name, value):
    return lambda: mock.patch.object(owner, name, value)


MUTANTS = (
    ("derived_follows_labels", _patch(dataset, "evaluate_case", _labels_drive_derivation),
     (EvaluateCaseTests, "test_enrichment_is_label_free_end_to_end")),
    ("derived_rereads_database", _patch(ev, "_enrich", _enrich_rereads_database),
     (evidence_tests.LabelFreeDerivationTests, "test_no_database_tool_or_gateway_is_touched")),
    ("item_window_picks_a_package",
     _patch(ev, "derive_item_window_from_logistics_result",
            _item_window_picks_the_delivered_package),
     (evidence_tests.DerivationFamilyTests, "test_multi_package_ambiguity_is_preserved")),
    ("unobserved_catalog_policy", _catalog_policies_unobserved,
     (evidence_tests.PolicyProvenanceTests, "test_only_observed_policies_drive_derivation")),
    ("provenance_gap_ignored", _patch(ev, "validate_policy_refs", lambda *a, **k: None),
     (evidence_tests.PolicyProvenanceTests, "test_removed_structured_field_fails_loudly")),
    ("malformed_fabricated_as_error", _patch(ev, "_collect", _malformed_becomes_error),
     (evidence_tests.FaultEnrichmentTests,
      "test_malformed_is_a_contract_failure_never_a_tool_result")),
    ("any_of_global_or", _patch(scoring, "score_evidence", _any_of_global_or),
     (scoring_tests.EvidenceScoreTests, "test_any_of_is_an_and_of_or_groups")),
    ("true_equals_one", _patch(scoring, "json_values_equal", lambda a, b: a == b),
     (scoring_tests.MatcherTests, "test_derived_field_value_and_exact_record_key")),
    ("forbidden_presence_fails_case", _patch(scoring, "score_evidence", _forbidden_fails_evidence),
     (scoring_tests.EvidenceScoreTests, "test_forbidden_is_reported_not_failed")),
    ("policy_factory_gets_case", _patch(dataset, "run_dataset", _factory_gets_the_case),
     (DatasetRunTests, "test_factory_is_called_once_per_case_without_arguments")),
    ("one_policy_for_all_cases", _patch(dataset, "run_dataset", _factory_called_once),
     (DatasetRunTests, "test_factory_is_called_once_per_case_without_arguments")),
    ("reused_instance_accepted", _patch(dataset, "run_dataset", _reuse_allowed),
     (DatasetRunTests, "test_reused_policy_instance_fails_loudly")),
    ("scope_free_rule_widened", _patch(ev, "_applicable_window_refs", _scope_free_rules_widen),
     (evidence_tests.GeneralSelectionFallbackTests,
      "test_scope_free_rule_is_not_widened_without_an_observed_general_selection")),
    ("general_joins_explicit_winner",
     _patch(ev, "_applicable_window_refs", _general_joins_explicit),
     (evidence_tests.GeneralSelectionFallbackTests,
      "test_explicit_category_winner_overrides_the_general_selection")),
    ("labels_reach_policy", _patch(dataset, "evaluate_case", _labels_reach_the_policy),
     (EvaluateCaseTests, "test_evaluate_case_never_lets_labels_reach_the_policy")),
)


def run_scenario(patcher, scenario):
    """Run one acceptance scenario outside any test result, so a failing check
    propagates instead of being recorded."""
    owner, name = scenario
    owner.setUpClass()
    try:
        probe = owner(name)
        try:
            if patcher is None:
                getattr(probe, name)()
            else:
                with patcher():
                    getattr(probe, name)()
        finally:
            probe.doCleanups()
    finally:
        owner.tearDownClass()


class MutationGuardTests(unittest.TestCase):
    def test_the_real_pipeline_passes_every_scenario(self):
        for name, _, scenario in MUTANTS:
            with self.subTest(mutant=name):
                run_scenario(None, scenario)

    def test_every_mutant_is_caught_by_its_scenario(self):
        for name, patcher, scenario in MUTANTS:
            with self.subTest(mutant=name):
                with self.assertRaises(Exception):
                    run_scenario(patcher, scenario)


if __name__ == "__main__":
    unittest.main()
