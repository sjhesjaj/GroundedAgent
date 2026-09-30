"""The deterministic V2 eval runtime foundation (Stage 4.4.0).

One case-run, from a case dict to a read-only, fully-checked execution
environment:

    case
    -> contract validation (the frozen eval/v2/case_contract.py, unchanged)
    -> fresh base fixture (aftersales/schema.sql + the demo seed)
    -> initial_state overlay (fixed table metadata, bound values, one transaction)
    -> PRAGMA foreign_key_check
    -> initial DB content hash
    -> PRAGMA query_only
    -> FixedClock(virtual_now)
    -> trusted persona (server-side, drift-checked against the frozen mapping)
    -> exactly-five read-only ToolRegistry
    -> direct tool observation through the existing executor
    -> complete get_logistics observation gate

This is foundation only. There is no Planner, no LLM, no scorer, and no dataset
here. Fault injection lives in `eval_v2.faults`: a case that declares faults can
be built here, but its tool calls must go through that case-run's single
`FaultInjectingGateway`; the direct path refuses it (`FaultGatewayRequired`).

Nothing here reads the system clock, generates an id, or depends on SQLite row
order. The case contract is loaded by path so this package does not depend on
`eval/` being importable, and nothing here touches the holdout or its unseal tool.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sqlite3
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Mapping, Sequence

from aftersales.business_tools import GET_LOGISTICS
from aftersales.clock import Clock, FixedClock
from aftersales.context import TrustedExecutionContext
from aftersales.demo import DEMO_PERSONAS, DEMO_SEED_PATH, resolve_persona
from aftersales.derived import derive_item_window_eligibility
from aftersales.executor import TRACE_OBSERVATION_ID, execute_tool
from aftersales.policy import PolicyRecord
from aftersales.registry import ToolRegistry, build_runtime_registry
from aftersales.schema import SCHEMA_PATH, TABLE_COLUMNS
from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    DerivedEvidence,
    ToolResult,
    ToolStatus,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CASE_CONTRACT_PATH = REPO_ROOT / "eval" / "v2" / "case_contract.py"

# The Stage 4 runtime registry, exactly. A sixth tool, a missing one, or any
# side effect is drift and the runtime refuses to start.
EXPECTED_TOOL_NAMES = (
    "search_after_sales_policy",
    "get_order",
    "get_logistics",
    "get_inventory",
    "get_after_sales_case",
)

# Fixed overlay metadata: table -> primary key. A case names neither tables nor
# columns in SQL; its keys are only ever checked against these constants.
OVERLAY_PRIMARY_KEYS: Mapping[str, str] = {
    "orders": "order_id",
    "order_items": "order_item_id",
    "logistics": "tracking_no",
    "inventory": "sku",
    "after_sales_cases": "case_id",
}

# Parents before children on the way in, children before parents on the way
# out, so the immediate foreign-key checks never depend on case key order.
INSERT_ORDER = ("orders", "inventory", "order_items", "logistics", "after_sales_cases")
UPDATE_ORDER = ("orders", "inventory", "order_items", "logistics", "after_sales_cases")
DELETE_ORDER = ("after_sales_cases", "logistics", "order_items", "inventory", "orders")

# The five business tables the content hash covers, in a fixed order.
HASHED_TABLES = ("orders", "order_items", "logistics", "inventory", "after_sales_cases")
DB_CONTENT_HASH_SCHEMA = "v2-db-content/1"

_INITIAL_STATE_KEYS = frozenset({"trusted_context", "faults"}) | frozenset(OVERLAY_PRIMARY_KEYS)
_PATCH_KEYS = {"insert": frozenset({"op", "row"}), "update": frozenset({"op", "set"}),
               "delete": frozenset({"op"})}

FAULT_GATEWAY_MESSAGE = "faulted case must execute through FaultInjectingGateway"


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class EvalRuntimeError(RuntimeError):
    """Base class: the eval runtime refused to build or run a case."""


class EvalCaseInvalid(EvalRuntimeError):
    """The case does not satisfy the frozen case contract."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("case violates the V2 case contract: " + "; ".join(self.errors))


class EvalFixtureError(EvalRuntimeError):
    """The initial_state overlay could not be applied exactly as written."""


class EvalRuntimeDrift(EvalRuntimeError):
    """The runtime no longer matches what the frozen contract promises."""


