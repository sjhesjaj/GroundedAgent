"""Run the V1 unit suite and record one outcome per test id.

Pure analysis tooling for Stage 4.0 (V1 freeze + test inventory). It imports no
runtime module itself, changes no code, and runs the suite exactly as
`python -m unittest discover` does (same loader, same start directory, same
top-level directory), so the per-test outcomes it writes are the outcomes of
the canonical run.

Usage (from the repository root):

    .\\.venv\\Scripts\\python.exe -X utf8 docs\\v2\\tools\\collect_v1_test_results.py <output.json>
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


class RecordingResult(unittest.TextTestResult):
    """A text result that also remembers every test's final outcome."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes: dict[str, dict[str, object]] = {}

    def _set(self, test, outcome: str, detail: str | None = None) -> None:
        self.outcomes[test.id()] = {"outcome": outcome, "detail": detail}

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "fail", self._exc_info_to_string(err, test)[-2000:])

    def addError(self, test, err):
        super().addError(test, err)
        # setUpClass / module fixtures report a _ErrorHolder, not a TestCase.
        self._set(test, "error", self._exc_info_to_string(err, test)[-2000:])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            kind = "fail" if issubclass(err[0], test.failureException) else "error"
            self._set(test, kind, self._exc_info_to_string(err, test)[-2000:])


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def main(output: Path) -> int:
    loader = unittest.TestLoader()
    # Identical to `python -m unittest discover` run from the repository root.
    suite = loader.discover(start_dir=".", pattern="test*.py", top_level_dir=None)
    discovered = [test.id() for test in _iter_tests(suite)]

    runner = unittest.TextTestRunner(resultclass=RecordingResult, verbosity=1, stream=sys.stderr)
    started = time.perf_counter()
    result = runner.run(suite)
    elapsed = time.perf_counter() - started

    missing = [test_id for test_id in discovered if test_id not in result.outcomes]
    payload = {
        "schema_version": 1,
        "git_head": _git("rev-parse", "HEAD"),
        "git_status_short": _git("status", "--short"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "command": "unittest discover (start_dir='.', pattern='test*.py')",
        "tests_run": result.testsRun,
        "discovered": len(discovered),
        "was_successful": result.wasSuccessful(),
        "counts": {
            outcome: sum(1 for item in result.outcomes.values() if item["outcome"] == outcome)
            for outcome in ("pass", "fail", "error", "skip", "expected_failure", "unexpected_success")
        },
        "unrecorded_test_ids": missing,
        "elapsed_seconds": round(elapsed, 3),
        "results": dict(sorted(result.outcomes.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}: run={result.testsRun} counts={payload['counts']}", file=sys.stderr)
    return 0 if result.wasSuccessful() and not missing else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: collect_v1_test_results.py <output.json>")
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
