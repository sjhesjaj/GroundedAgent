"""M10 rework 5: is the sentence asserting this number, and is it a pure refusal?

Rework 4 asked whether a cue *reached* a numeral. That is necessary and not
sufficient: `你说的99天就是公司规定的年假额度` has an attribution reaching the
numeral and still asserts it. Four shapes got through, and they share a cause -
attribution was treated as evidence that the sentence was not asserting, when it
only says where the number came from. Attribution is now a light modifier, not
an exemption, which removes three of the four at once. The fourth is a negation
that is itself negated.

The second defect is unrelated and lives one function away: `is_refusal()`
matches a substring anywhere, so a refusal sentence followed by an uncited claim
skipped the citation check as well.

Every family below changes one thing at a time - the numeral, where the
affirmation sits, how many negations there are, which of two numerals is being
pointed at, whether the mixed answer carries a citation - because a rule that
tracks vocabulary rather than structure fails those pairs inconsistently.
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

# The five that must never be delivered as written.
BLOCKED = (
    ("affirmation_before_the_numeral", "公司规定的年假确实是你说的99天。[来源1]"),
    ("equative_endorsement", "你说的99天就是公司规定的年假额度。[来源1]"),
    ("negated_negation", "正式员工并不是没有99天带薪年假。[来源1]"),
    ("endorsement_names_the_former", "你说的99天和原文的5天相比，前者才对。[来源1]"),
    ("mixed_refusal_without_citation",
     "根据现有资料无法确定奖金金额。正式员工入职满一年后每年享有5天带薪年假。"),
)

# The five that must be delivered unchanged.
DELIVERABLE = (
    ("correct_correction", "不是99天而是5天带薪年假。[来源1]"),
    ("explicit_hypothesis", "如果按你说的99天计算，结论仍需核对原文。[来源1]"),
    ("endorsement_names_the_latter", "不是99天。5天才对。[来源1]"),
    ("pure_refusal", UNGROUNDED_ANSWER_MESSAGE),
    ("mixed_refusal_with_citation",
     "根据现有资料无法确定奖金金额。正式员工入职满一年后每年享有5天带薪年假。[来源1]"),
)


class AssertionBoundaryTests(unittest.TestCase):
    """Whether the proposition affirms the number, as a pure function."""

    def test_the_five_counter_examples_are_not_deliverable(self):
        for label, answer_text in BLOCKED:
            with self.subTest(case=label):
                ok, _reason = validate_answer(answer_text, LEAVE, GUESS)
                self.assertFalse(ok)

    def test_the_five_positives_are_deliverable(self):
        for label, answer_text in DELIVERABLE:
            with self.subTest(case=label):
                ok, _reason = validate_answer(answer_text, LEAVE, GUESS)
                self.assertTrue(ok)

    # -- one thing changed at a time ------------------------------------

    def test_attribution_alone_never_exempts_whatever_the_numeral(self):
        """`你说的N天` says where N came from, not that N is unasserted."""
        for guess in ("99", "88", "30", "120"):
            question = f"正式员工入职满一年后每年有{guess}天年假吗？请核对原文。"
            with self.subTest(guess=guess):
                self.assertEqual(
                    ungrounded_quantities(
                        f"你说的{guess}天就是公司规定的年假额度。[来源1]", LEAVE, question
                    ),
                    [(guess, "天")],
                )
                # ...while a real denial of the same numeral still passes.
                self.assertEqual(
                    ungrounded_quantities(
                        f"不是{guess}天而是5天带薪年假。[来源1]", LEAVE, question
                    ),
                    [],
                )

    def test_an_affirmation_counts_on_either_side_of_the_numeral(self):
        for answer_text in (
            "公司规定的年假确实是你说的99天。[来源1]",   # before
            "你说的99天确实是公司规定的年假额度。[来源1]",  # after
            "确实，公司规定的年假就是99天。[来源1]",
            "你说的99天，的确如此。[来源1]",
        ):
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_one_negation_denies_and_two_affirm(self):
        """`没有99天` denies it; `并不是没有99天` asserts it."""
        self.assertEqual(
            ungrounded_quantities("正式员工没有99天带薪年假。[来源1]", LEAVE, GUESS), []
        )
        for doubled in (
            "正式员工并不是没有99天带薪年假。[来源1]",
            "正式员工不是没有99天带薪年假。[来源1]",
            "并不是不是99天。[来源1]",
        ):
            with self.subTest(answer=doubled):
                self.assertEqual(
                    ungrounded_quantities(doubled, LEAVE, GUESS), [("99", "天")]
                )

    def test_which_of_two_numerals_the_endorsement_names(self):
        """`前者`/`后者` swap with the order, and so does the verdict."""
        self.assertEqual(
            ungrounded_quantities(
                "你说的99天和原文的5天相比，前者才对。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )
        self.assertEqual(
            ungrounded_quantities(
                "原文的5天和你说的99天相比，后者才对。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )
        # The endorsement that names the grounded number leaves the denial alone.
        self.assertEqual(
            ungrounded_quantities("不是99天。5天才对。[来源1]", LEAVE, GUESS), []
        )
        self.assertEqual(
            ungrounded_quantities("5天才对，不是99天。[来源1]", LEAVE, GUESS), []
        )

    def test_a_denial_may_follow_the_numeral_if_nothing_intervenes(self):
        self.assertEqual(
            ungrounded_quantities(
                "你说的99天并不正确，原文写的是5天。[来源1]", LEAVE, GUESS
            ),
            [],
        )
        # ...but `没有` here belongs to `特殊限制`, four characters away.
        self.assertEqual(
            ungrounded_quantities(
                "正式员工每年享有99天带薪年假没有特殊限制。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )


class PureRefusalTests(unittest.TestCase):
    """Only a text that declines throughout is excused from citing."""

    def test_every_sentence_must_decline(self):
        self.assertTrue(is_pure_refusal("根据现有资料无法确定。"))
        self.assertTrue(is_pure_refusal("根据现有资料无法确定奖金金额。"))
        self.assertTrue(
            is_pure_refusal("根据现有资料无法确定奖金金额。也无法确定年假天数。")
        )
        self.assertFalse(
            is_pure_refusal(
                "根据现有资料无法确定奖金金额。正式员工入职满一年后每年享有5天带薪年假。"
            )
        )
        self.assertFalse(is_pure_refusal(""))

    def test_a_pure_refusal_needs_no_citation(self):
        ok, reason = validate_answer(UNGROUNDED_ANSWER_MESSAGE, LEAVE, GUESS)
        self.assertTrue(ok)
        self.assertEqual(reason, "refusal")

    def test_a_mixed_answer_still_needs_one(self):
        mixed = "根据现有资料无法确定奖金金额。正式员工入职满一年后每年享有5天带薪年假。"
        ok, reason = validate_answer(mixed, LEAVE, GUESS)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_citation")
        # The identical text with a citation is deliverable.
        ok, _reason = validate_answer(mixed + "[来源1]", LEAVE, GUESS)
        self.assertTrue(ok)

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


class AssertionBoundaryApiTests(unittest.TestCase):
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
        self.assertEqual(outcome["plain"], expected)
        self.assertEqual(outcome["shown"], expected)
        self.assertEqual(outcome["terminal"], "done")
        self.assertEqual(outcome["stored_plain"], expected)
        self.assertEqual(outcome["stored_stream"], expected)
        self.assertEqual(outcome["plain_calls"], outcome["stream_calls"])
        self.assertEqual(outcome["stream_calls"], calls)
        self.assertLessEqual(outcome["stream_calls"], 2)

    def test_a_recheck_that_corrects_the_assertion_is_delivered_by_both(self):
        for index, (label, wrong) in enumerate(BLOCKED):
            with self.subTest(case=label):
                self.assert_agreed(
                    self.both(f"b1-{index}", wrong, CORRECTED), CORRECTED, calls=2
                )

    def test_two_wrong_attempts_end_in_the_shared_refusal(self):
        for index, (label, wrong) in enumerate(BLOCKED):
            with self.subTest(case=label):
                outcome = self.both(f"b2-{index}", wrong, wrong)
                self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)
                # Nothing of the wrong answer was ever sent, not even a prefix.
                self.assertNotIn("99", outcome["shown"])
                self.assertNotEqual(outcome["stored_plain"], wrong)
                self.assertNotEqual(outcome["stored_stream"], wrong)

    def test_the_positives_are_delivered_unchanged_by_both(self):
        for index, (label, body) in enumerate(DELIVERABLE):
            with self.subTest(case=label):
                # A deliverable first answer costs one call; a refusal still
                # triggers the one bounded recheck, so it costs two.
                calls = 2 if rag.is_refusal(body) else 1
                self.assert_agreed(self.both(f"b3-{index}", body, body), body, calls=calls)


if __name__ == "__main__":
    unittest.main()
