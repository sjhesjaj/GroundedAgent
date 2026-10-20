"""M10 final rework: bracket boundaries, stock intent, answer completeness, ranking.

Four separate defects, each with its own reproduction:

A. `_CLAUSE_PUNCTUATION` listed opening brackets, so `不对（外部员工）公开` looked
   finished at the bracket. The brackets hold the *recipient* of the
   non-disclosure; nothing denies the 99 days.
B. `请给我此时的存货数量` named the stock and reached no System step, and
   `存量准确数` dragged in the document path because `准确` was read as a demand
   for source text rather than for an accurate reading.
C. Real-model failures: a body that only describes the reading it is about to
   do passed as an answer; an answer dropped a digit from a SKU and stayed
   "grounded"; and a question's own premise figure was treated as a claim about
   the source, refusing `answer_v2_002`.
D. Page title/summary matches were added identically to every claim on the page,
   so a common word in a title outranked the claim that stated the fact.
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
from orchestration.planner import Route, plan_request
from orchestration.wiki_adapter import load_wiki_pages, page_from_json, wiki_query
from rag import UNGROUNDED_ANSWER_MESSAGE, Chunk, ungrounded_quantities, validate_answer
from storage import SQLiteStorage

LEAVE_CHUNK = Chunk(text="正式员工入职满一年后，每年享有5天带薪年假。", source="p.md", index=1)
LEAVE = [(LEAVE_CHUNK, 1.0)]
STOCK = [(Chunk(text="SKU-C300 当前库存 7 箱。", source="system", index=1), 1.0)]
GUESS = "正式员工入职满一年后每年有99天年假吗？请核对原文。"


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


class BracketBoundaryTests(unittest.TestCase):
    """An opening bracket is not the end of a predicate."""

    def test_a_bracketed_recipient_does_not_refute_the_number(self):
        for opening, closing in (("（", "）"), ("(", ")"), ("[", "]"), ("【", "】")):
            answer = f"公司规定的99天不对{opening}外部员工{closing}公开。[来源1]"
            with self.subTest(bracket=opening):
                self.assertEqual(ungrounded_quantities(answer, LEAVE, GUESS), [("99", "天")])

    def test_a_citation_after_the_denial_still_ends_it(self):
        self.assertEqual(ungrounded_quantities("你说的99天不对[来源1]。", LEAVE, GUESS), [])

    def test_an_explanatory_parenthesis_after_the_denial_still_ends_it(self):
        """The bracket is an aside, and the predicate does not resume after it."""
        self.assertEqual(
            ungrounded_quantities("你说的99天不对（原文写的是5天）。[来源1]", LEAVE, GUESS), []
        )

    def test_the_predicate_resuming_after_the_bracket_is_what_decides(self):
        """Same bracket, same content - only what follows the close differs."""
        self.assertEqual(
            ungrounded_quantities("你说的99天不对（外部员工）。[来源1]", LEAVE, GUESS), []
        )
        self.assertEqual(
            ungrounded_quantities("你说的99天不对（外部员工）公开。[来源1]", LEAVE, GUESS),
            [("99", "天")],
        )

    def test_an_unclosed_bracket_is_not_an_ending(self):
        self.assertEqual(
            ungrounded_quantities("公司规定的99天不对（外部员工公开。[来源1]", LEAVE, GUESS),
            [("99", "天")],
        )


class StockIntentTests(unittest.TestCase):
    """Naming the stock is a System request; `准确` can describe the reading."""

    def test_a_stock_request_without_an_identifier_reaches_system(self):
        for question in (
            "请给我此时的存货数量，SKU 待会提供。",
            "此刻的存货量是多少，编号稍后给你。",
            "眼下库存还剩多少，我一会儿把货号发你。",
        ):
            with self.subTest(question=question):
                self.assertEqual(plan_request(question).route, Route.SYSTEM_ONLY)

    def test_precision_about_a_stock_reading_does_not_add_the_document_step(self):
        for question in (
            "正在盘库，请报告 SKU-A100 现在的存量准确数。",
            "请给 SKU-A100 的准确库存。",
            "SKU-A100 的存货准确数量是多少。",
        ):
            with self.subTest(question=question):
                self.assertEqual(plan_request(question).route, Route.SYSTEM_ONLY)

    def test_precision_about_a_policy_still_selects_the_document(self):
        """The control: same word, and the clause names no stock."""
        for question in (
            "请引用请假原文，报一个准确天数。",
            "报销上限的准确金额，按原文说。",
        ):
            with self.subTest(question=question):
                self.assertEqual(plan_request(question).route, Route.DOCUMENT_ONLY)

    def test_a_policy_question_phrased_with_now_stays_on_the_document(self):
        """`现在的订单管理制度怎么规定` is a policy question, not a lookup."""
        self.assertNotEqual(plan_request("现在的订单管理制度怎么规定").route, Route.SYSTEM_ONLY)


class AnswerCompletenessTests(unittest.TestCase):
    """A body has to answer, and it has to be about the right entity."""

    PROCESS = (
        "根据问题，需要从[来源1]中查找陪护假的期限。"
        "首先，我需要仔细阅读资料，看是否明确提到了该假期及天数。"
    )

    def test_a_body_that_only_describes_the_reading_is_not_an_answer(self):
        ok, reason = validate_answer(self.PROCESS, LEAVE, "请给出手册中陪护假的期限和引用。")
        self.assertFalse(ok)
        self.assertEqual(reason, "incomplete_process")

    def test_a_genuine_step_description_is_still_delivered(self):
        """`首先` is not a banned word - this answers the question it was asked."""
        ok, _reason = validate_answer(
            "申请流程是：首先提交表单，然后由主管审批。[来源1]", LEAVE, "请假怎么申请？"
        )
        self.assertTrue(ok)

    def test_a_refusal_is_not_mistaken_for_a_process_description(self):
        """Declining is a result, even when the sentence mentions reading.

        Only the citation rule has anything to say about the second phrasing -
        it is a mixed text, so it needs a source. That is rework-8 behaviour and
        is asserted here so the two rules stay distinguishable.
        """
        self.assertFalse(
            rag.describes_only_process("根据现有资料无法确定，需要查阅其它材料。", LEAVE)
        )
        ok, reason = validate_answer("根据现有资料无法确定。", LEAVE, "陪护假几天？")
        self.assertTrue(ok)
        self.assertEqual(reason, "refusal")
        self.assertEqual(
            validate_answer("根据现有资料无法确定，需要查阅其它材料。", LEAVE, "陪护假几天？")[1],
            "no_citation",
        )

    def test_the_models_own_deliberation_is_not_an_answer(self):
        """Observed on a real three-channel question: the facts were correct and
        buried in the draft that produced them.

        `describes_only_process` cannot catch this - the body *does* contain a
        result, which is exactly why that check steps aside when a quantity is
        present. What gives it away is talk about writing the answer.
        """
        body = (
            "根据用户问题，需要从资料中提取以下信息：1. 信息安全概览；2. 库存。"
            "我将逐一检查资料中是否有相关信息。我需要确保回答符合要求，"
            "控制在3句话以内。最终答案：内部资料不得上传至公共网盘。[来源1]"
        )
        self.assertTrue(rag.delivers_deliberation(body))
        ok, reason = validate_answer(body, LEAVE, "信息安全概览")
        self.assertFalse(ok)
        self.assertEqual(reason, "delivered_deliberation")

    def test_an_ordinary_answer_is_not_deliberation(self):
        for body in (
            "员工每周最多申请2天远程办公，须提前一个工作日获得直属主管批准。[来源1]",
            "申请流程是：首先提交表单，然后由主管审批。[来源1]",
            "培训预算每年的上限是2000元。[来源1]",
        ):
            with self.subTest(body=body):
                self.assertFalse(rag.delivers_deliberation(body))

    def test_a_dropped_digit_makes_it_another_entity(self):
        ok, reason = validate_answer(
            "sku-c30 当前库存 7 箱。[来源1]", STOCK, "SKU-C300 还有多少库存？"
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "unsupported_identifier")

    def test_case_and_separator_are_normalised_but_digits_are_not(self):
        for body in ("SKU-C300 当前库存 7 箱。[来源1]", "sku c300 当前库存 7 箱。[来源1]"):
            with self.subTest(body=body):
                self.assertTrue(validate_answer(body, STOCK, "SKU-C300 还有多少库存？")[0])

    def test_a_premise_figure_from_the_question_is_not_a_claim_about_the_source(self):
        """`answer_v2_002`: the caller states 2500, and asks about something else."""
        question = "单笔 2500 元的报销，除了直属主管还需要谁审批？门槛金额是多少"
        evidence = [(
            Chunk(text="单笔金额不超过 2000 元由直属主管审批，超过 2000 元还需部门负责人审批。",
                  source="p.md", index=1),
            1.0,
        )]
        ok, _reason = validate_answer(
            "单笔2500元的报销需要部门负责人审批，门槛金额是2000元。[来源1]", evidence, question
        )
        self.assertTrue(ok)

    def test_a_figure_the_question_asks_us_to_verify_is_still_a_claim(self):
        """The control: `吗`/`核对` makes the number the thing under test."""
        self.assertEqual(
            ungrounded_quantities("正式员工每年享有99天带薪年假。[来源1]", LEAVE, GUESS),
            [("99", "天")],
        )

    def test_a_premise_may_not_be_reused_for_a_different_object(self):
        """The caller's own figure is theirs *in the role they used it in*.

        `我在公司工作了99天` is elapsed tenure. Turning it into `每年享有99天年假`
        makes it the policy's entitlement, which needs evidence. Judging this by
        whether the whole question contained a verification word let both of
        these through - the same number, a different thing.
        """
        tenure = "我在公司工作了99天，请介绍正式员工年假政策。"
        self.assertEqual(
            ungrounded_quantities("正式员工每年享有99天带薪年假。[来源1]", LEAVE, tenure),
            [("99", "天")],
        )
        expense_evidence = [(
            Chunk(text="单笔金额不超过2000元由直属主管审批，超过2000元还需部门负责人审批。",
                  source="p.md", index=1),
            1.0,
        )]
        invoice = "我的报销单金额是2500元，请说明审批规则。"
        self.assertEqual(
            ungrounded_quantities("公司规定报销审批门槛是2500元。[来源1]", expense_evidence, invoice),
            [("2500", "元")],
        )

    def test_the_same_premise_in_the_same_role_is_still_allowed(self):
        expense_evidence = [(
            Chunk(text="单笔金额不超过2000元由直属主管审批，超过2000元还需部门负责人审批。",
                  source="p.md", index=1),
            1.0,
        )]
        for question, answer in (
            ("单笔 2500 元的报销，除了直属主管还需要谁审批？门槛金额是多少",
             "单笔2500元的报销需要部门负责人审批，门槛金额是2000元。[来源1]"),
            # `是否` asks about the approval, not about verifying 2500.
            ("这笔报销费用为2500元，是否需要部门负责人审批？",
             "这笔2500元报销需要部门负责人审批，门槛金额是2000元。[来源1]"),
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    ungrounded_quantities(answer, expense_evidence, question), []
                )


class PremiseRoleBindingTests(unittest.TestCase):
    """Which occurrence in the question corresponds to which in the answer.

    Two earlier attempts each had half the information. Classifying by the whole
    question let `我在公司工作了99天` be reused as an entitlement; looking only at
    words near the answer's occurrence let `我的年假是99天吗` - a claim the caller
    asked us to *check* - come back as `你的年假是99天`.
    """

    EXPENSE = [(
        Chunk(text="单笔金额不超过2000元由直属主管审批，超过2000元还需部门负责人审批。",
              source="p.md", index=1),
        1.0,
    )]

    def test_a_premise_may_not_change_attribute(self):
        """`99天` of tenure is not `99天` of annual leave."""
        self.assertEqual(
            ungrounded_quantities(
                "你的年假是99天。[来源1]", LEAVE, "我在公司工作了99天，请介绍正式员工年假政策。"
            ),
            [("99", "天")],
        )

    def test_a_figure_the_caller_asked_us_to_check_is_not_a_premise(self):
        """Same attribute on both sides, but the question is asking, not telling."""
        self.assertEqual(
            ungrounded_quantities(
                "你的年假是99天。[来源1]", LEAVE, "我的年假是99天吗？请核对原文。"
            ),
            [("99", "天")],
        )

    def test_an_instance_word_does_not_excuse_a_rule_assertion(self):
        self.assertEqual(
            ungrounded_quantities(
                "这笔2500元就是公司统一的报销审批分界线。[来源1]",
                self.EXPENSE, "我的报销单金额是2500元，请说明审批规则。",
            ),
            [("2500", "元")],
        )

    def test_a_verification_cue_in_another_clause_leaves_the_premise_alone(self):
        """`是否` asks about the approval; 2500 is still the caller's own figure."""
        self.assertEqual(
            ungrounded_quantities(
                "这笔2500元报销需要部门负责人审批，门槛金额是2000元。[来源1]",
                self.EXPENSE, "这笔报销费用为2500元，是否需要部门负责人审批？",
            ),
            [],
        )

    def test_the_same_attribute_restated_is_still_allowed(self):
        self.assertEqual(
            ungrounded_quantities(
                "单笔2500元的报销需要部门负责人审批，门槛金额是2000元。[来源1]",
                self.EXPENSE, "单笔 2500 元的报销，除了直属主管还需要谁审批？门槛金额是多少",
            ),
            [],
        )


