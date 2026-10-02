"""Stage 6 database state for evaluation (docs/v2/stage6-design.md §19.2).

    fixture patches, trusted mutate events     -> applied in BEGIN IMMEDIATE
    read_state(connection)                     -> {table: {primary key: row}}
    build_baseline(case, directory)            -> baseline B, from its own database
    compare_final_state(expected, B, F)        -> the frozen comparator
    check_links(B, F, ...)                     -> link invariants L1-L6

Baseline B is fresh Stage 6 fixture + initial_state patches + every mutate
event of the operator_script, in script order, in a separate database: no
model, no ActionGateway, no approval, receipt or pending effect. It is never
obtained by undoing writes in F.

Comparator (per compared table, B -> F)
    rows in both     equal, except the columns listed in update[pk]
    rows only in B   exactly the delete list
    rows only in F   matched one-to-one with the insert entries on the
                     author-writable columns (generated ids and digests are
                     never written by authors); no unmatched new row
    unlisted table   unchanged
The audit table is not compared (audit_trace_ok scores it).

Link invariants over the new rows (F minus B)
    L1  each receipt names exactly one new business row of its resource type,
        for the receipt's own order item; each new business row has exactly
        one receipt
    L2  receipt.pending_action_id <-> an EXECUTED pending whose receipt_id is
        that receipt; every EXECUTED pending has one
    L3  idempotency_key and args_sha256 recompute from persona, request,
        action and args (and args_json is the canonical form)
    L4  snapshot_sha256 = sha256(snapshot_json) and the strict
        s6-guard-snapshot/1 parse succeeds
    L5  every generated id is the DeterministicIdProvider id of its key
    L6  every new after-sales case belongs to the trusted customer
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from aftersales.action_db import create_stage6_database
from aftersales.action_errors import ActionValidationError, SnapshotIntegrityError
from aftersales.actions import ACTION_NAMES, ActionIntentValidator, build_action_registry
from aftersales.guard_snapshot import parse_snapshot_document
from aftersales.ids import DeterministicIdProvider, IdKind, RequestIdentity, idempotency_key
from aftersales.schema import TABLE_COLUMNS

from .runtime import EvalFixtureError

BUSINESS_TABLES = ("orders", "order_items", "logistics", "inventory", "after_sales_cases",
                   "sku_variants", "human_handoff_tickets")
ACTION_TABLES = ("pending_actions", "action_receipts")
COMPARED_TABLES = BUSINESS_TABLES + ACTION_TABLES
AUDIT_TABLE = "action_audit_events"

PRIMARY_KEYS: Mapping[str, str] = {
    "orders": "order_id", "order_items": "order_item_id", "logistics": "tracking_no",
    "inventory": "sku", "after_sales_cases": "case_id", "sku_variants": "sku",
    "human_handoff_tickets": "ticket_id", "pending_actions": "pending_action_id",
    "action_receipts": "receipt_id",
}

STAGE6_COLUMNS: Mapping[str, tuple[str, ...]] = {
    **{table: TABLE_COLUMNS[table] for table in BUSINESS_TABLES[:5]},
    "sku_variants": ("sku", "variant_group", "updated_at", "version"),
    "human_handoff_tickets": ("ticket_id", "order_id", "order_item_id", "handoff_trigger", "status",
                              "created_at", "updated_at", "version"),
    "pending_actions": ("pending_action_id", "idempotency_key", "request_id", "persona_id",
                        "action_name", "args_json", "args_sha256", "target_order_id",
                        "target_order_item_id", "status", "guard_decision", "guard_reason_code",
                        "snapshot_json", "snapshot_sha256", "action_spec_version",
                        "risk_policy_version", "policy_build_id", "approval_decision",
                        "approver_ref", "decided_at", "outcome_code", "receipt_id", "created_at",
                        "updated_at", "version"),
    "action_receipts": ("receipt_id", "idempotency_key", "request_id", "persona_id", "action_name",
                        "args_json", "args_sha256", "result_status", "resource_type", "resource_id",
                        "pending_action_id", "guard_decision", "guard_reason_code", "snapshot_json",
                        "snapshot_sha256", "action_spec_version", "risk_policy_version",
                        "policy_build_id", "executed_at"),
    AUDIT_TABLE: ("event_seq", "event_name", "request_id", "persona_id", "action_name",
                  "idempotency_key", "pending_action_id", "receipt_id", "phase", "decision", "code",
                  "approver_ref", "at"),
}

# What an expected insert row gives, exactly (§19.2 rule 4). `args` stands for
# the parsed args_json.
AUTHOR_WRITABLE: Mapping[str, tuple[str, ...]] = {
    "after_sales_cases": tuple(c for c in STAGE6_COLUMNS["after_sales_cases"] if c != "case_id"),
    "human_handoff_tickets": tuple(c for c in STAGE6_COLUMNS["human_handoff_tickets"]
                                   if c != "ticket_id"),
    "pending_actions": ("request_id", "persona_id", "action_name", "args", "target_order_id",
                        "target_order_item_id", "status", "guard_decision", "guard_reason_code",
                        "action_spec_version", "risk_policy_version", "policy_build_id",
                        "approval_decision", "approver_ref", "decided_at", "outcome_code",
                        "created_at", "updated_at", "version"),
    "action_receipts": ("request_id", "persona_id", "action_name", "args", "result_status",
                        "resource_type", "guard_decision", "guard_reason_code",
                        "action_spec_version", "risk_policy_version", "policy_build_id",
                        "executed_at"),
}

# Parents first on the way in, children first on the way out.
INSERT_ORDER = ("orders", "inventory", "sku_variants", "order_items", "logistics",
                "after_sales_cases", "human_handoff_tickets")
DELETE_ORDER = tuple(reversed(INSERT_ORDER))

TableState = Mapping[str, Mapping[str, Mapping[str, object]]]


# --------------------------------------------------------------------------
# Patches
# --------------------------------------------------------------------------


def _check_value(where: str, value: object) -> None:
    if value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
        return
    raise EvalFixtureError(where + " must be a string, an integer, or null")


def _checked(table: str, key: str, patch: object, where: str) -> dict:
    if table not in BUSINESS_TABLES:
        raise EvalFixtureError(where + ": only business tables can be patched")
    if not isinstance(key, str) or not key.strip():
        raise EvalFixtureError(where + ": keys must be non-empty strings")
    pk = PRIMARY_KEYS[table]
    columns = frozenset(STAGE6_COLUMNS[table]) - {pk}
    if not isinstance(patch, Mapping) or patch.get("op") not in ("insert", "update", "delete"):
        raise EvalFixtureError(where + " must be an insert, update, or delete patch")
    op = patch["op"]
    wanted = {"insert": {"op", "row"}, "update": {"op", "set"}, "delete": {"op"}}[op]
    if set(patch) != wanted:
        raise EvalFixtureError(where + " " + op + " patch has the wrong keys")
    if op == "insert":
        if not isinstance(patch["row"], Mapping) or set(patch["row"]) != columns:
            raise EvalFixtureError(where + ".row must give exactly the non-key columns of " + table)
        for column, value in patch["row"].items():
            _check_value(where + ".row." + column, value)
    elif op == "update":
        values = patch["set"]
        if not isinstance(values, Mapping) or not values or not set(values) <= columns:
            raise EvalFixtureError(where + ".set must name non-key columns of " + table)
        for column, value in values.items():
            _check_value(where + ".set." + column, value)
    return dict(patch)


def _apply(connection: sqlite3.Connection, table: str, key: str, patch: Mapping, where: str) -> None:
    pk = PRIMARY_KEYS[table]
    columns = STAGE6_COLUMNS[table]  # identifiers never come from the case
    if patch["op"] == "insert":
        row = patch["row"]
        connection.execute("INSERT INTO " + table + " (" + ", ".join(columns) + ") VALUES ("
                           + ", ".join("?" for _ in columns) + ")",
                           [key if column == pk else row[column] for column in columns])
        return
    if patch["op"] == "update":
        names = [column for column in columns if column in patch["set"]]
        cursor = connection.execute(
            "UPDATE " + table + " SET " + ", ".join(column + " = ?" for column in names)
            + " WHERE " + pk + " = ?", [patch["set"][column] for column in names] + [key])
    else:
        cursor = connection.execute("DELETE FROM " + table + " WHERE " + pk + " = ?", (key,))
    if cursor.rowcount != 1:
        raise EvalFixtureError(where + " matched no existing row")


def _in_transaction(connection: sqlite3.Connection, work) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        work()
    except sqlite3.Error as exc:
        connection.execute("ROLLBACK")
        raise EvalFixtureError("patch rejected by the database (" + type(exc).__name__ + ": "
                               + str(exc) + ")") from exc
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def harness_connection(path: str | Path) -> sqlite3.Connection:
    """The trusted harness's own writer (fixture and mutate only), never the gateway's."""
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def apply_initial_state(connection: sqlite3.Connection, initial_state: Mapping) -> None:
    """Inserts, then updates, then deletes, in the fixed table order; one transaction."""
    patches = {table: [(key, _checked(table, key, initial_state.get(table, {})[key],
                                      "initial_state." + table + "[" + key + "]"))
                       for key in sorted(initial_state.get(table, {}))]
               for table in BUSINESS_TABLES}

    def work() -> None:
        for op, order in (("insert", INSERT_ORDER), ("update", INSERT_ORDER), ("delete", DELETE_ORDER)):
            for table in order:
                for key, patch in patches[table]:
                    if patch["op"] == op:
                        _apply(connection, table, key, patch, "initial_state." + table + "[" + key + "]")

    _in_transaction(connection, work)


def apply_mutate(connection: sqlite3.Connection, event: Mapping) -> None:
    """One trusted mutate event in BEGIN IMMEDIATE; an update must raise the version."""
    table, key = event["table"], event["key"]
    where = "mutate " + str(table) + "[" + str(key) + "]"
    patch = _checked(table, key, event["patch"], where)

    def work() -> None:
        if patch["op"] == "update":
            values = patch["set"]
            if "version" not in values or "updated_at" not in values:
                raise EvalFixtureError(where + " must write version and updated_at")
            row = connection.execute("SELECT version FROM " + table + " WHERE "
                                     + PRIMARY_KEYS[table] + " = ?", (key,)).fetchone()
            if row is not None and values["version"] <= row[0]:
                raise EvalFixtureError(where + " must increase version")
        _apply(connection, table, key, patch, where)

    _in_transaction(connection, work)


# --------------------------------------------------------------------------
# Reading state
# --------------------------------------------------------------------------


def _rows(connection: sqlite3.Connection, table: str) -> list[dict]:
    declared = tuple(row[1] for row in connection.execute("PRAGMA table_info(" + table + ")"))
    if declared != STAGE6_COLUMNS[table]:
        raise EvalFixtureError(table + " columns differ from the Stage 6 schema")
    cursor = connection.cursor()
    try:
        cursor.row_factory = None
        rows = cursor.execute("SELECT " + ", ".join(declared) + " FROM " + table).fetchall()
    finally:
        cursor.close()
    return [dict(zip(declared, row)) for row in rows]


def read_state(connection: sqlite3.Connection) -> dict[str, dict[str, dict[str, object]]]:
    return {table: {row[PRIMARY_KEYS[table]]: row for row in _rows(connection, table)}
            for table in COMPARED_TABLES}


def read_audit(connection: sqlite3.Connection) -> tuple[dict, ...]:
    return tuple(sorted(_rows(connection, AUDIT_TABLE), key=lambda row: row["event_seq"]))


def state_sha256(state: TableState) -> str:
    payload = {table: [state[table][key] for key in sorted(state[table])] for table in COMPARED_TABLES}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_baseline(case: Mapping, directory: str | Path) -> dict[str, dict[str, dict[str, object]]]:
    """Baseline B in its own fresh database: fixture + patches + every mutate, in order."""
    path = Path(directory) / "baseline.db"
    create_stage6_database(path)
    connection = harness_connection(path)
    try:
        apply_initial_state(connection, case["initial_state"])
        for event in case["operator_script"]:
            if event["op"] == "mutate":
                apply_mutate(connection, event)
        return read_state(connection)
    finally:
        connection.close()


# --------------------------------------------------------------------------
# The comparator
# --------------------------------------------------------------------------


def _same(a: object, b: object) -> bool:
    """JSON equality: True is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def writable_projection(table: str, row: Mapping[str, object]) -> dict[str, object]:
    out = {}
    for column in AUTHOR_WRITABLE[table]:
        out[column] = json.loads(row["args_json"]) if column == "args" else row[column]
    return out


