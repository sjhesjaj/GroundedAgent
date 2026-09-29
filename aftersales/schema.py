"""Python-side declaration of the after-sales schema in `schema.sql`.

Constants only: this module performs no database work. The DDL is the source
of truth for the database; these declarations are what the tools and tests use
to talk about it, and `tests/test_v2_domain_schema.py` pins the two together.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

# Location only. Composition roots load it; tools never read it.
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class OrderStatus(str, Enum):
    PENDING_PAYMENT = "待付款"
    PAID = "已付款"
    SHIPPED = "已发货"
    DELIVERED = "已签收"
    COMPLETED = "已完成"
    CANCELLED = "已取消"


class LogisticsStatus(str, Enum):
    IN_TRANSIT = "运输中"
    OUT_FOR_DELIVERY = "派送中"
    DELIVERED = "已签收"
    EXCEPTION = "异常"
    RETURNED = "退回"


class CaseType(str, Enum):
    RETURN = "return"
    EXCHANGE = "exchange"


class CaseStatus(str, Enum):
    PENDING = "待处理"
    IN_PROGRESS = "处理中"
    COMPLETED = "已完成"
    REJECTED = "已拒绝"
    CANCELLED = "已取消"


# Entity name (used in evidence locators) -> table name.
ENTITY_TABLES: dict[str, str] = {
    "order": "orders",
    "order_item": "order_items",
    "logistics": "logistics",
    "inventory": "inventory",
    "after_sales_case": "after_sales_cases",
}

# Every column of every table, in DDL order.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "orders": (
        "order_id",
        "customer_id",
        "status",
        "paid_at",
        "total_amount",
        "updated_at",
        "version",
    ),
    "order_items": (
        "order_item_id",
        "order_id",
        "sku",
        "product_name",
        "category",
        "quantity",
        "unit_price",
        "updated_at",
        "version",
    ),
    "logistics": (
        "tracking_no",
        "order_id",
        "carrier",
        "status",
        "shipped_at",
        "delivered_at",
        "last_event_at",
        "updated_at",
        "version",
    ),
    "inventory": ("sku", "available_qty", "updated_at", "version"),
    "after_sales_cases": (
        "case_id",
        "order_id",
        "order_item_id",
        "customer_id",
        "type",
        "status",
        "reason",
        "created_at",
        "updated_at",
        "version",
    ),
}

PRIMARY_KEYS: dict[str, str] = {
    "orders": "order_id",
    "order_items": "order_item_id",
    "logistics": "tracking_no",
    "inventory": "sku",
    "after_sales_cases": "case_id",
}

# Columns that may legitimately be NULL: not yet paid, not yet delivered.
NULLABLE_COLUMNS: dict[str, frozenset[str]] = {
    "orders": frozenset({"paid_at"}),
    "order_items": frozenset(),
    "logistics": frozenset({"delivered_at"}),
    "inventory": frozenset(),
    "after_sales_cases": frozenset(),
}

# ISO-8601 timestamps with an explicit offset.
TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "orders": frozenset({"paid_at", "updated_at"}),
    "order_items": frozenset({"updated_at"}),
    "logistics": frozenset(
        {"shipped_at", "delivered_at", "last_event_at", "updated_at"}
    ),
    "inventory": frozenset({"updated_at"}),
    "after_sales_cases": frozenset({"created_at", "updated_at"}),
}

# Identity columns: lookup predicates only, never selected, never evidence.
IDENTITY_COLUMNS: dict[str, frozenset[str]] = {
    "orders": frozenset({"customer_id"}),
    "order_items": frozenset(),
    "logistics": frozenset(),
    "inventory": frozenset(),
    "after_sales_cases": frozenset({"customer_id"}),
}
