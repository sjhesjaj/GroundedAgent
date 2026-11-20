"""The read-only order views (routes.py, service.py, demo_store.py).

    GET /api/aftersales/orders
        the token's customer's own orders, lines and packages (customer only)
    GET /api/aftersales/operator/sessions/{id}/actions/{pending_action_id}/order-summary
        the order and line one pending action of that session names (operator only)

Display only: neither view feeds the agent, the Guard or a decision, and
reading one changes nothing. Offline, on the seeded demo database.
"""

from __future__ import annotations

import sqlite3
import unittest

from tests.test_aftersales_auth import PENDING, SESSION, AuthTestCase
from tests.test_aftersales_persistence import PersistentTestCase
from tests.test_aftersales_service import RETURN_ARGS, call, decision

ORDERS = "/api/aftersales/orders"
FORBIDDEN = (403, {"detail": {"code": "forbidden"}})
NOT_AUTHENTICATED = (401, {"detail": {"code": "not_authenticated"}})


def summary_path(session_id: str, pending_action_id: str) -> str:
    return ("/api/aftersales/operator/sessions/" + session_id + "/actions/"
            + pending_action_id + "/order-summary")


class OrderViewCase(AuthTestCase):
    def answer(self, response) -> tuple[int, dict]:
        return response.status_code, response.json()

    def orders(self, username: str) -> dict:
        response = self.raw.get(ORDERS, headers=self.as_(username))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("CUST-", response.text)   # the customer id never leaves the server
        return response.json()

    def summary(self, session_id: str, pending_action_id: str) -> dict:
        response = self.raw.get(summary_path(session_id, pending_action_id), headers=self.as_("operator"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("CUST-", response.text)
        return response.json()

    def waiting_b(self) -> tuple[str, str]:
        """customer-b's own return request (the shared `waiting` is customer-a's ORD-1001)."""
        session_id = self.session("demo-b")
        args = {"order_id": "ORD-2001", "order_item_id": "OI-2001-2", "reason_code": "no_longer_wanted"}
        payload = self.say(session_id, "ORD-2001 里的袜子不想要了，帮我退货",
                           decision(call("get_order", {"order_id": "ORD-2001"})),
                           decision(call("create_return", args)))
        self.assertEqual(payload["status"], "WAITING_APPROVAL")
        return session_id, payload["pending_action_id"]

    def state(self, session_id: str, pending_action_id: str) -> tuple:
        """Everything a read must leave alone."""
        return (self.view(session_id), self.pending(pending_action_id),
                self.count("action_receipts"), self.count("after_sales_cases"))


# --------------------------------------------------------------------------
# GET /orders
# --------------------------------------------------------------------------


class CustomerOrdersTests(OrderViewCase):
    def test_without_a_token_it_is_401(self):
        self.assertEqual(self.answer(self.raw.get(ORDERS)), NOT_AUTHENTICATED)

    def test_an_operator_is_refused(self):
        self.assertEqual(self.answer(self.raw.get(ORDERS, headers=self.as_("operator"))), FORBIDDEN)

    def test_each_customer_gets_only_their_own_orders(self):
        a = self.orders("customer-a")
        b = self.orders("customer-b")
        self.assertEqual([order["order_id"] for order in a["orders"]],
                         ["ORD-1004", "ORD-1003", "ORD-1002", "ORD-1001"])
        self.assertEqual([order["order_id"] for order in b["orders"]], ["ORD-2002", "ORD-2001"])
        for orders, foreign in ((a, ("ORD-2", "OI-2", "YT2001", "YT2002")),
                                (b, ("ORD-1", "OI-1", "SF100", "YT1004B"))):
            text = str(orders)
            for marker in foreign:
                self.assertNotIn(marker, text)
        self.assertEqual(a["business_time"], "2026-11-15T10:00:00+08:00")

    def test_a_customer_id_in_the_query_changes_nothing(self):
        own = self.orders("customer-a")
        response = self.raw.get(ORDERS + "?customer_id=CUST-002&persona_id=demo-b",
                                headers=self.as_("customer-a"))
        self.assertEqual((response.status_code, response.json()), (200, own))

    def test_an_order_carries_its_lines_and_amounts(self):
        orders = {order["order_id"]: order for order in self.orders("customer-a")["orders"]}
        order = orders["ORD-1001"]
        self.assertEqual({key: order[key] for key in ("status", "paid_at", "total_amount")},
                         {"status": "已签收", "paid_at": "2026-11-01T20:15:00+08:00",
                          "total_amount": "298.00"})
        self.assertEqual(order["items"], [
            {"order_item_id": "OI-1001-1", "sku": "SKU-TSHIRT-M", "product_name": "纯棉T恤 M码",
             "category": "服装", "quantity": 2, "unit_price": "99.00", "subtotal": "198.00"},
            {"order_item_id": "OI-1001-2", "sku": "SKU-UNDERWEAR-L", "product_name": "棉质内衣 L码",
             "category": "贴身衣物", "quantity": 1, "unit_price": "100.00", "subtotal": "100.00"},
        ])
        self.assertEqual(set(order), {"order_id", "status", "paid_at", "total_amount", "items", "packages"})

    def test_packages_belong_to_the_order_not_to_a_line(self):
        orders = {order["order_id"]: order for order in self.orders("customer-a")["orders"]}
        # ORD-1004 shipped in two packages, one delivered and one still in transit.
        self.assertEqual(orders["ORD-1004"]["packages"], [
            {"tracking_no": "SF1004A", "carrier": "顺丰速运", "status": "已签收",
             "shipped_at": "2026-11-10T18:00:00+08:00", "delivered_at": "2026-11-12T11:00:00+08:00",
             "last_event_at": "2026-11-12T11:00:00+08:00"},
            {"tracking_no": "YT1004B", "carrier": "圆通速递", "status": "运输中",
             "shipped_at": "2026-11-12T20:00:00+08:00", "delivered_at": None,
             "last_event_at": "2026-11-14T08:15:00+08:00"},
        ])
        self.assertEqual([item["order_item_id"] for item in orders["ORD-1004"]["items"]],
                         ["OI-1004-1", "OI-1004-2"])
        for order in orders.values():
            for item in order["items"]:
                self.assertFalse({"status", "tracking_no", "packages"} & set(item))
        # Unpaid, not shipped: no package at all.
        self.assertEqual((orders["ORD-1003"]["status"], orders["ORD-1003"]["packages"]), ("待付款", []))

    def test_reading_orders_writes_nothing_and_opens_no_session(self):
        self.orders("customer-a")
        self.orders("customer-b")
        self.assertEqual(self.service.operator_sessions(), [])
        self.assertEqual(self.count("pending_actions"), 0)
        self.assertEqual(self.count("action_audit_events"), 0)
        self.assertEqual(self.provider.requests, [])


# --------------------------------------------------------------------------
# GET /operator/sessions/{id}/actions/{pending_action_id}/order-summary
# --------------------------------------------------------------------------


class OperatorOrderSummaryTests(OrderViewCase):
    def test_without_a_token_it_is_401(self):
        session_id, pending_id = self.waiting()
        self.assertEqual(self.answer(self.raw.get(summary_path(session_id, pending_id))), NOT_AUTHENTICATED)

    def test_a_customer_is_refused_even_for_their_own_session(self):
        session_id, pending_id = self.waiting("demo-a")
        for username in ("customer-a", "customer-b"):
            with self.subTest(username=username):
                response = self.raw.get(summary_path(session_id, pending_id), headers=self.as_(username))
                self.assertEqual(self.answer(response), FORBIDDEN)

    def test_the_operator_reads_the_line_the_action_names(self):
        session_id, pending_id = self.waiting()
        before = self.state(session_id, pending_id)
        self.assertEqual(self.summary(session_id, pending_id), {
            "session_id": session_id,
            "pending_action_id": pending_id,
            "action_name": "create_return",
            "order": {"order_id": "ORD-1001", "status": "已签收", "paid_at": "2026-11-01T20:15:00+08:00",
                      "total_amount": "298.00", "packages": [
                          {"tracking_no": "SF1001", "carrier": "顺丰速运", "status": "已签收",
                           "shipped_at": "2026-11-02T16:00:00+08:00",
                           "delivered_at": "2026-11-05T14:30:00+08:00",
                           "last_event_at": "2026-11-05T14:30:00+08:00"}]},
            # Only the line of the action (RETURN_ARGS), not the order's other line.
            "item": {"order_item_id": RETURN_ARGS["order_item_id"], "sku": "SKU-UNDERWEAR-L",
                     "product_name": "棉质内衣 L码", "category": "贴身衣物", "quantity": 1,
                     "unit_price": "100.00", "subtotal": "100.00"},
        })
        self.assertEqual(self.state(session_id, pending_id), before)   # a read changes nothing

    def test_an_action_of_another_session_is_404(self):
        session_a, pending_a = self.waiting("demo-a")
        session_b, pending_b = self.waiting_b()
        self.assertEqual(self.summary(session_b, pending_b)["item"]["product_name"], "纯棉袜子")
        not_found = (404, {"detail": {"code": "pending_action_not_found"}})
        for session_id, pending_id in ((session_a, pending_b), (session_b, pending_a), (session_a, PENDING)):
            with self.subTest(session_id=session_id, pending_id=pending_id):
                response = self.raw.get(summary_path(session_id, pending_id), headers=self.as_("operator"))
                self.assertEqual(self.answer(response), not_found)
        response = self.raw.get(summary_path(SESSION, pending_a), headers=self.as_("operator"))
        self.assertEqual(self.answer(response), (404, {"detail": {"code": "session_not_found"}}))

    def test_malformed_ids_are_422(self):
        session_id, pending_id = self.waiting()
        for path in (summary_path(session_id, "not-a-pending-id"), summary_path("not-a-session", pending_id)):
            with self.subTest(path=path):
                self.assertEqual(self.raw.get(path, headers=self.as_("operator")).status_code, 422)

    def test_the_summary_follows_the_action_through_its_decision(self):
        session_id, pending_id = self.waiting()
        before = self.summary(session_id, pending_id)
        self.assertEqual(self.decide(session_id, pending_id, "APPROVE")["action"]["status"], "EXECUTED")
        self.assertEqual(self.summary(session_id, pending_id), before)

    def test_a_line_no_longer_in_the_order_is_null_and_the_decision_is_unaffected(self):
        session_id, pending_id = self.waiting()
        # Changed outside the agent: the action's line no longer belongs to its order.
        self.write("UPDATE order_items SET order_id = 'ORD-1002' WHERE order_item_id = 'OI-1001-2'")
        summary = self.summary(session_id, pending_id)
        self.assertEqual((summary["order"]["order_id"], summary["item"]), ("ORD-1001", None))
        rejected = self.decide(session_id, pending_id, "REJECT")
        self.assertEqual(rejected["action"]["status"], "REJECTED")
        self.assertEqual(self.pending(pending_id)["status"], "REJECTED")


class OrderSummaryNeverRecoversTests(PersistentTestCase):
    """The summary reads the committed head only: it never recovers, reconciles or loads.

    Every other session request loads the conversation first (ensure_loaded),
    which recovers a marked submission and reconciles with the gateway - both
    write. The summary must leave a session exactly as it found it.
    """

    def stored(self, session_id: str) -> tuple:
        """Everything a GET must not change: the session file, the checkpoints, the actions."""
        checkpoints = sqlite3.connect(str(self.data_dir / ("gen-" + self.generation()) / "checkpoints.db"))
        try:
            graph = tuple(checkpoints.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                          for table in ("checkpoints", "writes"))
        finally:
            checkpoints.close()
        return (self.session_path(session_id).read_bytes(), graph,
                self.rows("SELECT pending_action_id, status FROM pending_actions ORDER BY pending_action_id"),
                self.rows("SELECT event_name FROM action_audit_events ORDER BY event_seq"),
                self.count("action_receipts"))

    def get_summary(self, session_id: str, pending_action_id: str):
        return self.client.get(summary_path(session_id, pending_action_id))   # as the operator

    def test_a_session_with_a_submission_to_recover_is_409_and_left_as_it_was(self):
        session_id = self.session()
        self.crash_after_start_action(session_id)   # the gateway wrote; the head does not know
        pending_id = self.rows("SELECT pending_action_id FROM pending_actions")[0][0]
        before = self.stored(session_id)
        for _ in range(2):
            response = self.get_summary(session_id, pending_id)
            self.assertEqual((response.status_code, response.json()), (409, {"detail": {"code": "recovery_pending"}}))
        self.assertEqual(self.stored(session_id), before)
        self.assertIsNotNone(self.session_file(session_id)["inflight"])
        self.assertNotIn("action.replay_hit", str(before))
        self.assertFalse(self.conversation(session_id)._loaded)
        # The next request that loads the session recovers it; then the summary reads it.
        self.assertEqual(self.view(session_id)["status"], "WAITING_APPROVAL")
        self.assertIsNone(self.session_file(session_id)["inflight"])
        response = self.get_summary(session_id, pending_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item"]["order_item_id"], RETURN_ARGS["order_item_id"])

    def test_an_unloaded_session_is_read_from_its_head_and_stays_unloaded(self):
        session_id, pending_id = self.waiting()
        expected = self.get_summary(session_id, pending_id).json()
        self.restart()   # nothing in memory: the session is rebuilt only by a loading request
        before = self.stored(session_id)
        response = self.get_summary(session_id, pending_id)
        self.assertEqual((response.status_code, response.json()), (200, expected))
        self.assertEqual(self.stored(session_id), before)
        self.assertFalse(self.conversation(session_id)._loaded)

    def test_a_decision_recorded_behind_the_head_is_not_reconciled_by_the_summary(self):
        session_id, pending_id = self.waiting()
        self.restart()
        # The gateway's outcome moved on without this session's head (another process).
        self.service.store.gateway.resume_action(self.approval(pending_id))
        before = self.stored(session_id)
        response = self.get_summary(session_id, pending_id)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.stored(session_id), before)   # no decision event written onto the head
        self.assertFalse(self.conversation(session_id)._loaded)
        self.assertEqual(self.kinds(self.view(session_id))[-1], ("assistant", "operator_decision"))


class OrderLineLookupTests(OrderViewCase):
    """DemoStore.order_line: only the caller-named customer's order, only a line of that order."""

    def test_what_is_not_the_customers_is_none(self):
        store = self.service.store
        own = store.order_line("CUST-001", "ORD-1001", "OI-1001-1")
        self.assertEqual((own["order"]["order_id"], own["item"]["subtotal"]), ("ORD-1001", "198.00"))
        for customer_id, order_id, order_item_id, expected in (
                ("CUST-001", "ORD-2001", "OI-2001-1", (None, None)),      # another customer's order
                ("CUST-002", "ORD-1001", "OI-1001-1", (None, None)),
                ("CUST-001", "ORD-1001", "OI-2001-1", ("ORD-1001", None)),  # a line of another order
                ("CUST-001", "ORD-1001", "OI-1004-1", ("ORD-1001", None)),
                ("CUST-001", "ORD-9999", "OI-1001-1", (None, None)),
                ("CUST-001", None, None, (None, None)),
                ("CUST-001", "ORD-1001", None, ("ORD-1001", None))):
            with self.subTest(customer_id=customer_id, order_id=order_id, order_item_id=order_item_id):
                line = store.order_line(customer_id, order_id, order_item_id)
                self.assertEqual((None if line["order"] is None else line["order"]["order_id"],
                                  line["item"]), expected)


if __name__ == "__main__":
    unittest.main()
