import json
from pathlib import Path
from time import perf_counter

from rag import answer_structured, build_index, read_file, retrieve_fast, split_text


REFUSAL_MARKERS = ("无法确定", "没有相关", "未提及", "没有提及", "资料不足", "无法回答")


def main() -> None:
    document = Path("sample_company_rules.md")
    chunks = build_index(split_text(read_file(document.name, document.read_bytes()), document.name))
    cases = json.loads(Path("eval/v1/datasets/eval_no_answer.json").read_text(encoding="utf-8"))
    passed = 0
    durations = []
    for index, case in enumerate(cases, start=1):
        started = perf_counter()
        trace = {}
        results = retrieve_fast(case["question"], chunks, trace=trace)
        reply = answer_structured(case["question"], results, [])
        duration = perf_counter() - started
        durations.append(duration)
        refused = any(marker in reply for marker in REFUSAL_MARKERS)
        passed += refused
        print(f"{index:02d}. {'PASS' if refused else 'FAIL'} | {duration:.2f}s | {case['question']}")
        print(f"    {reply}")
    print(f"\n拒答准确率: {passed / len(cases):.1%} ({passed}/{len(cases)})")
    print(f"平均耗时: {sum(durations) / len(durations):.2f}s")
    print(f"最大耗时: {max(durations):.2f}s")


if __name__ == "__main__":
    main()

