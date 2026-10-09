"""Real Phase 0.5 spot checks; separate results preserve the Phase 0 baseline.

Run c (with follow-up), d, and approval progress three times each. An
ambiguous return is clarified using the fixture's underwear item, so the
deterministic Guard requires approval. No operator approval is fabricated.
This is a small spot check, not KB-DEV or sealed-set acceptance.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import llm_provider
from aftersales_service import answer_policy as ap
from aftersales_service import decision_policy as dp
from aftersales_service import knowledge_base as kb
from aftersales_service.service import AftersalesService
from .run_deepseek_spike import PERSONA, RESULTS, Recorder, preflight, summary, turn_record

BEIJING = timezone(timedelta(hours=8))
DIALOGUES = (
    ("c", ("ORD-1001 这件能退吗？", "你刚才说的天数从哪天开始算？")),
    ("d", ("不是 7 天无理由吗？",)),
    ("f", ("ORD-1001 我要退货，尺码不合适",)),
)
CLARIFICATION = "我要退内衣那件，商品明细号 OI-1001-2，尺码不合适，不换货。"
PROGRESS = "现在进度怎么样？"


class ContextRecorder(Recorder):
    def post(self, url, *args, **kwargs):
        response = super().post(url, *args, **kwargs)
        if str(url).endswith("/chat/completions") and self.events:
            event = self.events[-1]
            payload = kwargs.get("json") or {}
            if not payload.get("tools"):
                data = json.loads(payload["messages"][-1]["content"])
                event["history_messages"] = len(data.get("history_replies", ()))
                event["history_kinds"] = [reply["kind"] for reply in data.get("history_replies", ())]
                event["current_question"] = [message["text"] for message in data.get("user_messages", ())
                                             if message.get("label") == ap.CURRENT_QUESTION_LABEL]
                event["offered_refs"] = [source["ref"] for source in data.get("sources", ())]
                if response.ok:
                    choice = response.json()["choices"][0]
                    event["answer_content"] = choice["message"].get("content")
                    event["finish_reason"] = choice.get("finish_reason")
        return response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dotenv", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dialogues", nargs="+", choices=("c", "d", "f"), default=("c", "d", "f"))
    parser.add_argument("--output", default="deepseek_phase0_5.json")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if Path(args.output).name != args.output:
        parser.error("--output must be a file name inside results")
    config = llm_provider.load_config("deepseek", environ={"LLM_DOTENV": args.dotenv})
    provider = llm_provider.create_provider(config)
    preflight()
    recorder = ContextRecorder()
    runs = []
    started_at = datetime.now(BEIJING).isoformat(timespec="seconds")
    with mock.patch.dict("os.environ", {dp.DECISION_POLICY_ENV: dp.POLICY_M3}), \
            mock.patch.object(requests, "post", recorder.post), \
            tempfile.TemporaryDirectory(prefix="m3-phase0-5-", ignore_cleanup_errors=True) as data_dir:
        service = None
        try:
            for repeat in range(1, args.repeats + 1):
                for name, texts in DIALOGUES:
                    if name not in args.dialogues:
                        continue
                    if service is not None:
                        service.close()
                    # Actions mutate fixture state. A new session alone does not
                    # isolate repeat runs: use a fresh database for each dialogue.
                    service = AftersalesService(
                        lambda: provider, data_dir=Path(data_dir) / (name + "-" + str(repeat)))
                    session_id = service.create_session(PERSONA)["session_id"]
                    turns = []
                    waiting = False

                    def submit(text):
                        first, clock = len(recorder.events), time.perf_counter()
                        index = len(turns) + 1
                        try:
                            payload = service.submit(session_id, text)
                        except Exception as error:
                            turns.append({"turn": index, "text": text,
                                          "error": type(error).__name__ + ":" + str(getattr(error, "code", "")),
                                          "seconds": round(time.perf_counter() - clock, 3),
                                          "model_calls": recorder.events[first:]})
                            return None
                        turns.append(turn_record(service, session_id, index, text, payload,
                                                 time.perf_counter() - clock, recorder.events[first:]))
                        turns[-1]["answer_errors"] = [step["answer_error"]
                            for step in payload.get("trace", {}).get("steps", ()) if "answer_error" in step]
                        return payload

                    last = None
                    for text in texts:
                        last = submit(text)
                        if last is None:
                            break
                    if name == "f" and last is not None:
                        for _ in range(2):
                            if last["status"] != "NEEDS_CLARIFICATION":
                                break
                            last = submit(CLARIFICATION)
                            if last is None:
                                break
                        waiting = last is not None and last["status"] == "WAITING_APPROVAL"
                        if waiting:
                            submit(PROGRESS)
                    run = {"dialogue": name, "repeat": repeat, "turns": turns}
                    if name == "f":
                        run["reached_waiting_approval"] = waiting
                    runs.append(run)
                    print(json.dumps({"dialogue": name, "repeat": repeat,
                                      "turns": len(turns), "statuses": [turn.get("status") for turn in turns]},
                                     ensure_ascii=False), flush=True)
        finally:
            if service is not None:
                service.close()
    all_turns = [turn for run in runs for turn in run["turns"] if "error" not in turn]
    report = {"started_at": started_at, "finished_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
              "provider": config.provider, "model": config.model,
              "decision_policy": dp.M3_DECISION_VERSION, "answer_policy": ap.M3_ANSWER_VERSION,
              "embedding_model": kb.EMBEDDING_MODEL, "persona": PERSONA,
              "fixture_isolation": "fresh database per dialogue and repeat",
              "summary": summary(all_turns), "runs": runs}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