class FaultGatewayRequired(EvalRuntimeError):
    """The case declares faults, so its calls must go through its fault gateway."""


class IncompleteLogisticsObservation(EvalRuntimeError):
    """A get_logistics result is not one complete, self-consistent observation."""


class DatabaseChanged(EvalRuntimeError):
    """The case database no longer matches its initial content hash."""


# --------------------------------------------------------------------------
# Case contract (frozen, reused unchanged)
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def case_contract() -> ModuleType:
    """The frozen eval/v2/case_contract.py, loaded by path. Never copied."""
    spec = importlib.util.spec_from_file_location("_eval_v2_case_contract", CASE_CONTRACT_PATH)
    if spec is None or spec.loader is None:
        raise EvalRuntimeDrift("cannot load the frozen case contract")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_case(case: object) -> None:
    """Raise EvalCaseInvalid unless `case_errors(case) == []`."""
    errors = case_contract().case_errors(case)
    if errors:
        raise EvalCaseInvalid(errors)


def parse_virtual_now(value: object) -> datetime:
    """The case's business instant. Timezone-aware, or the runtime refuses."""
    if not isinstance(value, str):
        raise EvalCaseInvalid(["$.virtual_now: must be a string"])
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise EvalCaseInvalid(["$.virtual_now: not an ISO-8601 timestamp"]) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvalCaseInvalid(["$.virtual_now: must carry a timezone offset"])
    return parsed


# --------------------------------------------------------------------------
# Drift checks
# --------------------------------------------------------------------------


def check_persona_mapping() -> None:
    """The server-side personas must equal the frozen personas.json mapping."""
    frozen = case_contract().persona_customer_ids()
    runtime = {pid: persona.customer_id for pid, persona in DEMO_PERSONAS.items()}
    if frozen != runtime:
        # Ids only: customer ids stay out of error text.
        raise EvalRuntimeDrift(
            "server-side personas disagree with eval/v2/spec/personas.json for: "
            + ", ".join(sorted(pid for pid in set(frozen) | set(runtime)
                               if frozen.get(pid) != runtime.get(pid)))
        )


def resolve_case_persona(persona_id: object):
    """Resolve the case's persona server-side and confirm the frozen mapping."""
    check_persona_mapping()
    persona = resolve_persona(persona_id)  # raises on an unknown id
    if case_contract().persona_customer_ids().get(persona.persona_id) != persona.customer_id:
        raise EvalRuntimeDrift("persona " + persona.persona_id + " drifted from the frozen mapping")
    return persona


def check_runtime_registry(registry: object) -> None:
    """Exactly the five Stage 4 tools, all read-only, with the frozen arguments."""
    if not isinstance(registry, ToolRegistry):
        raise EvalRuntimeDrift("registry must be a ToolRegistry")
    names = registry.names()
    if len(names) != len(EXPECTED_TOOL_NAMES) or set(names) != set(EXPECTED_TOOL_NAMES):
        raise EvalRuntimeDrift(
            "runtime registry must hold exactly " + ", ".join(EXPECTED_TOOL_NAMES)
            + "; it holds " + ", ".join(names)
        )
    arguments = case_contract().TOOL_ARGUMENTS
    for spec in registry:
        if spec.side_effect is not False:
            raise EvalRuntimeDrift(spec.name + " declares a side effect")
        if tuple(spec.parameter_names) != tuple(arguments[spec.name]):
            raise EvalRuntimeDrift(spec.name + " parameters disagree with the case contract")


# --------------------------------------------------------------------------
# Fixture overlay
# --------------------------------------------------------------------------


def _check_value(where: str, value: object) -> None:
    # The schema's column types are TEXT, INTEGER, or NULL. A bool would bind
    # as 0/1 and a float would silently coerce; neither is accepted.
    if value is None or isinstance(value, str):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    raise EvalFixtureError(where + " must be a string, an integer, or null")


