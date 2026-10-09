"""M3 Phase 0 spike: m3-decision/1 on real DeepSeek, over the demo data.

    python -X utf8 -m eval_m3.spike.run_deepseek_spike --dotenv <.env with the DeepSeek key> [--repeats 2]

Not an evaluation: five hand-picked dialogues, each in a fresh session of the
real product service (AFTERSALES_DECISION_POLICY=m3, demo database in a
temporary data directory, business time fixed at 2026-11-15 by the demo
store, knowledge base with BM25 + bge-m3 through Ollama). The run refuses to
start if Ollama / bge-m3 is not reachable, and records every knowledge call's
retrieval mode.

Per turn it records the tools called, the steps, the citations, the reply,
every model call's prompt / completion / cached tokens (DeepSeek usage, read
from the HTTP response), every embedding call, and the end-to-end seconds.
Writes eval_m3/spike/results/deepseek_spike.json. The API key is read by
llm_provider from the given .env file and is never written anywhere.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

import llm_provider
from aftersales_service import decision_policy as dp
from aftersales_service import knowledge_base as kb
from aftersales_service.service import AftersalesService

RESULTS = Path(__file__).resolve().parent / "results"
PERSONA = "demo-a"
DIALOGUES = (
    ("a", ("退货运费谁出？",)),
    ("b", ("退款几天到账？",)),
    ("c", ("ORD-1001 这件能退吗？", "你刚才说的天数从哪天开始算？")),
    ("d", ("不是 7 天无理由吗？",)),
    ("e", ("运费谁出？", "那帮我退了")),
)
ORIGINAL_POST = requests.post


class Recorder:
    """Every chat completion and embedding call, with its usage and wall time."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def post(self, url, *args, **kwargs):
        started = time.perf_counter()
        response = ORIGINAL_POST(url, *args, **kwargs)
        seconds = round(time.perf_counter() - started, 3)
        payload = kwargs.get("json") or {}
        if str(url).endswith("/chat/completions"):
            usage = {}
            if response.ok:
                usage = response.json().get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            messages = payload.get("messages") or []
            self.events.append({
                "kind": "decision" if payload.get("tools") else "generation",
                "seconds": seconds,
                "http_status": response.status_code,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": usage.get("prompt_cache_hit_tokens", details.get("cached_tokens")),
                "cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
                "messages": len(messages),
                "history_messages": sum(1 for message in messages
                                        if message.get("role") == "assistant"
                                        and str(message.get("content", "")).startswith(dp.EARLIER_REPLY_LABEL)),
                "offered": [tool["function"]["name"] for tool in payload.get("tools") or []],
            })
        elif str(url).endswith("/api/embed"):
            self.events.append({"kind": "embed", "seconds": seconds,
                                "inputs": len(payload.get("input") or [])})
        return response


def preflight() -> None:
    probe = kb.OllamaEmbedder.from_environment()
    vectors = probe.embed(["预检"])   # raises EmbeddingUnavailable when Ollama / bge-m3 is missing
    if not vectors[0]:
        raise SystemExit("bge-m3 returned an empty vector")


