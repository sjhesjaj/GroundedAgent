"""M10 boundary-message tests: which fixed answer a System request earns.

Three outcomes are possible once the planner has chosen the System channel and
found no single SKU, and they say different things to the caller:

- **"give me a SKU"** - the question is about stock, which V1 does serve; the
  parameter is missing.
- **"one SKU per request"** - several usable SKUs arrived; the limit is the
  product's, not the caller's mistake.
- **"only stock queries"** - the question is about something V1 deliberately
  does not open.

Telling a stock question the third of these is the failure this suite guards:
it reports an in-scope request as out of scope, and sends the caller looking
for a capability rather than adding a SKU.

None of these paths may call the answer model; every message is fixed text.
Sentences are fresh, and checked against the frozen datasets below.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_orchestration import (
    MESSAGE_MULTIPLE_SKU,
    MESSAGE_NO_SKU,
    MESSAGE_SYSTEM_LIMITED,
    prepare,
)
from orchestration.planner import Route, plan_request
from rag import Chunk

REPO_ROOT = Path(__file__).resolve().parent.parent

CHUNKS = [
    Chunk(
        text="## 请假制度\n正式员工入职满一年后，每年享有 5 天带薪年假。",
        source="rules.md",
        index=1,
    )
]

# Stock questions with no SKU. Some never say `库存`, which is the point.
MISSING_SKU = (
    "仓库那边货还够不够",
    "帮我看一眼现在的存量",
    "手上还有多少现货",
    "备货还够不够",
    "查一下库存",
)

# Several usable SKUs in one request.
SEVERAL_SKUS = (
    "SKU-A100、SKU_B200 两个料号的数量我都要",
    "把 sku c300 和 SKU-A100 的存量一起报给我",
    "对比一下 SKU-A100 跟 Sku-C300 现在各有多少",
)

# Capabilities V1 deliberately does not open.
NOT_OPENED = (
    "ord-4102 现在什么状态",
    "apr-7788 的审批走到哪一步了",
    "帮我看下 subject-002 名下有几笔订单",
    "我的工单现在什么状态",
)


class MissingSkuTests(unittest.TestCase):
    def test_a_stock_question_without_a_sku_asks_for_one(self):
        for question in MISSING_SKU:
            with self.subTest(question=question):
                prepared = prepare(question, CHUNKS)
                self.assertEqual(prepared.route, Route.SYSTEM_ONLY.value)
                self.assertEqual(prepared.fixed_answer, MESSAGE_NO_SKU)

    def test_a_stock_question_is_never_told_the_capability_is_closed(self):
        for question in MISSING_SKU:
            with self.subTest(question=question):
                self.assertNotEqual(
                    prepare(question, CHUNKS).fixed_answer, MESSAGE_SYSTEM_LIMITED
                )


class SeveralSkusTests(unittest.TestCase):
    def test_several_usable_skus_report_the_one_per_request_limit(self):
        for question in SEVERAL_SKUS:
            with self.subTest(question=question):
                prepared = prepare(question, CHUNKS)
                self.assertEqual(prepared.fixed_answer, MESSAGE_MULTIPLE_SKU)
                # They supplied SKUs; saying "give me one" would be false.
                self.assertNotEqual(prepared.fixed_answer, MESSAGE_NO_SKU)


class ClosedCapabilityTests(unittest.TestCase):
    def test_an_unopened_capability_says_so(self):
        for question in NOT_OPENED:
            with self.subTest(question=question):
                prepared = prepare(question, CHUNKS)
                self.assertEqual(prepared.route, Route.SYSTEM_ONLY.value)
                self.assertEqual(prepared.fixed_answer, MESSAGE_SYSTEM_LIMITED)

    def test_it_is_not_disguised_as_a_missing_parameter(self):
        for question in NOT_OPENED:
            with self.subTest(question=question):
                self.assertNotEqual(
                    prepare(question, CHUNKS).fixed_answer, MESSAGE_NO_SKU
                )


class NoModelOnFixedAnswerTests(unittest.TestCase):
    def test_no_boundary_message_reaches_the_answer_model(self):
        for question in MISSING_SKU + SEVERAL_SKUS + NOT_OPENED:
            with self.subTest(question=question):
                prepared = prepare(question, CHUNKS)
                self.assertFalse(prepared.needs_generation)
                self.assertEqual(prepared.sources, [])
                self.assertEqual(prepared.results_for_answer, [])

    def test_every_boundary_question_still_routes_through_system(self):
        for question in MISSING_SKU + SEVERAL_SKUS + NOT_OPENED:
            with self.subTest(question=question):
                self.assertTrue(plan_request(question).signals.needs_system)


class PolicyQuestionsAreUnaffectedTests(unittest.TestCase):
    """Widening the stock vocabulary must not pull policy questions in.

    `_is_inventory_question` is only consulted after the System channel has
    already been chosen, so a returns-policy question never reaches it - and
    must still not reach it.
    """

    STOCK_WORDS_IN_A_POLICY_QUESTION = (
        "退货办法是怎么规定的",
        "补货流程的规定发我",
        "缺货预警的管理制度写了什么",
        "现货管理办法有哪些要求",
    )

    def test_a_policy_question_containing_stock_words_stays_on_document(self):
        for question in self.STOCK_WORDS_IN_A_POLICY_QUESTION:
            with self.subTest(question=question):
                plan = plan_request(question)
                self.assertFalse(plan.signals.needs_system)
                self.assertTrue(plan.signals.needs_document)
                self.assertEqual(plan.route, Route.DOCUMENT_ONLY)

    def test_such_a_question_produces_no_boundary_message(self):
        for question in self.STOCK_WORDS_IN_A_POLICY_QUESTION:
            with self.subTest(question=question):
                with patch(
                    "orchestration.document_adapter.retrieve_fast",
                    return_value=[(CHUNKS[0], 1.0)],
                ) as retrieve:
                    prepared = prepare(question, CHUNKS)
                retrieve.assert_called_once()
                self.assertNotIn(
                    prepared.fixed_answer,
                    {MESSAGE_NO_SKU, MESSAGE_MULTIPLE_SKU, MESSAGE_SYSTEM_LIMITED},
                )


class VerbatimReuseTests(unittest.TestCase):
    DATASETS = (
        "eval_answerability_blind_v2.json",
        "eval_answerability_dev.json",
        "eval_answerability_holdout.json",
        "eval_answerability_validation_v1.json",
        "eval_orchestrated_routes_blind_v2.json",
        "eval_orchestrated_routes_dev.json",
        "eval_orchestrated_routes_holdout.json",
        "eval_orchestrated_routes_validation_v1.json",
    )

    def test_no_dataset_question_is_reused_verbatim(self):
        known: set[str] = set()
        for name in self.DATASETS:
            path = REPO_ROOT / name
            if not path.exists():  # pragma: no cover - dataset is optional here
                continue
            for case in json.loads(path.read_text(encoding="utf-8")):
                known.add(case["question"].strip())

        local = (
            list(MISSING_SKU)
            + list(SEVERAL_SKUS)
            + list(NOT_OPENED)
            + list(PolicyQuestionsAreUnaffectedTests.STOCK_WORDS_IN_A_POLICY_QUESTION)
        )
        for question in local:
            with self.subTest(question=question):
                self.assertNotIn(question.strip(), known)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
