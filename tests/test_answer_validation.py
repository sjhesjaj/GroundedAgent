"""M10 answer-delivery tests: what may be returned, and by which interface.

Three layers, in order:

1. `validate_answer` and its parts, as pure functions;
2. `answer_structured` and `answer_stream` against scripted model responses,
   so a failure mode can be reproduced without a model;
3. `/api/chat` and `/api/chat/stream`, because a guard that is not on the path
   the product actually runs is not a guard.

The consistency cases in `InterfaceConsistencyTests` are the point of the
suite: given the same evidence and the same scripted model output, both
interfaces must reach the same verdict about what the answer is.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
import rag
from rag import (
    UNGROUNDED_ANSWER_MESSAGE,
    Chunk,
    answer_stream,
    answer_structured,
    citation_indices,
    invalid_citation_indices,
    needs_evidence_recheck,
    restates_question,
    ungrounded_quantities,
    validate_answer,
)
from storage import SQLiteStorage

CLIENT_A = "client-a"

LEAVE_CHUNK = Chunk(
    text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假；工作满三年后增加至 8 天。",
    source="sample_company_rules.md",
    index=1,
)
OVERTIME_CHUNK = Chunk(
    text="## 加班与调休\n经确认的加班时长可按 1:1 转为调休，调休应在 3 个月内使用，逾期自动失效。",
    source="sample_company_rules.md",
    index=2,
)

ONE_RESULT = [(LEAVE_CHUNK, 3.5)]
OVERTIME_RESULT = [(OVERTIME_CHUNK, 3.5)]


class ScriptedResponse:
    """One non-streaming Ollama reply."""

    def __init__(self, answer_text: str):
        self.answer_text = answer_text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "message": {
                "content": json.dumps({"answer": self.answer_text}, ensure_ascii=False)
            }
        }


class ScriptedStream:
    """One streaming Ollama reply, delivered a few characters at a time."""

    def __init__(self, answer_text: str, *, chunk_size: int = 3):
        payload = json.dumps({"answer": answer_text}, ensure_ascii=False)
        self.lines = [
            json.dumps({"message": {"content": payload[i : i + chunk_size]}})
            for i in range(0, len(payload), chunk_size)
        ]
        self.lines.append(json.dumps({"message": {"content": ""}, "done": True}))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, decode_unicode=False):
        assert decode_unicode
        return iter(self.lines)


def stream_text(answer_text: str, results, question: str, *, recheck: str | None = None) -> str:
    """Drive `answer_stream` with a scripted stream plus its scripted recheck.

    The stream now runs the same bounded evidence recheck the plain path runs,
    so a second (non-streaming) response has to be available. Defaulting it to
    the same text reproduces the "model says the same thing twice" case.
    """
    responses = [ScriptedStream(answer_text), ScriptedResponse(recheck if recheck is not None else answer_text)]
    with patch.object(rag.requests, "post", side_effect=responses):
        return "".join(answer_stream(question, results, []))


def structured_text(scripted: list[str], results, question: str) -> str:
    with patch.object(
        rag.requests, "post", side_effect=[ScriptedResponse(t) for t in scripted]
    ):
        return answer_structured(question, results, [])


# --------------------------------------------------------------------------
# 1. Pure judgement
# --------------------------------------------------------------------------


class CitationIndexTests(unittest.TestCase):
    def test_reads_both_spacings_the_prompt_produces(self):
        self.assertEqual(citation_indices("甲[来源1]乙[来源 2]"), [1, 2])

    def test_reads_a_combined_marker(self):
        self.assertEqual(citation_indices("结论[来源 1、3]"), [1, 3])

    def test_indices_inside_the_evidence_range_are_valid(self):
        self.assertEqual(invalid_citation_indices("甲[来源1]乙[来源2]", 2), [])

    def test_an_index_past_the_evidence_is_reported(self):
        self.assertEqual(invalid_citation_indices("甲[来源 5]", 2), [5])

    def test_zero_is_out_of_range(self):
        self.assertEqual(invalid_citation_indices("甲[来源 0]", 2), [0])

    def test_any_citation_is_out_of_range_without_evidence(self):
        self.assertEqual(invalid_citation_indices("甲[来源 1]", 0), [1])


class RestatesQuestionTests(unittest.TestCase):
    QUESTION = "设备遗失几小时内必须上报、报给谁？顺便把库存报给我"

    def test_an_exact_echo_is_a_restatement(self):
        self.assertTrue(restates_question(self.QUESTION, self.QUESTION))

    def test_punctuation_and_spacing_do_not_hide_an_echo(self):
        self.assertTrue(
            restates_question("设备遗失几小时内必须上报，报给谁 顺便把库存报给我", self.QUESTION)
        )

    def test_an_empty_answer_counts_as_no_answer(self):
        self.assertTrue(restates_question("   ", self.QUESTION))

    def test_a_real_answer_is_not_a_restatement(self):
        self.assertFalse(
            restates_question("设备遗失应在 2 小时内报告直属主管。[来源1]", self.QUESTION)
        )

    def test_a_short_answer_that_reuses_question_words_is_kept(self):
        # `5天` appears inside the question; it is still the answer to it.
        self.assertFalse(restates_question("5天", "年假有5天还是8天"))


class UngroundedQuantityTests(unittest.TestCase):
    def test_a_number_reused_for_another_conclusion_is_unsupported(self):
        # The evidence says overtime converts at 1:1. It says nothing about a
        # pay multiplier, so `1 倍` is an assertion the source does not make.
        self.assertEqual(
            ungrounded_quantities(
                "工作日加班的加班费按1倍计算。[来源1]", OVERTIME_RESULT, "加班费按几倍算"
            ),
            [("1", "倍")],
        )

    def test_a_quantity_the_evidence_states_is_grounded(self):
        self.assertEqual(
            ungrounded_quantities(
                "调休应在 3 个月内使用。[来源1]", OVERTIME_RESULT, "调休多久作废"
            ),
            [],
        )

    def test_chinese_numerals_match_their_arabic_source(self):
        self.assertEqual(
            ungrounded_quantities("调休三个月内有效。[来源1]", OVERTIME_RESULT, "调休"),
            [],
        )

    def test_a_guess_from_the_question_is_not_evidence_for_itself(self):
        """The caller's number must not become the proof of the caller's number.

        Evidence says 5 days. The user asks whether it is 99. Asserting 99 as
        fact is exactly the failure this check exists for - and it used to pass,
        because every quantity in the question was treated as supported.
        """
        self.assertEqual(
            ungrounded_quantities(
                "正式员工入职满一年后，每年享有 99 天带薪年假。[来源1]",
                ONE_RESULT,
                "正式员工入职满一年后，每年有 99 天年假吗？请核对原文",
            ),
            [("99", "天")],
        )

    def test_the_evidence_contradicting_the_guess_is_allowed(self):
        """`不是 99 天，是 5 天` quotes the guess in order to correct it."""
        self.assertEqual(
            ungrounded_quantities(
                "不是 99 天。正式员工每年享有 5 天带薪年假。[来源1]",
                ONE_RESULT,
                "每年有 99 天年假吗",
            ),
            [],
        )

    def test_restating_the_guess_as_a_condition_is_allowed(self):
        self.assertEqual(
            ungrounded_quantities(
                "如果按你说的 99 天计算，资料并未这样规定；原文写的是 5 天。[来源1]",
                ONE_RESULT,
                "每年有 99 天年假吗",
            ),
            [],
        )

    def test_a_quantity_the_evidence_supports_needs_no_hedging(self):
        self.assertEqual(
            ungrounded_quantities(
                "正式员工满三年后增加至 8 天。[来源1]", ONE_RESULT, "满三年多少天"
            ),
            [],
        )

    def test_nothing_is_checked_without_evidence(self):
        self.assertEqual(ungrounded_quantities("随便说 9 天", [], "问题"), [])


class ValidateAnswerTests(unittest.TestCase):
    def test_a_grounded_cited_answer_passes(self):
        ok, reason = validate_answer(
            "正式员工每年享有 5 天带薪年假。[来源1]", ONE_RESULT, "年假有多少天"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_an_explicit_refusal_always_passes(self):
        ok, reason = validate_answer(
            "根据现有资料无法确定。", ONE_RESULT, "年终奖发几个月"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "refusal")

    def test_a_narrated_plan_is_not_an_answer(self):
        """The failure mode this rule exists for, observed against the model.

        `我将逐一检查资料` restates nothing, carries no number, and cites no
        source, so the other three checks all pass it. What gives it away is
        that evidence was supplied and none of it was cited.
        """
        ok, reason = validate_answer(
            "根据用户问题，需要从资料中提取年假相关的内容。我将逐一检查资料中与问题相关的信息。",
            ONE_RESULT,
            "年假有多少天",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "no_citation")

    def test_an_uncited_answer_is_not_delivered_when_evidence_was_supplied(self):
        ok, reason = validate_answer("正式员工每年享有 5 天带薪年假。", ONE_RESULT, "年假有多少天")
        self.assertFalse(ok)
        self.assertEqual(reason, "no_citation")

    def test_an_uncited_answer_is_fine_when_there_was_no_evidence(self):
        # The legacy path calls the model with no retrieved results; there is
        # nothing to cite, so demanding a citation would be nonsense.
        ok, _reason = validate_answer("你好，有什么可以帮你？", [], "你好")
        self.assertTrue(ok)

    def test_a_refusal_needs_no_citation(self):
        ok, reason = validate_answer("根据现有资料无法确定。", ONE_RESULT, "年终奖发几个月")
        self.assertTrue(ok)
        self.assertEqual(reason, "refusal")

    def test_each_failure_reports_its_own_reason(self):
        cases = (
            ("", "empty_answer"),
            ("年假有多少天", "restates_question"),
            ("年假为 5 天。[来源 4]", "citation_out_of_range"),
            ("正式员工每年享有 5 天带薪年假。", "no_citation"),
            ("年假为 30 天。[来源1]", "ungrounded_quantity"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                ok, reason = validate_answer(text, ONE_RESULT, "年假有多少天")
                self.assertFalse(ok)
                self.assertEqual(reason, expected)


class RefusalWordingTests(unittest.TestCase):
    """One "no evidence" wording, whichever layer produced it.

    `rag` cannot import `chat_orchestration` - the dependency runs the other
    way - so the refusal text exists in both. This is what stops the copies
    from drifting into two different messages for the same situation.
    """

    def test_the_generation_refusal_matches_the_policy_refusal(self):
        import chat_orchestration

        self.assertEqual(UNGROUNDED_ANSWER_MESSAGE, chat_orchestration.MESSAGE_NO_ANSWER)

    def test_the_shared_wording_is_recognised_as_a_refusal(self):
        self.assertTrue(rag.is_refusal(UNGROUNDED_ANSWER_MESSAGE))


class EvidenceRecheckTriggerTests(unittest.TestCase):
    """The recheck stays a single retry; only its trigger widened."""

    def test_a_refusal_still_triggers_the_recheck(self):
        self.assertTrue(
            needs_evidence_recheck("根据现有资料无法确定。", ONE_RESULT, "年假有多少天")
        )

    def test_a_restated_question_now_triggers_it_too(self):
        self.assertTrue(needs_evidence_recheck("年假有多少天", ONE_RESULT, "年假有多少天"))

    def test_a_good_answer_does_not(self):
        self.assertFalse(
            needs_evidence_recheck("年假 5 天。[来源1]", ONE_RESULT, "年假有多少天")
        )

    def test_nothing_is_rechecked_without_evidence(self):
        self.assertFalse(needs_evidence_recheck("任何内容", [], "年假有多少天"))


# --------------------------------------------------------------------------
# 2. Generation paths against scripted responses
# --------------------------------------------------------------------------


class AnswerStructuredTests(unittest.TestCase):
    QUESTION = "年假有多少天"

    def test_a_restated_question_is_rechecked_and_repaired(self):
        answer = structured_text(
            [self.QUESTION, "正式员工每年享有 5 天带薪年假。[来源1]"],
            ONE_RESULT,
            self.QUESTION,
        )
        self.assertEqual(answer, "正式员工每年享有 5 天带薪年假。[来源1]")

    def test_a_restated_question_the_recheck_cannot_repair_becomes_a_refusal(self):
        answer = structured_text([self.QUESTION, self.QUESTION], ONE_RESULT, self.QUESTION)
        self.assertEqual(answer, UNGROUNDED_ANSWER_MESSAGE)
        self.assertNotIn(self.QUESTION, answer)

    def test_an_ungrounded_quantity_is_not_delivered(self):
        answer = structured_text(
            ["工作日加班的加班费按1倍计算。[来源1]", "根据现有资料无法确定。"],
            OVERTIME_RESULT,
            "加班费按几倍算",
        )
        self.assertTrue(rag.is_refusal(answer))
        self.assertNotIn("1倍", answer)

    def test_an_out_of_range_citation_is_not_delivered(self):
        answer = structured_text(
            ["年假为 5 天。[来源 7]", "年假为 5 天。[来源 7]"], ONE_RESULT, self.QUESTION
        )
        self.assertEqual(answer, UNGROUNDED_ANSWER_MESSAGE)

    def test_the_recheck_still_runs_at_most_once(self):
        with patch.object(
            rag.requests,
            "post",
            side_effect=[ScriptedResponse(self.QUESTION), ScriptedResponse(self.QUESTION)],
        ) as post:
            answer_structured(self.QUESTION, ONE_RESULT, [])
        self.assertEqual(post.call_count, 2)

    def test_a_good_first_answer_costs_one_call(self):
        with patch.object(
            rag.requests,
            "post",
            side_effect=[ScriptedResponse("年假 5 天。[来源1]")],
        ) as post:
            answer_structured(self.QUESTION, ONE_RESULT, [])
        self.assertEqual(post.call_count, 1)


class AnswerStreamTests(unittest.TestCase):
    QUESTION = "年假有多少天"

    def test_a_valid_answer_streams_through_unchanged(self):
        text = "正式员工每年享有 5 天带薪年假。[来源1]"
        self.assertEqual(stream_text(text, ONE_RESULT, self.QUESTION), text)

    def test_a_fabricated_citation_is_never_shown_at_all(self):
        """Nothing reaches the reader until the whole answer has been checked.

        The earlier design released prose as it arrived and only withheld an
        unclosed citation marker, so a rejected answer had already been
        displayed by the time it was rejected. Now the reader sees the refusal
        and nothing else.
        """
        bogus = "正式员工每年享有 5 天带薪年假。[来源 9]"
        with patch.object(
            rag.requests,
            "post",
            side_effect=[ScriptedStream(bogus), ScriptedResponse(bogus)],
        ):
            shown = "".join(answer_stream(self.QUESTION, ONE_RESULT, []))
        self.assertEqual(shown, UNGROUNDED_ANSWER_MESSAGE)
        self.assertNotIn("来源", shown)
        self.assertNotIn("5 天", shown)

    def test_a_restated_question_is_replaced_by_the_refusal(self):
        self.assertEqual(
            stream_text(self.QUESTION, ONE_RESULT, self.QUESTION),
            UNGROUNDED_ANSWER_MESSAGE,
        )

    def test_an_ungrounded_quantity_is_replaced_by_the_refusal(self):
        shown = stream_text(
            "工作日加班的加班费按1倍计算。[来源1]", OVERTIME_RESULT, "加班费按几倍算"
        )
        self.assertEqual(shown, UNGROUNDED_ANSWER_MESSAGE)
        self.assertNotIn("1倍", shown)

    def test_transport_errors_still_raise_rather_than_refuse(self):
        """A broken stream is an error, not a refusal - that distinction stays."""
        broken = [{"message": {"content": '{"answer":"partial'}}]

        class Truncated(ScriptedStream):
            def __init__(self):
                self.lines = [json.dumps(item) for item in broken]

        with patch.object(rag.requests, "post", return_value=Truncated()):
            with self.assertRaises(RuntimeError):
                list(answer_stream(self.QUESTION, ONE_RESULT, []))

    def test_a_refusal_streams_normally(self):
        text = "根据现有资料无法确定。"
        self.assertEqual(stream_text(text, ONE_RESULT, "年终奖发几个月"), text)

    def test_a_bracket_that_is_not_a_citation_is_still_delivered(self):
        text = "年假 5 天（见附录[A 章节说明，非来源编号，仅供参考]）。[来源1]"
        self.assertEqual(stream_text(text, ONE_RESULT, self.QUESTION), text)


class StreamingDeliveryCostTests(unittest.TestCase):
    """What "validate before emitting" costs, stated plainly.

    Nothing visible is released until the answer is complete and checked, so
    the first visible character now waits for the whole generation instead of
    the first token. These tests pin the resulting shape; the wall-clock cost
    is measured end to end in the revision report.
    """

    QUESTION = "年假有多少天"

    def test_a_grounded_answer_arrives_as_a_single_validated_delta(self):
        text = "正式员工每年享有 5 天带薪年假。[来源1]"
        stream = ScriptedStream(text, chunk_size=4)
        with patch.object(rag.requests, "post", return_value=stream):
            parts = list(answer_stream(self.QUESTION, ONE_RESULT, []))
        self.assertEqual(parts, [text])

    def test_nothing_is_emitted_before_the_answer_is_complete(self):
        """The generator must not yield while the model is still producing."""
        text = "正式员工每年享有 5 天带薪年假。[来源1]"
        stream = ScriptedStream(text, chunk_size=4)
        with patch.object(rag.requests, "post", return_value=stream):
            generator = answer_stream(self.QUESTION, ONE_RESULT, [])
            first = next(generator)
            # The first thing yielded is already the whole validated answer.
            self.assertEqual(first, text)
            self.assertEqual(list(generator), [])

    def test_empty_evidence_is_buffered_too(self):
        """The `results=[]` exception was removed by acceptance adjudication.

        An out-of-range citation or a restated question can happen with no
        evidence as well, and the earlier exception let the stream show that
        text before the plain path had refused it.
        """
        stream = ScriptedStream("Hello, world", chunk_size=4)
        with patch.object(rag.requests, "post", return_value=stream):
            parts = list(answer_stream(self.QUESTION, [], []))
        self.assertEqual(parts, ["Hello, world"])

    def test_a_fabricated_citation_without_evidence_is_never_shown(self):
        stream = ScriptedStream("年假 5 天。[来源 3]", chunk_size=4)
        with patch.object(rag.requests, "post", return_value=stream):
            shown = "".join(answer_stream(self.QUESTION, [], []))
        self.assertEqual(shown, UNGROUNDED_ANSWER_MESSAGE)
        self.assertNotIn("来源 3", shown)


class InterfaceConsistencyTests(unittest.TestCase):
    """Same evidence, same scripted output: same verdict on both interfaces."""

    QUESTION = "年假有多少天"

    CASES = (
        ("正式员工每年享有 5 天带薪年假。[来源1]", ONE_RESULT, "年假有多少天", True),
        ("根据现有资料无法确定。", ONE_RESULT, "年终奖发几个月", True),
        ("年假有多少天", ONE_RESULT, "年假有多少天", False),
        ("年假为 5 天。[来源 7]", ONE_RESULT, "年假有多少天", False),
        ("工作日加班的加班费按1倍计算。[来源1]", OVERTIME_RESULT, "加班费按几倍算", False),
    )

    def test_both_paths_agree_on_whether_the_text_is_deliverable(self):
        for text, results, question, deliverable in self.CASES:
            with self.subTest(text=text):
                verdict, _reason = validate_answer(text, results, question)
                self.assertEqual(verdict, deliverable)

                # Non-streaming: the recheck is scripted to repeat the same
                # text, so neither path gets a second, different answer.
                structured = structured_text([text, text], results, question)
                if deliverable:
                    self.assertEqual(structured, text)
                else:
                    self.assertTrue(rag.is_refusal(structured))

                # Streaming must produce the SAME final body - not merely
                # "fail somehow". Asserting only that it raised is what let the
                # two interfaces drift apart in the first place.
                streamed = stream_text(text, results, question)
                self.assertEqual(streamed, structured)
                if deliverable:
                    self.assertEqual(streamed, text)
                else:
                    self.assertEqual(streamed, UNGROUNDED_ANSWER_MESSAGE)
                    self.assertNotIn(text, streamed)

    def test_neither_path_ever_delivers_the_rejected_text(self):
        for text, results, question, deliverable in self.CASES:
            if deliverable:
                continue
            with self.subTest(text=text):
                structured = structured_text([text, text], results, question)
                self.assertNotEqual(structured, text)


# --------------------------------------------------------------------------
# 3. The paths the product actually runs
# --------------------------------------------------------------------------


class ApiDeliveryTests(unittest.TestCase):
    """The guards must be reachable through `/api/chat` and its SSE twin."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_storage = api.storage
        api.storage = SQLiteStorage(Path(self.temp_directory.name) / "test.db")
        with api.state_lock:
            api.chunks.clear()
            api.chunks.append(LEAVE_CHUNK)
            api.conversation_locks.clear()
        self.client = TestClient(api.create_app(v1_routes=True))
        patch(
            "orchestration.document_adapter.retrieve_fast",
            side_effect=lambda question, chunks, top_k=4, trace=None: (
                [(chunks[0], 3.5)] if chunks else []
            ),
        ).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()
        api.storage = self.original_storage
        self.temp_directory.cleanup()

    def payload(self, question, session_id):
        return {
            "question": question,
            "session_id": session_id,
            "client_id": CLIENT_A,
            "mode": "orchestrated",
        }

    def test_chat_persists_a_refusal_rather_than_a_restated_question(self):
        question = "年假天数的准确说法是什么"
        with patch.object(
            rag.requests,
            "post",
            side_effect=[ScriptedResponse(question), ScriptedResponse(question)],
        ):
            response = self.client.post("/api/chat", json=self.payload(question, "s-1"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], UNGROUNDED_ANSWER_MESSAGE)
        stored = api.storage.get_messages("s-1", CLIENT_A)
        self.assertEqual(stored[-1]["content"], UNGROUNDED_ANSWER_MESSAGE)

    @staticmethod
    def _sse(response):
        """Every delta, plus which terminal event arrived."""
        deltas, terminal, event = [], None, None
        for raw in response.text.splitlines():
            line = raw.strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                if event in {"done", "error"}:
                    terminal = event
            elif line.startswith("data:") and event == "delta":
                deltas.append(json.loads(line[5:].strip()).get("content", ""))
        return "".join(deltas), terminal

    # (first answer, recheck answer, expected final body, label)
    # The recheck is what the *existing* bounded second call returns. Both
    # interfaces get the identical two-response script.
    SHARED_DECISION_CASES = (
        ("正式员工每年享有 5 天带薪年假。[来源1]", "无所谓",
         "正式员工每年享有 5 天带薪年假。[来源1]", "first answer already valid"),
        ("正式员工每年享有 99 天带薪年假。[来源1]", "正式员工每年享有 5 天带薪年假。[来源1]",
         "正式员工每年享有 5 天带薪年假。[来源1]", "recheck corrects a wrong first answer"),
        ("根据现有资料无法确定。", "正式员工每年享有 5 天带薪年假。[来源1]",
         "正式员工每年享有 5 天带薪年假。[来源1]", "recheck corrects a false refusal"),
        ("正式员工每年享有 99 天带薪年假。[来源1]", "正式员工每年享有 99 天带薪年假。[来源1]",
         UNGROUNDED_ANSWER_MESSAGE, "both attempts undeliverable"),
    )

    def test_both_interfaces_share_the_recheck_and_its_decision(self):
        """R2: sharing `validate_answer` is not sharing the decision.

        The first rework shared only the validator, so the plain path could
        recover via the recheck while the stream, which had no recheck, refused
        - same evidence, same scripted model, different stored answer. These
        cases pin the whole chain: generation, the bounded recheck, extraction
        and the delivery verdict.
        """
        question = "年假有多少天"
        for first, recheck, expected, label in self.SHARED_DECISION_CASES:
            with self.subTest(case=label):
                with patch.object(
                    rag.requests,
                    "post",
                    side_effect=[ScriptedResponse(first), ScriptedResponse(recheck)],
                ) as plain_post:
                    plain = self.client.post(
                        "/api/chat", json=self.payload(question, f"p-{abs(hash(label))}")
                    )
                with patch.object(
                    rag.requests,
                    "post",
                    side_effect=[ScriptedStream(first), ScriptedResponse(recheck)],
                ) as stream_post:
                    streamed = self.client.post(
                        "/api/chat/stream",
                        json=self.payload(question, f"s-{abs(hash(label))}"),
                    )

                shown, terminal = self._sse(streamed)
                plain_body = plain.json()["answer"]

                self.assertEqual(plain_body, expected)
                self.assertEqual(shown, expected)
                self.assertEqual(terminal, "done")
                # identical model-call counts: at most two per question
                self.assertEqual(plain_post.call_count, stream_post.call_count)
                self.assertLessEqual(stream_post.call_count, 2)
                # identical persistence
                stored_plain = api.storage.get_messages(f"p-{abs(hash(label))}", CLIENT_A)
                stored_stream = api.storage.get_messages(f"s-{abs(hash(label))}", CLIENT_A)
                self.assertEqual(len(stored_plain), len(stored_stream))
                self.assertEqual(stored_plain[-1]["content"], stored_stream[-1]["content"])
                self.assertEqual(stored_stream[-1]["content"], expected)

    def test_stream_and_plain_agree_on_body_terminal_event_and_storage(self):
        """Same scripted output twice: both interfaces refuse and store that."""
        question = "年假天数的准确说法是什么"

        with patch.object(
            rag.requests,
            "post",
            side_effect=[ScriptedResponse(question), ScriptedResponse(question)],
        ):
            plain = self.client.post("/api/chat", json=self.payload(question, "s-2a"))
        with patch.object(
            rag.requests,
            "post",
            side_effect=[ScriptedStream(question), ScriptedResponse(question)],
        ):
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload(question, "s-2b")
            )

        shown, terminal = self._sse(streamed)
        plain_body = plain.json()["answer"]

        self.assertEqual(plain_body, UNGROUNDED_ANSWER_MESSAGE)
        self.assertEqual(shown, UNGROUNDED_ANSWER_MESSAGE)
        self.assertNotIn(question, shown)
        self.assertEqual(terminal, "done")
        stored_plain = api.storage.get_messages("s-2a", CLIENT_A)
        stored_stream = api.storage.get_messages("s-2b", CLIENT_A)
        self.assertEqual(len(stored_plain), len(stored_stream))
        self.assertEqual(stored_plain[-1]["content"], stored_stream[-1]["content"])
        self.assertEqual(stored_stream[-1]["content"], UNGROUNDED_ANSWER_MESSAGE)

    def test_a_valid_json_envelope_fallback_is_unwrapped_not_pasted(self):
        """R1 tail: a legal envelope from the fallback call is not the body.

        First reply is not JSON, so the plain-text fallback runs; that call
        happens to return a valid `{"answer": ...}`. The envelope must be
        unwrapped, never sent or stored verbatim.
        """
        question = "年假有多少天"
        body = "正式员工每年享有 5 天带薪年假。[来源1]"

        class RawReply:
            def __init__(self, text): self.text = text
            def raise_for_status(self): return None
            def json(self): return {"message": {"content": self.text}}

        with patch.object(
            rag.requests,
            "post",
            side_effect=[
                RawReply("not json at all"),
                RawReply(json.dumps({"answer": body}, ensure_ascii=False)),
            ],
        ):
            response = self.client.post("/api/chat", json=self.payload(question, "s-env"))

        answer_body = response.json()["answer"]
        self.assertEqual(answer_body, body)
        self.assertNotIn('{"answer"', answer_body)
        self.assertEqual(
            api.storage.get_messages("s-env", CLIENT_A)[-1]["content"], body
        )

    def test_a_plain_text_fallback_is_delivered_as_prose(self):
        """The other fallback shape: no envelope at all."""
        question = "年假有多少天"
        body = "正式员工每年享有 5 天带薪年假。[来源1]"

        class RawReply:
            def __init__(self, text): self.text = text
            def raise_for_status(self): return None
            def json(self): return {"message": {"content": self.text}}

        with patch.object(
            rag.requests, "post", side_effect=[RawReply("also not json"), RawReply(body)]
        ):
            response = self.client.post("/api/chat", json=self.payload(question, "s-plain"))
        self.assertEqual(response.json()["answer"], body)

    def test_a_transport_failure_still_errors_and_persists_nothing(self):
        """A broken stream stays an error - the refusal path must not swallow it."""
        question = "年假有多少天"

        class Truncated:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def raise_for_status(self): return None
            def iter_lines(self, decode_unicode=False):
                return iter([json.dumps({"message": {"content": '{"answer":"partial'}})])

        with patch.object(rag.requests, "post", return_value=Truncated()):
            response = self.client.post(
                "/api/chat/stream", json=self.payload(question, "s-2c")
            )
        _shown, terminal = self._sse(response)
        self.assertEqual(terminal, "error")
        self.assertEqual(api.storage.get_messages("s-2c", CLIENT_A), [])

    def test_a_valid_answer_still_reaches_both_interfaces(self):
        question = "年假有多少天"
        text = "正式员工每年享有 5 天带薪年假。[来源1]"

        with patch.object(rag.requests, "post", return_value=ScriptedResponse(text)):
            plain = self.client.post("/api/chat", json=self.payload(question, "s-3"))
        with patch.object(rag.requests, "post", return_value=ScriptedStream(text)):
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload(question, "s-4")
            )

        self.assertEqual(plain.json()["answer"], text)
        self.assertIn("event: done", streamed.text)
        self.assertEqual(
            api.storage.get_messages("s-3", CLIENT_A)[-1]["content"],
            api.storage.get_messages("s-4", CLIENT_A)[-1]["content"],
        )

    def test_both_interfaces_return_the_same_sources_for_the_same_question(self):
        question = "年假有多少天"
        text = "正式员工每年享有 5 天带薪年假。[来源1]"

        with patch.object(rag.requests, "post", return_value=ScriptedResponse(text)):
            plain = self.client.post("/api/chat", json=self.payload(question, "s-5"))
        with patch.object(rag.requests, "post", return_value=ScriptedStream(text)):
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload(question, "s-6")
            )

        streamed_sources = None
        for line in streamed.text.splitlines():
            if line.startswith("data: ") and '"sources"' in line:
                streamed_sources = json.loads(line[6:])["sources"]
                break
        self.assertIsNotNone(streamed_sources)
        self.assertEqual(plain.json()["sources"], streamed_sources)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
