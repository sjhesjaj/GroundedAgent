"""The four read-only after-sales business tools.

`get_order`, `get_logistics`, `get_inventory`, `get_after_sales_case` answer
*what is true right now* for one customer's records. Safety properties, all
structural rather than advisory (ported from the V1 system provider):

- a caller picks a tool by name and supplies a closed set of string arguments,
  never a table, column, predicate, or SQL string;
- every query is a fixed, single-statement template; every value is bound
  through a placeholder, so an injection string is only ever a value;
- this module contains no write statement of any kind and opens no connection -
  it borrows the one in the trusted context;
- rows are read positionally on the tool's own cursor, so a caller's
  `row_factory` changes nothing and is never overwritten;
- `customer_id` comes only from the trusted context, is used as a predicate,
  and is never selected - it cannot reach Evidence. Every customer-scoped
  lookup is authorized by order ownership (`orders.customer_id`);
- database faults propagate as exceptions: they are never an empty result.
  An empty result means the lookup ran and matched nothing, which is also what
  a record belonging to another customer looks like (existence is not leaked);
- business time comes only from `context.clock`.

Every result field becomes one `BusinessEvidence`, located to the field
(`logistics:SF1001#delivered_at`), with `observed_at` = the Clock's reading,
`record_updated_at` = the record's `updated_at`, and `state_version` = the
record's `version`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    FreshnessContract,
    SourceType,
    ToolResult,
    ToolStatus,
)

from .arguments import validate_arguments
from .clock import require_aware
from .context import TrustedExecutionContext
from .errors import RecordIntegrityError
from .schema import TIMESTAMP_COLUMNS

GET_ORDER = "get_order"
GET_LOGISTICS = "get_logistics"
GET_INVENTORY = "get_inventory"
GET_AFTER_SALES_CASE = "get_after_sales_case"

BUSINESS_TOOL_NAMES = (GET_ORDER, GET_LOGISTICS, GET_INVENTORY, GET_AFTER_SALES_CASE)

# One data source, one freshness contract: the demo database is read directly,
# so every read is a current observation.
BUSINESS_SOURCE = "aftersales-demo-db"
SOURCE_FRESHNESS = FreshnessContract.AUTHORITATIVE_ONLINE
# Above document (80) and wiki (60), but only for live operational state.
BUSINESS_AUTHORITY = 100
AUTHORITY_SCOPE = "current_operational_state"

# The caller-supplied parameters. Identity is never one of them.
TOOL_PARAMETERS: dict[str, tuple[str, ...]] = {
    GET_ORDER: ("order_id",),
    GET_LOGISTICS: ("order_id",),
    GET_INVENTORY: ("sku",),
    GET_AFTER_SALES_CASE: ("order_id",),
}

# Tools whose every query is scoped by the trusted customer_id.
IDENTITY_SCOPED_TOOLS = frozenset({GET_ORDER, GET_LOGISTICS, GET_AFTER_SALES_CASE})

# The only fields a business tool trace may carry. Names and counts only.
BUSINESS_TRACE_FIELDS = frozenset(
    {"tool", "parameter_names", "identity_scoped", "records_matched", "evidence_count"}
)

_CUSTOMER = "customer_id"


@dataclass(frozen=True)
class Query:
    """One fixed statement and how to turn its rows into evidence."""

    entity: str
    table: str
    sql: str
    # Binding order is this declaration, never dict iteration order.
    bindings: tuple[str, ...]
    columns: tuple[str, ...]
    key: str
    # (column, label) pairs that become evidence, in output order.
    fields: tuple[tuple[str, str], ...]
    # A primary-key lookup must match at most one row; a listing may match many.
    single_row: bool


ORDER_QUERY = Query(
    entity="order",
    table="orders",
    sql=(
        "SELECT o.order_id, o.status, o.paid_at, o.total_amount, o.updated_at,"
        " o.version FROM orders AS o WHERE o.customer_id = ? AND o.order_id = ?"
    ),
    bindings=(_CUSTOMER, "order_id"),
    columns=("order_id", "status", "paid_at", "total_amount", "updated_at", "version"),
    key="order_id",
    fields=(("status", "状态"), ("paid_at", "付款时间"), ("total_amount", "订单金额")),
    single_row=True,
)

ORDER_ITEMS_QUERY = Query(
    entity="order_item",
    table="order_items",
    sql=(
        "SELECT i.order_item_id, i.sku, i.product_name, i.category, i.quantity,"
        " i.unit_price, i.updated_at, i.version FROM order_items AS i"
        " JOIN orders AS o ON o.order_id = i.order_id"
        " WHERE o.customer_id = ? AND i.order_id = ? ORDER BY i.order_item_id"
    ),
    bindings=(_CUSTOMER, "order_id"),
    columns=(
        "order_item_id",
        "sku",
        "product_name",
        "category",
        "quantity",
        "unit_price",
        "updated_at",
        "version",
    ),
    key="order_item_id",
    fields=(
        ("sku", "SKU"),
        ("product_name", "商品名称"),
        ("category", "品类"),
        ("quantity", "数量"),
        ("unit_price", "单价"),
    ),
    single_row=False,
)

LOGISTICS_QUERY = Query(
    entity="logistics",
    table="logistics",
    sql=(
        "SELECT l.tracking_no, l.order_id, l.carrier, l.status, l.shipped_at,"
        " l.delivered_at, l.last_event_at, l.updated_at, l.version"
        " FROM logistics AS l JOIN orders AS o ON o.order_id = l.order_id"
        " WHERE o.customer_id = ? AND l.order_id = ? ORDER BY l.tracking_no"
    ),
    bindings=(_CUSTOMER, "order_id"),
    columns=(
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
    key="tracking_no",
    fields=(
        ("order_id", "关联订单"),
        ("carrier", "承运商"),
        ("status", "物流状态"),
        ("shipped_at", "发货时间"),
        ("delivered_at", "签收时间"),
        ("last_event_at", "最近物流事件时间"),
    ),
    # An order may ship in several packages; every one is returned.
    single_row=False,
)

INVENTORY_QUERY = Query(
    entity="inventory",
    table="inventory",
    sql="SELECT sku, available_qty, updated_at, version FROM inventory WHERE sku = ?",
    bindings=("sku",),
    columns=("sku", "available_qty", "updated_at", "version"),
    key="sku",
    fields=(("available_qty", "可售库存"),),
    single_row=True,
)

# Authorization is the order's ownership (orders.customer_id), never the
# case's own redundant customer_id: nothing in the schema forces the two to
# agree. A case whose customer_id disagrees with its order's owner is hidden
# from everyone (fail closed).
CASE_QUERY = Query(
    entity="after_sales_case",
    table="after_sales_cases",
    sql=(
        "SELECT c.case_id, c.order_id, c.order_item_id, c.type, c.status, c.reason,"
        " c.created_at, c.updated_at, c.version FROM after_sales_cases AS c"
        " JOIN orders AS o ON o.order_id = c.order_id"
        " WHERE o.customer_id = ? AND c.order_id = ?"
        " AND c.customer_id = o.customer_id ORDER BY c.case_id"
    ),
    bindings=(_CUSTOMER, "order_id"),
    columns=(
        "case_id",
        "order_id",
        "order_item_id",
        "type",
        "status",
        "reason",
        "created_at",
        "updated_at",
        "version",
    ),
    key="case_id",
    fields=(
        ("order_id", "关联订单"),
        ("order_item_id", "关联订单明细"),
        ("type", "类型"),
        ("status", "状态"),
        ("reason", "原因"),
        ("created_at", "创建时间"),
    ),
    single_row=False,
)

# Queries run in order. A later query runs only if the first one matched: an
# order's items are listed only once the order itself is visible.
TOOL_QUERIES: dict[str, tuple[Query, ...]] = {
    GET_ORDER: (ORDER_QUERY, ORDER_ITEMS_QUERY),
    GET_LOGISTICS: (LOGISTICS_QUERY,),
    GET_INVENTORY: (INVENTORY_QUERY,),
    GET_AFTER_SALES_CASE: (CASE_QUERY,),
}

ENTITY_LABELS: dict[str, str] = {
    "order": "订单",
    "order_item": "订单明细",
    "logistics": "物流单",
    "inventory": "SKU",
    "after_sales_case": "售后单",
}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _fetch(connection, query: Query, values: Mapping[str, str]) -> list[tuple]:
    bindings = tuple(values[name] for name in query.bindings)
    # The tool's own cursor with the factory cleared: the caller's
    # connection.row_factory is left untouched and cannot change decoding.
    cursor = connection.cursor()
    try:
        cursor.row_factory = None
        cursor.execute(query.sql, bindings)
        return cursor.fetchall()
    finally:
        cursor.close()


def _check_row(tool: str, query: Query, record: dict[str, object]) -> None:
    """A valid source cannot produce these; fail loudly instead of guessing."""
    where = tool + " read " + query.table + "."
    for column in TIMESTAMP_COLUMNS[query.table] & set(query.columns):
        value = record[column]
        if value is None:
            continue
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            parsed = None
        if parsed is None or parsed.utcoffset() is None:
            raise RecordIntegrityError(where + column + " without a valid ISO-8601 offset")
    version = record["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RecordIntegrityError(where + "version that is not a positive integer")
    if not isinstance(record[query.key], str) or not record[query.key].strip():
        raise RecordIntegrityError(where + query.key + " that is not a non-empty string")


def _render(value: object) -> str:
    return "空" if value is None else str(value)


def _to_evidence(
    tool: str, query: Query, record: dict[str, object], observed_at: str
) -> list[BusinessEvidence]:
    record_id = record[query.key]
    evidence = []
    for column, label in query.fields:
        value = record[column]
        evidence.append(
            BusinessEvidence(
                content=(
                    ENTITY_LABELS[query.entity] + " " + record_id + " 的"
                    + label + "为 " + _render(value) + "。"
                ),
                source_type=SourceType.BUSINESS,
                source=BUSINESS_SOURCE,
                locator=query.entity + ":" + record_id + "#" + column,
                version=None,
                observed_at=observed_at,
                authority=BUSINESS_AUTHORITY,
                confidence=None,
                metadata={
                    "tool": tool,
                    "entity": query.entity,
                    "record_id": record_id,
                    "field": column,
                    "value": value,
                    # Business authority holds for live state only, never for
                    # what a policy means.
                    "authority_scope": AUTHORITY_SCOPE,
                    # Linked by the executor to the tool call that read it.
                    OBSERVATION_ID_KEY: None,
                },
                record_updated_at=record["updated_at"],
                state_version=record["version"],
                freshness_contract=SOURCE_FRESHNESS,
                source_as_of=None,
            )
        )
    return evidence


def _run(
    tool: str, context: TrustedExecutionContext, arguments: Mapping[str, str]
) -> ToolResult:
    # Direct calls get the same closed-schema check the executor applies.
    values = validate_arguments(tool, TOOL_PARAMETERS[tool], arguments)
    if tool in IDENTITY_SCOPED_TOOLS:
        # From the trusted context only. `validate_arguments` has already
        # rejected any attempt to pass it as an argument.
        values[_CUSTOMER] = context.customer_id

    # One reading per call: every evidence item of this observation shares it.
    observed_at = require_aware("context.clock.now()", context.clock.now()).isoformat()

    evidence: list[BusinessEvidence] = []
    records_matched = 0
    for index, query in enumerate(TOOL_QUERIES[tool]):
        rows = _fetch(context.connection, query, values)
        if query.single_row and len(rows) > 1:
            raise RecordIntegrityError(
                tool + " matched " + str(len(rows)) + " rows in " + query.table
                + "; a record lookup must match at most one"
            )
        if index == 0 and not rows:
            break
        for row in rows:
            record = dict(zip(query.columns, row))
            _check_row(tool, query, record)
            evidence.extend(_to_evidence(tool, query, record, observed_at))
        records_matched += len(rows)

    trace: dict[str, object] = {
        "tool": tool,
        # Names only. Values, and the injected identity, are never traced.
        "parameter_names": sorted(TOOL_PARAMETERS[tool]),
        "identity_scoped": tool in IDENTITY_SCOPED_TOOLS,
        "records_matched": records_matched,
        "evidence_count": len(evidence),
    }
    return ToolResult(
        tool_name=tool,
        status=ToolStatus.OK if evidence else ToolStatus.EMPTY,
        evidence=tuple(evidence),
        trace=trace,
    )


# --------------------------------------------------------------------------
# Handlers: (context, arguments) -> ToolResult
# --------------------------------------------------------------------------


def get_order(context: TrustedExecutionContext, arguments: Mapping[str, str]) -> ToolResult:
    """The customer's order and its items. EMPTY if not theirs or not found."""
    return _run(GET_ORDER, context, arguments)


def get_logistics(
    context: TrustedExecutionContext, arguments: Mapping[str, str]
) -> ToolResult:
    """Every package (0..N) of one of the customer's orders, by tracking_no."""
    return _run(GET_LOGISTICS, context, arguments)


def get_inventory(
    context: TrustedExecutionContext, arguments: Mapping[str, str]
) -> ToolResult:
    """Available quantity for one SKU. Zero stock is an OK observation."""
    return _run(GET_INVENTORY, context, arguments)


def get_after_sales_case(
    context: TrustedExecutionContext, arguments: Mapping[str, str]
) -> ToolResult:
    """Every after-sales case the customer has on one order, by case_id."""
    return _run(GET_AFTER_SALES_CASE, context, arguments)


BUSINESS_HANDLERS = {
    GET_ORDER: get_order,
    GET_LOGISTICS: get_logistics,
    GET_INVENTORY: get_inventory,
    GET_AFTER_SALES_CASE: get_after_sales_case,
}
