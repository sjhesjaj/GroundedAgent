"""Question/gold-section overlap contracts, using only temporary synthetic data."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import check_evaluation_overlap as overlap


class M3EvaluationOverlapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.corpus = self.directory / "knowledge_base"
        self.policies = self.directory / "policy_sources"
        self.corpus.mkdir()
        self.policies.mkdir()
        self.dataset = self.directory / "dev.json"

    def write_document(self, doc_id="sample", body="## 说明\n退款通常按原支付渠道退回。",
                       source="knowledge_base", version="1", metadata_id=None):
        directory = self.corpus if source == "knowledge_base" else self.policies
        id_key = "doc_id" if source == "knowledge_base" else "policy_id"
        metadata = {id_key: metadata_id or doc_id, "version": version,
                    "title": "不参与重叠的标题"}
        path = directory / (doc_id + ".md")
        path.write_text("---\n" + json.dumps(metadata, ensure_ascii=False)
                        + "\n---\n\n" + body + "\n", encoding="utf-8")
        return path

    def gold(self, doc_id="sample", source="knowledge_base", version="1", sections=None):
        return {"source": source, "doc_id": doc_id, "version": version,
                "sections": sections if sections is not None else ["说明"]}

    def turn(self, question="原路退钱要到哪里查看？", gold=None):
        return {"question": question, "gold": [self.gold()] if gold is None else gold}

    def write_cases(self, turns=None, cases=None):
        if cases is None:
            cases = [{"id": "synthetic-001", "turns": turns or [self.turn()]}]
        self.dataset.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
        return self.dataset

    def evaluate(self):
        return overlap.evaluate_m3(self.dataset, self.corpus, self.policies)

    def cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = overlap.main(["--m3-dataset", str(self.dataset),
                                 "--corpus-dir", str(self.corpus),
                                 "--policy-dir", str(self.policies), *extra])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_verbatim_blocks_and_console_does_not_reveal_question_or_gold(self):
        sentence = "退款通常按原支付渠道退回。"
        self.write_document()
        self.write_cases([self.turn(sentence)])
        report = self.evaluate()
        self.assertEqual(report["summary"]["max_similarity"], 1.0)
        self.assertEqual(report["summary"]["blocking_pair_count"], 1)
        self.assertEqual(report["summary"]["blocking_turn_count"], 1)
        self.assertFalse(report["summary"]["passed"])
        code, stdout, stderr = self.cli()
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertNotIn(sentence, stdout)
        self.assertNotIn("synthetic-001", stdout)

    def test_paraphrase_passes(self):
        self.write_document()
        self.write_cases([self.turn("付款走的是银行卡，售后款会返到哪个账户？")])
        summary = self.evaluate()["summary"]
        self.assertLess(summary["max_similarity"], overlap.THRESHOLD_ELEVATED)
        self.assertTrue(summary["passed"])

    def test_elevated_band_is_report_only_without_fraction_gate(self):
        self.write_document(body="## 说明\nabcdefghij")
        self.write_cases([self.turn("abcdefghXY")])
        report = self.evaluate()
        self.assertGreaterEqual(report["summary"]["max_similarity"], 0.80)
        self.assertLess(report["summary"]["max_similarity"], 0.85)
        self.assertEqual(report["summary"]["elevated_pair_count"], 1)
        self.assertEqual(report["summary"]["elevated_only_pair_count"], 1)
        self.assertEqual(report["summary"]["elevated_turn_count"], 1)
        self.assertTrue(report["summary"]["passed"])
        self.assertFalse(report["thresholds"]["elevated_is_failure"])
        self.assertEqual(self.cli()[0], 0)

    def test_exact_blocking_boundary_is_inclusive(self):
        self.write_document(body="## 说明\nabcdefghijklmnopqrst")
        self.write_cases([self.turn("abcdefghijklmnopqXYZ")])
        report = self.evaluate()
        self.assertEqual(report["summary"]["max_similarity"], 0.85)
        self.assertEqual(report["summary"]["blocking_pair_count"], 1)
        self.assertFalse(report["summary"]["passed"])

    def test_reordered_text_uses_jaccard_when_it_exceeds_sequence_ratio(self):
        self.write_document(body="## 说明\nabcdefghij")
        self.write_cases([self.turn("fghijabcde")])
        pair = self.evaluate()["all_pairs"][0]
        self.assertGreater(pair["bigram_jaccard"], pair["sequence_ratio"])
        self.assertEqual(pair["similarity"], 0.80)
        self.assertEqual(pair["band"], "elevated")

    def test_existing_normalization_and_policy_id_alias(self):
        self.write_document(doc_id="rule", source="policy_sources", body="## 规则\nA B，退款！")
        self.write_cases([self.turn("ab退款。", [self.gold("rule", "policy_sources", sections=["规则"])])])
        pair = self.evaluate()["all_pairs"][0]
        self.assertEqual(pair["similarity"], 1.0)
        self.assertEqual(pair["source"], "policy_sources")
        self.assertEqual(pair["doc_id"], "rule")
        self.assertEqual(pair["gold_text"], "A B，退款！")

    def test_all_turns_docs_and_sections_are_compared_individually(self):
        self.write_document(body="## 甲\n完整第一句。\n完整第二句。\n### 乙\n另一段正文。")
        self.write_document("other", body="## 说明\n另一份文件。")
        self.write_cases([
            self.turn("完整第一句。完整第二句。", [self.gold(sections=["甲", "乙"]), self.gold("other")]),
            self.turn("另一段正文。", [self.gold(sections=["乙"])]),
        ])
        report = self.evaluate()
        self.assertEqual(report["summary"]["case_count"], 1)
        self.assertEqual(report["summary"]["turn_count"], 2)
        self.assertEqual(report["summary"]["pair_count"], 4)
        self.assertEqual(report["summary"]["blocking_turn_count"], 2)
        self.assertEqual({pair["turn_index"] for pair in report["all_pairs"]}, {1, 2})
        first = next(pair for pair in report["all_pairs"] if pair["section"] == "甲")
        self.assertEqual(first["gold_text"], "完整第一句。 完整第二句。")
        self.assertEqual(first["similarity"], 1.0)
        self.assertNotIn("乙", first["gold_text"])

    def test_front_matter_and_heading_do_not_enter_gold_text(self):
        self.write_document(body="## 重复问题\n独立的正文内容。")
        self.write_cases([self.turn("重复问题", [self.gold(sections=["重复问题"])])])
        pair = self.evaluate()["all_pairs"][0]
        self.assertEqual(pair["gold_text"], "独立的正文内容。")
        self.assertNotIn("title", pair["gold_text"])
        self.assertLess(pair["similarity"], 0.80)

    def test_empty_gold_is_counted_and_skipped_without_source_reads(self):
        self.write_cases(cases=[
            {"id": "empty", "turns": [self.turn("你好", []), self.turn("谢谢", [])]},
        ])
        with mock.patch.object(overlap, "_m3_document", side_effect=AssertionError("source read")):
            report = self.evaluate()
        self.assertEqual(report["summary"], {
            "case_count": 1, "turn_count": 2, "pair_count": 0,
            "empty_gold_case_count": 1, "empty_gold_turn_count": 2,
            "max_similarity": 0.0, "blocking_pair_count": 0, "blocking_turn_count": 0,
            "elevated_pair_count": 0, "elevated_turn_count": 0,
            "elevated_only_pair_count": 0, "passed": True,
        })

    def test_missing_doc_wrong_source_wrong_version_and_missing_heading_reject(self):
        self.write_document()
        invalid = [
            (self.gold("missing"), "source document is missing"),
            ({**self.gold(), "source": "other"}, "source must be"),
            (self.gold(version="2"), "gold version"),
            (self.gold(sections=["不存在"]), "section .* is missing"),
            (self.gold(sections=[]), "sections must be"),
            (self.gold(sections=["说明", "说明"]), "sections must be"),
            (self.gold("../sample"), "doc_id must be"),
        ]
        for reference, message in invalid:
            with self.subTest(reference=reference):
                self.write_cases([self.turn(gold=[reference])])
                with self.assertRaisesRegex(ValueError, message):
                    self.evaluate()
                self.assertEqual(self.cli()[0], 2)

    def test_mismatched_front_matter_id_is_rejected(self):
        self.write_document(metadata_id="not-sample")
        self.write_cases()
        with self.assertRaisesRegex(ValueError, "doc_id does not match"):
            self.evaluate()

    def test_duplicate_ids_missing_turns_and_empty_questions_are_rejected(self):
        invalid = [
            ([{"id": "repeat", "turns": [self.turn(gold=[])]}] * 2, "duplicate case id"),
            ([{"id": "", "turns": [self.turn(gold=[])]}], "id must be"),
            ([{"id": "missing-turns"}], "turns must be"),
            ([{"id": "empty-question", "turns": [self.turn("， ！", [])]}], "question must contain"),
            ([{"id": "missing-gold", "turns": [{"question": "你好"}]}], "gold must be"),
        ]
        for cases, message in invalid:
            with self.subTest(message=message):
                self.write_cases(cases=cases)
                with self.assertRaisesRegex(ValueError, message):
                    self.evaluate()

    def test_duplicate_headings_and_empty_sections_cannot_be_ambiguous_gold(self):
        for body in ("## 说明\n内容。\n## 说明\n重复。", "## 说明\n## 另一个标题\n内容。"):
            with self.subTest(body=body):
                self.write_document(body=body)
                self.write_cases()
                with self.assertRaises(ValueError):
                    self.evaluate()

    def test_explicit_report_has_full_pair_text_but_default_writes_nothing(self):
        self.write_document()
        self.write_cases()
        before = set(self.directory.rglob("*"))
        self.assertEqual(self.cli()[0], 0)
        self.assertEqual(set(self.directory.rglob("*")), before)
        report_path = self.directory / "reports" / "overlap.json"
        self.assertEqual(self.cli("--report", str(report_path))[0], 0)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["all_pairs"][0]["question"], self.turn()["question"])
        self.assertEqual(report["all_pairs"][0]["gold_text"], "退款通常按原支付渠道退回。")

    def test_m3_reads_only_explicit_dataset_and_referenced_source(self):
        referenced = self.write_document()
        self.write_cases()
        unopened = self.directory / "holdout.json"
        unopened.write_text("must never be opened", encoding="utf-8")
        unused_source = self.corpus / "unused.md"
        unused_source.write_text("invalid unreferenced source", encoding="utf-8")
        read_paths = []
        original = Path.read_text

        def tracked_read(path, *args, **kwargs):
            read_paths.append(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", tracked_read), \
                mock.patch.object(overlap, "load_questions", side_effect=AssertionError("legacy question set")), \
                mock.patch.object(overlap, "evaluate_v2", side_effect=AssertionError("legacy v2")), \
                mock.patch.object(overlap, "_corpus_entities", side_effect=AssertionError("legacy corpus")):
            self.assertEqual(self.cli()[0], 0)
        self.assertEqual(read_paths, [self.dataset, referenced])

    def test_import_does_not_read_legacy_sources(self):
        spec = importlib.util.spec_from_file_location("overlap_isolation_probe", overlap.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("unexpected source read")):
            spec.loader.exec_module(module)
        self.assertIsNone(module.ENTITIES)

    def test_existing_cli_dispatch_and_threshold_reporting_are_preserved(self):
        gate = {"name": "synthetic gate", "value": 0, "limit": 0, "passed": True}
        for mode in ([], ["--v2"]):
            with self.subTest(mode=mode), \
                    mock.patch.object(overlap, "COMPARISONS", (("legacy", Path("h.json"), ()),)), \
                    mock.patch.object(overlap, "evaluate", return_value={"gates": [gate]}) as evaluate, \
                    mock.patch.object(overlap, "evaluate_v2", return_value={"comparisons": [], "gates": [gate]}) as evaluate_v2, \
                    contextlib.redirect_stdout(io.StringIO()):
                report_path = self.directory / ("legacy-v2.json" if mode else "legacy.json")
                self.assertEqual(overlap.main([*mode, "--report", str(report_path)]), 0)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["thresholds"]["max_elevated_fraction"], 0.05)
                self.assertEqual(report["thresholds"]["masked_template"], 0.90)
                self.assertEqual(evaluate.call_count, 0 if mode else 1)
                self.assertEqual(evaluate_v2.call_count, 1 if mode else 0)


if __name__ == "__main__":
    unittest.main()