class BindingApiTests(unittest.TestCase):
    """The binding decision, through both real interfaces.

    Deterministic: only the Ollama HTTP responses are scripted. Routing,
    generation handling, the bounded recheck, delivery, the SSE protocol and
    SQLite are product code.
    """

    EXPENSE_CHUNK = Chunk(
        text="单笔金额不超过2000元由直属主管审批，超过2000元还需部门负责人审批。",
        source="sample_company_rules.md", index=1,
    )
    CORRECTED = "单笔金额不超过2000元由直属主管审批，超过2000元还需部门负责人审批。[来源1]"

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

    def both(self, question, session, first, recheck):
        payload = lambda sid: {
            "question": question, "session_id": sid,
            "client_id": "client-a", "mode": "orchestrated",
        }
        with patch.object(
            rag.requests, "post", side_effect=[Reply(first), Reply(recheck)]
        ) as plain_post:
            plain = self.client.post("/api/chat", json=payload(f"{session}-p"))
        with patch.object(
            rag.requests, "post", side_effect=[Stream(first), Reply(recheck)]
        ) as stream_post:
            streamed = self.client.post("/api/chat/stream", json=payload(f"{session}-s"))
        shown, terminal = self._sse(streamed)
        last = lambda sid: (
            api.storage.get_messages(sid, "client-a")[-1]["content"]
            if api.storage.get_messages(sid, "client-a") else None
        )
        return {
            "plain": plain.json()["answer"], "shown": shown, "terminal": terminal,
            "plain_calls": plain_post.call_count, "stream_calls": stream_post.call_count,
            "stored_plain": last(f"{session}-p"), "stored_stream": last(f"{session}-s"),
        }

    def assert_agreed(self, outcome, expected, *, calls):
        self.assertEqual(outcome["plain"], expected)
        self.assertEqual(outcome["shown"], expected)  # raw delta join, not stripped
        self.assertEqual(outcome["terminal"], "done")
        self.assertEqual(outcome["stored_plain"], expected)
        self.assertEqual(outcome["stored_stream"], expected)
        self.assertEqual(outcome["plain_calls"], outcome["stream_calls"])
        self.assertEqual(outcome["stream_calls"], calls)
        self.assertLessEqual(outcome["stream_calls"], 2)

    def test_a_checked_claim_is_not_delivered_as_a_premise(self):
        question = "我的年假是99天吗？请核对原文。"
        wrong = "你的年假是99天。[来源1]"
        right = "正式员工入职满一年后，每年享有5天带薪年假。[来源1]"
        # Both attempts wrong: the shared refusal, and the body never streamed.
        outcome = self.both(question, "b1", wrong, wrong)
        self.assert_agreed(outcome, UNGROUNDED_ANSWER_MESSAGE, calls=2)
        self.assertNotIn("99", outcome["shown"])
        # A correct recheck still rescues it.
        self.assert_agreed(self.both(question, "b2", wrong, right), right, calls=2)

    def test_the_callers_own_figure_in_the_same_role_is_delivered(self):
        with api.state_lock:
            api.chunks.clear()
            api.chunks.append(self.EXPENSE_CHUNK)
        body = "这笔2500元报销需要部门负责人审批，门槛金额是2000元。[来源1]"
        outcome = self.both("这笔报销费用为2500元，是否需要部门负责人审批？", "b3", body, body)
        self.assert_agreed(outcome, body, calls=1)