def _perfect_matching(candidates: list[list[int]], size: int) -> bool:
    """Every expected entry gets its own actual row (augmenting paths)."""
    owner = [-1] * size

    def assign(entry: int, seen: set[int]) -> bool:
        for actual in candidates[entry]:
            if actual in seen:
                continue
            seen.add(actual)
            if owner[actual] < 0 or assign(owner[actual], seen):
                owner[actual] = entry
                return True
        return False

    return all(assign(entry, set()) for entry in range(len(candidates)))


@dataclass(frozen=True, kw_only=True)
class FinalStateReport:
    ok: bool
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "problems": list(self.problems)}


def compare_final_state(expected: Mapping, baseline: TableState, final: TableState) -> FinalStateReport:
    problems: list[str] = []
    unknown = sorted(set(expected) - set(COMPARED_TABLES))
    if unknown:
        problems.append("expected_final_state names unknown table(s): " + ", ".join(unknown))
    for table in COMPARED_TABLES:
        before, after = baseline[table], final[table]
        entry = expected.get(table, {})
        updates = entry.get("update", {})
        deletes = set(entry.get("delete", []))
        inserts = entry.get("insert", [])
        for key in sorted(set(before) & set(after)):
            wanted = dict(before[key])
            wanted.update(updates.get(key, {}))
            if not _same(wanted, dict(after[key])):
                problems.append(table + "[" + key + "] differs from B" + (" + update" if key in updates else ""))
        for key in sorted(set(updates) - (set(before) & set(after))):
            problems.append(table + ".update[" + key + "] is not a row present in both B and F")
        removed = set(before) - set(after)
        if removed != deletes:
            problems.append(table + ": deleted rows " + str(sorted(removed)) + " but delete lists "
                            + str(sorted(deletes)))
        added = [after[key] for key in sorted(set(after) - set(before))]
        if not inserts and not added:
            continue
        if table not in AUTHOR_WRITABLE:
            problems.append(table + ": " + str(len(added)) + " unexpected new row(s)")
            continue
        projections = [writable_projection(table, row) for row in added]
        candidates = [[index for index, actual in enumerate(projections) if _same(item["row"], actual)]
                      for item in inserts]
        for position, matches in enumerate(candidates):
            if not matches:
                problems.append(table + ".insert[" + str(position) + "] matches no new row")
        if len(inserts) != len(added):
            problems.append(table + ": " + str(len(added)) + " new row(s), " + str(len(inserts))
                            + " expected insert(s)")
        elif all(candidates) and not _perfect_matching(candidates, len(added)):
            problems.append(table + ": inserts cannot be matched one-to-one")
    return FinalStateReport(ok=not problems, problems=tuple(problems))


