"""After-sales schema and demo seed (docs/v2/stage4-design.md §3.1)."""

import sqlite3
import unittest
from datetime import datetime

from aftersales.demo import DEMO_PERSONAS, DEMO_VIRTUAL_NOW, open_demo_database
from aftersales.schema import (
    ENTITY_TABLES,
    IDENTITY_COLUMNS,
    NULLABLE_COLUMNS,
    PRIMARY_KEYS,
    TABLE_COLUMNS,
    TIMESTAMP_COLUMNS,
    CaseStatus,
    CaseType,
    LogisticsStatus,
    OrderStatus,
)

from tests.v2_support import memory_connection

# docs/v2/stage4-design.md §3.1, verbatim. The schema may add columns (it adds
# order_items.updated_at) but must never lose one of these.
DESIGN_MINIMUM_FIELDS = {
    "orders": {"order_id", "customer_id", "status", "paid_at", "total_amount",
               "updated_at", "version"},
    "order_items": {"order_item_id", "order_id", "sku", "product_name", "category",
                    "quantity", "unit_price", "version"},
    "logistics": {"tracking_no", "order_id", "carrier", "status", "shipped_at",
                  "delivered_at", "last_event_at", "updated_at", "version"},
    "inventory": {"sku", "available_qty", "updated_at", "version"},
    "after_sales_cases": {"case_id", "order_id", "order_item_id", "customer_id", "type",
                          "status", "reason", "created_at", "updated_at", "version"},
}

# Stage 6 concerns that must not be pre-built (D27, §11).
FORBIDDEN_NAMES = ("idempotency", "approval", "pending")


class DomainTestCase(unittest.TestCase):
    def setUp(self):
        self.connection = memory_connection()
        self.addCleanup(self.connection.close)

    def table_info(self, table):
        # row = (cid, name, type, notnull, default, pk)
        return self.connection.execute("PRAGMA table_info(" + table + ")").fetchall()

    def rows(self, table):
        cursor = self.connection.execute("SELECT * FROM " + table)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


class SchemaShapeTests(DomainTestCase):
    def test_tables_are_exactly_the_declared_ones(self):
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(names, set(TABLE_COLUMNS))
        self.assertEqual(set(ENTITY_TABLES.values()), set(TABLE_COLUMNS))

    def test_ddl_matches_the_python_declaration(self):
        for table, columns in TABLE_COLUMNS.items():
            with self.subTest(table=table):
                self.assertEqual(tuple(row[1] for row in self.table_info(table)), columns)

    def test_design_minimum_fields_are_present(self):
        for table, fields in DESIGN_MINIMUM_FIELDS.items():
            with self.subTest(table=table):
                self.assertLessEqual(fields, set(TABLE_COLUMNS[table]))

    def test_every_record_carries_updated_at_and_an_integer_version(self):
        for table in TABLE_COLUMNS:
            with self.subTest(table=table):
                info = {row[1]: row for row in self.table_info(table)}
                self.assertIn("updated_at", info)
                self.assertEqual(info["version"][2], "INTEGER")
                self.assertEqual(info["version"][3], 1)

    def test_lookup_keys_are_primary_keys_and_other_columns_not_null(self):
        """Ports V1 tests.test_system_provider.FixtureTests.test_lookup_keys_are_primary_keys_and_columns_are_not_null"""
        for table, key in PRIMARY_KEYS.items():
            with self.subTest(table=table):
                info = self.table_info(table)
                self.assertEqual([row[1] for row in info if row[5]], [key])
                for row in info:
                    if row[1] == key or row[1] in NULLABLE_COLUMNS[table]:
                        continue
                    self.assertEqual(row[3], 1, table + "." + row[1] + " must be NOT NULL")

    def test_an_order_may_have_several_packages(self):
        # No unique index may cover logistics.order_id alone: one order, 0..N packages.
        for index in self.connection.execute("PRAGMA index_list(logistics)").fetchall():
            if index[2]:  # unique
                columns = [
                    column[2]
                    for column in self.connection.execute("PRAGMA index_info(" + index[1] + ")")
                ]
                self.assertNotEqual(columns, ["order_id"])
        stamp = "2026-11-15T09:00:00+08:00"
        self.connection.execute(
            "INSERT INTO logistics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("SF1001-2", "ORD-1001", "顺丰速运", "运输中", stamp, None, stamp, stamp, 1),
        )
        count = self.connection.execute(
            "SELECT COUNT(*) FROM logistics WHERE order_id = 'ORD-1001'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_no_stage6_structures_exist(self):
        rendered = repr(TABLE_COLUMNS).lower()
        for name in FORBIDDEN_NAMES:
            with self.subTest(name=name):
                self.assertNotIn(name, rendered)

    def test_customer_id_has_no_entity_of_its_own(self):
        self.assertNotIn("customer", ENTITY_TABLES)
        self.assertNotIn("customers", TABLE_COLUMNS)
        for table, columns in IDENTITY_COLUMNS.items():
            self.assertLessEqual(columns, set(TABLE_COLUMNS[table]))


class ConstraintTests(DomainTestCase):
    def test_duplicate_primary_key_is_rejected(self):
        """Ports V1 tests.test_system_provider.FixtureTests.test_duplicate_primary_key_is_rejected"""
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO inventory VALUES (?, ?, ?, ?)",
                ("SKU-MUG", 1, "2026-11-15T09:00:00+08:00", 1),
            )

    def test_negative_quantity_is_rejected(self):
        """Ports V1 tests.test_system_provider.FixtureTests.test_negative_quantity_is_rejected"""
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO inventory VALUES (?, ?, ?, ?)",
                ("SKU-NEG", -1, "2026-11-15T09:00:00+08:00", 1),
            )

    def test_version_must_be_at_least_one(self):
        for version in (0, -1):
            with self.subTest(version=version):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.connection.execute(
                        "INSERT INTO inventory VALUES (?, ?, ?, ?)",
                        ("SKU-V" + str(version), 1, "2026-11-15T09:00:00+08:00", version),
                    )

    def test_status_and_type_are_closed_enumerations(self):
        cases = (
            ("UPDATE orders SET status = ? WHERE order_id = 'ORD-1001'", "已退款"),
            ("UPDATE logistics SET status = ? WHERE tracking_no = 'SF1001'", "丢失"),
            ("UPDATE after_sales_cases SET type = ? WHERE case_id = 'AS-1001'", "refund"),
            ("UPDATE after_sales_cases SET status = ? WHERE case_id = 'AS-1001'", "待审批"),
        )
        for sql, value in cases:
            with self.subTest(value=value):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.connection.execute(sql, (value,))

    def test_enumerations_match_the_ddl(self):
        for enum, table, column in (
            (OrderStatus, "orders", "status"),
            (LogisticsStatus, "logistics", "status"),
            (CaseType, "after_sales_cases", "type"),
            (CaseStatus, "after_sales_cases", "status"),
        ):
            for member in enum:
                with self.subTest(enum=enum.__name__, value=member.value):
                    self.connection.execute("SAVEPOINT probe")
                    # Accepted by the CHECK constraint: no IntegrityError.
                    self.connection.execute(
                        "UPDATE " + table + " SET " + column + " = ?", (member.value,)
                    )
                    self.connection.execute("ROLLBACK TO probe")
                    self.connection.execute("RELEASE probe")

    def test_money_is_a_two_decimal_string(self):
        for bad in ("12", "12.5", "abc", "-1.00"):
            with self.subTest(bad=bad):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.connection.execute(
                        "UPDATE orders SET total_amount = ? WHERE order_id = 'ORD-1001'",
                        (bad,),
                    )


