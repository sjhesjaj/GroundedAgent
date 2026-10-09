"""Formal corpus contracts and cache lifecycle; all providers are test doubles."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aftersales.demo import DEMO_VIRTUAL_NOW
from aftersales_service import knowledge_base as kb
from aftersales_service.embedding_cache import CachedEmbedder, EmbeddingCacheMiss, content_hash


class CorpusBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "corpus"
        self.directory.mkdir()
        self.cache = Path(self.temporary.name) / "cache"

    def write(self, body="## 说明\n退款通常按原支付渠道退回。", **updates):
        meta = {"doc_id": "sample", "title": "说明", "doc_type": "faq", "scope": [],
                "effective_from": "2026-01-01T00:00:00+08:00", "effective_to": None,
                "version": "1", "restates": None, **updates}
        path = self.directory / "sample.md"
        path.write_text("---\n" + json.dumps(meta, ensure_ascii=False, indent=2)
                        + "\n---\n\n" + body + "\n", encoding="utf-8")
        return path

    def load(self, **options):
        return kb.load_corpus(self.directory, **options)

    def approve_short_body(self, path):
        record = {"schema_version": 1, "user_confirmed": True,
                  "hash_normalization": "utf-8-lf", "exceptions": [
                      {"doc_id": path.stem, "normalized_markdown_sha256":
                       content_hash(path.read_text(encoding="utf-8"))}]}
        manifest = self.directory / "reviewed-short-bodies.json"
        manifest.write_text(json.dumps(record), encoding="utf-8")
        return manifest

    def test_json_error_reports_its_actual_line(self):
        path = self.write()
        path.write_text('---\n{\n  "doc_id": "sample",\n  broken\n}\n---\n## 内容\n正文。', encoding="utf-8")
        with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:4: front matter is not JSON"):
            self.load()

    def test_duplicate_and_escaped_duplicate_keys_report_line(self):
        for key in ('"title"', '"\\u0074itle"'):
            with self.subTest(key=key):
                path = self.write()
                text = path.read_text(encoding="utf-8")
                text = text.replace('  "title": "说明",', '  "title": "说明",\n  ' + key + ': "重复",')
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:5: duplicate.*title"):
                    self.load()

    def test_field_type_errors_are_corpus_errors_at_field_lines(self):
        for key, value in (("doc_type", []), ("scope", [["服装"]]), ("restates", 7), ("version", 'x"y')):
            with self.subTest(key=key):
                path = self.write(**{key: value})
                number = next(index for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                              if '"' + key + '"' in line)
                with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:" + str(number) + ":"):
                    self.load()

    def test_timezone_and_reversed_period_are_rejected(self):
        for updates in ({"effective_from": "2026-01-01T00:00:00"},
                        {"effective_to": "2025-12-31T00:00:00+08:00"}):
            with self.subTest(updates=updates):
                self.write(**updates)
                with self.assertRaises(kb.CorpusError):
                    self.load()

    def test_unheaded_and_empty_heading_content_cannot_be_discarded(self):
        for body in ("未归入标题的正文。\n## 说明\n正文。", "## 空标题\n\n## 正文\n内容。", ""):
            with self.subTest(body=body):
                self.write(body)
                with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:\d+:"):
                    self.load()

    def test_multilevel_headings_split_and_preserve_source_lines(self):
        path = self.write("# 第一部分\n退款记录。\n\n### 第二部分\n支付凭证。\n凭证需清晰。")
        document = self.load()[0]
        lines = path.read_text(encoding="utf-8").splitlines()
        passages = kb.split_passages(document)
        self.assertEqual([item.section for item in passages], ["第一部分", "第二部分"])
        for passage in passages:
            self.assertIn(passage.section, lines[passage.heading_line - 1])
            self.assertIn(lines[passage.start_line - 1], passage.text)
            self.assertIn(lines[passage.end_line - 1], passage.text)

    def test_long_section_pieces_retain_text_and_line_ranges(self):
        self.write("## 长说明\n" + "运费凭证请保存。" * 40 + "\n" + "退款记录可核对。" * 50)
        document = self.load()[0]
        passages = kb.split_passages(document)
        self.assertGreater(len(passages), 1)
        self.assertEqual("".join(passage.text for passage in passages), document.sections[0][1])
        self.assertTrue(all(len(passage.text) <= kb.PASSAGE_CHARS for passage in passages))
        self.assertLess(passages[0].start_line, passages[-1].end_line)

    def test_kb_only_eligibility_and_quality_conditions_need_restates(self):
        for clause in ("商品可以申请退货。", "定制商品不适用无理由退货。", "七天无理由。",
                       "换货窗口为十五个自然日。", "质量争议需要鉴定时转人工核实。",
                       "签收后三天内可退。", "内衣拆封后能换。", "定制件不给退。",
                       "定制商品不能无理由退。", "商品有质量问题就必须转人工。"):
            with self.subTest(clause=clause):
                self.write("## 说明\n" + clause)
                with self.assertRaisesRegex(kb.CorpusError, "eligibility statement requires restates"):
                    self.load()

    def test_wrapping_cannot_bypass_eligibility_lint(self):
        path = self.write("## 说明\n商品可以申请\n退货。")
        number = path.read_text(encoding="utf-8").splitlines().index("商品可以申请") + 1
        with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:" + str(number) + ": eligibility"):
            self.load()

    def test_shipping_refund_and_price_protection_days_are_kb_only(self):
        self.write("## 费用与到账\n退货寄回费用按原因区分。运费险在包裹被仓库签收后72小时内到账。"
                   "银行卡3至7个工作日到账。价保申请时限为签收后7天，请准备截图。")
        self.assertIsNone(self.load()[0].restates)

    def test_reimbursement_words_are_not_product_eligibility(self):
        self.write("## 运费返还\n运费可以退回账户。审核后运费可退。款项可退，差额可以退。")
        self.assertEqual(len(self.load()), 1)
        self.write("## 说明\n运费可退。商品可退。")
        with self.assertRaisesRegex(kb.CorpusError, "eligibility"):
            self.load()

    def test_matching_arabic_chinese_and_formal_day_counts(self):
        for count in ("7天", "七个自然日", "柒日", "７个自然日", "七至七天", "7天至7天", "7个自然天"):
            with self.subTest(count=count):
                self.write("## 标准退货\n签收次日起" + count + "内可以申请退货。", restates="standard-return")
                self.assertEqual(self.load()[0].restates, "standard-return")

    def test_wrong_days_ranges_and_units_fail_at_specific_body_line(self):
        for count in ("15天", "十五个自然日", "七至十五天", "7至15个自然日", "7天至15天",
                      "7个工作日", "7个工作天", "168小时", "15个自然天", "十五天", "一日"):
            with self.subTest(count=count):
                path = self.write("## 标准退货\n签收次日起" + count + "内可以申请退货。", restates="standard-return")
                number = len(path.read_text(encoding="utf-8").splitlines())
                with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:" + str(number) + ":.*window_days"):
                    self.load()

    def test_titles_are_checked_for_eligibility_and_wrong_numbers(self):
        self.write(title="七天无理由说明")
        with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:4: eligibility"):
            self.load()
        self.write("## 说明\n标准窗口为7个自然日。", title="十五天标准退货", restates="standard-return")
        with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:4:.*window_days"):
            self.load()

    def test_line_wrapping_does_not_hide_wrong_day_count(self):
        self.write("## 标准退货\n签收次日起十五\n个自然日内可以申请退货。", restates="standard-return")
        with self.assertRaisesRegex(kb.CorpusError, "window_days"):
            self.load()

    def test_missing_number_and_cross_rule_equal_number_are_rejected(self):
        self.write("## 说明\n标准退货以结构化规则为准。", restates="standard-return")
        with self.assertRaisesRegex(kb.CorpusError, "must state.*window_days"):
            self.load()
        self.write("## 说明\n换货窗口为15个自然日。", restates="november-promo-return",
                   effective_from="2026-11-01T00:00:00+08:00", effective_to="2026-12-01T00:00:00+08:00")
        with self.assertRaisesRegex(kb.CorpusError, "different rule"):
            self.load()

    def test_ordinal_dates_and_zero_clock_are_not_window_lengths(self):
        self.write("## 活动退货\n窗口为十五个自然日。周一签收，周二为第一天。"
                   "第二日继续按自然日计数，最后一日结束后关闭。最后一天也计入窗口。"
                   "活动到2026年12月1日零时结束。一个天数不表示全部条件。", restates="november-promo-return",
                   effective_from="2026-11-01T00:00:00+08:00", effective_to="2026-12-01T00:00:00+08:00")
        self.assertEqual(len(self.load()), 1)

    def test_a_true_one_day_window_remains_rejected(self):
        self.write("## 标准退货\n窗口为七个自然日。另称退货窗口一日。", restates="standard-return")
        with self.assertRaisesRegex(kb.CorpusError, "window_days=7"):
            self.load()

    def test_custom_rule_may_retain_its_source_quality_overview(self):
        self.write("## 定制说明\n定制商品不适用无理由退货。存在质量争议时应转人工核实。",
                   restates="custom-non-returnable", scope=["定制"])
        self.assertEqual(self.load()[0].restates, "custom-non-returnable")

    def test_non_window_rule_cannot_smuggle_a_window_number(self):
        self.write("## 定制说明\n定制商品可以在七天内退货。", restates="custom-non-returnable")
        with self.assertRaisesRegex(kb.CorpusError, "window_days=None"):
            self.load()

    def test_formal_build_length_check_does_not_reject_short_unit_fixtures(self):
        self.write()
        self.assertEqual(len(self.load()), 1)
        with self.assertRaisesRegex(kb.CorpusError, "150-800"):
            self.load(validate_lengths=True)

    def test_user_approved_exact_short_body_passes_formal_length_check(self):
        path = self.write()
        self.approve_short_body(path)
        self.assertEqual(len(self.load(validate_lengths=True)), 1)

    def test_changed_short_body_does_not_keep_its_user_approval(self):
        path = self.write()
        self.approve_short_body(path)
        text = path.read_text(encoding="utf-8").replace("通常", "一般")
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(kb.CorpusError, "approval content hash differs for sample"):
            self.load(validate_lengths=True)

    def test_short_body_approval_survives_crlf_checkout(self):
        path = self.write()
        self.approve_short_body(path)
        text = path.read_text(encoding="utf-8")
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(len(self.load(validate_lengths=True)), 1)

    def test_headings_cannot_make_a_short_body_pass_minimum_length(self):
        self.write("## " + "标题" * 100 + "\n退款通常按原支付渠道退回。")
        self.assertEqual(self.load()[0].body_chars, len("退款通常按原支付渠道退回。"))
        with self.assertRaisesRegex(kb.CorpusError, "150-800.*excluding headings"):
            self.load(validate_lengths=True)

    def test_formal_length_bounds_are_inclusive_without_broad_exemption(self):
        for length, valid in ((149, False), (150, True), (800, True), (801, False)):
            with self.subTest(length=length):
                self.write("## 说明\n" + "字" * length)
                if valid:
                    self.assertEqual(self.load(validate_lengths=True)[0].body_chars, length)
                else:
                    with self.assertRaisesRegex(kb.CorpusError, "150-800"):
                        self.load(validate_lengths=True)

    def test_short_body_manifest_requires_explicit_confirmation_and_valid_schema(self):
        path = self.write()
        manifest = self.approve_short_body(path)
        record = json.loads(manifest.read_text(encoding="utf-8"))
        for change in ({"user_confirmed": False}, {"user_confirmed": 1},
                       {"schema_version": True}, {"hash_normalization": "other"},
                       {"exceptions": [dict(record["exceptions"][0], doc_id="other")]},
                       {"exceptions": record["exceptions"] * 2}):
            with self.subTest(change=change):
                manifest.write_text(json.dumps({**record, **change}), encoding="utf-8")
                with self.assertRaises(kb.CorpusError):
                    self.load(validate_lengths=True)

    def test_expired_kb_only_version_can_have_different_operational_numbers(self):
        self.write("## 旧版到账\n银行卡通常三个工作日到账。", effective_to="2026-10-01T00:00:00+08:00")
        document = self.load()[0]
        self.assertFalse(document.in_force(DEMO_VIRTUAL_NOW))
        self.assertFalse(kb.KnowledgeBase((document,)).search("到账", as_of=DEMO_VIRTUAL_NOW).passages)

    def test_build_offline_cache_miss_names_document_line_and_writes_nothing(self):
        self.write("## 凭证说明\n" + "请保存订单与支付记录，截图需要清晰。" * 15)
        with mock.patch.object(kb.OllamaEmbedder, "embed", side_effect=AssertionError("live provider")):
            with self.assertRaisesRegex(kb.CorpusError, r"sample\.md:\d+: embedding cache miss"):
                kb.build_corpus(self.directory, cache_dir=self.cache, offline=True)
        self.assertFalse(self.cache.exists())

    def test_build_reuses_preseeded_cache_and_cli_has_success_summary(self):
        self.write("## 凭证说明\n" + "请保存订单与支付记录，截图需要清晰。" * 15)
        passages = kb.KnowledgeBase(self.load()).passages
        provider = mock.Mock()
        provider.embed.side_effect = lambda texts: [[1.0, 2.0] for _ in texts]
        cache = CachedEmbedder(self.cache, model=kb.EMBEDDING_MODEL, provider=provider)
        cache.embed([passage.search_text for passage in passages])
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.cache.rglob("*") if path.is_file()}
        output = io.StringIO()
        with mock.patch.object(kb.OllamaEmbedder, "embed", side_effect=AssertionError("live provider")):
            with contextlib.redirect_stdout(output):
                code = kb.main(["--corpus-dir", str(self.directory), "--cache-dir", str(self.cache), "--offline"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual((summary["documents"], summary["embedding_dimensions"], summary["offline"]), (1, 2, True))
        self.assertEqual(summary["eligibility_lint"], "passed")
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.cache.rglob("*") if path.is_file()})

    def test_cli_failure_is_nonzero_and_has_document_line(self):
        self.write("## 说明\n商品可以退货。")
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = kb.main(["--corpus-dir", str(self.directory), "--cache-dir", str(self.cache), "--offline"])
        self.assertEqual(code, 2)
        self.assertRegex(output.getvalue(), r"sample\.md:\d+: eligibility")

    def test_cache_corruption_diagnostic_maps_to_affected_document(self):
        first = self.write("## 凭证说明\n" + "请保存订单与支付记录，截图需要清晰。" * 15)
        text = first.read_text(encoding="utf-8")
        second = self.directory / "second.md"
        second.write_text(text.replace('"sample"', '"second"').replace("截图需要清晰", "资料需要完整"), encoding="utf-8")
        passages = kb.KnowledgeBase(self.load()).passages
        provider = mock.Mock()
        provider.embed.side_effect = lambda texts: [[1.0, 2.0] for _ in texts]
        cache = CachedEmbedder(self.cache, model=kb.EMBEDDING_MODEL, provider=provider)
        cache.embed([passage.search_text for passage in passages])
        damaged = next(passage for passage in passages if passage.document.doc_id == "second")
        cache.entry_path(damaged.search_text).write_text("{broken", encoding="utf-8")
        with self.assertRaises(kb.CorpusError) as raised:
            kb.build_corpus(self.directory, cache_dir=self.cache, offline=True)
        self.assertIn("second.md:" + str(damaged.start_line), str(raised.exception))
        self.assertNotIn("sample.md:", str(raised.exception))


class EmbeddingCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "cache"
        self.provider = mock.Mock()
        self.provider.embed.side_effect = lambda texts: [[float(len(text)), 1.0] for text in texts]

    def cached(self, *, model="bge-m3", offline=False):
        return CachedEmbedder(self.directory, model=model, provider=self.provider, offline=offline)

    def test_key_uses_model_and_exact_content_hash(self):
        cache = self.cached()
        self.assertEqual(cache.entry_path("中文").name, content_hash("中文") + ".json")
        self.assertEqual(cache.entry_path("中文").parent.name, content_hash("bge-m3"))
        self.assertNotEqual(cache.entry_path("中文"), self.cached(model="other").entry_path("中文"))
        self.assertNotEqual(cache.entry_path("中文"), cache.entry_path("中文 "))

    def test_deduplicates_inputs_and_reuses_across_process_objects(self):
        vectors = self.cached().embed(["甲", "乙乙", "甲"])
        self.provider.embed.assert_called_once_with(["甲", "乙乙"])
        self.assertEqual(vectors, [[1.0, 1.0], [2.0, 1.0], [1.0, 1.0]])
        self.assertEqual(self.cached().embed(["乙乙", "甲"]), [[2.0, 1.0], [1.0, 1.0]])
        self.assertEqual(self.provider.embed.call_count, 1)

    def test_only_missing_content_reaches_provider(self):
        self.cached().embed(["old"])
        self.cached().embed(["old", "new"])
        self.assertEqual(self.provider.embed.call_args.args[0], ["new"])

    def test_offline_miss_does_not_create_directory_or_call_provider(self):
        with self.assertRaises(EmbeddingCacheMiss) as raised:
            self.cached(offline=True).embed(["missing"])
        self.assertEqual(raised.exception.content_hashes, (content_hash("missing"),))
        self.provider.embed.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_offline_hit_does_not_write_or_call_provider(self):
        self.cached().embed(["hit"])
        self.provider.reset_mock()
        cache = self.cached(offline=True)
        with mock.patch.object(cache, "_write", side_effect=AssertionError("write")):
            self.assertEqual(cache.embed(["hit"]), [[3.0, 1.0]])
        self.provider.embed.assert_not_called()

    def test_model_change_requires_a_new_offline_entry(self):
        self.cached().embed(["same"])
        self.provider.reset_mock()
        with self.assertRaises(EmbeddingCacheMiss):
            self.cached(model="changed", offline=True).embed(["same"])
        self.provider.embed.assert_not_called()

    def test_malformed_or_identity_mismatched_cache_is_never_refreshed_offline(self):
        cache = self.cached()
        cache.embed(["entry"])
        path = cache.entry_path("entry")
        record = json.loads(path.read_text(encoding="utf-8"))
        for change in ({"model": "other"}, {"content_sha256": "wrong"}, {"schema": True},
                       {"embedding": [float("nan")]}, {"embedding": [True]}, {"embedding": []}):
            with self.subTest(change=change):
                path.write_text(json.dumps({**record, **change}), encoding="utf-8")
                self.provider.reset_mock()
                with self.assertRaises(kb.EmbeddingUnavailable):
                    self.cached(offline=True).embed(["entry"])
                self.provider.embed.assert_not_called()
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(kb.EmbeddingUnavailable, "malformed cache JSON"):
            self.cached(offline=True).embed(["entry"])

    def test_invalid_provider_batches_leave_no_entries(self):
        for result in ([], [[True]], [[float("inf")]], [[1.0], [1.0, 2.0]]):
            with self.subTest(result=result):
                self.provider.embed.side_effect = None
                self.provider.embed.return_value = result
                with self.assertRaises(kb.EmbeddingUnavailable):
                    self.cached().embed(["one", "two"])
                self.assertFalse(self.directory.exists())

    def test_dimensions_cannot_change_when_extending_same_model_cache(self):
        cache = self.cached()
        cache.embed(["old"])
        self.provider.embed.side_effect = lambda texts: [[1.0] for _ in texts]
        with self.assertRaisesRegex(kb.EmbeddingUnavailable, "dimensions differ"):
            cache.embed(["old", "new"])
        self.assertFalse(cache.entry_path("new").exists())

    def test_offline_product_miss_falls_back_without_provider_or_writes(self):
        with tempfile.TemporaryDirectory() as corpus_directory:
            path = Path(corpus_directory) / "faq.md"
            meta = {"doc_id": "faq", "title": "支付记录", "doc_type": "faq", "scope": [],
                    "effective_from": "2026-01-01T00:00:00+08:00", "effective_to": None,
                    "version": "1", "restates": None}
            path.write_text("---\n" + json.dumps(meta) + "\n---\n## 记录\n支付记录请保存。", encoding="utf-8")
            base = kb.KnowledgeBase(kb.load_corpus(path.parent), embedder=self.cached(offline=True))
            result = base.search("支付记录请保存", as_of=DEMO_VIRTUAL_NOW)
        self.assertEqual((result.mode, result.fallback), (kb.MODE_BM25, kb.FALLBACK_EMBEDDING_UNAVAILABLE))
        self.assertTrue(result.passages)
        self.provider.embed.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_environment_offline_has_no_live_provider(self):
        with mock.patch.dict("os.environ", {"AFTERSALES_KB_OFFLINE": "1",
                                            "AFTERSALES_KB_EMBED_CACHE": str(self.directory)}):
            embedder = kb.environment_embedder()
        self.assertTrue(embedder.offline)
        self.assertIsNone(embedder.provider)
        self.assertEqual(embedder.directory, self.directory)


if __name__ == "__main__":
    unittest.main()
