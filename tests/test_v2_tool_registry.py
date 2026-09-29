"""The runtime ToolRegistry: exactly five read-only tools (design §5, §6)."""

import ast
import unittest
from datetime import datetime
from pathlib import Path

from aftersales.errors import ToolNotReady
from aftersales.policy import (
    POLICY_TOOL_NAME,
    PolicyAdapterNotReady,
    PolicyRecord,
    PolicyRuleType,
    PolicySearchAdapter,
    make_policy_handler,
)
from aftersales.registry import (
    RUNTIME_TOOL_NAMES,
    ParameterSpec,
    ToolKind,
    ToolRegistry,
    ToolSpec,
    build_runtime_registry,
)

from tests.v2_support import make_context, memory_connection

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "search_after_sales_policy": (("query",), ToolKind.KNOWLEDGE_READ, False),
    "get_order": (("order_id",), ToolKind.BUSINESS_READ, True),
    "get_logistics": (("order_id",), ToolKind.BUSINESS_READ, True),
    "get_inventory": (("sku",), ToolKind.BUSINESS_READ, False),
    "get_after_sales_case": (("order_id",), ToolKind.BUSINESS_READ, True),
}

# Future actions live only in the domain specification (design §5, §6).
FUTURE_ACTIONS = ("create_return", "create_exchange", "escalate_to_human")


class RuntimeRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_runtime_registry()

    def test_registry_holds_exactly_the_five_read_only_tools(self):
        self.assertEqual(set(self.registry.names()), set(EXPECTED_TOOLS))
        self.assertEqual(len(self.registry), 5)
        self.assertEqual(self.registry.names(), RUNTIME_TOOL_NAMES)

    def test_every_registered_tool_is_side_effect_free(self):
        for spec in self.registry:
            with self.subTest(tool=spec.name):
                self.assertIs(spec.side_effect, False)

    def test_contracts_are_frozen(self):
        for name, (parameters, kind, identity_scoped) in EXPECTED_TOOLS.items():
            with self.subTest(tool=name):
                spec = self.registry.get(name)
                self.assertEqual(spec.parameter_names, parameters)
                self.assertIs(spec.kind, kind)
                self.assertIs(spec.identity_scoped, identity_scoped)
                self.assertTrue(callable(spec.handler))

    def test_input_schemas_are_closed(self):
        for spec in self.registry:
            with self.subTest(tool=spec.name):
                schema = spec.input_schema()
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(schema["required"], list(spec.parameter_names))
                self.assertEqual(set(schema["properties"]), set(spec.parameter_names))
                for prop in schema["properties"].values():
                    self.assertEqual(prop["type"], "string")
                    self.assertEqual(prop["minLength"], 1)
                # Identity is never part of a tool's schema.
                for identity in ("customer_id", "persona_id", "subject_id"):
                    self.assertNotIn(identity, schema["properties"])

    def test_to_dict_publishes_no_handler(self):
        for spec in self.registry:
            payload = spec.to_dict()
            self.assertEqual(
                set(payload),
                {"name", "description", "kind", "side_effect", "identity_scoped", "input_schema"},
            )

    def test_registry_is_immutable(self):
        with self.assertRaises(TypeError):
            self.registry._tools["get_order"] = None

    def test_no_future_action_exists_anywhere_in_code(self):
        sources = sorted((REPO_ROOT / "aftersales").glob("*.py")) + sorted(
            (REPO_ROOT / "orchestration").glob("*.py")
        ) + sorted(REPO_ROOT.glob("*.py"))
        self.assertTrue(sources)
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for action in FUTURE_ACTIONS:
                with self.subTest(path=path.name, action=action):
                    self.assertNotIn(action, text)
        for action in FUTURE_ACTIONS:
            self.assertNotIn(action, self.registry)


class RegistryValidationTests(unittest.TestCase):
    def spec(self, **overrides):
        values = dict(
            name="t", description="d", kind=ToolKind.BUSINESS_READ,
            parameters=(ParameterSpec(name="p", description="d"),),
            side_effect=False, identity_scoped=False, handler=lambda context, arguments: None,
        )
        values.update(overrides)
        return ToolSpec(**values)

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ValueError):
            ToolRegistry([self.spec(), self.spec()])

    def test_non_spec_entries_are_rejected(self):
        with self.assertRaises(ValueError):
            ToolRegistry([{"name": "t"}])

    def test_spec_fields_are_validated(self):
        bad = {
            "blank_name": dict(name=" "),
            "kind_is_text": dict(kind="business_read"),
            "parameters_is_list": dict(parameters=[ParameterSpec(name="p", description="d")]),
            "duplicate_parameter": dict(parameters=(ParameterSpec(name="p", description="d"),) * 2),
            "side_effect_not_bool": dict(side_effect=0),
            "identity_not_bool": dict(identity_scoped="yes"),
            "handler_not_callable": dict(handler="get_order"),
        }
        for name, overrides in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.spec(**overrides)


class PolicyContractTests(unittest.TestCase):
    def test_not_ready_adapter_raises_rather_than_returning_policy_text(self):
        adapter = PolicyAdapterNotReady()
        self.assertIsInstance(adapter, PolicySearchAdapter)
        with self.assertRaises(ToolNotReady):
            adapter.search("退货", as_of=datetime(2026, 11, 15).astimezone())

    def test_handler_rejects_a_non_adapter(self):
        with self.assertRaises(ValueError):
            make_policy_handler(object())

    def test_policy_module_hardcodes_no_policy_text(self):
        source = (REPO_ROOT / "aftersales" / "policy.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for word in ("天内", "无理由", "可退", "不可退"):
                    self.assertNotIn(word, node.value)

    def test_policy_tool_is_bound_to_the_composition_roots_adapter(self):
        connection = memory_connection()
        self.addCleanup(connection.close)
        calls = []

        class Adapter:
            def search(self, query, *, as_of):
                calls.append(query)
                raise ToolNotReady("stub")

        registry = build_runtime_registry(Adapter())
        spec = registry.get(POLICY_TOOL_NAME)
        with self.assertRaises(ToolNotReady):
            spec.handler(make_context(connection), {"query": "q"})
        self.assertEqual(calls, ["q"])

    def valid_record(self, **overrides):
        values = dict(
            policy_id="P-RETURN-7D", version="1", title="t", rule_type=PolicyRuleType.RETURN_WINDOW,
            scope=(),
            params={
                "window_days": 7, "start_event": "delivered",
                "counting_rule": "natural_days_from_next_day", "utc_offset": "+08:00",
            },
            effective_from="2026-01-01T00:00:00+08:00", effective_to=None,
            source_doc="rules.md", locator="rules.md#return", build_id="build-1",
        )
        values.update(overrides)
        return PolicyRecord(**values)

    def test_policy_record_contract(self):
        record = self.valid_record()
        self.assertEqual(record.rule_type, PolicyRuleType.RETURN_WINDOW)
        self.assertEqual(
            {member.value for member in PolicyRuleType},
            {"return_window", "exchange_window", "non_returnable", "handoff"},
        )

    def test_policy_record_rejects_invalid_fields(self):
        bad = {
            "naive_from": dict(effective_from="2026-01-01T00:00:00"),
            "to_before_from": dict(effective_to="2025-01-01T00:00:00+08:00"),
            "blank_build": dict(build_id=""),
            "rule_type_text": dict(rule_type="return_window"),
            "scope_list": dict(scope=["服装"]),
            "params_not_mapping": dict(params=[("window_days", 7)]),
        }
        for name, overrides in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.valid_record(**overrides)


if __name__ == "__main__":
    unittest.main()
