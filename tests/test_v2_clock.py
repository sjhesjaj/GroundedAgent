"""Clock / virtual_now (docs/v2/stage4-design.md §3.2)."""

import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aftersales.clock import BUSINESS_TIMEZONE, Clock, FixedClock, SystemClock
from aftersales.demo import DEMO_VIRTUAL_NOW
from aftersales.executor import execute_tool
from aftersales.registry import build_runtime_registry
from orchestration.contracts import ToolResult, ToolStatus

from tests.v2_support import VALID_BUSINESS_CALLS, at, make_context, memory_connection

REPO_ROOT = Path(__file__).resolve().parent.parent

# The V2 business-time scope: the whole domain package plus the V2 evidence
# contract. Audit time (agent_trace, storage, wiki build) is out of scope.
SCANNED_PYTHON = sorted((REPO_ROOT / "aftersales").glob("*.py")) + [
    REPO_ROOT / "orchestration" / "contracts.py"
]
SCANNED_SQL = [
    REPO_ROOT / "aftersales" / "schema.sql",
    REPO_ROOT / "system_fixtures" / "aftersales_demo_seed.sql",
]
# Hard-coded allowlist: the one place allowed to read the system clock.
ALLOWED = {REPO_ROOT / "aftersales" / "clock.py"}

CLOCK_OWNERS = {"datetime", "date"}
CLOCK_METHODS = {"now", "utcnow", "today", "fromtimestamp", "utcfromtimestamp"}
TIME_FUNCTIONS = {"time", "time_ns", "localtime", "gmtime", "ctime"}
SQL_CLOCK_MARKERS = ("current_timestamp", "current_date", "current_time", "'now'", '"now"')


def _owner_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def clock_violations(source: str) -> list[str]:
    """Every direct read of system time in `source`, as a description."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            owner = _owner_name(node.value)
            if owner in CLOCK_OWNERS and node.attr in CLOCK_METHODS:
                found.append(owner + "." + node.attr)
            if owner == "time" and node.attr in TIME_FUNCTIONS:
                found.append("time." + node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name in TIME_FUNCTIONS:
                    found.append("from time import " + alias.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(sql_clock_violations(node.value))
    return found


def sql_clock_violations(text: str) -> list[str]:
    lowered = text.lower()
    return ["SQL " + marker for marker in SQL_CLOCK_MARKERS if marker in lowered]


class FixedClockTests(unittest.TestCase):
    def test_returns_the_fixed_aware_instant_every_time(self):
        instant = datetime(2026, 11, 15, 10, 0, tzinfo=BUSINESS_TIMEZONE)
        clock = FixedClock(instant)
        self.assertEqual(clock.now(), instant)
        self.assertEqual(clock.now(), clock.now())
        self.assertIsNotNone(clock.now().utcoffset())
        self.assertIsInstance(clock, Clock)

    def test_rejects_naive_and_non_datetime_values(self):
        for bad in (datetime(2026, 11, 15), "2026-11-15T10:00:00+08:00", None, 0):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    FixedClock(bad)

    def test_keeps_the_offset_it_was_given(self):
        utc = datetime(2031, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(FixedClock(utc).now().utcoffset(), timedelta(0))


class SystemClockTests(unittest.TestCase):
    def test_returns_an_aware_datetime_in_the_business_timezone(self):
        now = SystemClock().now()
        self.assertIsNotNone(now.utcoffset())
        self.assertEqual(now.utcoffset(), timedelta(hours=8))
        self.assertIsInstance(SystemClock(), Clock)

    def test_tracks_real_time(self):
        # Test code may read the wall clock; the domain may not.
        before = datetime.now(timezone.utc)
        now = SystemClock().now()
        after = datetime.now(timezone.utc)
        self.assertLessEqual(before, now)
        self.assertLessEqual(now, after)

    def test_accepts_another_timezone(self):
        self.assertEqual(SystemClock(timezone.utc).now().utcoffset(), timedelta(0))


class NoSystemTimeBypassTests(unittest.TestCase):
    """Guard 1 of §3.2: static scan. clock.py is the only allowed reader."""

    def test_the_scanner_catches_every_form(self):
        samples = {
            "datetime.now()": "from datetime import datetime\nx = datetime.now()",
            "datetime.datetime.utcnow()": "import datetime\nx = datetime.datetime.utcnow()",
            "date.today()": "from datetime import date\nx = date.today()",
            "time.time()": "import time\nx = time.time()",
            "time.localtime()": "import time\nx = time.localtime()",
            "from time import time": "from time import time",
            "CURRENT_TIMESTAMP": "SQL = 'SELECT CURRENT_TIMESTAMP'",
            "datetime('now')": "SQL = \"SELECT datetime('now')\"",
        }
        for name, source in samples.items():
            with self.subTest(form=name):
                self.assertTrue(clock_violations(source))

    def test_the_scanner_allows_clock_reads_and_parsing(self):
        source = (
            "from datetime import datetime\n"
            "x = context.clock.now()\n"
            "y = datetime.fromisoformat('2026-11-15T10:00:00+08:00')\n"
        )
        self.assertEqual(clock_violations(source), [])

    def test_the_clock_module_is_the_only_reader(self):
        clock_source = (REPO_ROOT / "aftersales" / "clock.py").read_text(encoding="utf-8")
        self.assertTrue(clock_violations(clock_source))

    def test_v2_domain_python_never_reads_system_time(self):
        """V2 semantics superseding V1 tests.test_system_provider.EvidenceMappingTests.test_module_imports_no_clock"""
        self.assertTrue(SCANNED_PYTHON)
        for path in SCANNED_PYTHON:
            if path in ALLOWED:
                continue
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                source = path.read_text(encoding="utf-8")
                self.assertEqual(clock_violations(source), [])

    def test_v2_schema_and_seed_never_read_system_time(self):
        for path in SCANNED_SQL:
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                self.assertEqual(sql_clock_violations(path.read_text(encoding="utf-8")), [])


class VirtualNowSentinelTests(unittest.TestCase):
    """Guard 2 of §3.2, at tool level: any read of system time would disagree."""

    def setUp(self):
        self.connection = memory_connection()
        self.addCleanup(self.connection.close)
        self.registry = build_runtime_registry()

    def test_business_tools_observe_whatever_year_the_clock_says(self):
        for instant in (at(2031, 6, 1, 9), at(2019, 2, 3, 4), DEMO_VIRTUAL_NOW):
            context = make_context(self.connection, now=instant)
            for tool, arguments in VALID_BUSINESS_CALLS:
                with self.subTest(year=instant.year, tool=tool):
                    result = execute_tool(self.registry, context, tool, arguments)
                    self.assertEqual(result.status, ToolStatus.OK)
                    self.assertEqual(
                        {item.observed_at for item in result.evidence},
                        {instant.isoformat()},
                    )

    def test_policy_search_receives_the_clock_as_of(self):
        seen = []

        class RecordingAdapter:
            def search(self, query, *, as_of):
                seen.append(as_of)
                return ToolResult(
                    tool_name="search_after_sales_policy", status=ToolStatus.EMPTY
                )

        registry = build_runtime_registry(RecordingAdapter())
        instant = at(2031, 11, 11)
        execute_tool(
            registry,
            make_context(self.connection, now=instant),
            "search_after_sales_policy",
            {"query": "退货时限"},
        )
        self.assertEqual(seen, [instant])


if __name__ == "__main__":
    unittest.main()
