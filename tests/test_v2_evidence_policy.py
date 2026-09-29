"""V2 Evidence Policy: freshness, scope isolation, failures, conflicts (Stage 4.2)."""

import ast
import json
import unittest
from datetime import timedelta
from pathlib import Path

from aftersales.clock import FixedClock
from aftersales.demo import DEMO_VIRTUAL_NOW
from aftersales.derived import (
    derive_business_state_conflict,
    derive_inventory_available,
    derive_window_eligibility,
)
from aftersales.executor import execute_tool
from aftersales.registry import build_runtime_registry
from orchestration.contracts import (
    DerivedEvidence,
    Evidence,
    FreshnessContract,
    SourceType,
    ToolResult,
    ToolStatus,
    evidence_ref,
)
from orchestration.evidence_policy_v2 import (
    BLOCKING_REASON_CODES,
    REASON_BUSINESS_STATE_CONFLICT,
    REASON_CODE_ORDER,
    REASON_CONFLICTING_OBSERVATIONS,
    REASON_DERIVED_INPUT_UNAVAILABLE,
    REASON_EMPTY_TOOL_RESULT,
    REASON_EVIDENCE_SUFFICIENT,
    REASON_FRESHNESS_UNSATISFIED,
    REASON_FRESHNESS_UNSUPPORTED,
    REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE,
    REASON_INSUFFICIENT_POLICY_EVIDENCE,
    REASON_MISSING_EVIDENCE,
    REASON_OUT_OF_SCOPE_EVIDENCE,
    REASON_TOOL_ERROR,
    ClaimScope,
    EvidenceOutcome,
    EvidenceRequirement,
    FreshnessRequirement,
    assess_business_freshness,
    evaluate_evidence_v2,
)

