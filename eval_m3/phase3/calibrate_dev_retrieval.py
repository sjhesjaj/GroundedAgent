"""Inspect frozen KB-DEV retrieval signals; no agent, answer model or scorer.

The dataset path is deliberately fixed. This calibration never discovers or
opens another dataset. It may contact local Ollama for missing bge-m3 vectors;
--offline instead uses strictly read-only embedding cache access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from aftersales_service import knowledge_base as kb

DEV_PATH = ROOT / "eval_m3" / "datasets" / "kb-dev.json"


def cache_snapshot() -> dict:
    return {str(path.relative_to(kb.EMBEDDING_CACHE_DIRECTORY)):
            (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in kb.EMBEDDING_CACHE_DIRECTORY.rglob("*.json")}


def inspect(*, offline: bool) -> dict:
    cache_before = cache_snapshot() if offline else None
    cases = json.loads(DEV_PATH.read_text(encoding="utf-8"))
    documents = kb.load_corpus()
    knowledge = kb.KnowledgeBase(documents, embedder=kb.cached_embedder(offline=offline))
    lexical_knowledge = kb.KnowledgeBase(documents)
    vectors = knowledge._passage_vectors()
    questions = [turn["question"] for case in cases for turn in case["turns"]]
    question_vectors = iter(knowledge._embedder.embed(questions))
    rows = []
    for case in cases:
        as_of = datetime.fromisoformat(case["virtual_now"])
        for turn_number, turn in enumerate(case["turns"], 1):
            query = turn["question"]
            question = next(question_vectors)
            gold = {(item["doc_id"], item["version"], section)
                    for item in turn["gold"] if item["source"] == "knowledge_base"
                    for section in item["sections"]}
            terms = kb.tokens(query)
            signals = []
            for index, passage in enumerate(knowledge.passages):
                if not passage.document.in_force(as_of):
                    continue
                identity = (passage.document.doc_id, passage.document.version, passage.section)
                signals.append({"index": index, "doc_id": identity[0], "version": identity[1],
                                "section": identity[2], "gold": identity in gold,
                                "cosine": kb._cosine(question, vectors[index]),
                                "bm25": knowledge._bm25.score(terms, index)})
            lexical = [item["index"] for item in sorted(signals, key=lambda s: (-s["bm25"], s["index"]))
                       if item["bm25"] > 0]
            dense = [item["index"] for item in sorted(signals, key=lambda s: (-s["cosine"], s["index"]))]
            ranking = kb.reciprocal_rank_fusion([lexical, dense])
            signal_by_index = {item["index"]: item for item in signals}
            is_negative = (case["type"] in {"unanswerable", "off_topic", "smalltalk"}
                           or case["injection_kind"] == "system_prompt_exfiltration")
            rows.append({"case_id": case["id"], "turn": turn_number, "type": case["type"],
                         "question": query, "virtual_now": case["virtual_now"],
                         "kb_gold_sections": len(gold), "diagnostic_negative": is_negative,
                         "max_cosine": max((item["cosine"] for item in signals), default=0),
                         "max_bm25": max((item["bm25"] for item in signals), default=0),
                         "gold_signals": [item for item in signals if item["gold"]],
                         "hybrid_top4": [signal_by_index[index] for index in ranking[:4]],
                         "cosine_top8": sorted(signals, key=lambda s: (-s["cosine"], s["index"]))[:8],
                         "bm25_top8": sorted(signals, key=lambda s: (-s["bm25"], s["index"]))[:8],
                         "_signals": signals, "_ranking": ranking})
            kept = {item["index"] for item in signals if item["cosine"] >= 0.45}
            filtered_ranking = kb.reciprocal_rank_fusion([[index for index in lexical if index in kept],
                                                         [index for index in dense if index in kept]])[:4]
            hybrid_actual = knowledge.search(query, as_of=as_of)
            bm25_actual = lexical_knowledge.search(query, as_of=as_of)
            hybrid_actual_indices = [knowledge.passages.index(passage) for passage in hybrid_actual.passages]
            bm25_actual_indices = [lexical_knowledge.passages.index(passage) for passage in bm25_actual.passages]
            bm25_proposed = lexical[:4] if max((item["bm25"] for item in signals), default=0) >= 2 else []
            rows[-1]["runtime_hybrid_matches_candidate"] = hybrid_actual_indices == filtered_ranking
            rows[-1]["runtime_bm25_matches_candidate"] = bm25_actual_indices == bm25_proposed
    candidates = []
    positive = [row for row in rows if row["kb_gold_sections"]]
    negative = [row for row in rows if row["diagnostic_negative"]]
    for signal, thresholds in (("cosine", [0.0, 0.3, 0.35, 0.4, 0.425, 0.45, 0.475, 0.5,
                                           0.525, 0.55, 0.575, 0.6, 0.65, 0.7]),
                               ("bm25", [0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0])):
        for threshold in thresholds:
            rejected_positive = [f'{row["case_id"]}:{row["turn"]}' for row in positive
                                 if row["max_" + signal] < threshold]
            surviving_negative = [f'{row["case_id"]}:{row["turn"]}' for row in negative
                                  if row["max_" + signal] >= threshold]
            removed_gold = [dict(case_id=row["case_id"], turn=row["turn"], doc_id=item["doc_id"],
                                 section=item["section"], score=item[signal])
                            for row in positive for item in row["gold_signals"]
                            if item[signal] < threshold]
            candidates.append({"signal": signal, "threshold": threshold,
                               "query_gate_rejected_positive": rejected_positive,
                               "query_gate_surviving_diagnostic_negative": surviving_negative,
                               "passage_floor_removed_gold_sections": removed_gold})
    baseline = []
    for row in positive:
        by_index = {item["index"]: item for item in row["_signals"]}
        kept = {item["index"] for item in row["_signals"] if item["cosine"] >= 0.45}
        lexical = [item["index"] for item in sorted(row["_signals"], key=lambda s: (-s["bm25"], s["index"]))
                   if item["bm25"] > 0 and item["index"] in kept]
        dense = [item["index"] for item in sorted(row["_signals"], key=lambda s: (-s["cosine"], s["index"]))
                 if item["index"] in kept]
        filtered = kb.reciprocal_rank_fusion([lexical, dense])[:4]
        original_gold = {index for index in row["_ranking"][:4] if by_index[index]["gold"]}
        filtered_gold = {index for index in filtered if by_index[index]["gold"]}
        bm25 = [item["index"] for item in sorted(row["_signals"], key=lambda s: (-s["bm25"], s["index"]))
                if item["bm25"] > 0]
        bm25_gated = bm25 if row["max_bm25"] >= 2 else []
        baseline.append({"case_id": row["case_id"], "turn": row["turn"],
                         "gold_sections": row["kb_gold_sections"],
                         "hybrid_baseline_top4_gold_sections": len(original_gold),
                         "hybrid_cosine_045_top4_gold_sections": len(filtered_gold),
                         "hybrid_cosine_045_lost_baseline_gold": [by_index[index] for index in sorted(original_gold - filtered_gold)],
                         "bm25_baseline_top4_gold_sections": sum(by_index[index]["gold"] for index in bm25[:4]),
                         "bm25_query_gate_2_top4_gold_sections": sum(by_index[index]["gold"] for index in bm25_gated[:4])})
    for row in rows:
        del row["_signals"], row["_ranking"]
    return {"schema": "m3-dev-retrieval-calibration/1",
            "dataset": "eval_m3/datasets/kb-dev.json",
            "dataset_sha256": hashlib.sha256(DEV_PATH.read_bytes()).hexdigest(),
            "dataset_normalized_utf8_lf_sha256": hashlib.sha256(
                DEV_PATH.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
            "embedding_model": kb.EMBEDDING_MODEL, "embedding_dimensions": len(vectors[0]),
            "offline": offline, "cases": len(cases), "turns": len(rows),
            "read_only_cache_verified": cache_before == cache_snapshot() if offline else None,
            "runtime_hybrid_matches_candidate": all(row["runtime_hybrid_matches_candidate"] for row in rows),
            "runtime_bm25_matches_candidate": all(row["runtime_bm25_matches_candidate"] for row in rows),
            "selected_floor": {"user_confirmed": True,
                               "hybrid": {"signal": "cosine", "scope": "passage", "minimum": 0.45},
                               "fallback": {"signal": "bm25", "scope": "query_maximum", "minimum": 2.0,
                                            "after_admission": "positive-score lexical ranking"}},
            "positive_kb_gold_turns": len(positive), "diagnostic_negative_turns": len(negative),
            "kb_gold_sections": sum(row["kb_gold_sections"] for row in positive),
            "scope": "Raw frozen DEV questions only; no model-rewritten queries, agent runs or HOLDOUT reads.",
            "interpretation": "Cosine and BM25 are matching signals, not confidence. A negative can share a known topic while asking an unavailable fact; a relevance floor is not an answerability gate.",
            "rows": rows, "candidates": candidates, "candidate_top4_comparison": baseline}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "eval_m3/phase3/dev-retrieval-calibration.json")
    options = parser.parse_args()
    report = inspect(offline=options.offline)
    options.report.parent.mkdir(parents=True, exist_ok=True)
    options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"rows", "candidates", "candidate_top4_comparison"}},
                     ensure_ascii=False, indent=2))
    return 0 if (report["runtime_hybrid_matches_candidate"]
                 and report["runtime_bm25_matches_candidate"]
                 and report["read_only_cache_verified"] is not False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
