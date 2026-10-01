"""Stage 6.1 action contracts, capability gate, ids, idempotency, schema (design §4, §5, §7-§9)."""

import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from aftersales.action_db import ACTION_SCHEMA_PATH, STAGE6_ACTION_TABLES, STAGE6_SEED_PATH
from aftersales.action_errors import (
    ActionContractError,
    ActionValidationError,
    CapabilityConfigurationError,
)
from aftersales.action_policy import RISK_POLICY_VERSION, S6_RISK_POLICY, ActionRiskPolicy
from aftersales.actions import (
    ACTION_NAMES,
    ACTION_SPEC_VERSION,
    APPROVAL_AND_CONTROL_ARGUMENTS,
    EXCHANGE_REASON_CODES,
    FORBIDDEN_ACTION_ARGUMENT_NAMES,
    HANDOFF_TRIGGERS,
    IDENTITY_AND_AUTHORITY_ARGUMENTS,
    REASON_HANDOFF_TRIGGER,
    REASON_LABELS,
    RETURN_REASON_CODES,
    SYSTEM_OWNED_ID_ARGUMENTS,
    ActionIntentValidator,
    ActionParameter,
    ActionRegistry,
    ActionSpec,
    ValidatedAction,
    build_action_registry,
    canonical_args,
)
from aftersales.arguments import IDENTITY_ARGUMENT_NAMES
from aftersales.capabilities import DEPLOYMENT_ACTIONS, DEPLOYMENT_READ_TOOLS, CapabilityGate
from aftersales.ids import (
    ID_PATTERN,
    DeterministicIdProvider,
    IdKind,
    RequestIdentity,
    UuidIdProvider,
    idempotency_key,
)
from aftersales.policy_source import canonical
from aftersales.registry import RUNTIME_TOOL_NAMES, ToolKind
from aftersales.schema import SCHEMA_PATH
from eval_v2.control import canonical_json

from tests.stage6_support import (
    EXCHANGE_ARGS,
    HANDOFF_ARGS,
    RETURN_ARGS,
    Stage6Database,
    identity,
    validate,
)
from tests.test_v2_clock import clock_violations, sql_clock_violations

REPO_ROOT = Path(__file__).resolve().parent.parent

FROZEN_PARAMETERS = {
    "create_return": (("order_id", None), ("order_item_id", None),
                      ("reason_code", ("no_longer_wanted", "size_or_spec_mismatch", "quality_issue"))),
    "create_exchange": (("order_id", None), ("order_item_id", None), ("target_sku", None),
                        ("reason_code", ("size_or_spec_mismatch", "quality_issue"))),
    "escalate_to_human": (("order_id", None), ("order_item_id", None),
                          ("handoff_trigger", ("quality_dispute",))),
}


class ActionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_action_registry()

    def test_exactly_three_frozen_actions(self):
        self.assertEqual(ACTION_NAMES, ("create_return", "create_exchange", "escalate_to_human"))
        self.assertEqual(self.registry.names(), ACTION_NAMES)
        self.assertEqual(len(self.registry), 3)
        self.assertEqual(ACTION_SPEC_VERSION, "s6-actions/1")

    def test_every_action_schema_and_enum_is_frozen(self):
        for name, parameters in FROZEN_PARAMETERS.items():
            with self.subTest(action=name):
                spec = self.registry.get(name)
                self.assertEqual(tuple((p.name, p.enum) for p in spec.parameters), parameters)
                schema = spec.input_schema()
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(schema["required"], [p for p, _ in parameters])
                for parameter, enum in parameters:
                    prop = schema["properties"][parameter]
                    self.assertEqual(prop["type"], "string")
                    self.assertEqual(prop["minLength"], 1)
                    self.assertEqual(prop.get("enum"), None if enum is None else list(enum))

    def test_vocabularies_are_frozen(self):
        self.assertEqual(RETURN_REASON_CODES, ("no_longer_wanted", "size_or_spec_mismatch", "quality_issue"))
        self.assertEqual(EXCHANGE_REASON_CODES, ("size_or_spec_mismatch", "quality_issue"))
        self.assertEqual(HANDOFF_TRIGGERS, ("quality_dispute",))
        self.assertEqual(dict(REASON_LABELS), {
            "no_longer_wanted": "不想要了（无理由退货）",
            "size_or_spec_mismatch": "尺码或规格不合适",
            "quality_issue": "商品质量问题",
        })
        self.assertEqual(dict(REASON_HANDOFF_TRIGGER), {"quality_issue": "quality_dispute"})
        with self.assertRaises(TypeError):
            REASON_LABELS["x"] = "y"

    def test_resource_types(self):
        self.assertEqual({spec.name: spec.resource_type for spec in self.registry}, {
            "create_return": "after_sales_case", "create_exchange": "after_sales_case",
            "escalate_to_human": "human_handoff_ticket"})

    def test_specs_are_actions_not_tools(self):
        for spec in self.registry:
            self.assertIs(spec.kind, ToolKind.BUSINESS_ACTION)
            self.assertIs(spec.side_effect, True)
            self.assertNotIn("handler", {f for f in vars(spec)})
            self.assertEqual(set(spec.to_dict()), {"name", "description", "kind", "side_effect",
                                                   "resource_type", "input_schema"})

    def test_no_spec_may_declare_a_forbidden_parameter(self):
        for name in sorted(FORBIDDEN_ACTION_ARGUMENT_NAMES):
            with self.subTest(parameter=name):
                with self.assertRaises(ValueError):
                    ActionSpec(name="create_return", description="d", resource_type="after_sales_case",
                               parameters=(ActionParameter(name="order_id", description="d"),
                                           ActionParameter(name="order_item_id", description="d"),
                                           ActionParameter(name=name, description="d")))

    def test_registry_accepts_action_specs_only_and_rejects_duplicates(self):
        spec = self.registry.get("create_return")
        with self.assertRaises(ValueError):
            ActionRegistry([spec, spec])
        with self.assertRaises(ValueError):
            ActionRegistry([object()])

    def test_forbidden_vocabulary_is_the_frozen_union(self):
        self.assertEqual(IDENTITY_AND_AUTHORITY_ARGUMENTS, {
            "customer_id", "persona_id", "subject_id", "user_id", "role", "is_admin", "admin",
            "manager", "operator", "approver", "approver_ref"})
        self.assertEqual(APPROVAL_AND_CONTROL_ARGUMENTS, {
            "approval_required", "approved", "approval", "approval_decision", "skip_approval",
            "force", "override", "decision", "status"})
        self.assertEqual(SYSTEM_OWNED_ID_ARGUMENTS, {
            "idempotency_key", "request_id", "run_id", "case_id", "ticket_id",
            "pending_action_id", "receipt_id"})
        self.assertLessEqual(IDENTITY_ARGUMENT_NAMES, FORBIDDEN_ACTION_ARGUMENT_NAMES)


