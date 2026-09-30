"""Stage 4 Eval Completion: label-free evidence enrichment and the Evidence Policy bridge.

Synthetic cases only, built on the demo seed plus explicit overlays. No dataset
content, no Planner, no LLM; the sealed holdout is never read.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from aftersales import policy_catalog
from eval_v2 import evidence as ev
from eval_v2.control import (
    ControlState,
    Finish,
    ToolCall,
    ToolContractFailure,
    ToolObservation,
)
from eval_v2.evidence import (
    DERIVATION_FAMILIES,
    DERIVED_PRODUCER,
    EVIDENCE_STATE_SCHEMA,
    EvalEvidenceError,
    EvidenceState,
    derive_evidence_state,
    derive_from_control_state,
    evaluate_evidence_state,
    evidence_state_sha256,
)
from eval_v2.faults import FaultInjectingGateway
from eval_v2.runner import control_run_sha256, run_case
from eval_v2.runtime import V2CaseRuntime
from orchestration.contracts import (
    BusinessEvidence,
    DerivedEvidence,
    SourceType,
    ToolResult,
    ToolStatus,
    evidence_ref,
)
from orchestration.evidence_policy_v2 import (
    ClaimScope,
    EvidenceOutcome,
    EvidenceRequirement,
    FreshnessRequirement,
    evaluate_evidence_v2,
)

from tests.test_v2_clock import clock_violations
from tests.test_v2_eval_runtime import base_case

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_SOURCE = ROOT / "eval_v2" / "evidence.py"

NOW = "2026-11-15T10:00:00+08:00"          # November promo (15 days) in force
AFTER_PROMO = "2026-12-10T10:00:00+08:00"   # standard 7-day return window only
SENTINEL_2031 = "2031-11-15T10:00:00+08:00"
USER_TEXT = "SECRET-USER-TEXT-SENTINEL"

PROMO = "policy:november-promo-return@1#build-0001"
STANDARD_RETURN = "policy:standard-return@1#build-0001"
APPAREL_EXCHANGE = "policy:apparel-exchange@1#build-0001"
STANDARD_EXCHANGE = "policy:standard-exchange@1#build-0001"


# --------------------------------------------------------------------------
# Synthetic cases and runs
# --------------------------------------------------------------------------


def search(query):
    return ToolCall(tool_name="search_after_sales_policy", arguments={"query": query})


def get_order(order_id="ORD-1001"):
    return ToolCall(tool_name="get_order", arguments={"order_id": order_id})


def get_logistics(order_id="ORD-1001"):
    return ToolCall(tool_name="get_logistics", arguments={"order_id": order_id})


def get_inventory(sku):
    return ToolCall(tool_name="get_inventory", arguments={"sku": sku})


def fault(tool, mode, on_call=1, **match):
    return {"tool": tool, "match": dict(match), "mode": mode, "on_call": on_call}


def synthetic_case(*, virtual_now=NOW, faults=(), case_id="synthetic-1", archetype="A01",
                   expected_evidence=None, **overlay):
    case = base_case(faults=list(faults), **overlay)
    case["case_id"] = case_id
    case["archetype"] = archetype
    case["virtual_now"] = virtual_now
    case["user_turns"] = [{"text": USER_TEXT}]
    if expected_evidence is not None:
        case["expected_evidence"] = expected_evidence
    return case


class ScriptedPolicy:
    """Plays a fixed script and keeps every ControlState it was shown."""

    def __init__(self, *script):
        self.script = list(script)
        self.states: list[ControlState] = []

    def next_action(self, state):
        self.states.append(state)
        return self.script.pop(0)


def script_of(*calls, final="answer"):
    return tuple(calls) + (Finish(disposition=final),)


def run(case, *calls, final="answer", max_steps=12):
    return run_case(case, ScriptedPolicy(*script_of(*calls, final=final)), max_steps=max_steps)


# The scenarios, as (case kwargs, tool calls).
A01 = (dict(), (search("退货 服装"), get_order(), get_logistics()))
A03 = (dict(virtual_now=AFTER_PROMO), (search("退货 服装"), get_order(), get_logistics()))
A06 = (dict(), (get_inventory("SKU-TSHIRT-L"),))
A07 = (dict(orders={"ORD-1002": {"op": "update", "set": {"status": "已签收"}}}),
       (get_order("ORD-1002"), get_logistics("ORD-1002")))
A14 = (dict(faults=[fault("get_logistics", "error")]),
       (search("退货 服装"), get_order(), get_logistics()))
A15 = (dict(faults=[fault("get_logistics", "timeout")]),
       (search("退货 服装"), get_order(), get_logistics()))
MALFORMED = (dict(faults=[fault("get_logistics", "malformed")]),
             (search("退货 服装"), get_order(), get_logistics()))
MULTI_PACKAGE = (dict(order_items={"OI-1004-1": {"op": "update", "set": {"category": "服装"}}}),
                 (search("退货 服装"), get_order("ORD-1004"), get_logistics("ORD-1004")))
PURE_POLICY = (dict(), (search("退货"),))
EXCHANGE_ONLY = (dict(), (search("换货 服装"), get_order(), get_logistics()))
EMPTY_LOGISTICS = (dict(), (search("退货 服装"), get_order("ORD-1003"),
                            get_logistics("ORD-1003")))


def scenario_record(scenario, **extra):
    kwargs, calls = scenario
    return run(synthetic_case(**{**kwargs, **extra}), *calls)


def scenario_state(scenario, **extra):
    return derive_evidence_state(scenario_record(scenario, **extra))


def facts(state, fact_key=None, subject=None):
    return [fact for fact in state.derived_evidence
            if (fact_key is None or fact.fact_key == fact_key)
            and (subject is None or fact.subject == subject)]


def records_of(state, family=None):
    return [record for record in state.derivation_records
            if family is None or record.family == family]


def replace_result(record, index, result):
    """The same raw record with observation `index` carrying another result."""
    old = record.observations[index]
    new = ToolObservation(sequence=old.sequence, control_step=old.control_step,
                          turn_index=old.turn_index, tool_step=old.tool_step,
                          observation_id=old.observation_id, tool_name=old.tool_name,
                          arguments=dict(old.arguments), result=result)
    observations = list(record.observations)
    observations[index] = new
    return dataclasses.replace(record, observations=tuple(observations))


def edited_policy_result(record, index, edit):
    result = copy.deepcopy(record.observations[index].result)
    result.evidence = tuple(edit(list(result.evidence)))
    return replace_result(record, index, result)


def requirement(requirement_id, subject, field, *, scope=ClaimScope.CURRENT_OPERATIONAL_STATE,
                providers=()):
    return EvidenceRequirement(requirement_id=requirement_id, scope=scope, subject=subject,
                               field=field, providers=providers)


# --------------------------------------------------------------------------
# EvidenceState
# --------------------------------------------------------------------------


class EvidenceStateTests(unittest.TestCase):
    def test_state_shape(self):
        record = scenario_record(A01)
        state = derive_evidence_state(record)
        self.assertEqual(state.schema, EVIDENCE_STATE_SCHEMA)
        self.assertEqual(state.control_run_sha256, control_run_sha256(record))
        self.assertEqual(state.virtual_now, NOW)
        self.assertEqual([r.tool_name for r in state.tool_results],
                         ["search_after_sales_policy", "get_order", "get_logistics"])
        self.assertEqual(state.contract_failures, ())
        self.assertEqual(set(state.to_dict()), {
            "schema", "control_run_sha256", "virtual_now", "tool_results", "contract_failures",
            "evidence_items", "derived_evidence", "derivation_records"})
        self.assertTrue(state.unchanged())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.virtual_now = AFTER_PROMO

    def test_tool_results_are_private_copies_of_the_observed_results(self):
        record = scenario_record(A01)
        state = derive_evidence_state(record)
        for observation, result in zip(record.observations, state.tool_results):
            self.assertIsNot(result, observation.result)
            self.assertEqual(result.to_dict(), observation.result.to_dict())
        state.tool_results[1].evidence[0].metadata["value"] = "tampered"
        self.assertFalse(state.unchanged())
        self.assertTrue(record.observations[1].result_unchanged())
        with self.assertRaises(EvalEvidenceError):
            evidence_state_sha256(state)

    def test_evidence_items_carry_ref_and_producer(self):
        state = scenario_state(A01)
        derived_refs = {evidence_ref(fact) for fact in state.derived_evidence}
        tool_refs = set()
        for result in state.tool_results:
            tool_refs |= {evidence_ref(item) for item in result.evidence}
        self.assertEqual({item.ref for item in state.evidence_items}, tool_refs | derived_refs)
        for item in state.evidence_items:
            self.assertEqual(item.ref, evidence_ref(item.evidence))
            if isinstance(item.evidence, DerivedEvidence):
                self.assertEqual(item.producer, DERIVED_PRODUCER)
            else:
                self.assertEqual(item.producer, item.evidence.metadata["tool"]
                                 if isinstance(item.evidence, BusinessEvidence)
                                 else "search_after_sales_policy")

    def test_duplicate_refs_keep_the_first_occurrence(self):
        record = run(synthetic_case(), get_inventory("SKU-TSHIRT-L"), get_inventory("SKU-TSHIRT-L"))
        state = derive_evidence_state(record)
        refs = [item.ref for item in state.evidence_items]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(len(refs), 2)  # one business field, one derived fact
        self.assertEqual(len(facts(state, "inventory_available")), 1)
        inventory = records_of(state, "inventory_available")
        self.assertEqual([r.input_observation_ids for r in inventory],
                         [("turn:1:tool:1",), ("turn:1:tool:2",)])
        self.assertEqual({r.evidence_ref for r in inventory},
                         {evidence_ref(facts(state, "inventory_available")[0])})

    def test_derivation_is_deterministic_and_ordered(self):
        record = scenario_record(MULTI_PACKAGE)
        first, second = derive_evidence_state(record), derive_evidence_state(record)
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.sha256(), second.sha256())
        keys = [(f.fact_key, f.subject, evidence_ref(f)) for f in first.derived_evidence]
        self.assertEqual(keys, sorted(keys))
        families = [DERIVATION_FAMILIES.index(r.family) for r in first.derivation_records]
        self.assertEqual(families, sorted(families))
        again = derive_evidence_state(scenario_record(MULTI_PACKAGE))
        self.assertEqual(again.sha256(), first.sha256())

    def test_evidence_state_is_derived_from_the_record_only(self):
        self.assertEqual(tuple(inspect.signature(derive_evidence_state).parameters), ("record",))
        with self.assertRaises(ValueError):
            derive_evidence_state(synthetic_case())

    def test_modified_observation_result_is_refused(self):
        record = scenario_record(A06)
        record.observations[0].result.evidence[0].metadata["value"] = 5
        with self.assertRaises(EvalEvidenceError):
            derive_evidence_state(record)


# --------------------------------------------------------------------------
# Label-free derivation
# --------------------------------------------------------------------------


class LabelFreeDerivationTests(unittest.TestCase):
    def test_same_record_different_labels_same_state(self):
        kwargs, calls = MULTI_PACKAGE
        left = synthetic_case(**kwargs)
        right = synthetic_case(**kwargs, archetype="A09", expected_evidence={
            "all_of": [{"subject": {"entity": "derived"}, "field": "within_return_window",
                        "source_types": ["derived"], "value": True}],
            "any_of": [], "forbidden": []})
        right["expected_capabilities"] = {"required": ["derived_facts"], "forbidden": []}
        left_record, right_record = run(left, *calls), run(right, *calls)
        self.assertEqual(left_record.canonical_json(), right_record.canonical_json())
        left_state = derive_evidence_state(left_record)
        right_state = derive_evidence_state(right_record)
        self.assertEqual(left_state.canonical_json(), right_state.canonical_json())
        # The label names a fact the evidence cannot establish; none is invented.
        self.assertEqual(facts(right_state, "within_return_window"), [])

    def test_2031_sentinel_follows_the_run_clock(self):
        state = scenario_state(A01, virtual_now=SENTINEL_2031)
        days = facts(state, "days_since_delivery", "logistics:SF1001")
        self.assertEqual([fact.value for fact in days], [1836])
        for fact in state.derived_evidence:
            self.assertEqual(fact.observed_at, SENTINEL_2031)
        window = facts(state, "within_return_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs) for f in window], [(False, (STANDARD_RETURN,))])

    def test_no_database_tool_or_gateway_is_touched(self):
        record = scenario_record(MULTI_PACKAGE)
        expected = derive_evidence_state(record).canonical_json()

        def refuse(*args, **kwargs):
            raise AssertionError("enrichment reached a database, tool or gateway")

        with mock.patch.object(sqlite3, "connect", refuse), \
                mock.patch.object(V2CaseRuntime, "from_case", refuse), \
                mock.patch.object(FaultInjectingGateway, "execute", refuse), \
                mock.patch("aftersales.executor.execute_tool", refuse), \
                mock.patch("eval_v2.runtime.execute_tool", refuse), \
                mock.patch("aftersales.registry.ToolRegistry.get", refuse, create=True):
            self.assertEqual(derive_evidence_state(record).canonical_json(), expected)


# --------------------------------------------------------------------------
# Derivation families
# --------------------------------------------------------------------------


class DerivationFamilyTests(unittest.TestCase):
    def test_a01_window_fact_from_policy_order_and_logistics(self):
        state = scenario_state(A01)
        window = facts(state, "within_return_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs) for f in window], [(True, (PROMO,))])
        self.assertEqual(window[0].details["days_since_delivery"], 10)
        self.assertEqual(window[0].details["window_days"], 15)
        self.assertEqual([f.value for f in facts(state, "days_since_delivery")], [10])
        self.assertEqual([f.value for f in facts(state, "business_state_conflict")], [False])
        # 贴身衣物 was not among the search's selected categories: no rule, no fact.
        self.assertEqual(facts(state, subject="order_item:OI-1001-2"), [])
        # The same search also returned the apparel exchange rule for 服装.
        exchange = facts(state, "within_exchange_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs) for f in exchange],
                         [(True, (APPAREL_EXCHANGE,))])
        item = records_of(state, "item_window_eligibility")
        self.assertEqual([(r.subject, r.fact_key, r.status, r.policy_refs,
                           r.input_observation_ids) for r in item],
                         [("order_item:OI-1001-1", "within_exchange_window", "produced",
                           (APPAREL_EXCHANGE,), ("turn:1:tool:2", "turn:1:tool:3")),
                          ("order_item:OI-1001-1", "within_return_window", "produced",
                           (PROMO,), ("turn:1:tool:2", "turn:1:tool:3"))])

    def test_a03_outside_window(self):
        state = scenario_state(A03)
        window = facts(state, "within_return_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs) for f in window], [(False, (STANDARD_RETURN,))])
        self.assertEqual(window[0].details["days_since_delivery"], 35)

    def test_a06_inventory_zero(self):
        state = scenario_state(A06)
        self.assertEqual([(f.subject, f.value) for f in state.derived_evidence],
                         [("inventory:SKU-TSHIRT-L", False)])

    def test_a07_business_state_conflict(self):
        state = scenario_state(A07)
        conflict = facts(state, "business_state_conflict", "order:ORD-1002")
        self.assertEqual([f.value for f in conflict], [True])
        self.assertEqual(conflict[0].details["fired_rules"], ["order_delivered_package_in_transit"])

    def test_conflict_pairs_by_call_arguments_only(self):
        record = run(synthetic_case(), get_order("ORD-1001"), get_logistics("ORD-1002"))
        state = derive_evidence_state(record)
        self.assertEqual(records_of(state, "business_state_conflict"), [])

    def test_empty_logistics_is_a_diagnostic_not_a_fact(self):
        state = scenario_state(EMPTY_LOGISTICS)
        self.assertEqual(facts(state, "business_state_conflict"), [])
        self.assertEqual([(r.family, r.status, r.code) for r in state.derivation_records], [
            ("business_state_conflict", "not_derivable", "logistics_evidence_incomplete")])

    def test_multi_package_ambiguity_is_preserved(self):
        state = scenario_state(MULTI_PACKAGE)
        self.assertEqual(facts(state, "within_return_window"), [])
        self.assertEqual(facts(state, "within_exchange_window"), [])
        item = records_of(state, "item_window_eligibility")
        self.assertEqual([(r.subject, r.fact_key, r.status, r.code, r.policy_refs)
                          for r in item],
                         [("order_item:OI-1004-1", "within_exchange_window", "not_derivable",
                           "item_package_link_ambiguous", (APPAREL_EXCHANGE,)),
                          ("order_item:OI-1004-1", "within_return_window", "not_derivable",
                           "item_package_link_ambiguous", (PROMO,))])
        days = records_of(state, "days_since_delivery")
        self.assertEqual([(r.subject, r.status, r.code) for r in days],
                         [("logistics:SF1004A", "produced", None),
                          ("logistics:YT1004B", "not_derivable", "start_event_absent")])
        # Days of the delivered package exist, but no window fact is built from them.
        self.assertEqual([f.value for f in facts(state, "days_since_delivery")], [3])

    def test_pure_policy_collects_evidence_and_fabricates_nothing(self):
        state = scenario_state(PURE_POLICY)
        self.assertEqual(state.derived_evidence, ())
        self.assertEqual(state.derivation_records, ())
        self.assertTrue(state.evidence_items)
        self.assertTrue(all(item.producer == "search_after_sales_policy"
                            and item.evidence.source_type is SourceType.DOCUMENT
                            for item in state.evidence_items))

    def test_days_need_an_observed_window_rule(self):
        state = derive_evidence_state(run(synthetic_case(), get_order(), get_logistics()))
        self.assertEqual(records_of(state, "days_since_delivery"), [])
        self.assertEqual(records_of(state, "item_window_eligibility"), [])
        self.assertEqual([f.fact_key for f in state.derived_evidence], ["business_state_conflict"])

    def test_not_derivable_records_carry_no_text(self):
        state = scenario_state(MULTI_PACKAGE)
        payload = json.dumps([r.to_dict() for r in state.derivation_records], ensure_ascii=False)
        for word in (USER_TEXT, "CUST-", "SELECT", "Error", "陶瓷", "服装"):
            self.assertNotIn(word, payload)
        for record in state.derivation_records:
            self.assertEqual(set(record.to_dict()), {
                "family", "fact_key", "subject", "status", "code", "input_observation_ids",
                "policy_refs", "evidence_ref"})

    def test_value_error_still_propagates(self):
        record = scenario_record(A06)
        result = copy.deepcopy(record.observations[0].result)
        result.evidence[0].metadata["value"] = -1
        with self.assertRaises(ValueError):
            derive_evidence_state(replace_result(record, 0, result))


# --------------------------------------------------------------------------
# Policy provenance
# --------------------------------------------------------------------------


class PolicyProvenanceTests(unittest.TestCase):
    def test_only_observed_policies_drive_derivation(self):
        state = scenario_state(EXCHANGE_ONLY)
        observed = {item.evidence.metadata["policy_ref"] for item in state.evidence_items
                    if item.producer == "search_after_sales_policy"}
        self.assertNotIn(PROMO, observed)
        self.assertEqual(facts(state, "within_return_window"), [])
        exchange = facts(state, "within_exchange_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs) for f in exchange], [(True, (APPAREL_EXCHANGE,))])
        for fact in state.derived_evidence:
            self.assertLessEqual(set(fact.policy_refs), observed)
        for record in state.derivation_records:
            self.assertLessEqual(set(record.policy_refs), observed)

    def test_removed_structured_field_fails_loudly(self):
        record = scenario_record(A01)
        edited = edited_policy_result(record, 0, lambda items: [
            item for item in items
            if not (item.metadata["policy_ref"] == PROMO
                    and item.metadata["field"] == "window_days")])
        with self.assertRaises(EvalEvidenceError):
            derive_evidence_state(edited)

    def test_changed_structured_value_fails_loudly(self):
        record = scenario_record(A01)

        def edit(items):
            for item in items:
                if item.metadata["field"] == "window_days":
                    item.metadata["value"] = 400
            return items

        with self.assertRaises(EvalEvidenceError):
            derive_evidence_state(edited_policy_result(record, 0, edit))

    def test_forged_policy_ref_fails_loudly(self):
        record = scenario_record(A01)

        def edit(items):
            for item in items:
                item.metadata["policy_ref"] = "policy:invented-return@1#build-0001"
            return items

        with self.assertRaises(EvalEvidenceError):
            derive_evidence_state(edited_policy_result(record, 0, edit))

    def test_missing_policy_ref_fails_loudly(self):
        record = scenario_record(PURE_POLICY)

        def edit(items):
            del items[0].metadata["policy_ref"]
            return items

        with self.assertRaises(EvalEvidenceError):
            derive_evidence_state(edited_policy_result(record, 0, edit))

    def test_selected_categories_missing_or_contradictory_fails_loudly(self):
        record = scenario_record(A01)

        def missing(items):
            for item in items:
                if item.metadata["policy_ref"] == PROMO:
                    del item.metadata["selected_categories"]
            return items

        def contradictory(items):
            first = next(item for item in items if item.metadata["policy_ref"] == PROMO)
            first.metadata["selected_categories"] = ["服装", "家居"]
            return items

        for edit in (missing, contradictory):
            with self.subTest(edit=edit.__name__):
                with self.assertRaises(EvalEvidenceError):
                    derive_evidence_state(edited_policy_result(record, 0, edit))

    def test_catalog_is_read_once_and_only_for_observed_refs(self):
        record = scenario_record(A01)
        real = policy_catalog.PublishedPolicyCatalog.snapshot
        looked_up = []

        def snapshot(catalog):
            result = real(catalog)
            original = result.lookup

            def lookup(ref):
                looked_up.append(ref)
                return original(ref)

            object.__setattr__(result, "lookup", lookup)
            return result

        with mock.patch.object(policy_catalog.PublishedPolicyCatalog, "snapshot", snapshot):
            derive_evidence_state(record)
        observed = {item.metadata["policy_ref"] for item in record.observations[0].result.evidence}
        self.assertIn(PROMO, observed)
        self.assertEqual(set(looked_up), observed)


# --------------------------------------------------------------------------
# Explicit category winner > observed general (None) selection > nothing
# --------------------------------------------------------------------------


def recategorized(**categories):
    """An order_items overlay setting item categories, e.g. OI_1001_1="家居"."""
    return {"order_items": {key.replace("_", "-"): {"op": "update", "set": {"category": value}}
                            for key, value in categories.items()}}


def item_window_records(state, item):
    return [(r.fact_key, r.status, r.code, r.policy_refs)
            for r in records_of(state, "item_window_eligibility")
            if r.subject == "order_item:" + item]


def observed_selection(state):
    """policy_ref -> selected_categories, as the search observations reported them."""
    return {item.evidence.metadata["policy_ref"]: item.evidence.metadata["selected_categories"]
            for item in state.evidence_items if item.producer == "search_after_sales_policy"}


def forged_policy_evidence(ref, *, as_of, selected_categories):
    """Structurally valid evidence for a real catalog rule, rendered as the adapter does."""
    snapshot = policy_catalog.PublishedPolicyCatalog().snapshot()
    record = snapshot.lookup(ref)
    versions = {doc: (version, digest) for doc, version, digest in snapshot.source_versions}
    version, digest = versions[record.source_doc]
    return policy_catalog.policy_evidence(
        record, as_of=datetime.fromisoformat(as_of), source_version=version,
        source_digest=digest, provenance=json.loads(dict(snapshot.provenance)[record.policy_id]),
        selected_categories=selected_categories)


def with_categories(ref, categories):
    def edit(items):
        for item in items:
            if item.metadata["policy_ref"] == ref:
                item.metadata["selected_categories"] = list(categories)
        return items
    return edit


GENERAL_RETURN = (search("退货"), get_order(), get_logistics())
GENERAL_EXCHANGE = (search("换货"), get_order(), get_logistics())


class GeneralSelectionFallbackTests(unittest.TestCase):
    def test_general_return_rule_applies_to_an_unscoped_category(self):
        state = scenario_state((recategorized(OI_1001_1="家居"), GENERAL_RETURN),
                               virtual_now=AFTER_PROMO)
        self.assertEqual(observed_selection(state)[STANDARD_RETURN], [None, "定制", "服装"])
        window = facts(state, "within_return_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs) for f in window],
                         [(False, (STANDARD_RETURN,))])
        self.assertEqual(window[0].details["category"], "家居")
        self.assertEqual(window[0].details["days_since_delivery"], 35)

    def test_general_exchange_rule_applies_to_unscoped_categories(self):
        state = scenario_state((recategorized(OI_1001_1="家居", OI_1001_2="数码"),
                                GENERAL_EXCHANGE))
        self.assertEqual(observed_selection(state)[STANDARD_EXCHANGE], [None, "定制"])
        for item, category in (("OI-1001-1", "家居"), ("OI-1001-2", "数码")):
            with self.subTest(category=category):
                window = facts(state, "within_exchange_window", "order_item:" + item)
                self.assertEqual([(f.value, f.policy_refs, f.details["category"])
                                  for f in window], [(True, (STANDARD_EXCHANGE,), category)])
        self.assertEqual(facts(state, "within_return_window"), [])

    def test_explicit_category_winner_overrides_the_general_selection(self):
        state = scenario_state((dict(), GENERAL_EXCHANGE))
        selection = observed_selection(state)
        self.assertEqual(selection[STANDARD_EXCHANGE], [None, "定制"])
        self.assertEqual(selection[APPAREL_EXCHANGE], ["服装"])
        # 服装: the apparel winner only - no second fact from the general rule.
        self.assertEqual(item_window_records(state, "OI-1001-1"),
                         [("within_exchange_window", "produced", None, (APPAREL_EXCHANGE,))])
        apparel = facts(state, "within_exchange_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs, f.details["window_days"]) for f in apparel],
                         [(True, (APPAREL_EXCHANGE,), 30)])
        # 贴身衣物 has no explicit winner: the observed general selection applies.
        self.assertEqual(item_window_records(state, "OI-1001-2"),
                         [("within_exchange_window", "produced", None, (STANDARD_EXCHANGE,))])

    def test_general_fallback_keeps_catalog_precedence(self):
        state = scenario_state((recategorized(OI_1001_1="家居"), GENERAL_RETURN))
        self.assertNotIn(STANDARD_RETURN, observed_selection(state))
        window = facts(state, "within_return_window", "order_item:OI-1001-1")
        self.assertEqual([(f.value, f.policy_refs, f.details["window_days"]) for f in window],
                         [(True, (PROMO,), 15)])
        for record in state.derivation_records:
            self.assertNotIn(STANDARD_RETURN, record.policy_refs)

    def test_scope_free_rule_is_not_widened_without_an_observed_general_selection(self):
        state = scenario_state((recategorized(OI_1001_1="家居"),
                                (search("退货 服装"), get_order(), get_logistics())))
        self.assertEqual(observed_selection(state)[PROMO], ["服装"])
        self.assertEqual(item_window_records(state, "OI-1001-1"), [])
        self.assertEqual(facts(state, subject="order_item:OI-1001-1"), [])
        self.assertEqual(facts(state, subject="order_item:OI-1001-2"), [])

    def test_general_fallback_still_meets_the_multi_package_gate(self):
        state = scenario_state((dict(), (search("退货"), get_order("ORD-1004"),
                                         get_logistics("ORD-1004"))))
        self.assertEqual(facts(state, "within_return_window"), [])
        for item in ("OI-1004-1", "OI-1004-2"):  # 家居, 家电: general fallback
            with self.subTest(item=item):
                self.assertEqual(item_window_records(state, item), [
                    ("within_return_window", "not_derivable", "item_package_link_ambiguous",
                     (PROMO,))])

    def test_general_selection_on_a_scoped_rule_fails_loudly(self):
        record = scenario_record((dict(), GENERAL_EXCHANGE))
        edited = edited_policy_result(record, 0, with_categories(APPAREL_EXCHANGE,
                                                                 [None, "服装"]))
        with self.assertRaises(EvalEvidenceError):
            derive_evidence_state(edited)

    def test_disagreeing_winners_for_one_category_fail_loudly(self):
        record = scenario_record((dict(), GENERAL_EXCHANGE))
        edited = edited_policy_result(record, 0, with_categories(STANDARD_EXCHANGE,
                                                                 [None, "定制", "服装"]))
        with self.assertRaises(EvalEvidenceError) as raised:
            derive_evidence_state(edited)
        self.assertIn("disagreeing", str(raised.exception))

    def test_disagreeing_general_winners_fail_loudly(self):
        record = scenario_record((dict(), GENERAL_RETURN))
        standard = forged_policy_evidence(STANDARD_RETURN, as_of=NOW, selected_categories=[None])
        # The forged rule passes provenance validation; the None target is what breaks.
        edited = edited_policy_result(record, 0, lambda items: items + list(standard))
        with self.assertRaises(EvalEvidenceError) as raised:
            derive_evidence_state(edited)
        self.assertIn("disagreeing", str(raised.exception))


# --------------------------------------------------------------------------
# Faults
# --------------------------------------------------------------------------


class FaultEnrichmentTests(unittest.TestCase):
    def test_malformed_is_a_contract_failure_never_a_tool_result(self):
        state = scenario_state(MALFORMED)
        self.assertEqual([(f.tool_name, f.kind) for f in state.contract_failures],
                         [("get_logistics", "malformed")])
        self.assertEqual([r.tool_name for r in state.tool_results],
                         ["search_after_sales_policy", "get_order"])
        self.assertFalse([r for r in state.tool_results if r.status is ToolStatus.ERROR])
        self.assertEqual(state.derived_evidence, ())
        self.assertEqual(state.derivation_records, ())

    def test_error_and_timeout_are_error_results_without_derived_facts(self):
        for scenario, name in ((A14, "error"), (A15, "timeout")):
            with self.subTest(fault=name):
                state = scenario_state(scenario)
                logistics = [r for r in state.tool_results if r.tool_name == "get_logistics"]
                self.assertEqual([r.status for r in logistics], [ToolStatus.ERROR])
                self.assertEqual(state.contract_failures, ())
                self.assertEqual(state.derived_evidence, ())
                self.assertEqual(state.derivation_records, ())


# --------------------------------------------------------------------------
# Live control state
# --------------------------------------------------------------------------


class ControlStateDerivationTests(unittest.TestCase):
    def test_same_enrichment_from_the_live_state(self):
        kwargs, calls = MULTI_PACKAGE
        policy = ScriptedPolicy(*script_of(*calls))
        record = run_case(synthetic_case(**kwargs), policy, max_steps=12)
        live = derive_from_control_state(policy.states[-1])
        final = derive_evidence_state(record).to_dict()
        self.assertIsNone(live.control_run_sha256)
        live_dict = live.to_dict()
        live_dict.pop("control_run_sha256")
        final.pop("control_run_sha256")
        self.assertEqual(live_dict, final)

    def test_reads_only_virtual_now_and_observations(self):
        tree = ast.parse(EVIDENCE_SOURCE.read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "derive_from_control_state")
        read = {node.attr for node in ast.walk(function) if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name) and node.value.id == "state"}
        self.assertEqual(read, {"virtual_now", "observations"})
        with self.assertRaises(ValueError):
            derive_from_control_state(object())


# --------------------------------------------------------------------------
# Evidence Policy bridge
# --------------------------------------------------------------------------


DELIVERED_AT = requirement("delivered_at", "logistics:SF1001", "delivered_at",
                           providers=("get_logistics",))
WITHIN_WINDOW = requirement("window", "order_item:OI-1001-1", "within_return_window",
                            providers=("get_logistics",))


class EvidencePolicyBridgeTests(unittest.TestCase):
    def test_bridge_is_the_production_policy(self):
        self.assertEqual(tuple(inspect.signature(evaluate_evidence_state).parameters),
                         ("state", "requirements"))
        state = scenario_state(A01)
        requirements = (DELIVERED_AT, WITHIN_WINDOW)
        direct = evaluate_evidence_v2(
            state.tool_results, requirements=requirements,
            freshness=FreshnessRequirement(as_of=state.as_of()),
            derived=state.derived_evidence)
        self.assertEqual(evaluate_evidence_state(state, requirements=requirements).to_dict(),
                         direct.to_dict())
        with self.assertRaises(ValueError):
            evaluate_evidence_state(state, requirements=[DELIVERED_AT])

    def test_business_current_evidence_is_sufficient(self):
        decision = evaluate_evidence_state(scenario_state(A01), requirements=(DELIVERED_AT,))
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)

    def test_derived_evidence_is_sufficient(self):
        state = scenario_state(A01)
        decision = evaluate_evidence_state(state, requirements=(WITHIN_WINDOW,))
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        support = decision.requirements[0].supporting_refs
        self.assertEqual(support, tuple(evidence_ref(f) for f in
                                        facts(state, "within_return_window")))

    def test_tool_error_blocks_with_tool_error(self):
        for scenario in (A14, A15):
            decision = evaluate_evidence_state(scenario_state(scenario),
                                               requirements=(DELIVERED_AT,))
            self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
            self.assertEqual(decision.requirements[0].cause, "tool_error")

    def test_empty_and_error_stay_distinct(self):
        missing = requirement("delivered_at", "logistics:SF1003", "delivered_at",
                              providers=("get_logistics",))
        empty = evaluate_evidence_state(scenario_state(EMPTY_LOGISTICS), requirements=(missing,))
        error = evaluate_evidence_state(scenario_state(A14), requirements=(missing,))
        self.assertEqual(empty.requirements[0].cause, "empty_tool_result")
        self.assertEqual(error.requirements[0].cause, "tool_error")

    def test_stale_or_wrong_instant_is_blocked(self):
        state = scenario_state(A01)
        later = (state.as_of() + timedelta(hours=1)).isoformat()
        moved = dataclasses.replace(state, virtual_now=later)
        for item in (DELIVERED_AT, WITHIN_WINDOW):
            with self.subTest(requirement=item.requirement_id):
                decision = evaluate_evidence_state(moved, requirements=(item,))
                self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
                self.assertEqual(decision.requirements[0].cause,
                                 "freshness_requirement_unsatisfied")

    def test_usable_business_state_conflict_blocks(self):
        status = requirement("status", "order:ORD-1002", "status", providers=("get_order",))
        decision = evaluate_evidence_state(scenario_state(A07), requirements=(status,))
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertIn("unresolved_business_state_conflict", decision.reason_codes)
        self.assertTrue(decision.requirements[0].satisfied)

    def test_unrelated_error_does_not_poison(self):
        kwargs, calls = A01
        record = run(synthetic_case(faults=[fault("get_inventory", "error")]),
                     *calls, get_inventory("SKU-TSHIRT-M"))
        decision = evaluate_evidence_state(derive_evidence_state(record),
                                           requirements=(DELIVERED_AT, WITHIN_WINDOW))
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertIn(("get_inventory", ToolStatus.ERROR),
                      [(o.tool, o.status) for o in decision.tool_outcomes])

    def test_contract_failure_is_not_a_tool_result(self):
        state = scenario_state(MALFORMED)
        decision = evaluate_evidence_state(state, requirements=(DELIVERED_AT,))
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, "missing_evidence")
        self.assertNotIn("get_logistics", [o.tool for o in decision.tool_outcomes])

    def test_policy_requirement_is_met_by_observed_policy_evidence(self):
        policy = requirement("promo", "policy:november-promo-return", "window_days",
                             scope=ClaimScope.POLICY, providers=("search_after_sales_policy",))
        decision = evaluate_evidence_state(scenario_state(PURE_POLICY), requirements=(policy,))
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)


# --------------------------------------------------------------------------
# Static boundaries
# --------------------------------------------------------------------------


def imported_modules(tree):
    modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    modules += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names]
    return modules


NEW_MODULES = {name: ROOT / "eval_v2" / (name + ".py") for name in ("evidence", "scoring",
                                                                      "dataset")}
FORBIDDEN_IMPORT_ROOTS = {"llm_provider", "requests", "openai", "ollama", "agent", "httpx",
                          "urllib", "socket", "chat_orchestration", "diagnostic_eval",
                          "eval_env", "agent_trace", "tools", "uuid", "random", "time",
                          "secrets", "threading", "concurrent", "multiprocessing", "asyncio",
                          "sqlite3", "rag", "wiki_runtime", "api", "app"}


class StaticBoundaryTests(unittest.TestCase):
    def test_new_modules_have_no_llm_network_clock_or_randomness(self):
        for name, path in NEW_MODULES.items():
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            with self.subTest(module=name):
                self.assertEqual(clock_violations(source), [])
                modules = imported_modules(tree)
                self.assertFalse({m.split(".")[0] for m in modules} & FORBIDDEN_IMPORT_ROOTS)
                self.assertNotIn("orchestration.planner", modules)
                for marker in ("datetime.now", "utcnow", "date.today", "time.time",
                               "perf_counter", "uuid4", "shuffle"):
                    self.assertNotIn(marker, source)

    def test_evidence_module_never_names_a_label(self):
        source = EVIDENCE_SOURCE.read_text(encoding="utf-8")
        for word in ("expected_", "archetype"):
            self.assertNotIn(word, source)
        tree = ast.parse(source)
        self.assertNotIn("case_id", {node.attr for node in ast.walk(tree)
                                     if isinstance(node, ast.Attribute)})

    def test_evidence_module_never_reaches_a_database_tool_or_gateway(self):
        tree = ast.parse(EVIDENCE_SOURCE.read_text(encoding="utf-8"))
        names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 for alias in node.names}
        for name in ("execute_tool", "execute_observation", "FaultInjectingGateway",
                     "V2CaseRuntime", "build_runtime_registry", "run_case"):
            self.assertNotIn(name, names)


if __name__ == "__main__":
    unittest.main()
