import json
from pathlib import Path

from agent import decide_action


def main() -> None:
    cases = json.loads(Path("eval/v1/datasets/eval_agent_routes.json").read_text(encoding="utf-8"))
    passed = 0
    for index, case in enumerate(cases, start=1):
        decision = decide_action(case["question"], [])
        actual = decision.get("tool") if decision["type"] == "tool" else "direct"
        ok = actual == case["expected_action"]
        passed += ok
        print(
            f"{index:02d}. {'PASS' if ok else 'FAIL'} | "
            f"expected={case['expected_action']} | actual={actual} | {case['question']}"
        )
    print(f"\nTool Selection Accuracy: {passed / len(cases):.1%} ({passed}/{len(cases)})")


if __name__ == "__main__":
    main()

