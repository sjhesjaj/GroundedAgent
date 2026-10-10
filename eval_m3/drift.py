"""Phase 5 drift check: the frozen Stage 6 runner with m3-decision/1 on the full Stage 6 DEV.

    python -X utf8 -m eval_m3.drift --dotenv ../knowledge-agent/.env --output <empty dir> \
        --baseline <stage6-dev-r1.cases.jsonl> --yes

Each of s6-dev-001..040 once, as the Stage 6 formal DEV run did, with only the
decision policy replaced:

    run_stage6_case(case, lambda: M3DecisionPolicy(provider))
    score_stage6_case(case, run, generator=SharedGenerator(provider, formal=True))

The frozen runner offers only its five read tools, so the knowledge and pending
tools are absent: this measures what the m3 prompt and composition change on
Stage 6 behaviour. The comparison is the Stage 6 formal DEV result (b55d5ed,
37/40), whose case rows are passed as --baseline (they live outside the
repository). No grounding gate on either side. A provider or any other error is
recorded by class name and the run goes on with the next case: no retry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from aftersales_service.decision_policy import M3_DECISION_VERSION, M3DecisionPolicy
from eval_v2.generation import SharedGenerator
from eval_v2.stage6_runner import run_stage6_case
from eval_v2.stage6_scoring import HARD_INVARIANTS, score_stage6_case

from .run import _git, beijing_now, call_cost_usd, cost_summary, deepseek_providers, is_peak
from .runner import STAGE6_DEV_PATH, RecordingProvider

DEV_SHA256 = "80df024f9fde9dbe6b116ff7b12a2613bdfe8d87d7234b453e8ee3cd287bcbf3"
COMPARED = ("stage6_e2e_success", "action_selection_ok", "capabilities_ok", "clarification_ok", "final_state_ok",
            "generation_ok")
EST_CALLS_PER_CASE, EST_INPUT_PER_CALL, EST_OUTPUT_PER_CALL = 4, 6_000, 200


def _metric(score: dict, name: str):
    return score.get(name) if name == "stage6_e2e_success" else score.get("metrics", {}).get(name)


def run_one(case: dict, provider: RecordingProvider) -> dict:
    row = {"case_id": case["case_id"], "started_at": beijing_now().isoformat(timespec="seconds"),
           "score": None, "error": None, "calls_before": len(provider.calls)}
    phase = "run"
    try:
        run = run_stage6_case(case, lambda: M3DecisionPolicy(provider))
        phase = "score"
        row["score"] = score_stage6_case(case, run, generator=SharedGenerator(provider, formal=True)).to_dict()
        row["capabilities"] = row["score"]["details"].get("capabilities")
    except Exception as error:   # recorded by class name only, never retried
        row["error"] = {"phase": phase, "error_type": type(error).__name__}
    row["calls"] = provider.calls[row.pop("calls_before"):]
    row["finished_at"] = beijing_now().isoformat(timespec="seconds")
    return row


def compare(rows: list[dict], baseline: list[dict]) -> dict:
    base = {row["case_id"]: row["score"] for row in baseline}
    scored = [row for row in rows if row["score"]]
    per_case, changed = [], []
    for row in rows:
        old = base.get(row["case_id"])
        new = row["score"]
        item = {"case_id": row["case_id"], "error": row["error"],
                **{name: [_metric(old, name) if old else None, _metric(new, name) if new else None]
                   for name in COMPARED}}
        per_case.append(item)
        if any(pair[0] != pair[1] for name, pair in item.items() if name in COMPARED):
            changed.append(row["case_id"])
    return {
        "cases": len(rows), "scored": len(scored), "errors": [row for row in rows if row["error"]],
        "m3": {name: sum(bool(_metric(row["score"], name)) for row in scored) for name in COMPARED},
        "stage6_formal": {name: sum(bool(_metric(score, name)) for score in base.values()) for name in COMPARED},
        "m3_failed_e2e": [row["case_id"] for row in scored if not row["score"]["stage6_e2e_success"]],
        "stage6_failed_e2e": [case_id for case_id, score in base.items() if not score["stage6_e2e_success"]],
        "hard_invariant_violations": {name: [row["case_id"] for row in scored
                                             if not row["score"]["hard_invariants"][name]]
                                      for name in HARD_INVARIANTS},
        "changed_cases": changed, "per_case": per_case,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dotenv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True, help="Stage 6 formal DEV cases.jsonl")
    parser.add_argument("--allow-peak", action="store_true")
    parser.add_argument("--yes", action="store_true")
    arguments = parser.parse_args(argv)

    raw = STAGE6_DEV_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DEV_SHA256:
        print("the Stage 6 DEV dataset is not the frozen one", file=sys.stderr)
        return 2
    cases = json.loads(raw.decode("utf-8"))
    baseline = [json.loads(line) for line in arguments.baseline.read_text(encoding="utf-8").splitlines()]
    if [row["case_id"] for row in baseline] != [case["case_id"] for case in cases]:
        print("the baseline rows do not match the Stage 6 DEV cases", file=sys.stderr)
        return 2
    if arguments.output.exists() and any(arguments.output.iterdir()):
        print("--output must be a new or empty directory (no resume, no rerun)", file=sys.stderr)
        return 2
    calls = len(cases) * EST_CALLS_PER_CASE
    estimate = {"cases": len(cases), "calls": calls,
                "usd_offpeak_no_cache": round(calls * (EST_INPUT_PER_CALL * 0.30 + EST_OUTPUT_PER_CALL * 1.20)
                                              / 1_000_000 / 2, 3)}
    print(json.dumps({"beijing_time": beijing_now().isoformat(timespec="seconds"), "peak": is_peak(beijing_now()),
                      "estimate": estimate}, ensure_ascii=False), flush=True)
    if is_peak(beijing_now()) and not arguments.allow_peak:
        print("refused: DeepSeek peak hours; pass --allow-peak to override", file=sys.stderr)
        return 3
    if not arguments.yes and input("send these calls? [y/N] ").strip().lower() != "y":
        return 1
    config, inner, _ = deepseek_providers(arguments.dotenv)
    provider = RecordingProvider(inner, role="agent")

    arguments.output.mkdir(parents=True, exist_ok=True)
    started = beijing_now()
    rows = []
    with (arguments.output / "cases.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for case in cases:
            row = run_one(case, provider)
            rows.append(row)
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            print(json.dumps({"case": case["case_id"], "error": row["error"],
                              "e2e": row["score"]["stage6_e2e_success"] if row["score"] else None},
                             ensure_ascii=False), flush=True)
    summary = {
        "meta": {"what": "Phase 5 drift check: frozen Stage 6 runner + " + M3_DECISION_VERSION + ", Stage 6 DEV",
                 "started_at": started.isoformat(timespec="seconds"),
                 "finished_at": beijing_now().isoformat(timespec="seconds"),
                 "git_commit": _git("rev-parse", "HEAD"), "git_dirty": bool(_git("status", "--porcelain")),
                 "model": config.model, "dataset_sha256": DEV_SHA256,
                 "baseline_sha256": hashlib.sha256(arguments.baseline.read_bytes()).hexdigest()},
        "comparison": compare(rows, baseline),
        "cost": {**cost_summary(provider.calls),
                 "usd_check": round(sum(call_cost_usd(call) for call in provider.calls), 4)},
    }
    (arguments.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
    result = summary["comparison"]
    print(json.dumps({"m3": result["m3"], "stage6_formal": result["stage6_formal"],
                      "changed": result["changed_cases"], "errors": len(result["errors"]),
                      "cost_usd": summary["cost"]["usd"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
