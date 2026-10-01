"""Stage 6.4A: baseline B, final state F, the frozen comparator and L1-L6 (design §19.2)."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest

from eval_v2.runtime import EvalFixtureError
from eval_v2.stage6_runner import run_stage6_reference
from eval_v2.stage6_state import (
    AUTHOR_WRITABLE,
    COMPARED_TABLES,
    STAGE6_COLUMNS,
    apply_mutate,
    build_baseline,
    check_links,
    compare_final_state,
    harness_connection,
)
from aftersales.action_db import create_stage6_database

from tests import stage6_eval_support as support


def reference(case):
    return run_stage6_reference(case)


class BaselineTests(unittest.TestCase):
    def test_baseline_is_fixture_plus_patches_plus_mutates_only(self):
        case = support.a22c()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            baseline = build_baseline(case, directory)
        self.assertIn("AS-T22C", baseline["after_sales_cases"])  # the trusted mutate
        self.assertEqual(baseline["pending_actions"], {})       # no run effect at all
        self.assertEqual(baseline["action_receipts"], {})
        result = reference(case)
        self.assertEqual(result.baseline_state, baseline)
        self.assertEqual(len(result.final_state["pending_actions"]), 1)

    def test_baseline_is_built_independently_of_the_run(self):
        result = reference(support.approved_return_case())
        # F has the action's rows; B never had them (built, not undone).
        new_cases = set(result.final_state["after_sales_cases"]) - set(result.baseline_state["after_sales_cases"])
        self.assertEqual(len(new_cases), 1)
        self.assertTrue(next(iter(new_cases)).startswith("AS6-"))
        self.assertEqual(set(result.baseline_state["after_sales_cases"]), set(result.initial_state["after_sales_cases"]))

    def test_mutate_must_raise_the_version(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = directory + "/m.db"
            create_stage6_database(path)
            connection = harness_connection(path)
            try:
                with self.assertRaises(EvalFixtureError):
                    apply_mutate(connection, support.mutate("orders", "ORD-1001", support.bump(4)))
                with self.assertRaises(EvalFixtureError):
                    apply_mutate(connection, support.mutate("orders", "ORD-1001", {
                        "op": "update", "set": {"version": 9}}))
                with self.assertRaises(EvalFixtureError):
                    apply_mutate(connection, support.mutate("pending_actions", "PA-1", {"op": "delete"}))
                apply_mutate(connection, support.mutate("orders", "ORD-1001", support.bump(5)))
                self.assertEqual(connection.execute("SELECT version FROM orders WHERE order_id = 'ORD-1001'")
                                 .fetchone()[0], 5)
            finally:
                connection.close()


class ComparatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = support.approved_return_case()
        cls.result = reference(cls.case)

    def compare(self, expected=None, final=None):
        return compare_final_state(self.case["expected_final_state"] if expected is None else expected,
                                   self.result.baseline_state, self.result.final_state if final is None else final)

    def test_the_expected_state_matches(self):
        self.assertTrue(self.compare().ok)

    def test_author_writable_columns_exclude_generated_ones(self):
        for table, columns in AUTHOR_WRITABLE.items():
            for generated in ("case_id", "ticket_id", "pending_action_id", "receipt_id", "resource_id",
                              "idempotency_key", "args_sha256", "snapshot_json", "snapshot_sha256"):
                self.assertNotIn(generated, columns, table)

    def test_wrong_value_missing_insert_and_extra_row_are_caught(self):
        expected = copy.deepcopy(self.case["expected_final_state"])
        expected["pending_actions"]["insert"][0]["row"]["version"] = 2
        self.assertFalse(self.compare(expected).ok)
        expected = copy.deepcopy(self.case["expected_final_state"])
        del expected["action_receipts"]
        report = self.compare(expected)
        self.assertFalse(report.ok)
        self.assertTrue(any("unexpected new row" in p or "new row(s)" in p for p in report.problems))
        expected = copy.deepcopy(self.case["expected_final_state"])
        expected["after_sales_cases"]["insert"].append(copy.deepcopy(expected["after_sales_cases"]["insert"][0]))
        self.assertFalse(self.compare(expected).ok)  # one actual row cannot match two entries

    def test_unlisted_table_must_not_change(self):
        final = copy.deepcopy(self.result.final_state)
        final["orders"]["ORD-1001"]["version"] = 99
        report = self.compare(final=final)
        self.assertFalse(report.ok)
        self.assertTrue(any("ORD-1001" in problem for problem in report.problems))

    def test_update_and_delete_semantics(self):
        final = copy.deepcopy(self.result.final_state)
        final["orders"]["ORD-1001"].update(version=5, updated_at=support.EARLY_UPDATE)
        del final["inventory"]["SKU-MUG"]
        expected = copy.deepcopy(self.case["expected_final_state"])
        expected["orders"] = {"update": {"ORD-1001": {"version": 5, "updated_at": support.EARLY_UPDATE}}}
        expected["inventory"] = {"delete": ["SKU-MUG"]}
        self.assertTrue(self.compare(expected, final).ok)
        expected["inventory"] = {"delete": ["SKU-KETTLE"]}
        self.assertFalse(self.compare(expected, final).ok)
        expected["inventory"] = {"delete": ["SKU-MUG"]}
        expected["orders"]["update"]["ORD-1001"]["version"] = 6  # an unlisted column must stay too
        self.assertFalse(self.compare(expected, final).ok)

    def test_generated_ids_play_no_part_in_matching(self):
        final = copy.deepcopy(self.result.final_state)
        (old, row), = [(key, value) for key, value in final["after_sales_cases"].items()
                       if key.startswith("AS6-")]
        del final["after_sales_cases"][old]
        final["after_sales_cases"]["AS6-FFFFFFFFFFFFFFFF"] = dict(row, case_id="AS6-FFFFFFFFFFFFFFFF")
        self.assertTrue(self.compare(final=final).ok)  # the comparator ignores ids; L1/L5 do not

    def test_audit_is_not_compared(self):
        self.assertNotIn("action_audit_events", COMPARED_TABLES)
        self.assertIn("action_audit_events", STAGE6_COLUMNS)


class LinkInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = reference(support.approved_return_case())
        cls.exchange = reference(support.exchange_case())

    def links(self, final, result=None):
        result = result or self.result
        return check_links(result.baseline_state, final, customer_id=result.customer_id, id_namespace="eval")

    def test_clean_runs_hold_all_six(self):
        for result in (self.result, self.exchange, reference(support.handoff_case()),
                       reference(support.a23()), reference(support.a22a())):
            report = check_links(result.baseline_state, result.final_state,
                                 customer_id=result.customer_id, id_namespace="eval")
            self.assertTrue(report.ok, report.problems)

    def final(self, result=None):
        return copy.deepcopy((result or self.result).final_state)

    def receipt(self, final):
        return next(iter(final["action_receipts"].values()))

    def test_l1_receipt_and_business_row(self):
        final = self.final()
        case_id = self.receipt(final)["resource_id"]
        del final["after_sales_cases"][case_id]
        self.assertFalse(self.links(final).l1)
        final = self.final()
        orphan = dict(next(row for key, row in final["after_sales_cases"].items() if key.startswith("AS6-")),
                      case_id="AS6-0000000000000000")
        final["after_sales_cases"]["AS6-0000000000000000"] = orphan
        self.assertFalse(self.links(final).l1)

    def test_l2_pending_and_receipt_point_at_each_other(self):
        final = self.final()
        pending = next(iter(final["pending_actions"].values()))
        pending["receipt_id"] = "RC-0000000000000000"
        self.assertFalse(self.links(final).l2)
        final = self.final()
        self.receipt(final)["pending_action_id"] = None
        self.assertFalse(self.links(final).l2)

    def test_l3_key_and_digest_recompute(self):
        final = self.final()
        self.receipt(final)["args_sha256"] = "0" * 64
        self.assertFalse(self.links(final).l3)
        final = self.final()
        pending = next(iter(final["pending_actions"].values()))
        pending["idempotency_key"] = "s6k1-" + "0" * 64
        self.assertFalse(self.links(final).l3)

    def test_l4_snapshot(self):
        final = self.final()
        receipt = self.receipt(final)
        document = json.loads(receipt["snapshot_json"])
        document["extra"] = 1
        receipt["snapshot_json"] = json.dumps(document)
        receipt["snapshot_sha256"] = hashlib.sha256(receipt["snapshot_json"].encode()).hexdigest()
        self.assertFalse(self.links(final).l4)
        final = self.final()
        self.receipt(final)["snapshot_sha256"] = "0" * 64
        self.assertFalse(self.links(final).l4)

    def test_l5_deterministic_ids(self):
        report = check_links(self.result.baseline_state, self.result.final_state,
                             customer_id=self.result.customer_id, id_namespace="another")
        self.assertFalse(report.l5)
        self.assertTrue(report.l1 and report.l2 and report.l3 and report.l4)

    def test_l6_trusted_customer(self):
        final = self.final(self.exchange)
        row = next(row for key, row in final["after_sales_cases"].items() if key.startswith("AS6-"))
        row["customer_id"] = "CUST-002"
        self.assertFalse(self.links(final, self.exchange).l6)


if __name__ == "__main__":
    unittest.main()
