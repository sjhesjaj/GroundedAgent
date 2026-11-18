"""Stage 1 trace overhead: TRACE_ENABLED on vs off over mocked requests (A/B).

The request path is real - FastAPI, planner, executor, evidence policy,
`answer_structured`/`answer_stream`, provider, conversation commit to SQLite -
but retrieval and the model are mocked to return instantly, so the measured
difference is the instrumentation (spans, sanitizing, two trace-DB writes),
not model noise. Arms alternate request by request so drift hits both equally.

Usage:
    .venv\\Scripts\\python.exe eval\\trace_overhead.py --output eval\\<name>.json [--n 200]

An existing output is never overwritten (the Stage 1 measurement lives in
eval/stage1_trace_overhead.json). The report records the git commit and
whether the tree was dirty, so a measurement can be tied to the code it ran.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LLM_DOTENV", "")  # never pick up a real DeepSeek config here
os.environ["LLM_PROVIDER"] = "ollama"

import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import v1_api  # noqa: E402
import wiki_runtime  # noqa: E402
from rag import Chunk  # noqa: E402
from storage import SQLiteStorage  # noqa: E402

CHUNK = Chunk(text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假。", source="sample_company_rules.md", index=1)
ANSWER = json.dumps({"answer": "年假为 5 天。[来源 1]"}, ensure_ascii=False)


class Fake:
    def __init__(self, stream: bool):
        self.stream = stream

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": ANSWER}, "prompt_eval_count": 120, "eval_count": 30, "done": True}

    def iter_lines(self, decode_unicode=False):
        parts = [ANSWER[:10], ANSWER[10:]]
        lines = [{"message": {"content": p}} for p in parts]
        lines.append({"message": {"content": ""}, "done": True, "prompt_eval_count": 120, "eval_count": 30})
        return iter(json.dumps(line, ensure_ascii=False) for line in lines)


def fake_post(url, *args, **kwargs):
    return Fake(bool(kwargs.get("stream")))


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    pick = lambda q: ordered[min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)]
    return {"n": len(samples), "mean_ms": statistics.fmean(samples), "stdev_ms": statistics.stdev(samples),
            "p50_ms": pick(0.5), "p95_ms": pick(0.95), "p99_ms": pick(0.99), "max_ms": ordered[-1]}


def compare(on: list[float], off: list[float]) -> dict:
    a, b = summarize(on), summarize(off)
    diff = a["mean_ms"] - b["mean_ms"]
    se = math.sqrt(a["stdev_ms"] ** 2 / a["n"] + b["stdev_ms"] ** 2 / b["n"])
    return {
        "trace_on": a, "trace_off": b,
        "mean_delta_ms": diff, "mean_delta_95ci_ms": [diff - 1.96 * se, diff + 1.96 * se],
        "p50_delta_ms": a["p50_ms"] - b["p50_ms"], "p95_delta_ms": a["p95_ms"] - b["p95_ms"],
        "mean_delta_pct": 100 * diff / b["mean_ms"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=200, help="requests per arm per scenario")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--output", default=str(ROOT / "eval" / "stage1_trace_overhead.json"))
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        print(f"REFUSED: {output} exists; measurements are never overwritten, pass a new --output")
        return 2
    git = lambda *cmd: subprocess.run(["git", *cmd], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    code = {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}

    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    v1_api.storage = SQLiteStorage(root / "app.db")
    v1_api.chunks[:] = [CHUNK]
    patches = [
        patch.object(wiki_runtime, "RUNTIME", wiki_runtime.WikiRuntime(root=root / "wiki")),
        patch("orchestration.document_adapter.retrieve_fast",
              side_effect=lambda question, chunks, top_k=4, trace=None: [(chunks[0], 3.5)]),
        patch.object(requests, "post", side_effect=fake_post),
    ]
    for item in patches:
        item.start()
    client = TestClient(api.app)
    scenarios = {"orchestrated_chat": "/api/chat", "orchestrated_stream": "/api/chat/stream"}
    counter = 0

    def one(url: str, traced: bool) -> float:
        nonlocal counter
        counter += 1
        os.environ["TRACE_ENABLED"] = "1" if traced else "0"
        body = {"question": "年假最多可以休多少天", "session_id": f"bench-{counter}",
                "client_id": "bench", "mode": "orchestrated"}
        started = perf_counter()
        response = client.post(url, json=body)
        elapsed = (perf_counter() - started) * 1000
        assert response.status_code == 200, response.text
        assert ("X-Run-Id" in response.headers) == traced
        return elapsed

    results = {}
    try:
        for name, url in scenarios.items():
            for _ in range(args.warmup):
                one(url, True)
                one(url, False)
            on, off = [], []
            for index in range(args.n):
                # ABAB... with the order flipped every other pair.
                first = index % 2 == 0
                (on if first else off).append(one(url, first))
                (off if first else on).append(one(url, not first))
            results[name] = compare(on, off)
        connection = sqlite3.connect(v1_api.storage.path)
        diag = sorted(r[0] for r in connection.execute(
            "SELECT trace_overhead_ms FROM trace_runs WHERE trace_overhead_ms IS NOT NULL"))
        span_counts = sorted(r[0] for r in connection.execute("SELECT span_count FROM trace_runs"))
        db_bytes = (root / "app.db").stat().st_size
        connection.close()
    finally:
        for item in patches:
            item.stop()
        os.environ.pop("TRACE_ENABLED", None)
        client.close()

    report = {
        "method": "TestClient, real request path, retrieval + model mocked; arms alternate per request",
        "git": code, "n_per_arm": args.n, "warmup_pairs": args.warmup,
        "python": platform.python_version(), "platform": platform.platform(),
        "scenarios": results,
        "internal_trace_overhead_ms": {"p50": diag[len(diag) // 2], "p95": diag[int(0.95 * (len(diag) - 1))],
                                       "max": diag[-1], "note": "self-timed by the recorder; diagnostic only"},
        "spans_per_run": {"min": span_counts[0], "max": span_counts[-1]},
        "app_db_bytes_after_benchmark": db_bytes,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, data in results.items():
        a, b = data["trace_on"], data["trace_off"]
        print(f"{name}: off mean {b['mean_ms']:.2f}ms p50 {b['p50_ms']:.2f} p95 {b['p95_ms']:.2f} | "
              f"on mean {a['mean_ms']:.2f}ms p50 {a['p50_ms']:.2f} p95 {a['p95_ms']:.2f} | "
              f"delta mean {data['mean_delta_ms']:+.2f}ms ({data['mean_delta_pct']:+.1f}%) "
              f"95%CI [{data['mean_delta_95ci_ms'][0]:+.2f}, {data['mean_delta_95ci_ms'][1]:+.2f}] "
              f"p50 {data['p50_delta_ms']:+.2f} p95 {data['p95_delta_ms']:+.2f}")
    print(f"internal trace_overhead_ms p50/p95/max: {report['internal_trace_overhead_ms']['p50']:.2f} / "
          f"{report['internal_trace_overhead_ms']['p95']:.2f} / {report['internal_trace_overhead_ms']['max']:.2f}")
    print(f"Wrote {output}")
    directory.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
