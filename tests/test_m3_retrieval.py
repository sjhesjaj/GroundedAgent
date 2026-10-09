"""Product retrieval boundaries, with in-memory vectors and no live providers."""

from __future__ import annotations

import tempfile
import unittest
import math
from datetime import datetime
from pathlib import Path
from unittest import mock

from aftersales_service import knowledge_base as kb


NOW = datetime.fromisoformat("2026-12-01T00:00:00+08:00")
LEXICAL_QUERY = "退货运费需要保存凭证"


def document(doc_id, *, start="2026-01-01T00:00:00+08:00", end=None,
             body="退货运费需要保存凭证。"):
    return kb.KnowledgeDocument(
        doc_id=doc_id, title="退货运费说明", doc_type="faq", scope=(),
        effective_from=datetime.fromisoformat(start),
        effective_to=None if end is None else datetime.fromisoformat(end),
        version="1", restates=None, source="test/" + doc_id + ".md",
        sections=(("寄回运费", body),))


class RetrievalBoundaryTests(unittest.TestCase):
    def test_expiry_is_exclusive_and_future_start_is_inclusive_for_dense_candidates(self):
        previous = document("previous", end=NOW.isoformat())
        current = document("current", start=NOW.isoformat())
        future = document("future", start="2026-12-02T00:00:00+08:00")
        provider = mock.Mock()
        provider.embed.side_effect = [[[1.0, 0.0], [0.8, 0.6], [1.0, 0.0]], [[1.0, 0.0]]]
        found = kb.KnowledgeBase((previous, current, future), embedder=provider).search("运费", as_of=NOW)
        self.assertEqual([item.document.doc_id for item in found.passages], ["current"])
        self.assertEqual(found.mode, kb.MODE_HYBRID)

    def test_effective_boundary_uses_absolute_business_time(self):
        current = document("current", start=NOW.isoformat())
        knowledge = kb.KnowledgeBase((current,))
        same_instant = datetime.fromisoformat("2026-11-30T16:00:00+00:00")
        before = datetime.fromisoformat("2026-11-30T15:59:59+00:00")
        self.assertEqual(len(knowledge.search(LEXICAL_QUERY, as_of=same_instant).passages), 1)
        self.assertFalse(knowledge.search(LEXICAL_QUERY, as_of=before).passages)

    def test_no_active_documents_never_contacts_an_embedder(self):
        provider = mock.Mock()
        provider.embed.side_effect = AssertionError("No query embedding is needed")
        previous = document("previous", end=NOW.isoformat())
        found = kb.KnowledgeBase((previous,), embedder=provider).search("运费", as_of=NOW)
        self.assertFalse(found.passages)
        provider.embed.assert_not_called()

    def test_dimension_mismatch_records_bm25_fallback(self):
        provider = mock.Mock()
        provider.embed.side_effect = [[[1.0, 0.0]], [[1.0]]]
        knowledge = kb.KnowledgeBase((document("current"),), embedder=provider)
        result = kb.knowledge_tool_result(knowledge, {"query": LEXICAL_QUERY},
                                          observation_id="observation:1", as_of=NOW)
        self.assertEqual(result.status.value, "ok")
        self.assertEqual(result.trace["retrieval_mode"], kb.MODE_BM25)
        self.assertEqual(result.trace["fallback"], kb.FALLBACK_EMBEDDING_UNAVAILABLE)

    def test_empty_fallback_still_records_mode_and_cause(self):
        provider = mock.Mock()
        provider.embed.side_effect = kb.EmbeddingUnavailable("Unavailable")
        knowledge = kb.KnowledgeBase((document("current"),), embedder=provider)
        result = kb.knowledge_tool_result(knowledge, {"query": "zzzz"},
                                          observation_id="observation:1", as_of=NOW)
        self.assertEqual(result.status.value, "empty")
        self.assertEqual(result.trace["retrieval_mode"], kb.MODE_BM25)
        self.assertEqual(result.trace["fallback"], kb.FALLBACK_EMBEDDING_UNAVAILABLE)

    def test_offline_cache_miss_falls_back_without_network_or_cache_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "absent-cache"
            provider = mock.Mock()
            provider.embed.side_effect = AssertionError("Offline retrieval must not call a provider")
            cache = kb.CachedEmbedder(directory, model=kb.EMBEDDING_MODEL, offline=True, provider=provider)
            knowledge = kb.KnowledgeBase((document("current"),), embedder=cache)
            found = knowledge.search(LEXICAL_QUERY, as_of=NOW)
            self.assertTrue(found.passages)
            self.assertEqual((found.mode, found.fallback), (kb.MODE_BM25, kb.FALLBACK_EMBEDDING_UNAVAILABLE))
            self.assertFalse(directory.exists())
            provider.embed.assert_not_called()

    def test_naive_business_time_is_rejected_before_embedding(self):
        provider = mock.Mock()
        knowledge = kb.KnowledgeBase((document("current"),), embedder=provider)
        with self.assertRaises(ValueError):
            knowledge.search(LEXICAL_QUERY, as_of=NOW.replace(tzinfo=None))
        provider.embed.assert_not_called()


