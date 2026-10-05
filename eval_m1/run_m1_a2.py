"""M1-A2: the DEV-only shadow / enforce run of the grounding gate (docs/v2/m1-a2-grounding-eval.md).

    python -B -m eval_m1.run_m1_a2 --rounds 3 --out <an empty directory outside the repository>

Run once, by a person, after review. Never retried, resumed or rerun case by case.

Preflight (no provider is built before every check passes): a clean working
tree (untracked files included), HEAD recorded and containing the M1-A1.1
commit, zero diff from that commit under the frozen and product directories,
the gate's rule version m1-grounding/2 (the version the attribution re-check
implements), the
frozen DEV bytes (SHA-256 below, exactly s6-dev-001..040, each case valid), an
empty output directory outside every checkout of the repository.

The run: rounds 1..3; in each round the 40 cases under shadow, then the same
40 under enforce, each case once with

    run_stage6_case(case, factory)        factory() -> GroundingGatedPolicy(
                                              LLMNativeActionLoopPolicy(provider, formal=True), mode=...)
    score_stage6_case(case, run, generator=SharedGenerator(provider, formal=True))

on the existing DeepSeek configuration (llm_provider.load_config; the key is
read where llm_provider reads it and never appears here). A provider error is
recorded by class name and the run goes on with the next case; any other
exception is recorded the same way and later marks the summary
stop_required. The plan always runs to the end: a partial run could not be
resumed or rerun, so the stop conditions (hard invariants, false rejections,
unexpected errors) are judged by eval_m1.summarize, not here.

Output (the jsonl is never committed; only its SHA-256 is registered):

    <out>/started.json
    <out>/round-<r>/<mode>/cases.jsonl   one row per case: score, run, grounding records
    <out>/round-<r>/<mode>/meta.json     commit, model, times, SHA-256s
    <out>/summary.json, <out>/report.md  eval_m1.summarize
    <out>/manifest.json                  SHA-256 of every other output file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from eval_v2.stage6_runner import run_stage6_case
from eval_v2.stage6_runtime import validate_stage6_case
from eval_v2.stage6_scoring import score_stage6_case

from .grounding_policy import AUDIT_RULE_VERSION, MODES, GroundingGatedPolicy
from .summarize import SCOPE, load_rows, render_report, summarize_rows

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_PATH = "eval/v2/stage6-dev.json"
DEV_SHA256 = "80df024f9fde9dbe6b116ff7b12a2613bdfe8d87d7234b453e8ee3cd287bcbf3"
DEV_CASE_IDS = tuple("s6-dev-%03d" % number for number in range(1, 41))
M1_A1_MERGE = "e67e682957ac52a79a8d66a6864d1de3d20ce39a"
# M1-A1.1 (the exchange target SKU is contract only): the gate this run measures.
# Kept as an ancestor of main by a merge commit; a squash merge fails the preflight.
M1_A1_1_COMMIT = "b74ab2b5ac70c6298041fce503d630877aaf3bce"
ROUNDS = 3
# Must be byte-for-byte unchanged since M1-A1.1 (frozen eval + product).
FROZEN_PATHS = ("eval_v2", "aftersales", "eval/v2", "aftersales_service")
# Hashed into every meta.json: what this run executed.
SOURCE_PATHS = ("eval_m1", "eval_v2", "aftersales", "aftersales_service", "orchestration",
                "eval/v2/spec", "llm_provider.py", DEV_PATH)
# The Stage 6 formal configuration (HANDOFF section 31.A).
FORMAL_CONFIG = {"provider": "deepseek", "model": "deepseek-flash",
                 "base_url": "https://api.deepseek.com", "timeout": 180.0}
# Recorded as provider failures (class name only); anything else is an unexpected error.
PROVIDER_ERRORS = (requests.RequestException, TimeoutError, ConnectionError)
ROW_SCHEMA = "m1-a2-case/1"

InnerFactory = Callable[[], object]
GeneratorFactory = Callable[[], object]


class PreflightError(RuntimeError):
    """A precondition failed; no provider was built and nothing was run."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(root), *args], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return completed.stdout.decode("utf-8").strip()


def _checkout_roots(root: Path) -> list[Path]:
    roots = [root]
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            roots.append(Path(line[len("worktree "):]).resolve())
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    roots.append((common if common.is_absolute() else root / common).resolve())
    return roots


