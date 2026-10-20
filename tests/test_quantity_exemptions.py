"""M10 rework 4: which number a modifier reaches, and refusals that assert.

Two independent ways an unsupported number was still being delivered:

1. The exemption was granted because a cue appeared *somewhere* in the prefix,
   with nothing tying it to this numeral. `没有特殊限制的正式员工每年享有99天`
   was excused by a `没有` whose object is the restrictions.
2. `validate_answer()` returned early on `is_refusal()`, so a text that opened
   with a refusal phrase and then asserted a false number skipped the quantity
   check outright. Tightening (1) cannot reach (2); they are separate paths and
   they get separate tests.

The variants matter more than the headline cases. Each family below changes one
thing - the numeral, the conditional connective, what is being negated, what is
being attributed, whether a quotation is later endorsed - so a verdict that
tracks a keyword rather than a relation shows up as an inconsistency.
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
from rag import UNGROUNDED_ANSWER_MESSAGE, Chunk, ungrounded_quantities, validate_answer
from storage import SQLiteStorage

CLIENT_A = "client-a"

LEAVE_CHUNK = Chunk(
    text="正式员工入职满一年后，每年享有5天带薪年假。",
    source="sample_company_rules.md",
    index=1,
)
LEAVE = [(LEAVE_CHUNK, 1.0)]
GUESS = "正式员工入职满一年后每年有99天年假吗？请核对原文。"
CORRECT = "正式员工每年享有5天带薪年假。[来源1]"


class ExemptionScopeTests(unittest.TestCase):
    """A cue excuses a number only when it actually reaches that number."""

    BLOCKED = (
        ("没有特殊限制的正式员工每年享有99天带薪年假。[来源1]",
         "`没有` 否定的是特殊限制"),
        ("如果入职满一年就每年享有99天带薪年假。[来源1]",
         "`如果` 管的是资格条件，99 是条件成立后的断言"),
        ("按你提供的入职日期计算每年享有99天带薪年假。[来源1]",
         "`按你` 引用的是入职日期"),
        ("你说的99天确实是公司规定的年假额度。[来源1]",
         "引用之后紧跟背书，等于自己也这么断言"),
    )

    DELIVERABLE = (
        ("不是99天而是5天带薪年假。[来源1]", "明确纠正"),
        ("如果按你说的99天计算，结论仍需核对原文。[来源1]", "明确设为待核实假设"),
        ("没有99天这一说。[来源1]", "直接否定"),
        ("不是每年99天。[来源1]", "中间只隔着限定数值本身的轻modifier"),
        ("不是每年有99天。[来源1]", "同上"),
    )

    def test_a_cue_whose_object_is_something_else_does_not_excuse(self):
        for answer_text, why in self.BLOCKED:
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS),
                    [("99", "天")],
                    msg=why,
                )

    def test_the_legitimate_forms_are_kept(self):
        for answer_text, why in self.DELIVERABLE:
            with self.subTest(answer=answer_text):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [], msg=why
                )

    # -- one thing changed at a time ------------------------------------

    def test_substituting_the_numeral_does_not_change_the_verdict(self):
        """The rule follows the relation, not the digits."""
        for guess in ("99", "88", "30"):
            question = f"正式员工入职满一年后每年有{guess}天年假吗？请核对原文。"
            with self.subTest(guess=guess):
                self.assertEqual(
                    ungrounded_quantities(
                        f"没有特殊限制的正式员工每年享有{guess}天带薪年假。[来源1]",
                        LEAVE, question,
                    ),
                    [(guess, "天")],
                )
                self.assertEqual(
                    ungrounded_quantities(
                        f"不是{guess}天而是5天带薪年假。[来源1]", LEAVE, question
                    ),
                    [],
                )

    def test_conditional_connectives_are_interchangeable(self):
        """`如果…就`, `若…便`, `假如…那么`: the 99 is asserted either way."""
        for opener, joiner in (("如果", "就"), ("若", "便"), ("假如", "那么"),
                               ("倘若", "就"), ("要是", "那么")):
            answer_text = f"{opener}入职满一年{joiner}每年享有99天带薪年假。[来源1]"
            with self.subTest(connective=f"{opener}…{joiner}"):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_changing_what_is_negated_does_not_grant_the_exemption(self):
        for negated in ("特殊限制", "额外条件", "其他规定", "附加要求"):
            answer_text = f"没有{negated}的正式员工每年享有99天带薪年假。[来源1]"
            with self.subTest(negated=negated):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_changing_what_is_attributed_does_not_grant_the_exemption(self):
        for attributed in ("提供的入职日期", "说的部门口径", "提到的岗位序列"):
            answer_text = f"按你{attributed}计算每年享有99天带薪年假。[来源1]"
            with self.subTest(attributed=attributed):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_a_quotation_that_is_later_endorsed_is_an_assertion(self):
        for endorsement in ("确实是公司规定的额度", "没错", "正是年假额度",
                            "的确如此", "属实"):
            answer_text = f"你说的99天{endorsement}。[来源1]"
            with self.subTest(endorsement=endorsement):
                self.assertEqual(
                    ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
                )

    def test_a_denial_that_is_later_endorsed_loses_the_exemption(self):
        """The endorsement rule still bites when the cue is a real denial.

        Rework 5 demoted attribution to a light modifier, so the cases this rule
        used to catch (`你说的99天确实…`) are now blocked before it is reached.
        It still does the work here: the sentence opens by denying the number
        and then affirms it, which is an assertion whatever the opening said.
        The near-identical control below - same denial, no endorsement - passes.
        """
        self.assertEqual(
            ungrounded_quantities(
                "原文没有提到99天，但这个数确实没错。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )
        self.assertEqual(
            ungrounded_quantities("原文没有提到99天，只写了5天。[来源1]", LEAVE, GUESS),
            [],
        )

    def test_an_endorsement_across_a_clause_still_endorses(self):
        self.assertEqual(
            ungrounded_quantities("按你说的99天，属实。[来源1]", LEAVE, GUESS),
            [("99", "天")],
        )

    def test_an_endorsement_belongs_to_the_number_it_follows(self):
        """`不是99天。5天才对。` endorses the 5. The 99 stays denied.

        Without this bound, widening the endorsement search would refuse a
        perfectly good correction.
        """
        self.assertEqual(
            ungrounded_quantities("不是99天。5天才对。[来源1]", LEAVE, GUESS), []
        )

    def test_a_negated_endorsement_is_not_an_endorsement(self):
        self.assertEqual(
            ungrounded_quantities(
                "你说的99天并不正确，原文写的是5天。[来源1]", LEAVE, GUESS
            ),
            [],
        )

    def test_denying_that_the_source_mentions_the_number_is_a_denial(self):
        """`没有提到99天` / `未提及99天`: the verb's object *is* the numeral.

        `未提` was already a denial cue, so blocking these was self-contradiction
        rather than caution.
        """
        for answer_text in (
            "原文没有提到99天，只写了5天。[来源1]",
            "资料未提及99天，写的是5天。[来源1]",
            "资料并未规定99天，实际是5天。[来源1]",
            "不存在99天的年假，原文是5天。[来源1]",
            "并不是你说的99天，而是5天。[来源1]",
        ):
            with self.subTest(answer=answer_text):
                self.assertEqual(ungrounded_quantities(answer_text, LEAVE, GUESS), [])

    def test_a_bare_preposition_before_the_numeral_no_longer_blocks(self):
        """Was a known conservative refusal in rework 4; now handled.

        `按` in `假设按99天计算` is the same preposition as in `按你说的99天`,
        just without an attributed source, and it takes the numeral itself as
        its object. Recording it as a permanent limitation would have been
        recording an inconsistency.
        """
        self.assertEqual(
            ungrounded_quantities("假设按99天计算，也需要核对原文。[来源1]", LEAVE, GUESS),
            [],
        )

    def test_the_remaining_conservative_refusal_is_recorded_not_forgotten(self):
        """One denial this rule still cannot recognise, pinned so it stays visible.

        In `不是公司规定的99天`, `公司规定的` sits between the cue and the
        numeral and could equally be the cue's own object; nothing here can tell
        that it is not. It is refused rather than delivered - the safe direction,
        and the bounded recheck still gets one attempt at a clearer phrasing -
        but it is a false refusal and this test says so out loud. Admitting a
        general "attributive ending in 的" would widen the exemption in exactly
        the direction that has already failed acceptance twice, so the cost is
        recorded instead of paid.
        """
        self.assertEqual(
            ungrounded_quantities(
                "不是公司规定的99天，原文写的是5天。[来源1]", LEAVE, GUESS
            ),
            [("99", "天")],
        )


class RefusalDoesNotSkipFactsTests(unittest.TestCase):
    """A refusal phrase is not a certificate for the rest of the sentence."""

    def test_a_pure_refusal_is_still_deliverable_without_a_citation(self):
        ok, reason = validate_answer(UNGROUNDED_ANSWER_MESSAGE, LEAVE, GUESS)
        self.assertTrue(ok)
        self.assertEqual(reason, "refusal")

    def test_a_refusal_carrying_a_false_number_is_not_deliverable(self):
        answer_text = "根据现有资料无法确定奖金金额。正式员工每年享有99天带薪年假。[来源1]"
        # The quantity check already saw it; the short-circuit was what let it
        # through, so both halves are asserted.
        self.assertEqual(
            ungrounded_quantities(answer_text, LEAVE, GUESS), [("99", "天")]
        )
        ok, reason = validate_answer(answer_text, LEAVE, GUESS)
        self.assertFalse(ok)
        self.assertEqual(reason, "ungrounded_quantity")

    def test_a_refusal_about_one_thing_plus_a_grounded_fact_is_fine(self):
        """Refusing one sub-question while answering another is legitimate."""
        ok, _reason = validate_answer(
            "根据现有资料无法确定奖金金额。正式员工每年享有5天带薪年假。[来源1]",
            LEAVE, GUESS,
        )
        self.assertTrue(ok)

    def test_the_refusal_wording_is_not_changed_to_hide_the_problem(self):
        self.assertEqual(UNGROUNDED_ANSWER_MESSAGE, "根据现有资料无法确定。")


class Reply:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": json.dumps({"answer": self.text}, ensure_ascii=False)}}


class Stream:
    def __init__(self, text: str, chunk_size: int = 4, *, done: bool = True):
        payload = json.dumps({"answer": text}, ensure_ascii=False)
        self.lines = [
            json.dumps({"message": {"content": payload[i : i + chunk_size]}})
            for i in range(0, len(payload), chunk_size)
        ]
        if done:
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


class ErrorStream(Stream):
    """A stream that reports a model error partway through."""

    def __init__(self, text: str):
        super().__init__(text, done=False)
        self.lines.append(json.dumps({"error": "model exploded"}))


class QuantityExemptionApiTests(unittest.TestCase):
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
        """Drive both interfaces with the identical two-response script."""
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
            # Raw join, never stripped before comparison.
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

    WRONG_ASSERTIONS = (
        ("denial_of_restrictions", "没有特殊限制的正式员工每年享有99天带薪年假。[来源1]"),
        ("condition_with_jiu", "如果入职满一年就每年享有99天带薪年假。[来源1]"),
        ("attribution_elsewhere", "按你提供的入职日期计算每年享有99天带薪年假。[来源1]"),
        ("endorsed_guess", "你说的99天确实是公司规定的年假额度。[来源1]"),
        ("refusal_plus_false_fact",
         "根据现有资料无法确定奖金金额。正式员工每年享有99天带薪年假。[来源1]"),
    )

    def test_a_recheck_that_corrects_the_assertion_is_delivered_by_both(self):
        """Scenario 1: the bounded recheck still rescues a wrong first answer."""
        for index, (label, wrong) in enumerate(self.WRONG_ASSERTIONS):
            with self.subTest(case=label):
                outcome = self.both(f"r1-{index}", wrong, CORRECT)
                self.assert_agreed(outcome, CORRECT, calls=2)

    def test_two_wrong_attempts_end_in_the_shared_refusal(self):
        """Scenario 2: nothing wrong is sent or stored by either interface."""
        for index, (label, wrong) in enumerate(self.WRONG_ASSERTIONS):
            with self.subTest(case=label):
                outcome = self.both(f"r2-{index}", wrong, wrong)
                self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)
                self.assertNotIn("99", outcome["shown"])
                self.assertNotEqual(outcome["stored_plain"], wrong)
                self.assertNotEqual(outcome["stored_stream"], wrong)

    LEGITIMATE = (
        ("correct_correction", "不是99天而是5天带薪年假。[来源1]", 1),
        ("explicit_hypothesis", "如果按你说的99天计算，结论仍需核对原文。[来源1]", 1),
        # A pure refusal is deliverable, but it still triggers the one bounded
        # recheck, so it costs two calls - unchanged behaviour.
        ("pure_refusal", UNGROUNDED_ANSWER_MESSAGE, 2),
    )

    def test_the_legitimate_answers_are_still_delivered_unchanged(self):
        """Scenario 3: tightening must not collapse everything into a refusal."""
        for index, (label, body, calls) in enumerate(self.LEGITIMATE):
            with self.subTest(case=label):
                outcome = self.both(f"r3-{index}", body, body)
                self.assert_agreed(outcome, body, calls=calls)

    # -- scenario 4: the older protections, unchanged --------------------

    def test_a_missing_done_marker_still_errors_and_stores_nothing(self):
        with patch.object(
            rag.requests, "post", side_effect=[Stream(CORRECT, done=False)]
        ):
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload("r4-nodone")
            )
        _shown, terminal = self._sse(streamed)
        self.assertEqual(terminal, "error")
        self.assertEqual(api.storage.get_messages("r4-nodone", CLIENT_A), [])

    def test_a_midstream_model_error_still_errors_and_stores_nothing(self):
        with patch.object(rag.requests, "post", side_effect=[ErrorStream(CORRECT)]):
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload("r4-midstream")
            )
        _shown, terminal = self._sse(streamed)
        self.assertEqual(terminal, "error")
        self.assertEqual(api.storage.get_messages("r4-midstream", CLIENT_A), [])

    def test_a_transport_failure_still_errors_and_stores_nothing(self):
        def explode(*_args, **_kwargs):
            raise rag.requests.RequestException("connection reset")

        with patch.object(rag.requests, "post", side_effect=explode):
            streamed = self.client.post(
                "/api/chat/stream", json=self.payload("r4-transport")
            )
        _shown, terminal = self._sse(streamed)
        self.assertEqual(terminal, "error")
        self.assertEqual(api.storage.get_messages("r4-transport", CLIENT_A), [])

    def test_an_out_of_range_citation_is_still_refused_by_both(self):
        bad = "正式员工每年享有5天带薪年假。[来源9]"
        outcome = self.both("r4-citation", bad, bad)
        self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)


if __name__ == "__main__":
    unittest.main()