class IdentifierSpellingTests(unittest.TestCase):
    """However the identifier is spaced, the digits have to match."""

    QUESTION = "SKU-C300 当前有多少库存？"

    def test_a_space_separated_identifier_is_extracted_at_all(self):
        """The gap was in extraction, not in normalisation.

        The pattern required `-` or `_`, so `sku c30` produced no identifier -
        and "found nothing" was indistinguishable from "nothing was wrong".
        """
        self.assertEqual(
            rag.unsupported_identifiers("sku c30 当前库存7箱。[来源1]", STOCK, self.QUESTION),
            ["sku c30"],
        )

    def test_dropped_added_and_changed_digits_are_all_caught(self):
        for wrong in ("sku c30", "sku-c30", "sku c301", "sku-c3000", "SKU C030"):
            with self.subTest(spelling=wrong):
                self.assertTrue(
                    rag.unsupported_identifiers(
                        f"{wrong} 当前库存7箱。[来源1]", STOCK, self.QUESTION
                    )
                )

    def test_case_and_separator_spellings_of_the_right_id_are_accepted(self):
        for right in ("SKU-C300", "sku-c300", "sku c300", "SKU C300", "skuc300"):
            with self.subTest(spelling=right):
                self.assertEqual(
                    rag.unsupported_identifiers(
                        f"{right} 当前库存7箱。[来源1]", STOCK, self.QUESTION
                    ),
                    [],
                )

    def test_ordinary_words_are_not_read_as_identifiers(self):
        """A hard separator is what makes it an identifier, not letters+digits."""
        self.assertEqual(
            rag.unsupported_identifiers(
                "该标准参考 GB18030 与 ISO 8601。[来源1]", STOCK, self.QUESTION
            ),
            [],
        )

    def test_an_unknown_family_is_still_checked(self):
        """Learning `sku` from the evidence must not exempt every other family.

        `SPU-C300` is written as plainly as an identifier can be; the evidence
        does not contain it, so it names something else.
        """
        self.assertEqual(
            rag.unsupported_identifiers(
                "SPU-C300 当前库存7箱。[来源1]", STOCK, self.QUESTION
            ),
            ["SPU-C300"],
        )

    def test_extra_whitespace_does_not_hide_the_identifier(self):
        self.assertEqual(
            rag.unsupported_identifiers(
                "sku  c30 当前库存7箱。[来源1]", STOCK, self.QUESTION
            ),
            ["sku  c30"],
        )

    def test_a_trailing_character_is_part_of_the_identifier(self):
        """`SKU-C300A` must not be trimmed down to the known `SKU-C300`."""
        self.assertEqual(
            rag.unsupported_identifiers(
                "SKU-C300A 当前库存7箱。[来源1]", STOCK, self.QUESTION
            ),
            ["SKU-C300A"],
        )