def preflight(out: Path, *, root: Path = REPO_ROOT) -> tuple[list[dict], dict]:
    """Every check before a provider exists. Returns the cases and the run context."""
    root, out = Path(root).resolve(), Path(out).resolve()
    dirty = git(root, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        raise PreflightError("the working tree must be clean, untracked files included:\n" + dirty)
    head = git(root, "rev-parse", "HEAD")
    try:
        git(root, "merge-base", "--is-ancestor", M1_A1_1_COMMIT, head)
    except subprocess.CalledProcessError:
        raise PreflightError("HEAD does not contain the M1-A1.1 commit " + M1_A1_1_COMMIT) from None
    changed = git(root, "diff", "--name-only", M1_A1_1_COMMIT, head, "--", *FROZEN_PATHS)
    if changed:
        raise PreflightError("frozen or product files differ from the M1-A1.1 commit:\n" + changed)
    from aftersales_service.action_grounding import GROUNDING_VERSION

    if GROUNDING_VERSION != AUDIT_RULE_VERSION:
        raise PreflightError("the gate runs " + GROUNDING_VERSION + ", the re-check implements "
                             + AUDIT_RULE_VERSION)
    if any(out == checkout or out.is_relative_to(checkout) for checkout in _checkout_roots(root)):
        raise PreflightError("--out must lie outside every checkout of the repository")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PreflightError("--out must be a new or empty directory (no resume, no rerun)")
    dataset = root / DEV_PATH
    if sha256_file(dataset) != DEV_SHA256:
        raise PreflightError("the DEV dataset is not the frozen " + DEV_SHA256)
    cases = json.loads(dataset.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or tuple(case.get("case_id") for case in cases) != DEV_CASE_IDS:
        raise PreflightError("the DEV dataset must be exactly s6-dev-001..040, in order")
    for case in cases:
        validate_stage6_case(case)
    sources = git(root, "ls-files", "--", *SOURCE_PATHS).splitlines()
    context = {
        "schema": "m1-a2-context/1", "scope": SCOPE, "commit": head, "m1_a1_merge": M1_A1_MERGE,
        "m1_a1_1_commit": M1_A1_1_COMMIT, "grounding_version": AUDIT_RULE_VERSION,
        "working_tree_clean": True, "dataset": DEV_PATH, "dataset_sha256": DEV_SHA256,
        "cases": len(cases), "rounds": ROUNDS, "modes": list(MODES),
        "source_sha256": {name: sha256_file(root / name) for name in sources},
    }
    return cases, context


def formal_provider():
    """The existing DeepSeek configuration, exactly as Stage 6 ran it. Returns (provider, public config)."""
    from llm_provider import create_provider, load_config

    config = load_config("deepseek")
    public = config.public_dict()
    if any(public.get(name) != value for name, value in FORMAL_CONFIG.items()) or not public["api_key_set"]:
        raise PreflightError("the configuration is not the Stage 6 formal one: " + json.dumps(
            {name: public.get(name) for name in (*FORMAL_CONFIG, "api_key_set")}, sort_keys=True))
    return create_provider(config), public


def _failure(phase: str, error: BaseException) -> dict:
    # The class name only: provider messages can carry request or key material.
    return {"phase": phase, "error_type": type(error).__name__}


def run_one(case: dict, mode: str, round_number: int, *, inner_factory: InnerFactory,
            generator_factory: GeneratorFactory) -> dict:
    """One case-run (main run plus any runner-created reruns), scored, as one privacy-safe row."""
    policies: list[GroundingGatedPolicy] = []

    def factory() -> GroundingGatedPolicy:
        policy = GroundingGatedPolicy(inner_factory(), mode=mode, case_id=case["case_id"],
                                      round=round_number, policy_run=len(policies) + 1)
        policies.append(policy)
        return policy

    row = {"schema": ROW_SCHEMA, "case_id": case["case_id"], "mode": mode, "round": round_number,
           "started_at": now(), "finished_at": None, "score": None, "run": None,
           "grounding_decisions": [], "diagnostics": [], "provider_failure": None, "error": None,
           "calls": {"control": 0, "generation": 0}}
    phase, run = "run", None
    try:
        run = run_stage6_case(case, factory)
        row["run"] = run.to_dict()
        phase = "score"
        row["score"] = score_stage6_case(case, run, generator=generator_factory()).to_dict()
    except PROVIDER_ERRORS as error:
        row["provider_failure"] = _failure(phase, error)
    except Exception as error:  # recorded, never retried; the summary then requires a stop
        row["error"] = _failure(phase, error)
    finally:
        row["grounding_decisions"] = [record.to_dict() for policy in policies
                                      for record in policy.grounding_decisions]
        row["diagnostics"] = [item for policy in policies for item in policy.diagnostics]
        # One control call per inner decision record; one generation call per scored answer.
        row["calls"]["control"] = sum(len(tuple(policy.decision_records)) for policy in policies)
        main = None if run is None else run.main_record
        row["calls"]["generation"] = int(phase == "score" and main is not None
                                         and main.final_disposition == "answer")
        row["finished_at"] = now()
    return row


def run_experiment(cases: list[dict], out: Path, context: dict, *, inner_factory: InnerFactory,
                   generator_factory: GeneratorFactory, rounds: int = ROUNDS,
                   run_case: Callable[..., dict] = run_one) -> dict:
    """Round by round: shadow's 40, then enforce's 40. Every case exactly once; no early stop."""
    out = Path(out).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PreflightError("--out must be a new or empty directory (no resume, no rerun)")
    out.mkdir(parents=True, exist_ok=True)
    with (out / "started.json").open("x", encoding="utf-8") as stream:  # one invocation per directory
        stream.write(json.dumps({**context, "started_at": now()}, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n")
    interrupted = False
    try:
        for round_number in range(1, rounds + 1):
            for mode in MODES:
                directory = out / ("round-%d" % round_number) / mode
                directory.mkdir(parents=True)
                path = directory / "cases.jsonl"
                started = now()
                try:
                    with path.open("x", encoding="utf-8", newline="\n") as stream:
                        for case in cases:
                            row = run_case(case, mode, round_number, inner_factory=inner_factory,
                                           generator_factory=generator_factory)
                            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                            status = ("provider_failure" if row["provider_failure"]
                                      else "error" if row["error"] else "scored")
                            print("round=%d mode=%s case=%s %s" % (round_number, mode, case["case_id"], status),
                                  flush=True)
                finally:
                    write_json(directory / "meta.json", {
                        **context, "round": round_number, "mode": mode, "started_at": started,
                        "finished_at": now(), "model": context.get("model"),
                        "files_sha256": {"cases.jsonl": sha256_file(path)}})
    except KeyboardInterrupt:
        interrupted = True
    finally:
        rows = load_rows(out)
        summary = summarize_rows(rows, expected_rounds=rounds, expected_case_ids=[case["case_id"] for case in cases])
        summary.update({"commit": context["commit"], "model": context.get("model"),
                        "interrupted": interrupted, "finished_at": now(),
                        "cases_sha256": {path.relative_to(out).as_posix(): sha256_file(path)
                                         for path in sorted(out.glob("round-*/*/cases.jsonl"))}})
        write_json(out / "summary.json", summary)
        (out / "report.md").write_text(render_report(summary), encoding="utf-8")
        write_json(out / "manifest.json", {
            "schema": "m1-a2-manifest/1", "commit": context["commit"],
            "files_sha256": {path.relative_to(out).as_posix(): sha256_file(path)
                             for path in sorted(out.rglob("*"))
                             if path.is_file() and path.name != "manifest.json"}})
    if interrupted:
        raise KeyboardInterrupt
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1-A2 DEV grounding-gate comparison (run once).")
    parser.add_argument("--rounds", type=int, choices=(ROUNDS,), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cases, context = preflight(args.out)
        provider, public = formal_provider()
    except PreflightError as error:
        print("M1-A2 preflight failed: " + str(error))
        return 2
    except Exception as error:
        print("M1-A2 preflight failed: " + type(error).__name__)
        return 2
    from eval_v2.action_loop import LLMNativeActionLoopPolicy
    from eval_v2.generation import SharedGenerator

    context["model"] = public
    try:
        summary = run_experiment(
            cases, args.out, context, rounds=args.rounds,
            inner_factory=lambda: LLMNativeActionLoopPolicy(provider, formal=True),
            generator_factory=lambda: SharedGenerator(provider, formal=True))
    except KeyboardInterrupt:
        print("Interrupted. Partial results and summary kept; do not resume or rerun.")
        return 130
    print("M1-A2 finished: complete=%s stop_required=%s" % (summary["complete"], summary["stop_required"]))
    return 0 if summary["complete"] and not summary["stop_required"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
