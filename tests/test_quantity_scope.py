"""M10 rework 3, package A: complete numerals, and what modifies which number.

Two failures motivate this file, and they are opposites:

1. a numeral this module cannot convert was being *truncated* into one it
   could - `一万两千件` read as `2000 件`, which is worse than not reading it at
   all, because the leftover looks grounded;
2. a modifier anywhere in the neighbourhood excused every number near it, so
   `不是5天而是99天` passed for the same reason `不是99天而是5天` did.

The tests are therefore written as *pairs that differ in one thing only* -
the numeral, the connective, the punctuation, or the position of the negation -
because that is the only way to show the judgement follows the language rather
than a keyword that happens to be present.
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
    parse_numeral,
    quantities,
    quantity_mentions,
    ungrounded_quantities,
    validate_answer,
)
from storage import SQLiteStorage

CLIENT_A = "client-a"

LEAVE_CHUNK = Chunk(
    text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假。",
    source="sample_company_rules.md",
    index=1,
)
STOCK_234 = Chunk(text="## 库存\n仓库当前库存为 234 件。", source="sample_company_rules.md", index=2)
STOCK_34 = Chunk(text="## 库存\n仓库当前库存为 34 件。", source="sample_company_rules.md", index=2)
STOCK_2000 = Chunk(text="## 库存\n仓库当前库存为 2000 件。", source="sample_company_rules.md", index=2)

LEAVE = [(LEAVE_CHUNK, 3.5)]
GUESS = "正式员工入职满一年后，每年有 99 天年假吗？请核对原文"


class CompleteNumeralTests(unittest.TestCase):
    """A numeral is read whole or not at all."""

    def test_supported_forms_convert(self):
        for token, expected in (
            ("42", "42"), ("2.5", "2.5"),
            ("四十二", "42"), ("十五", "15"), ("三十", "30"),
            ("二百三十四", "234"), ("一百零五", "105"), ("两千", "2000"),
            ("三百五十", "350"), ("零", "0"),
        ):
            with self.subTest(token=token):
                self.assertEqual(parse_numeral(token), expected)

    def test_unsupported_forms_are_refused_not_approximated(self):
        """None means "I cannot read this", which is a usable answer.

        Returning a number for any of these would be inventing one.
        """
        for token in ("一万两千", "二点五", "十万", "五十万", "一亿",
                      "二〇二五", "五六", "十百", "3.5.1"):
            with self.subTest(token=token):
                self.assertIsNone(parse_numeral(token))

    def test_an_unreadable_numeral_is_never_truncated_to_a_readable_suffix(self):
        """The regression: the numeral must be captured whole before conversion.

        A character class that stopped at `千` matched only the tail of
        `一万两千件`, so an invented figure arrived wearing the evidence's own
        number. Both halves are asserted: the extent that was matched, and the
        fact that it did not become `2000`.
        """
        mentions = quantity_mentions("仓库当前库存为一万两千件。")
        self.assertEqual([m.raw for m in mentions], ["一万两千"])
        self.assertEqual([m.value for m in mentions], [None])
        self.assertEqual(quantities("仓库当前库存为一万两千件。"), {("?一万两千", "件")})

        decimal = quantity_mentions("每年享有二点五天带薪年假。")
        self.assertEqual([m.raw for m in decimal], ["二点五"])
        self.assertEqual(quantities("每年享有二点五天带薪年假。"), {("?二点五", "天")})

    def test_mixed_digit_and_chinese_numerals_stay_one_numeral(self):
        self.assertEqual(quantities("库存为1万2千件。"), {("?1万2千", "件")})

    def test_an_unreadable_numeral_matches_only_an_identical_one(self):
        """It is not grounded by a *value*, but the same words still match.

        The evidence saying `一万两千件` does support an answer that says
        `一万两千件`; what it must never support is `2000 件`, and vice versa.
        """
        source = [(Chunk(text="库存为一万两千件。", source="s.md", index=1), 3.5)]
        self.assertEqual(ungrounded_quantities("库存为一万两千件。[来源1]", source, "库存多少"), [])
        self.assertEqual(
            ungrounded_quantities("库存为2000件。[来源1]", source, "库存多少"),
            [("2000", "件")],
        )

    def test_positions_are_recorded_for_every_occurrence(self):
        mentions = quantity_mentions("先休5天，再休5天")
        self.assertEqual(len(mentions), 2)
        self.assertNotEqual(mentions[0].start, mentions[1].start)


class NumeralDeliveryMatrixTests(unittest.TestCase):
    """The A1 matrix, end to end through `validate_answer`."""

    CASES = (
        ([(STOCK_234, 3.5)], "仓库当前库存为二百三十四件。[来源1]", True),
        ([(STOCK_34, 3.5)], "仓库当前库存为二百三十四件。[来源1]", False),
        ([(STOCK_2000, 3.5)], "仓库当前库存为两千件。[来源1]", True),
        ([(STOCK_2000, 3.5)], "仓库当前库存为一万两千件。[来源1]", False),
    )

    def test_matrix(self):
        for results, answer_text, deliverable in self.CASES:
            with self.subTest(answer=answer_text, evidence=results[0][0].text):
                ok, reason = validate_answer(answer_text, results, "仓库现在有多少件库存")
                self.assertEqual(ok, deliverable)
                if not ok:
                    self.assertEqual(reason, "ungrounded_quantity")

    def test_an_unsupported_decimal_does_not_borrow_the_evidence_number(self):
        ok, reason = validate_answer("每年享有二点五天带薪年假。[来源1]", LEAVE, "年假有多少天")
        self.assertFalse(ok)
        self.assertEqual(reason, "ungrounded_quantity")


class ModifierScopeTests(unittest.TestCase):
    """Which number a `不是` or an `如果` actually reaches."""

    # (answer, deliverable, why)
    MATRIX = (
        ("年假不是99天而是5天。[来源1]", True, "99 被否定，5 有依据"),
        ("年假不是5天而是99天。[来源1]", False, "同样的词，位置相反：99 是断言"),
        ("正式员工每年享有99天带薪年假且可以先休5天。[来源1]", False, "别的断言里的 5 天不替 99 免责"),
        ("正式员工每年享有99天带薪年假且没有特殊限制。[来源1]", False, "无关否定不替 99 免责"),
        ("并不是所有人都有99天年假但正式员工每年确实有99天。[来源1]", False, "前否后肯，后一次仍是断言"),
        ("如果入职满一年则每年可享受99天带薪年假。[来源1]", False, "资格条件不把 99 变成假设"),
        ("如果按你说的99天计算，资料并未这样规定；原文写的是5天。[来源1]", True, "明确复述提问者的猜测"),
    )

    def test_matrix(self):
        for answer_text, deliverable, why in self.MATRIX:
            with self.subTest(answer=answer_text, why=why):
                flagged = ungrounded_quantities(answer_text, LEAVE, GUESS)
                self.assertEqual(not flagged, deliverable, msg=f"{why}: {flagged}")

    def test_punctuation_is_not_what_makes_a_second_claim_safe(self):
        """The same two claims, with and without a comma, judge the same.

        The previous implementation split on punctuation only, so inserting or
        removing a comma silently changed the verdict.
        """
        for answer_text in (
            "正式员工每年享有99天带薪年假，没有特殊限制。[来源1]",
            "正式员工每年享有99天带薪年假且没有特殊限制。[来源1]",
            "正式员工每年享有99天带薪年假。没有特殊限制。[来源1]",
            "正式员工每年享有99天带薪年假但没有特殊限制。[来源1]",
        ):
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_the_connective_may_change_without_changing_the_verdict(self):
        for connective in ("且", "并且", "但", "但是", "不过", "然而", "同时", "，"):
            with self.subTest(connective=connective):
                answer_text = f"正式员工每年享有99天带薪年假{connective}可以先休5天。[来源1]"
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_the_negation_reaches_forward_only(self):
        """`不是` before the number excuses it; after the number it does not."""
        self.assertEqual(
            ungrounded_quantities("不是99天。[来源1]", LEAVE, GUESS), []
        )
        self.assertEqual(
            ungrounded_quantities("每年99天，这不是我编的。[来源1]", LEAVE, GUESS),
            [("99", "天")],
        )

    def test_a_negation_after_the_number_in_the_same_assertion_does_not_excuse_it(self):
        """Position inside one assertion, with no punctuation to fall back on.

        This is the case that separates "look before the number" from "look
        anywhere in this segment": there is no comma and no connective here, so
        splitting cannot help. `没有` belongs to `没有特殊限制`; it says nothing
        about the 99, which is still asserted as fact.
        """
        self.assertEqual(
            ungrounded_quantities(
                "正式员工每年享有99天带薪年假没有特殊限制。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )
        # The mirror image: the denial in front, same words, same segment.
        self.assertEqual(
            ungrounded_quantities("没有99天这一说。[来源1]", LEAVE, GUESS), []
        )

    def test_swapping_the_numerals_swaps_the_verdict(self):
        """One substitution, opposite outcomes - the sentence frame is identical."""
        frame = "年假不是{a}天而是{b}天。[来源1]"
        self.assertEqual(ungrounded_quantities(frame.format(a=99, b=5), LEAVE, GUESS), [])
        self.assertEqual(
            ungrounded_quantities(frame.format(a=5, b=99), LEAVE, GUESS), [("99", "天")]
        )

    def test_a_repeated_numeral_is_judged_at_each_occurrence(self):
        """No set-dedup before the judgement: occurrence two is its own claim."""
        self.assertEqual(
            ungrounded_quantities(
                "并不是所有人都有99天年假但正式员工每年确实有99天。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )
        # ...and when *every* occurrence is denied, nothing is flagged.
        self.assertEqual(
            ungrounded_quantities(
                "资料并非99天，也不是99天那么多；写的是5天。[来源1]", LEAVE, GUESS
            ),
            [],
        )

    def test_a_number_the_caller_never_mentioned_is_not_excused_by_a_denial(self):
        """The exemption exists for the caller's own guess, not for any number.

        Writing `不是` in front of an invented figure must not launder it.
        """
        self.assertEqual(
            ungrounded_quantities("不是77天。[来源1]", LEAVE, GUESS), [("77", "天")]
        )

    def test_a_grounded_answer_needs_no_hedging_at_all(self):
        self.assertEqual(
            ungrounded_quantities("正式员工每年享有5天带薪年假。[来源1]", LEAVE, GUESS), []
        )


class QuantityApiEvidenceTests(unittest.TestCase):
    """The three counter-example families, through both real interfaces.

    Deterministic: the Ollama HTTP responses are scripted. Everything else -
    routing, generation handling, the bounded recheck, delivery, the SSE
    protocol and SQLite - is the product code.
    """

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

    class Reply:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": json.dumps({"answer": self.text}, ensure_ascii=False)}}

    class Stream:
        def __init__(self, text, chunk_size=3):
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

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=False):
            assert decode_unicode
            return iter(self.lines)

    # (label, first answer, recheck answer, expected delivered body)
    CASES = (
        ("wrong correction direction",
         "年假不是5天而是99天。[来源1]",
         "正式员工每年享有 5 天带薪年假。[来源1]",
         "正式员工每年享有 5 天带薪年假。[来源1]"),
        ("unrelated negation in the same sentence",
         "正式员工每年享有99天带薪年假且没有特殊限制。[来源1]",
         "正式员工每年享有99天带薪年假且没有特殊限制。[来源1]",
         UNGROUNDED_ANSWER_MESSAGE),
        ("the same numeral denied then asserted",
         "并不是所有人都有99天年假但正式员工每年确实有99天。[来源1]",
         "并不是所有人都有99天年假但正式员工每年确实有99天。[来源1]",
         UNGROUNDED_ANSWER_MESSAGE),
        ("an unreadable numeral is not delivered",
         "每年享有二点五天带薪年假。[来源1]",
         "每年享有二点五天带薪年假。[来源1]",
         UNGROUNDED_ANSWER_MESSAGE),
    )

    def test_both_interfaces_deliver_and_store_the_same_verdict(self):
        question = "正式员工入职满一年后，每年有 99 天年假吗？请核对原文"
        for index, (label, first, recheck, expected) in enumerate(self.CASES):
            with self.subTest(case=label):
                plain_session, stream_session = f"q-p-{index}", f"q-s-{index}"
                with patch.object(
                    rag.requests, "post",
                    side_effect=[self.Reply(first), self.Reply(recheck)],
                ) as plain_post:
                    plain = self.client.post(
                        "/api/chat", json=self.payload(question, plain_session)
                    )
                with patch.object(
                    rag.requests, "post",
                    side_effect=[self.Stream(first), self.Reply(recheck)],
                ) as stream_post:
                    streamed = self.client.post(
                        "/api/chat/stream", json=self.payload(question, stream_session)
                    )

                shown, terminal = self._sse(streamed)
                self.assertEqual(plain.json()["answer"], expected)
                # The raw delta join, deliberately not stripped: stripping here
                # is what hid an interface difference in the previous round.
                self.assertEqual(shown, expected)
                self.assertEqual(terminal, "done")
                self.assertNotIn("99", shown.replace(expected, ""))
                self.assertEqual(plain_post.call_count, stream_post.call_count)
                self.assertLessEqual(stream_post.call_count, 2)

                stored_plain = api.storage.get_messages(plain_session, CLIENT_A)
                stored_stream = api.storage.get_messages(stream_session, CLIENT_A)
                self.assertEqual(stored_plain[-1]["content"], expected)
                self.assertEqual(stored_stream[-1]["content"], expected)

    def test_a_correct_chinese_numeral_is_still_delivered_by_both(self):
        """The positive case: fixing truncation must not refuse everything."""
        question = "年假有多少天"
        body = "正式员工每年享有五天带薪年假。[来源1]"
        with patch.object(rag.requests, "post", side_effect=[self.Reply(body)]) as plain_post:
            plain = self.client.post("/api/chat", json=self.payload(question, "q-ok-p"))
        with patch.object(rag.requests, "post", side_effect=[self.Stream(body)]) as stream_post:
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload(question, "q-ok-s")
            )

        shown, terminal = self._sse(streamed)
        self.assertEqual(plain.json()["answer"], body)
        self.assertEqual(shown, body)
        self.assertEqual(terminal, "done")
        self.assertEqual(plain_post.call_count, 1)
        self.assertEqual(stream_post.call_count, 1)
        self.assertEqual(
            api.storage.get_messages("q-ok-s", CLIENT_A)[-1]["content"], body
        )


if __name__ == "__main__":
    unittest.main()