# --------------------------------------------------------------------------
# Link invariants
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class LinkReport:
    l1: bool
    l2: bool
    l3: bool
    l4: bool
    l5: bool
    l6: bool
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.l1 and self.l2 and self.l3 and self.l4 and self.l5 and self.l6

    def to_dict(self) -> dict[str, object]:
        return {"L1": self.l1, "L2": self.l2, "L3": self.l3, "L4": self.l4, "L5": self.l5,
                "L6": self.l6, "ok": self.ok, "problems": list(self.problems)}


_RESOURCE_TABLES = {"after_sales_case": ("after_sales_cases", IdKind.AFTER_SALES_CASE),
                    "human_handoff_ticket": ("human_handoff_tickets", IdKind.HANDOFF_TICKET)}


def _new(baseline: TableState, final: TableState, table: str) -> dict[str, Mapping]:
    return {key: final[table][key] for key in final[table] if key not in baseline[table]}


def check_links(baseline: TableState, final: TableState, *, customer_id: str,
                id_namespace: str) -> LinkReport:
    problems = {name: [] for name in ("L1", "L2", "L3", "L4", "L5", "L6")}
    cases = _new(baseline, final, "after_sales_cases")
    tickets = _new(baseline, final, "human_handoff_tickets")
    pendings = _new(baseline, final, "pending_actions")
    receipts = _new(baseline, final, "action_receipts")
    for table in ACTION_TABLES:
        if set(baseline[table]) - set(final[table]):
            problems["L2"].append(table + " lost rows")
    business = {"after_sales_case": cases, "human_handoff_ticket": tickets}
    provider = DeterministicIdProvider(id_namespace)
    validator = ActionIntentValidator(build_action_registry(), ACTION_NAMES)

    # L1
    referenced: dict[tuple[str, str], int] = {}
    for receipt_id, receipt in receipts.items():
        resources = business.get(receipt["resource_type"], {})
        resource = resources.get(receipt["resource_id"])
        referenced[(receipt["resource_type"], receipt["resource_id"])] = (
            referenced.get((receipt["resource_type"], receipt["resource_id"]), 0) + 1)
        if resource is None:
            problems["L1"].append(receipt_id + " names no new " + receipt["resource_type"])
            continue
        try:
            args = json.loads(receipt["args_json"])
        except (TypeError, ValueError):
            args = {}
        if (resource["order_id"] != args.get("order_id")
                or resource["order_item_id"] != args.get("order_item_id")):
            problems["L1"].append(receipt_id + " names a row of another order item")
    for kind, rows in business.items():
        for key in rows:
            if referenced.get((kind, key), 0) != 1:
                problems["L1"].append(key + " is not named by exactly one receipt")
    # L2
    for receipt_id, receipt in receipts.items():
        pid = receipt["pending_action_id"]
        if pid is None:
            if receipt["guard_decision"] != "ALLOW":
                problems["L2"].append(receipt_id + " is an approval receipt without a pending action")
            continue
        pending = final["pending_actions"].get(pid)
        if pending is None or pending["status"] != "EXECUTED" or pending["receipt_id"] != receipt_id:
            problems["L2"].append(receipt_id + " does not close an EXECUTED pending action")
    for pid, pending in final["pending_actions"].items():
        if pending["status"] == "EXECUTED":
            receipt = final["action_receipts"].get(pending["receipt_id"])
            if receipt is None or receipt["pending_action_id"] != pid:
                problems["L2"].append(pid + " is EXECUTED without its receipt")
        elif pending["receipt_id"] is not None:
            problems["L2"].append(pid + " has a receipt but is " + pending["status"])
    # L3, L4, L5
    for table, rows in (("pending_actions", pendings), ("action_receipts", receipts)):
        for key, row in rows.items():
            try:
                args = json.loads(row["args_json"])
                action = validator.validate(row["action_name"], args)
                identity = RequestIdentity(persona_id=row["persona_id"], request_id=row["request_id"])
                expected_key = idempotency_key(identity, action)
            except (TypeError, ValueError, ActionValidationError):
                problems["L3"].append(key + " args do not satisfy the action contract")
                continue
            if (action.canonical_args_json != row["args_json"] or action.args_sha256 != row["args_sha256"]
                    or expected_key != row["idempotency_key"]):
                problems["L3"].append(key + " args_sha256 / idempotency_key do not recompute")
            if table == "pending_actions" and (row["target_order_id"] != action.target_order_id
                                               or row["target_order_item_id"] != action.target_order_item_id):
                problems["L3"].append(key + " targets disagree with args")
            try:
                if hashlib.sha256(row["snapshot_json"].encode("utf-8")).hexdigest() != row["snapshot_sha256"]:
                    raise SnapshotIntegrityError("digest")
                parse_snapshot_document(row["snapshot_json"], expected_sha256=row["snapshot_sha256"],
                                        expected_action=row["action_name"])
            except (SnapshotIntegrityError, AttributeError, TypeError, ValueError):
                problems["L4"].append(key + " snapshot fails s6-guard-snapshot/1")
            if table == "pending_actions":
                if key != provider.new_id(IdKind.PENDING_ACTION, row["idempotency_key"]):
                    problems["L5"].append(key + " is not the deterministic pending id of its key")
            else:
                if key != provider.new_id(IdKind.RECEIPT, row["idempotency_key"]):
                    problems["L5"].append(key + " is not the deterministic receipt id of its key")
                resource = _RESOURCE_TABLES.get(row["resource_type"])
                if resource is None or row["resource_id"] != provider.new_id(resource[1], row["idempotency_key"]):
                    problems["L5"].append(key + " resource id is not the deterministic id of its key")
    # L6
    for key, row in cases.items():
        if row["customer_id"] != customer_id:
            problems["L6"].append(key + " belongs to another customer")
    flat = tuple(name + ": " + text for name, items in problems.items() for text in items)
    return LinkReport(l1=not problems["L1"], l2=not problems["L2"], l3=not problems["L3"],
                      l4=not problems["L4"], l5=not problems["L5"], l6=not problems["L6"],
                      problems=flat)