def _checked_patches(initial_state: Mapping) -> dict[str, list[tuple[str, dict]]]:
    """Structural check of every patch, before the database is touched."""
    if not isinstance(initial_state, Mapping):
        raise EvalFixtureError("initial_state must be an object")
    unknown = sorted(str(key) for key in initial_state if key not in _INITIAL_STATE_KEYS)
    if unknown:
        raise EvalFixtureError("initial_state has unknown key(s): " + ", ".join(unknown))

    patches: dict[str, list[tuple[str, dict]]] = {}
    for table, pk in OVERLAY_PRIMARY_KEYS.items():
        entries = initial_state.get(table, {})
        if not isinstance(entries, Mapping):
            raise EvalFixtureError(table + " overlay must be an object")
        columns = frozenset(TABLE_COLUMNS[table]) - {pk}
        if not all(isinstance(key, str) and key.strip() for key in entries):
            raise EvalFixtureError(table + " keys must be non-empty strings")
        checked = []
        # Sorted, so the result never depends on the case's JSON key order.
        for key in sorted(entries):
            where = table + "[" + key + "]"
            patch = entries[key]
            if not isinstance(patch, Mapping) or patch.get("op") not in _PATCH_KEYS:
                raise EvalFixtureError(where + " must be an insert, update, or delete patch")
            op = patch["op"]
            if set(patch) != _PATCH_KEYS[op]:
                raise EvalFixtureError(where + " " + op + " patch has the wrong keys")
            if op == "insert":
                row = patch["row"]
                if not isinstance(row, Mapping) or set(row) != columns:
                    raise EvalFixtureError(
                        where + ".row must give exactly the non-key columns of " + table
                    )
                for column in row:
                    _check_value(where + ".row." + column, row[column])
            elif op == "update":
                values = patch["set"]
                if not isinstance(values, Mapping) or not values:
                    raise EvalFixtureError(where + ".set must be a non-empty object")
                if pk in values:
                    raise EvalFixtureError(where + ".set must not change the primary key")
                bad = sorted(str(column) for column in values if column not in columns)
                if bad:
                    raise EvalFixtureError(where + ".set has unknown column(s): " + ", ".join(bad))
                for column in values:
                    _check_value(where + ".set." + column, values[column])
            checked.append((key, dict(patch)))
        patches[table] = checked
    return patches


def _insert(connection: sqlite3.Connection, table: str, key: str, row: Mapping) -> None:
    pk = OVERLAY_PRIMARY_KEYS[table]
    # Identifiers come from TABLE_COLUMNS, never from the case.
    columns = TABLE_COLUMNS[table]
    values = [key if column == pk else row[column] for column in columns]
    connection.execute(
        "INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES ("
        + ", ".join("?" for _ in columns) + ")",
        values,
    )


def _update(connection: sqlite3.Connection, table: str, key: str, values: Mapping) -> None:
    pk = OVERLAY_PRIMARY_KEYS[table]
    columns = [column for column in TABLE_COLUMNS[table] if column in values]
    cursor = connection.execute(
        "UPDATE " + table + " SET " + ", ".join(column + " = ?" for column in columns)
        + " WHERE " + pk + " = ?",
        [values[column] for column in columns] + [key],
    )
    if cursor.rowcount != 1:
        raise EvalFixtureError(table + "[" + key + "] update matched no existing row")


def _delete(connection: sqlite3.Connection, table: str, key: str) -> None:
    pk = OVERLAY_PRIMARY_KEYS[table]
    cursor = connection.execute("DELETE FROM " + table + " WHERE " + pk + " = ?", (key,))
    if cursor.rowcount != 1:
        raise EvalFixtureError(table + "[" + key + "] delete matched no existing row")


def apply_initial_state_overlay(connection: sqlite3.Connection, initial_state: Mapping) -> None:
    """Apply the case's overlay verbatim, in one transaction, or not at all.

    Inserts, then updates, then deletes, each in the fixed table order above.
    Nothing is defaulted, bumped, or timestamped: `version` and `updated_at`
    are whatever the case wrote.
    """
    patches = _checked_patches(initial_state)
    if connection.in_transaction:
        raise EvalFixtureError("overlay needs a connection with no open transaction")
    connection.execute("BEGIN")
    try:
        for table in INSERT_ORDER:
            for key, patch in patches[table]:
                if patch["op"] == "insert":
                    _insert(connection, table, key, patch["row"])
        for table in UPDATE_ORDER:
            for key, patch in patches[table]:
                if patch["op"] == "update":
                    _update(connection, table, key, patch["set"])
        for table in DELETE_ORDER:
            for key, patch in patches[table]:
                if patch["op"] == "delete":
                    _delete(connection, table, key)
    except sqlite3.Error as exc:
        connection.execute("ROLLBACK")
        # SQLite's text names the violated constraint, never a bound value.
        raise EvalFixtureError(
            "overlay rejected by the database (" + type(exc).__name__ + ": " + str(exc) + ")"
        ) from exc
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def require_foreign_keys_intact(connection: sqlite3.Connection) -> None:
    """PRAGMA foreign_key_check must be empty. Nothing is repaired."""
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = sorted({str(row[0]) for row in violations})
        raise EvalFixtureError(
            "fixture violates foreign keys in: " + ", ".join(tables)
            + " (" + str(len(violations)) + " row(s))"
        )


