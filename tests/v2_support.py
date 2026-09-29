"""Shared fixtures for the V2 after-sales tests. Offline; no file is created."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from aftersales.clock import BUSINESS_TIMEZONE, FixedClock
from aftersales.context import TrustedExecutionContext
from aftersales.demo import DEMO_SEED_PATH, DEMO_VIRTUAL_NOW, resolve_persona
from aftersales.policy import PolicyRecord, PolicyRuleType
from aftersales.schema import SCHEMA_PATH, TABLE_COLUMNS
from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    FreshnessContract,
    SourceType,
)

PERSONA_A = "demo-a"
PERSONA_B = "demo-b"
CUSTOMER_A = "CUST-001"
CUSTOMER_B = "CUST-002"

ORDER_A_DELIVERED = "ORD-1001"
ORDER_A_IN_TRANSIT = "ORD-1002"
ORDER_A_UNPAID = "ORD-1003"
ORDER_A_TWO_PACKAGES = "ORD-1004"
ORDER_B_DELIVERED = "ORD-2001"
SKU_STOCKED = "SKU-TSHIRT-M"
SKU_ZERO = "SKU-TSHIRT-L"

NON_STRING_VALUES = (1, True, None, [], {}, 1.5)

# One valid call per business tool, as persona A.
VALID_BUSINESS_CALLS = (
    ("get_order", {"order_id": ORDER_A_DELIVERED}),
    ("get_logistics", {"order_id": ORDER_A_DELIVERED}),
    ("get_inventory", {"sku": SKU_STOCKED}),
    ("get_after_sales_case", {"order_id": ORDER_A_DELIVERED}),
)
IDENTITY_SCOPED_CALLS = tuple(
    call for call in VALID_BUSINESS_CALLS if call[0] != "get_inventory"
)


def memory_connection() -> sqlite3.Connection:
    """A fresh, writable in-memory demo database (tests may break it on purpose)."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.executescript(DEMO_SEED_PATH.read_text(encoding="utf-8"))
    return connection


def snapshot(connection: sqlite3.Connection) -> dict[str, list]:
    return {
        table: connection.execute("SELECT * FROM " + table + " ORDER BY 1").fetchall()
        for table in TABLE_COLUMNS
    }


def make_context(
    connection,
    persona_id: str = PERSONA_A,
    now: datetime = DEMO_VIRTUAL_NOW,
) -> TrustedExecutionContext:
    return TrustedExecutionContext(
        persona=resolve_persona(persona_id),
        clock=FixedClock(now),
        connection=connection,
    )


def at(year: int, month: int = 1, day: int = 1, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BUSINESS_TIMEZONE)


WINDOW_PARAMS_7D = {
    "window_days": 7,
    "start_event": "delivered",
    "counting_rule": "natural_days_from_next_day",
    "utc_offset": "+08:00",
}


def window_policy(**overrides) -> PolicyRecord:
    """A valid 7-day return-window rule, in force from 2026-01-01 on."""
    values = dict(
        policy_id="P-RETURN-7D",
        version="1",
        title="七天无理由退货",
        rule_type=PolicyRuleType.RETURN_WINDOW,
        scope=(),
        params=dict(WINDOW_PARAMS_7D),
        effective_from="2026-01-01T00:00:00+08:00",
        effective_to=None,
        source_doc="aftersales_rules.md",
        locator="aftersales_rules.md#return-window",
        build_id="build-1",
    )
    values.update(overrides)
    return PolicyRecord(**values)


def business_evidence(
    entity: str,
    record_id: str,
    field: str,
    value,
    *,
    observed_at: str = DEMO_VIRTUAL_NOW.isoformat(),
    record_updated_at: str | None = "2026-11-01T00:00:00+08:00",
    state_version: int = 1,
    freshness_contract: FreshnessContract = FreshnessContract.AUTHORITATIVE_ONLINE,
    source_as_of: str | None = None,
    observation_id: str | None = None,
) -> BusinessEvidence:
    """One hand-built business field, shaped exactly like a tool's output."""
    return BusinessEvidence(
        content=entity + " " + record_id + " " + field + " " + str(value),
        source_type=SourceType.BUSINESS,
        source="aftersales-demo-db",
        locator=entity + ":" + record_id + "#" + field,
        observed_at=observed_at,
        authority=100,
        metadata={
            "tool": "test",
            "entity": entity,
            "record_id": record_id,
            "field": field,
            "value": value,
            "authority_scope": "current_operational_state",
            OBSERVATION_ID_KEY: observation_id,
        },
        record_updated_at=record_updated_at,
        state_version=state_version,
        freshness_contract=freshness_contract,
        source_as_of=source_as_of,
    )


class RecordingCursor:
    """Delegates to a real cursor while recording what the tool did."""

    def __init__(self, cursor, recorder):
        self._cursor = cursor
        self._recorder = recorder
        self.closed = False

    @property
    def row_factory(self):
        return self._cursor.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._cursor.row_factory = value

    def execute(self, sql, bindings=()):
        self._recorder.executed.append((sql, bindings))
        return self._cursor.execute(sql, bindings)

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self.closed = True
        self._recorder.closed_cursors += 1
        return self._cursor.close()


class RecordingConnection:
    """Only `cursor()` is used by the tools; everything else delegates."""

    def __init__(self, connection):
        self._connection = connection
        self.cursor_calls = 0
        self.closed_cursors = 0
        self.executed: list[tuple] = []
        self.cursors: list[RecordingCursor] = []

    def cursor(self):
        self.cursor_calls += 1
        recording = RecordingCursor(self._connection.cursor(), self)
        self.cursors.append(recording)
        return recording

    def __getattr__(self, name):
        return getattr(self._connection, name)
