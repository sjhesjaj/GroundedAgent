"""Stage 4.3.6: sealed holdout metadata and the precommitted unseal script.

Every opening test runs against a throwaway git repository and a synthetic
20-case holdout built here. Nothing reads, locates, or opens the real sealed
holdout; the real unseal is never executed.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import unseal_v2_holdout as unseal_mod

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / unseal_mod.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
SCRIPT = Path(unseal_mod.__file__).read_text(encoding="utf-8")
ARCHETYPES = ["A%02d" % n for n in range(1, 21)]
FINALS = ["answer", "refuse", "handoff", "boundary"]

SAFE_METADATA = {
    "schema": "v2-sealed-holdout-manifest/1",
    "status": "sealed",
    "sealed_against_repo_commit": "9312d31ab57be81a91ee16922ca904792b8c4709",
    "input_bundle_digest": "7b3d4684cf3fa7425d25a6e842f47876392f6b0c095592f8371d5aa0cad61fc8",
    "input_file_count": 17,
    "holdout_sha256": "0d312305e62ffc3bf4c73cf3ee0715a8cbe1b910d44183b9bb8abf9cc2d88e8a",
    "seal_receipt_sha256": "1c899e4f8d54a168aef77489f9450f1d08f4166949a9164d935412fbae9ff002",
    "case_count": 20,
    "archetype_counts": {a: 1 for a in ARCHETYPES},
    "contract_validation": {"all_cases_valid": True, "error_count": 0},
    "fixture_integrity": {"all_cases_valid": True},
    "coverage": {"all_required_final_values_represented": True, "both_personas_represented": True,
                 "min_distinct_virtual_now_met": True},
    "expected_action_all_null": True,
    "expected_final_state_all_null": True,
    "author_context": {"fresh_isolated_context": True, "frozen_bundle_only": True,
                       "implementation_visible": False, "dev_validation_visible": False,
                       "failure_analysis_visible": False, "agent_runs": 0,
                       "external_sources_used": False},
    "isolation_note": "Process/context isolation, not filesystem access control. The hash proves "
                      "immutability, not that the file was never readable.",
}

PATH_LIKE = re.compile(r"[A-Za-z]:[\\/]|\\|(^|\s)[.~]?/|/Users/|/home/|\.(json|md|txt|jsonl)\b")
FORBIDDEN_KEY_TOKENS = ("path", "filename", "directory", "folder", "location", "case_id", "user_text", "persona_counts",
                        "final_counts", "virtual_now_values", "order", "sku", "tracking", "evidence",
                        "fault", "label")


def synthetic_case(index: int) -> dict:
    """Structurally valid, deliberately contentless."""
    return {
        "case_id": "synthetic-%02d" % (index + 1),
        "archetype": ARCHETYPES[index],
        "initial_state": {"trusted_context": {"persona_id": "demo-a" if index % 2 == 0 else "demo-b"},
                          "faults": []},
        "virtual_now": "2026-11-15T10:00:00+08:00" if index < 10 else "2031-11-15T10:00:00+08:00",
        "user_turns": [{"text": "synthetic"}],
        "expected_capabilities": {"required": ["search_after_sales_policy"], "forbidden": []},
        "expected_evidence": {"all_of": [], "any_of": [], "forbidden": []},
        "expected_answerability": {"final": FINALS[index % 4], "clarify": {"required": False, "slots": []}},
        "expected_action": None,
        "expected_final_state": None,
    }


def synthetic_receipt(holdout_sha: str) -> dict:
    """Exactly the frozen v2-sealed-holdout-receipt/1 contract."""
    return {
        "schema": "v2-sealed-holdout-receipt/1",
        "freeze_merge_commit": SAFE_METADATA["sealed_against_repo_commit"],
        "input_bundle_digest": SAFE_METADATA["input_bundle_digest"],
        "input_file_count": 17,
        "holdout_sha256": holdout_sha,
        "case_count": 20,
        "archetype_counts": {a: 1 for a in ARCHETYPES},
        "contract_validation": {"all_cases_valid": True, "error_count": 0},
        "fixture_integrity": {"all_cases_valid": True},
        "coverage": copy.deepcopy(SAFE_METADATA["coverage"]),
        "expected_action_all_null": True,
        "expected_final_state_all_null": True,
        "agent_runs": 0,
        "external_sources_used": False,
    }


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.autocrlf=false", *args],
                          check=True, capture_output=True, text=True).stdout


class Sandbox:
    """A throwaway repo holding the 17 frozen author inputs, their input
    manifest and a synthetic seal (synthetic holdout and receipt hashes)."""

    template: Path | None = None

    @classmethod
    def build_template(cls, tmp: Path) -> None:
        """Committed frozen inputs and manifests; copied per test."""
        root = tmp / "template"
        inputs = json.loads((ROOT / unseal_mod.INPUT_MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        for relative in [f["path"] for f in inputs["files"]] + [
                unseal_mod.INPUT_MANIFEST_RELATIVE, unseal_mod.MANIFEST_RELATIVE]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        git(root, "init", "-q")
        git(root, "config", "core.autocrlf", "false")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "template")
        cls.template = root

    def __init__(self, tmp: Path, *, cases=None, receipt_edit=None, holdout_bytes=None):
        self.root = tmp / "repo"
        shutil.copytree(self.template, self.root)
        cases = [synthetic_case(i) for i in range(20)] if cases is None else cases
        self.holdout_bytes = (holdout_bytes if holdout_bytes is not None
                              else json.dumps(cases, ensure_ascii=False, indent=2).encode("utf-8"))
        receipt = synthetic_receipt(unseal_mod.sha256_hex(self.holdout_bytes))
        if receipt_edit:
            receipt_edit(receipt)
        self.receipt_bytes = json.dumps(receipt, ensure_ascii=False, indent=2).encode("utf-8")
        self.pinned = dict(unseal_mod.PINNED,
                           holdout_sha256=unseal_mod.sha256_hex(self.holdout_bytes),
                           seal_receipt_sha256=unseal_mod.sha256_hex(self.receipt_bytes))
        manifest = dict(copy.deepcopy(MANIFEST), **self.pinned)
        (self.root / unseal_mod.MANIFEST_RELATIVE).write_text(json.dumps(manifest, indent=2) + "\n",
                                                              encoding="utf-8")
        git(self.root, "commit", "-q", "-am", "seal")

        sealed = tmp / "sealed"
        sealed.mkdir()
        self.holdout = sealed / "h.bin"
        self.receipt = sealed / "r.bin"
        self.holdout.write_bytes(self.holdout_bytes)
        self.receipt.write_bytes(self.receipt_bytes)

    def unseal(self, holdout=None, receipt=None):
        return unseal_mod.unseal(holdout or self.holdout, receipt or self.receipt,
                                 root=self.root, pinned=self.pinned)

    def eval_dir(self) -> list[str]:
        return sorted(p.name for p in (self.root / "eval/v2").iterdir())


_TEMPLATE_DIR = None


def setUpModule():
    global _TEMPLATE_DIR
    _TEMPLATE_DIR = Path(tempfile.mkdtemp())
    Sandbox.build_template(_TEMPLATE_DIR)


def tearDownModule():
    shutil.rmtree(_TEMPLATE_DIR, True)


class ManifestTests(unittest.TestCase):
    def test_manifest_is_exactly_the_safe_metadata(self):
        self.assertEqual(MANIFEST, SAFE_METADATA)

    def test_manifest_is_pinned_by_the_script(self):
        for key, value in unseal_mod.PINNED.items():
            self.assertEqual(MANIFEST[key], value)

    def test_no_path_like_field(self):
        for key, value in _flatten_items(MANIFEST):
            with self.subTest(key=key):
                if isinstance(value, str) and key != "isolation_note":
                    self.assertIsNone(PATH_LIKE.search(value))
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]|\\|/Users/|/home/",
                                    (ROOT / unseal_mod.MANIFEST_RELATIVE).read_text(encoding="utf-8")))

    def test_no_forbidden_content_metadata(self):
        for key, _ in _flatten_items(MANIFEST):
            leaf = key.split(".")[-1].lower()
            for token in FORBIDDEN_KEY_TOKENS:
                with self.subTest(key=key, token=token):
                    self.assertNotIn(token, leaf)
        self.assertEqual(set(MANIFEST["archetype_counts"]), set(ARCHETYPES))

    def test_sealed_commit_is_not_the_input_base_commit(self):
        inputs = json.loads((ROOT / "eval/v2/holdout-input.manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(MANIFEST["sealed_against_repo_commit"], inputs["base_commit"])
        self.assertEqual(MANIFEST["input_bundle_digest"], inputs["content_digest"])
        self.assertEqual(MANIFEST["input_file_count"], len(inputs["files"]))


def _flatten_items(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            path = prefix + "." + key if prefix else key
            yield path, value
            yield from _flatten_items(value, path)


class UnsealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def assert_refused_cleanly(self, box, **kwargs):
        before = box.eval_dir()
        with self.assertRaises(unseal_mod.UnsealRefused):
            box.unseal(**kwargs)
        self.assertEqual(box.eval_dir(), before)
        self.assertFalse((box.root / unseal_mod.HOLDOUT_DESTINATION).exists())
        self.assertFalse((box.root / unseal_mod.RECEIPT_DESTINATION).exists())

    def test_correct_synthetic_unseal_succeeds_with_exact_bytes(self):
        box = Sandbox(self.tmp)
        result = box.unseal()
        self.assertEqual(result, {"cases": 20, "destinations": [unseal_mod.HOLDOUT_DESTINATION,
                                                                unseal_mod.RECEIPT_DESTINATION]})
        self.assertEqual((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes(), box.holdout_bytes)
        self.assertEqual((box.root / unseal_mod.RECEIPT_DESTINATION).read_bytes(), box.receipt_bytes)
        self.assertEqual(unseal_mod.sha256_hex((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes()),
                         box.pinned["holdout_sha256"])
        self.assertFalse([n for n in box.eval_dir() if n.endswith(".tmp")])
        # No automatic commit: the opened files are left for a person to commit.
        self.assertIn("?? eval/v2/holdout.json", git(box.root, "status", "--porcelain"))

    def test_non_canonical_bytes_preserved(self):
        raw = ("﻿" + json.dumps([synthetic_case(i) for i in range(20)], ensure_ascii=True)
               + "\r\n").encode("utf-8")
        box = Sandbox(self.tmp, holdout_bytes=raw)
        box.unseal()
        self.assertEqual((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes(), raw)

    def test_wrong_holdout_hash_rejected(self):
        box = Sandbox(self.tmp)
        box.holdout.write_bytes(box.holdout_bytes + b" ")
        self.assert_refused_cleanly(box)

    def test_wrong_receipt_hash_rejected(self):
        box = Sandbox(self.tmp)
        box.receipt.write_bytes(box.receipt_bytes.replace(b"true", b"false", 1))
        self.assert_refused_cleanly(box)

    # Direct checks: same verification code, no git repository needed.
    def holdout_rejected(self, cases=None, raw=None):
        raw = raw if raw is not None else json.dumps(cases).encode("utf-8")
        manifest = dict(copy.deepcopy(MANIFEST), holdout_sha256=unseal_mod.sha256_hex(raw))
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.verify_holdout(raw, manifest, Sandbox.template)

    def receipt_rejected(self, edit):
        receipt = synthetic_receipt(MANIFEST["holdout_sha256"])
        edit(receipt)
        raw = json.dumps(receipt).encode("utf-8")
        manifest = dict(copy.deepcopy(MANIFEST), seal_receipt_sha256=unseal_mod.sha256_hex(raw))
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.verify_receipt(raw, manifest)

    def test_receipt_manifest_mismatch_rejected(self):
        # End to end once: refusal leaves nothing behind.
        self.assert_refused_cleanly(Sandbox(self.tmp, receipt_edit=lambda r: r.__setitem__("case_count", 21)))
        edits = [
            lambda r: r.__setitem__("case_count", 21),
            lambda r: r.__setitem__("input_bundle_digest", "0" * 64),
            lambda r: r.__setitem__("freeze_merge_commit", "f9024050bb68260b6c9fb7fdea906384dfd606cf"),
            lambda r: r.__setitem__("agent_runs", 1),
            lambda r: r.__setitem__("external_sources_used", True),
            lambda r: r["archetype_counts"].__setitem__("A21", 1),
            lambda r: r["archetype_counts"].pop("A07"),
            lambda r: r["contract_validation"].__setitem__("error_count", 2),
            lambda r: r["fixture_integrity"].__setitem__("all_cases_valid", False),
            lambda r: r["coverage"].__setitem__("both_personas_represented", False),
            lambda r: r.__setitem__("expected_action_all_null", False),
            lambda r: r.pop("holdout_sha256"),
            lambda r: r.__setitem__("input_file_count", True),
            lambda r: r.__setitem__("case_count", 20.0),
            lambda r: r["archetype_counts"].__setitem__("A21", 0),
            lambda r: r["coverage"].__setitem__("extra_flag", True),
        ]
        for i, edit in enumerate(edits):
            with self.subTest(edit=i):
                self.receipt_rejected(edit)

    def test_unedited_receipt_matches_manifest(self):
        raw = json.dumps(synthetic_receipt(MANIFEST["holdout_sha256"])).encode("utf-8")
        manifest = dict(copy.deepcopy(MANIFEST), seal_receipt_sha256=unseal_mod.sha256_hex(raw))
        unseal_mod.verify_receipt(raw, manifest)

    def test_exact_receipt_schema_accepted_end_to_end(self):
        box = Sandbox(self.tmp)
        self.assertEqual(set(json.loads(box.receipt_bytes)), unseal_mod.RECEIPT_KEYS)
        box.unseal()

    def test_wrong_receipt_schema_rejected(self):
        for value in ("v2-sealed-holdout-receipt/2", "v2-sealed-holdout-manifest/1", None):
            with self.subTest(value=value):
                self.receipt_rejected(lambda r: r.__setitem__("schema", value))
        self.receipt_rejected(lambda r: r.pop("schema"))

    def test_missing_freeze_merge_commit_rejected(self):
        self.receipt_rejected(lambda r: r.pop("freeze_merge_commit"))

    def test_old_commit_aliases_rejected(self):
        for alias in ("sealed_against_repo_commit", "freeze_repo_commit"):
            with self.subTest(alias=alias):
                self.receipt_rejected(lambda r: r.__setitem__(alias, r.pop("freeze_merge_commit")))
                self.receipt_rejected(lambda r: r.__setitem__(alias, r["freeze_merge_commit"]))

    def test_nested_wrapper_rejected(self):
        def wrap_all(receipt):
            inner = dict(receipt)
            receipt.clear()
            receipt.update(schema=inner["schema"], receipt=inner)

        def move_author_fields(receipt):
            receipt["author_context"] = {"agent_runs": receipt.pop("agent_runs"),
                                         "external_sources_used": receipt.pop("external_sources_used")}

        def extra_top_level(receipt):
            receipt["seal"] = {"agent_runs": 0}

        for edit in (wrap_all, move_author_fields, extra_top_level):
            with self.subTest(edit=edit.__name__):
                self.receipt_rejected(edit)

    def test_dirty_workspace_rejected(self):
        box = Sandbox(self.tmp)
        (box.root / "stray.txt").write_text("x", encoding="utf-8")
        self.assert_refused_cleanly(box)
        (box.root / "stray.txt").unlink()
        (box.root / "eval/v2/spec/slots.json").write_text("{}", encoding="utf-8")
        self.assert_refused_cleanly(box)

    def test_destination_already_exists_rejected(self):
        box = Sandbox(self.tmp)
        (box.root / unseal_mod.HOLDOUT_DESTINATION).write_bytes(b"[]")
        git(box.root, "add", "-A")
        git(box.root, "commit", "-q", "-m", "x")
        with self.assertRaises(unseal_mod.UnsealRefused):
            box.unseal()
        self.assertEqual((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes(), b"[]")
        self.assertFalse((box.root / unseal_mod.RECEIPT_DESTINATION).exists())

    def test_second_run_rejected(self):
        box = Sandbox(self.tmp)
        box.unseal()
        git(box.root, "add", "-A")
        git(box.root, "commit", "-q", "-m", "open holdout")
        with self.assertRaises(unseal_mod.UnsealRefused):
            box.unseal()
        # Deleting the opened copy does not re-arm the gate: history remembers it.
        git(box.root, "rm", "-q", unseal_mod.HOLDOUT_DESTINATION, unseal_mod.RECEIPT_DESTINATION)
        git(box.root, "commit", "-q", "-m", "remove")
        self.assert_refused_cleanly(box)

    def test_wrong_manifest_rejected(self):
        box = Sandbox(self.tmp)
        path = box.root / unseal_mod.MANIFEST_RELATIVE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(dict(manifest, status="opened")), encoding="utf-8")
        git(box.root, "commit", "-q", "-am", "edit")
        self.assert_refused_cleanly(box)
        git(box.root, "rm", "-q", unseal_mod.MANIFEST_RELATIVE)
        git(box.root, "commit", "-q", "-m", "drop manifest")
        self.assert_refused_cleanly(box)
        loose = self.tmp / "loose"
        (loose / "eval/v2").mkdir(parents=True)
        for variant in (dict(manifest, schema="other/1"), dict(manifest, holdout_sha256="0" * 64), []):
            with self.subTest(variant=str(variant)[:40]):
                (loose / unseal_mod.MANIFEST_RELATIVE).write_text(json.dumps(variant), encoding="utf-8")
                with self.assertRaises(unseal_mod.UnsealRefused):
                    unseal_mod.load_manifest(loose, box.pinned)

    def test_malformed_json_rejected(self):
        self.assert_refused_cleanly(Sandbox(self.tmp, holdout_bytes=b"[{not json"))
        self.holdout_rejected(raw=b"\xff\xfe[]")

    def test_not_an_array_rejected(self):
        self.holdout_rejected(raw=b"{}")

    def test_contract_invalid_case_rejected(self):
        cases = [synthetic_case(i) for i in range(20)]
        cases[5]["notes"] = "extra key"
        self.assert_refused_cleanly(Sandbox(self.tmp, cases=cases))

    def test_cross_field_invalid_case_rejected(self):
        cases = [synthetic_case(i) for i in range(20)]
        cases[3]["expected_capabilities"]["forbidden"] = ["search_after_sales_policy"]
        self.holdout_rejected(cases)

    def test_wrong_case_count_rejected(self):
        self.holdout_rejected([synthetic_case(i) for i in range(19)])
        self.holdout_rejected([synthetic_case(i) for i in range(20)] + [synthetic_case(0)])

    def test_incorrect_archetype_distribution_rejected(self):
        dup = [synthetic_case(i) for i in range(20)]
        dup[19]["archetype"] = "A01"
        self.assert_refused_cleanly(Sandbox(self.tmp, cases=dup))
        stage6 = [synthetic_case(i) for i in range(20)]
        stage6[19]["archetype"] = "A21"
        self.holdout_rejected(stage6)

    def test_duplicate_case_id_rejected(self):
        cases = [synthetic_case(i) for i in range(20)]
        cases[1]["case_id"] = cases[0]["case_id"]
        self.holdout_rejected(cases)

    def test_missing_coverage_rejected(self):
        def same(field, value):
            cases = [synthetic_case(i) for i in range(20)]
            for case in cases:
                field(case, value)
            return cases
        variants = [
            same(lambda c, v: c["initial_state"]["trusted_context"].__setitem__("persona_id", v), "demo-a"),
            same(lambda c, v: c.__setitem__("virtual_now", v), "2026-11-15T10:00:00+08:00"),
            same(lambda c, v: c["expected_answerability"].__setitem__("final", v), "answer"),
        ]
        for i, cases in enumerate(variants):
            with self.subTest(variant=i):
                self.holdout_rejected(cases)

    def test_valid_synthetic_holdout_passes_direct_check(self):
        raw = json.dumps([synthetic_case(i) for i in range(20)]).encode("utf-8")
        manifest = dict(copy.deepcopy(MANIFEST), holdout_sha256=unseal_mod.sha256_hex(raw))
        self.assertEqual(len(unseal_mod.verify_holdout(raw, manifest, Sandbox.template)), 20)

    def loose_copy(self) -> Path:
        """The template tree without running git, for direct input checks."""
        root = self.tmp / "loose"
        shutil.copytree(Sandbox.template, root, ignore=shutil.ignore_patterns(".git"))
        return root

    def inputs_rejected(self, root: Path):
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.verify_frozen_inputs(root, MANIFEST)

    def test_unchanged_frozen_inputs_pass(self):
        unseal_mod.verify_frozen_inputs(self.loose_copy(), MANIFEST)
        unseal_mod.verify_frozen_inputs(ROOT, MANIFEST)

    def test_frozen_input_file_modified_rejected(self):
        root = self.loose_copy()
        checker = root / "eval/v2/case_contract.py"
        checker.write_bytes(checker.read_bytes() + b"\n# changed after sealing\n")
        self.inputs_rejected(root)
        root2 = self.tmp / "e2e"
        root2.mkdir()
        box = Sandbox(root2)
        policy = box.root / "policy_sources/standard-return.md"
        policy.write_bytes(policy.read_bytes().replace("7".encode(), "9".encode()))
        git(box.root, "commit", "-q", "-am", "edit a frozen input")
        self.assert_refused_cleanly(box)

    def test_input_manifest_digest_modified_rejected(self):
        root = self.loose_copy()
        path = root / unseal_mod.INPUT_MANIFEST_RELATIVE
        inputs = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(dict(inputs, content_digest="0" * 64)), encoding="utf-8")
        self.inputs_rejected(root)

    def test_input_manifest_hash_entry_modified_rejected(self):
        root = self.loose_copy()
        path = root / unseal_mod.INPUT_MANIFEST_RELATIVE
        inputs = json.loads(path.read_text(encoding="utf-8"))
        inputs["files"][3]["sha256"] = "0" * 64
        path.write_text(json.dumps(inputs), encoding="utf-8")
        self.inputs_rejected(root)
        # Recomputing the digest over the forged entry still fails: it no longer
        # equals the sealed digest, and the file no longer matches its entry.
        inputs["content_digest"] = unseal_mod.input_content_digest(inputs["files"])
        path.write_text(json.dumps(inputs), encoding="utf-8")
        self.inputs_rejected(root)

    def test_frozen_input_missing_rejected(self):
        root = self.loose_copy()
        (root / "system_fixtures/aftersales_demo_seed.sql").unlink()
        self.inputs_rejected(root)

    def test_input_manifest_structure_rejected(self):
        root = self.loose_copy()
        path = root / unseal_mod.INPUT_MANIFEST_RELATIVE
        original = json.loads(path.read_text(encoding="utf-8"))
        variants = [dict(original, schema="other/1"), dict(original, files=original["files"][:-1]),
                    dict(original, files=[dict(original["files"][0], path="../x")] + original["files"][1:])]
        for variant in variants:
            path.write_text(json.dumps(variant), encoding="utf-8")
            self.inputs_rejected(root)
        path.unlink()
        self.inputs_rejected(root)

    def test_line_endings_do_not_affect_input_hashes(self):
        root = self.loose_copy()
        spec = root / "docs/v2/holdout-domain-spec.md"
        data = spec.read_bytes().replace(b"\r\n", b"\n")
        spec.write_bytes(data.replace(b"\n", b"\r\n"))
        unseal_mod.verify_frozen_inputs(root, MANIFEST)

    def test_interrupted_write_leaves_nothing(self):
        box = Sandbox(self.tmp)
        real_replace = unseal_mod.os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("simulated interruption")
            return real_replace(src, dst)

        before = box.eval_dir()
        with patch.object(unseal_mod.os, "replace", flaky):
            with self.assertRaises(OSError):
                box.unseal()
        self.assertEqual(len(calls), 2)
        self.assertEqual(box.eval_dir(), before)
        self.assertEqual(git(box.root, "status", "--porcelain"), "")

    def test_cli_success_output_and_refusal_hide_paths(self):
        box = Sandbox(self.tmp)
        real = unseal_mod.unseal
        out, err = io.StringIO(), io.StringIO()
        with patch.object(unseal_mod, "unseal",
                          lambda h, r: real(h, r, root=box.root, pinned=box.pinned)):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(unseal_mod.main([str(box.holdout), str(box.receipt)]), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "holdout hash verified", "receipt hash verified", "20 cases validated",
            "wrote eval/v2/holdout.json", "wrote eval/v2/holdout.receipt.json",
            "Commit the opened holdout immediately and record that commit SHA."])
        self.assertNotIn(str(box.holdout), out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with patch.object(unseal_mod, "unseal",
                          lambda h, r: real(h, r, root=box.root, pinned=box.pinned)):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(unseal_mod.main([str(box.holdout), str(box.receipt)]), 1)
        self.assertIn("refused", err.getvalue())
        self.assertNotIn(str(self.tmp), err.getvalue() + out.getvalue())


class ScriptSafetyTests(unittest.TestCase):
    def test_no_hardcoded_external_path_or_discovery(self):
        for pattern in (r"[A-Za-z]:\\\\", r"[A-Za-z]:/", r"/Users/", r"/home/",
                        r"expanduser", r"Path\.home", r"os\.environ", r"getenv", r"\bglob\(",
                        r"rglob", r"os\.walk", r"scandir", r"listdir", r"iterdir"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, SCRIPT))

    def test_no_agent_llm_or_network_dependency(self):
        tree = ast.parse(SCRIPT)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        imported.discard("__future__")
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        for banned in ("socket", "urllib", "http", "ssl", "requests", "httpx", "llm_provider",
                       "orchestration", "agent", "chat_orchestration", "rag", "aftersales"):
            self.assertNotIn(banned, imported)

    def test_receipt_cannot_choose_destinations(self):
        self.assertEqual(unseal_mod.HOLDOUT_DESTINATION, "eval/v2/holdout.json")
        self.assertEqual(unseal_mod.RECEIPT_DESTINATION, "eval/v2/holdout.receipt.json")
        source = ast.get_source_segment(SCRIPT, next(
            n for n in ast.walk(ast.parse(SCRIPT)) if isinstance(n, ast.FunctionDef) and n.name == "unseal"))
        self.assertIn("(HOLDOUT_DESTINATION, holdout_bytes), (RECEIPT_DESTINATION, receipt_bytes)", source)

    def test_sealed_files_are_not_in_the_repository(self):
        self.assertFalse((ROOT / unseal_mod.HOLDOUT_DESTINATION).exists())
        self.assertFalse((ROOT / unseal_mod.RECEIPT_DESTINATION).exists())


if __name__ == "__main__":
    unittest.main()
