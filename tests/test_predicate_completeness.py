"""M10 rework 7: a partial match is not a complete structure.

Two defects, the same mistake in two places.

`_REFUTATION_PATTERN` matched `不对` inside `不对第三方公开` and `有误` inside
`有误工补贴`. Neither denies the number - one says "not disclosed to third
parties", the other says a subsidy exists - but the prefix matched and the
exemption was granted. Adding more excluded objects would not fix that; what
was missing was any check of the *tail* of the match.

`is_pure_refusal()` had the same shape from the other direction: it assumed the
text was a refusal and looked for reasons to reject it (a fact across a full
stop, then a quantity before the refusal word). Reverse judgement always misses
a phrasing - here a fact joined by a bare `而`, and a fact carrying no number at
all. It is now a positive test: every character has to be accounted for by a
refusal clause.

The pairs below differ in one thing only - the tail after a matched refutation,
the side the fact sits on, whether the fact has a number - because that is what
tells a structural rule from a vocabulary one.
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
    ("prefix_not_a_refutation_bu_dui", "公司规定的99天不对第三方公开。[来源1]"),
    ("prefix_not_a_refutation_you_wu", "正式员工每年休99天有误工补贴。[来源1]"),
    ("mixed_refusal_er_fact_after",
     "奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。"),
    ("mixed_refusal_no_number",
     "正式员工享有带薪年假这点已确定只是奖金金额无法确定。"),
    ("mixed_refusal_fact_first",
     "正式员工入职满一年后每年享有5天带薪年假而奖金金额无法确定。"),
)

DELIVERABLE = (
    ("refutation_bu_dui", "你说的99天不对，原文写的是5天。[来源1]"),
    ("refutation_you_wu", "你说的99天有误，原文写的是5天。[来源1]"),
    ("cited_mixed_er",
     "奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。[来源1]"),
    ("comma_only_refusals", "根据现有资料无法确定奖金金额，也无法确定补贴标准。"),
    ("endorsement_names_the_latter", "不是99天。5天才对。[来源1]"),
)


class RefutationCompletenessTests(unittest.TestCase):
    """A refutation must end where a refutation ends."""

    def test_a_matched_prefix_of_a_longer_word_is_not_a_refutation(self):
        """Each pair shares the matched characters and differs in the tail."""
        for answer_text in (
            "公司规定的99天不对第三方公开。[来源1]",
            "公司规定的99天不对外披露。[来源1]",
            "正式员工每年休99天有误工补贴。[来源1]",
            "正式员工每年休99天有误差范围。[来源1]",
        ):
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_a_refutation_that_ends_the_clause_still_excuses(self):
        for refutation in ("不对", "有误", "并不正确", "并不准确", "不属实", "不对吧"):
            answer_text = f"你说的99天{refutation}，原文写的是5天。[来源1]"
            with self.subTest(refutation=refutation):
                self.assertEqual(ungrounded_quantities(answer_text, LEAVE, GUESS), [])

    def test_a_refutation_at_the_very_end_still_excuses(self):
        self.assertEqual(
            ungrounded_quantities("你说的99天不对。[来源1]", LEAVE, GUESS), []
        )

    def test_the_pair_holds_for_other_guessed_numerals(self):
        for guess in ("99", "88", "30"):
            question = f"正式员工入职满一年后每年有{guess}天年假吗？请核对原文。"
            with self.subTest(guess=guess):
                self.assertEqual(
                    ungrounded_quantities(
                        f"公司规定的{guess}天不对第三方公开。[来源1]", LEAVE, question
                    ),
                    [(guess, "天")],
                )
                self.assertEqual(
                    ungrounded_quantities(
                        f"你说的{guess}天不对，原文写的是5天。[来源1]", LEAVE, question
                    ),
                    [],
                )

    def test_the_earlier_trailing_denial_cases_stay_blocked(self):
        """Rework 6's fix is unaffected: `没有问题` denies a problem."""
        for denied in ("问题", "额外限制", "障碍"):
            with self.subTest(denied=denied):
                self.assertEqual(
                    ungrounded_quantities(
                        f"正式员工每年休99天没有{denied}。[来源1]", LEAVE, GUESS
                    ),
                    [("99", "天")],
                )


class PureRefusalCompletenessTests(unittest.TestCase):
    """Exemption from citing is granted positively, not by failing to object."""

    def test_a_fact_on_either_side_of_the_refusal_breaks_purity(self):
        for text in (
            "奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。",
            "正式员工入职满一年后每年享有5天带薪年假而奖金金额无法确定。",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_pure_refusal(text))
                ok, reason = validate_answer(text, LEAVE, GUESS)
                self.assertFalse(ok)
                self.assertEqual(reason, "no_citation")

    def test_a_fact_without_any_number_breaks_purity_too(self):
        """The quantity-position check could not see this one at all."""
        for text in (
            "正式员工享有带薪年假这点已确定只是奖金金额无法确定。",
            "奖金金额无法确定只是正式员工享有带薪年假这点已确定。",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_pure_refusal(text))
                ok, reason = validate_answer(text, LEAVE, GUESS)
                self.assertFalse(ok)
                self.assertEqual(reason, "no_citation")

    def test_the_refusal_forms_that_stay_exempt(self):
        for text in (
            "根据现有资料无法确定。",
            "根据现有资料无法确定奖金金额。",
            "奖金金额无法确定。",
            "暂时无法确定奖金金额。",
            "根据现有资料无法确定奖金金额，也无法确定补贴标准。",
            "根据现有资料无法确定奖金金额；也无法确定补贴标准。",
            "原文中未提及奖金金额。",
            "资料不足，无法确定。",
            "资料中没有相关记录，无法确定。",
            "根据资料无法回答这个问题。",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_pure_refusal(text))
                ok, _reason = validate_answer(text, LEAVE, GUESS)
                self.assertTrue(ok)

    def test_the_same_mixed_answers_with_a_citation_are_deliverable(self):
        for text in (
            "奖金金额无法确定而正式员工入职满一年后每年享有5天带薪年假。",
            "正式员工入职满一年后每年享有5天带薪年假而奖金金额无法确定。",
        ):
            with self.subTest(text=text):
                self.assertFalse(validate_answer(text, LEAVE, GUESS)[0])
                self.assertTrue(validate_answer(text + "[来源1]", LEAVE, GUESS)[0])

    def test_a_topic_may_not_smuggle_a_second_claim(self):
        """A tail short enough to look like a topic, but changing the subject."""
        self.assertFalse(is_pure_refusal("无法确定奖金而年假5天成立。"))

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


class PredicateCompletenessApiTests(unittest.TestCase):
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
                    self.both(f"c1-{index}", wrong, CORRECTED), CORRECTED, calls=2
                )

    def test_two_wrong_attempts_end_in_the_shared_refusal(self):
        for index, (label, wrong) in enumerate(BLOCKED):
            with self.subTest(case=label):
                outcome = self.both(f"c2-{index}", wrong, wrong)
                self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)
                # Not one character of the wrong body was streamed first.
                self.assertNotIn("99", outcome["shown"])
                self.assertNotIn("带薪年假", outcome["shown"])
                self.assertNotEqual(outcome["stored_plain"], wrong)
                self.assertNotEqual(outcome["stored_stream"], wrong)

    def test_the_positives_are_delivered_unchanged_by_both(self):
        for index, (label, body) in enumerate(DELIVERABLE):
            with self.subTest(case=label):
                calls = 2 if rag.is_refusal(body) else 1
                self.assert_agreed(self.both(f"c3-{index}", body, body), body, calls=calls)


if __name__ == "__main__":
    unittest.main()
