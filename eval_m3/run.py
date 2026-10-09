"""eval_m3 command line: preflight, cost estimate, case-runs, judge, scores, summary.

    python -X utf8 -m eval_m3.run --suite kb-dev --cases kb-dev-001,kb-dev-021 \
        --dotenv ../knowledge-agent/.env --output eval_m3/phase4/trial --yes
    python -X utf8 -m eval_m3.run --suite kb-dev --suite stage6-subset --runs 3 ...   (Phase 5)
    python -X utf8 -m eval_m3.run --preflight-only --suite kb-dev
    python -X utf8 -m eval_m3.run --suite kb-dev --provider mock --output <dir>       (offline dry run)
    python -X utf8 -m eval_m3.run --rescore <dir>     (re-score saved records + verdicts, no model call)

Real runs refuse to start unless local Ollama serves bge-m3 and the DeepSeek
configuration loads; they also refuse inside DeepSeek peak hours (Beijing,
weekdays 09-12 and 14-18) unless --allow-peak. The estimate is printed before
anything is sent; without --yes the runner asks for confirmation on stdin.
A run whose knowledge-base reads were not all hybrid is marked invalid.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import judge as judge_module
from . import scoring
from .runner import (BEIJING, KB_DEV_PATH, ROOT, STAGE6_DEV_PATH, SUITE_KB_DEV, SUITE_STAGE6, SUITES,
                     RecordingProvider, load_suite, new_run_directory, normalized_sha256, run_case)

# deepseek-flash, USD per 1M tokens at peak (api-docs.deepseek.com/quick_start/pricing, read 2026-10-09);
# off-peak is half. Peak: 01-04 and 06-10 UTC, Monday-Friday = Beijing 09-12 and 14-18.
PRICE_PEAK = {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20}
USD_TO_CNY = 7.1   # display only
# Per-turn planning figures, rounded up from the Phase 4 trial (eval_m3/phase4/trial: 7 business
# turns, 130,008 agent input / 1,715 output tokens, p50 4.4 s; 7 judge calls, 12,259 / 1,630 tokens).
# A policy read alone returns about 14k tokens, so policy turns dominate the input.
EST_AGENT_INPUT, EST_AGENT_OUTPUT, EST_AGENT_SECONDS = 20_000, 300, 5.0
EST_JUDGE_INPUT, EST_JUDGE_OUTPUT, EST_JUDGE_SECONDS = 2_000, 300, 2.0


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def is_peak(moment: datetime) -> bool:
    """Chinese public holidays are off-peak too; treating them as peak is the cautious side."""
    local = moment.astimezone(BEIJING)
    return local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)


def call_cost_usd(call: dict) -> float:
    factor = 1.0 if is_peak(datetime.fromisoformat(call["at"])) else 0.5
    prompt = call.get("prompt_tokens") or 0
    hit = call.get("cache_hit_tokens")
    miss = call.get("cache_miss_tokens")
    if hit is None or miss is None:
        hit, miss = 0, prompt
    output = call.get("completion_tokens") or 0
    return factor * (hit * PRICE_PEAK["cache_hit"] + miss * PRICE_PEAK["cache_miss"]
                     + output * PRICE_PEAK["output"]) / 1_000_000


def cost_summary(calls: list[dict]) -> dict:
    usd = sum(call_cost_usd(call) for call in calls)
    return {"calls": len(calls), "errors": sum(1 for call in calls if call.get("error")),
            "prompt_tokens": sum(call.get("prompt_tokens") or 0 for call in calls),
            "cache_hit_tokens": sum(call.get("cache_hit_tokens") or 0 for call in calls),
            "completion_tokens": sum(call.get("completion_tokens") or 0 for call in calls),
            "usd": round(usd, 4), "cny_approx": round(usd * USD_TO_CNY, 3)}


def estimate(cases, runs: int, *, judge: bool) -> dict:
    business = sum(1 for case in cases for _ in case.turns if case.type != "smalltalk") * runs
    judged = sum(len(case.turns) for case in cases if case.suite == SUITE_KB_DEV) * runs if judge else 0
    tokens_in = business * EST_AGENT_INPUT + judged * EST_JUDGE_INPUT
    tokens_out = business * EST_AGENT_OUTPUT + judged * EST_JUDGE_OUTPUT
    peak = (tokens_in * PRICE_PEAK["cache_miss"] + tokens_out * PRICE_PEAK["output"]) / 1_000_000
    return {"case_runs": len(cases) * runs, "business_turns": business, "judged_turns": judged,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "usd_offpeak_no_cache": round(peak / 2, 3), "usd_peak_no_cache": round(peak, 3),
            "cny_offpeak_no_cache": round(peak / 2 * USD_TO_CNY, 2),
            "minutes": round((business * EST_AGENT_SECONDS + judged * EST_JUDGE_SECONDS) / 60, 1),
            "basis": "upper bound: every input token billed as a cache miss"}


def check_ollama() -> dict:
    import requests
    from aftersales_service import knowledge_base as kb

    url = os.environ.get("AFTERSALES_KB_OLLAMA_URL", kb.DEFAULT_OLLAMA_URL).rstrip("/")
    if os.environ.get("AFTERSALES_KB_OFFLINE", "0").lower().strip() in ("1", "true"):
        return {"ok": False, "url": url, "reason": "AFTERSALES_KB_OFFLINE is set"}
    try:
        tags = requests.get(url + "/api/tags", timeout=3).json()
    except Exception as error:
        return {"ok": False, "url": url, "reason": "Ollama not reachable: " + type(error).__name__}
    names = [model.get("name", "") for model in tags.get("models", [])]
    if not any(name.split(":")[0] == kb.EMBEDDING_MODEL for name in names):
        return {"ok": False, "url": url, "reason": kb.EMBEDDING_MODEL + " is not pulled", "models": names}
    try:
        vector = kb.OllamaEmbedder(base_url=url).embed(["预检"])[0]
    except Exception as error:
        return {"ok": False, "url": url, "reason": "bge-m3 embed failed: " + type(error).__name__}
    return {"ok": True, "url": url, "model": kb.EMBEDDING_MODEL, "dimension": len(vector)}


def deepseek_providers(dotenv: Path):
    import llm_provider

    config = llm_provider.load_config("deepseek", environ={"LLM_DOTENV": str(dotenv)})
    return config, llm_provider.create_provider(config), llm_provider.create_provider(config)


def _git(*arguments: str) -> str | None:
    try:
        return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def _slim(run: dict) -> dict:
    """The persisted case-run: per-turn state replaced by the turn's state changes.

    Baseline and final state stay, so a run can be re-scored offline (--rescore).
    """
    slim = dict(run)
    before = run.get("baseline_state")
    turns = []
    for turn in run["turns"]:
        turn = dict(turn)
        after = turn.pop("state_after", None)
        if before is not None and after is not None:
            turn["state_changes"] = judge_module.state_changes(before, after)
            before = after
        turns.append(turn)
    slim["turns"] = turns
    if run.get("baseline_state") is not None and run.get("final_state") is not None:
        slim["final_state_changes"] = judge_module.state_changes(run["baseline_state"], run["final_state"])
    return slim


def _write_report(path: Path, summary: dict) -> None:
    lines = ["# eval_m3 run", "", "level: " + summary["meta"]["level"], ""]
    for suite, data in summary["suites"].items():
        lines += ["## " + suite, "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```", ""]
    lines += ["## cost", "", "```json", json.dumps(summary["cost"], ensure_ascii=False, indent=2), "```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def rescore(directory: Path) -> dict:
    """Recompute scores and suite summaries from saved records and verdicts; no model call."""
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    cases = {case.case_id: case for suite in SUITES for case in load_suite(suite)}
    runs = summary["meta"]["runs"]
    scores_by_suite: dict[str, list[list[dict]]] = {}
    for run_number in range(1, runs + 1):
        for path in sorted((directory / ("run-" + str(run_number))).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                if "baseline_state" not in item["record"]:
                    raise ValueError(item["case_id"] + ": the record has no saved state; it cannot be re-scored")
                case = cases[item["case_id"]]
                verdicts = {int(key): value for key, value in (item["verdicts"] or {}).items()} or None
                score = scoring.score_case_run(case, item["record"], verdicts)
                scores_by_suite.setdefault(case.suite, [[] for _ in range(runs)])[run_number - 1].append(score)
    for suite, runs_scores in scores_by_suite.items():
        retrieval = summary["suites"].get(suite, {}).get("retrieval")
        summary["suites"][suite] = scoring.summarize(runs_scores)
        if retrieval is not None:
            summary["suites"][suite]["retrieval"] = retrieval
    summary["meta"]["rescored_at"] = beijing_now().isoformat(timespec="seconds")
    summary["meta"]["rescored_with"] = _git("rev-parse", "HEAD")
    return summary


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["--rescore"]:
        if len(argv) != 2:
            print("usage: --rescore <result directory>", file=sys.stderr)
            return 2
        directory = Path(argv[1])
        summary = rescore(directory)
        (directory / "summary.rescored.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                         encoding="utf-8")
        print(json.dumps({suite: data["pass_hat_k"] for suite, data in summary["suites"].items()}))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", action="append", choices=SUITES, required=True)
    parser.add_argument("--cases", help="comma-separated case ids (default: every case of the suites)")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--provider", choices=("deepseek", "mock"), default="deepseek")
    parser.add_argument("--dotenv", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--level", default="Phase 4 runner check; not a Phase 5 result")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--no-retrieval", action="store_true")
    parser.add_argument("--allow-peak", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    arguments = parser.parse_args(argv)

    cases = [case for suite in dict.fromkeys(arguments.suite) for case in load_suite(suite)]
    if arguments.cases:
        wanted = [item.strip() for item in arguments.cases.split(",") if item.strip()]
        known = {case.case_id for case in cases}
        unknown = [item for item in wanted if item not in known]
        if unknown:
            parser.error("unknown case ids: " + ", ".join(unknown))
        cases = [case for case in cases if case.case_id in wanted]
    real = arguments.provider == "deepseek"
    use_judge = not arguments.no_judge
    preflight = {"provider": arguments.provider, "beijing_time": beijing_now().isoformat(timespec="seconds"),
                 "peak": is_peak(beijing_now()), "cases": len(cases), "runs": arguments.runs}
    if real:
        preflight["ollama"] = check_ollama()
        try:
            config, agent_inner, judge_inner = deepseek_providers(arguments.dotenv)
            preflight["deepseek"] = {"ok": True, "model": config.model, "base_url": config.base_url}
        except Exception as error:
            preflight["deepseek"] = {"ok": False, "reason": type(error).__name__ + ": " + str(error)[:200]}
    preflight["estimate"] = estimate(cases, arguments.runs, judge=use_judge)
    print(json.dumps({"preflight": preflight}, ensure_ascii=False, indent=2), flush=True)
    if real and not (preflight["ollama"]["ok"] and preflight["deepseek"]["ok"]):
        print("preflight failed: real runs need Ollama with bge-m3 and the DeepSeek configuration", file=sys.stderr)
        return 2
    if arguments.preflight_only:
        return 0
    if real and preflight["peak"] and not arguments.allow_peak:
        print("refused: DeepSeek peak hours (Beijing weekdays 09-12, 14-18); pass --allow-peak to override",
              file=sys.stderr)
        return 3
    if real and not arguments.yes:
        if input("send these calls? [y/N] ").strip().lower() != "y":
            return 1
    if arguments.output is None:
        parser.error("--output is required for a run")

    from .mock_models import MockAgent, MockJudge
    if real:
        agent = RecordingProvider(agent_inner, role="agent")
        judge = RecordingProvider(judge_inner, role="judge")
        knowledge_factory = None
    else:
        from aftersales_service import knowledge_base as kb
        agent, judge = RecordingProvider(MockAgent(), role="agent"), RecordingProvider(MockJudge(), role="judge")
        base = kb.KnowledgeBase(kb.load_corpus())   # BM25 only: offline, no Ollama
        knowledge_factory = lambda: base   # noqa: E731

    output = arguments.output
    output.mkdir(parents=True, exist_ok=True)
    started = beijing_now()
    scores_by_suite: dict[str, list[list[dict]]] = {}
    with new_run_directory() as temporary:
        for run_number in range(1, arguments.runs + 1):
            records = {}
            for case in cases:
                run = run_case(case, agent, data_root=Path(temporary) / ("run-" + str(run_number)),
                               knowledge_base_factory=knowledge_factory)
                verdicts = (judge_module.judge_case_run(judge, case, run)
                            if use_judge and case.suite == SUITE_KB_DEV and "baseline_state" in run else None)
                score = scoring.score_case_run(case, run, verdicts)
                scores_by_suite.setdefault(case.suite, [[] for _ in range(arguments.runs)])[run_number - 1].append(score)
                line = {"run": run_number, "case_id": case.case_id, "record": _slim(run),
                        "verdicts": verdicts, "score": score}
                handle = records.setdefault(case.suite, (output / ("run-" + str(run_number))).joinpath(
                    case.suite + ".jsonl"))
                handle.parent.mkdir(parents=True, exist_ok=True)
                with handle.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(line, ensure_ascii=False) + "\n")
                print(json.dumps({"run": run_number, "case": case.case_id, "passed": score["passed"],
                                  "invariants": all(score["hard_invariants"].values()),
                                  "infra_error": score["infra_error"], "seconds": run["seconds"]},
                                 ensure_ascii=False), flush=True)
    suites = {suite: scoring.summarize(runs) for suite, runs in scores_by_suite.items()}
    retrieval = None
    if real and SUITE_KB_DEV in scores_by_suite and not arguments.no_retrieval:
        from aftersales_service.knowledge_base import shared_knowledge_base
        from .retrieval import retrieval_recall
        retrieval = retrieval_recall(load_suite(SUITE_KB_DEV), shared_knowledge_base())
        suites[SUITE_KB_DEV]["retrieval"] = {key: retrieval[key] for key in ("turns", "gold_sections", "summary")}
        (output / "retrieval.json").write_text(json.dumps(retrieval, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    valid = all(data["non_hybrid_retrievals"] == 0 for data in suites.values()) if real else False
    summary = {
        "meta": {"level": arguments.level, "valid": valid,
                 "validity_rule": "real provider, every knowledge-base read hybrid (bge-m3)",
                 "started_at": started.isoformat(timespec="seconds"),
                 "finished_at": beijing_now().isoformat(timespec="seconds"),
                 "git_commit": _git("rev-parse", "HEAD"), "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                 "git_dirty": bool(_git("status", "--porcelain")),
                 "provider": arguments.provider, "model": getattr(agent, "model", None),
                 "policy": "m3", "judge": judge_module.JUDGE_VERSION if use_judge else None,
                 "runs": arguments.runs, "cases": [case.case_id for case in cases],
                 "inputs": {"kb_dev_normalized_sha256": normalized_sha256(KB_DEV_PATH),
                            "stage6_dev_normalized_sha256": normalized_sha256(STAGE6_DEV_PATH)}},
        "preflight": preflight,
        "suites": suites,
        "cost": {"agent": cost_summary(agent.calls), "judge": cost_summary(judge.calls),
                 "total_usd": round(sum(call_cost_usd(call) for call in agent.calls + judge.calls), 4),
                 "pricing": {"model": "deepseek-flash", "usd_per_1m_peak": PRICE_PEAK, "offpeak_factor": 0.5,
                             "note": "computed from recorded usage; DeepSeek's bill is authoritative"}},
    }
    if not real:
        summary["cost"] = {"note": "mock provider: no real calls, no cost"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_report(output / "report.md", summary)
    print(json.dumps({"valid": valid, "cost_usd": summary["cost"].get("total_usd"),
                      **{suite: {"pass_hat_k": data["pass_hat_k"], "invariants": data["hard_invariants_all_hold"]}
                         for suite, data in suites.items()}}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