def turn_record(service: AftersalesService, session_id: str, turn_index: int, text: str,
                payload: dict, seconds: float, events: list[dict]) -> dict:
    trace = payload.get("trace") or {}
    steps = trace.get("steps") or []
    conversation = service._sessions[session_id]
    knowledge = [observation.result.trace for observation in conversation._observations
                 if observation.turn_index == turn_index
                 and observation.tool_name == kb.KNOWLEDGE_TOOL_NAME]
    calls = [event for event in events if event["kind"] != "embed"]
    return {
        "turn": turn_index,
        "text": text,
        "seconds": round(seconds, 3),
        "status": payload.get("status"),
        "reply_kind": payload["reply"]["kind"],
        "reply": payload["reply"]["text"],
        "tools": [step["tool_name"] + json.dumps(step["arguments"], ensure_ascii=False)
                  for step in steps if step["kind"] == "tool_call"],
        "steps": [{key: step.get(key) for key in ("run", "step", "kind", "tool_name", "result_status",
                                                   "disposition", "diagnostic", "code", "action_name")
                   if step.get(key) is not None} for step in steps],
        "decisions": [{key: call.get(key) for key in ("control_step", "selected_function",
                                                       "batch_functions", "action_kind", "diagnostic")}
                      for call in trace.get("model_calls") or []],
        "citations": [{key: citation[key] for key in ("producer", "source_type", "locator")}
                      for citation in payload.get("citations") or []],
        "action": (None if payload.get("action") is None
                   else {key: payload["action"].get(key) for key in ("action_name", "status", "arguments")}),
        "retrieval_modes": [{"mode": item.get("retrieval_mode"), "fallback": item.get("fallback"),
                             "passages": item.get("passages")} for item in knowledge],
        "model_calls": calls,
        "embed_calls": [event for event in events if event["kind"] == "embed"],
        "prompt_tokens": sum(call["prompt_tokens"] or 0 for call in calls),
        "completion_tokens": sum(call["completion_tokens"] or 0 for call in calls),
        "cached_tokens": sum(call["cached_tokens"] or 0 for call in calls),
    }


def summary(turns: list[dict]) -> dict:
    def stats(values: list[float]) -> dict:
        return {"p50": round(statistics.median(values), 3), "max": round(max(values), 3),
                "min": round(min(values), 3)}

    return {
        "turns": len(turns),
        "seconds": stats([turn["seconds"] for turn in turns]),
        "model_calls_per_turn": stats([len(turn["model_calls"]) for turn in turns]),
        "prompt_tokens": stats([turn["prompt_tokens"] for turn in turns]),
        "completion_tokens": stats([turn["completion_tokens"] for turn in turns]),
        "cached_tokens": stats([turn["cached_tokens"] for turn in turns]),
        "all_retrieval_hybrid": all(item["mode"] == kb.MODE_HYBRID
                                    for turn in turns for item in turn["retrieval_modes"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dotenv", required=True)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    config = llm_provider.load_config("deepseek", environ={"LLM_DOTENV": args.dotenv})
    provider = llm_provider.create_provider(config)
    preflight()
    recorder = Recorder()
    runs = []
    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with mock.patch.dict("os.environ", {dp.DECISION_POLICY_ENV: dp.POLICY_M3}), \
            mock.patch.object(requests, "post", recorder.post), \
            tempfile.TemporaryDirectory(prefix="m3-spike-", ignore_cleanup_errors=True) as data_dir:
        service = AftersalesService(lambda: provider, data_dir=data_dir)
        try:
            for repeat in range(1, args.repeats + 1):
                for name, texts in DIALOGUES:
                    session_id = service.create_session(PERSONA)["session_id"]
                    turns = []
                    for turn_index, text in enumerate(texts, start=1):
                        first = len(recorder.events)
                        clock = time.perf_counter()
                        try:
                            payload = service.submit(session_id, text)
                        except Exception as error:   # a failed turn is a finding, not a crash
                            turns.append({"turn": turn_index, "text": text,
                                          "error": type(error).__name__ + ":" + str(getattr(error, "code", "")),
                                          "seconds": round(time.perf_counter() - clock, 3),
                                          "model_calls": recorder.events[first:]})
                            break
                        seconds = time.perf_counter() - clock
                        turns.append(turn_record(service, session_id, turn_index, text, payload,
                                                 seconds, recorder.events[first:]))
                    runs.append({"dialogue": name, "repeat": repeat, "turns": turns})
        finally:
            service.close()
    all_turns = [turn for run in runs for turn in run["turns"] if "error" not in turn]
    report = {
        "started_at": started_at,
        "provider": config.provider, "model": config.model,
        "decision_policy": dp.M3_DECISION_VERSION,
        "embedding_model": kb.EMBEDDING_MODEL,
        "persona": PERSONA,
        "summary": summary(all_turns),
        "runs": runs,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "deepseek_spike.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
