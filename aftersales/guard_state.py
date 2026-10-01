"""The Guard's own fresh reads: GuardStateReader -> GuardState (docs/v2/stage6-design.md §6.2).

Called only by `Guard.capture`, inside the transaction the ActionGateway has
already opened with BEGIN IMMEDIATE, on the gateway's writer connection.

- Fixed single-statement templates R1-R8; every value is bound through a
  placeholder; rows are read positionally on the reader's own cursor, so a
  caller's row_factory changes nothing. No SQL is built from action values.
- SELECT lists hold structured columns only. Never customer_id (the trusted
  customer id is a bound predicate only), never the free-text columns
  after_sales_cases.reason, order_items.product_name or logistics.carrier.
- No Clock: every business instant is the explicit `txn_now` the gateway read once.
- A database error is never an empty result: it is GuardFailure(state_read_failed).
  A version that is not a positive integer is GuardFailure(state_version_missing);
  any other integrity violation is GuardFailure(state_malformed).

The BusinessEvidence the derived-fact functions need is built here, in the
same shape the business tools produce (entity / record_id / field / value
metadata, field locator, observed_at = txn_now, state_version, order_id
relation), so `Guard.decide` reads nothing.

Eval read hook (docs/v2/stage6-design.md §19.2 `action_faults: guard_read`)
    Optional and inert by default: a reader built without a hook runs every
    template exactly as before. With a hook, `hook.before_read(read)` is called
    just before each named read (GUARD_READS). It may raise sqlite3.Error - the
    read fails and the reader's own handling turns it into
    GuardFailure(state_read_failed) - or return READ_MALFORMED, in which case
    the read yields one row of NULLs that the unchanged decoders reject as
    GuardFailure(state_malformed). Nothing runs first and is then rewritten.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Protocol, Sequence

from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    SourceType,
)

from .action_errors import (
    FAILURE_STATE_MALFORMED,
    FAILURE_STATE_READ,
    FAILURE_STATE_VERSION_MISSING,
    GuardFailure,
)
from .actions import CREATE_EXCHANGE, CREATE_RETURN, ESCALATE_TO_HUMAN, ValidatedAction
from .business_tools import AUTHORITY_SCOPE, BUSINESS_AUTHORITY, BUSINESS_SOURCE, SOURCE_FRESHNESS
from .clock import require_aware
from .context import TrustedExecutionContext
from .schema import CaseStatus, CaseType, LogisticsStatus, OrderStatus

# One capture is one observation of the Guard; derived functions require that
# every evidence item they combine shares one (observed_at, observation_id).
GUARD_OBSERVATION_ID = "guard:capture"
GUARD_READER = "guard_state_reader"

ORDER_STATUSES = frozenset(item.value for item in OrderStatus)
PACKAGE_STATUSES = frozenset(item.value for item in LogisticsStatus)
CASE_TYPES = frozenset(item.value for item in CaseType)
CASE_STATUSES = frozenset(item.value for item in CaseStatus)
TICKET_STATUSES = frozenset({"待处理", "处理中", "已关闭"})
PENDING_STATUSES = frozenset({
    "PENDING_APPROVAL", "APPROVED", "REJECTED", "EXECUTED", "STALE", "DENIED", "FAILED",
})

# --------------------------------------------------------------------------
# Fixed templates (§6.2). Structured columns only.
# --------------------------------------------------------------------------

R1_ORDER = (
    "SELECT o.order_id, o.status, o.updated_at, o.version"
    " FROM orders AS o WHERE o.customer_id = ? AND o.order_id = ?"
)
R2_ORDER_ITEM = (
    "SELECT i.order_item_id, i.order_id, i.sku, i.category, i.quantity, i.updated_at, i.version"
    " FROM order_items AS i JOIN orders AS o ON o.order_id = i.order_id"
    " WHERE o.customer_id = ? AND i.order_id = ? AND i.order_item_id = ?"
)
R3_LOGISTICS = (
    "SELECT l.tracking_no, l.order_id, l.status, l.delivered_at, l.updated_at, l.version"
    " FROM logistics AS l JOIN orders AS o ON o.order_id = l.order_id"
    " WHERE o.customer_id = ? AND l.order_id = ? ORDER BY l.tracking_no"
)
R4_ITEM_CASES = (
    "SELECT c.case_id, c.order_item_id, c.type, c.status, c.updated_at, c.version"
    " FROM after_sales_cases AS c WHERE c.order_item_id = ? ORDER BY c.case_id"
)
R5_TARGET_INVENTORY = (
    "SELECT v.sku, v.available_qty, v.updated_at, v.version"
    " FROM inventory AS v WHERE v.sku = ?"
)
R6_VARIANTS = (
    "SELECT s.sku, s.variant_group, s.updated_at, s.version"
    " FROM sku_variants AS s WHERE s.sku IN (?, ?) ORDER BY s.sku"
)
R7_TICKETS = (
    "SELECT t.ticket_id, t.order_item_id, t.handoff_trigger, t.status, t.updated_at, t.version"
    " FROM human_handoff_tickets AS t WHERE t.order_item_id = ? AND t.handoff_trigger = ?"
    " ORDER BY t.ticket_id"
)
R8_OTHER_PENDINGS = (
    "SELECT p.pending_action_id, p.status, p.version"
    " FROM pending_actions AS p WHERE p.target_order_item_id = ?"
    " AND p.status IN ('PENDING_APPROVAL', 'APPROVED')"
    " AND (? IS NULL OR p.pending_action_id <> ?) ORDER BY p.pending_action_id"
)

GUARD_SQL_TEMPLATES = {
    "R1": R1_ORDER,
    "R2": R2_ORDER_ITEM,
    "R3": R3_LOGISTICS,
    "R4": R4_ITEM_CASES,
    "R5": R5_TARGET_INVENTORY,
    "R6": R6_VARIANTS,
    "R7": R7_TICKETS,
    "R8": R8_OTHER_PENDINGS,
}

# Read names of the eval read hook, in template order (§19.2 guard_read).
GUARD_READS = {
    "order": R1_ORDER,
    "order_item": R2_ORDER_ITEM,
    "logistics": R3_LOGISTICS,
    "item_cases": R4_ITEM_CASES,
    "inventory": R5_TARGET_INVENTORY,
    "variants": R6_VARIANTS,
    "tickets": R7_TICKETS,
    "pendings": R8_OTHER_PENDINGS,
}
READ_MALFORMED = "malformed"
# Selected columns per read: a malformed read is one row of that many NULLs.
_READ_WIDTHS = {read: len(sql.split(" FROM ", 1)[0].split(",")) for read, sql in GUARD_READS.items()}


class GuardReadHook(Protocol):
    def before_read(self, read: str) -> str | None:
        """Eval only: may raise sqlite3.Error, or return READ_MALFORMED; None reads normally."""


# The Guard-relevant record set of each action (§12.1), by table.
RECORD_TABLES = {
    CREATE_RETURN: ("after_sales_cases", "logistics", "order_items", "orders"),
    CREATE_EXCHANGE: ("after_sales_cases", "inventory", "logistics", "order_items", "orders",
                      "sku_variants"),
    ESCALATE_TO_HUMAN: ("human_handoff_tickets", "order_items", "orders"),
}


# --------------------------------------------------------------------------
# Typed state
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class OrderRow:
    order_id: str
    status: str
    version: int


@dataclass(frozen=True, kw_only=True)
class OrderItemRow:
    order_item_id: str
    order_id: str
    sku: str
    category: str
    quantity: int
    version: int


@dataclass(frozen=True, kw_only=True)
class PackageRow:
    tracking_no: str
    order_id: str
    status: str
    delivered_at: str | None
    version: int


@dataclass(frozen=True, kw_only=True)
class CaseRow:
    case_id: str
    order_item_id: str
    case_type: str
    status: str
    version: int


@dataclass(frozen=True, kw_only=True)
class InventoryRow:
    sku: str
    available_qty: int
    version: int


@dataclass(frozen=True, kw_only=True)
class VariantRow:
    sku: str
    variant_group: str
    version: int


@dataclass(frozen=True, kw_only=True)
class TicketRow:
    ticket_id: str
    order_item_id: str
    handoff_trigger: str
    status: str
    version: int


@dataclass(frozen=True, kw_only=True)
class PendingRow:
    pending_action_id: str
    status: str
    version: int


@dataclass(frozen=True, kw_only=True)
class GuardEvidence:
    """The derived-fact inputs, built from this capture's rows only."""

    order_status: BusinessEvidence | None
    logistics: tuple[BusinessEvidence, ...]      # status + order_id of every package
    delivered_at: tuple[BusinessEvidence, ...]   # delivered_at of every package
    category: BusinessEvidence | None
    available_qty: BusinessEvidence | None


