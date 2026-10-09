"""M3 Phase 0: single-query bge-m3 embedding latency through Ollama on this machine.

    python -X utf8 -m eval_m3.spike.bge_m3_latency [--runs 20]

One embedding call per query (the product embeds each search_knowledge_base
query once; passage vectors are computed once per process). Reports the cold
first call, then p50 / p95 over --runs warm calls, for 127.0.0.1 (what the
product uses) and for localhost (Windows tries IPv6 first). Writes
eval_m3/spike/results/bge_m3_latency.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import requests

from aftersales_service.knowledge_base import EMBEDDING_MODEL, KnowledgeBase, load_corpus

RESULTS = Path(__file__).resolve().parent / "results"
QUERIES = ("退货运费谁出？", "退款几天到账？", "ORD-1001 这件能退吗？", "你刚才说的天数从哪天开始算？",
           "不是 7 天无理由吗？")


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def embed(base_url: str, texts: list[str]) -> tuple[float, int]:
    started = time.perf_counter()
    response = requests.post(base_url + "/api/embed", json={"model": EMBEDDING_MODEL, "input": texts},
                             timeout=(2, 120))
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return elapsed, len(response.json()["embeddings"][0])


def measure(base_url: str, runs: int, unload_first: bool) -> dict[str, object]:
    if unload_first:
        requests.post(base_url + "/api/generate", json={"model": EMBEDDING_MODEL, "keep_alive": 0},
                      timeout=(2, 60))
    cold, dimensions = embed(base_url, [QUERIES[0]])
    warm = [embed(base_url, [QUERIES[index % len(QUERIES)]])[0] for index in range(runs)]
    passages = [passage.search_text for passage in KnowledgeBase(load_corpus()).passages]
    batch, _ = embed(base_url, passages)
    return {
        "base_url": base_url, "dimensions": dimensions,
        "first_call_seconds": round(cold, 4), "first_call_after_unload": unload_first,
        "warm_runs": runs,
        "warm_p50_seconds": round(statistics.median(warm), 4),
        "warm_p95_seconds": round(percentile(warm, 0.95), 4),
        "warm_min_seconds": round(min(warm), 4), "warm_max_seconds": round(max(warm), 4),
        "corpus_passages": len(passages), "corpus_batch_seconds": round(batch, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    report = {"model": EMBEDDING_MODEL,
              "ipv4": measure("http://127.0.0.1:11434", args.runs, unload_first=True),
              "localhost": measure("http://localhost:11434", args.runs, unload_first=False)}
    ps = requests.get("http://127.0.0.1:11434/api/ps", timeout=(2, 10)).json()
    report["ollama_ps"] = [{key: model.get(key) for key in ("name", "size", "size_vram")}
                           for model in ps.get("models", [])]
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "bge_m3_latency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
