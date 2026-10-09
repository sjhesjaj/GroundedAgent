"""Small real-provider product spot checks, never a DEV/HOLDOUT evaluator.

Each dialogue uses a fresh temporary product database. No operator approval is
fabricated. The pending-progress dialogue restarts the service before its
follow-up to exercise the persisted current-session gateway outcomes.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from unittest import mock

import llm_provider
from aftersales_service import decision_policy as dp
from aftersales_service.service import AftersalesService


class RecordedProvider:
    def __init__(self, provider):
        self._inner = provider
        self.name = provider.name
        self.model = provider.model
        self.timeout = provider.timeout
        self.calls = []

    def chat(self, messages, **kwargs):
        event = {"kind": "decision" if kwargs.get("tools") else "generation",
                 "max_tokens": kwargs.get("max_tokens"), "timeout_seconds": self.timeout}
        self.calls.append(event)
        started = perf_counter()
        try:
            response = self._inner.chat(messages, **kwargs)
        except Exception as error:
            event.update({"error": type(error).__name__, "seconds": round(perf_counter()-started, 3),
                          "prompt_tokens": None, "completion_tokens": None})
            raise
        event.update({"seconds": round(perf_counter()-started, 3),
                      "prompt_tokens": response.prompt_tokens,
                      "completion_tokens": response.completion_tokens,
                      "finish_reason": response.finish_reason})
        return response


DIALOGUES = [
    ("shipping_followup", [
        "商家把商品型号发错了，我寄回去的运费能报销多少，需不需要留下凭证？",
        "那刚才说的凭证具体是什么？",
    ]),
    ("pending_progress_after_restart", [
        "请把ORD-1001的棉质内衣L码申请退货，尺码不合适，商品保持完好。",
        "进度怎么样？",
    ]),
    ("mixed_then_progress", [
        "自己不想要商品了，退货的寄回运费由谁承担？",
        "明白了。请把ORD-1001的纯棉T恤M码两件申请退货，不想要了，商品完好。",
        "现在进度怎么样？",
    ]),
    ("smalltalk", ["你好", "谢谢", "再见"]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotenv", type=Path, default=Path.cwd()/".env")
    parser.add_argument("--output", type=Path, default=Path("eval_m3/phase3/reports/deepseek-spot-check.json"))
    arguments = parser.parse_args()
    config = llm_provider.load_config("deepseek", environ={"LLM_DOTENV": str(arguments.dotenv)})
    provider = RecordedProvider(llm_provider.create_provider(config))
    started_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    runs = []
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return provider

    with mock.patch.dict("os.environ", {dp.DECISION_POLICY_ENV: dp.POLICY_M3}), \
            tempfile.TemporaryDirectory(prefix="m3-phase3-spot-", ignore_cleanup_errors=True) as temporary:
        for name, questions in DIALOGUES:
            service = AftersalesService(factory, data_dir=Path(temporary)/name)
            turns = []
            run = {"dialogue": name, "turns": turns, "recovery_model_calls": None}
            try:
                session_id = service.create_session("demo-a")["session_id"]
                for index, question in enumerate(questions, 1):
                    if name == "pending_progress_after_restart" and index == 2:
                        before_recovery = len(provider.calls)
                        service.close()
                        service = AftersalesService(factory, data_dir=Path(temporary)/name)
                        service.session_view(session_id)
                        run["recovery_model_calls"] = len(provider.calls)-before_recovery
                    first_call, first_factory = len(provider.calls), len(factory_calls)
                    started = perf_counter()
                    try:
                        payload = service.submit(session_id, question)
                    except Exception as error:
                        turns.append({"turn": index, "question": question,
                                      "error": type(error).__name__, "code": getattr(error, "code", None),
                                      "calls": provider.calls[first_call:],
                                      "seconds": round(perf_counter()-started, 3)})
                        break
                    trace = payload.get("trace") or {}
                    action = payload.get("action")
                    turns.append({
                        "turn": index, "question": question, "status": payload.get("status"),
                        "reply": payload.get("reply"), "citations": payload.get("citations", []),
                        "action": None if action is None else {key: action.get(key) for key in
                            ("action_name", "status", "arguments", "code")},
                        "trace": trace, "calls": provider.calls[first_call:],
                        "provider_factory_calls": len(factory_calls)-first_factory,
                        "seconds": round(perf_counter()-started, 3),
                    })
                    print(json.dumps({"dialogue": name, "turn": index, "status": payload.get("status"),
                                      "calls": len(provider.calls)-first_call,
                                      "tools": [step.get("tool_name") for step in trace.get("steps", [])
                                                if step.get("tool_name")]}, ensure_ascii=False), flush=True)
                runs.append(run)
            finally:
                service.close()
    all_turns = [turn for run in runs for turn in run["turns"]]
    progress = [turn for run in runs if "progress" in run["dialogue"] for turn in run["turns"]
                if "进度" in turn["question"]]
    report = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "provider": config.provider, "model": config.model, "policy": dp.POLICY_M3,
        "level": "Handwritten product spot checks; not DEV/HOLDOUT evaluation or milestone scoring",
        "fixture_isolation": "Fresh temporary seeded database per dialogue; no fabricated operator approval",
        "summary": {"dialogues": len(runs), "turns": len(all_turns),
                    "errored_turns": sum("error" in turn for turn in all_turns),
                    "chat_calls": len(provider.calls),
                    "prompt_tokens": sum(call.get("prompt_tokens") or 0 for call in provider.calls),
                    "completion_tokens": sum(call.get("completion_tokens") or 0 for call in provider.calls),
                    "progress_turns": len(progress),
                    "progress_used_pending_tool": sum(any(step.get("tool_name") == "get_my_pending_requests"
                        for step in turn.get("trace", {}).get("steps", [])) for turn in progress)},
        "runs": runs,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