class ValidatorTests(unittest.TestCase):
    def validator(self, actions=ACTION_NAMES):
        return ActionIntentValidator(build_action_registry(), actions)

    def assert_diagnostic(self, diagnostic, name, args, actions=ACTION_NAMES):
        with self.assertRaises(ActionValidationError) as caught:
            self.validator(actions).validate(name, args)
        self.assertEqual(caught.exception.diagnostic, diagnostic)

    def test_valid_actions(self):
        for name, args in (("create_return", RETURN_ARGS), ("create_exchange", EXCHANGE_ARGS),
                           ("escalate_to_human", HANDOFF_ARGS)):
            with self.subTest(action=name):
                action = self.validator().validate(name, dict(args))
                self.assertEqual(dict(action.args), args)
                self.assertEqual(action.target_order_id, args["order_id"])
                self.assertEqual(action.target_order_item_id, args["order_item_id"])

    def test_unknown_and_not_granted_actions(self):
        self.assert_diagnostic("unknown_function", "refund_money", {"order_id": "ORD-1001"})
        self.assert_diagnostic("unknown_function", None, {})
        self.assert_diagnostic("action_not_allowed", "create_return", RETURN_ARGS,
                               actions=("create_exchange",))

    def test_identity_arguments_are_rejected_first(self):
        for name in sorted(IDENTITY_ARGUMENT_NAMES):
            with self.subTest(argument=name):
                # Even with a missing parameter, an unknown key and a forbidden one.
                args = {"order_id": "ORD-1001", name: "CUST-002", "approved": "true", "x": "y"}
                self.assert_diagnostic("identity_argument", "create_return", args)

    def test_forbidden_arguments_are_rejected_before_shape(self):
        for name in sorted(FORBIDDEN_ACTION_ARGUMENT_NAMES - IDENTITY_ARGUMENT_NAMES):
            with self.subTest(argument=name):
                self.assert_diagnostic("forbidden_action_argument", "create_return",
                                       {**RETURN_ARGS, name: "x"})
                self.assert_diagnostic("forbidden_action_argument", "create_exchange", {name: "x"})

    def test_shape_and_values(self):
        for args in ({}, {"order_id": "ORD-1001"}, {**RETURN_ARGS, "note": "x"},
                     {**RETURN_ARGS, "reason_code": "free text reason"},
                     {**RETURN_ARGS, "reason_code": "size_or_spec_mismatch "},
                     {**RETURN_ARGS, "order_id": "   "}, {**RETURN_ARGS, "order_id": 1001},
                     {**RETURN_ARGS, "order_id": "O" * 129}, ["order_id"], None,
                     {1: "x"}):
            with self.subTest(args=repr(args)[:40]):
                self.assert_diagnostic("invalid_action_arguments", "create_return", args)
        # create_exchange does not accept the return-only reason.
        self.assert_diagnostic("invalid_action_arguments", "create_exchange",
                               {**EXCHANGE_ARGS, "reason_code": "no_longer_wanted"})
        self.assert_diagnostic("invalid_action_arguments", "escalate_to_human",
                               {**HANDOFF_ARGS, "handoff_trigger": "angry_customer"})

    def test_values_are_verbatim(self):
        action = self.validator().validate("create_exchange", {**EXCHANGE_ARGS, "target_sku": " SKU-X "})
        self.assertEqual(action.args["target_sku"], " SKU-X ")

    def test_canonical_args_match_the_eval_canonicalizer(self):
        action = validate("create_exchange", EXCHANGE_ARGS)
        self.assertEqual(action.canonical_args_json, canonical_json(dict(EXCHANGE_ARGS)))
        self.assertEqual(action.canonical_args_json, canonical_args(EXCHANGE_ARGS))
        self.assertEqual(action.args_sha256,
                         hashlib.sha256(action.canonical_args_json.encode("utf-8")).hexdigest())
        for value in ({"b": "中文", "a": [1, True, None]}, {"z": {"y": "x"}}, ["退货", 2]):
            self.assertEqual(canonical(value), canonical_json(value))

    def test_validated_action_cannot_be_tampered(self):
        action = validate("create_return", RETURN_ARGS)
        with self.assertRaises(TypeError):
            action.args["order_id"] = "ORD-2001"
        with self.assertRaises(ActionContractError):
            ValidatedAction(action_name="create_return", args={**RETURN_ARGS, "order_id": "ORD-2001"},
                            canonical_args_json=action.canonical_args_json,
                            args_sha256=action.args_sha256, target_order_id="ORD-2001",
                            target_order_item_id="OI-1001-2")
        with self.assertRaises(ActionContractError):
            ValidatedAction(action_name="create_return", args=dict(RETURN_ARGS),
                            canonical_args_json=action.canonical_args_json,
                            args_sha256=action.args_sha256, target_order_id="ORD-1002",
                            target_order_item_id="OI-1001-2")

    def test_effective_actions_must_be_registered(self):
        with self.assertRaises(CapabilityConfigurationError):
            ActionIntentValidator(build_action_registry(), ("create_return", "refund_money"))


class CapabilityGateTests(unittest.TestCase):
    def test_deployment_upper_bound(self):
        self.assertEqual(DEPLOYMENT_READ_TOOLS, RUNTIME_TOOL_NAMES)
        self.assertEqual(DEPLOYMENT_ACTIONS, ACTION_NAMES)
        caps = CapabilityGate().narrow()
        self.assertEqual(caps.read_tools, RUNTIME_TOOL_NAMES)
        self.assertEqual(caps.actions, ACTION_NAMES)

    def test_the_gate_only_shrinks(self):
        gate = CapabilityGate(actions=("create_exchange", "escalate_to_human"))
        self.assertEqual(gate.narrow().actions, ("create_exchange", "escalate_to_human"))
        self.assertEqual(gate.narrow(actions=("escalate_to_human",)).actions, ("escalate_to_human",))
        self.assertEqual(gate.narrow(actions=()).actions, ())
        with self.assertRaises(CapabilityConfigurationError):
            gate.narrow(actions=("create_return",))  # outside this gate's static set

    def test_unknown_capabilities_are_configuration_errors(self):
        for kwargs in ({"actions": ("refund_money",)}, {"read_tools": ("drop_table",)},
                       {"actions": "create_return"}, {"actions": ("create_return", "create_return")}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CapabilityConfigurationError):
                    CapabilityGate(**kwargs)
                with self.assertRaises(CapabilityConfigurationError):
                    CapabilityGate().narrow(**kwargs)


