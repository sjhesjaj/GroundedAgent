"""C-D1: an executed exchange takes its units from the target SKU - product runtime only.

docs/v2/exchange-stock-deduction.md. Offline. Runs on SQLite, and on PostgreSQL
with AFTERSALES_TEST_PG_URL (tests.stage6_support). Stage 6 and its evaluations
keep S6-D10 (stock is a precondition only): their gateways leave the switch off.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest import mock

from aftersales.action_gateway import ActionGateway
from aftersales.action_outcome import ActionStatus
from aftersales.action_policy import S6_RISK_POLICY, ActionRiskPolicy
from aftersales.action_store import ActionStore
from tests.stage6_support import (
    EXCHANGE_ARGS,
    STOCK_TSHIRT_L,
    VIRTUAL_NOW,
    Stage6Database,
    approval,
    audit_trail,
    identity,
    pending_row,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = VIRTUAL_NOW.isoformat()
STOCK_BEFORE = (5, 13, "2026-11-14T20:00:00+08:00")  # STOCK_TSHIRT_L
UNTOUCHED = ("orders", "order_items", "logistics", "inventory", "after_sales_cases",
             "human_handoff_tickets", "action_receipts")
# Exchanges need approval under this policy, so T2 executes them.
APPROVE_EXCHANGES = ActionRiskPolicy(
    version="s6-risk-test", dispositions={**S6_RISK_POLICY.dispositions,
                                          "create_exchange": "REQUIRE_APPROVAL"})


class DeductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Stage6Database()
        self.addCleanup(self.db.close)
        self.db.execute(STOCK_TSHIRT_L)  # SKU-TSHIRT-L: 5 units; OI-1001-1 holds 2

    def gateway(self, **options) -> ActionGateway:
        return self.db.gateway(deduct_exchange_stock=True, **options)

    def exchange(self, gateway: ActionGateway | None = None):
        return (gateway or self.gateway()).start_action(
            identity(), validate("create_exchange", EXCHANGE_ARGS))

    def stock(self) -> tuple:
        return tuple(self.db.rows("SELECT available_qty, version, updated_at FROM inventory"
                                  " WHERE sku = 'SKU-TSHIRT-L'")[0])

    def exchanges(self) -> int:
        """Exchange cases the gateway wrote (the seed holds one of its own)."""
        return self.db.rows("SELECT count(*) FROM after_sales_cases WHERE type = 'exchange'"
                            " AND case_id LIKE 'AS6-%'")[0][0]

    def test_an_executed_exchange_takes_its_units(self):
        outcome = self.exchange()
        self.assertIs(outcome.status, ActionStatus.EXECUTED)
        self.assertEqual(self.stock(), (3, 14, NOW))
        self.assertEqual((self.exchanges(), self.db.count("action_receipts")), (1, 1))

    def test_a_replay_takes_nothing_more(self):
        self.exchange()
        again = self.exchange()
        self.assertEqual((again.status, again.idempotent_replay), (ActionStatus.EXECUTED, True))
        self.assertEqual(self.stock(), (3, 14, NOW))
        self.assertEqual(self.exchanges(), 1)

    def test_too_little_stock_is_denied_and_writes_nothing(self):
        self.db.execute("UPDATE inventory SET available_qty = 1, version = 14 WHERE sku = 'SKU-TSHIRT-L'")
        before = self.db.dump()
        outcome = self.exchange()
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.DENIED, "inventory_unavailable"))
        after = self.db.dump()
        for table in UNTOUCHED:
            self.assertEqual(after[table], before[table], table)

    def test_a_shortfall_at_the_write_rolls_the_whole_exchange_back(self):
        before = self.db.dump()
        with mock.patch.object(ActionStore, "deduct_exchange_stock", return_value=False):
            outcome = self.exchange()
        self.assertEqual((outcome.status, outcome.code, outcome.guard.decision),
                         (ActionStatus.DENIED, "inventory_unavailable", "DENY"))
        after = self.db.dump()
        for table in UNTOUCHED + ("pending_actions",):
            self.assertEqual(after[table], before[table], table)
        # Only the compensating audit row survives; guard.evaluated rolled back with the rest.
        self.assertEqual(audit_trail(self.db),
                         [("action.not_executed", None, "DENIED", "inventory_unavailable")])

    def test_an_approved_exchange_takes_its_units_when_it_executes(self):
        gateway = self.gateway(risk_policy=APPROVE_EXCHANGES)
        waiting = self.exchange(gateway)
        self.assertIs(waiting.status, ActionStatus.WAITING_APPROVAL)
        self.assertEqual(self.stock(), STOCK_BEFORE)
        done = gateway.resume_action(approval(waiting.pending_action_id))
        self.assertIs(done.status, ActionStatus.EXECUTED)
        self.assertEqual(self.stock(), (3, 14, NOW))

    def test_a_shortfall_at_approved_execution_denies_the_pending(self):
        gateway = self.gateway(risk_policy=APPROVE_EXCHANGES)
        pending_id = self.exchange(gateway).pending_action_id
        self.assertTrue(gateway.record_decision(approval(pending_id)).approval_recorded)
        with mock.patch.object(ActionStore, "deduct_exchange_stock", return_value=False):
            outcome = gateway.execute_approved(pending_id)
        self.assertEqual((outcome.status, outcome.code), (ActionStatus.DENIED, "inventory_unavailable"))
        row = pending_row(self.db, pending_id)
        self.assertEqual((row["status"], row["outcome_code"]), ("DENIED", "inventory_unavailable"))
        self.assertEqual((self.exchanges(), self.db.count("action_receipts")), (0, 0))
        self.assertEqual(self.stock(), STOCK_BEFORE)
        self.assertEqual(audit_trail(self.db, pending_id)[-1],
                         ("action.not_executed", "resume", "DENIED", "inventory_unavailable"))

    def test_the_store_takes_stock_only_at_the_version_and_amount_seen(self):
        connection = self.db.connect()
        try:
            store = ActionStore(connection)
            take = store.deduct_exchange_stock
            self.assertFalse(take(sku="SKU-TSHIRT-L", quantity=2, expected_version=12, at=NOW))
            self.assertFalse(take(sku="SKU-TSHIRT-L", quantity=6, expected_version=13, at=NOW))
            self.assertFalse(take(sku="SKU-NOPE", quantity=1, expected_version=13, at=NOW))
            for quantity in (0, -1, True, "2"):
                with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                    take(sku="SKU-TSHIRT-L", quantity=quantity, expected_version=13, at=NOW)
            self.assertTrue(take(sku="SKU-TSHIRT-L", quantity=5, expected_version=13, at=NOW))
        finally:
            connection.close()
        self.assertEqual(self.stock(), (0, 14, NOW))


class SwitchTests(unittest.TestCase):
    def test_off_by_default_keeps_the_stage6_behaviour(self):
        with Stage6Database() as db:
            db.execute(STOCK_TSHIRT_L)
            outcome = db.gateway().start_action(identity(), validate("create_exchange", EXCHANGE_ARGS))
            self.assertIs(outcome.status, ActionStatus.EXECUTED)
            self.assertEqual(tuple(db.rows("SELECT available_qty, version, updated_at FROM inventory"
                                           " WHERE sku = 'SKU-TSHIRT-L'")[0]), STOCK_BEFORE)

    def test_the_switch_is_a_bool(self):
        with Stage6Database() as db:
            for value in ("yes", 1, None):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    db.gateway(deduct_exchange_stock=value)

    def test_only_the_product_store_turns_it_on(self):
        product = (REPO_ROOT / "aftersales_service" / "demo_store.py").read_text(encoding="utf-8")
        self.assertEqual(product.count("deduct_exchange_stock=True"), 1)
        from aftersales_service.demo_store import DemoStore
        store = DemoStore()
        try:
            self.assertTrue(store.gateway._deduct_exchange_stock)
        finally:
            store.close()

    def test_no_evaluation_runtime_turns_it_on(self):
        sources = [path for folder in ("eval_v2", "eval_m3", "eval_m1", "eval")
                   for path in sorted((REPO_ROOT / folder).rglob("*.py"))]
        self.assertIn(REPO_ROOT / "eval_v2" / "stage6_runtime.py", sources)
        self.assertIn(REPO_ROOT / "eval_m3" / "runner.py", sources)
        for path in sources:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                self.assertNotIn("deduct_exchange_stock", text)
        # Both evaluation gateways are built here, without the keyword: off.
        for path in (REPO_ROOT / "eval_v2" / "stage6_runtime.py", REPO_ROOT / "eval_m3" / "runner.py"):
            calls = [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                     if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ActionGateway"]
            self.assertEqual(len(calls), 1, path.name)


if __name__ == "__main__":
    unittest.main()
