"""M10 rework 6: what a trailing denial denies, and what counts as pure refusal.

Two defects, one of them mine. Rework 5 added a branch letting a denial that
*follows* a numeral excuse it, and checked only that the gap between them was
empty. An empty gap proves adjacency, not aboutness: in `99天没有问题` the
denial governs the problem and the sentence affirms the 99 days. That branch was
a regression - the fourth-round candidate refused those same inputs.

The second is older. `is_pure_refusal()` split on full stops and asked each
piece to contain a refusal substring, so a comma, a semicolon or a `但` put an
uncited factual claim back through the citation exemption.

The pairs below are the point: same numeral with a different denied object,
same clauses joined by different punctuation, the fact before or after the
refusal. A rule keyed on vocabulary or adjacency splits those pairs; one keyed
on what is being denied does not.
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
    ("post_denial_of_problem", "正式员工每年休99天没有问题。[来源1]"),
    ("post_denial_of_restriction", "正式员工每年休99天没有额外限制。[来源1]"),
    ("mixed_refusal_comma",
     "根据现有资料无法确定奖金金额，但正式员工入职满一年后每年享有5天带薪年假。"),
    ("mixed_refusal_semicolon",
     "根据现有资料无法确定奖金金额；正式员工入职满一年后每年享有5天带薪年假。"),
    ("mixed_refusal_reversed",
     "正式员工入职满一年后每年享有5天带薪年假，但根据现有资料无法确定奖金金额。"),
)

DELIVERABLE = (
    ("supported_permission", "正式员工入职满一年后每年休5天没有问题。[来源1]"),
    ("actual_post_denial", "你说的99天并不正确，原文写的是5天。[来源1]"),
    ("comma_only_refusals", "根据现有资料无法确定奖金金额，也无法确定补贴标准。"),
    ("cited_mixed_comma",
     "根据现有资料无法确定奖金金额，但正式员工入职满一年后每年享有5天带薪年假。[来源1]"),
    ("supported_hypothesis_form", "假设按99天计算，也需要核对原文。[来源1]"),
)


class TrailingDenialObjectTests(unittest.TestCase):
    """A denial after the numeral must be denying *the numeral's claim*."""

    def test_denying_something_else_does_not_excuse_the_numeral(self):
        """Same numeral, same adjacency, different denied object."""
        for denied in ("问题", "额外限制", "障碍", "任何门槛"):
            answer_text = f"正式员工每年休99天没有{denied}。[来源1]"
            with self.subTest(denied=denied):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_denying_the_correctness_of_the_claim_does_excuse_it(self):
        for refutation in ("并不正确", "不对", "有误", "这个说法并不准确", "并不属实"):
            answer_text = f"你说的99天{refutation}，原文写的是5天。[来源1]"
            with self.subTest(refutation=refutation):
                self.assertEqual(ungrounded_quantities(answer_text, LEAVE, GUESS), [])

    def test_background_around_the_numeral_changes_nothing(self):
        self.assertEqual(
            ungrounded_quantities(
                "按公司现行制度，正式员工每年休99天没有问题。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )
        self.assertEqual(
            ungrounded_quantities(
                "经核对原文，你说的99天并不正确，原文写的是5天。[来源1]", LEAVE, GUESS
            ),
            [],
        )

    def test_the_pair_holds_for_other_guessed_numerals(self):
        for guess in ("99", "88", "30", "120"):
            question = f"正式员工入职满一年后每年有{guess}天年假吗？请核对原文。"
            with self.subTest(guess=guess):
                self.assertEqual(
                    ungrounded_quantities(
                        f"正式员工每年休{guess}天没有问题。[来源1]", LEAVE, question
                    ),
                    [(guess, "天")],
                )
                self.assertEqual(
                    ungrounded_quantities(
                        f"你说的{guess}天并不正确，原文写的是5天。[来源1]", LEAVE, question
                    ),
                    [],
                )

    def test_the_grounded_quantity_in_the_same_frame_is_fine(self):
        """`休5天没有问题` is the same sentence with an evidenced number."""
        self.assertEqual(
            ungrounded_quantities(
                "正式员工入职满一年后每年休5天没有问题。[来源1]", LEAVE, GUESS
            ),
            [],
        )

    def test_a_denial_that_is_not_a_refutation_is_not_read_as_one(self):
        """`不对外公开` contains `不对` and refutes nothing."""
        self.assertEqual(
            ungrounded_quantities("每年99天不对外公开。[来源1]", LEAVE, GUESS),
            [("99", "天")],
        )


class PureRefusalScopeTests(unittest.TestCase):
    """Only text that declines throughout may skip the citation requirement."""

    def test_a_fact_joined_by_any_punctuation_breaks_purity(self):
        fact = "正式员工入职满一年后每年享有5天带薪年假"
        for joiner in ("，但", "；", "。", "，而", "，不过"):
            text = f"根据现有资料无法确定奖金金额{joiner}{fact}。"
            with self.subTest(joiner=joiner):
                self.assertFalse(is_pure_refusal(text))
                ok, reason = validate_answer(text, LEAVE, GUESS)
                self.assertFalse(ok)
                self.assertEqual(reason, "no_citation")

    def test_the_order_of_fact_and_refusal_does_not_matter(self):
        text = "正式员工入职满一年后每年享有5天带薪年假，但根据现有资料无法确定奖金金额。"
        self.assertFalse(is_pure_refusal(text))
        ok, reason = validate_answer(text, LEAVE, GUESS)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_citation")

    def test_comma_joined_refusals_stay_pure(self):
        for text in (
            "根据现有资料无法确定。",
            "根据现有资料无法确定奖金金额。",
            "根据现有资料无法确定奖金金额，也无法确定补贴标准。",
            "根据现有资料无法确定奖金金额；也无法确定补贴标准。",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_pure_refusal(text))
                ok, _reason = validate_answer(text, LEAVE, GUESS)
                self.assertTrue(ok)

    def test_a_quantity_asserted_before_the_refusal_breaks_purity(self):
        """Stating a figure and then declining is not declining throughout."""
        self.assertFalse(is_pure_refusal("每年5天年假，具体天数无法确定。"))

    def test_the_same_mixed_answer_with_a_citation_is_deliverable(self):
        text = "根据现有资料无法确定奖金金额，但正式员工入职满一年后每年享有5天带薪年假。"
        self.assertFalse(validate_answer(text, LEAVE, GUESS)[0])
        self.assertTrue(validate_answer(text + "[来源1]", LEAVE, GUESS)[0])

    def test_the_quantity_check_still_runs_on_mixed_refusals(self):
        """The rework-4 fix stays: a refusal phrase is not a fact certificate."""
        ok, reason = validate_answer(
            "根据现有资料无法确定奖金金额，但正式员工每年享有99天带薪年假。[来源1]",
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


class PredicateScopeApiTests(unittest.TestCase):
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
                    self.both(f"p1-{index}", wrong, CORRECTED), CORRECTED, calls=2
                )

    def test_two_wrong_attempts_end_in_the_shared_refusal(self):
        for index, (label, wrong) in enumerate(BLOCKED):
            with self.subTest(case=label):
                outcome = self.both(f"p2-{index}", wrong, wrong)
                self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)
                # Not a single character of the wrong body was streamed first.
                self.assertNotIn("99", outcome["shown"])
                self.assertNotIn("带薪年假", outcome["shown"])
                self.assertNotEqual(outcome["stored_plain"], wrong)
                self.assertNotEqual(outcome["stored_stream"], wrong)

    def test_the_positives_are_delivered_unchanged_by_both(self):
        for index, (label, body) in enumerate(DELIVERABLE):
            with self.subTest(case=label):
                # A deliverable non-refusal costs one call; anything carrying a
                # refusal phrase still triggers the one bounded recheck.
                calls = 2 if rag.is_refusal(body) else 1
                self.assert_agreed(self.both(f"p3-{index}", body, body), body, calls=calls)


if __name__ == "__main__":
    unittest.main()