from tests.v2_support import (
    ORDER_A_DELIVERED,
    SKU_ZERO,
    at,
    business_evidence,
    make_context,
    memory_connection,
    window_policy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "orchestration" / "evidence_policy_v2.py"

NOW = DEMO_VIRTUAL_NOW
NOW_ISO = NOW.isoformat()
TWO_DAYS_AGO = (NOW - timedelta(days=2)).isoformat()
FRESH = FreshnessRequirement(as_of=NOW)
OPERATIONAL = ClaimScope.CURRENT_OPERATIONAL_STATE
POLICY = ClaimScope.POLICY


def ok(tool, *evidence):
    return ToolResult(tool_name=tool, status=ToolStatus.OK, evidence=tuple(evidence))


def empty(tool):
    return ToolResult(tool_name=tool, status=ToolStatus.EMPTY)


def error(tool):
    return ToolResult(tool_name=tool, status=ToolStatus.ERROR, error_code="tool_error",
                      error_message=tool + " failed with OperationalError")


def need(requirement_id, subject, field, *, scope=OPERATIONAL, providers=(), source_types=None):
    return EvidenceRequirement(
        requirement_id=requirement_id, scope=scope, subject=subject, field=field,
        providers=providers, source_types=source_types,
    )


def policy_doc(field="window_days", subject="policy:P-RETURN-7D", source_type=SourceType.DOCUMENT):
    return Evidence(
        content="自签收次日起 7 个自然日内可申请无理由退货。",
        source_type=source_type,
        source="aftersales_rules.md",
        locator=subject + "#" + field,
        version="1",
        authority=80 if source_type is SourceType.DOCUMENT else 60,
    )


def inventory(quantity=20, **kwargs):
    kwargs.setdefault("observed_at", NOW_ISO)
    return business_evidence("inventory", "SKU-X", "available_qty", quantity, **kwargs)


INVENTORY_NEED = need("stock", "inventory:SKU-X", "available_qty", providers=("get_inventory",))


class FreshnessTests(unittest.TestCase):
    def test_authoritative_online_with_old_record_updated_at_is_current(self):
        """Regression (design §6, D4): the record last changed two days ago, but
        it was read directly from the authoritative source at virtual_now."""
        item = inventory(record_updated_at=TWO_DAYS_AGO)
        self.assertIs(item.freshness_contract, FreshnessContract.AUTHORITATIVE_ONLINE)
        self.assertIsNone(assess_business_freshness(item, FRESH))
        decision = evaluate_evidence_v2(
            (ok("get_inventory", item),), requirements=(INVENTORY_NEED,), freshness=FRESH
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(decision.reason_codes, (REASON_EVIDENCE_SUFFICIENT,))
        self.assertEqual(decision.usable_evidence, (item,))
        self.assertEqual(decision.requirements[0].supporting_refs, (evidence_ref(item),))

    def test_the_same_holds_for_a_real_tool_read(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        context = make_context(connection)
        result = execute_tool(build_runtime_registry(), context, "get_inventory", {"sku": SKU_ZERO})
        (item,) = result.evidence
        # The seed record is older than the read by more than a day.
        self.assertGreater(NOW.isoformat(), item.record_updated_at)
        decision = evaluate_evidence_v2(
            (result,),
            requirements=(need("stock", "inventory:" + SKU_ZERO, "available_qty"),),
            freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)

    def test_record_updated_at_cannot_prove_freshness(self):
        # A read taken two days ago, of a record that changed a second before
        # virtual_now, is still a two-day-old read.
        item = inventory(observed_at=TWO_DAYS_AGO, record_updated_at=TWO_DAYS_AGO)
        self.assertEqual(assess_business_freshness(item, FRESH), REASON_FRESHNESS_UNSATISFIED)
        item.record_updated_at = (NOW - timedelta(seconds=1)).isoformat()
        self.assertEqual(assess_business_freshness(item, FRESH), REASON_FRESHNESS_UNSATISFIED)
        # And a "fresh" record_updated_at does not rescue a missing observed_at.
        item.observed_at = None
        item.record_updated_at = NOW_ISO
        self.assertEqual(assess_business_freshness(item, FRESH), REASON_FRESHNESS_UNSUPPORTED)

    def test_record_updated_at_is_never_read(self):
        item = inventory(record_updated_at=TWO_DAYS_AGO)
        verdicts = set()
        for value in (None, TWO_DAYS_AGO, NOW_ISO, "2019-01-01T00:00:00+08:00"):
            item.record_updated_at = value
            verdicts.add(assess_business_freshness(item, FRESH))
        self.assertEqual(verdicts, {None})

    def test_observed_at_is_required(self):
        item = inventory()
        item.observed_at = None
        self.assertEqual(assess_business_freshness(item, FRESH), REASON_FRESHNESS_UNSUPPORTED)
        decision = evaluate_evidence_v2(
            (ok("get_inventory", item),), requirements=(INVENTORY_NEED,), freshness=FRESH
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertIn(REASON_FRESHNESS_UNSUPPORTED, decision.reason_codes)
        self.assertEqual(decision.requirements[0].cause, REASON_FRESHNESS_UNSUPPORTED)
        self.assertEqual(decision.usable_evidence, ())

    def test_max_age_is_the_callers_sla(self):
        item = inventory(observed_at=(NOW - timedelta(minutes=5)).isoformat())
        self.assertEqual(assess_business_freshness(item, FRESH), REASON_FRESHNESS_UNSATISFIED)
        self.assertIsNone(assess_business_freshness(
            item, FreshnessRequirement(as_of=NOW, max_age=timedelta(minutes=5))))
        self.assertEqual(
            assess_business_freshness(
                item, FreshnessRequirement(as_of=NOW, max_age=timedelta(minutes=4))),
            REASON_FRESHNESS_UNSATISFIED,
        )

    def test_future_observation_is_never_fresh(self):
        """A: as_of 10:00, observed_at 10:05. A negative age must not pass max_age."""
        item = inventory(observed_at=(NOW + timedelta(minutes=5)).isoformat(),
                         record_updated_at=TWO_DAYS_AGO)
        for max_age in (None, timedelta(0), timedelta(minutes=10), timedelta(days=365)):
            with self.subTest(max_age=max_age):
                freshness = FreshnessRequirement(as_of=NOW, max_age=max_age)
                self.assertEqual(assess_business_freshness(item, freshness),
                                 REASON_FRESHNESS_UNSATISFIED)
                decision = evaluate_evidence_v2(
                    (ok("get_inventory", item),), requirements=(INVENTORY_NEED,),
                    freshness=freshness,
                )
                self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
                self.assertEqual(decision.requirements[0].cause, REASON_FRESHNESS_UNSATISFIED)
        # The same instant written in UTC is still five minutes in the future.
        item.observed_at = "2026-11-15T02:05:00+00:00"
        self.assertEqual(
            assess_business_freshness(item, FreshnessRequirement(as_of=NOW, max_age=timedelta(hours=1))),
            REASON_FRESHNESS_UNSATISFIED,
        )

    def test_future_snapshot_is_never_fresh(self):
        """B: snapshot.source_as_of > as_of."""
        snapshot = inventory(freshness_contract=FreshnessContract.SNAPSHOT,
                             source_as_of=(NOW + timedelta(minutes=1)).isoformat())
        for max_age in (timedelta(0), timedelta(minutes=10), timedelta(days=365)):
            with self.subTest(max_age=max_age):
                self.assertEqual(
                    assess_business_freshness(
                        snapshot, FreshnessRequirement(as_of=NOW, max_age=max_age)),
                    REASON_FRESHNESS_UNSATISFIED,
                )

    def test_negative_max_age_fails_loudly(self):
        """C."""
        for bad in (-timedelta(seconds=1), -timedelta(days=1)):
            with self.subTest(max_age=bad):
                with self.assertRaises(ValueError):
                    FreshnessRequirement(as_of=NOW, max_age=bad)
        # A frozen requirement mutated afterwards is caught before use as well.
        freshness = FreshnessRequirement(as_of=NOW, max_age=timedelta(minutes=5))
        object.__setattr__(freshness, "max_age", -timedelta(minutes=5))
        with self.assertRaises(ValueError):
            evaluate_evidence_v2((ok("get_inventory", inventory()),),
                                 requirements=(INVENTORY_NEED,), freshness=freshness)

    def test_old_record_updated_at_with_current_read_stays_fresh_under_any_sla(self):
        """D."""
        item = inventory(record_updated_at=TWO_DAYS_AGO)
        for max_age in (None, timedelta(0), timedelta(minutes=1)):
            with self.subTest(max_age=max_age):
                self.assertIsNone(assess_business_freshness(
                    item, FreshnessRequirement(as_of=NOW, max_age=max_age)))

    def test_snapshot_requires_source_as_of(self):
        snapshot = inventory(
            freshness_contract=FreshnessContract.SNAPSHOT,
            source_as_of=(NOW - timedelta(minutes=1)).isoformat(),
            record_updated_at=NOW_ISO,
        )
        sla = FreshnessRequirement(as_of=NOW, max_age=timedelta(minutes=10))
        self.assertIsNone(assess_business_freshness(snapshot, sla))
        snapshot.source_as_of = None
        # record_updated_at == as_of does not stand in for source_as_of.
        self.assertEqual(assess_business_freshness(snapshot, sla), REASON_FRESHNESS_UNSUPPORTED)

    def test_snapshot_is_judged_by_source_as_of_not_observed_at(self):
        snapshot = inventory(
            freshness_contract=FreshnessContract.SNAPSHOT,
            source_as_of=(NOW - timedelta(hours=3)).isoformat(),
        )
        sla = FreshnessRequirement(as_of=NOW, max_age=timedelta(hours=1))
        # observed_at is "now", but the snapshot itself is three hours old.
        self.assertEqual(snapshot.observed_at, NOW_ISO)
        self.assertEqual(assess_business_freshness(snapshot, sla), REASON_FRESHNESS_UNSATISFIED)

    def test_snapshot_without_an_sla_does_not_pass(self):
        # Even a snapshot as of this very instant: with no SLA there is nothing
        # to judge it against, and no TTL is invented.
        snapshot = inventory(freshness_contract=FreshnessContract.SNAPSHOT, source_as_of=NOW_ISO)
        self.assertEqual(assess_business_freshness(snapshot, FRESH), REASON_FRESHNESS_UNSUPPORTED)
        decision = evaluate_evidence_v2(
            (ok("get_inventory", snapshot),), requirements=(INVENTORY_NEED,), freshness=FRESH
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_FRESHNESS_UNSUPPORTED)

    def test_business_evidence_without_a_contract_cannot_prove_currency(self):
        plain = Evidence(content="库存 20", source_type=SourceType.BUSINESS, source="db",
                         locator="inventory:SKU-X#available_qty", observed_at=NOW_ISO,
                         authority=100, metadata={"value": 20})
        self.assertEqual(assess_business_freshness(plain, FRESH), REASON_FRESHNESS_UNSUPPORTED)

    def test_freshness_requirement_contract(self):
        from datetime import datetime

        for bad in (dict(as_of=datetime(2026, 11, 15)), dict(as_of=NOW_ISO),
                    dict(as_of=NOW, max_age=-timedelta(seconds=1)), dict(as_of=NOW, max_age=5)):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(ValueError):
                    FreshnessRequirement(**bad)


class FailureKindTests(unittest.TestCase):
    """missing / empty / error stay distinct, per requirement."""

    def cause(self, results):
        decision = evaluate_evidence_v2(results, requirements=(INVENTORY_NEED,), freshness=FRESH)
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertIn(REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE, decision.reason_codes)
        return decision.requirements[0].cause, decision

    def test_missing(self):
        cause, decision = self.cause(())
        self.assertEqual(cause, REASON_MISSING_EVIDENCE)
        self.assertEqual(decision.tool_outcomes, ())

    def test_empty(self):
        cause, decision = self.cause((empty("get_inventory"),))
        self.assertEqual(cause, REASON_EMPTY_TOOL_RESULT)
        self.assertNotIn(REASON_MISSING_EVIDENCE, decision.reason_codes)
        self.assertNotIn(REASON_TOOL_ERROR, decision.reason_codes)

    def test_error(self):
        cause, decision = self.cause((error("get_inventory"),))
        self.assertEqual(cause, REASON_TOOL_ERROR)
        self.assertNotIn(REASON_EMPTY_TOOL_RESULT, decision.reason_codes)
        rendered = json.dumps(decision.to_dict(), ensure_ascii=False)
        self.assertNotIn("OperationalError", rendered)

    def test_error_outranks_empty_among_providers(self):
        cause, _ = self.cause((empty("get_inventory"), error("get_inventory")))
        self.assertEqual(cause, REASON_TOOL_ERROR)

    def test_an_unrelated_tool_does_not_explain_absence(self):
        cause, _ = self.cause((error("get_order"),))
        self.assertEqual(cause, REASON_MISSING_EVIDENCE)

    def test_another_ok_call_can_still_satisfy(self):
        decision = evaluate_evidence_v2(
            (error("get_inventory"), ok("get_inventory", inventory())),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(
            [(o.tool, o.status) for o in decision.tool_outcomes],
            [("get_inventory", ToolStatus.ERROR), ("get_inventory", ToolStatus.OK)],
        )

    def test_a_different_record_is_still_missing(self):
        other = business_evidence("inventory", "SKU-Y", "available_qty", 3)
        cause, _ = self.cause((ok("get_inventory", other),))
        self.assertEqual(cause, REASON_MISSING_EVIDENCE)


class RequirementDrivenTests(unittest.TestCase):
    """Extra evidence or tool results never poison a supported decision."""

    POLICY_NEED = need("rule", "policy:P-RETURN-7D", "window_days", scope=POLICY,
                       providers=("search_after_sales_policy",))

    def assert_sufficient(self, decision):
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(decision.reason_codes, (REASON_EVIDENCE_SUFFICIENT,))
        self.assertTrue(all(outcome.satisfied for outcome in decision.requirements))

    def test_1_operational_support_plus_unrelated_policy_evidence(self):
        stock = inventory()
        # Speaks to the very same fact, but is not operational evidence.
        doc_on_stock = policy_doc(field="available_qty", subject="inventory:SKU-X")
        decision = evaluate_evidence_v2(
            (ok("get_inventory", stock),
             ok("search_after_sales_policy", doc_on_stock, policy_doc())),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)
        (outcome,) = decision.requirements
        self.assertEqual(outcome.supporting_refs, (evidence_ref(stock),))
        self.assertEqual(outcome.out_of_scope_refs, (evidence_ref(doc_on_stock),))

    def test_2_policy_support_plus_unrelated_business_evidence(self):
        business_on_rule = business_evidence("policy", "P-RETURN-7D", "window_days", 30)
        decision = evaluate_evidence_v2(
            (ok("search_after_sales_policy", policy_doc()),
             ok("get_order", business_on_rule, inventory())),
            requirements=(self.POLICY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)
        self.assertEqual(decision.requirements[0].out_of_scope_refs,
                         (evidence_ref(business_on_rule),))

    def test_3_supported_inventory_plus_unrelated_error(self):
        decision = evaluate_evidence_v2(
            (ok("get_inventory", inventory()), error("get_after_sales_case"), error("get_order")),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)
        # Kept as diagnostics.
        self.assertEqual(
            [(o.tool, o.status) for o in decision.tool_outcomes],
            [("get_inventory", ToolStatus.OK), ("get_after_sales_case", ToolStatus.ERROR),
             ("get_order", ToolStatus.ERROR)],
        )

    def test_3b_provider_error_on_one_call_but_support_from_another(self):
        decision = evaluate_evidence_v2(
            (error("get_inventory"), ok("get_inventory", inventory())),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)

    def test_4_required_inventory_with_only_an_error(self):
        decision = evaluate_evidence_v2(
            (error("get_inventory"),), requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.reason_codes,
                         (REASON_TOOL_ERROR, REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE))

    def test_5_required_policy_with_only_out_of_scope_business_evidence(self):
        decision = evaluate_evidence_v2(
            (ok("get_order", business_evidence("policy", "P-RETURN-7D", "window_days", 30)),),
            requirements=(self.POLICY_NEED,), freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.reason_codes,
                         (REASON_OUT_OF_SCOPE_EVIDENCE, REASON_INSUFFICIENT_POLICY_EVIDENCE))

    def test_6_supported_plus_unrelated_empty(self):
        decision = evaluate_evidence_v2(
            (ok("get_inventory", inventory()), empty("get_after_sales_case"),
             empty("search_after_sales_policy")),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)

    def test_unrelated_stale_evidence_does_not_block(self):
        stale = business_evidence("order", "ORD-1", "status", "已签收", observed_at=TWO_DAYS_AGO)
        decision = evaluate_evidence_v2(
            (ok("get_inventory", inventory()), ok("get_order", stale)),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)
        self.assertEqual([(e.ref, e.cause) for e in decision.excluded_evidence],
                         [(evidence_ref(stale), REASON_FRESHNESS_UNSATISFIED)])

    def test_unrelated_disagreement_is_reported_but_does_not_block(self):
        first = business_evidence("order", "ORD-1", "status", "已发货", state_version=3)
        second = business_evidence("order", "ORD-1", "status", "已签收", state_version=4)
        decision = evaluate_evidence_v2(
            (ok("get_inventory", inventory()), ok("get_order", first), ok("get_order", second)),
            requirements=(INVENTORY_NEED,), freshness=FRESH,
        )
        self.assert_sufficient(decision)
        (report,) = decision.conflicts
        self.assertEqual((report.subject, report.field), ("order:ORD-1", "status"))

    def test_outcome_follows_requirements_not_reason_membership(self):
        # Every decision's outcome agrees with its requirement outcomes (plus
        # a business-state conflict), across a mix of scenarios.
        scenarios = (
            (ok("get_inventory", inventory()), error("get_order")),
            (error("get_inventory"),),
            (empty("get_inventory"), ok("search_after_sales_policy", policy_doc())),
            (),
        )
        for results in scenarios:
            with self.subTest(results=[r.tool_name + ":" + r.status.value for r in results]):
                decision = evaluate_evidence_v2(results, requirements=(INVENTORY_NEED,),
                                                freshness=FRESH)
                unsatisfied = any(not o.satisfied for o in decision.requirements)
                self.assertEqual(decision.outcome is EvidenceOutcome.BLOCKED, unsatisfied)


class ScopeIsolationTests(unittest.TestCase):
    def test_business_state_requires_business_or_derived_evidence(self):
        # A document "stating" the order's status is not current state.
        doc = policy_doc(field="status", subject="order:ORD-1001")
        decision = evaluate_evidence_v2(
            (ok("search_after_sales_policy", doc),),
            requirements=(need("status", "order:ORD-1001", "status"),),
            freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_OUT_OF_SCOPE_EVIDENCE)
        self.assertIn(REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE, decision.reason_codes)

    def test_business_state_is_not_supported_by_policy_evidence_alone(self):
        decision = evaluate_evidence_v2(
            (ok("search_after_sales_policy", policy_doc()),),
            requirements=(need("policy", "policy:P-RETURN-7D", "window_days", scope=POLICY),
                          need("within", "logistics:SF1001", "within_return_window")),
            freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertTrue(decision.requirements[0].satisfied)
        self.assertFalse(decision.requirements[1].satisfied)
        self.assertIn(REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE, decision.reason_codes)
        self.assertNotIn(REASON_INSUFFICIENT_POLICY_EVIDENCE, decision.reason_codes)

    def test_policy_claim_requires_policy_evidence(self):
        # Business evidence, however authoritative, never decides what a rule means.
        business = business_evidence("policy", "P-RETURN-7D", "window_days", 30)
        decision = evaluate_evidence_v2(
            (ok("get_order", business),),
            requirements=(need("policy", "policy:P-RETURN-7D", "window_days", scope=POLICY),),
            freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_OUT_OF_SCOPE_EVIDENCE)
        self.assertIn(REASON_INSUFFICIENT_POLICY_EVIDENCE, decision.reason_codes)
        # With the rule text present, the same claim is supported.
        decision = evaluate_evidence_v2(
            (ok("get_order", business), ok("search_after_sales_policy", policy_doc())),
            requirements=(need("policy", "policy:P-RETURN-7D", "window_days", scope=POLICY),),
            freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(decision.requirements[0].supporting_refs, (evidence_ref(policy_doc()),))

    def test_wiki_and_document_both_count_for_policy(self):
        for source_type in (SourceType.WIKI, SourceType.DOCUMENT):
            with self.subTest(source_type=source_type.value):
                decision = evaluate_evidence_v2(
                    (ok("search_after_sales_policy", policy_doc(source_type=source_type)),),
                    requirements=(need("p", "policy:P-RETURN-7D", "window_days", scope=POLICY),),
                    freshness=FRESH,
                )
                self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)

    def test_source_types_may_narrow_but_not_widen(self):
        with self.assertRaises(ValueError):
            need("x", "order:1", "status", source_types=frozenset({SourceType.DOCUMENT}))
        with self.assertRaises(ValueError):
            need("x", "p:1", "f", scope=POLICY, source_types=frozenset({SourceType.BUSINESS}))
        narrowed = need("x", "inventory:SKU-X", "inventory_available",
                        source_types=frozenset({SourceType.DERIVED}))
        decision = evaluate_evidence_v2(
            (ok("get_inventory", business_evidence(
                "inventory", "SKU-X", "inventory_available", True)),),
            requirements=(narrowed,), freshness=FRESH,
        )
        self.assertEqual(decision.requirements[0].cause, REASON_OUT_OF_SCOPE_EVIDENCE)

    def test_system_evidence_is_rejected_on_the_v2_path(self):
        system = Evidence(content="库存 20", source_type=SourceType.SYSTEM, source="sys",
                          locator="inventory:SKU-X#available_qty", observed_at=NOW_ISO,
                          authority=100)
        with self.assertRaises(ValueError):
            evaluate_evidence_v2((ok("system_query", system),), requirements=(INVENTORY_NEED,),
                                 freshness=FRESH)


class DerivedEvidenceTests(unittest.TestCase):
    DELIVERED = "2026-11-05T14:30:00+08:00"

    def setUp(self):
        self.delivered_at = business_evidence(
            "logistics", "SF1001", "delivered_at", self.DELIVERED, observed_at=NOW_ISO)
        self.policy = window_policy()
        self.within = derive_window_eligibility(self.delivered_at, self.policy,
                                                clock=FixedClock(NOW))
        self.requirements = (
            need("within", "logistics:SF1001", "within_return_window"),
            need("policy", "policy:P-RETURN-7D", "window_days", scope=POLICY,
                 providers=("search_after_sales_policy",)),
        )

    def evaluate(self, results, derived, freshness=FRESH):
        return evaluate_evidence_v2(results, requirements=self.requirements,
                                    freshness=freshness, derived=derived)

    def test_derived_evidence_enters_usable_evidence(self):
        decision = self.evaluate(
            (ok("get_logistics", self.delivered_at),
             ok("search_after_sales_policy", policy_doc())),
            (self.within,),
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertIn(self.within, decision.usable_evidence)
        self.assertEqual(decision.derived_evidence, (self.within,))
        self.assertEqual(decision.requirements[0].supporting_refs, (evidence_ref(self.within),))

    def test_derived_fact_without_its_inputs_is_not_usable(self):
        decision = self.evaluate((ok("search_after_sales_policy", policy_doc()),), (self.within,))
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_DERIVED_INPUT_UNAVAILABLE)
        self.assertEqual(decision.derived_evidence, ())
        self.assertEqual(
            [(item.ref, item.cause) for item in decision.excluded_evidence],
            [(evidence_ref(self.within), REASON_DERIVED_INPUT_UNAVAILABLE)],
        )

    def test_derived_fact_over_a_stale_input_is_not_usable(self):
        stale = business_evidence("logistics", "SF1001", "delivered_at", self.DELIVERED,
                                  observed_at=TWO_DAYS_AGO)
        within = derive_window_eligibility(stale, self.policy, clock=FixedClock(NOW))
        decision = self.evaluate(
            (ok("get_logistics", stale), ok("search_after_sales_policy", policy_doc())),
            (within,),
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        excluded = {item.ref: item.cause for item in decision.excluded_evidence}
        self.assertEqual(excluded[evidence_ref(stale)], REASON_FRESHNESS_UNSATISFIED)
        self.assertEqual(excluded[evidence_ref(within)], REASON_DERIVED_INPUT_UNAVAILABLE)

    def test_derived_fact_from_another_instant_must_be_rederived(self):
        earlier = at(2026, 11, 10, 10)
        delivered_then = business_evidence("logistics", "SF1001", "delivered_at", self.DELIVERED,
                                           observed_at=earlier.isoformat())
        within_then = derive_window_eligibility(delivered_then, self.policy,
                                                clock=FixedClock(earlier))
        self.assertIs(within_then.value, True)  # true on the 10th, false on the 15th
        decision = self.evaluate(
            (ok("get_logistics", delivered_then), ok("search_after_sales_policy", policy_doc())),
            (within_then,),
            freshness=FreshnessRequirement(as_of=NOW, max_age=timedelta(days=30)),
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_FRESHNESS_UNSATISFIED)

    def test_derived_evidence_cannot_arrive_as_a_tool_result(self):
        with self.assertRaises(ValueError):
            self.evaluate((ok("get_logistics", self.delivered_at, self.within),), ())

    def test_chained_derived_inputs_resolve(self):
        # A derived fact whose input is another derived fact.
        inner = derive_inventory_available(inventory(0), clock=FixedClock(NOW))
        outer = DerivedEvidence(
            content="x", source_type=SourceType.DERIVED, source="test",
            locator="inventory:SKU-X#exchange_possible", observed_at=NOW_ISO, authority=100,
            fact_key="exchange_possible", subject="inventory:SKU-X", value=False,
            input_refs=(evidence_ref(inner),), derivation_id="test/v1",
        )
        requirement = need("possible", "inventory:SKU-X", "exchange_possible")
        decision = evaluate_evidence_v2((ok("get_inventory", inventory(0)),),
                                        requirements=(requirement,), freshness=FRESH,
                                        derived=(outer, inner))
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        decision = evaluate_evidence_v2((), requirements=(requirement,), freshness=FRESH,
                                        derived=(outer, inner))
        self.assertEqual(decision.requirements[0].cause, REASON_DERIVED_INPUT_UNAVAILABLE)


class ConflictTests(unittest.TestCase):
    def state(self, order_status, *package_statuses):
        order = business_evidence("order", "ORD-1", "status", order_status, observation_id="o")
        logistics = []
        for index, status in enumerate(package_statuses):
            for field, value in (("order_id", "ORD-1"), ("status", status)):
                logistics.append(business_evidence(
                    "logistics", "T" + str(index), field, value, observation_id="l"))
        conflict = derive_business_state_conflict(order, logistics, clock=FixedClock(NOW))
        results = (ok("get_order", order), ok("get_logistics", *logistics))
        return results, conflict, order, logistics

    REQUIREMENTS = (
        need("order_status", "order:ORD-1", "status", providers=("get_order",)),
        need("package_status", "logistics:T0", "status", providers=("get_logistics",)),
    )

    def test_unresolved_business_state_conflict_blocks(self):
        results, conflict, order, logistics = self.state("已发货", "已签收")
        self.assertIs(conflict.value, True)
        decision = evaluate_evidence_v2(results, requirements=self.REQUIREMENTS,
                                        freshness=FRESH, derived=(conflict,))
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertIn(REASON_BUSINESS_STATE_CONFLICT, decision.reason_codes)
        self.assertNotIn(REASON_EVIDENCE_SUFFICIENT, decision.reason_codes)
        # Both sides are exposed, so both can be cited.
        (report,) = decision.conflicts
        self.assertEqual(report.subject, "order:ORD-1")
        self.assertEqual(
            set(report.evidence_refs),
            {evidence_ref(conflict), evidence_ref(order)} | {evidence_ref(e) for e in logistics},
        )
        # Every individual requirement is satisfied: only the conflict blocks.
        self.assertTrue(all(outcome.satisfied for outcome in decision.requirements))

    def test_conflict_blocks_even_when_no_requirement_mentions_it(self):
        results, conflict, _, _ = self.state("已签收", "运输中")
        decision = evaluate_evidence_v2(results, requirements=self.REQUIREMENTS[:1],
                                        freshness=FRESH, derived=(conflict,))
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.reason_codes, (REASON_BUSINESS_STATE_CONFLICT,))

    def test_conflict_is_not_dropped_when_it_is_stale(self):
        results, conflict, _, _ = self.state("已发货", "已签收")
        later = FreshnessRequirement(as_of=NOW + timedelta(hours=1), max_age=timedelta(hours=2))
        decision = evaluate_evidence_v2(results, requirements=self.REQUIREMENTS,
                                        freshness=later, derived=(conflict,))
        self.assertIn(REASON_BUSINESS_STATE_CONFLICT, decision.reason_codes)

    def test_consistent_state_passes(self):
        results, conflict, _, _ = self.state("已签收", "已签收")
        self.assertIs(conflict.value, False)
        decision = evaluate_evidence_v2(results, requirements=self.REQUIREMENTS,
                                        freshness=FRESH, derived=(conflict,))
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(decision.conflicts, ())
        self.assertIn(conflict, decision.derived_evidence)

    def test_disagreeing_observations_of_one_field_block(self):
        first = business_evidence("order", "ORD-1", "status", "已发货", state_version=3)
        second = business_evidence("order", "ORD-1", "status", "已签收", state_version=4)
        decision = evaluate_evidence_v2(
            (ok("get_order", first), ok("get_order", second)),
            requirements=(self.REQUIREMENTS[0],), freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_CONFLICTING_OBSERVATIONS)
        (report,) = decision.conflicts
        self.assertEqual(set(report.evidence_refs), {evidence_ref(first), evidence_ref(second)})

    def test_repeated_identical_observations_are_not_a_conflict(self):
        first = business_evidence("order", "ORD-1", "status", "已签收", observation_id="a")
        again = business_evidence("order", "ORD-1", "status", "已签收", observation_id="b")
        decision = evaluate_evidence_v2(
            (ok("get_order", first), ok("get_order", again)),
            requirements=(self.REQUIREMENTS[0],), freshness=FRESH,
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(len(decision.usable_evidence), 1)

    def test_two_window_verdicts_under_different_rules_block(self):
        # Standard 7 days says no; a 15-day rule says yes. Which rule wins is a
        # precedence question this layer does not guess.
        delivered_at = business_evidence("logistics", "SF1001", "delivered_at",
                                         "2026-11-05T14:30:00+08:00")
        seven = derive_window_eligibility(delivered_at, window_policy(), clock=FixedClock(NOW))
        fifteen = derive_window_eligibility(
            delivered_at,
            window_policy(policy_id="P-PROMO", params={**window_policy().params, "window_days": 15}),
            clock=FixedClock(NOW),
        )
        decision = evaluate_evidence_v2(
            (ok("get_logistics", delivered_at),),
            requirements=(need("within", "logistics:SF1001", "within_return_window"),),
            freshness=FRESH, derived=(seven, fifteen),
        )
        self.assertIs(decision.outcome, EvidenceOutcome.BLOCKED)
        self.assertEqual(decision.requirements[0].cause, REASON_CONFLICTING_OBSERVATIONS)


class SufficiencyTests(unittest.TestCase):
    def test_a01_end_to_end_on_demo_data(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        registry = build_runtime_registry()
        now = at(2026, 11, 10, 10)
        context = make_context(connection, now=now)
        order = execute_tool(registry, context, "get_order", {"order_id": ORDER_A_DELIVERED},
                             observation_id="obs-order")
        logistics = execute_tool(registry, context, "get_logistics",
                                 {"order_id": ORDER_A_DELIVERED}, observation_id="obs-logistics")
        by_locator = {e.locator: e for e in order.evidence + logistics.evidence}
        within = derive_window_eligibility(
            by_locator["logistics:SF1001#delivered_at"], window_policy(scope=("服装",)),
            clock=context.clock, category=by_locator["order_item:OI-1001-1#category"],
        )
        conflict = derive_business_state_conflict(
            by_locator["order:ORD-1001#status"], logistics.evidence, clock=context.clock)
        decision = evaluate_evidence_v2(
            (order, logistics, ok("search_after_sales_policy", policy_doc())),
            requirements=(
                need("rule", "policy:P-RETURN-7D", "window_days", scope=POLICY,
                     providers=("search_after_sales_policy",)),
                need("delivered", "order:ORD-1001", "status", providers=("get_order",)),
                need("within", "order_item:OI-1001-1", "within_return_window"),
            ),
            freshness=FreshnessRequirement(as_of=now),
            derived=(within, conflict),
        )
        self.assertIs(decision.outcome, EvidenceOutcome.SUFFICIENT)
        self.assertEqual(decision.reason_codes, (REASON_EVIDENCE_SUFFICIENT,))
        self.assertIs(within.value, True)
        self.assertEqual(len(decision.derived_evidence), 2)

    def test_to_dict_is_json_and_byte_stable(self):
        def run():
            delivered_at = business_evidence("logistics", "SF1001", "delivered_at",
                                             "2026-11-05T14:30:00+08:00")
            within = derive_window_eligibility(delivered_at, window_policy(),
                                               clock=FixedClock(NOW))
            return evaluate_evidence_v2(
                (ok("get_logistics", delivered_at), empty("get_after_sales_case")),
                requirements=(need("within", "logistics:SF1001", "within_return_window"),
                              need("case", "after_sales_case:AS-1", "status",
                                   providers=("get_after_sales_case",))),
                freshness=FRESH, derived=(within,),
            ).to_dict()

        first = json.dumps(run(), sort_keys=True, ensure_ascii=False)
        second = json.dumps(run(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["reason_codes"],
                         [REASON_EMPTY_TOOL_RESULT, REASON_INSUFFICIENT_OPERATIONAL_EVIDENCE])

    def test_reason_codes_are_stable_constants(self):
        self.assertEqual(len(REASON_CODE_ORDER), len(set(REASON_CODE_ORDER)))
        self.assertEqual(BLOCKING_REASON_CODES, set(REASON_CODE_ORDER) - {REASON_EVIDENCE_SUFFICIENT})
        for code in REASON_CODE_ORDER:
            self.assertRegex(code, r"^[a-z_]+$")

    def test_reason_codes_carry_no_input_text(self):
        secret = "ORD-SECRET-9'; DROP TABLE"
        decision = evaluate_evidence_v2(
            (error("get_order"),),
            requirements=(need("status", "order:" + secret, "status", providers=("get_order",)),),
            freshness=FRESH,
        )
        for code in decision.reason_codes:
            self.assertIn(code, REASON_CODE_ORDER)
            self.assertNotIn("SECRET", code)


class InputContractTests(unittest.TestCase):
    def test_inputs_must_be_tuples_and_typed(self):
        bad_calls = (
            dict(results=[ok("get_inventory", inventory())]),
            dict(results=(ok("get_inventory", inventory()) for _ in range(1))),
            dict(requirements=[INVENTORY_NEED]),
            dict(requirements=()),
            dict(requirements=(INVENTORY_NEED, INVENTORY_NEED)),
            dict(requirements=("stock",)),
            dict(freshness=NOW),
            dict(derived=[]),
            dict(derived=(inventory(),)),
        )
        for overrides in bad_calls:
            arguments = dict(results=(ok("get_inventory", inventory()),),
                             requirements=(INVENTORY_NEED,), freshness=FRESH, derived=())
            arguments.update(overrides)
            with self.subTest(overrides=sorted(overrides)):
                with self.assertRaises(ValueError):
                    evaluate_evidence_v2(
                        arguments["results"], requirements=arguments["requirements"],
                        freshness=arguments["freshness"], derived=arguments["derived"],
                    )

    def test_mutated_results_are_caught(self):
        result = ok("get_inventory", inventory())
        result.status = ToolStatus.EMPTY
        with self.assertRaises(ValueError):
            evaluate_evidence_v2((result,), requirements=(INVENTORY_NEED,), freshness=FRESH)
        result = error("get_inventory")
        result.evidence = (inventory(),)
        with self.assertRaises(ValueError):
            evaluate_evidence_v2((result,), requirements=(INVENTORY_NEED,), freshness=FRESH)

    def test_module_is_independent_of_the_planner_and_v1_policy(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        for forbidden in ("planner", "evidence_policy", "aftersales", "sqlite3", "rag",
                          "storage", "llm_provider", "requests"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(any(name.split(".")[-1] == forbidden or
                                     name.startswith(forbidden) for name in imported))


if __name__ == "__main__":
    unittest.main()
