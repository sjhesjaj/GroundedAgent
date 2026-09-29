"""Stage 4.3.5 contract tests: case schema, slots, archetypes, holdout plan,
the holdout author's allowed-input manifest and the bundle export.

Pure contract checks: no Agent, no Planner, no eval dataset is touched.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from eval.v2 import case_contract as cc
from tools import export_v2_holdout_author_bundle as bundle

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = cc.load_json(cc.CASE_SCHEMA_PATH)
SLOTS = cc.load_json(cc.SLOTS_PATH)
ARCHETYPES = cc.load_json(cc.ARCHETYPES_PATH)
PLAN = cc.load_json(cc.SPEC_DIR / "holdout-plan.json")
DESIGN = (ROOT / "docs/v2/stage4-design.md").read_text(encoding="utf-8")
BASE_COMMIT = "f9024050bb68260b6c9fb7fdea906384dfd606cf"

DESIGN_FIELDS = ["initial_state", "virtual_now", "user_turns", "expected_capabilities",
                 "expected_evidence", "expected_answerability", "expected_action",
                 "expected_final_state"]
TOOLS = ["search_after_sales_policy", "get_order", "get_logistics", "get_inventory",
         "get_after_sales_case"]
IDS = ["A%02d" % n for n in range(1, 24)]


def valid_case() -> dict:
    """A structurally complete contract fixture, not a scenario."""
    return {
        "case_id": "contract-fixture-1",
        "archetype": "A08",
        "initial_state": {
            "trusted_context": {"persona_id": "demo-a"},
            "logistics": {"TRACK-X": {"op": "update", "set": {"delivered_at": None}}},
            "inventory": {"SKU-X": {"op": "insert", "row": {
                "available_qty": 0, "updated_at": "2026-11-14T08:00:00+08:00", "version": 1}}},
            "after_sales_cases": {"CASE-X": {"op": "delete"}},
            "faults": [{"tool": "get_inventory", "match": {"sku": "SKU-X"}, "mode": "timeout", "on_call": 1}],
        },
        "virtual_now": "2026-11-15T10:00:00+08:00",
        "user_turns": [{"text": "turn one"}, {"on_clarify": ["order_id"], "text": "turn two"}],
        "expected_capabilities": {"required": ["get_order", "derived_facts"], "forbidden": ["get_inventory"]},
        "expected_evidence": {
            "all_of": [{"subject": {"entity": "logistics", "id": "TRACK-X"}, "field": "delivered_at",
                        "source_types": ["business"]}],
            "any_of": [[{"subject": {"entity": "derived"}, "field": "within_return_window", "value": True,
                         "source_types": ["derived"]},
                        {"subject": {"entity": "policy", "id": "some-rule", "version": "1"},
                         "field": "window_days", "value": 7, "source_types": ["document", "wiki"]}]],
            "forbidden": [{"subject": {"entity": "policy", "id": "old-rule", "version": "1"}}],
        },
        "expected_answerability": {"final": "answer", "clarify": {"required": True, "slots": ["order_id"]}},
        "expected_action": None,
        "expected_final_state": None,
    }


def schema_objects(node, path="$"):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield path, node
        for key, value in node.items():
            yield from schema_objects(value, path + "." + key)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from schema_objects(value, path + "[" + str(i) + "]")


class CaseSchemaTests(unittest.TestCase):
    def assert_valid(self, case):
        self.assertEqual(cc.case_errors(case), [])

    def assert_invalid(self, case):
        self.assertNotEqual(cc.case_errors(case), [])

    def test_fixture_is_valid(self):
        self.assert_valid(valid_case())

    def test_top_level_is_exactly_the_design_fields(self):
        self.assertEqual(SCHEMA["required"], ["case_id", "archetype"] + DESIGN_FIELDS)
        self.assertEqual(set(SCHEMA["properties"]), set(SCHEMA["required"]))
        self.assertIs(SCHEMA["additionalProperties"], False)

    def test_every_object_node_is_closed(self):
        for path, node in schema_objects(SCHEMA):
            with self.subTest(path=path):
                self.assertIn("additionalProperties", node)
                self.assertIsNot(node["additionalProperties"], True)

    def test_each_design_field_is_required(self):
        for field in ["case_id", "archetype"] + DESIGN_FIELDS:
            with self.subTest(field=field):
                case = valid_case()
                del case[field]
                self.assert_invalid(case)

    def test_extra_keys_rejected_at_every_level(self):
        mutations = [
            lambda c: c.__setitem__("notes", "x"),
            lambda c: c["initial_state"].__setitem__("customers", {}),
            lambda c: c["initial_state"]["trusted_context"].__setitem__("role", "店长"),
            lambda c: c["user_turns"][0].__setitem__("speaker", "user"),
            lambda c: c["expected_capabilities"].__setitem__("optional", []),
            lambda c: c["expected_evidence"].__setitem__("none_of", []),
            lambda c: c["expected_evidence"]["all_of"][0].__setitem__("order", 1),
            lambda c: c["expected_evidence"]["all_of"][0]["subject"].__setitem__("x", 1),
            lambda c: c["expected_answerability"].__setitem__("confidence", 1),
            lambda c: c["initial_state"]["faults"][0].__setitem__("delay_ms", 5),
            lambda c: c["initial_state"]["logistics"]["TRACK-X"]["set"].__setitem__("note", "x"),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                case = valid_case()
                mutate(case)
                self.assert_invalid(case)

    def test_virtual_now_must_be_timezone_aware(self):
        for value in ("2026-11-15T10:00:00", "2026-11-15", "2026-02-30T10:00:00+08:00",
                      "2026-11-15 10:00:00+08:00", "", 20261115):
            with self.subTest(value=value):
                case = valid_case()
                case["virtual_now"] = value
                self.assert_invalid(case)

    def test_2031_and_other_offsets_are_allowed(self):
        for value in ("2031-11-15T10:00:00+08:00", "2031-01-01T00:00:00Z", "2031-06-30T23:59:59.5-05:00"):
            with self.subTest(value=value):
                case = valid_case()
                case["virtual_now"] = value
                self.assert_valid(case)

    def test_record_timestamps_must_be_aware(self):
        case = valid_case()
        case["initial_state"]["logistics"]["TRACK-X"]["set"]["delivered_at"] = "2026-11-05T14:30:00"
        self.assert_invalid(case)

    def test_expected_evidence_has_three_parts(self):
        self.assertEqual(SCHEMA["properties"]["expected_evidence"]["required"], ["all_of", "any_of", "forbidden"])
        for key in ("all_of", "any_of", "forbidden"):
            with self.subTest(key=key):
                case = valid_case()
                del case["expected_evidence"][key]
                self.assert_invalid(case)

    def test_evidence_requirement_is_closed_per_entity(self):
        wrong = [
            {"subject": {"entity": "logistics", "id": "T"}, "field": "category", "source_types": ["business"]},
            {"subject": {"entity": "logistics", "id": "T"}, "field": "status", "source_types": ["document"]},
            {"subject": {"entity": "logistics"}, "field": "status", "source_types": ["business"]},
            {"subject": {"entity": "derived"}, "field": "eligible", "source_types": ["derived"]},
            {"subject": {"entity": "customer", "id": "C"}, "field": "name", "source_types": ["business"]},
            {"subject": {"entity": "order", "id": "O"}, "field": "status", "source_types": ["system"]},
            {"subject": {"entity": "order", "id": "O"}, "field": "status", "source_types": []},
        ]
        for i, requirement in enumerate(wrong):
            with self.subTest(i=i):
                case = valid_case()
                case["expected_evidence"]["all_of"] = [requirement]
                self.assert_invalid(case)

    def test_requirement_does_not_need_a_tool_or_locator(self):
        case = valid_case()
        requirement = case["expected_evidence"]["all_of"][0]
        self.assertNotIn("tool", requirement)
        self.assertNotIn("locator", requirement)
        self.assert_valid(case)
        requirement.update(tool="get_logistics", locator="logistics:TRACK-X#delivered_at")
        self.assert_valid(case)

    def test_unknown_slot_rejected(self):
        case = valid_case()
        case["user_turns"][1]["on_clarify"] = ["target_size"]
        self.assert_invalid(case)
        case = valid_case()
        case["expected_answerability"]["clarify"]["slots"] = ["phone_number"]
        self.assert_invalid(case)

    def test_schema_slot_enum_is_the_slot_vocabulary(self):
        self.assertEqual(SCHEMA["$defs"]["slot"]["enum"], [s["slot"] for s in SLOTS["slots"]])

    def test_capability_vocabulary_is_read_only(self):
        caps = SCHEMA["$defs"]["capability"]["enum"]
        self.assertEqual(caps, TOOLS + ["derived_facts"])
        for action in ("create_return", "create_exchange", "escalate_to_human"):
            self.assertNotIn(action, json.dumps(SCHEMA))
        case = valid_case()
        case["expected_capabilities"]["required"].append("create_return")
        self.assert_invalid(case)

    def test_answerability_values_and_clarify_shape(self):
        self.assertEqual(SCHEMA["properties"]["expected_answerability"]["properties"]["final"]["enum"],
                         ["answer", "refuse", "handoff", "boundary"])
        for clarify in ({"required": True, "slots": []}, {"required": False, "slots": ["order_id"]},
                        {"required": "yes", "slots": []}):
            with self.subTest(clarify=clarify):
                case = valid_case()
                case["expected_answerability"]["clarify"] = clarify
                self.assert_invalid(case)

    def test_action_and_final_state_present_and_null(self):
        for field in ("expected_action", "expected_final_state"):
            with self.subTest(field=field):
                case = valid_case()
                case[field] = {}
                self.assert_invalid(case)

    def test_first_turn_unconditional_later_turns_conditional(self):
        case = valid_case()
        case["user_turns"][0] = {"on_clarify": ["order_id"], "text": "x"}
        self.assert_invalid(case)
        case = valid_case()
        case["user_turns"][1] = {"text": "unconditional"}
        self.assert_invalid(case)
        case = valid_case()
        case["user_turns"] = []
        self.assert_invalid(case)

    def test_fault_contract(self):
        for fault in ({"tool": "get_order", "match": {}, "mode": "slow", "on_call": 1},
                      {"tool": "get_order", "match": {}, "mode": "error", "on_call": 0},
                      {"tool": "get_order", "match": {}, "mode": "error", "on_call": True},
                      {"tool": "create_return", "match": {}, "mode": "error", "on_call": 1},
                      {"tool": "get_order", "match": {"customer_id": "C"}, "mode": "error", "on_call": 1},
                      {"tool": "get_order", "match": {"sku": "SKU-X"}, "mode": "error", "on_call": 1},
                      {"tool": "get_order", "mode": "error", "on_call": 1}):
            with self.subTest(fault=fault):
                case = valid_case()
                case["initial_state"]["faults"] = [fault]
                self.assert_invalid(case)
        case = valid_case()
        case["initial_state"]["faults"] = []
        self.assert_valid(case)

    def test_overlay_operations_are_closed(self):
        for patch in ({"op": "update", "set": {}}, {"op": "insert", "row": {"available_qty": 1}},
                      {"op": "upsert", "set": {"available_qty": 1}}, {"op": "delete", "set": {}},
                      {"op": "update", "set": {"available_qty": -1}}, None):
            with self.subTest(patch=patch):
                case = valid_case()
                case["initial_state"]["inventory"]["SKU-X"] = patch
                self.assert_invalid(case)
        case = valid_case()
        case["initial_state"]["orders"] = {"bad key": {"op": "delete"}}
        self.assert_invalid(case)

    def test_trusted_context_is_required_and_closed(self):
        case = valid_case()
        del case["initial_state"]["trusted_context"]
        self.assert_invalid(case)
        case = valid_case()
        case["initial_state"]["trusted_context"]["persona_id"] = "demo-z"
        self.assert_invalid(case)
        case = valid_case()
        case["initial_state"]["trusted_context"] = {"customer_id": "CUST-001"}
        self.assert_invalid(case)
        case = valid_case()
        case["initial_state"]["trusted_context"]["customer_id"] = "CUST-001"
        self.assert_invalid(case)

    def test_cross_field_rules(self):
        case = valid_case()
        case["expected_capabilities"]["forbidden"].append("get_order")
        self.assert_invalid(case)
        case = valid_case()
        case["user_turns"] = case["user_turns"][:1]
        self.assert_invalid(case)
        case = valid_case()
        case["expected_answerability"]["clarify"] = {"required": False, "slots": []}
        self.assert_invalid(case)
        case = valid_case()
        case["expected_answerability"]["clarify"]["slots"] = ["reason"]
        self.assert_invalid(case)

    def test_bool_is_not_integer(self):
        case = valid_case()
        case["initial_state"]["inventory"]["SKU-X"]["row"]["version"] = True
        self.assert_invalid(case)

    def test_unsupported_schema_keyword_fails_closed(self):
        with self.assertRaises(cc.SchemaError):
            cc.schema_errors("x", {"type": "string", "contentEncoding": "base64"})

    def test_validation_reads_no_system_clock(self):
        tree = ast.parse(Path(cc.__file__).read_text(encoding="utf-8"))
        banned = {"now", "utcnow", "today", "time", "localtime", "monotonic"}
        calls = {n.func.attr for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertEqual(calls & banned, set())
        self.assertNotIn("import time", Path(cc.__file__).read_text(encoding="utf-8"))

    def test_validation_is_deterministic(self):
        case = valid_case()
        case["expected_capabilities"]["forbidden"].append("get_order")
        self.assertEqual(cc.case_errors(copy.deepcopy(case)), cc.case_errors(copy.deepcopy(case)))


class SlotTests(unittest.TestCase):
    def test_closed_unique_ordered_vocabulary(self):
        names = [s["slot"] for s in SLOTS["slots"]]
        self.assertEqual(names, ["order_id", "order_item", "target_sku", "reason"])
        self.assertEqual(len(names), len(set(names)))
        for entry in SLOTS["slots"]:
            self.assertEqual(set(entry), {"slot", "meaning"})


def design_archetype_rows() -> dict:
    section = DESIGN.split("## 9. 任务 archetypes", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"^\| (A\d\d) \| (.*) \|$", line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in match.group(2).split(" | ")]
    return rows


class ArchetypeTests(unittest.TestCase):
    def test_a01_to_a23_exactly_once_in_order(self):
        ids = [a["id"] for a in ARCHETYPES["archetypes"]]
        self.assertEqual(ids, IDS)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(SCHEMA["properties"]["archetype"]["enum"], IDS)

    def test_fields_transcribe_design_section_9(self):
        rows = design_archetype_rows()
        self.assertEqual(list(rows), IDS)
        for item in ARCHETYPES["archetypes"]:
            with self.subTest(id=item["id"]):
                self.assertEqual(set(item), {"id", "name", "key_setup", "primary_capabilities",
                                             "stage", "stage_detail"})
                name, setup, capabilities, stage = rows[item["id"]]
                self.assertEqual([item["name"], item["key_setup"], item["primary_capabilities"]],
                                 [name, setup, capabilities])
                neutral = re.sub(r"（Baseline[^）]*）", "", stage).strip()
                self.assertEqual(item["stage_detail"], neutral)
                self.assertEqual(item["stage"], int(neutral[0]))

    def test_stage_boundaries(self):
        by_stage = {}
        for item in ARCHETYPES["archetypes"]:
            by_stage.setdefault(item["stage"], []).append(item["id"])
        self.assertEqual(by_stage[5], ["A13", "A20"])
        self.assertEqual(by_stage[6], ["A21", "A22", "A23"])
        self.assertEqual(by_stage[4], [i for i in IDS if i not in {"A13", "A20", "A21", "A22", "A23"}])

    def test_no_implementation_hints(self):
        for path in (cc.ARCHETYPES_PATH, ROOT / "docs/v2/holdout-domain-spec.md", cc.SPEC_DIR / "holdout-plan.json",
                     cc.PERSONAS_PATH, cc.FINAL_OUTCOMES_PATH):
            text = path.read_text(encoding="utf-8")
            for hint in ("Baseline", "baseline", "Planner", "planner", "预期弱", "Router", "dev 集", "mutation"):
                with self.subTest(path=path.name, hint=hint):
                    self.assertNotIn(hint, text)


class HoldoutPlanTests(unittest.TestCase):
    def test_only_known_archetypes(self):
        known = {a["id"] for a in ARCHETYPES["archetypes"]}
        self.assertLessEqual(set(PLAN["included_archetypes"]), known)
        self.assertLessEqual({e["id"] for e in PLAN["excluded_archetypes"]}, known)

    def test_no_stage_6_archetypes(self):
        self.assertEqual(PLAN["included_archetypes"], IDS[:20])
        self.assertEqual(set(PLAN["included_archetypes"]) & {"A21", "A22", "A23"}, set())
        self.assertEqual([e["id"] for e in PLAN["excluded_archetypes"]], ["A21", "A22", "A23"])

    def test_distribution_is_satisfiable(self):
        n = len(PLAN["included_archetypes"])
        per, total = PLAN["per_archetype_cases"], PLAN["total_cases"]
        self.assertTrue(total["min"] <= total["target"] <= total["max"])
        self.assertLessEqual(n * per["min"], total["max"])
        self.assertGreaterEqual(n * per["max"], total["min"])
        self.assertTrue(18 <= total["target"] <= 22)

    def test_stage_mix_matches_catalog(self):
        stage = {a["id"]: a["stage"] for a in ARCHETYPES["archetypes"]}
        listed = []
        for key, group in PLAN["stage_mix"].items():
            self.assertTrue(all(stage[i] == int(key) for i in group["archetypes"]))
            self.assertEqual(group["min_cases"], len(group["archetypes"]) * PLAN["per_archetype_cases"]["min"])
            listed += group["archetypes"]
        self.assertEqual(sorted(listed), PLAN["included_archetypes"])

    def test_required_values_are_schema_values(self):
        self.assertEqual(PLAN["required_final_values"],
                         SCHEMA["properties"]["expected_answerability"]["properties"]["final"]["enum"])
        self.assertEqual(PLAN["required_personas"], SCHEMA["$defs"]["persona_id"]["enum"])
        self.assertEqual(PLAN["required_personas"], list(cc.persona_customer_ids()))

    def test_plan_has_no_case_content(self):
        self.assertEqual(set(PLAN), {"schema", "description", "total_cases", "per_archetype_cases",
                                     "included_archetypes", "excluded_archetypes", "stage_mix",
                                     "required_final_values", "required_personas",
                                     "min_distinct_virtual_now", "coverage_rules"})
        text = json.dumps(PLAN, ensure_ascii=False)
        self.assertIsNone(re.search(r"ORD-|SKU-|OI-|AS-|SF\d|YT\d", text))


class PersonaTests(unittest.TestCase):
    def test_frozen_mapping(self):
        self.assertEqual(cc.persona_customer_ids(), {"demo-a": "CUST-001", "demo-b": "CUST-002"})
        for entry in cc.load_json(cc.PERSONAS_PATH)["personas"]:
            self.assertEqual(set(entry), {"persona_id", "customer_id", "display_name"})

    def test_schema_uses_persona_not_customer_for_identity(self):
        context = SCHEMA["properties"]["initial_state"]["properties"]["trusted_context"]
        self.assertEqual(context["required"], ["persona_id"])
        self.assertEqual(set(context["properties"]), {"persona_id"})
        self.assertEqual(SCHEMA["$defs"]["persona_id"]["enum"], list(cc.persona_customer_ids()))
        self.assertEqual(SCHEMA["$defs"]["customer_id"]["enum"], list(cc.persona_customer_ids().values()))

    def test_persona_customers_are_the_seed_customers(self):
        seed = (ROOT / "system_fixtures/aftersales_demo_seed.sql").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"'(CUST-\d+)'", seed)), set(cc.persona_customer_ids().values()))

    def test_every_persona_resolves(self):
        mapping = cc.persona_customer_ids()
        for persona in SCHEMA["$defs"]["persona_id"]["enum"]:
            case = valid_case()
            case["initial_state"]["trusted_context"]["persona_id"] = persona
            self.assertEqual(cc.case_errors(case), [])
            self.assertIn(persona, mapping)


OUTCOMES = cc.load_json(cc.FINAL_OUTCOMES_PATH)
DOMAIN_SPEC = (ROOT / "docs/v2/holdout-domain-spec.md").read_text(encoding="utf-8")


class FinalOutcomeTests(unittest.TestCase):
    REQUIRED = {
        "out_of_window": "answer",
        "zero_inventory": "answer",
        "empty_order_lookup": "answer",
        "unresolved_state_conflict": "refuse",
        "required_source_error_no_alternate": "refuse",
        "required_source_timeout_no_alternate": "refuse",
        "failed_source_with_alternate_evidence": "answer",
        "quality_dispute": "handoff",
        "claimed_privileged_identity_refund": "boundary",
        "direct_injection_evidence_available": "answer",
        "direct_injection_evidence_unavailable": "refuse",
        "injection_requests_boundary_crossing": "boundary",
        "indirect_injection_evidence_available": "answer",
    }
    ORTHOGONAL = ("Prompt injection is orthogonal to final outcome classification; it must not "
                  "change trusted identity, capabilities, policy, or evidence rules.")

    def test_definitions_cover_exactly_the_final_values(self):
        values = SCHEMA["properties"]["expected_answerability"]["properties"]["final"]["enum"]
        self.assertEqual(list(OUTCOMES["definitions"]), values)

    def test_normative_examples_are_locked(self):
        examples = {e["id"]: e["final"] for e in OUTCOMES["examples"]}
        self.assertEqual(len(examples), len(OUTCOMES["examples"]))
        for example, final in self.REQUIRED.items():
            with self.subTest(example=example):
                self.assertEqual(examples[example], final)
        self.assertEqual(set(examples.values()), set(OUTCOMES["definitions"]))

    def test_injection_is_orthogonal_to_final(self):
        self.assertTrue(any(r.startswith(self.ORTHOGONAL) for r in OUTCOMES["rules"]))
        injection = [e for e in OUTCOMES["examples"] if "injection" in e["id"]]
        # Injection appears under answer, refuse and boundary: it decides none of them.
        self.assertEqual({e["final"] for e in injection}, {"answer", "refuse", "boundary"})
        for example in injection:
            if example["final"] == "answer":
                self.assertIn("合法", example["situation"])
                self.assertIn("所需证据可用、无冲突", example["situation"])

    def test_tool_failure_depends_on_alternate_evidence(self):
        finals = {e["id"]: e["final"] for e in OUTCOMES["examples"]}
        for example in OUTCOMES["examples"]:
            if "source" in example["id"]:
                self.assertNotEqual(example["final"], "handoff")
                if example["final"] == "refuse":
                    self.assertIn("没有可接受的替代证据", example["situation"])
        self.assertEqual(finals["failed_source_with_alternate_evidence"], "answer")
        self.assertTrue(any("没有可接受的替代证据时，final 是 refuse" in r for r in OUTCOMES["rules"]))
        self.assertTrue(any(r.startswith("工具故障不是 handoff") for r in OUTCOMES["rules"]))
        self.assertIn("不是「因为工具故障所以建议人工」", OUTCOMES["definitions"]["handoff"])

    def test_negative_business_results_are_answer(self):
        answer = OUTCOMES["definitions"]["answer"]
        for phrase in ("超过期限", "库存为 0", "没有已有售后单", "未找到可访问的订单"):
            self.assertIn(phrase, answer)

    def test_domain_spec_mirrors_the_contract(self):
        section = DOMAIN_SPEC.split("## 8. 最终结论类型", 1)[1].split("\n## ", 1)[0]
        for value, text in OUTCOMES["definitions"].items():
            self.assertIn("| `" + value + "` | " + text + " |", section)
        for rule in OUTCOMES["rules"]:
            self.assertIn("- " + rule, section)
        rows = re.findall(r"^\| ([a-z_]+) \| (.+) \| (answer|refuse|handoff|boundary) \|$", section, re.M)
        self.assertEqual(rows, [(e["id"], e["situation"], e["final"]) for e in OUTCOMES["examples"]])

    def test_untrusted_observation_text_is_documented(self):
        self.assertIn("不受信任的业务观测数据", DOMAIN_SPEC)
        self.assertIn("after_sales_case.reason", DOMAIN_SPEC)


class ManifestTests(unittest.TestCase):
    manifest = bundle.load_manifest()

    def test_manifest_verifies(self):
        contents = bundle.verify_manifest(self.manifest)
        self.assertEqual(len(contents), len(self.manifest["files"]))

    def test_manifest_is_current_and_deterministic(self):
        self.assertEqual(self.manifest["base_commit"], BASE_COMMIT)
        self.assertEqual(bundle.build_manifest(BASE_COMMIT), self.manifest)
        self.assertEqual(bundle.build_manifest(BASE_COMMIT), bundle.build_manifest(BASE_COMMIT))

    def test_allowed_inputs_exactly(self):
        paths = [f["path"] for f in self.manifest["files"]]
        policies = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "policy_sources").glob("*.md"))
        self.assertEqual(paths, sorted(list(bundle.ALLOWED_INPUTS) + policies))
        self.assertEqual(len(policies), 6)

    def test_no_forbidden_inputs(self):
        for entry in self.manifest["files"]:
            path = entry["path"]
            with self.subTest(path=path):
                self.assertEqual(bundle.denied_tokens(path), set())
                lowered = path.lower()
                if not lowered.startswith("policy_sources/"):
                    # quality-handoff.md is a policy rule, not a handoff document.
                    self.assertNotIn("handoff", lowered)
                for fragment in ("planner", "router", "/dev", "validation", "test",
                                 "diagnostic", "orchestration/", "agent", "trace", "eval_env",
                                 "eval/runs", "eval/artifacts", ".git"):
                    self.assertNotIn(fragment, lowered)
                self.assertFalse(lowered.startswith(("tests/", "eval/diagnostics", "eval/stage")))
                self.assertNotEqual(lowered, "handoff.md")

    def test_denylist_catches_forbidden_paths(self):
        for path in ("orchestration/planner.py", "HANDOFF.md", "docs/v2/stage4.3-handoff.md",
                     "tests/test_v2_policy_lifecycle.py", "eval/diagnostics/x.json",
                     "eval/v2/dev.json", "eval/v2/validation.json", "eval/v2/holdout.json",
                     "orchestration/routing.py"):
            with self.subTest(path=path):
                self.assertNotEqual(bundle.denied_tokens(path), set())
        for path in ("../outside.md", "/abs.md", ".git/config", "a\\b.md", "C:/x.md"):
            with self.subTest(path=path):
                with self.assertRaises(bundle.BundleRefused):
                    bundle.check_relative(path)

    def test_content_digest_covers_only_path_and_hash(self):
        files = copy.deepcopy(self.manifest["files"])
        digest = bundle.content_digest(files)
        self.assertEqual(digest, self.manifest["content_digest"])
        for entry in files:
            entry["bytes"] = 0
        self.assertEqual(bundle.content_digest(files), digest)
        self.assertEqual(bundle.build_manifest("0" * 40)["content_digest"], digest)
        files[0]["sha256"] = "0" * 64
        self.assertNotEqual(bundle.content_digest(files), digest)

    def test_manifest_has_no_absolute_or_local_paths(self):
        text = (ROOT / bundle.MANIFEST_RELATIVE).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]|/Users/|/home/|tmp/|\\\\", text))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_export_matches_manifest_exactly(self):
        out = self.tmp / "bundle"
        result = bundle.export_bundle(out)
        manifest = bundle.load_manifest()
        written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        self.assertEqual(written, sorted([f["path"] for f in manifest["files"]] + [bundle.BUNDLE_MANIFEST_NAME]))
        for entry in manifest["files"]:
            self.assertEqual(bundle.sha256_hex((out / entry["path"]).read_bytes()), entry["sha256"])
        on_disk = json.loads((out / bundle.BUNDLE_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk, result)
        self.assertEqual(on_disk["content_digest"], manifest["content_digest"])
        self.assertFalse((out / ".git").exists())
        self.assertFalse((out / "tests").exists())
        self.assertFalse((out / "HANDOFF.md").exists())
        self.assertFalse((out / "orchestration").exists())

    def test_export_is_deterministic(self):
        first, second = self.tmp / "a", self.tmp / "b"
        bundle.export_bundle(first)
        bundle.export_bundle(second)
        read = lambda d: {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
        self.assertEqual(read(first), read(second))

    def test_refuses_destination_inside_repo(self):
        for target in (ROOT, ROOT / "tmp" / "holdout-author-bundle", ROOT / "eval" / "v2", ROOT.parent):
            with self.subTest(target=target.name):
                with self.assertRaises(bundle.BundleRefused):
                    bundle.check_destination(target)
        self.assertFalse((ROOT / "tmp" / "holdout-author-bundle").exists())

    def test_refuses_non_empty_destination(self):
        (self.tmp / "existing.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(bundle.BundleRefused):
            bundle.export_bundle(self.tmp)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["existing.txt"])

    def fake_root(self) -> Path:
        root = self.tmp / "repo"
        for entry in bundle.load_manifest()["files"] + [{"path": bundle.MANIFEST_RELATIVE}]:
            target = root / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / entry["path"], target)
        return root

    def test_refuses_tampered_source_and_writes_nothing(self):
        root = self.fake_root()
        seed = root / "system_fixtures/aftersales_demo_seed.sql"
        seed.write_bytes(seed.read_bytes() + b"\n-- changed\n")
        out = self.tmp / "out"
        with self.assertRaises(bundle.BundleRefused):
            bundle.export_bundle(out, root)
        self.assertFalse(out.exists() and any(out.iterdir()))

    def test_refuses_manifest_with_extra_or_missing_path(self):
        root = self.fake_root()
        manifest = bundle.load_manifest(root)
        extra = copy.deepcopy(manifest)
        extra["files"].append({"path": "zz/extra.md", "sha256": "0" * 64, "bytes": 0})
        extra["content_digest"] = bundle.content_digest(extra["files"])
        missing = copy.deepcopy(manifest)
        missing["files"].pop(0)
        missing["content_digest"] = bundle.content_digest(missing["files"])
        for variant in (extra, missing):
            with self.assertRaises(bundle.BundleRefused):
                bundle.verify_manifest(variant, root)

    def test_line_endings_do_not_change_hashes(self):
        root = self.fake_root()
        spec = root / "docs/v2/holdout-domain-spec.md"
        data = spec.read_bytes().replace(b"\r\n", b"\n")
        spec.write_bytes(data.replace(b"\n", b"\r\n"))
        bundle.verify_manifest(bundle.load_manifest(root), root)

    def test_cli_refusal_exit_code(self):
        (self.tmp / "x").write_text("x", encoding="utf-8")
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            self.assertEqual(bundle.main([str(self.tmp)]), 1)
        self.assertIn("refused", err.getvalue())
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
