"""M10 rework 8: real text boundaries, and a topic that is positively a topic.

Two residuals, both about evidence that was never actually gathered.

A. The refutation's end was checked against a *truncated* window. `following`
   stops before the next quantity, so `公司规定的99天不对5天内入职的员工公开`
   handed the regex only `不对` and its `$` matched - while the sentence went on.
   Whitespace failed the same way from the other side: a space or tab was
   listed as a legal terminator, so `不对 第三方公开` and `有误\tab工补贴`
   were approved with their objects unread.

B. `topic`/`about` accepted any short span containing no quantity and no listed
   breaker. A character count and "no known bad word found" are not evidence
   that a span is a noun topic, so short facts on either side of the refusal
   were swallowed, and a *negated* refusal predicate still qualified.

The pairs below change one thing at a time - whether a next quantity truncates
the window, whether whitespace precedes real punctuation or sits mid-word,
which side the fact is on, whether it carries a number, and the polarity of the
refusal predicate.
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
    is_pure_refusal,
    ungrounded_quantities,
    validate_answer,
)
from storage import SQLiteStorage

CLIENT_A = "client-a"

LEAVE_CHUNK = Chunk(
    text="正式员工入职满一年后，每年享有5天带薪年假。",
    source="sample_company_rules.md",
    index=1,
)
LEAVE = [(LEAVE_CHUNK, 1.0)]
GUESS = "正式员工入职满一年后每年有99天年假吗？请核对原文。"
CORRECTED = "正式员工入职满一年后每年享有5天带薪年假。[来源1]"

BLOCKED = (
    ("window_end_faked_by_next_quantity", "公司规定的99天不对5天内入职的员工公开。[来源1]"),
    ("space_is_not_a_predicate_end", "公司规定的99天不对 第三方公开。[来源1]"),
    ("tab_is_not_a_predicate_end", "正式员工每年休99天有误\t工补贴。[来源1]"),
    ("fact_before_via_zhiyu", "年假带薪至于奖金无法确定。"),
    ("fact_after_via_ling", "奖金无法确定另年假为带薪。"),
    ("negated_refusal_predicate", "不是无法确定年假是带薪的。"),
    ("short_fact_before", "员工有年假至于奖金无法确定。"),
    ("short_fact_after", "奖金无法确定员工有年假。"),
    ("juxtaposed_fact", "年假带薪奖金无法确定。"),
)

DELIVERABLE = (
    ("refutation_bu_dui", "你说的99天不对，原文写的是5天。[来源1]"),
    ("refutation_space_before_punctuation", "你说的99天不对 ，原文写的是5天。[来源1]"),
    ("refutation_you_wu", "你说的99天有误，原文写的是5天。[来源1]"),
    ("correction_then_endorsed_latter", "不是99天。5天才对。[来源1]"),
    ("pure_refusal_topic_after", "无法确定奖金金额。"),
    ("pure_refusal_topic_before", "奖金金额无法确定。"),
    ("pure_refusal_two_topics", "根据现有资料无法确定奖金金额，也无法确定补贴标准。"),
    ("cited_mixed_zhiyu", "年假带薪至于奖金无法确定。[来源1]"),
)


class RefutationBoundaryTests(unittest.TestCase):
    """Where the refutation ends is a fact about the whole answer."""

    def test_the_next_quantity_must_not_fake_a_sentence_end(self):
        """The pair differs only in whether a later quantity truncates the window.

        Both sentences say the rule is not disclosed to someone; neither denies
        the 99. The first used to pass purely because `5天` cut the checked
        window short.
        """
        for answer_text in (
            "公司规定的99天不对5天内入职的员工公开。[来源1]",
            "公司规定的99天不对第三方公开。[来源1]",
            "公司规定的99天不对5天内的员工公开。[来源1]",
        ):
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_whitespace_alone_does_not_end_a_predicate(self):
        for answer_text in (
            "公司规定的99天不对 第三方公开。[来源1]",
            "正式员工每年休99天有误\t工补贴。[来源1]",
            "正式员工每年休99天有误 工补贴。[来源1]",
        ):
            with self.subTest(answer=repr(answer_text)):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_whitespace_before_real_punctuation_is_still_an_ending(self):
        """The mirror image: a space *before* a comma does not break a refutation."""
        for answer_text in (
            "你说的99天不对，原文写的是5天。[来源1]",
            "你说的99天不对 ，原文写的是5天。[来源1]",
            "你说的99天不对\t，原文写的是5天。[来源1]",
        ):
            with self.subTest(answer=repr(answer_text)):
                self.assertEqual(ungrounded_quantities(answer_text, LEAVE, GUESS), [])

    def test_a_real_end_of_text_is_an_ending(self):
        for answer_text in ("你说的99天不对", "你说的99天不对[来源1]", "你说的99天不对。[来源1]"):
            with self.subTest(answer=answer_text):
                self.assertEqual(ungrounded_quantities(answer_text, LEAVE, GUESS), [])

    def test_the_pair_holds_for_other_guessed_numerals(self):
        for guess in ("99", "88", "30"):
            question = f"正式员工入职满一年后每年有{guess}天年假吗？请核对原文。"
            with self.subTest(guess=guess):
                self.assertEqual(
                    ungrounded_quantities(
                        f"公司规定的{guess}天不对5天内入职的员工公开。[来源1]", LEAVE, question
                    ),
                    [(guess, "天")],
                )
                self.assertEqual(
                    ungrounded_quantities(
                        f"你说的{guess}天不对，原文写的是5天。[来源1]", LEAVE, question
                    ),
                    [],
                )

    def test_the_earlier_refutation_rules_still_hold(self):
        """Rework 6 and 7 fixes are unaffected."""
        for answer_text in (
            "正式员工每年休99天没有问题。[来源1]",
            "正式员工每年休99天有误工补贴。[来源1]",
            "每年99天不对外公开。[来源1]",
        ):
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )


class RefusalTopicRecognitionTests(unittest.TestCase):
    """A topic has to be recognised as one, not merely be short and unobjectionable."""

    def test_a_fact_is_not_a_topic_whichever_side_it_sits_on(self):
        for text in (
            "年假带薪至于奖金无法确定。",
            "奖金无法确定另年假为带薪。",
            "员工有年假至于奖金无法确定。",
            "奖金无法确定员工有年假。",
            "年假带薪奖金无法确定。",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_pure_refusal(text))
                ok, reason = validate_answer(text, LEAVE, GUESS)
                self.assertFalse(ok)
                self.assertEqual(reason, "no_citation")

    def test_a_negated_refusal_predicate_is_not_a_refusal(self):
        text = "不是无法确定年假是带薪的。"
        self.assertFalse(is_pure_refusal(text))
        ok, reason = validate_answer(text, LEAVE, GUESS)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_citation")

    def test_the_recognised_refusal_forms_stay_exempt(self):
        for text in (
            "根据现有资料无法确定。",
            "奖金金额无法确定。",
            "无法确定奖金金额。",
            "年假天数无法确定。",
            "暂时无法确定奖金金额。",
            "原文中未提及奖金金额。",
            "资料不足，无法确定。",
            "资料中没有相关记录，无法确定。",
            "根据资料无法回答这个问题。",
            "根据现有资料无法确定奖金金额，也无法确定补贴标准。",
            "根据现有资料无法确定奖金金额；也无法确定补贴标准。",
            "无法确定报销上限和审批流程。",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_pure_refusal(text))
                ok, _reason = validate_answer(text, LEAVE, GUESS)
                self.assertTrue(ok)

    def test_the_same_texts_with_a_citation_are_deliverable(self):
        for text in (
            "年假带薪至于奖金无法确定。",
            "奖金无法确定员工有年假。",
            "年假带薪奖金无法确定。",
            "奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。",
        ):
            with self.subTest(text=text):
                self.assertFalse(validate_answer(text, LEAVE, GUESS)[0])
                self.assertTrue(validate_answer(text + "[来源1]", LEAVE, GUESS)[0])

    def test_the_earlier_mixed_refusal_cases_still_need_a_citation(self):
        """Rework 6 and 7 fixes are unaffected."""
        for text in (
            "奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。",
            "正式员工入职满一年后每年享有5天带薪年假而奖金金额无法确定。",
            "正式员工享有带薪年假这点已确定只是奖金金额无法确定。",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_pure_refusal(text))

    def test_the_quantity_check_still_runs_on_mixed_refusals(self):
        """The rework-4 fix stays: a refusal phrase is not a fact certificate."""
        ok, reason = validate_answer(
            "根据现有资料无法确定奖金金额。正式员工每年享有99天带薪年假。[来源1]",
            LEAVE, GUESS,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "ungrounded_quantity")


class Reply:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": json.dumps({"answer": self.text}, ensure_ascii=False)}}


class Stream:
    def __init__(self, text: str, chunk_size: int = 4):
        payload = json.dumps({"answer": text}, ensure_ascii=False)
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


class UnconsumedMeaningApiTests(unittest.TestCase):
    """Both real interfaces, with the model's HTTP responses scripted."""

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
                [(chunks[0], 1.0)] if chunks else []
            ),
        ).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        with api.state_lock:
            api.chunks.clear()
            api.conversation_locks.clear()
        api.storage = self.original_storage
        self.temp_directory.cleanup()

    def payload(self, session_id):
        return {
            "question": GUESS,
            "session_id": session_id,
            "client_id": CLIENT_A,
            "mode": "orchestrated",
        }

    @staticmethod
    def _sse(response):
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

    def both(self, session, first, recheck):
        with patch.object(
            rag.requests, "post", side_effect=[Reply(first), Reply(recheck)]
        ) as plain_post:
            plain = self.client.post("/api/chat", json=self.payload(f"{session}-p"))
        with patch.object(
            rag.requests, "post", side_effect=[Stream(first), Reply(recheck)]
        ) as stream_post:
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload(f"{session}-s")
            )
        shown, terminal = self._sse(streamed)
        return {
            "plain": plain.json()["answer"],
            # Raw delta join, never stripped before comparison.
            "shown": shown,
            "terminal": terminal,
            "plain_calls": plain_post.call_count,
            "stream_calls": stream_post.call_count,
            "stored_plain": self._last(f"{session}-p"),
            "stored_stream": self._last(f"{session}-s"),
        }

    @staticmethod
    def _last(session):
        messages = api.storage.get_messages(session, CLIENT_A)
        return messages[-1]["content"] if messages else None

    def assert_agreed(self, outcome, expected, *, calls):
        """Both interfaces must match `expected`, not merely match each other."""
        self.assertEqual(outcome["plain"], expected)
        self.assertEqual(outcome["shown"], expected)
        self.assertEqual(outcome["terminal"], "done")
        self.assertEqual(outcome["stored_plain"], expected)
        self.assertEqual(outcome["stored_stream"], expected)
        self.assertEqual(outcome["plain_calls"], outcome["stream_calls"])
        self.assertEqual(outcome["stream_calls"], calls)
        self.assertLessEqual(outcome["stream_calls"], 2)

    def test_a_recheck_that_corrects_the_answer_is_delivered_by_both(self):
        for index, (label, wrong) in enumerate(BLOCKED):
            with self.subTest(case=label):
                self.assert_agreed(
                    self.both(f"u1-{index}", wrong, CORRECTED), CORRECTED, calls=2
                )

    def test_two_wrong_attempts_end_in_the_shared_refusal(self):
        for index, (label, wrong) in enumerate(BLOCKED):
            with self.subTest(case=label):
                outcome = self.both(f"u2-{index}", wrong, wrong)
                self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)
                self.assertNotIn("99", outcome["shown"])
                self.assertNotIn("带薪", outcome["shown"])
                self.assertNotEqual(outcome["stored_plain"], wrong)
                self.assertNotEqual(outcome["stored_stream"], wrong)

    def test_the_positives_are_delivered_unchanged_by_both(self):
        for index, (label, body) in enumerate(DELIVERABLE):
            with self.subTest(case=label):
                calls = 2 if rag.is_refusal(body) else 1
                self.assert_agreed(self.both(f"u3-{index}", body, body), body, calls=calls)


if __name__ == "__main__":
    unittest.main()
