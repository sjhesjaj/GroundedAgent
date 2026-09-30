"""Stage 4 Eval Completion: the control-layer scorer and the expected-evidence matcher.

Synthetic cases only. Labels are read here, after the control run and the
evidence enrichment are over - never before.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from eval_v2 import scoring
from eval_v2.control import Clarify, Finish, ToolCall, canonical_json
from eval_v2.evidence import EvidenceItem, derive_evidence_state
from eval_v2.runner import run_case
from eval_v2.scoring import (
    CONTROL_SCORE_SCHEMA,
    EvalScoringError,
    evidence_matches,
    json_values_equal,
    score_case,
)
from orchestration.contracts import evidence_ref

from tests.test_v2_eval_evidence import (
    A01,
    A03,
    A06,
    A07,
    A14,
    A15,
    MALFORMED,
    MULTI_PACKAGE,
    PURE_POLICY,
    ScriptedPolicy,
    fault,
    get_inventory,
    get_logistics,
    get_order,
    run,
    script_of,
    search,
    synthetic_case,
)


def business(entity, record_id, field, **extra):
    return {"subject": {"entity": entity, "id": record_id}, "field": field,
            "source_types": ["business"], **extra}


def derived(field, record_id=None, **extra):
    subject = {"entity": "derived"}
    if record_id is not None:
        subject["id"] = record_id
    return {"subject": subject, "field": field, "source_types": ["derived"], **extra}


def policy(policy_id, field, version=None, **extra):
    subject = {"entity": "policy", "id": policy_id}
    if version is not None:
        subject["version"] = version
    return {"subject": subject, "field": field, "source_types": ["document"], **extra}


def labels(*, all_of=(), any_of=(), forbidden=(), required=(), forbidden_caps=(),
           final="answer", clarify=None):
    return dict(
        expected_evidence={"all_of": list(all_of), "any_of": [list(g) for g in any_of],
                           "forbidden": list(forbidden)},
        expected_capabilities={"required": list(required), "forbidden": list(forbidden_caps)},
        expected_answerability={"final": final, "clarify": clarify or {"required": False,
                                                                      "slots": []}})


def labelled_case(scenario_kwargs, label_values, **extra):
    case = synthetic_case(**{**scenario_kwargs, **extra})
    case.update(copy.deepcopy(label_values))
    return case


def evaluate(scenario, label_values, *, final="answer", **extra):
    kwargs, calls = scenario
    case = labelled_case(kwargs, label_values, **extra)
    record = run(case, *calls, final=final)
    state = derive_evidence_state(record)
    return score_case(case, record, state), case, record, state


A01_LABELS = labels(
    all_of=[business("logistics", "SF1001", "delivered_at", tool="get_logistics"),
            business("order_item", "OI-1001-1", "category", value="服装"),
            derived("within_return_window", "OI-1001-1", value=True)],
    any_of=[[policy("november-promo-return", "window_days", value=15),
             policy("standard-return", "window_days", value=7)]],
    required=["search_after_sales_policy", "get_order", "get_logistics", "derived_facts"])


def item(state, predicate):
    return next(entry for entry in state.evidence_items if predicate(entry.evidence))


# --------------------------------------------------------------------------
# JSON value comparison
# --------------------------------------------------------------------------


class JsonValueTests(unittest.TestCase):
    def test_type_sensitive_equality(self):
        for left, right, equal in (
                (True, 1, False), (False, 0, False), (1, 1, True), (True, True, True),
                (None, None, True), (None, "null", False), ("1", 1, False), (1, 1.0, False),
                ([True], [1], False), ([1, 2], [1, 2], True), ({"a": 1, "b": [True]},
                                                             {"b": [True], "a": 1}, True),
                ({"a": 1}, {"a": True}, False), (1.5, 1.5, False)):
            with self.subTest(left=left, right=right):
                self.assertIs(json_values_equal(left, right), equal)


# --------------------------------------------------------------------------
# Matcher
# --------------------------------------------------------------------------


class MatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kwargs, calls = A01
        cls.state = derive_evidence_state(run(synthetic_case(**kwargs), *calls))

    def refs(self, spec):
        return scoring.matching_refs(spec, self.state.evidence_items)

    def test_business_subject_field_value_tool_locator(self):
        delivered = item(self.state, lambda e: e.locator == "logistics:SF1001#delivered_at")
        self.assertEqual(self.refs(business("logistics", "SF1001", "delivered_at")),
                         (delivered.ref,))
        for spec in (
                business("logistics", "SF1001", "delivered_at",
                         value="2026-11-05T14:30:00+08:00", tool="get_logistics",
                         locator="logistics:SF1001#delivered_at"),):
            self.assertEqual(self.refs(spec), (delivered.ref,))
        for spec in (business("logistics", "SF1002", "delivered_at"),
                     business("logistics", "SF1001", "shipped_at",
                              value="2026-11-05T14:30:00+08:00"),
                     business("logistics", "SF1001", "delivered_at", value="2026-11-05"),
                     business("logistics", "SF1001", "delivered_at", tool="get_order"),
                     business("logistics", "SF1001", "delivered_at", locator="SF1001"),
                     business("order", "SF1001", "delivered_at")):
            with self.subTest(spec=spec):
                self.assertEqual(self.refs(spec), ())

    def test_matcher_never_reads_content(self):
        status = item(self.state, lambda e: e.locator == "order:ORD-1001#status")
        spec = business("order", "ORD-1001", "status", value="已签收")
        self.assertTrue(evidence_matches(spec, status))
        altered = copy.deepcopy(status.evidence)
        altered.content = "已发货 已取消 anything"
        self.assertTrue(evidence_matches(spec, EvidenceItem(
            ref=status.ref, producer=status.producer, evidence=altered)))
        altered.metadata["value"] = "已发货"
        altered.content = "已签收"
        self.assertFalse(evidence_matches(spec, EvidenceItem(
            ref=status.ref, producer=status.producer, evidence=altered)))

    def test_policy_id_version_field_value(self):
        self.assertEqual(len(self.refs(policy("november-promo-return", "window_days", value=15))), 1)
        self.assertEqual(len(self.refs(policy("november-promo-return", "window_days",
                                              version="1"))), 1)
        for spec in (policy("november-promo-return", "window_days", version="2"),
                     policy("november-promo-return", "window_days", value=7),
                     policy("standard-return", "window_days"),
                     {**policy("november-promo-return", "window_days"),
                      "source_types": ["wiki"]}):
            with self.subTest(spec=spec):
                self.assertEqual(self.refs(spec), ())

    def test_derived_field_value_and_exact_record_key(self):
        window = [e.ref for e in self.state.evidence_items
                  if getattr(e.evidence, "fact_key", None) == "within_return_window"]
        self.assertEqual(len(window), 1)
        self.assertEqual(self.refs(derived("within_return_window")), tuple(window))
        self.assertEqual(self.refs(derived("within_return_window", "OI-1001-1", value=True)),
                         tuple(window))
        for spec in (derived("within_return_window", "OI-1001"),       # substring
                     derived("within_return_window", "order_item:OI-1001-1"),
                     derived("within_return_window", "OI-1001-1", value=1),  # True != 1
                     derived("within_return_window", value=False),
                     derived("inventory_available"),
                     derived("within_return_window", tool="get_logistics")):
            with self.subTest(spec=spec):
                self.assertEqual(self.refs(spec), ())
        days = derived("days_since_delivery", "SF1001", value=10)
        self.assertEqual(len(self.refs(days)), 1)
        self.assertEqual(self.refs({**days, "value": True}), ())

    def test_source_types_are_enforced(self):
        spec = {**derived("within_return_window"), "source_types": ["business"]}
        self.assertEqual(self.refs(spec), ())


# --------------------------------------------------------------------------
# Evidence score
# --------------------------------------------------------------------------


class EvidenceScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kwargs, calls = A01
        cls.items = derive_evidence_state(run(synthetic_case(**kwargs), *calls)).evidence_items

    HIT = business("logistics", "SF1001", "delivered_at")
    MISS = business("logistics", "SF9999", "delivered_at")
    HIT2 = derived("within_return_window", "OI-1001-1", value=True)
    MISS2 = derived("inventory_available")

    def score(self, **expected):
        base = {"all_of": [], "any_of": [], "forbidden": []}
        base.update(expected)
        # Looked up at call time, so the mutation guards can swap it.
        return scoring.score_evidence(base, self.items)

    def test_all_of_needs_every_requirement(self):
        score = self.score(all_of=[self.HIT, self.MISS, self.HIT2])
        self.assertEqual((score.all_of_total, score.all_of_matched, score.missing_all_of),
                         (3, 2, (1,)))
        self.assertFalse(score.evidence_ok)
        self.assertTrue(self.score(all_of=[self.HIT, self.HIT2]).evidence_ok)
        self.assertTrue(self.score().evidence_ok)

    def test_any_of_is_an_and_of_or_groups(self):
        score = self.score(any_of=[[self.MISS, self.HIT], [self.MISS2]])
        self.assertEqual((score.any_of_groups_total, score.any_of_groups_matched,
                          score.missing_any_of_groups), (2, 1, (1,)))
        self.assertFalse(score.evidence_ok)
        self.assertTrue(self.score(any_of=[[self.MISS, self.HIT], [self.HIT2]]).evidence_ok)

    def test_forbidden_is_reported_not_failed(self):
        forbidden = {"subject": {"entity": "logistics", "id": "SF1001"}}
        score = self.score(all_of=[self.HIT], forbidden=[forbidden])
        self.assertTrue(score.forbidden_evidence_present)
        self.assertEqual(len(score.forbidden_refs), 6)  # every SF1001 field
        self.assertEqual(list(score.forbidden_refs), sorted(score.forbidden_refs))
        self.assertTrue(score.evidence_ok)
        absent = self.score(forbidden=[{"subject": {"entity": "order", "id": "ORD-2001"}}])
        self.assertFalse(absent.forbidden_evidence_present)
        self.assertEqual(absent.forbidden_refs, ())


# --------------------------------------------------------------------------
# Case score
# --------------------------------------------------------------------------


class CaseScoreTests(unittest.TestCase):
    def test_a01_control_success(self):
        score, *_ = evaluate(A01, A01_LABELS)
        self.assertEqual(score.schema, CONTROL_SCORE_SCHEMA)
        self.assertEqual((score.case_id, score.archetype), ("synthetic-1", "A01"))
        self.assertEqual(score.actual_capabilities,
                         ("search_after_sales_policy", "get_order", "get_logistics",
                          "derived_facts"))
        self.assertTrue(score.capabilities_ok and score.clarification_ok and score.evidence_ok
                        and score.final_ok and score.db_ok)
        self.assertTrue(score.control_success)
        self.assertEqual(set(score.to_dict()), {
            "schema", "case_id", "archetype", "termination", "control_steps", "tool_calls",
            "actual_capabilities", "missing_required_capabilities",
            "used_forbidden_capabilities", "extra_capabilities", "capabilities_ok",
            "clarification", "evidence", "expected_final", "actual_final", "final_ok",
            "db_ok", "control_success"})
        for name in ("task_success", "end_to_end_success"):
            self.assertNotIn(name, score.canonical_json())

    def test_a03_a06_a07_values(self):
        for scenario, spec in (
                (A03, derived("within_return_window", "OI-1001-1", value=False)),
                (A06, derived("inventory_available", "SKU-TSHIRT-L", value=False)),
                (A07, derived("business_state_conflict", "ORD-1002", value=True))):
            with self.subTest(spec=spec["field"]):
                score, *_ = evaluate(scenario, labels(all_of=[spec]))
                self.assertTrue(score.evidence_ok)
                flipped, *_ = evaluate(scenario, labels(all_of=[{**spec,
                                                                 "value": not spec["value"]}]))
                self.assertFalse(flipped.evidence_ok)

    def test_a14_a15_required_tool_failure_is_insufficient(self):
        for scenario in (A14, A15):
            score, *_ = evaluate(scenario, A01_LABELS)
            self.assertFalse(score.evidence_ok)
            self.assertEqual(score.evidence.missing_all_of, (0, 2))
            self.assertFalse(score.control_success)

    def test_multi_package_window_label_is_not_met(self):
        score, *_ = evaluate(MULTI_PACKAGE, labels(all_of=[derived("within_return_window")]))
        self.assertFalse(score.evidence_ok)

    def test_capabilities(self):
        score, *_ = evaluate(A01, labels(required=["get_inventory", "get_order"],
                                         forbidden_caps=["get_logistics"]))
        self.assertEqual(score.missing_required_capabilities, ("get_inventory",))
        self.assertEqual(score.used_forbidden_capabilities, ("get_logistics",))
        self.assertEqual(score.extra_capabilities,
                         ("search_after_sales_policy", "derived_facts"))
        self.assertFalse(score.capabilities_ok)
        extra_only, *_ = evaluate(A01, labels(required=["get_order"]))
        self.assertTrue(extra_only.capabilities_ok)  # extras are a diagnostic only
        self.assertTrue(extra_only.extra_capabilities)

    def test_derived_facts_capability_follows_derivation_records(self):
        pure, *_ = evaluate(PURE_POLICY, labels(forbidden_caps=["derived_facts"]))
        self.assertNotIn("derived_facts", pure.actual_capabilities)
        self.assertTrue(pure.capabilities_ok)
        malformed, *_ = evaluate(MALFORMED, labels(required=["get_logistics"]))
        # A malformed call still exercised the capability.
        self.assertIn("get_logistics", malformed.actual_capabilities)
        self.assertNotIn("derived_facts", malformed.actual_capabilities)

    def test_final_disposition(self):
        score, *_ = evaluate(A06, labels(final="refuse"), final="answer")
        self.assertEqual((score.expected_final, score.actual_final, score.final_ok),
                         ("refuse", "answer", False))
        score, *_ = evaluate(A06, labels(final="refuse"), final="refuse")
        self.assertTrue(score.final_ok)
        case = labelled_case({}, labels(final="answer"))
        record = run_case(case, ScriptedPolicy(*([get_inventory("SKU-MUG")] * 2)), max_steps=2)
        score = score_case(case, record, derive_evidence_state(record))
        self.assertEqual((score.termination, score.final_ok), ("max_steps_exceeded", False))

    def test_db_ok_is_rechecked_from_the_record(self):
        score, case, record, state = evaluate(A06, labels())
        self.assertTrue(score.db_ok)
        for forged in (dataclasses.replace(record, database_unchanged=False),
                       dataclasses.replace(record, final_db_sha256="0" * 64)):
            forged_state = derive_evidence_state(forged)
            self.assertFalse(score_case(case, forged, forged_state).db_ok)

    def test_inputs_must_belong_together(self):
        score, case, record, state = evaluate(A06, labels())
        other = dict(case, case_id="synthetic-2")
        with self.assertRaises(EvalScoringError):
            score_case(other, record, state)
        _, _, _, other_state = evaluate(A01, labels())
        with self.assertRaises(EvalScoringError):
            score_case(case, record, other_state)
        with self.assertRaises(Exception):
            score_case(dict(case, archetype="A99"), record, state)

    def test_scorer_mutates_nothing(self):
        _, case, record, state = evaluate(MULTI_PACKAGE, A01_LABELS)
        before = (canonical_json(case), record.canonical_json(), state.canonical_json(),
                  canonical_json([r.to_dict() for r in state.tool_results]))
        first = score_case(case, record, state)
        second = score_case(case, record, state)
        after = (canonical_json(case), record.canonical_json(), state.canonical_json(),
                 canonical_json([r.to_dict() for r in state.tool_results]))
        self.assertEqual(before, after)
        self.assertTrue(state.unchanged())
        self.assertTrue(all(o.result_unchanged() for o in record.observations
                            if hasattr(o, "result_unchanged")))
        self.assertEqual(first.canonical_json(), second.canonical_json())


# --------------------------------------------------------------------------
# Clarification
# --------------------------------------------------------------------------


def clarify_case(required, slots, conditional):
    case = synthetic_case()
    case["user_turns"] = [{"text": "第一句"}] + [
        {"on_clarify": list(turn_slots), "text": "补充"} for turn_slots in conditional]
    case["expected_answerability"]["clarify"] = {"required": required, "slots": list(slots)}
    case["expected_capabilities"] = {"required": [], "forbidden": []}
    return case


def clarification_score(case, *actions):
    record = run_case(case, ScriptedPolicy(*actions), max_steps=6)
    return score_case(case, record, derive_evidence_state(record))


class ClarificationScoreTests(unittest.TestCase):
    def test_not_required(self):
        case = clarify_case(False, (), ())
        clean = clarification_score(case, Finish(disposition="answer")).clarification
        self.assertTrue(clean.clarification_ok)
        self.assertFalse(clean.over_ask)
        asked = clarification_score(case, Clarify(slots=("order_id",))).clarification
        self.assertTrue(asked.over_ask and asked.clarify_attempted and asked.unanswered_clarification)
        self.assertFalse(asked.clarification_ok)

    def test_required_and_matched(self):
        case = clarify_case(True, ("order_id",), (("order_id",),))
        score = clarification_score(case, Clarify(slots=("order_id",)),
                                    Finish(disposition="answer"))
        clarification = score.clarification
        self.assertEqual((clarification.clarify_required, clarification.clarify_matched,
                          clarification.expected_slots, clarification.requested_slots),
                         (True, True, ("order_id",), ("order_id",)))
        self.assertTrue(clarification.clarification_ok)
        self.assertTrue(score.control_success)

    def test_required_but_never_asked(self):
        case = clarify_case(True, ("order_id",), (("order_id",),))
        clarification = clarification_score(case, Finish(disposition="answer")).clarification
        self.assertEqual(clarification.missing_slots, ("order_id",))
        self.assertFalse(clarification.clarification_ok)

    def test_extra_slot_is_an_over_ask(self):
        case = clarify_case(True, ("order_id",), (("order_id", "reason"),))
        clarification = clarification_score(case, Clarify(slots=("order_id", "reason")),
                                            Finish(disposition="answer")).clarification
        self.assertEqual(clarification.extra_slots, ("reason",))
        self.assertTrue(clarification.over_ask and clarification.clarify_matched)
        self.assertFalse(clarification.clarification_ok)

    def test_missing_slot_and_unanswered(self):
        case = clarify_case(True, ("order_id", "reason"), (("order_id",), ("reason",)))
        partial = clarification_score(case, Clarify(slots=("order_id",)),
                                      Finish(disposition="answer")).clarification
        self.assertEqual(partial.missing_slots, ("reason",))
        self.assertFalse(partial.clarification_ok)
        both = clarification_score(case, Clarify(slots=("order_id",)), Clarify(slots=("reason",)),
                                   Finish(disposition="answer")).clarification
        self.assertTrue(both.clarification_ok)
        unanswered = clarification_score(case, Clarify(slots=("target_sku",))).clarification
        self.assertTrue(unanswered.unanswered_clarification)
        self.assertFalse(unanswered.clarification_ok)


# --------------------------------------------------------------------------
# Labels never reach the policy
# --------------------------------------------------------------------------


class RecordingPolicy:
    """Deterministic: fixed tool plan, records every state it is shown."""

    def __init__(self):
        self.plan = [search("退货 服装"), get_order(), get_logistics(),
                     Finish(disposition="answer")]
        self.seen: list[str] = []

    def next_action(self, state):
        self.seen.append(canonical_json({
            "virtual_now": state.virtual_now, "persona_id": state.persona_id,
            "allowed_tools": list(state.allowed_tools), "max_steps": state.max_steps,
            "step_number": state.step_number, "remaining_steps": state.remaining_steps,
            "user_messages": [[m.turn_index, m.text] for m in state.user_messages],
            "observations": [o.to_dict() for o in state.observations]}))
        return self.plan[len(self.seen) - 1]


class LabelLeakageTests(unittest.TestCase):
    def test_labels_change_the_score_never_the_run(self):
        left = labelled_case({}, A01_LABELS, case_id="leak-a", archetype="A01")
        right = labelled_case({}, labels(all_of=[derived("inventory_available")], final="refuse",
                                         forbidden_caps=["get_logistics"]),
                              case_id="leak-b", archetype="A12")
        policies = RecordingPolicy(), RecordingPolicy()
        records = [run_case(case, policy, max_steps=8)
                   for case, policy in zip((left, right), policies)]
        self.assertEqual(policies[0].seen, policies[1].seen)
        for text in policies[0].seen:
            for word in ("leak-a", "A01", "expected", "archetype"):
                self.assertNotIn(word, text)
        states = [derive_evidence_state(record) for record in records]
        enriched = [state.to_dict() for state in states]
        for payload in enriched:
            # The only difference: each names its own raw record (case ids differ).
            payload.pop("control_run_sha256")
        self.assertEqual(json.dumps(enriched[0]), json.dumps(enriched[1]))
        scores = [score_case(case, record, state)
                  for case, record, state in zip((left, right), records, states)]
        self.assertTrue(scores[0].control_success)
        self.assertFalse(scores[1].control_success)
        self.assertNotEqual(scores[0].canonical_json(), scores[1].canonical_json())


if __name__ == "__main__":
    unittest.main()