# --------------------------------------------------------------------------
# Database content hash
# --------------------------------------------------------------------------


def database_content_sha256(connection: sqlite3.Connection) -> str:
    """SHA-256 of the five business tables' content - not of the file bytes.

    Each table is read with its declared column order, rows are sorted by
    primary key in Python, and the whole is canonical JSON. So the hash depends
    only on business state: not on SQLite row order, insertion order, the
    connection object, or the clock. A stray table or column is drift.
    """
    present = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if present != set(HASHED_TABLES):
        raise EvalRuntimeDrift("database tables differ from the five business tables")
    tables: dict[str, object] = {}
    for table in HASHED_TABLES:
        columns = TABLE_COLUMNS[table]
        declared = tuple(row[1] for row in connection.execute(
            "PRAGMA table_info(" + table + ")").fetchall())
        if declared != columns:
            raise EvalRuntimeDrift(table + " columns differ from aftersales.schema")
        cursor = connection.cursor()
        try:
            cursor.row_factory = None
            rows = cursor.execute("SELECT " + ", ".join(columns) + " FROM " + table).fetchall()
        finally:
            cursor.close()
        for row in rows:
            for column, value in zip(columns, row):
                if not (value is None or isinstance(value, (str, int))):
                    raise EvalRuntimeDrift(table + "." + column + " holds a non-JSON value")
        key = columns.index(OVERLAY_PRIMARY_KEYS[table])
        tables[table] = {
            "columns": list(columns),
            "rows": [list(row) for row in sorted(rows, key=lambda row: row[key])],
        }
    canonical = json.dumps(
        {"schema": DB_CONTENT_HASH_SCHEMA, "tables": tables},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The case runtime
# --------------------------------------------------------------------------


def _open_case_database(initial_state: Mapping) -> tuple[sqlite3.Connection, str]:
    """A fresh :memory: database with the overlay applied, then made read-only."""
    # Autocommit mode: the overlay manages its own single transaction.
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise EvalFixtureError("SQLite refused to enable foreign keys")
        # The existing files, never a copy of their SQL.
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.executescript(DEMO_SEED_PATH.read_text(encoding="utf-8"))
        apply_initial_state_overlay(connection, initial_state)
        require_foreign_keys_intact(connection)
        initial_sha256 = database_content_sha256(connection)
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise EvalFixtureError("SQLite refused to enable query_only")
    except BaseException:
        connection.close()
        raise
    return connection, initial_sha256


_CONSTRUCT = object()


class V2CaseRuntime:
    """One case-run: validated case, fresh read-only database, trusted context.

    Build it with `V2CaseRuntime.from_case(case)`, preferably as a context
    manager so the connection is always closed.
    """

    def __init__(self, token: object, *, case: dict, connection: sqlite3.Connection,
                 context: TrustedExecutionContext, registry: ToolRegistry,
                 initial_db_sha256: str) -> None:
        if token is not _CONSTRUCT:
            raise TypeError("use V2CaseRuntime.from_case(case)")
        self._case = case
        self._connection = connection
        self._context = context
        self._registry = registry
        self._initial_db_sha256 = initial_db_sha256
        self._faults = tuple(copy.deepcopy(case["initial_state"]["faults"]))
        self._gateway_claimed = False
        self._closed = False

    @classmethod
    def from_case(cls, case: object) -> "V2CaseRuntime":
        # One private snapshot: validation and execution see the same case.
        snapshot = copy.deepcopy(case)
        validate_case(snapshot)
        clock = FixedClock(parse_virtual_now(snapshot["virtual_now"]))
        persona = resolve_case_persona(snapshot["initial_state"]["trusted_context"]["persona_id"])
        registry = build_runtime_registry()
        check_runtime_registry(registry)
        connection, initial_sha256 = _open_case_database(snapshot["initial_state"])
        try:
            context = TrustedExecutionContext(persona=persona, clock=clock, connection=connection)
            return cls(_CONSTRUCT, case=snapshot, connection=connection, context=context,
                       registry=registry, initial_db_sha256=initial_sha256)
        except BaseException:
            connection.close()
            raise

    # -- read-only views ---------------------------------------------------

    @property
    def case(self) -> dict:
        return copy.deepcopy(self._case)

    @property
    def case_id(self) -> str:
        return self._case["case_id"]

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def context(self) -> TrustedExecutionContext:
        return self._context

    @property
    def clock(self) -> Clock:
        return self._context.clock

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def initial_db_sha256(self) -> str:
        return self._initial_db_sha256

    @property
    def faults(self) -> tuple[dict, ...]:
        return copy.deepcopy(self._faults)

    @property
    def closed(self) -> bool:
        return self._closed

    # -- invariants --------------------------------------------------------

    def require_open(self) -> None:
        if self._closed:
            raise EvalRuntimeError("case runtime is closed")

    def database_sha256(self) -> str:
        self.require_open()
        return database_content_sha256(self._connection)

    def assert_database_unchanged(self) -> None:
        """Stage 4/5 cases expect no final state: the content must not move."""
        if self.database_sha256() != self._initial_db_sha256:
            raise DatabaseChanged("case database content changed since the fixture was built")

    # -- tool gateway --------------------------------------------------------

    def claim_tool_gateway(self) -> None:
        """Bind this case-run to exactly one tool gateway.

        Fault counters belong to the whole case-run. A second gateway would
        start them again from zero, so a second claim is refused.
        """
        self.require_open()
        if self._gateway_claimed:
            raise EvalRuntimeError("case runtime already has a tool gateway")
        self._gateway_claimed = True

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connection.close()

    def __enter__(self) -> "V2CaseRuntime":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        return ("V2CaseRuntime(case_id=" + repr(self._case["case_id"])
                + ", clock=" + repr(self._context.clock)
                + ", closed=" + repr(self._closed) + ")")


# --------------------------------------------------------------------------
# Direct observation (foundation only)
# --------------------------------------------------------------------------


def execute_observation(runtime: V2CaseRuntime, tool_name: str, arguments: Mapping[str, str],
                        *, observation_id: str) -> ToolResult:
    """Run one tool call through the existing executor, with every guard intact.

    `observation_id` is supplied by the caller (the future trace / control
    layer); it is never generated here. A case with faults is refused: running
    it without the fault gateway would produce a fake result, and a gateway
    created here per call would restart its on_call counters.
    """
    if not isinstance(runtime, V2CaseRuntime):
        raise ValueError("runtime must be a V2CaseRuntime, got " + type(runtime).__name__)
    runtime.require_open()
    if not isinstance(observation_id, str) or not observation_id.strip():
        raise ValueError("observation_id is required and must be a non-empty string")
    if runtime.faults:
        raise FaultGatewayRequired(FAULT_GATEWAY_MESSAGE)
    return execute_tool(runtime.registry, runtime.context, tool_name, arguments,
                        observation_id=observation_id)


# --------------------------------------------------------------------------
# Complete get_logistics observation gate
# --------------------------------------------------------------------------


def _trace_count(trace: Mapping, name: str) -> int:
    value = trace[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IncompleteLogisticsObservation("trace." + name + " must be a non-negative integer")
    return value


def complete_delivered_at_evidence(result: ToolResult) -> tuple[BusinessEvidence, ...]:
    """Every package's delivered_at from ONE complete get_logistics observation.

    Accepts a whole ToolResult only - never an evidence list - and returns one
    delivered_at item per logistics record the observation matched, delivered
    or not, sorted by record_id. It never filters by value, picks a package, or
    chooses the earliest / latest delivery. Anything that does not add up to a
    complete observation raises IncompleteLogisticsObservation.
    """
    if not isinstance(result, ToolResult):
        raise IncompleteLogisticsObservation(
            "expected a get_logistics ToolResult, got " + type(result).__name__)
    if result.tool_name != GET_LOGISTICS:
        raise IncompleteLogisticsObservation("result is not a get_logistics observation")
    if result.status is ToolStatus.ERROR:
        raise IncompleteLogisticsObservation("get_logistics failed; an ERROR is not an observation")
    if result.status not in (ToolStatus.OK, ToolStatus.EMPTY):
        raise IncompleteLogisticsObservation("result status must be ok or empty")

    trace = result.trace
    if not isinstance(trace, Mapping):
        raise IncompleteLogisticsObservation("result trace must be a mapping")
    for name in (TRACE_OBSERVATION_ID, "records_matched", "evidence_count"):
        if name not in trace:
            raise IncompleteLogisticsObservation("trace is missing " + name)
    observation_id = trace[TRACE_OBSERVATION_ID]
    if not isinstance(observation_id, str) or not observation_id.strip():
        raise IncompleteLogisticsObservation("trace.observation_id must be a non-empty string")
    records_matched = _trace_count(trace, "records_matched")
    evidence = result.evidence
    if not isinstance(evidence, tuple):
        raise IncompleteLogisticsObservation("result evidence must be a tuple")
    if _trace_count(trace, "evidence_count") != len(evidence):
        raise IncompleteLogisticsObservation("trace.evidence_count disagrees with the evidence")

    if result.status is ToolStatus.EMPTY:
        if records_matched != 0 or evidence:
            raise IncompleteLogisticsObservation("an empty observation must match no records")
        return ()
    if not evidence:
        raise IncompleteLogisticsObservation("an ok observation must carry evidence")

    record_ids: set[str] = set()
    order_ids: set[str] = set()
    observed_at: set[object] = set()
    delivered: list[BusinessEvidence] = []
    for index, item in enumerate(evidence):
        where = "evidence[" + str(index) + "]"
        if not isinstance(item, BusinessEvidence):
            raise IncompleteLogisticsObservation(where + " must be BusinessEvidence")
        metadata = item.metadata
        if not isinstance(metadata, Mapping):
            raise IncompleteLogisticsObservation(where + ".metadata must be a mapping")
        if metadata.get("tool") != GET_LOGISTICS or metadata.get("entity") != "logistics":
            raise IncompleteLogisticsObservation(where + " is not a logistics field")
        record_id = metadata.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise IncompleteLogisticsObservation(where + ".metadata.record_id must be non-empty")
        if metadata.get(OBSERVATION_ID_KEY) != observation_id:
            raise IncompleteLogisticsObservation(where + " belongs to another observation")
        relations = item.relations
        order_id = relations.get("order_id") if isinstance(relations, Mapping) else None
        if not isinstance(order_id, str) or not order_id.strip():
            raise IncompleteLogisticsObservation(where + " has no order_id relation")
        record_ids.add(record_id)
        order_ids.add(order_id)
        observed_at.add(item.observed_at)
        if metadata.get("field") == "delivered_at":
            delivered.append(item)

    if len(record_ids) != records_matched:
        raise IncompleteLogisticsObservation(
            "trace.records_matched disagrees with the distinct logistics records")
    if len(order_ids) != 1:
        raise IncompleteLogisticsObservation("observation spans more than one order")
    if len(observed_at) != 1:
        raise IncompleteLogisticsObservation("observation has more than one observed_at")
    per_record = Counter(item.metadata["record_id"] for item in delivered)
    if set(per_record) != record_ids or any(count != 1 for count in per_record.values()):
        raise IncompleteLogisticsObservation(
            "every logistics record needs exactly one delivered_at field")
    if len(delivered) != records_matched:
        raise IncompleteLogisticsObservation("delivered_at count disagrees with records_matched")
    return tuple(sorted(delivered, key=lambda item: item.metadata["record_id"]))


def derive_item_window_from_logistics_result(
    logistics_result: ToolResult,
    policy: PolicyRecord,
    *,
    clock: Clock,
    category: BusinessEvidence,
) -> DerivedEvidence:
    """Item-level window eligibility from one complete get_logistics observation.

    The only item-window path the eval runtime offers: the whole observation
    goes through the completeness gate, then the ambiguity gate of
    `derive_item_window_eligibility`. No package is ever chosen here.
    """
    deliveries = complete_delivered_at_evidence(logistics_result)
    return derive_item_window_eligibility(deliveries, policy, clock=clock, category=category)
