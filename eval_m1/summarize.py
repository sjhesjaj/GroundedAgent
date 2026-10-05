"""Summarize an M1-A2 result directory: no policy, no provider, no database.

    python -B -m eval_m1.summarize <out> [--markdown]

Definitions (docs/v2/m1-a2-grounding-eval.md, section 4):

    ungrounded_admitted     main-run actions that reached the gateway (the case-run has a
                            main_outcome) and whose grounding record says grounded == false.
                            Reruns (rerun_request / new_request) are counted apart, as
                            ungrounded_admitted_rerun.
    would_reject            shadow: grounding records with grounded == false, by code
    gate_rejections         enforce: the same count, by code (every one was replaced by Finish)
    scorer metrics          stage6_e2e_success, final_state_ok, capabilities_ok,
                            action_selection_ok, final_ok, as Stage6Score reports them
    hard invariants         the six Stage 6 hard invariants; each group must be 40/40
    completion_cost_cases   same round, final_state_ok true under shadow and false under enforce

Every rejection carries the attribution the wrapper's independent re-check
gave it (true_rejection / false_rejection / fail_closed). stop_required lists
every reason the results must not be written up as they are: a failed hard
invariant, a false rejection, a fail-closed rejection or wrapper diagnostic,
a gate decision the re-check disagrees with, an enforced run that admitted an
ungrounded action, an unexpected (non-provider) error, or malformed rows.
Provider failures are listed and leave their case unscored; they do not stop.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Iterable, Mapping, Sequence

from eval_v2.stage6_runner import RERUN_OPS
from eval_v2.stage6_scoring import HARD_INVARIANTS

from .grounding_policy import ATTRIBUTION_FAIL_CLOSED, ATTRIBUTION_FALSE, ATTRIBUTIONS, MODES

SCOPE = "DEV 上的诊断对比，不是新的泛化结论；holdout 未重跑"
SUMMARY_SCHEMA = "m1-a2-summary/1"
METRICS = ("stage6_e2e_success", "final_state_ok", "capabilities_ok", "action_selection_ok", "final_ok")
COST_METRICS = ("final_state_ok", "final_ok", "stage6_e2e_success", "capabilities_ok", "action_selection_ok")


def _metric(score: Mapping, name: str) -> object:
    return score.get(name) if name == "stage6_e2e_success" else score.get("metrics", {}).get(name)


def _rerun_admissions(run: Mapping) -> list[tuple[int, str | None]]:
    """(policy_run, args_sha256) of every rerun whose action reached the gateway.

    The runner creates one policy per rerun event, in event order, after the
    main run's (policy_run 1).
    """
    out, policy_run = [], 1
    for event in run.get("events", []):
        if event.get("op") not in RERUN_OPS:
            continue
        policy_run += 1
        outcome = event.get("outcome") or {}
        record = event.get("run_record") or {}
        proposed = [item.get("args_sha256") for item in record.get("events", [])]
        if outcome.get("status") is not None:
            out.append((policy_run, proposed[0] if len(proposed) == 1 else None))
    return out


class _Row:
    """One validated result row and the facts derived from it."""

    def __init__(self, row: Mapping, problems: list[dict]) -> None:
        self.case_id, self.mode, self.round = row["case_id"], row["mode"], row["round"]
        self.context = {"case_id": self.case_id, "mode": self.mode, "round": self.round}
        self.score, self.run = row.get("score"), row.get("run")
        self.provider_failure, self.error = row.get("provider_failure"), row.get("error")
        self.calls = row.get("calls") or {}
        self.diagnostics = list(row.get("diagnostics") or [])
        self.decisions = [decision for decision in row.get("grounding_decisions") or []
                          if self._valid_decision(decision, problems)]
        self.ungrounded_admitted = 0
        self.ungrounded_admitted_rerun = 0
        self.limitations: list[dict] = []
        if self.score is None and self.provider_failure is None and self.error is None:
            problems.append({**self.context, "code": "unscored_without_failure"})
        if self.score is not None and (self.run is None or self.score.get("case_id") != self.case_id
                                       or self.run.get("case_id") != self.case_id):
            problems.append({**self.context, "code": "score_or_run_mismatch"})
        if self.run is not None:
            self._admissions(problems)

    def _valid_decision(self, decision: object, problems: list[dict]) -> bool:
        ok = (isinstance(decision, Mapping)
              and all(decision.get(key) == value for key, value in self.context.items())
              and type(decision.get("policy_run")) is int and type(decision.get("step")) is int
              and type(decision.get("grounded")) is bool and isinstance(decision.get("audit"), Mapping)
              and (decision["grounded"] == (decision.get("code") is None))
              and (decision["grounded"] == (decision.get("attribution") is None))
              and (decision["grounded"] or decision.get("attribution") in ATTRIBUTIONS))
        if not ok:
            problems.append({**self.context, "code": "invalid_grounding_decision"})
        return ok

    def _decision(self, policy_run: int, args_sha256: str | None) -> Mapping | None:
        found = [item for item in self.decisions
                 if item["policy_run"] == policy_run and item["args_sha256"] == args_sha256]
        return found[0] if len(found) == 1 else None

    def _admissions(self, problems: list[dict]) -> None:
        main_action = self.run.get("main_action")
        if self.run.get("main_outcome") is not None:
            decision = self._decision(1, (main_action or {}).get("args_sha256"))
            if decision is None:
                problems.append({**self.context, "code": "admission_without_grounding_record"})
            elif not decision["grounded"]:
                self.ungrounded_admitted = 1
            elif self.score is not None and self.score.get("metrics", {}).get("action_args_ok") is False:
                # Grounded, admitted, and still not the customer's target: target binding (M1-A3).
                self.limitations.append({**self.context, "action_name": decision["action_name"],
                                         "args_sha256": decision["args_sha256"],
                                         "supports": decision["supports"]})
        for policy_run, args_sha256 in _rerun_admissions(self.run):
            decision = self._decision(policy_run, args_sha256)
            if decision is None:
                problems.append({**self.context, "code": "rerun_admission_without_grounding_record"})
            elif not decision["grounded"]:
                self.ungrounded_admitted_rerun += 1
        if self.mode == MODES[1] and (self.ungrounded_admitted or self.ungrounded_admitted_rerun):
            problems.append({**self.context, "code": "enforce_admitted_ungrounded_action"})

    @property
    def rejections(self) -> list[Mapping]:
        return [item for item in self.decisions if not item["grounded"]]


def _distribution(items: Iterable[Mapping], key) -> dict[str, int]:
    return dict(sorted(Counter(key(item) for item in items).items()))


def _group_round(rows: Sequence[_Row], expected_ids: Sequence[str]) -> dict:
    scored = [row for row in rows if row.score is not None]
    rejections = [item for row in rows for item in row.rejections]
    present = {row.case_id for row in rows}
    return {
        "cases_present": len(rows), "scored": len(scored),
        "missing_cases": [case_id for case_id in expected_ids if case_id not in present],
        "unscored_cases": sorted(row.case_id for row in rows if row.score is None),
        "complete": len(scored) == len(expected_ids),
        "ungrounded_admitted": sum(row.ungrounded_admitted for row in rows),
        "ungrounded_admitted_cases": sorted(row.case_id for row in rows if row.ungrounded_admitted),
        "ungrounded_admitted_rerun": sum(row.ungrounded_admitted_rerun for row in rows),
        "rejections": {
            "count": len(rejections),
            "main": sum(1 for item in rejections if item["policy_run"] == 1),
            "rerun": sum(1 for item in rejections if item["policy_run"] > 1),
            "by_code": _distribution(rejections, lambda item: item["code"]),
            "by_reason": _distribution(rejections, lambda item: str(item["audit"].get("reason"))),
            "by_attribution": _distribution(rejections, lambda item: item["attribution"]),
            "cases": sorted({item["case_id"] for item in rejections}),
        },
        "metrics": {name: sum(_metric(row.score, name) is True for row in scored) for name in METRICS},
        "hard_invariants": {name: sum(row.score.get("hard_invariants", {}).get(name) is True for row in scored)
                            for name in HARD_INVARIANTS},
        "provider_failures": sorted(row.case_id for row in rows if row.provider_failure),
        "errors": sorted(row.case_id for row in rows if row.error),
        "calls": {kind: sum(int(row.calls.get(kind, 0)) for row in rows) for kind in ("control", "generation")},
    }


def _means(rounds: Sequence[dict]) -> dict:
    def avg(values):
        values = list(values)
        return round(mean(values), 3) if values else None

    codes = sorted({code for item in rounds for code in item["rejections"]["by_code"]})
    return {
        "rounds": len(rounds), "all_complete": all(item["complete"] for item in rounds),
        "ungrounded_admitted": avg(item["ungrounded_admitted"] for item in rounds),
        "ungrounded_admitted_rerun": avg(item["ungrounded_admitted_rerun"] for item in rounds),
        "rejections": avg(item["rejections"]["count"] for item in rounds),
        "rejections_by_code": {code: avg(item["rejections"]["by_code"].get(code, 0) for item in rounds)
                               for code in codes},
        "metrics": {name: avg(item["metrics"][name] for item in rounds) for name in METRICS},
        "hard_invariants": {name: avg(item["hard_invariants"][name] for item in rounds)
                            for name in HARD_INVARIANTS},
        "calls": {kind: avg(item["calls"][kind] for item in rounds) for kind in ("control", "generation")},
    }


def summarize_rows(rows: Iterable[Mapping], *, expected_rounds: int = 3,
                   expected_case_ids: Sequence[str]) -> dict:
    expected_ids = list(expected_case_ids)
    problems: list[dict] = []
    buckets: dict[tuple[str, int], list[_Row]] = {(mode, number): [] for number in range(1, expected_rounds + 1)
                                                  for mode in MODES}
    seen = set()
    for source in rows:
        context = {key: source.get(key) for key in ("case_id", "mode", "round")} if isinstance(source, Mapping) else {}
        key = (context.get("mode"), context.get("round"))
        if key not in buckets or context.get("case_id") not in expected_ids:
            problems.append({**context, "code": "invalid_row_identity"})
            continue
        identity = (*key, context["case_id"])
        if identity in seen:
            problems.append({**context, "code": "duplicate_row"})
            continue
        seen.add(identity)
        buckets[key].append(_Row(source, problems))
    all_rows = [row for group in buckets.values() for row in group]

    groups = {}
    for mode in MODES:
        rounds = [{"round": number, **_group_round(buckets[mode, number], expected_ids)}
                  for number in range(1, expected_rounds + 1)]
        groups[mode] = {"rounds": rounds, "mean": _means(rounds)}

    comparisons = []
    for number in range(1, expected_rounds + 1):
        sides = {mode: {row.case_id: row for row in buckets[mode, number] if row.score is not None}
                 for mode in MODES}
        paired = [case_id for case_id in expected_ids if all(case_id in sides[mode] for mode in MODES)]
        entry = {"round": number, "paired_cases": len(paired), "complete": len(paired) == len(expected_ids),
                 "metrics": {}}
        for name in COST_METRICS:
            values = {case_id: tuple(_metric(sides[mode][case_id].score, name) for mode in MODES)
                      for case_id in paired}
            entry["metrics"][name] = {
                "cost_cases": [case_id for case_id, pair in values.items() if pair == (True, False)],
                "gain_cases": [case_id for case_id, pair in values.items() if pair == (False, True)],
            }
            entry["metrics"][name]["delta"] = (len(entry["metrics"][name]["gain_cases"])
                                               - len(entry["metrics"][name]["cost_cases"]))
        entry["completion_cost_cases"] = entry["metrics"]["final_state_ok"]["cost_cases"]
        comparisons.append(entry)
    frequency = {name: dict(sorted(Counter(case_id for entry in comparisons
                                           for case_id in entry["metrics"][name]["cost_cases"]).items()))
                 for name in COST_METRICS}

    attributions = []
    for row in all_rows:
        for item in row.rejections:
            audit = item["audit"]
            attributions.append({
                **row.context, "policy_run": item["policy_run"], "step": item["step"],
                "action_name": item["action_name"], "args_sha256": item["args_sha256"],
                "code": item["code"], "attribution": item["attribution"], "reason": audit.get("reason"),
                "prior_read_tools": item.get("prior_read_tools", []),
                "earlier_order_read_had_target": audit.get("earlier_order_read_had_target"),
                "value_sources": audit.get("value_sources", {}),
            })
            if item["attribution"] == ATTRIBUTION_FAIL_CLOSED:
                problems.append({**row.context, "code": "fail_closed_rejection"})
            if audit.get("code_consistent") is False:
                problems.append({**row.context, "code": "rejection_code_inconsistent_with_recheck"})
        for item in row.decisions:
            if item["grounded"] and item["audit"].get("gate_agrees") is False:
                problems.append({**row.context, "code": "admission_the_recheck_rejects"})
        if row.diagnostics:
            problems.append({**row.context, "code": "wrapper_diagnostics",
                             "diagnostics": sorted({item.get("code") for item in row.diagnostics})})
    false_rejections = [item for item in attributions if item["attribution"] == ATTRIBUTION_FALSE]
    hard_failures = [{**row.context, "invariant": name} for row in all_rows if row.score is not None
                     for name in HARD_INVARIANTS if row.score.get("hard_invariants", {}).get(name) is not True]
    errors = [{**row.context, **row.error} for row in all_rows if row.error]
    provider_failures = [{**row.context, **row.provider_failure} for row in all_rows if row.provider_failure]
    stop_reasons = [reason for reason, present in (("false_rejection", false_rejections),
                                                    ("hard_invariant_failure", hard_failures),
                                                    ("integrity", problems),
                                                    ("unexpected_error", errors)) if present]
    complete = all(item["complete"] for group in groups.values() for item in group["rounds"])
    return {
        "schema": SUMMARY_SCHEMA, "scope": SCOPE, "expected_rounds": expected_rounds,
        "expected_cases": len(expected_ids), "complete": complete,
        "stop_required": bool(stop_reasons), "stop_reasons": stop_reasons,
        "groups": groups, "comparisons": {"rounds": comparisons, "cost_case_frequency": frequency},
        "rejection_attributions": attributions, "false_rejections": false_rejections,
        "gate_cannot_stop": [item for row in all_rows for item in row.limitations],
        "hard_invariant_failures": hard_failures, "provider_failures": provider_failures,
        "errors": errors, "integrity_problems": problems,
    }


# --------------------------------------------------------------------------
# The Markdown report
# --------------------------------------------------------------------------


def _cell(value: object) -> str:
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value) or "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows]
    return lines


def _sources(value_sources: Mapping) -> str:
    parts = []
    for name, flags in value_sources.items():
        where = [label for label, key in (("用户文本", "in_user_text"), ("某次读取", "in_some_read")) if flags.get(key)]
        parts.append(name + "∈" + ("/".join(where) if where else "无来源"))
    return "; ".join(parts)


def render_report(summary: Mapping) -> str:
    groups, rounds = summary["groups"], range(1, summary["expected_rounds"] + 1)
    lines = ["# M1-A2 Grounding Gate 前后对比（DEV）", "", "**" + summary["scope"] + "**", "",
             "- 完整：%s；必须停止：%s %s" % (summary["complete"], summary["stop_required"],
                                         summary["stop_reasons"] or ""), ""]
    lines += ["## 主指标：无依据动作进入网关（ungrounded_admitted）", ""]
    lines += _table(["组"] + ["第 %d 轮" % number for number in rounds] + ["均值", "rerun 中（逐轮）"],
                    [[mode] + [item["ungrounded_admitted"] for item in groups[mode]["rounds"]]
                     + [groups[mode]["mean"]["ungrounded_admitted"],
                        [item["ungrounded_admitted_rerun"] for item in groups[mode]["rounds"]]]
                     for mode in MODES])
    lines += ["", "shadow 中进入网关的无依据动作（逐轮）：", ""]
    lines += ["- 第 %d 轮：%s" % (item["round"], _cell(item["ungrounded_admitted_cases"]))
              for item in groups["shadow"]["rounds"]]
    lines += ["", "## would_reject（shadow）与 gate_rejections（enforce）", ""]
    lines += _table(["组", "轮", "次数（主/rerun）", "按 code", "按复核原因", "按归因"],
                    [[mode, item["round"], "%d（%d/%d）" % (item["rejections"]["count"], item["rejections"]["main"],
                                                         item["rejections"]["rerun"]),
                      json.dumps(item["rejections"]["by_code"]), json.dumps(item["rejections"]["by_reason"]),
                      json.dumps(item["rejections"]["by_attribution"])]
                     for mode in MODES for item in groups[mode]["rounds"]])
    lines += ["", "## Scorer 指标（为真的 case 数 / 已评分）", ""]
    lines += _table(["组", "轮", "已评分"] + list(METRICS),
                    [[mode, item["round"], item["scored"]] + [item["metrics"][name] for name in METRICS]
                     for mode in MODES for item in groups[mode]["rounds"]]
                    + [[mode, "均值", "—"] + [groups[mode]["mean"]["metrics"][name] for name in METRICS]
                       for mode in MODES])
    lines += ["", "## 六个硬不变量（每组每轮必须 40/40）", ""]
    lines += _table(["组", "轮", "已评分"] + list(HARD_INVARIANTS),
                    [[mode, item["round"], item["scored"]] + [item["hard_invariants"][name] for name in HARD_INVARIANTS]
                     for mode in MODES for item in groups[mode]["rounds"]])
    lines += ["", "## 代价（同一轮配对：shadow 真 → enforce 假 为代价，反之为收益）", ""]
    lines += _table(["轮", "指标", "差值（enforce − shadow）", "代价 case", "收益 case"],
                    [[entry["round"], name, cost["delta"], cost["cost_cases"], cost["gain_cases"]]
                     for entry in summary["comparisons"]["rounds"] for name, cost in entry["metrics"].items()])
    lines += ["", "代价 case 在几轮中出现：", "", "```json",
              json.dumps(summary["comparisons"]["cost_case_frequency"], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 每次 enforce 拒绝的归因", ""]
    enforce = [item for item in summary["rejection_attributions"] if item["mode"] == MODES[1]]
    lines += _table(["case", "轮", "run/step", "动作", "code", "复核原因", "归因", "此前读取", "编号来源", "更早读取曾含目标"],
                    [[item["case_id"], item["round"], "%d/%d" % (item["policy_run"], item["step"]), item["action_name"],
                      item["code"], item["reason"], {"true_rejection": "真拦截", "false_rejection": "误拒",
                                                     "fail_closed": "fail-closed"}[item["attribution"]],
                      item["prior_read_tools"], _sources(item["value_sources"]),
                      item["earlier_order_read_had_target"]] for item in enforce])
    lines += ["", "误拒：%d 条。" % len(summary["false_rejections"]), ""]
    lines += ["## gate 挡不住的（grounded 且进入网关，但 action_args_ok 为假：目标绑定，属于 M1-A3）", ""]
    lines += _table(["case", "组", "轮", "动作", "supports"],
                    [[item["case_id"], item["mode"], item["round"], item["action_name"], item["supports"]]
                     for item in summary["gate_cannot_stop"]])
    lines += ["", "## 调用次数", ""]
    lines += _table(["组", "轮", "控制调用", "生成调用"],
                    [[mode, item["round"], item["calls"]["control"], item["calls"]["generation"]]
                     for mode in MODES for item in groups[mode]["rounds"]])
    lines += ["", "## Provider 失败、异常与完整性问题", "", "```json",
              json.dumps({key: summary[key] for key in ("provider_failures", "errors", "hard_invariant_failures",
                                                        "false_rejections", "integrity_problems")},
                         ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def load_rows(out: Path) -> list[dict]:
    rows = []
    for path in sorted(Path(out).glob("round-*/*/cases.jsonl")):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize an M1-A2 result directory.")
    parser.add_argument("out", type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)
    case_ids = ["s6-dev-%03d" % number for number in range(1, 41)]
    summary = summarize_rows(load_rows(args.out), expected_rounds=args.rounds, expected_case_ids=case_ids)
    text = render_report(summary) if args.markdown else json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    return 0 if summary["complete"] and not summary["stop_required"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
