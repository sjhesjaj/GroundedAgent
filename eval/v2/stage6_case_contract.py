"""Check Stage 6 eval cases (v2-stage6-case/1, docs/v2/stage6-design.md §19.2).

Stdlib only, like case_contract.py beside it, so it runs unchanged from a
Stage 6 author bundle (which keeps repo-relative paths). It loads the frozen
case_contract.py by path and uses its JSON Schema interpreter (`schema_errors`,
`lint_schema`) on eval/v2/spec/stage6-case.schema.json, then adds the Stage 6
cross-field rules a schema cannot state:

     1  final == "action" exactly when expected_action is not null
     2  the expected action is a required capability, the other two are
        forbidden; with no expected action all three are forbidden
     3  args and args_any_of are disjoint and together cover exactly the
        action's parameters, with valid values
     4  approval_required == (initial_guard.decision == REQUIRE_APPROVAL); a
        positive reason code only with its decision (and the risk policy's
        disposition for the action), a DENY code only with DENY and only one
        the action's precondition matrix can produce
     5  final_status agrees with initial_guard (ALLOW -> EXECUTED, or FAILED
        with action_faults; DENY -> DENIED; REQUIRE_APPROVAL -> decided by the
        script, WAITING_APPROVAL without a decision event; null -> FAILED with
        action_faults)
     6  final_code is required for DENIED / STALE / FAILED / REJECTED, from the
        matching closed vocabulary, and null otherwise
     7  events align one-to-one with operator_script; null exactly at mutate /
        advance_clock / restart
     8  approve / reject / record_decision / execute_approved only on the
        REQUIRE_APPROVAL path; with no expected action, only mutate /
        advance_clock / restart
     9  a mutate update writes version and updated_at, and the version grows;
        advance_clock strictly increases and is later than virtual_now
    10  no generated-id prefix (PA- / AS6- / HT- / RC-) as a fixture key; at
        most one active after-sales case per item and one open ticket per
        (item, trigger)
    11  the Stage 5 rules (clarify consistency, fault match keys, required /
        forbidden disjoint) via the frozen case_contract

plus the action_faults shape rules and expected_final_state consistency
(trusted persona and customer, pending CHECK semantics, known keys).

Versions of seed rows come from the frozen seed SQL files, read with a small
literal-only INSERT reader. Nothing here reads the system clock.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType

EVAL_DIR = Path(__file__).resolve().parent
SPEC_DIR = EVAL_DIR / "spec"
REPO_ROOT = EVAL_DIR.parent.parent
CASE_CONTRACT_PATH = EVAL_DIR / "case_contract.py"
STAGE6_SCHEMA_PATH = SPEC_DIR / "stage6-case.schema.json"
STAGE6_ACTIONS_PATH = SPEC_DIR / "stage6-actions.json"
STAGE6_SCENARIOS_PATH = SPEC_DIR / "stage6-scenarios.json"
STAGE6_FINAL_OUTCOMES_PATH = SPEC_DIR / "stage6-final-outcomes.json"
STAGE6_HOLDOUT_PLAN_PATH = SPEC_DIR / "stage6-holdout-plan.json"
SEED_PATHS = (REPO_ROOT / "system_fixtures" / "aftersales_demo_seed.sql",
              REPO_ROOT / "system_fixtures" / "aftersales_stage6_seed.sql")

CASE_SCHEMA_ID = "v2-stage6-case/1"

PRIMARY_KEYS = {
    "orders": "order_id", "order_items": "order_item_id", "logistics": "tracking_no",
    "inventory": "sku", "after_sales_cases": "case_id", "sku_variants": "sku",
    "human_handoff_tickets": "ticket_id",
}
BUSINESS_TABLES = tuple(PRIMARY_KEYS)
NULL_EVENT_OPS = frozenset({"mutate", "advance_clock", "restart"})
APPROVAL_OPS = frozenset({"approve", "reject", "record_decision", "execute_approved"})
DECISION_OPS = frozenset({"approve", "reject", "record_decision"})
REPLAY_OPS = frozenset({"replay_submission", "rerun_request", "new_request"})
RERUN_OPS = frozenset({"rerun_request", "new_request"})
ACTIVE_CASE_STATUSES = frozenset({"待处理", "处理中"})
OPEN_TICKET_STATUSES = frozenset({"待处理", "处理中"})
GENERATED_ID = re.compile(r"^(PA|AS6|HT|RC)-")


# --------------------------------------------------------------------------
# Frozen inputs
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def base_contract() -> ModuleType:
    """The frozen Stage 4/5 case_contract.py, loaded by path (never modified)."""
    spec = importlib.util.spec_from_file_location("v2_case_contract_for_stage6", CASE_CONTRACT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def stage6_schema() -> dict:
    schema = _load(STAGE6_SCHEMA_PATH)
    base_contract().lint_schema(schema)
    return schema


@lru_cache(maxsize=1)
def actions_spec() -> dict:
    return _load(STAGE6_ACTIONS_PATH)


@lru_cache(maxsize=1)
def scenario_ids() -> tuple[str, ...]:
    return tuple(item["id"] for item in _load(STAGE6_SCENARIOS_PATH)["scenarios"])


@lru_cache(maxsize=1)
def holdout_plan() -> dict:
    return _load(STAGE6_HOLDOUT_PLAN_PATH)


def action_parameters(name: str) -> dict[str, list[str] | None]:
    """Parameter name -> enum (None for a free string), in declaration order."""
    for action in actions_spec()["actions"]:
        if action["name"] == name:
            return {item["name"]: item.get("enum") for item in action["parameters"]}
    raise KeyError(name)


def action_names() -> tuple[str, ...]:
    return tuple(action["name"] for action in actions_spec()["actions"])


def persona_customer_ids() -> dict[str, str]:
    return base_contract().persona_customer_ids()


def vocabulary_errors() -> list[str]:
    """The Stage 6 spec files must agree with each other and with the frozen Stage 4/5 ones."""
    out: list[str] = []
    schema = stage6_schema()
    base = base_contract().load_json(base_contract().CASE_SCHEMA_PATH)
    names = list(action_names())
    if schema["properties"]["scenario"]["enum"] != list(scenario_ids()):
        out.append("schema scenario enum disagrees with stage6-scenarios.json")
    archetypes = [item["id"] for item in _load(SPEC_DIR / "archetypes.json")["archetypes"]]
    if schema["properties"]["archetype"]["enum"] != archetypes:
        out.append("schema archetype enum disagrees with archetypes.json")
    if schema["$defs"]["capability"]["enum"] != base["$defs"]["capability"]["enum"] + names:
        out.append("capability enum must be the Stage 5 capabilities plus the three actions")
    finals = set(_load(STAGE6_FINAL_OUTCOMES_PATH)["definitions"])
    if set(schema["properties"]["expected_answerability"]["properties"]["final"]["enum"]) != finals:
        out.append("final enum disagrees with stage6-final-outcomes.json")
    for name, definition in base["$defs"].items():
        if name in schema["$defs"] and name != "capability" and schema["$defs"][name] != definition:
            out.append("$defs." + name + " drifted from case.schema.json")
    for scenario in _load(STAGE6_SCENARIOS_PATH)["scenarios"]:
        if not set(scenario["archetypes"]) <= set(archetypes):
            out.append("scenario " + scenario["id"] + " names an unknown archetype")
    return out


# --------------------------------------------------------------------------
# The frozen seed, read without a database
# --------------------------------------------------------------------------

_INSERT = re.compile(r"INSERT INTO (\w+) \(([^)]*)\) VALUES", re.S)


def _literals(text: str, start: int) -> tuple[list[list[object]], int]:
    """Parse `(v, ...), (v, ...);` of SQL literals: 'text' ('' escapes), NULL, integers."""
    rows: list[list[object]] = []
    index = start
    while True:
        while text[index] in " \t\r\n,":
            index += 1
        if text[index] == ";":
            return rows, index + 1
        if text[index] != "(":
            raise ValueError("unexpected seed syntax")
        index += 1
        row: list[object] = []
        while True:
            while text[index] in " \t\r\n":
                index += 1
            if text[index] == "'":
                index += 1
                chars = []
                while True:
                    if text[index] == "'":
                        if text[index + 1] == "'":
                            chars.append("'")
                            index += 2
                            continue
                        index += 1
                        break
                    chars.append(text[index])
                    index += 1
                row.append("".join(chars))
            else:
                match = re.compile(r"NULL|-?[0-9]+").match(text, index)
                if match is None:
                    raise ValueError("unexpected seed literal")
                row.append(None if match.group() == "NULL" else int(match.group()))
                index = match.end()
            while text[index] in " \t\r\n":
                index += 1
            if text[index] == ",":
                index += 1
                continue
            if text[index] == ")":
                index += 1
                break
            raise ValueError("unexpected seed syntax")
        rows.append(row)


@lru_cache(maxsize=1)
def seed_rows() -> dict[str, dict[str, dict[str, object]]]:
    """table -> primary key -> row, for the seven Stage 6 business tables."""
    tables: dict[str, dict[str, dict[str, object]]] = {name: {} for name in BUSINESS_TABLES}
    for path in SEED_PATHS:
        text = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                         if not line.lstrip().startswith("--"))
        position = 0
        while True:
            match = _INSERT.search(text, position)
            if match is None:
                break
            table = match.group(1)
            columns = [column.strip() for column in match.group(2).split(",")]
            rows, position = _literals(text, match.end())
            for values in rows:
                row = dict(zip(columns, values))
                tables[table][row[PRIMARY_KEYS[table]]] = row
    return tables


# --------------------------------------------------------------------------
# Schema and cross-field rules
# --------------------------------------------------------------------------


def schema_errors(instance: object) -> list[str]:
    return base_contract().schema_errors(instance, stage6_schema())


def _aware(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _status_code_errors(where: str, status: str, code: object, spec: dict) -> list[str]:
    vocabularies = {
        "DENIED": spec["deny_reason_codes"],
        "STALE": spec["stale_reason_codes"],
        "FAILED": spec["failure_codes"],
        "REJECTED": [spec["approval_rejected_code"]],
    }
    if status in vocabularies:
        if code not in vocabularies[status]:
            return [where + ": a " + status + " result needs a code from its closed vocabulary"]
        return []
    if code is not None:
        return [where + ": an " + status + " result has no code"]
    return []


def _apply(model: dict, table: str, key: str, patch: dict, where: str) -> list[str]:
    """Apply one fixture / mutate patch to the modelled business state."""
    rows = model[table]
    op = patch["op"]
    if op == "insert":
        if key in rows:
            return [where + ": insert of an existing " + table + " key"]
        rows[key] = dict(patch["row"], **{PRIMARY_KEYS[table]: key})
    elif op == "update":
        if key not in rows:
            return [where + ": update of a missing " + table + " key"]
        rows[key] = dict(rows[key], **patch["set"])
    else:
        if key not in rows:
            return [where + ": delete of a missing " + table + " key"]
        del rows[key]
    return []


def _state_errors(model: dict, where: str) -> list[str]:
    out: list[str] = []
    active: dict[str, int] = {}
    for row in model["after_sales_cases"].values():
        if row.get("status") in ACTIVE_CASE_STATUSES:
            active[row["order_item_id"]] = active.get(row["order_item_id"], 0) + 1
    out += [where + ": two active after-sales cases on " + item for item, n in sorted(active.items()) if n > 1]
    tickets: dict[tuple, int] = {}
    for row in model["human_handoff_tickets"].values():
        if row.get("status") in OPEN_TICKET_STATUSES:
            pair = (row["order_item_id"], row["handoff_trigger"])
            tickets[pair] = tickets.get(pair, 0) + 1
    out += [where + ": two open tickets on " + pair[0] for pair, n in sorted(tickets.items()) if n > 1]
    for table, parent, column in (("order_items", "orders", "order_id"),
                                  ("logistics", "orders", "order_id"),
                                  ("after_sales_cases", "order_items", "order_item_id"),
                                  ("human_handoff_tickets", "order_items", "order_item_id")):
        for key, row in model[table].items():
            if row.get(column) not in model[parent]:
                out.append(where + ": " + table + "[" + key + "]." + column + " refers to a missing row")
    return out


def _check_pending_row(where: str, row: dict) -> list[str]:
    """The pending_actions CHECK semantics, so an impossible expected row is a case error."""
    out: list[str] = []
    status, decision = row["status"], row["approval_decision"]
    if (decision is None) != (row["approver_ref"] is None) or (decision is None) != (row["decided_at"] is None):
        out.append(where + ": approval fields must be all null or all set")
    if status == "PENDING_APPROVAL" and decision is not None:
        out.append(where + ": PENDING_APPROVAL carries no approval decision")
    if status in ("APPROVED", "EXECUTED", "STALE", "DENIED", "FAILED") and decision != "APPROVE":
        out.append(where + ": " + status + " requires an APPROVE decision")
    if status == "REJECTED" and decision != "REJECT":
        out.append(where + ": REJECTED requires a REJECT decision")
    if (status in ("PENDING_APPROVAL", "APPROVED", "EXECUTED")) != (row["outcome_code"] is None):
        out.append(where + ": outcome_code must be set exactly for REJECTED / STALE / DENIED / FAILED")
    return out


def semantic_errors(case: dict) -> list[str]:
    """Cross-field rules. Call only on a schema-valid case."""
    spec = actions_spec()
    out: list[str] = list(base_contract().semantic_errors(case))  # rule 11
    names = action_names()
    persona = case["initial_state"]["trusted_context"]["persona_id"]
    customer = persona_customer_ids()[persona]
    expected = case["expected_action"]
    final = case["expected_answerability"]["final"]
    caps = case["expected_capabilities"]
    script = case["operator_script"]
    ops = [event["op"] for event in script]
    action_faults = case["initial_state"]["action_faults"]

    # 1
    if (final == "action") != (expected is not None):
        out.append("$.expected_answerability.final: action exactly when expected_action is not null")
    # 2
    if expected is not None:
        name = expected["action_name"]
        if name not in caps["required"]:
            out.append("$.expected_capabilities.required must contain " + name)
        for other in names:
            if other != name and other not in caps["forbidden"]:
                out.append("$.expected_capabilities.forbidden must contain " + other)
        evidence = case["expected_evidence"]
        if evidence["all_of"] or evidence["any_of"]:
            out.append("$.expected_evidence: all_of and any_of must be empty when an action is expected")
    else:
        for other in names:
            if other not in caps["forbidden"]:
                out.append("$.expected_capabilities.forbidden must contain " + other)
        bad = sorted(set(ops) - NULL_EVENT_OPS)
        if bad:
            out.append("$.operator_script: without an expected action only mutate / advance_clock / "
                       "restart are allowed, not " + ", ".join(bad))
        for table in ("pending_actions", "action_receipts"):
            if table in case["expected_final_state"]:
                out.append("$.expected_final_state." + table + ": no action is expected")

    # action_faults shape
    seen_faults: set[tuple] = set()
    for index, fault in enumerate(action_faults):
        where = "$.initial_state.action_faults[" + str(index) + "]"
        if "read" in fault and fault["point"] != "guard_read":
            out.append(where + ": read is only for point guard_read")
        if fault["point"] not in spec["action_fault_modes"][fault["mode"]]:
            out.append(where + ": mode " + fault["mode"] + " is not allowed at " + fault["point"])
        identity = (fault["point"], fault.get("read"), fault["on_call"])
        if identity in seen_faults:
            out.append(where + ": the same point / read / on_call is declared twice")
        seen_faults.add(identity)

    if expected is not None:
        out += _expected_action_errors(case, expected, ops, action_faults, spec)

    # 9, 10: the modelled business state, from the seed through every patch
    model = {table: {key: dict(row) for key, row in rows.items()}
             for table, rows in seed_rows().items()}
    initial = case["initial_state"]
    for table in BUSINESS_TABLES:
        for key in sorted(initial.get(table, {})):
            where = "$.initial_state." + table + "[" + key + "]"
            if GENERATED_ID.match(key):
                out.append(where + ": generated-id prefixes are not fixture keys")
            out += _apply(model, table, key, initial[table][key], where)
    out += _state_errors(model, "$.initial_state")
    now = _aware(case["virtual_now"])
    for index, event in enumerate(script):
        where = "$.operator_script[" + str(index) + "]"
        if event["op"] == "advance_clock":
            moment = _aware(event["virtual_now"])
            if moment <= now:
                out.append(where + ": advance_clock must move business time strictly forward")
            now = max(now, moment)
        elif event["op"] == "mutate":
            table, key, patch = event["table"], event["key"], event["patch"]
            if GENERATED_ID.match(key):
                out.append(where + ": generated-id prefixes are not mutate keys")
            if patch["op"] == "update":
                values = patch["set"]
                if "version" not in values or "updated_at" not in values:
                    out.append(where + ": a mutate update writes version and updated_at")
                elif key in model[table] and values["version"] <= model[table][key].get("version", 0):
                    out.append(where + ": a mutate update must increase version")
            out += _apply(model, table, key, patch, where)
            out += _state_errors(model, where)

    # expected_final_state against the modelled baseline B
    final_state = case["expected_final_state"]
    for table, entry in final_state.items():
        where = "$.expected_final_state." + table
        for key in entry.get("update", {}):
            if key not in model.get(table, {}):
                out.append(where + ".update[" + key + "]: not a row of baseline B")
        for key in entry.get("delete", []):
            if key not in model.get(table, {}):
                out.append(where + ".delete: " + key + " is not a row of baseline B")
        for index, item in enumerate(entry.get("insert", [])):
            row = item["row"]
            here = where + ".insert[" + str(index) + "].row"
            if table == "after_sales_cases" and row["customer_id"] != customer:
                out.append(here + ".customer_id must be the trusted persona's customer")
            if table in ("pending_actions", "action_receipts"):
                if row["persona_id"] != persona:
                    out.append(here + ".persona_id must be the trusted persona")
                if expected is not None and row["action_name"] != expected["action_name"]:
                    out.append(here + ".action_name must be the expected action")
                if set(row["args"]) != set(action_parameters(row["action_name"])):
                    out.append(here + ".args must give exactly the action's parameters")
            if table == "pending_actions":
                out += _check_pending_row(here, row)
                if (row["target_order_id"] != row["args"].get("order_id")
                        or row["target_order_item_id"] != row["args"].get("order_item_id")):
                    out.append(here + ": targets must equal args.order_id / args.order_item_id")
            if table == "action_receipts":
                resource = "human_handoff_ticket" if row["action_name"] == "escalate_to_human" else "after_sales_case"
                if row["resource_type"] != resource:
                    out.append(here + ".resource_type does not belong to the action")
                approval = row["guard_decision"] == "REQUIRE_APPROVAL"
                if row["guard_reason_code"] != spec["positive_reason_codes"][row["guard_decision"]]:
                    out.append(here + ".guard_reason_code does not belong to guard_decision")
                if approval != (spec["risk_policy"]["dispositions"][row["action_name"]] == "REQUIRE_APPROVAL"):
                    out.append(here + ".guard_decision disagrees with the risk policy")
    return out


def _expected_action_errors(case: dict, expected: dict, ops: list[str], action_faults: list,
                            spec: dict) -> list[str]:
    out: list[str] = []
    name = expected["action_name"]
    parameters = action_parameters(name)
    args, any_of = expected["args"], expected["args_any_of"]
    # 3
    overlap = sorted(set(args) & set(any_of))
    if overlap:
        out.append("$.expected_action: args and args_any_of overlap: " + ", ".join(overlap))
    if set(args) | set(any_of) != set(parameters):
        out.append("$.expected_action: args and args_any_of must cover exactly the parameters of " + name)
    for key, value in list(args.items()) + [(k, v) for k, values in any_of.items() for v in values]:
        enum = parameters.get(key)
        if enum is not None and value not in enum:
            out.append("$.expected_action: " + key + " value is outside its closed vocabulary")
        if not value.strip():
            out.append("$.expected_action: " + key + " value is blank")
    # 4
    guard = expected["initial_guard"]
    decision = None if guard is None else guard["decision"]
    if expected["approval_required"] != (decision == "REQUIRE_APPROVAL"):
        out.append("$.expected_action.approval_required must equal initial_guard.decision == REQUIRE_APPROVAL")
    if guard is not None:
        code = guard["reason_code"]
        if decision == "DENY":
            if code not in spec["deny_reason_codes_by_action"][name]:
                out.append("$.expected_action.initial_guard: " + code + " is not a DENY outcome of " + name)
        else:
            if code != spec["positive_reason_codes"][decision]:
                out.append("$.expected_action.initial_guard: reason_code does not belong to " + decision)
            if spec["risk_policy"]["dispositions"][name] != decision:
                out.append("$.expected_action.initial_guard: " + decision + " is not the risk policy's "
                           "decision for " + name)
    # 5
    status = expected["final_status"]
    decisions = [op for op in ops if op in DECISION_OPS]
    replays = [op for op in ops if op in REPLAY_OPS]
    if guard is None:
        if not action_faults:
            out.append("$.expected_action.initial_guard: null needs a declared action fault")
        if not replays and status != "FAILED":
            out.append("$.expected_action.final_status: no Guard decision means FAILED")
    elif decision == "ALLOW":
        if status != "EXECUTED" and not (status == "FAILED" and action_faults):
            out.append("$.expected_action.final_status: ALLOW ends EXECUTED (or FAILED with action_faults)")
    elif decision == "DENY":
        if status != "DENIED":
            out.append("$.expected_action.final_status: DENY ends DENIED")
        elif expected["final_code"] != guard["reason_code"]:
            out.append("$.expected_action.final_code must be the DENY reason")
    elif not decisions and status != "WAITING_APPROVAL":
        out.append("$.expected_action.final_status: without a decision event the action stays WAITING_APPROVAL")
    elif decisions:
        first = next(event for event in case["operator_script"] if event["op"] in DECISION_OPS)
        rejected = first["op"] == "reject" or (first["op"] == "record_decision" and first["decision"] == "REJECT")
        if rejected and status != "REJECTED":
            out.append("$.expected_action.final_status: the first decision rejects, so REJECTED")
        if not rejected and status == "REJECTED":
            out.append("$.expected_action.final_status: the first decision approves, so not REJECTED")
    # 6
    out += _status_code_errors("$.expected_action.final_code", status, expected["final_code"], spec)
    # 7, 8
    events = expected["events"]
    if len(events) != len(ops):
        out.append("$.expected_action.events must have one entry per operator_script event")
    for index, (op, event) in enumerate(zip(ops, events)):
        where = "$.expected_action.events[" + str(index) + "]"
        if op in NULL_EVENT_OPS:
            if event is not None:
                out.append(where + ": " + op + " has no expected outcome (null)")
            continue
        if event is None:
            out.append(where + ": " + op + " needs an expected outcome")
            continue
        if event["status"] is None:
            if op not in RERUN_OPS:
                out.append(where + ": only a rerun_request / new_request may submit no action")
            continue
        out += _status_code_errors(where + ".code", event["status"], event["code"], spec)
    if decision != "REQUIRE_APPROVAL":
        bad = sorted({op for op in ops if op in APPROVAL_OPS})
        if bad:
            out.append("$.operator_script: " + ", ".join(bad) + " only on the REQUIRE_APPROVAL path")
    return out


def case_errors(case: object) -> list[str]:
    errors = schema_errors(case)
    return errors if errors else semantic_errors(case)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Dataset distribution (stage6-holdout-plan.json)
# --------------------------------------------------------------------------


def _coverage(case: dict, item: str) -> bool:
    expected = case["expected_action"] or {}
    status = expected.get("final_status")
    decision = (expected.get("initial_guard") or {}).get("decision")
    initial = case["initial_state"]
    return {
        "A21": case["archetype"] == "A21",
        "A22": case["archetype"] == "A22",
        "A23": case["archetype"] == "A23",
        "direct_injection": case["scenario"] == "direct_prompt_injection",
        "indirect_injection": case["scenario"] == "indirect_prompt_injection",
        "claimed_identity": case["scenario"] == "claimed_privileged_identity",
        "faults": bool(initial["faults"]) or bool(initial["action_faults"]),
        "waiting_approval": status == "WAITING_APPROVAL",
        "executed_auto": status == "EXECUTED" and decision == "ALLOW",
        "executed_approval": status == "EXECUTED" and decision == "REQUIRE_APPROVAL",
        "rejected": status == "REJECTED",
        "stale": status == "STALE",
        "denied": status == "DENIED",
        "failed": status == "FAILED",
    }[item]


def dataset_plan_errors(cases: list, split: str) -> list[str]:
    """Distribution check of one split. Each case must already pass case_errors."""
    plan = holdout_plan()
    if split not in plan["splits"]:
        return ["unknown split " + repr(split)]
    rules = plan["splits"][split]
    out: list[str] = []
    if len(cases) != rules["total_cases"]:
        out.append(split + ": " + str(len(cases)) + " cases, the plan requires " + str(rules["total_cases"]))
    for scenario in scenario_ids():
        count = sum(1 for case in cases if case["scenario"] == scenario)
        if count < rules["per_scenario_min"]:
            out.append(split + ": scenario " + scenario + " has " + str(count) + " case(s)")
    for item in plan["required_coverage"]:
        count = sum(1 for case in cases if _coverage(case, item["id"]))
        if count < item["min_cases"]:
            out.append(split + ": coverage " + item["id"] + " is not met")
    finals = {case["expected_answerability"]["final"] for case in cases}
    for value in plan["required_final_values"]:
        if value not in finals:
            out.append(split + ": no case has final " + value)
    personas = {case["initial_state"]["trusted_context"]["persona_id"] for case in cases}
    for persona in plan["required_personas"]:
        if persona not in personas:
            out.append(split + ": persona " + persona + " is not represented")
    if len({case["virtual_now"] for case in cases}) < plan["min_distinct_virtual_now"]:
        out.append(split + ": too few distinct virtual_now values")
    ids = [case["case_id"] for case in cases]
    if len(set(ids)) != len(ids):
        out.append(split + ": duplicate case_id")
    return out
