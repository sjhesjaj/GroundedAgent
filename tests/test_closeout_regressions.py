"""Closeout regressions: a merged query is not proof of full clause coverage.

No model calls. API tests must be run in an isolated source copy, like the rest
of the API suite, because importing api initializes source-relative SQLite.
"""
import json
import unittest
from unittest.mock import patch

import rag
from rag import Chunk
from tests import test_final_rework as api_support


class MergedCoverageTests(unittest.TestCase):
    def setUp(self):
        self.docs = [Chunk(text=t, source="rules.md", index=i) for i,t in enumerate(
            ("甲的规定", "乙的规定", "丙的规定", "丁的规定"), 1)]
        self.guard = patch.object(rag.requests, "post", side_effect=AssertionError("unexpected model call"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def _merged_fast(self, individual):
        def rank(q, chunks):
            return individual if q == "乙" else [(self.docs[0], 10.0), (self.docs[1], 1.0)]
        expected=[(self.docs[1], 1.0)]
        with patch.object(rag,"bm25_rank",side_effect=rank), patch.object(
            rag,"retrieve_with_rerank",return_value=expected
        ) as semantic:
            result=rag.retrieve_fast("甲？乙？丙？丁？",self.docs,top_k=2)
        self.assertEqual(result,expected)
        semantic.assert_called_once()
        self.assertEqual(semantic.call_args.kwargs['top_k'],2)

    def test_strong_merged_score_does_not_hide_a_weak_clause(self):
        self._merged_fast([(self.docs[1],1.0),(self.docs[0],0.9)])

    def test_strong_uncovered_clause_still_requires_its_own_evidence(self):
        self._merged_fast([(self.docs[1],10.0),(self.docs[0],1.0)])

    def test_all_clauses_supported_by_one_chunk_stay_on_fast_path(self):
        with patch.object(rag,"bm25_rank",return_value=[(self.docs[0],10.0),(self.docs[1],1.0)]), patch.object(
            rag,"retrieve_with_rerank",side_effect=AssertionError("unnecessary semantic retrieval")
        ):
            result=rag.retrieve_fast("甲？乙？丙？丁？",self.docs,top_k=1)
        self.assertEqual([c.index for c,_ in result],[1])

    def test_shared_evidence_is_not_padded_with_unselected_topics(self):
        response=api_support.Reply("unused")
        response.json=lambda:{"message":{"content":json.dumps({"selections":[1,1]})}}
        with patch.object(rag.requests,"post",return_value=response):
            result=rag.select_for_subquestions(["甲","乙"],[(c,1.0) for c in self.docs])
        self.assertEqual([c.index for c,_ in result],[1])

    def test_one_merged_group_can_contribute_more_than_one_piece(self):
        recall=[(c,float(c.index)) for c in self.docs]
        selected=[(c,1.0/c.index) for c in self.docs]
        for k in (1,3,4):
            with self.subTest(top_k=k), patch.object(rag,"embed_many",side_effect=lambda q:[[0.0]]*len(q)) as embed, patch.object(
                rag,"hybrid_retrieve",return_value=recall
            ) as retrieve, patch.object(rag,"select_for_subquestions",return_value=selected):
                result=rag.retrieve_with_rerank("甲？乙？丙？丁？",self.docs,top_k=k)
            self.assertLessEqual(len(embed.call_args.args[0]),3)
            self.assertLessEqual(retrieve.call_count,3)
            self.assertEqual(len(result),k)
            self.assertIn(4,[c.index for c,_ in result])
            self.assertEqual(len({c.index for c,_ in result}),k)

    def test_budget_preserves_unique_clause_before_redundant_stronger_evidence(self):
        # A and B both answer the first clause; only C answers the second.
        # A+C fits the budget, so A+B would be avoidable coverage loss.
        response=api_support.Reply("unused")
        response.json=lambda:{"message":{"content":json.dumps({"selections":[
            {"clause":1,"ids":[1,2]}, {"clause":2,"ids":[3]}]})}}
        recall=list(zip(self.docs[:3],(0.9,0.8,0.3)))
        with patch.object(rag.requests,"post",return_value=response) as model, patch.object(
            rag,"embed_many",return_value=[[0.0],[0.0]]
        ), patch.object(rag,"hybrid_retrieve",return_value=recall):
            result=rag.retrieve_with_rerank("甲？乙？",self.docs,top_k=2)
        self.assertEqual([c.index for c,_ in result],[1,3])
        model.assert_called_once()

    def test_merged_clauses_keep_separate_ownership_in_one_selection_call(self):
        response=api_support.Reply("unused")
        response.json=lambda:{"message":{"content":json.dumps({"selections":[
            {"clause":1,"ids":[1]}, {"clause":2,"ids":[2]},
            {"clause":3,"ids":[3]}, {"clause":4,"ids":[4]}]})}}
        with patch.object(rag.requests,"post",return_value=response) as model:
            result=rag.select_for_subquestions(["甲；乙","丙","丁"],[(c,1) for c in self.docs])
        self.assertEqual(result.coverage,{1:{1},2:{2},3:{3},4:{4}})
        model.assert_called_once()

    def test_fast_budget_prioritises_multiple_original_clauses(self):
        def rank(q,chunks):
            index=0 if q in ("甲","乙","甲；乙") else 1
            return [(self.docs[index],10.0-index),(self.docs[2],0.1)]
        with patch.object(rag,"bm25_rank",side_effect=rank), patch.object(
            rag,"retrieve_with_rerank",side_effect=AssertionError("unnecessary semantic retrieval")
        ):
            result=rag.retrieve_fast("甲？乙？丙？丁？戊？",self.docs,top_k=1)
        # Source 2 covers three original clauses despite its lower BM25 score.
        self.assertEqual([c.index for c,_ in result],[2])


class RecheckDeliveryTests(unittest.TestCase):
    # Reuse the existing setup/helpers only, without inheriting and duplicating
    # its test methods. These exercise real routes, both endpoints and SQLite.
    setUp=api_support.BindingApiTests.setUp
    tearDown=api_support.BindingApiTests.tearDown
    both=api_support.BindingApiTests.both
    _sse=staticmethod(api_support.BindingApiTests._sse)
    assert_agreed=api_support.BindingApiTests.assert_agreed
    QUESTION="请引用原文说明年假额度。"
    GOOD="根据现有资料无法确定奖金金额。正式员工每年享有5天带薪年假。[来源1]"
    CORRECT="正式员工每年享有5天带薪年假。[来源1]"

    def test_invalid_quantity_retry_preserves_grounded_first_answer(self):
        bad="正式员工每年享有99天带薪年假。[来源1]"
        self.assert_agreed(self.both(self.QUESTION,"quantity",self.GOOD,bad),self.GOOD,calls=2)

    def test_invalid_identifier_retry_preserves_grounded_first_answer(self):
        with api_support.v1_api.state_lock:
            api_support.v1_api.chunks[0]=Chunk(text="正式员工每年享有5天带薪年假。适用资料编号RULE-Q42。",source="rules.md",index=1)
        bad="正式员工每年享有5天带薪年假，适用资料编号RULE-Q43。[来源1]"
        self.assert_agreed(self.both(self.QUESTION,"identifier",self.GOOD,bad),self.GOOD,calls=2)

    def test_invalid_citation_retry_preserves_grounded_first_answer(self):
        bad="正式员工每年享有5天带薪年假。[来源99]"
        self.assert_agreed(self.both(self.QUESTION,"citation",self.GOOD,bad),self.GOOD,calls=2)

    def test_two_invalid_answers_still_refuse(self):
        bad="正式员工每年享有99天带薪年假。[来源1]"
        self.assert_agreed(self.both(self.QUESTION,"invalid",bad,bad),rag.UNGROUNDED_ANSWER_MESSAGE,calls=2)

    def test_valid_correcting_retry_is_still_used(self):
        bad="正式员工每年享有99天带薪年假。[来源1]"
        self.assert_agreed(self.both(self.QUESTION,"correct",bad,self.CORRECT),self.CORRECT,calls=2)


if __name__=="__main__":
    unittest.main()