class RiskPolicyTests(unittest.TestCase):
    def test_frozen_s6_risk_1(self):
        self.assertEqual(RISK_POLICY_VERSION, "s6-risk/1")
        self.assertEqual(S6_RISK_POLICY.version, "s6-risk/1")
        self.assertEqual(dict(S6_RISK_POLICY.dispositions), {
            "create_return": "REQUIRE_APPROVAL", "create_exchange": "ALLOW",
            "escalate_to_human": "ALLOW"})
        with self.assertRaises(TypeError):
            S6_RISK_POLICY.dispositions["create_return"] = "ALLOW"

    def test_risk_policy_must_cover_exactly_the_actions(self):
        for dispositions in ({"create_return": "ALLOW"},
                             {**S6_RISK_POLICY.dispositions, "refund_money": "ALLOW"},
                             {**S6_RISK_POLICY.dispositions, "create_return": "DENY"}):
            with self.subTest(dispositions=dispositions):
                with self.assertRaises(ValueError):
                    ActionRiskPolicy(version="x", dispositions=dispositions)


class IdentityAndIdTests(unittest.TestCase):
    def test_request_identity_format(self):
        RequestIdentity(persona_id="demo-a", request_id="req-1")
        RequestIdentity(persona_id="demo-a", request_id="A" + "b" * 63)
        for bad in ("", " req", "-req", "req 1", "req/1", "a" * 65, "请求", None, 1):
            with self.subTest(request_id=bad):
                with self.assertRaises(ValueError):
                    RequestIdentity(persona_id="demo-a", request_id=bad)
        with self.assertRaises(ValueError):
            RequestIdentity(persona_id="  ", request_id="req-1")

    def test_idempotency_key_binds_identity_action_and_args(self):
        action = validate("create_exchange", EXCHANGE_ARGS)
        key = idempotency_key(identity(), action)
        material = canonical({"schema": "s6-idempotency/1", "persona_id": "demo-a",
                              "request_id": "req-1", "action_name": "create_exchange",
                              "args": dict(sorted(EXCHANGE_ARGS.items()))})
        self.assertEqual(key, "s6k1-" + hashlib.sha256(material.encode("utf-8")).hexdigest())
        self.assertNotIn("CUST-001", material)
        variants = {
            idempotency_key(identity(request_id="req-2"), action),
            idempotency_key(identity(persona_id="demo-b"), action),
            idempotency_key(identity(), validate("create_exchange",
                                                 {**EXCHANGE_ARGS, "reason_code": "quality_issue"})),
        }
        self.assertNotIn(key, variants)
        self.assertEqual(len(variants), 3)
        self.assertEqual(key, idempotency_key(identity(), validate("create_exchange", dict(EXCHANGE_ARGS))))

    def test_deterministic_ids_follow_s6_ids_1(self):
        key = idempotency_key(identity(), validate("create_return", RETURN_ARGS))
        provider = DeterministicIdProvider("eval")
        for kind, prefix in ((IdKind.PENDING_ACTION, "PA"), (IdKind.AFTER_SALES_CASE, "AS6"),
                             (IdKind.HANDOFF_TICKET, "HT"), (IdKind.RECEIPT, "RC")):
            with self.subTest(kind=kind):
                material = canonical({"schema": "s6-ids/1", "namespace": "eval",
                                      "kind": prefix, "key": key})
                expected = prefix + "-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16].upper()
                self.assertEqual(provider.new_id(kind, key), expected)
                # Stateless: a fresh provider (a restart) gives the same id.
                self.assertEqual(DeterministicIdProvider("eval").new_id(kind, key), expected)
                self.assertRegex(expected, ID_PATTERN)
        self.assertNotEqual(provider.new_id(IdKind.RECEIPT, key),
                            DeterministicIdProvider("other").new_id(IdKind.RECEIPT, key))

    def test_ids_need_a_server_owned_key(self):
        for provider in (DeterministicIdProvider("eval"), UuidIdProvider()):
            for bad in ("", "AS6-1", "s6k1-XYZ", None):
                with self.subTest(provider=type(provider).__name__, key=bad):
                    with self.assertRaises(ValueError):
                        provider.new_id(IdKind.RECEIPT, bad)

    def test_uuid_provider_format(self):
        key = idempotency_key(identity(), validate("create_return", RETURN_ARGS))
        value = UuidIdProvider().new_id(IdKind.AFTER_SALES_CASE, key)
        self.assertRegex(value, ID_PATTERN)
        self.assertTrue(value.startswith("AS6-"))


