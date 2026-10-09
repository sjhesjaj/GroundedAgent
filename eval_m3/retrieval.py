"""Static retrieval Recall@1/3/5 on KB-DEV, for BM25, vector and hybrid (no LLM).

The query is the customer turn's text; the gold is the turn's knowledge_base
gold sections (policy_sources gold is not retrievable from the KB, and a turn
with no knowledge_base gold is not in the denominator). A passage is a hit if
its (doc_id, version, section) is a gold section, so a superseded version is a
miss. Only documents in force at the case's business time are ranked.

Each ranker applies the product's own relevance floor (knowledge_base.py):
    bm25     positive BM25 scores, nothing if the query's top score < 2
    vector   cosine-ranked passages with cosine >= 0.45
    hybrid   RRF of the two lists above, exactly the product's hybrid order
Ranks go to depth 5 although the tool returns 4 passages, so Recall@5 is a
ranking measure, not what the agent can see.

Recall@k per turn = gold sections found in the top k / gold sections; the
summary gives the macro mean over turns and the micro share over sections.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from aftersales_service import knowledge_base as kb

DEPTHS = (1, 3, 5)
RANKERS = ("bm25", "vector", "hybrid")


def rankings(knowledge: kb.KnowledgeBase, query: str, as_of: datetime,
             question_vector: Sequence[float]) -> dict[str, list[int]]:
    candidates = [index for index, passage in enumerate(knowledge.passages) if passage.document.in_force(as_of)]
    terms = kb.tokens(query)
    scored = [(knowledge._bm25.score(terms, index), index) for index in candidates]
    top = max((score for score, _ in scored), default=0.0)
    lexical_all = [index for score, index in sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]
    bm25 = lexical_all if top >= kb.BM25_QUERY_RELEVANCE_FLOOR else []
    vectors = knowledge._passage_vectors()
    similarity = {index: kb._cosine(question_vector, vectors[index]) for index in candidates}
    eligible = {index for index, score in similarity.items() if score >= kb.COSINE_RELEVANCE_FLOOR}
    dense = sorted(eligible, key=lambda index: (-similarity[index], index))
    hybrid = kb.reciprocal_rank_fusion([[index for index in lexical_all if index in eligible], dense])
    return {"bm25": bm25, "vector": dense, "hybrid": hybrid}


def retrieval_recall(cases, knowledge: kb.KnowledgeBase) -> dict:
    turns = [(case, turn) for case in cases for turn in case.turns
             if any(item["source"] == "knowledge_base" for item in turn.labels.get("gold", []))]
    questions = [turn.text for _, turn in turns]
    vectors = knowledge._embedder.embed(questions) if questions else []
    rows = []
    for (case, turn), vector in zip(turns, vectors):
        gold = {(item["doc_id"], item["version"], section) for item in turn.labels["gold"]
                if item["source"] == "knowledge_base" for section in item["sections"]}
        ranked = rankings(knowledge, turn.text, case.virtual_now, vector)
        row = {"case_id": case.case_id, "turn": turn.index, "gold_sections": len(gold), "recall": {}}
        for ranker in RANKERS:
            identities = [(knowledge.passages[index].document.doc_id, knowledge.passages[index].document.version,
                           knowledge.passages[index].section) for index in ranked[ranker][:max(DEPTHS)]]
            row["recall"][ranker] = {}
            for depth in DEPTHS:
                found = {identity for identity in identities[:depth] if identity in gold}
                row["recall"][ranker][str(depth)] = {"found": len(found), "recall": round(len(found) / len(gold), 4)}
        rows.append(row)
    summary = {}
    total = sum(row["gold_sections"] for row in rows)
    for ranker in RANKERS:
        summary[ranker] = {}
        for depth in DEPTHS:
            key = str(depth)
            macro = (sum(row["recall"][ranker][key]["recall"] for row in rows) / len(rows)) if rows else None
            micro = (sum(row["recall"][ranker][key]["found"] for row in rows) / total) if total else None
            summary[ranker]["recall@" + key] = {
                "macro": None if macro is None else round(macro, 4),
                "micro": None if micro is None else round(micro, 4)}
    return {"turns": len(rows), "gold_sections": total, "summary": summary, "rows": rows}
