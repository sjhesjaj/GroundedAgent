"""PostgreSQL concurrency, with and without the gateway's row locks: counted, not asserted.

Three scenarios, each run ROUNDS times with WORKERS threads released together by a
barrier, on a fresh PostgreSQL schema per round:

  approve   one pending return; every worker approves it (resume_action, as the
            operator endpoint does): how many executions, receipts, and what each
            caller is told
  same_key  every worker submits the same action with the same request id, so the
            same idempotency key (escalate_to_human: it executes at once): how many
            tickets and receipts land, and what each caller is told
  same_sku  every worker exchanges its own order item into one target SKU whose
            stock is smaller than the number of requests: how many exchanges are
            accepted against that stock, and the final stock

Gateways are built as the product builds them: deduct_exchange_stock=True
(decision C-D1, docs/v2/exchange-stock-deduction.md), so an executed exchange
takes its units from stock. Before stage C there was no deduction at all.

Modes: "locked" is the code as merged. "unlocked" replaces the three row-lock
functions of aftersales.action_db_pg with no-ops inside this process only (the
same switch as the stage A mutation check); nothing on disk changes.

    python -X utf8 scripts/pg_concurrency_compare.py \
        --url postgresql://aftersales:aftersales-dev@127.0.0.1:55432/aftersales \
        --out docs/v2/pg-concurrency-results.json     (also writes the table as .md)
        [--scenarios same_sku]   a subset; the other scenarios are kept from --out

Not a test: it is not collected by unittest discover and asserts nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import secrets
import sys
import threading
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import aftersales.action_db_pg as action_db_pg  # noqa: E402
from aftersales.action_db import PgDatabase, connect_writer, create_stage6_database, drop_stage6_database  # noqa: E402
from aftersales.action_gateway import ActionGateway  # noqa: E402
from aftersales.action_policy import S6_RISK_POLICY  # noqa: E402
from aftersales.actions import ActionIntentValidator, build_action_registry  # noqa: E402
from aftersales.approval import ApprovalDecision  # noqa: E402
from aftersales.capabilities import CapabilityGate  # noqa: E402
from aftersales.clock import FixedClock  # noqa: E402
from aftersales.demo import DEMO_VIRTUAL_NOW  # noqa: E402
from aftersales.ids import DeterministicIdProvider, RequestIdentity  # noqa: E402
from aftersales.policy_catalog import PublishedPolicyCatalog  # noqa: E402

LOCK_FUNCTIONS = ("lock_anchor", "lock_pending", "lock_guard_reads")
OPERATOR = "op-demo-1"
RETURN_ARGS = {"order_id": "ORD-1001", "order_item_id": "OI-1001-2", "reason_code": "no_longer_wanted"}
HANDOFF_ARGS = {"order_id": "ORD-1001", "order_item_id": "OI-1001-1", "handoff_trigger": "quality_dispute"}
TARGET_SKU = "SKU-TSHIRT-L"
TARGET_STOCK = 5


@contextlib.contextmanager
def row_locks(enabled: bool):
    """enabled=False: the three lock functions become no-ops until the block ends."""
    if enabled:
        yield
        return
    saved = {name: getattr(action_db_pg, name) for name in LOCK_FUNCTIONS}
    try:
        for name in LOCK_FUNCTIONS:
            setattr(action_db_pg, name, lambda *args, **kwargs: None)
        yield
    finally:
        for name, function in saved.items():
            setattr(action_db_pg, name, function)


class Round:
    """One fresh schema and a gateway factory over it."""

    def __init__(self, url: str, label: str) -> None:
        self.target = PgDatabase(url, "pgcmp_" + label + "_" + secrets.token_hex(4))
        create_stage6_database(self.target)
        self.capabilities = CapabilityGate().narrow()
        self.validator = ActionIntentValidator(build_action_registry(), self.capabilities.actions)

    def gateway(self) -> ActionGateway:
        return ActionGateway(self.target, clock=FixedClock(DEMO_VIRTUAL_NOW),
                             id_provider=DeterministicIdProvider("pg-compare"),
                             catalog=PublishedPolicyCatalog(), capabilities=self.capabilities,
                             risk_policy=S6_RISK_POLICY, deduct_exchange_stock=True)

    def validate(self, name: str, args: dict):
        return self.validator.validate(name, args)

    def sql(self, statement: str, params: tuple = ()) -> list[tuple]:
        connection = connect_writer(self.target)
        try:
            return connection.execute(statement, params).fetchall()
        finally:
            connection.close()

    def close(self) -> None:
        drop_stage6_database(self.target)


def race(workers: int, call) -> list[str]:
    """Run call(i) on `workers` threads released together; one outcome label per worker."""
    barrier = threading.Barrier(workers)
    labels: list[str | None] = [None] * workers

    def run(index: int) -> None:
        try:
            barrier.wait()
            outcome = call(index)
            label = outcome.status.value
            if outcome.idempotent_replay:
                label += " (replay)"
            if outcome.decision_conflict:
                label += " (conflict)"
            if outcome.code:
                label += ": " + outcome.code
            labels[index] = label
        except BaseException as error:  # counted, never hidden
            labels[index] = "exception: " + type(error).__name__
    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [label or "no outcome" for label in labels]


def failed_audit_events(round_: Round) -> int:
    """Audit rows that record an attempt as not executed / FAILED."""
    return round_.sql("SELECT count(*) FROM action_audit_events"
                      " WHERE event_name = 'action.not_executed' AND decision = 'FAILED'")[0][0]


# --------------------------------------------------------------------------
# Scenarios: each returns the round's counts
# --------------------------------------------------------------------------


def scenario_approve(round_: Round, workers: int) -> dict:
    start = round_.gateway().start_action(
        RequestIdentity(persona_id="demo-a", request_id="req-approve"),
        round_.validate("create_return", RETURN_ARGS))
    pending_id = start.pending_action_id
    decision = ApprovalDecision(pending_action_id=pending_id, decision="APPROVE",
                                approver_ref=OPERATOR, decided_at=DEMO_VIRTUAL_NOW.isoformat())
    outcomes = race(workers, lambda i: round_.gateway().resume_action(decision))
    executed = round_.sql("SELECT count(*) FROM after_sales_cases WHERE order_item_id = ?"
                          " AND case_id LIKE 'AS6-%'", (RETURN_ARGS["order_item_id"],))[0][0]
    receipts = round_.sql("SELECT count(*) FROM action_receipts")[0][0]
    status = round_.sql("SELECT status FROM pending_actions WHERE pending_action_id = ?",
                        (pending_id,))[0][0]
    told_executed = sum(label.startswith("EXECUTED") for label in outcomes)
    return {
        "start": start.status.value, "executions": executed, "receipts": receipts,
        "pending_status": status, "callers_told_executed": told_executed,
        "callers_told_otherwise": workers - told_executed,
        "failed_audit_events": failed_audit_events(round_), "outcomes": dict(Counter(outcomes)),
        # Exactly one execution, and every caller told the truth about it.
        "error": not (executed == 1 and receipts == 1 and status == "EXECUTED"
                      and told_executed == workers),
    }


def scenario_same_key(round_: Round, workers: int) -> dict:
    identity = RequestIdentity(persona_id="demo-a", request_id="req-same-key")
    action = round_.validate("escalate_to_human", HANDOFF_ARGS)
    outcomes = race(workers, lambda i: round_.gateway().start_action(identity, action))
    tickets = round_.sql("SELECT count(*) FROM human_handoff_tickets")[0][0]
    receipts = round_.sql("SELECT count(*) FROM action_receipts")[0][0]
    told_executed = sum(label.startswith("EXECUTED") for label in outcomes)
    return {
        "tickets": tickets, "receipts": receipts, "callers_told_executed": told_executed,
        "callers_told_otherwise": workers - told_executed,
        "failed_audit_events": failed_audit_events(round_), "outcomes": dict(Counter(outcomes)),
        "error": not (tickets == 1 and receipts == 1 and told_executed == workers),
    }


def scenario_same_sku(round_: Round, workers: int) -> dict:
    # Fixture rows in this round's schema only: one delivered order per worker, each
    # with one SKU-TSHIRT-M item (quantity 1), copied from the seed's ORD-1001.
    for i in range(workers):
        order, item, tracking = "ORD-9%03d" % (i + 1), "OI-9%03d-1" % (i + 1), "SF9%03d" % (i + 1)
        round_.sql("INSERT INTO orders SELECT ?, customer_id, status, paid_at, total_amount,"
                   " updated_at, version FROM orders WHERE order_id = 'ORD-1001'", (order,))
        round_.sql("INSERT INTO order_items SELECT ?, ?, sku, product_name, category, 1, unit_price,"
                   " updated_at, version FROM order_items WHERE order_item_id = 'OI-1001-1'",
                   (item, order))
        round_.sql("INSERT INTO logistics SELECT ?, ?, carrier, status, shipped_at, delivered_at,"
                   " last_event_at, updated_at, version FROM logistics WHERE tracking_no = 'SF1001'",
                   (tracking, order))
    round_.sql("UPDATE inventory SET available_qty = ?, version = version + 1 WHERE sku = ?",
               (TARGET_STOCK, TARGET_SKU))
    actions = [round_.validate("create_exchange", {
        "order_id": "ORD-9%03d" % (i + 1), "order_item_id": "OI-9%03d-1" % (i + 1),
        "target_sku": TARGET_SKU, "reason_code": "size_or_spec_mismatch"}) for i in range(workers)]
    outcomes = race(workers, lambda i: round_.gateway().start_action(
        RequestIdentity(persona_id="demo-a", request_id="req-sku-%d" % i), actions[i]))
    accepted = round_.sql("SELECT count(*) FROM after_sales_cases WHERE type = 'exchange'"
                          " AND order_item_id LIKE 'OI-9%'")[0][0]
    final_stock = round_.sql("SELECT available_qty FROM inventory WHERE sku = ?", (TARGET_SKU,))[0][0]
    return {
        "stock_before": TARGET_STOCK, "exchanges_accepted": accepted, "final_stock": final_stock,
        "units_over_stock": max(0, accepted - TARGET_STOCK),
        "callers_failed": sum(label.startswith("FAILED") for label in outcomes),
        # Units still in stock that a refused request was waiting for.
        "unsold_while_requested": min(workers - accepted, final_stock),
        "outcomes": dict(Counter(outcomes)),
        # Oversold (more units accepted than there were), stock that does not add up
        # (each accepted exchange takes one unit), or a request refused while its unit
        # was still there.
        "error": (accepted > TARGET_STOCK or final_stock != TARGET_STOCK - accepted
                  or min(workers - accepted, final_stock) > 0),
    }


SCENARIOS = {
    "approve": scenario_approve,
    "same_key": scenario_same_key,
    "same_sku": scenario_same_sku,
}


def summarise(name: str, rounds: list[dict]) -> dict:
    summary = {"rounds": len(rounds), "error_rounds": sum(r["error"] for r in rounds)}
    if name == "approve":
        summary.update(max_executions=max(r["executions"] for r in rounds),
                       max_receipts=max(r["receipts"] for r in rounds),
                       max_callers_told_otherwise=max(r["callers_told_otherwise"] for r in rounds),
                       max_failed_audit_events=max(r["failed_audit_events"] for r in rounds),
                       rounds_with_exactly_one_execution=sum(r["executions"] == 1 for r in rounds))
    elif name == "same_key":
        summary.update(max_tickets=max(r["tickets"] for r in rounds),
                       max_receipts=max(r["receipts"] for r in rounds),
                       max_callers_told_otherwise=max(r["callers_told_otherwise"] for r in rounds),
                       max_failed_audit_events=max(r["failed_audit_events"] for r in rounds),
                       rounds_with_exactly_one_row=sum(r["tickets"] == 1 and r["receipts"] == 1
                                                       for r in rounds))
    else:
        summary.update(min_exchanges_accepted=min(r["exchanges_accepted"] for r in rounds),
                       max_exchanges_accepted=max(r["exchanges_accepted"] for r in rounds),
                       max_units_over_stock=max(r["units_over_stock"] for r in rounds),
                       final_stock_values=sorted({r["final_stock"] for r in rounds}),
                       max_unsold_while_requested=max(r["unsold_while_requested"] for r in rounds),
                       callers_failed=sum(r["callers_failed"] for r in rounds))
    outcomes: Counter = Counter()
    for r in rounds:
        outcomes.update(r["outcomes"])
    summary["outcomes_all_rounds"] = dict(outcomes)
    return summary


SCENARIO_TITLES = {
    "approve": "同一个待审批退货，{w} 个线程同时批准",
    "same_key": "同一个幂等键的动作，{w} 个线程同时提交",
    "same_sku": "同一 SKU（库存 {s}）被 {w} 个换货同时申请",
}


def describe(name: str, summary: dict, workers: int) -> str:
    rounds = summary["rounds"]
    if name == "approve":
        return ("执行最多 {e} 次、receipt 最多 {r} 条；{one}/{n} 轮恰好执行 1 次；"
                "最多 {t}/{w} 个调用方被告知未执行（实际已执行），审计最多 {a} 条 FAILED").format(
            e=summary["max_executions"], r=summary["max_receipts"],
            one=summary["rounds_with_exactly_one_execution"], n=rounds,
            t=summary["max_callers_told_otherwise"], w=workers, a=summary["max_failed_audit_events"])
    if name == "same_key":
        return ("工单最多 {k} 条、receipt 最多 {r} 条；{one}/{n} 轮恰好落库 1 条；"
                "最多 {t}/{w} 个调用方被告知未执行（实际已执行），审计最多 {a} 条 FAILED").format(
            k=summary["max_tickets"], r=summary["max_receipts"],
            one=summary["rounds_with_exactly_one_row"], n=rounds,
            t=summary["max_callers_told_otherwise"], w=workers, a=summary["max_failed_audit_events"])
    accepted = summary["min_exchanges_accepted"], summary["max_exchanges_accepted"]
    return ("每轮接受 {a} 个换货，超卖最多 {o} 件；最终库存 {f}；"
            "有库存却拒绝最多 {u} 件；共 {k} 个调用方收到 FAILED").format(
        a=str(accepted[0]) if accepted[0] == accepted[1] else "%d–%d" % accepted,
        o=summary["max_units_over_stock"], u=summary["max_unsold_while_requested"],
        k=summary["callers_failed"], f="/".join(str(v) for v in summary["final_stock_values"]))


def table(results: dict) -> str:
    workers = results["workers"]
    lines = ["| 场景 | 去锁：出错轮数 | 去锁：结果 | 加锁：出错轮数 | 加锁：结果 |",
             "|---|---|---|---|---|"]
    for name, modes in results["scenarios"].items():
        off, on = modes["unlocked"]["summary"], modes["locked"]["summary"]
        lines.append("| {t} | {eo}/{n} | {do} | {el}/{n} | {dl} |".format(
            t=SCENARIO_TITLES[name].format(w=workers, s=TARGET_STOCK),
            eo=off["error_rounds"], el=on["error_rounds"], n=off["rounds"],
            do=describe(name, off, workers), dl=describe(name, on, workers)))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="postgresql://... (a disposable database)")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scenarios", default=",".join(SCENARIOS),
                        help="comma-separated subset; the other scenarios are kept from --out")
    arguments = parser.parse_args()
    chosen = [name.strip() for name in arguments.scenarios.split(",") if name.strip()]
    if not chosen or set(chosen) - set(SCENARIOS):
        parser.error("--scenarios must name some of: " + ", ".join(SCENARIOS))

    results: dict = {
        "rounds": arguments.rounds, "workers": arguments.workers,
        "python": platform.python_version(), "scenarios": {},
    }
    kept = [name for name in SCENARIOS if name not in chosen]
    if kept:
        # A partial rerun completes the earlier file: its other scenarios stay as they were,
        # and what this run replaces is kept as a summary under "superseded".
        if not arguments.out.is_file():
            parser.error("a --scenarios subset needs an existing --out to complete")
        previous = json.loads(arguments.out.read_text(encoding="utf-8"))
        if (previous["rounds"], previous["workers"]) != (arguments.rounds, arguments.workers):
            parser.error("rounds and workers must match the existing --out")
        results["scenarios"] = {name: previous["scenarios"][name] for name in kept}
        results["kept_from_previous_run"] = kept
        results["superseded"] = {**previous.get("superseded", {}), **{
            name: {mode: previous["scenarios"][name][mode]["summary"] for mode in ("unlocked", "locked")}
            for name in chosen}}
    with contextlib.closing(connect_writer(PgDatabase(arguments.url, "public"))) as probe:
        results["server"] = probe.execute("SHOW server_version").fetchone()[0]
    started = time.monotonic()
    for name, scenario in SCENARIOS.items():
        if name not in chosen:
            continue
        results["scenarios"][name] = {}
        for mode, locked in (("unlocked", False), ("locked", True)):
            rounds = []
            for index in range(arguments.rounds):
                round_ = Round(arguments.url, name[:4] + "_" + mode[:2] + str(index))
                try:
                    with row_locks(locked):
                        rounds.append(scenario(round_, arguments.workers))
                finally:
                    round_.close()
            results["scenarios"][name][mode] = {"summary": summarise(name, rounds), "per_round": rounds}
            print(name, mode, json.dumps(results["scenarios"][name][mode]["summary"], ensure_ascii=False),
                  flush=True)
    results["seconds"] = round(time.monotonic() - started, 1)
    results["scenarios"] = {name: results["scenarios"][name] for name in SCENARIOS}
    # The switch never outlives the run.
    assert all(getattr(action_db_pg, name).__module__ == action_db_pg.__name__ for name in LOCK_FUNCTIONS)
    results["table_markdown"] = table(results)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    arguments.out.with_suffix(".md").write_text(results["table_markdown"], encoding="utf-8")
    print(results["table_markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