class SchemaTests(unittest.TestCase):
    def test_stage4_schema_and_seed_are_untouched(self):
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        for table in STAGE6_ACTION_TABLES:
            self.assertNotIn(table, schema)
        seed = (REPO_ROOT / "system_fixtures" / "aftersales_demo_seed.sql").read_text(encoding="utf-8")
        self.assertNotIn("sku_variants", seed)

    def test_new_sql_reads_no_clock(self):
        for path in (ACTION_SCHEMA_PATH, STAGE6_SEED_PATH):
            with self.subTest(path=path.name):
                self.assertEqual(sql_clock_violations(path.read_text(encoding="utf-8")), [])
        for name in ("actions.py", "action_policy.py", "action_errors.py", "action_db.py",
                     "action_store.py", "action_gateway.py", "action_outcome.py",
                     "capabilities.py", "guard.py", "guard_state.py", "ids.py"):
            with self.subTest(module=name):
                source = (REPO_ROOT / "aftersales" / name).read_text(encoding="utf-8")
                self.assertEqual(clock_violations(source), [])

    def test_stage6_database_has_the_frozen_tables_and_seed(self):
        with Stage6Database() as db:
            tables = {row[0] for row in db.rows(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
            self.assertEqual(tables, {"orders", "order_items", "logistics", "inventory",
                                      "after_sales_cases", *STAGE6_ACTION_TABLES})
            indexes = {row[0] for row in db.rows(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 's6_%'")}
            self.assertEqual(indexes, {"s6_one_active_case_per_item", "s6_one_open_pending_per_item",
                                       "s6_one_open_ticket_per_item_trigger"})
            self.assertEqual(db.rows("SELECT sku, variant_group, version FROM sku_variants ORDER BY sku"), [
                ("SKU-EARBUDS", "SKU-EARBUDS", 1), ("SKU-KETTLE", "SKU-KETTLE", 1),
                ("SKU-MUG", "SKU-MUG", 1), ("SKU-SOCKS", "SKU-SOCKS", 1),
                ("SKU-TSHIRT-L", "TSHIRT", 1), ("SKU-TSHIRT-M", "TSHIRT", 1),
                ("SKU-UNDERWEAR-L", "SKU-UNDERWEAR-L", 1)])
            self.assertEqual(db.rows("PRAGMA journal_mode")[0][0], "wal")
            for table in ("human_handoff_tickets", "pending_actions", "action_receipts",
                          "action_audit_events"):
                self.assertEqual(db.count(table), 0)

    def test_partial_unique_indexes_enforce_one_active_record(self):
        with Stage6Database() as db:
            sql = ("INSERT INTO after_sales_cases (case_id, order_id, order_item_id, customer_id,"
                   " type, status, reason, created_at, updated_at, version) VALUES"
                   " (?, 'ORD-1001', 'OI-1001-2', 'CUST-001', 'return', ?, 'r',"
                   " '2026-11-10T10:00:00+08:00', '2026-11-10T10:00:00+08:00', 1)")
            db.execute(sql, ("AS-T1", "待处理"))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql, ("AS-T2", "处理中"))
            db.execute(sql, ("AS-T3", "已完成"))  # terminal cases do not count
            ticket = ("INSERT INTO human_handoff_tickets (ticket_id, order_id, order_item_id,"
                      " handoff_trigger, status, created_at, updated_at, version) VALUES"
                      " (?, 'ORD-1001', 'OI-1001-1', 'quality_dispute', ?,"
                      " '2026-11-10T10:00:00+08:00', '2026-11-10T10:00:00+08:00', 1)")
            db.execute(ticket, ("HT-T1", "待处理"))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(ticket, ("HT-T2", "处理中"))
            db.execute(ticket, ("HT-T3", "已关闭"))

    def test_receipt_and_pending_check_constraints(self):
        with Stage6Database() as db:
            base = ("INSERT INTO action_receipts (receipt_id, idempotency_key, request_id, persona_id,"
                    " action_name, args_json, args_sha256, result_status, resource_type, resource_id,"
                    " pending_action_id, guard_decision, guard_reason_code, snapshot_json,"
                    " snapshot_sha256, action_spec_version, risk_policy_version, policy_build_id,"
                    " executed_at) VALUES ('RC-1', 'k', 'r', 'p', ?, '{}', 'h', ?, ?, 'X', NULL, ?,"
                    " 'risk_policy_allows', '{}', 'h', 'v', 'v', 'b', 't')")
            for params in (("escalate_to_human", "EXECUTED", "after_sales_case", "ALLOW"),
                           ("create_exchange", "DENIED", "after_sales_case", "ALLOW"),
                           ("create_return", "EXECUTED", "after_sales_case", "REQUIRE_APPROVAL"),
                           ("refund_money", "EXECUTED", "after_sales_case", "ALLOW")):
                with self.subTest(params=params):
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(base, params)


if __name__ == "__main__":
    unittest.main()
