import json
import sys
from pathlib import Path

from rag import bm25_rank, build_index, hybrid_retrieve, read_file, retrieve, split_text


def main() -> None:
    document = Path("sample_company_rules.md")
    case_file = Path(sys.argv[1] if len(sys.argv) > 1 else "eval/v1/datasets/eval_cases.json")
    cases = json.loads(case_file.read_text(encoding="utf-8"))
    chunks = build_index(
        split_text(read_file(document.name, document.read_bytes()), document.name)
    )

    print(f"测试文件：{case_file.name}；问题：{len(cases)} 条；知识块：{len(chunks)} 个\n")
    methods = {
        "纯向量": lambda q: retrieve(q, chunks, top_k=len(chunks)),
        "纯BM25": lambda q: bm25_rank(q, chunks),
        "RRF融合": lambda q: hybrid_retrieve(q, chunks, top_k=len(chunks)),
    }
    total = len(cases)
    for method_name, search in methods.items():
        hits_at_1 = hits_at_3 = 0
        reciprocal_rank_sum = 0.0
        misses = []
        for case in cases:
            results = search(case["question"])
            rank = next(
                (i for i, (chunk, _) in enumerate(results, start=1)
                 if case["expected_heading"] in chunk.text),
                None,
            )
            hits_at_1 += rank == 1
            hits_at_3 += rank is not None and rank <= 3
            reciprocal_rank_sum += 1 / rank if rank else 0
            if rank != 1:
                misses.append((rank, case["question"], results[0][0].index))
        print(f"{method_name}: Recall@1={hits_at_1/total:.1%} | Recall@3={hits_at_3/total:.1%} | MRR={reciprocal_rank_sum/total:.3f}")
        if method_name == "RRF融合":
            print("\nRRF 未排第一的问题：")
            for rank, question, top_index in misses:
                print(f"- rank={rank}, top={top_index}: {question}")


if __name__ == "__main__":
    main()
