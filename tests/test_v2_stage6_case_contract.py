"""Stage 6.4A: the Stage 6 spec files and the stdlib case contract (design §19.1, §19.2, §19.6, §21)."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from aftersales.action_db import create_stage6_database
from aftersales.action_errors import FAILURE_CODES
from aftersales.action_outcome import COMPLETION_CLAIM_MARKERS
from aftersales.action_policy import S6_RISK_POLICY
from aftersales.actions import FORBIDDEN_ACTION_ARGUMENT_NAMES, REASON_LABELS, build_action_registry
from aftersales.guard import DENY_REASON_CODES
from aftersales.guard_snapshot import STALE_REASON_CODES
from aftersales.guard_state import GUARD_READS
from eval_v2.action_loop import STAGE6_PROTOCOL_DIAGNOSTICS
from eval_v2.stage6_runtime import stage6_contract
from eval_v2.stage6_state import BUSINESS_TABLES, harness_connection, read_state

from tests import stage6_eval_support as support
from tests.stage6_support import Stage6Database

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "eval" / "v2" / "spec"
FROZEN_SCENARIOS = (
    "exchange_auto_execute", "return_approval_execute", "return_waiting_approval",
    "handoff_ticket_create", "return_handoff_required", "return_window_closed",
    "exchange_window_closed", "non_returnable", "inventory_unavailable", "existing_active_case",
    "state_conflict", "missing_data", "policy_unavailable", "state_read_error",
    "direct_prompt_injection", "indirect_prompt_injection", "claimed_privileged_identity",
    "wrong_customer_resource", "repeated_approve_resume", "duplicate_submission",
    "approval_state_change", "approval_rejected", "guard_denies_on_resume", "restart_resume",
    "consult_no_action",
)
CASES = (support.exchange_case, support.handoff_case, support.waiting_return_case,
         support.approved_return_case, support.consult_case, support.deny_return_case,
         support.a21a, support.a21b, support.a21c, support.a21d, support.a22a, support.a22b,
         support.a22c, support.a22d, support.guard_denies_on_resume, support.restart_resume,
         support.a23, support.policy_unavailable_case, support.guard_read_case,
         support.write_fault_case, support.commit_fault_then_replay, support.read_fault_consult,
         support.claimed_identity_refund, support.claimed_identity_return,
         support.wrong_customer_case, support.direct_injection_case,
         support.indirect_injection_case)


def load(name: str) -> dict:
    return json.loads((SPEC / name).read_text(encoding="utf-8"))


class SpecFileTests(unittest.TestCase):
    def setUp(self):
        self.contract = stage6_contract()

    def test_spec_files_agree(self):
        self.assertEqual(self.contract.vocabulary_errors(), [])

    def test_scenarios_are_exactly_the_frozen_vocabulary(self):
        self.assertEqual(self.contract.scenario_ids(), FROZEN_SCENARIOS)
        self.assertEqual(len(FROZEN_SCENARIOS), 25)

    def test_actions_spec_is_the_frozen_code_contract(self):
        spec = load("stage6-actions.json")
        registry = build_action_registry()
        self.assertEqual([action["name"] for action in spec["actions"]], list(registry.names()))
        for action in spec["actions"]:
            code = registry.get(action["name"])
            self.assertEqual([p["name"] for p in action["parameters"]], list(code.parameter_names))
            for parameter in action["parameters"]:
                self.assertEqual(parameter.get("enum"), None if code.parameter(parameter["name"]).enum is None
                                 else list(code.parameter(parameter["name"]).enum))
            self.assertEqual(action["risk_disposition"], S6_RISK_POLICY.dispositions[action["name"]])
            self.assertEqual(action["resource_type"], code.resource_type)
        self.assertEqual(spec["risk_policy"]["version"], S6_RISK_POLICY.version)
        self.assertEqual(spec["reason_labels"], dict(REASON_LABELS))
        self.assertEqual(spec["deny_reason_codes"], list(DENY_REASON_CODES))
        self.assertEqual(spec["stale_reason_codes"], list(STALE_REASON_CODES))
        self.assertEqual(set(spec["failure_codes"]), set(FAILURE_CODES))
        self.assertEqual(set().union(*map(set, spec["forbidden_argument_names"].values())),
                         set(FORBIDDEN_ACTION_ARGUMENT_NAMES))
        self.assertEqual(spec["protocol_diagnostics"], list(STAGE6_PROTOCOL_DIAGNOSTICS))
        self.assertEqual(spec["completion_claim_markers"], list(COMPLETION_CLAIM_MARKERS))
        self.assertEqual(spec["guard_reads"], list(GUARD_READS))
        for name, codes in spec["deny_reason_codes_by_action"].items():
            self.assertLessEqual(set(codes), set(DENY_REASON_CODES), name)

    def test_schema_uses_only_the_frozen_checker_subset(self):
        schema = load("stage6-case.schema.json")
        self.contract.base_contract().lint_schema(schema)
        self.assertEqual(schema["required"], [
            "case_id", "archetype", "scenario", "initial_state", "virtual_now", "user_turns",
            "operator_script", "expected_capabilities", "expected_evidence", "expected_answerability",
            "expected_action", "expected_final_state"])
        self.assertEqual(schema["properties"]["initial_state"]["required"],
                         ["trusted_context", "faults", "action_faults"])
        patches = set(schema["properties"]["initial_state"]["properties"]) - {
            "trusted_context", "faults", "action_faults"}
        self.assertEqual(patches, set(BUSINESS_TABLES))
        self.assertEqual(set(schema["properties"]["expected_final_state"]["properties"]),
                         set(BUSINESS_TABLES) | {"pending_actions", "action_receipts"})
        self.assertNotIn("action_audit_events", json.dumps(schema["properties"]["expected_final_state"]))

    def test_holdout_plan_is_a_distribution_contract_only(self):
        plan = load("stage6-holdout-plan.json")
        self.assertEqual({split: rules["total_cases"] for split, rules in plan["splits"].items()},
                         {"dev": 40, "validation": 40, "holdout": 25})
        self.assertTrue(all(rules["per_scenario_min"] == 1 for rules in plan["splits"].values()))
        self.assertEqual([item["id"] for item in plan["required_coverage"]], [
            "A21", "A22", "A23", "direct_injection", "indirect_injection", "claimed_identity", "faults",
            "waiting_approval", "executed_auto", "executed_approval", "rejected", "stale", "denied",
            "failed"])
        # A distribution contract only: no private path, no case content.
        self.assertFalse({"path", "paths", "location", "cases"} & set(plan))
        self.assertNotIn("case_id", json.dumps(plan, ensure_ascii=False))

    def test_plan_requires_every_final_outcome_class(self):
        required = load("stage6-holdout-plan.json")["required_final_values"]
        definitions = load("stage6-final-outcomes.json")["definitions"]
        self.assertEqual(required, ["answer", "refuse", "handoff", "boundary", "action"])
        self.assertEqual(set(required), set(definitions))
        self.assertEqual(set(definitions), {"answer", "refuse", "handoff", "boundary", "action"})
        self.assertEqual(len(required), len(set(required)))

    def test_frozen_stage4_5_inputs_are_byte_identical(self):
        # Every Stage 4/5 holdout author input keeps its sealed sha256 (LF-normalized).
        manifest = json.loads((ROOT / "eval" / "v2" / "holdout-input.manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            data = (ROOT / entry["path"]).read_bytes().replace(b"\r\n", b"\n")
            with self.subTest(path=entry["path"]):
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])

    def test_only_dev_dataset_and_sealed_manifest_exist(self):
        # The sealed manifest holds safe metadata only; the DEV split and its author receipt are the
        # only Stage 6 dataset files; no VALIDATION or opened holdout is in the repo.
        found = [path.relative_to(ROOT).as_posix() for path in ROOT.joinpath("eval").rglob("*")
                 if path.is_file() and "stage6" in path.name.lower()
                 and any(token in path.name.lower() for token in ("dev", "validation", "holdout.json",
                                                                   "holdout.manifest", "sealed", "receipt.json"))]
        self.assertEqual(sorted(found), ["eval/v2/stage6-dev.json", "eval/v2/stage6-dev.receipt.json",
                                         "eval/v2/stage6-holdout.manifest.json"])


class SeedReaderTests(unittest.TestCase):
    def test_seed_reader_matches_the_real_fixture(self):
        with Stage6Database() as db:
            connection = harness_connection(db.path)
            try:
                state = read_state(connection)
            finally:
                connection.close()
        seed = stage6_contract().seed_rows()
        for table in BUSINESS_TABLES:
            self.assertEqual(seed[table], state[table], table)


class CaseRuleTests(unittest.TestCase):
    def setUp(self):
        self.contract = stage6_contract()

    def errors(self, case):
        return self.contract.case_errors(case)

    def assert_rule(self, case, fragment):
        errors = self.errors(case)
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_valid_synthetic_cases_pass(self):
        for build in CASES:
            case = build()
            with self.subTest(case=case["case_id"]):
                self.assertEqual(self.errors(case), [])

    def test_schema_rejects_bad_shapes(self):
        bad = {
            "unknown scenario": lambda c: c.update(scenario="refund_money"),
            "action table patch": lambda c: c["initial_state"].update(pending_actions={}),
            "audit patch": lambda c: c["initial_state"].update(action_audit_events={}),
            "null final state": lambda c: c.update(expected_final_state=None),
            "unknown op": lambda c: c.update(operator_script=[{"op": "refund"}]),
            "mutate action table": lambda c: c.update(operator_script=[support.mutate(
                "pending_actions", "PA-1", {"op": "delete"})]),
            "fault point": lambda c: c["initial_state"].update(action_faults=[
                {"point": "refund", "mode": "error", "on_call": 1}]),
            "naive time": lambda c: c.update(virtual_now="2026-11-15T10:00:00"),
            "audit expectation": lambda c: c["expected_final_state"].update(action_audit_events={}),
            "generated id column": lambda c: c["expected_final_state"]["after_sales_cases"]["insert"][0][
                "row"].update(case_id="AS6-0000000000000000"),
        }
        for label, mutate in bad.items():
            case = support.exchange_case()
            mutate(case)
            with self.subTest(label=label):
                self.assertNotEqual(self.contract.schema_errors(case), [])

    def test_rule_1_final_action_iff_expected_action(self):
        case = support.exchange_case()
        case["expected_answerability"]["final"] = "answer"
        self.assert_rule(case, "action exactly when")
        case = support.consult_case(final="action")
        self.assert_rule(case, "action exactly when")

    def test_rule_2_capabilities(self):
        case = support.exchange_case()
        case["expected_capabilities"]["forbidden"].remove("create_return")
        self.assert_rule(case, "forbidden must contain create_return")
        case = support.exchange_case()
        case["expected_capabilities"]["required"] = []
        self.assert_rule(case, "required must contain create_exchange")
        case = support.consult_case()
        case["expected_capabilities"]["forbidden"] = ["create_return"]
        self.assert_rule(case, "forbidden must contain create_exchange")
        case = support.exchange_case()
        case["expected_evidence"]["all_of"] = [{"subject": {"entity": "order", "id": "ORD-1001"},
                                                "field": "status", "source_types": ["business"]}]
        self.assert_rule(case, "all_of and any_of must be empty")

    def test_rule_3_args(self):
        case = support.exchange_case()
        del case["expected_action"]["args"]["target_sku"]
        self.assert_rule(case, "cover exactly the parameters")
        case = support.exchange_case()
        case["expected_action"]["args_any_of"] = {"order_id": ["ORD-1001"]}
        self.assert_rule(case, "overlap")
        case = support.exchange_case()
        case["expected_action"]["args"]["reason_code"] = "no_longer_wanted"
        self.assert_rule(case, "outside its closed vocabulary")

    def test_rule_4_guard_compatibility(self):
        case = support.exchange_case()
        case["expected_action"]["initial_guard"] = {"decision": "ALLOW", "reason_code": "risk_policy_requires_approval"}
        self.assert_rule(case, "does not belong to ALLOW")
        case = support.exchange_case()
        case["expected_action"]["initial_guard"] = support.REQUIRE
        case["expected_action"]["approval_required"] = True
        self.assert_rule(case, "not the risk policy's decision")
        case = support.exchange_case()
        case["expected_action"]["approval_required"] = True
        self.assert_rule(case, "approval_required must equal")
        case = support.deny_return_case()
        case["expected_action"]["initial_guard"] = support.deny("inventory_unavailable")
        case["expected_action"]["final_code"] = "inventory_unavailable"
        self.assert_rule(case, "is not a DENY outcome of create_return")

    def test_rule_5_final_status(self):
        case = support.exchange_case()
        case["expected_action"]["final_status"] = "FAILED"
        case["expected_action"]["final_code"] = "write_failed"
        self.assert_rule(case, "ALLOW ends EXECUTED")
        case = support.waiting_return_case()
        case["expected_action"]["final_status"] = "EXECUTED"
        self.assert_rule(case, "stays WAITING_APPROVAL")
        case = support.policy_unavailable_case()
        case["initial_state"]["action_faults"] = []
        self.assert_rule(case, "null needs a declared action fault")
        case = support.a23()
        case["expected_action"]["final_status"] = "EXECUTED"
        case["expected_action"]["final_code"] = None
        self.assert_rule(case, "the first decision rejects")
        case = support.deny_return_case()
        case["expected_action"]["final_status"] = "EXECUTED"
        case["expected_action"]["final_code"] = None
        self.assert_rule(case, "DENY ends DENIED")

    def test_rule_6_final_code(self):
        case = support.a22a()
        case["expected_action"]["final_code"] = "return_window_closed"
        self.assert_rule(case, "closed vocabulary")
        case = support.exchange_case()
        case["expected_action"]["final_code"] = "write_failed"
        self.assert_rule(case, "has no code")

    def test_rule_7_events_align(self):
        case = support.a22a()
        case["expected_action"]["events"] = case["expected_action"]["events"][:1]
        self.assert_rule(case, "one entry per operator_script event")
        case = support.a22a()
        case["expected_action"]["events"][0] = support.event("STALE", "record_version_changed")
        self.assert_rule(case, "has no expected outcome")
        case = support.a21c()
        case["expected_action"]["events"][1] = None
        self.assert_rule(case, "needs an expected outcome")
        case = support.a21c()
        case["expected_action"]["events"][1] = {"status": None}
        self.assert_rule(case, "only a rerun_request / new_request may submit no action")

    def test_rule_8_approval_events_only_on_the_approval_path(self):
        case = support.exchange_case()
        case["operator_script"] = [{"op": "approve"}]
        case["expected_action"]["events"] = [support.event("EXECUTED", replay=True)]
        self.assert_rule(case, "only on the REQUIRE_APPROVAL path")
        case = support.consult_case()
        case["operator_script"] = [{"op": "replay_submission"}]
        self.assert_rule(case, "only mutate / advance_clock / restart")

    def test_rule_9_mutate_and_clock(self):
        case = support.a22a()
        case["operator_script"][0]["patch"]["set"]["version"] = 1
        self.assert_rule(case, "must increase version")
        case = support.a22a()
        del case["operator_script"][0]["patch"]["set"]["updated_at"]
        self.assert_rule(case, "writes version and updated_at")
        case = support.guard_denies_on_resume()
        case["operator_script"][0]["virtual_now"] = support.NOW
        self.assert_rule(case, "strictly forward")

    def test_rule_10_fixture_keys_and_active_rows(self):
        case = support.exchange_case()
        case["initial_state"]["after_sales_cases"] = {"AS6-0123456789ABCDEF": {"op": "insert", "row": {
            "order_id": "ORD-1001", "order_item_id": "OI-1001-2", "customer_id": "CUST-001", "type": "return",
            "status": "已完成", "reason": "x", "created_at": support.NOW, "updated_at": support.NOW, "version": 1}}}
        self.assert_rule(case, "generated-id prefixes")
        case = support.exchange_case()
        row = {"order_id": "ORD-2001", "order_item_id": "OI-2001-1", "customer_id": "CUST-002", "type": "return",
               "status": "待处理", "reason": "x", "created_at": support.NOW, "updated_at": support.NOW, "version": 1}
        case["initial_state"]["after_sales_cases"] = {"AS-X1": {"op": "insert", "row": row}}
        self.assert_rule(case, "two active after-sales cases")
        case = support.exchange_case()
        case["initial_state"]["order_items"] = {"OI-9": {"op": "insert", "row": {
            "order_id": "ORD-9999", "sku": "SKU-MUG", "product_name": "x", "category": "家居", "quantity": 1,
            "unit_price": "1.00", "updated_at": support.NOW, "version": 1}}}
        self.assert_rule(case, "refers to a missing row")

    def test_final_state_consistency(self):
        case = support.exchange_case()
        case["expected_final_state"]["after_sales_cases"]["insert"][0]["row"]["customer_id"] = "CUST-002"
        self.assert_rule(case, "trusted persona's customer")
        case = support.waiting_return_case()
        case["expected_final_state"]["pending_actions"]["insert"][0]["row"]["approval_decision"] = "APPROVE"
        self.assert_rule(case, "all null or all set")
        case = support.waiting_return_case()
        row = case["expected_final_state"]["pending_actions"]["insert"][0]["row"]
        row.update(status="REJECTED", approval_decision="APPROVE", approver_ref="op-demo-1",
                   decided_at=support.NOW, outcome_code="approval_rejected")
        self.assert_rule(case, "REJECTED requires a REJECT decision")
        case = support.consult_case()
        case["expected_final_state"]["action_receipts"] = support.exchange_case()["expected_final_state"]["action_receipts"]
        self.assert_rule(case, "no action is expected")
        case = support.exchange_case()
        case["expected_final_state"]["orders"] = {"update": {"ORD-9999": {"version": 9}}}
        self.assert_rule(case, "not a row of baseline B")

    def test_action_fault_shape(self):
        case = support.policy_unavailable_case()
        case["initial_state"]["action_faults"][0]["read"] = "order"
        self.assert_rule(case, "read is only for point guard_read")
        case = support.write_fault_case()
        case["initial_state"]["action_faults"][0]["mode"] = "malformed"
        self.assert_rule(case, "is not allowed at business_write")
        case = support.policy_unavailable_case()
        case["initial_state"]["action_faults"].append(dict(case["initial_state"]["action_faults"][0]))
        self.assert_rule(case, "declared twice")

    def test_stage5_rules_still_apply(self):
        case = support.exchange_case()
        case["expected_capabilities"]["required"].append("create_return")
        self.assert_rule(case, "overlap")
        case = support.read_fault_consult()
        case["initial_state"]["faults"][0]["match"] = {"sku": "SKU-MUG"}
        self.assert_rule(case, "not arguments of get_order")
        case = support.consult_case()
        case["expected_answerability"]["clarify"] = {"required": True, "slots": ["order_id"]}
        self.assert_rule(case, "no conditional turn")


FINAL_VALUES = ("answer", "refuse", "handoff", "boundary", "action")


class DatasetPlanTests(unittest.TestCase):
    """Synthetic contract fixtures only: just the fields the distribution check reads."""

    SIZES = (("holdout", 25), ("dev", 40), ("validation", 40))

    def minimal(self, index, scenario, *, final="action", status="DENIED", decision="DENY",
                archetype="A01", persona="demo-a", now="2026-11-15T10:00:00+08:00", faults=False,
                action=True):
        return {"case_id": "p" + str(index), "scenario": scenario, "archetype": archetype,
                "virtual_now": now, "expected_answerability": {"final": final},
                "initial_state": {"trusted_context": {"persona_id": persona}, "faults": [],
                                  "action_faults": [{"point": "commit"}] if faults else []},
                "expected_action": ({"final_status": status, "initial_guard": {"decision": decision}}
                                    if action else None)}

    def plan_cases(self, total=25):
        scenarios = stage6_contract().scenario_ids()
        cases = [self.minimal(i, scenario) for i, scenario in enumerate(scenarios)]
        overrides = {
            0: dict(status="EXECUTED", decision="ALLOW"), 1: dict(status="EXECUTED", decision="REQUIRE_APPROVAL"),
            2: dict(status="WAITING_APPROVAL", decision="REQUIRE_APPROVAL"),
            12: dict(status="FAILED", decision="ALLOW", faults=True),
            13: dict(final="refuse", action=False),                 # state_read_error, model side
            14: dict(final="answer", action=False),                 # direct_prompt_injection, consultation
            16: dict(final="boundary", action=False),               # claimed_privileged_identity
            18: dict(archetype="A21"), 20: dict(archetype="A22", status="STALE", decision="REQUIRE_APPROVAL"),
            21: dict(archetype="A23", status="REJECTED", decision="REQUIRE_APPROVAL"),
            24: dict(final="handoff", action=False, persona="demo-b", now="2026-12-01T10:00:00+08:00"),
        }
        for index, values in overrides.items():
            cases[index] = self.minimal(index, cases[index]["scenario"], **values)
        for index in range(len(cases), total):
            cases.append(self.minimal(index, scenarios[5 + index % 10]))
        return cases

    def test_conforming_distributions(self):
        for split, total in self.SIZES:
            with self.subTest(split=split):
                cases = self.plan_cases(total)
                self.assertEqual({case["expected_answerability"]["final"] for case in cases}, set(FINAL_VALUES))
                self.assertEqual(stage6_contract().dataset_plan_errors(cases, split), [])

    def test_every_final_outcome_class_is_required(self):
        for split, total in self.SIZES:
            for value in FINAL_VALUES:
                cases = self.plan_cases(total)
                replacement = "answer" if value == "action" else "action"
                for case in cases:
                    if case["expected_answerability"]["final"] == value:
                        case["expected_answerability"]["final"] = replacement
                with self.subTest(split=split, missing=value):
                    self.assertEqual(stage6_contract().dataset_plan_errors(cases, split),
                                     [split + ": no case has final " + value])

    def test_missing_coverage_is_reported(self):
        cases = self.plan_cases()
        cases[21] = self.minimal(21, cases[21]["scenario"])
        errors = stage6_contract().dataset_plan_errors(cases, "holdout")
        self.assertTrue(any("A23" in error for error in errors), errors)
        self.assertTrue(any("rejected" in error for error in errors), errors)
        errors = stage6_contract().dataset_plan_errors(self.plan_cases()[:24], "holdout")
        self.assertTrue(any("consult_no_action" in error for error in errors))
        self.assertTrue(any("25" in error for error in errors))
        self.assertNotEqual(stage6_contract().dataset_plan_errors(self.plan_cases(), "dev"), [])

    def test_scenario_rule_is_kept_for_every_split(self):
        for split, total in self.SIZES:
            cases = [case for case in self.plan_cases(total) if case["scenario"] != "restart_resume"]
            cases += [self.minimal(100 + index, "missing_data") for index in range(total - len(cases))]
            with self.subTest(split=split):
                self.assertIn(split + ": scenario restart_resume has 0 case(s)",
                              stage6_contract().dataset_plan_errors(cases, split))


if __name__ == "__main__":
    unittest.main()