class RankingTests(unittest.TestCase):
    """A common word in a title must not outrank the claim stating the fact."""

    # The fixture has to look like a handbook for the point to exist: `员工`
    # appears on most pages, `年假` and `报销` on one each. That distribution is
    # the whole premise - a word that is everywhere is weak evidence.
    _SECTIONS = (
        ("grievance", "员工建议与申诉", "员工可通过人力资源系统提交实名或匿名建议。"),
        # The real corpus wording, so the annual-leave claim actually carries
        # `三年` and `带薪` - a shortened stand-in would not exercise the case.
        ("leave", "请假制度",
         "正式员工入职满一年后，每年享有 5 天带薪年假；工作满三年后增加至 8 天。"),
        ("expense", "费用报销", "报销申请应在费用发生后 30 天内提交。"),
        ("travel", "差旅申请", "员工出差前应在系统中提交目的地、日期和预算。"),
        ("attendance", "考勤与迟到", "员工应通过办公系统完成上下班打卡。"),
        ("training", "培训学习", "员工每年可申请岗位相关的培训预算。"),
        ("device", "设备领用", "员工领用办公设备需在系统登记。"),
        ("handover", "离职交接", "员工离职前应完成工作交接并归还设备。"),
    )

    PAGES = tuple(
        {
            "page_id": f"wiki-{slug}", "title": title, "version": "1.0",
            "summary": f"本页涵盖：{title}。", "aliases": [],
            "claims": [{
                "claim_id": f"wiki-{slug}-c1", "text": text,
                "source": "p.md", "locator": f"section:{title}",
            }],
        }
        for slug, title, text in _SECTIONS
    )

    def pages(self):
        return tuple(page_from_json(page) for page in self.PAGES)

    def test_the_fact_outranks_a_page_titled_with_a_common_word(self):
        top = wiki_query("概览一下正式员工的年假规定。", self.pages(), top_k=3).evidence[0]
        self.assertEqual(top.locator, "section:请假制度")

    def test_a_page_titled_with_the_topic_word_still_wins(self):
        """The control: `报销` is specific, so the title match keeps its weight."""
        top = wiki_query("员工报销的规定", self.pages(), top_k=3).evidence[0]
        self.assertEqual(top.locator, "section:费用报销")

    def test_recall_is_unchanged(self):
        """Reordering only: every page that mentions the term is still eligible."""
        mentioning = sum(
            1 for page in self.PAGES
            if "员工" in page["title"] or "员工" in page["claims"][0]["text"]
        )
        result = wiki_query("员工", self.pages(), top_k=20)
        self.assertEqual(len(result.evidence), mentioning)

    def test_a_summary_restating_the_title_does_not_double_the_title(self):
        """The compiled Wiki's generated summaries are title restatements.

        `本页涵盖：员工建议与申诉。` carries no information the title does not,
        yet it used to add a second helping of `员工` on top of the title's - so
        a page matching one common word outscored the page whose claim matched
        three. Counting each term once, at its best field, removes the extra
        helping without touching what the weights mean.
        """
        top = wiki_query(
            "老员工到岗三年以后，带薪假期额度有什么变化？", self.pages(), top_k=3
        ).evidence[0]
        self.assertEqual(top.locator, "section:请假制度")

    def test_a_term_in_several_fields_counts_once_at_its_best_field(self):
        """Two pages, one matched term, decided by which field it sits in.

        `年假` is in one page's title and in the other's claim. The title match
        must still win - merging is per term, it does not flatten the weights.
        """
        pages = tuple(
            page_from_json(page)
            for page in (
                {
                    "page_id": "wiki-titled", "title": "年假", "version": "1.0",
                    "summary": "本页涵盖：年假。", "aliases": [],
                    "claims": [{
                        "claim_id": "wiki-titled-c1", "text": "请提前在系统提交申请。",
                        "source": "p.md", "locator": "section:年假",
                    }],
                },
                {
                    "page_id": "wiki-body", "title": "考勤与迟到", "version": "1.0",
                    "summary": "本页涵盖：考勤与迟到。", "aliases": [],
                    "claims": [{
                        "claim_id": "wiki-body-c1", "text": "年假的打卡要求另行规定。",
                        "source": "p.md", "locator": "section:考勤与迟到",
                    }],
                },
            )
        )
        self.assertEqual(wiki_query("年假", pages, top_k=2).evidence[0].locator, "section:年假")

    def test_claims_on_one_page_stay_distinguishable(self):
        """Merging the claim into the page maximum destroyed ranking *within* a page.

        `CLAIM_WEIGHT` is the smallest weight, so a term already in the title or
        summary contributed nothing from the claim - and every claim on the page
        scored identically, including one matching no query term at all. First
        place then fell to whichever claim happened to be written first.

        The committed sample collection is used deliberately: on this question
        every matched term also appears in the page's title, alias or summary,
        so the merged rank is genuinely identical for all three claims. Only the
        claim's own weighted match can decide, which is what makes this a test of
        the tie-break rather than of the merge.
        """
        evidence = wiki_query(
            "正式员工和实习生是否都享有带薪年假？", load_wiki_pages(), top_k=3
        ).evidence
        self.assertIn("实习生不享有带薪年假", evidence[0].content)
        # The claim that matches no query term must not lead its page.
        self.assertNotIn("请假应提前在系统提交申请", evidence[0].content)

    def test_a_claim_matching_nothing_ranks_last_on_its_page(self):
        pages = (
            page_from_json({
                "page_id": "wiki-remote", "title": "远程办公", "version": "1.0",
                "summary": "远程办公的规定。", "aliases": [],
                "claims": [
                    {"claim_id": "wiki-remote-c1", "text": "设备遗失应在 2 小时内报告。",
                     "source": "p.md", "locator": "section:远程办公"},
                    {"claim_id": "wiki-remote-c2", "text": "员工每周最多申请 2 天远程办公。",
                     "source": "p.md", "locator": "section:远程办公"},
                ],
            }),
        )
        evidence = wiki_query("每周可以远程办公几天？", pages, top_k=2).evidence
        self.assertIn("每周最多申请 2 天远程办公", evidence[0].content)

    def test_the_published_score_is_still_the_documented_total(self):
        result = wiki_query("费用报销", self.pages(), top_k=1)
        self.assertIsInstance(result.evidence[0].metadata["retrieval_score"], int)


if __name__ == "__main__":
    unittest.main()