class SeedTests(DomainTestCase):
    def test_every_timestamp_is_aware_iso8601_and_not_after_virtual_now(self):
        for table, columns in TIMESTAMP_COLUMNS.items():
            for row in self.rows(table):
                for column in columns:
                    value = row[column]
                    if value is None:
                        self.assertIn(column, NULLABLE_COLUMNS[table])
                        continue
                    with self.subTest(table=table, column=column, key=row[PRIMARY_KEYS[table]]):
                        parsed = datetime.fromisoformat(value)
                        self.assertIsNotNone(parsed.utcoffset())
                        self.assertLessEqual(parsed, DEMO_VIRTUAL_NOW)

    def test_fixture_has_at_least_two_customers(self):
        """Ports V1 tests.test_system_provider.FixtureTests.test_fixture_has_at_least_two_subjects"""
        customers = {row["customer_id"] for row in self.rows("orders")}
        self.assertGreaterEqual(len(customers), 2)

    def test_every_persona_owns_orders_and_every_customer_has_a_persona(self):
        customers = {row["customer_id"] for row in self.rows("orders")}
        persona_customers = {persona.customer_id for persona in DEMO_PERSONAS.values()}
        self.assertGreaterEqual(len(DEMO_PERSONAS), 2)
        self.assertEqual(customers, persona_customers)

    def test_required_business_situations_exist(self):
        orders = self.rows("orders")
        logistics = self.rows("logistics")
        inventory = self.rows("inventory")
        self.assertGreater(len(orders), 2)
        self.assertGreater(len(self.rows("order_items")), 2)
        self.assertTrue(any(row["status"] == "已签收" for row in logistics))
        self.assertTrue(any(row["status"] == "运输中" for row in logistics))
        self.assertTrue(any(row["delivered_at"] is None for row in logistics))
        # At least one order shipped in several packages with differing statuses.
        by_order: dict[str, set[str]] = {}
        for row in logistics:
            by_order.setdefault(row["order_id"], set()).add(row["status"])
        self.assertTrue(any(len(statuses) >= 2 for statuses in by_order.values()))
        self.assertTrue(any(row["available_qty"] > 0 for row in inventory))
        self.assertTrue(any(row["available_qty"] == 0 for row in inventory))
        self.assertTrue(self.rows("after_sales_cases"))

    def test_relations_are_consistent(self):
        orders = {row["order_id"]: row for row in self.rows("orders")}
        items = {row["order_item_id"]: row for row in self.rows("order_items")}
        skus = {row["sku"] for row in self.rows("inventory")}
        for item in items.values():
            self.assertIn(item["order_id"], orders)
            self.assertIn(item["sku"], skus)
        for shipment in self.rows("logistics"):
            self.assertIn(shipment["order_id"], orders)
        for case in self.rows("after_sales_cases"):
            with self.subTest(case=case["case_id"]):
                order = orders[case["order_id"]]
                # A case belongs to the same customer as its order.
                self.assertEqual(case["customer_id"], order["customer_id"])
                self.assertEqual(items[case["order_item_id"]]["order_id"], case["order_id"])

    def test_order_totals_match_their_items(self):
        from decimal import Decimal

        for order in self.rows("orders"):
            with self.subTest(order=order["order_id"]):
                total = sum(
                    Decimal(item["unit_price"]) * item["quantity"]
                    for item in self.rows("order_items")
                    if item["order_id"] == order["order_id"]
                )
                self.assertEqual(total, Decimal(order["total_amount"]))


class DemoDatabaseTests(unittest.TestCase):
    def test_demo_database_is_loaded_and_refuses_writes(self):
        with open_demo_database() as connection:
            count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            self.assertGreater(count, 0)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM orders")
        # Closed on exit.
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