@dataclass(frozen=True, kw_only=True)
class GuardState:
    """Everything the Guard read, structured fields only. No free text."""

    action_name: str
    order: OrderRow | None
    item: OrderItemRow | None
    packages: tuple[PackageRow, ...]
    item_cases: tuple[CaseRow, ...]
    target_inventory: InventoryRow | None
    variants: tuple[VariantRow, ...]
    tickets: tuple[TicketRow, ...]
    other_pendings: tuple[PendingRow, ...]
    evidence: GuardEvidence
    versions: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]


GUARD_STATE_FIELDS = tuple(item.name for item in fields(GuardState))


# --------------------------------------------------------------------------
# Row checks
# --------------------------------------------------------------------------


def _malformed() -> GuardFailure:
    return GuardFailure(FAILURE_STATE_MALFORMED)


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GuardFailure(FAILURE_STATE_VERSION_MISSING)
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _malformed()
    return value


def _member(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _malformed()
    return value


def _count(value: object, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _malformed()
    return value


def _timestamp(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _malformed()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _malformed() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _malformed()
    return value


def _evidence(entity: str, record_id: str, field: str, value: object, *, version: int,
              updated_at: str, observed_at: str, relations: dict[str, str]) -> BusinessEvidence:
    locator = entity + ":" + record_id + "#" + field
    return BusinessEvidence(
        # Mechanical content: decide reads metadata, never content.
        content=locator + " = " + ("null" if value is None else str(value)),
        source_type=SourceType.BUSINESS,
        source=BUSINESS_SOURCE,
        locator=locator,
        version=None,
        observed_at=observed_at,
        authority=BUSINESS_AUTHORITY,
        confidence=None,
        metadata={
            "tool": GUARD_READER,
            "entity": entity,
            "record_id": record_id,
            "field": field,
            "value": value,
            "authority_scope": AUTHORITY_SCOPE,
            OBSERVATION_ID_KEY: GUARD_OBSERVATION_ID,
        },
        record_updated_at=updated_at,
        state_version=version,
        freshness_contract=SOURCE_FRESHNESS,
        source_as_of=None,
        relations=dict(relations),
    )


# --------------------------------------------------------------------------
# The reader
# --------------------------------------------------------------------------


class GuardStateReader:
    """Fixed templates, structured columns, one pass per capture."""

    def __init__(self, read_hook: GuardReadHook | None = None) -> None:
        if read_hook is not None and not callable(getattr(read_hook, "before_read", None)):
            raise ValueError("read_hook must provide before_read(read)")
        self._read_hook = read_hook

    def _fetch(self, read: str, connection: object, bindings: tuple) -> list[tuple]:
        if self._read_hook is not None:
            if self._read_hook.before_read(read) == READ_MALFORMED:
                return [(None,) * _READ_WIDTHS[read]]
        return _fetch(connection, GUARD_READS[read], bindings)

    def read(self, action: ValidatedAction, context: TrustedExecutionContext, *,
             txn_now: datetime, exclude_pending_id: str | None) -> GuardState:
        if not isinstance(action, ValidatedAction):
            raise ValueError("action must be a ValidatedAction")
        if not isinstance(context, TrustedExecutionContext):
            raise ValueError("context must be a TrustedExecutionContext")
        observed_at = require_aware("txn_now", txn_now).isoformat()
        if exclude_pending_id is not None and (
                not isinstance(exclude_pending_id, str) or not exclude_pending_id.strip()):
            raise ValueError("exclude_pending_id must be a non-empty string or None")
        try:
            return self._read(action, context, observed_at, exclude_pending_id)
        except GuardFailure:
            raise
        except sqlite3.Error:
            # Never "empty": a failed read is an infrastructure failure.
            raise GuardFailure(FAILURE_STATE_READ) from None

    # -- one pass ------------------------------------------------------------

    def _read(self, action: ValidatedAction, context: TrustedExecutionContext,
              observed_at: str, exclude: str | None) -> GuardState:
        connection = context.connection
        customer = context.customer_id  # bound predicate only, never selected
        name = action.action_name
        order_id = action.target_order_id
        item_id = action.target_order_item_id

        order_rows = self._fetch("order", connection, (customer, order_id))
        order = _single(order_rows, self._order)
        if order is None or order.order_id != order_id:
            if order is not None:
                raise _malformed()
            return _state(name, observed_at)
        order_updated = _timestamp(order_rows[0][2])

        item_rows = self._fetch("order_item", connection, (customer, order_id, item_id))
        item = _single(item_rows, self._item)
        if item is None:
            return _state(name, observed_at, order=order, order_updated=order_updated)
        if item.order_item_id != item_id or item.order_id != order_id:
            raise _malformed()
        item_updated = _timestamp(item_rows[0][5])

        packages: list[tuple[PackageRow, str]] = []
        cases: tuple[CaseRow, ...] = ()
        pendings: tuple[PendingRow, ...] = ()
        inventory: tuple[InventoryRow, str] | None = None
        variants: tuple[VariantRow, ...] = ()
        tickets: tuple[TicketRow, ...] = ()

        if name in (CREATE_RETURN, CREATE_EXCHANGE):
            for row in self._fetch("logistics", connection, (customer, order_id)):
                package = self._package(row)
                if package.order_id != order_id:
                    raise _malformed()
                packages.append((package, _timestamp(row[4])))
            if len({package.tracking_no for package, _ in packages}) != len(packages):
                raise _malformed()
            cases = tuple(self._case(row) for row in self._fetch("item_cases", connection, (item_id,)))
            if any(case.order_item_id != item_id for case in cases):
                raise _malformed()
            pendings = tuple(self._pending(row) for row in self._fetch(
                "pendings", connection, (item_id, exclude, exclude)))
        if name == CREATE_EXCHANGE:
            target = action.args["target_sku"]
            inventory_rows = self._fetch("inventory", connection, (target,))
            row = _single(inventory_rows, self._inventory)
            if row is not None:
                if row.sku != target:
                    raise _malformed()
                inventory = (row, _timestamp(inventory_rows[0][2]))
            variants = tuple(self._variant(row) for row in self._fetch(
                "variants", connection, (item.sku, target)))
            if len({variant.sku for variant in variants}) != len(variants) or not all(
                    variant.sku in (item.sku, target) for variant in variants):
                raise _malformed()
        if name == ESCALATE_TO_HUMAN:
            trigger = action.args["handoff_trigger"]
            tickets = tuple(self._ticket(row) for row in self._fetch(
                "tickets", connection, (item_id, trigger)))
            if any(ticket.order_item_id != item_id or ticket.handoff_trigger != trigger
                   for ticket in tickets):
                raise _malformed()

        return _state(
            name, observed_at, order=order, order_updated=order_updated,
            item=item, item_updated=item_updated, packages=tuple(packages), cases=cases,
            pendings=pendings, inventory=inventory, variants=variants, tickets=tickets,
        )

    # -- row decoding ----------------------------------------------------------

    @staticmethod
    def _order(row: Sequence[object]) -> OrderRow:
        return OrderRow(order_id=_text(row[0]), status=_member(row[1], ORDER_STATUSES),
                        version=_version(row[3]))

    @staticmethod
    def _item(row: Sequence[object]) -> OrderItemRow:
        return OrderItemRow(order_item_id=_text(row[0]), order_id=_text(row[1]),
                            sku=_text(row[2]), category=_text(row[3]),
                            quantity=_count(row[4], 1), version=_version(row[6]))

    @staticmethod
    def _package(row: Sequence[object]) -> PackageRow:
        return PackageRow(tracking_no=_text(row[0]), order_id=_text(row[1]),
                          status=_member(row[2], PACKAGE_STATUSES),
                          delivered_at=_timestamp(row[3], nullable=True),
                          version=_version(row[5]))

    @staticmethod
    def _case(row: Sequence[object]) -> CaseRow:
        _timestamp(row[4])
        return CaseRow(case_id=_text(row[0]), order_item_id=_text(row[1]),
                       case_type=_member(row[2], CASE_TYPES),
                       status=_member(row[3], CASE_STATUSES), version=_version(row[5]))

    @staticmethod
    def _inventory(row: Sequence[object]) -> InventoryRow:
        return InventoryRow(sku=_text(row[0]), available_qty=_count(row[1], 0),
                            version=_version(row[3]))

    @staticmethod
    def _variant(row: Sequence[object]) -> VariantRow:
        _timestamp(row[2])
        return VariantRow(sku=_text(row[0]), variant_group=_text(row[1]), version=_version(row[3]))

    @staticmethod
    def _ticket(row: Sequence[object]) -> TicketRow:
        _timestamp(row[4])
        return TicketRow(ticket_id=_text(row[0]), order_item_id=_text(row[1]),
                         handoff_trigger=_text(row[2]),
                         status=_member(row[3], TICKET_STATUSES), version=_version(row[5]))

    @staticmethod
    def _pending(row: Sequence[object]) -> PendingRow:
        return PendingRow(pending_action_id=_text(row[0]),
                          status=_member(row[1], PENDING_STATUSES), version=_version(row[2]))


def _fetch(connection: object, sql: str, bindings: tuple) -> list[tuple]:
    # The reader's own cursor with the factory cleared: the caller's
    # row_factory is left untouched and cannot change decoding.
    cursor = connection.cursor()
    try:
        cursor.row_factory = None
        cursor.execute(sql, bindings)
        return cursor.fetchall()
    finally:
        cursor.close()


def _single(rows: list[tuple], decode) -> object:
    if len(rows) > 1:
        raise _malformed()
    return decode(rows[0]) if rows else None


def _state(name: str, observed_at: str, *, order: OrderRow | None = None,
           order_updated: str | None = None, item: OrderItemRow | None = None,
           item_updated: str | None = None,
           packages: tuple[tuple[PackageRow, str], ...] = (),
           cases: tuple[CaseRow, ...] = (), pendings: tuple[PendingRow, ...] = (),
           inventory: tuple[InventoryRow, str] | None = None,
           variants: tuple[VariantRow, ...] = (), tickets: tuple[TicketRow, ...] = ()) -> GuardState:
    order_status = None
    if order is not None:
        order_status = _evidence("order", order.order_id, "status", order.status,
                                 version=order.version, updated_at=order_updated,
                                 observed_at=observed_at, relations={})
    logistics: list[BusinessEvidence] = []
    delivered: list[BusinessEvidence] = []
    for package, updated in packages:
        common = dict(version=package.version, updated_at=updated, observed_at=observed_at,
                      relations={"order_id": package.order_id})
        logistics.append(_evidence("logistics", package.tracking_no, "status", package.status, **common))
        logistics.append(_evidence("logistics", package.tracking_no, "order_id", package.order_id,
                                   **common))
        delivered.append(_evidence("logistics", package.tracking_no, "delivered_at",
                                   package.delivered_at, **common))
    category = None
    if item is not None:
        category = _evidence("order_item", item.order_item_id, "category", item.category,
                             version=item.version, updated_at=item_updated,
                             observed_at=observed_at, relations={"order_id": item.order_id})
    available = None
    target_inventory = None
    if inventory is not None:
        target_inventory, inventory_updated = inventory
        available = _evidence("inventory", target_inventory.sku, "available_qty",
                              target_inventory.available_qty, version=target_inventory.version,
                              updated_at=inventory_updated, observed_at=observed_at, relations={})

    records = {
        "orders": () if order is None else ((order.order_id, order.version),),
        "order_items": () if item is None else ((item.order_item_id, item.version),),
        "logistics": tuple((package.tracking_no, package.version) for package, _ in packages),
        "after_sales_cases": tuple((case.case_id, case.version) for case in cases),
        "inventory": () if target_inventory is None else (
            (target_inventory.sku, target_inventory.version),),
        "sku_variants": tuple((variant.sku, variant.version) for variant in variants),
        "human_handoff_tickets": tuple((ticket.ticket_id, ticket.version) for ticket in tickets),
    }
    versions = tuple(
        (table, tuple(sorted(records[table])))
        for table in sorted(RECORD_TABLES[name])
    )
    return GuardState(
        action_name=name,
        order=order,
        item=item,
        packages=tuple(package for package, _ in packages),
        item_cases=cases,
        target_inventory=target_inventory,
        variants=variants,
        tickets=tickets,
        other_pendings=pendings,
        evidence=GuardEvidence(
            order_status=order_status,
            logistics=tuple(logistics),
            delivered_at=tuple(delivered),
            category=category,
            available_qty=available,
        ),
        versions=versions,
    )
