"""Stage 3: score the current Planner's `requires_freshness` on the temporal dev set.

Read-only with respect to the agent: it calls `plan_request` and, for plans
without a System step, feeds the real Evidence Policy the kind of evidence the
Wiki/Document adapters actually produce, to show what a freshness flag costs
downstream. No model, no retrieval, no network.

Usage:
    .venv\\Scripts\\python.exe eval\\stage3_freshness_baseline.py --output eval\\stage3\\<name>.json

An existing output is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestration.contracts import Evidence, SourceType, ToolResult, ToolStatus  # noqa: E402
from orchestration.document_adapter import _to_evidence as document_evidence  # noqa: E402
from orchestration.evidence_policy import REASON_FRESHNESS_UNSUPPORTED, evaluate_evidence  # noqa: E402
from orchestration.planner import FRESHNESS_MARKERS, ToolName, plan_request  # noqa: E402
from orchestration.wiki_adapter import WIKI_AUTHORITY  # noqa: E402
from rag import Chunk  # noqa: E402

DATASET = ROOT / "eval" / "v1" / "datasets" / "eval_temporal_freshness_dev.json"
EXPECTED_ANSWERABLE = {"current_knowledge", "incidental"}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def freshness_refusal_probe(plan) -> bool | None:
    """Would the Evidence Policy refuse this plan for freshness alone?

    Only answerable without a live system: plans with a System step get
    `observed_at` from the provider, so the probe returns None for them. The
    evidence mirrors the adapters exactly - documents carry no version or
    observation time; Wiki claims carry a page version the policy ignores.
    """
    if ToolName.SYSTEM_QUERY in plan.steps:
        return None
    results = {}
    if ToolName.DOCUMENT_SEARCH in plan.steps:
        chunk = Chunk(text="## 示例\n示例条款正文。", source="sample_company_rules.md", index=1)
        results[ToolName.DOCUMENT_SEARCH] = ToolResult(
            tool_name=ToolName.DOCUMENT_SEARCH.value, status=ToolStatus.OK,
            evidence=(document_evidence(chunk, 3.0, 1),))
    if ToolName.WIKI_QUERY in plan.steps:
        results[ToolName.WIKI_QUERY] = ToolResult(
            tool_name=ToolName.WIKI_QUERY.value, status=ToolStatus.OK,
            evidence=(Evidence(content="示例概览。", source_type=SourceType.WIKI, source="sample_company_rules.md",
                               locator="section:示例", version="v1", observed_at=None, authority=WIKI_AUTHORITY),))
    if not results:
        return None
    decision = evaluate_evidence(plan, results)
    return REASON_FRESHNESS_UNSUPPORTED in decision.reason_codes


def rate(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--kind", default="baseline (current planner, before any change)")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists() or output.with_suffix(".md").exists():
        print(f"REFUSED: {output} (or its .md) exists; results are never overwritten")
        return 2
    assert "blind" not in DATASET.name and "holdout" not in DATASET.name

    raw = DATASET.read_bytes()
    cases = json.loads(raw.decode("utf-8"))
    rows = []
    for case in cases:
        plan = plan_request(case["question"])
        predicted = plan.signals.requires_freshness
        expected = case["expected_requires_freshness"]
        outcome = ("TP" if predicted and expected else "FP" if predicted else "FN" if expected else "TN")
        probe = freshness_refusal_probe(plan)
        rows.append({
            "id": case["id"], "question": case["question"], "markers": case["markers"],
            "temporal_intent": case["temporal_intent"],
            "expected_requires_freshness": expected, "predicted_requires_freshness": predicted,
            "freshness_outcome": outcome,
            "expected_route": case["expected_route"], "predicted_route": plan.route.value,
            "route_match": plan.route.value == case["expected_route"],
            "steps": [s.value for s in plan.steps], "reason_codes": list(plan.reason_codes),
            "planner_marker_hits": [m for m in FRESHNESS_MARKERS if m in case["question"]],
            "refused_for_freshness_without_system": probe,
        })

    counts = Counter(r["freshness_outcome"] for r in rows)
    tp, fp, fn, tn = (counts[k] for k in ("TP", "FP", "FN", "TN"))
    by_intent = defaultdict(Counter)
    for r in rows:
        by_intent[r["temporal_intent"]][r["freshness_outcome"]] += 1
        by_intent[r["temporal_intent"]]["route_match"] += r["route_match"]
        by_intent[r["temporal_intent"]]["n"] += 1
    by_marker = defaultdict(Counter)
    for r in rows:
        for m in r["markers"]:
            by_marker[m]["n"] += 1
            by_marker[m]["correct"] += r["freshness_outcome"] in ("TP", "TN")
    answerable = [r for r in rows if r["temporal_intent"] in EXPECTED_ANSWERABLE]
    blocked = [r for r in answerable if r["refused_for_freshness_without_system"]]

    report = {
        "stage": "3", "kind": args.kind,
        "dataset": DATASET.name, "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "git": {"commit": git("rev-parse", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))},
        "planner_freshness_markers": list(FRESHNESS_MARKERS),
        "freshness": {
            "n": len(rows), "accuracy": rate(tp + tn, len(rows)),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": rate(tp, tp + fp), "recall": rate(tp, tp + fn),
            "false_positive_rate": rate(fp, fp + tn), "false_negative_rate": rate(fn, fn + tp),
        },
        "route_accuracy": rate(sum(r["route_match"] for r in rows), len(rows)),
        "by_intent": {k: dict(v) for k, v in sorted(by_intent.items())},
        "by_marker": {k: {"n": v["n"], "freshness_correct": v["correct"]} for k, v in sorted(by_marker.items())},
        "downstream": {
            "expected_answerable": len(answerable),
            "refused_for_freshness_without_system": len(blocked),
            "ids": [r["id"] for r in blocked],
            "note": "probe uses the real evaluate_evidence with adapter-shaped evidence; "
                    "document evidence has version=None and observed_at=None",
        },
        "cases": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    f = report["freshness"]
    print(f"freshness accuracy {f['accuracy']} | TP {tp} FP {fp} FN {fn} TN {tn} | "
          f"precision {f['precision']} recall {f['recall']} | route accuracy {report['route_accuracy']}")
    print(f"expected-answerable cases refused for freshness: {len(blocked)}/{len(answerable)}")
    print(f"Wrote {output}")
    return 0


def markdown(report: dict) -> str:
    f = report["freshness"]
    lines = [
        f"# Stage 3 · freshness 评分：{report['kind']}", "",
        f"- 数据集 `{report['dataset']}`，sha256 `{report['dataset_sha256'][:16]}…`，{f['n']} 条",
        f"- 代码 `{report['git']['commit'][:7]}`，dirty={report['git']['dirty']}",
        f"- Planner 的 freshness 词表：{'、'.join(report['planner_freshness_markers'])}", "",
        "| 指标 | 值 |", "|---|---|",
        f"| freshness accuracy | {f['accuracy']} ({f['tp'] + f['tn']}/{f['n']}) |",
        f"| TP / FP / FN / TN | {f['tp']} / {f['fp']} / {f['fn']} / {f['tn']} |",
        f"| precision / recall | {f['precision']} / {f['recall']} |",
        f"| false positive rate / false negative rate | {f['false_positive_rate']} / {f['false_negative_rate']} |",
        f"| route accuracy（次要） | {report['route_accuracy']} |",
        f"| 应该能回答、却会因 freshness 被拒答 | {report['downstream']['refused_for_freshness_without_system']}"
        f"/{report['downstream']['expected_answerable']} |", "",
        "## 按意图", "", "| intent | n | TP | FP | FN | TN | route 正确 |", "|---|---|---|---|---|---|---|",
    ]
    for k, v in report["by_intent"].items():
        lines.append(f"| {k} | {v.get('n', 0)} | {v.get('TP', 0)} | {v.get('FP', 0)} | {v.get('FN', 0)} | "
                     f"{v.get('TN', 0)} | {v.get('route_match', 0)} |")
    lines += ["", "## 逐条", "",
              "| id | intent | 期望 / 实际 freshness | 结果 | 期望 / 实际 route | Planner 命中的词 | 因 freshness 拒答 |",
              "|---|---|---|---|---|---|---|"]
    for r in report["cases"]:
        lines.append(f"| `{r['id']}` | {r['temporal_intent']} | {r['expected_requires_freshness']} / "
                     f"{r['predicted_requires_freshness']} | {r['freshness_outcome']} | {r['expected_route']} / "
                     f"{r['predicted_route']} | {'、'.join(r['planner_marker_hits']) or '-'} | "
                     f"{'-' if r['refused_for_freshness_without_system'] is None else r['refused_for_freshness_without_system']} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