class RelevanceFloorTests(unittest.TestCase):
    def knowledge(self, similarities, *, fail=False):
        provider = mock.Mock()
        if fail:
            provider.embed.side_effect = kb.EmbeddingUnavailable("Unavailable")
        else:
            provider.embed.side_effect = [
                [[score, math.sqrt(1 - score * score)] for score in similarities],
                [[1.0, 0.0]],
            ]
        return kb.KnowledgeBase(tuple(document("doc-" + str(index))
                                      for index in range(len(similarities))), embedder=provider)

    def result(self, knowledge):
        return kb.knowledge_tool_result(knowledge, {"query": LEXICAL_QUERY},
                                        observation_id="observation:1", as_of=NOW)

    def test_hybrid_threshold_is_inclusive_and_filters_each_passage(self):
        knowledge = self.knowledge([0.8, 0.45, 0.449])
        with mock.patch.object(knowledge._bm25, "score", return_value=30):
            result = self.result(knowledge)
        self.assertEqual({e.metadata["doc_id"] for e in result.evidence}, {"doc-0", "doc-1"})
        self.assertEqual(result.trace["relevance_signal"], "cosine")
        self.assertEqual(result.trace["relevance_scope"], "passage")
        self.assertEqual(result.trace["relevance_floor"], 0.45)
        self.assertEqual(result.trace["relevance_filtered_passages"], 1)
        self.assertEqual(result.trace["eligible_passages"], 3)
        self.assertTrue(all(e.confidence is None for e in result.evidence))

    def test_strong_lexical_match_cannot_bypass_the_hybrid_passage_floor(self):
        knowledge = self.knowledge([0.449])
        with mock.patch.object(knowledge._bm25, "score", return_value=30):
            result = self.result(knowledge)
        self.assertEqual(result.status.value, "empty")
        self.assertEqual(result.trace["retrieval_mode"], kb.MODE_HYBRID)
        self.assertIsNone(result.trace["fallback"])
        self.assertEqual(result.trace["relevance_filtered_passages"], 1)

    def test_dense_match_does_not_need_a_lexical_overlap(self):
        knowledge = self.knowledge([0.8])
        with mock.patch.object(knowledge._bm25, "score", return_value=0):
            result = self.result(knowledge)
        self.assertEqual(result.status.value, "ok")
        self.assertEqual(result.trace["relevance_filtered_passages"], 0)

    def test_bm25_query_threshold_is_inclusive_and_retains_weak_positive_passages(self):
        knowledge = self.knowledge([0.0, 0.0, 0.0], fail=True)
        with mock.patch.object(knowledge._bm25, "score", side_effect=[2.0, 1.0, 0.0]):
            result = self.result(knowledge)
        self.assertEqual([e.metadata["doc_id"] for e in result.evidence], ["doc-0", "doc-1"])
        self.assertEqual(result.trace["retrieval_mode"], kb.MODE_BM25)
        self.assertEqual(result.trace["fallback"], kb.FALLBACK_EMBEDDING_UNAVAILABLE)
        self.assertEqual(result.trace["relevance_signal"], "bm25")
        self.assertEqual(result.trace["relevance_scope"], "query")
        self.assertEqual(result.trace["relevance_floor"], 2.0)
        self.assertEqual(result.trace["relevance_top_score"], 2.0)
        self.assertEqual(result.trace["relevance_filtered_passages"], 0)

    def test_bm25_query_below_floor_is_empty_and_records_all_filtered_passages(self):
        knowledge = self.knowledge([0.0, 0.0], fail=True)
        with mock.patch.object(knowledge._bm25, "score", side_effect=[1.999, 1.0]):
            result = self.result(knowledge)
        self.assertEqual(result.status.value, "empty")
        self.assertEqual(result.trace["retrieval_mode"], kb.MODE_BM25)
        self.assertEqual(result.trace["relevance_scope"], "query")
        self.assertEqual(result.trace["relevance_filtered_passages"], 2)

    def test_expired_high_scores_do_not_help_current_query_pass_its_floor(self):
        knowledge = kb.KnowledgeBase((document("expired", end=NOW.isoformat()), document("current")))
        with mock.patch.object(knowledge._bm25, "score", return_value=1.999) as score:
            result = self.result(knowledge)
        self.assertEqual(result.status.value, "empty")
        self.assertEqual(score.call_count, 1)
        self.assertEqual(result.trace["eligible_passages"], 1)

    def test_no_active_documents_have_no_embedding_attempt_or_fallback_reason(self):
        provider = mock.Mock()
        knowledge = kb.KnowledgeBase((document("expired", end=NOW.isoformat()),), embedder=provider)
        result = self.result(knowledge)
        self.assertEqual(result.status.value, "empty")
        self.assertEqual(result.trace["retrieval_mode"], kb.MODE_HYBRID)
        self.assertIsNone(result.trace["fallback"])
        self.assertIsNone(result.trace["relevance_top_score"])
        self.assertEqual(result.trace["eligible_passages"], 0)
        provider.embed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
