"""Stage 6.4B: the sealed Stage 6 holdout metadata and the precommitted unseal tool.

Every opening test runs against a throwaway git repository holding the 27
frozen author inputs and a synthetic seal. The synthetic holdout is made of
evaluator fixture cases (tests/stage6_eval_support.py) with fresh ids, and its
receipt has exactly the frozen receipt tool's shape and serialization. The
positive paths stub only the distribution plan, as the author-bundle tests do:
this session does not construct a dataset that meets the full plan, and the
plan-invalid test runs the real plan. Nothing here reads, locates or opens the
real sealed holdout or its receipt; the real unseal is never executed.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eval.v2 import stage6_dataset_receipt as receipt_tool
from tools import unseal_v2_holdout as stage5_unseal
from tools import unseal_v2_stage6_holdout as unseal_mod

from tests import stage6_eval_support as support

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / unseal_mod.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
INPUTS = json.loads((ROOT / unseal_mod.INPUT_MANIFEST_RELATIVE).read_text(encoding="utf-8"))
SCRIPT = Path(unseal_mod.__file__).read_text(encoding="utf-8")
FREEZE = "f27ee9583a971725b33d579a3d8fceba24b7d768"
DIGEST = "15ac3593ce55a0b7d04d4f3522ebabc370bf57c98b1dcdef89472e34e9d5d371"

# Exactly the isolated author's report; nothing else.
SAFE_METADATA = {
    "schema": "v2-stage6-sealed-holdout-manifest/1",
    "status": "sealed",
    "sealed_against_repo_commit": FREEZE,
    "input_bundle_digest": DIGEST,
    "input_file_count": 27,
    "holdout_sha256": "64925a4d8e7d66f2e150a9a8f2286b4bfdcac74e077448a990798ab6d62d3057",
    "author_receipt_sha256": "89e5834fcc1efa8017da54b2dd957002af8861a496d7c58bb6144aeb3915d46b",
    "case_count": 25,
    "scenario_counts": {name: 1 for name in (
        "approval_rejected", "approval_state_change", "claimed_privileged_identity", "consult_no_action",
        "direct_prompt_injection", "duplicate_submission", "exchange_auto_execute", "exchange_window_closed",
        "existing_active_case", "guard_denies_on_resume", "handoff_ticket_create", "indirect_prompt_injection",
        "inventory_unavailable", "missing_data", "non_returnable", "policy_unavailable",
        "repeated_approve_resume", "restart_resume", "return_approval_execute", "return_handoff_required",
        "return_waiting_approval", "return_window_closed", "state_conflict", "state_read_error",
        "wrong_customer_resource")},
    "archetype_counts": {"A01": 2, "A02": 1, "A03": 3, "A04": 1, "A05": 1, "A06": 1, "A07": 1, "A08": 1,
                         "A10": 2, "A11": 1, "A12": 1, "A13": 1, "A14": 2, "A18": 1, "A19": 1, "A21": 2,
                         "A22": 2, "A23": 1},
    "final_counts": {"action": 21, "answer": 1, "boundary": 1, "handoff": 1, "refuse": 1},
    "final_status_counts": {"DENIED": 10, "EXECUTED": 6, "FAILED": 1, "REJECTED": 1, "STALE": 1,
                            "WAITING_APPROVAL": 2},
    "persona_counts": {"demo-a": 12, "demo-b": 13},
    "distinct_virtual_now": 6,
    "contract_validation": {"all_cases_valid": True, "error_count": 0},
    "plan_validation": {"all_rules_met": True, "error_count": 0},
    "author_context": {"fresh_isolated_context": True, "frozen_bundle_only": True,
                       "implementation_visible": False, "other_datasets_visible": False,
                       "failure_analysis_visible": False, "agent_runs": 0, "oracle_runs": 0,
                       "external_sources_used": False},
    "isolation_note": "Process/context isolation, not filesystem access control. Hashes prove immutability "
                      "of the sealed bytes, not that filesystem permissions made the files unreadable.",
}

PATH_LIKE = re.compile(r"[A-Za-z]:[\\/]|\\|(^|\s)[.~]?/|/Users/|/home/|\.(json|md|txt|jsonl|py)\b")
FORBIDDEN_KEY_TOKENS = ("path", "filename", "directory", "folder", "location", "case_id", "user_text",
                        "user_turns", "args", "evidence", "final_state", "row", "order", "sku", "tracking",
                        "fault", "label")
LEAK_MARKERS = ("synthetic-", "ORD-", "OI-", "SKU-", "create_return", "create_exchange")

BUILDERS = (support.exchange_case, support.handoff_case, support.waiting_return_case,
            support.approved_return_case, support.consult_case, support.deny_return_case)
REAL_LOAD_CHECKER = unseal_mod.load_checker


def sha(data: bytes) -> str:
    return unseal_mod.sha256_hex(data)


def synthetic_cases(count: int = 25) -> list:
    """Evaluator fixture cases with fresh ids: contract-valid, deliberately not a plan-valid split."""
    return [BUILDERS[i % len(BUILDERS)]("synthetic-%02d" % (i + 1)) for i in range(count)]


def dataset_bytes(cases: list) -> bytes:
    return json.dumps(cases, ensure_ascii=False, indent=2).encode("utf-8")


def distributions(cases: list) -> dict:
    """The test's own count of the safe distributions."""
    def counts(values):
        out: dict = {}
        for value in values:
            out[value] = out.get(value, 0) + 1
        return dict(sorted(out.items()))
    return {
        "scenario_counts": counts(c["scenario"] for c in cases),
        "archetype_counts": counts(c["archetype"] for c in cases),
        "final_counts": counts(c["expected_answerability"]["final"] for c in cases),
        "final_status_counts": counts(c["expected_action"]["final_status"] for c in cases
                                      if c["expected_action"] is not None),
        "persona_counts": counts(c["initial_state"]["trusted_context"]["persona_id"] for c in cases),
        "distinct_virtual_now": len({c["virtual_now"] for c in cases}),
    }


def receipt_dict(holdout: bytes, dist: dict) -> dict:
    """The frozen v2-stage6-dataset-receipt/1 shape for a synthetic holdout."""
    return {
        "schema": "v2-stage6-dataset-receipt/1", "split": "holdout", "freeze_merge_commit": FREEZE,
        "input_bundle_digest": DIGEST, "input_file_count": 27, "dataset_sha256": sha(holdout),
        "case_count": 25, **copy.deepcopy(dist),
        "contract_validation": {"all_cases_valid": True, "error_count": 0},
        "plan_validation": {"all_rules_met": True, "error_count": 0},
        "author_context": dict(receipt_tool.ATTESTATION),
    }


def seal_for(cases=None, *, holdout_bytes=None, seal=None, receipt_edit=None,
             serializer=receipt_tool.receipt_bytes) -> SimpleNamespace:
    """A synthetic seal: holdout bytes, receipt bytes, pinned values and manifest."""
    cases = synthetic_cases() if cases is None else cases
    holdout = dataset_bytes(cases) if holdout_bytes is None else holdout_bytes
    dist = dict(distributions(cases), **(seal or {}))
    receipt = receipt_dict(holdout, dist)
    if receipt_edit:
        receipt_edit(receipt)
    receipt_raw = serializer(receipt)
    pinned = dict(unseal_mod.PINNED, holdout_sha256=sha(holdout), author_receipt_sha256=sha(receipt_raw))
    manifest = copy.deepcopy(SAFE_METADATA)
    manifest.update(pinned)
    manifest.update(copy.deepcopy(dist))
    return SimpleNamespace(holdout=holdout, receipt=receipt_raw, pinned=pinned, manifest=manifest)


class StubPlan:
    """The frozen case contract with only the distribution plan stubbed."""

    def __init__(self, real):
        self.real = real

    def case_errors(self, case):
        return self.real.case_errors(case)

    def schema_errors(self, case):
        return self.real.schema_errors(case)

    @staticmethod
    def dataset_plan_errors(cases, split):
        return []


def stub_checker(root: Path) -> StubPlan:
    return StubPlan(REAL_LOAD_CHECKER(root))


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "core.autocrlf=false", *args],
                          check=True, capture_output=True, text=True).stdout


def remove_tree(path: Path) -> None:
    def retry(function, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    try:
        shutil.rmtree(path, onexc=retry)
    except OSError:
        pass


def pycache_entries(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if ".git" not in p.relative_to(root).parts
                  and (p.name == "__pycache__" or p.suffix == ".pyc"))


class Sandbox:
    """A throwaway repo with the 27 frozen inputs, their manifest and a synthetic seal."""

    template: Path | None = None

    @classmethod
    def build_template(cls, tmp: Path) -> None:
        root = tmp / "template"
        for relative in [f["path"] for f in INPUTS["files"]] + [
                unseal_mod.INPUT_MANIFEST_RELATIVE, unseal_mod.MANIFEST_RELATIVE]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        git(root, "init", "-q")
        git(root, "config", "core.autocrlf", "false")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "freeze")
        cls.template = root

    def __init__(self, tmp: Path, **seal_options):
        self.root = tmp / "repo"
        shutil.copytree(self.template, self.root)
        self.seal = seal_for(**seal_options)
        self.holdout_bytes, self.receipt_bytes = self.seal.holdout, self.seal.receipt
        self.pinned = self.seal.pinned
        (self.root / unseal_mod.MANIFEST_RELATIVE).write_text(
            json.dumps(self.seal.manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        git(self.root, "commit", "-q", "-am", "seal")
        sealed = tmp / "sealed"
        sealed.mkdir()
        self.holdout = sealed / "h.bin"
        self.receipt = sealed / "r.bin"
        self.holdout.write_bytes(self.holdout_bytes)
        self.receipt.write_bytes(self.receipt_bytes)

    def unseal(self, *, real_plan: bool = False, holdout=None, receipt=None):
        call = lambda: unseal_mod.unseal(holdout or self.holdout, receipt or self.receipt,  # noqa: E731
                                         root=self.root, pinned=self.pinned)
        if real_plan:
            return call()
        with patch.object(unseal_mod, "load_checker", stub_checker):
            return call()

    def eval_dir(self) -> list[str]:
        return sorted(p.name for p in (self.root / "eval/v2").iterdir())

    def branch(self) -> str:
        return git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip()


_TEMPLATE_DIR: Path | None = None
REAL_CHECKER = None


def setUpModule():
    global _TEMPLATE_DIR, REAL_CHECKER
    _TEMPLATE_DIR = Path(tempfile.mkdtemp())
    Sandbox.build_template(_TEMPLATE_DIR)
    REAL_CHECKER = REAL_LOAD_CHECKER(Sandbox.template)


def tearDownModule():
    remove_tree(_TEMPLATE_DIR)


def _flatten_items(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            path = prefix + "." + key if prefix else key
            yield path, value
            yield from _flatten_items(value, path)


# --------------------------------------------------------------------------- manifest


class ManifestTests(unittest.TestCase):
    def test_manifest_is_exactly_the_reported_safe_metadata(self):
        self.assertEqual(MANIFEST, SAFE_METADATA)
        self.assertEqual(list(MANIFEST), list(unseal_mod.MANIFEST_KEYS))
        for key, value in _flatten_items(MANIFEST):
            with self.subTest(key=key):
                self.assertIs(type(value), type(dict(_flatten_items(SAFE_METADATA))[key]))

    def test_pinned_values_agree_with_the_manifest(self):
        self.assertEqual(unseal_mod.PINNED, {
            "sealed_against_repo_commit": FREEZE, "input_bundle_digest": DIGEST, "input_file_count": 27,
            "holdout_sha256": SAFE_METADATA["holdout_sha256"],
            "author_receipt_sha256": SAFE_METADATA["author_receipt_sha256"], "case_count": 25})
        for key, value in unseal_mod.PINNED.items():
            with self.subTest(key=key):
                self.assertIs(type(MANIFEST[key]), type(value))
                self.assertEqual(MANIFEST[key], value)
        self.assertEqual(unseal_mod.load_manifest(ROOT, unseal_mod.PINNED), MANIFEST)

    def test_sealed_against_the_freeze_commit_and_frozen_bundle(self):
        self.assertEqual(INPUTS["content_digest"], MANIFEST["input_bundle_digest"])
        self.assertEqual(len(INPUTS["files"]), MANIFEST["input_file_count"])
        # The freeze commit is the merge of the bundle PR, not the input base commit.
        self.assertNotEqual(MANIFEST["sealed_against_repo_commit"], INPUTS["base_commit"])
        unseal_mod.verify_frozen_inputs(ROOT, MANIFEST)

    def test_distributions_are_consistent_and_use_frozen_vocabularies(self):
        contract = REAL_CHECKER
        plan = contract.holdout_plan()
        statuses = json.loads((ROOT / "eval/v2/spec/stage6-actions.json").read_text(encoding="utf-8"))
        archetypes = json.loads((ROOT / "eval/v2/spec/archetypes.json").read_text(encoding="utf-8"))
        self.assertEqual(set(MANIFEST["scenario_counts"]), set(contract.scenario_ids()))
        self.assertEqual(set(MANIFEST["final_counts"]), set(plan["required_final_values"]))
        self.assertEqual(set(MANIFEST["persona_counts"]), set(plan["required_personas"]))
        self.assertLessEqual(set(MANIFEST["final_status_counts"]), set(statuses["final_statuses"]))
        self.assertLessEqual(set(MANIFEST["archetype_counts"]), {a["id"] for a in archetypes["archetypes"]})
        for key in ("scenario_counts", "archetype_counts", "final_counts", "persona_counts"):
            self.assertEqual(sum(MANIFEST[key].values()), MANIFEST["case_count"], key)
        self.assertEqual(sum(MANIFEST["final_status_counts"].values()), MANIFEST["final_counts"]["action"])
        self.assertGreaterEqual(MANIFEST["distinct_virtual_now"], plan["min_distinct_virtual_now"])
        self.assertEqual(MANIFEST["case_count"], plan["splits"]["holdout"]["total_cases"])

    def test_receipt_contract_is_the_frozen_receipt_tools(self):
        self.assertEqual(unseal_mod.RECEIPT_KEYS, frozenset(receipt_tool.RECEIPT_KEYS))
        self.assertEqual(unseal_mod.RECEIPT_SCHEMA, receipt_tool.RECEIPT_SCHEMA)
        self.assertEqual(unseal_mod.ATTESTATION, receipt_tool.ATTESTATION)
        self.assertEqual(MANIFEST["author_context"], receipt_tool.ATTESTATION)
        self.assertEqual(set(unseal_mod.RECEIPT_FIELDS.values()) | {"schema", "status", "author_receipt_sha256",
                                                                   "isolation_note"},
                         set(unseal_mod.MANIFEST_KEYS))

    def test_no_path_stored_in_the_manifest(self):
        for key, value in _flatten_items(MANIFEST):
            with self.subTest(key=key):
                if isinstance(value, str) and key != "isolation_note":
                    self.assertIsNone(PATH_LIKE.search(value))
        text = (ROOT / unseal_mod.MANIFEST_RELATIVE).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]|\\|/Users/|/home/|Documents|stage6-author-bundle", text))
        for value in (v for _, v in _flatten_items(MANIFEST) if isinstance(v, str)):
            self.assertTrue(value in (SAFE_METADATA["schema"], "sealed", SAFE_METADATA["isolation_note"])
                            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value), value)

    def test_no_case_content_in_the_manifest(self):
        top_level = set(MANIFEST)
        for key, _ in _flatten_items(MANIFEST):
            leaf = key.split(".")[-1].lower()
            if key.split(".")[0] in unseal_mod.DISTRIBUTIONS and key not in top_level:
                continue  # vocabulary keys, checked against the frozen specs above
            for token in FORBIDDEN_KEY_TOKENS:
                with self.subTest(key=key, token=token):
                    self.assertNotIn(token, leaf)


# --------------------------------------------------------------------------- opening


class UnsealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(remove_tree, self.tmp)

    def assert_refused_cleanly(self, box, fragment=None, **kwargs):
        before, status = box.eval_dir(), git(box.root, "status", "--porcelain")
        with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
            box.unseal(**kwargs)
        self.assertEqual(box.eval_dir(), before)
        self.assertEqual(git(box.root, "status", "--porcelain"), status)
        self.assertFalse((box.root / unseal_mod.HOLDOUT_DESTINATION).exists())
        self.assertFalse((box.root / unseal_mod.RECEIPT_DESTINATION).exists())
        message = str(ctx.exception)
        if fragment:
            self.assertIn(fragment, message)
        for marker in LEAK_MARKERS + (str(self.tmp),):
            self.assertNotIn(marker, message)
        return message

    # -- success: exact raw bytes ---------------------------------------------

    def test_synthetic_unseal_copies_exact_raw_bytes(self):
        box = Sandbox(self.tmp)
        head = git(box.root, "rev-parse", "HEAD")
        result = box.unseal()
        self.assertEqual(result, {"cases": 25, "destinations": [unseal_mod.HOLDOUT_DESTINATION,
                                                                unseal_mod.RECEIPT_DESTINATION]})
        holdout = (box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes()
        receipt = (box.root / unseal_mod.RECEIPT_DESTINATION).read_bytes()
        self.assertEqual(holdout, box.holdout_bytes)
        self.assertEqual(receipt, box.receipt_bytes)
        self.assertEqual(sha(holdout), box.pinned["holdout_sha256"])
        self.assertEqual(sha(receipt), box.pinned["author_receipt_sha256"])
        self.assertFalse([n for n in box.eval_dir() if n.endswith(".tmp")])
        # No commit: the opened files are left untracked.
        self.assertEqual(git(box.root, "rev-parse", "HEAD"), head)
        status = git(box.root, "status", "--porcelain", "--untracked-files=all").splitlines()
        self.assertEqual(sorted(status), ["?? " + unseal_mod.HOLDOUT_DESTINATION,
                                          "?? " + unseal_mod.RECEIPT_DESTINATION])

    def test_synthetic_receipt_is_what_the_frozen_receipt_tool_emits(self):
        cases = synthetic_cases()
        raw = dataset_bytes(cases)
        built = receipt_tool.build_receipt(raw, split="holdout", freeze_merge_commit=FREEZE,
                                           input_bundle_digest=DIGEST, input_file_count=27,
                                           contract=StubPlan(REAL_CHECKER))
        self.assertEqual(built, receipt_dict(raw, distributions(cases)))
        self.assertEqual(unseal_mod.safe_distributions(cases), distributions(cases))

    def test_no_reserialization(self):
        cases = synthetic_cases()
        raw = ("\r\n[ " + " ,\r\n".join(json.dumps(case, ensure_ascii=True, separators=(" ,", ": "))
                                         for case in cases) + "\r\n]  \r\n").encode("utf-8")
        self.assertNotEqual(raw, dataset_bytes(json.loads(raw)))
        compact = lambda receipt: json.dumps(receipt, separators=(",", ":")).encode("utf-8") + b"\r\n"  # noqa: E731
        box = Sandbox(self.tmp, cases=cases, holdout_bytes=raw, serializer=compact)
        box.unseal()
        self.assertEqual((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes(), raw)
        self.assertEqual((box.root / unseal_mod.RECEIPT_DESTINATION).read_bytes(), box.receipt_bytes)
        self.assertTrue(box.receipt_bytes.endswith(b"}\r\n"))

    def test_each_sealed_file_read_exactly_once_and_never_reopened(self):
        box = Sandbox(self.tmp)
        real_read = Path.read_bytes
        reads: list[str] = []
        key = lambda path: os.path.normcase(os.path.abspath(path))  # noqa: E731

        def counting(path):
            data = real_read(path)
            reads.append(key(path))
            if key(path) in (key(box.holdout), key(box.receipt)):
                path.write_bytes(b"changed after the read")  # a re-read would see these bytes
            return data

        with patch.object(Path, "read_bytes", counting):
            box.unseal()
        self.assertEqual(reads.count(key(box.receipt)), 1)
        self.assertEqual(reads.count(key(box.holdout)), 1)
        self.assertEqual((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes(), box.holdout_bytes)
        self.assertEqual((box.root / unseal_mod.RECEIPT_DESTINATION).read_bytes(), box.receipt_bytes)

    # -- hashes ----------------------------------------------------------------

    def test_receipt_hash_mismatch(self):
        box = Sandbox(self.tmp)
        box.receipt.write_bytes(box.receipt_bytes.replace(b"true", b"false", 1))
        self.assert_refused_cleanly(box, "author receipt sha256 mismatch")

    def test_holdout_hash_mismatch(self):
        box = Sandbox(self.tmp)
        box.holdout.write_bytes(box.holdout_bytes + b" ")
        self.assert_refused_cleanly(box, "sealed holdout sha256 mismatch")

    def test_hash_is_checked_before_parsing(self):
        seal = seal_for()
        for raw in (b"{not json", b"\xff\xfe", b""):
            with self.subTest(raw=raw):
                with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
                    unseal_mod.verify_receipt(raw, seal.manifest)
                self.assertIn("sha256 mismatch", str(ctx.exception))
                with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
                    unseal_mod.verify_holdout(raw, seal.manifest, StubPlan(REAL_CHECKER))
                self.assertIn("sha256 mismatch", str(ctx.exception))

    # -- receipt ---------------------------------------------------------------

    def receipt_rejected(self, edit=None, *, raw=None, fragment=None):
        seal = seal_for()
        if raw is None:
            receipt = json.loads(seal.receipt)
            edit(receipt)
            raw = receipt_tool.receipt_bytes(receipt)
        manifest = dict(seal.manifest, author_receipt_sha256=sha(raw))
        with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
            unseal_mod.verify_receipt(raw, manifest)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_unedited_synthetic_receipt_passes(self):
        seal = seal_for()
        receipt = unseal_mod.verify_receipt(seal.receipt, seal.manifest)
        self.assertEqual(set(receipt), unseal_mod.RECEIPT_KEYS)

    def test_wrong_receipt_schema(self):
        for value in ("v2-stage6-dataset-receipt/2", "v2-sealed-holdout-receipt/1",
                      "v2-stage6-sealed-holdout-manifest/1", None, ["v2-stage6-dataset-receipt/1"]):
            with self.subTest(value=value):
                self.receipt_rejected(lambda r: r.__setitem__("schema", value), fragment="schema")
        self.receipt_rejected(lambda r: r.pop("schema"), fragment="schema")

    def test_wrong_receipt_key_set(self):
        for key in sorted(unseal_mod.RECEIPT_KEYS - {"schema"}):
            with self.subTest(missing=key):
                self.receipt_rejected(lambda r: r.pop(key), fragment="keys")
        for alias, original in (("sealed_against_repo_commit", "freeze_merge_commit"),
                                ("holdout_sha256", "dataset_sha256"), ("freeze_repo_commit", "freeze_merge_commit"),
                                ("seal_receipt_sha256", "dataset_sha256")):
            with self.subTest(alias=alias):
                self.receipt_rejected(lambda r: r.__setitem__(alias, r.pop(original)), fragment="keys")
                self.receipt_rejected(lambda r: r.__setitem__(alias, r[original]), fragment="keys")

        def wrap_all(receipt):
            inner = dict(receipt)
            receipt.clear()
            receipt.update(schema=inner["schema"], receipt=inner)

        def flatten_author_context(receipt):
            receipt.update(receipt.pop("author_context"))

        for edit in (wrap_all, flatten_author_context, lambda r: r.__setitem__("dataset_path", "x"),
                     lambda r: r.__setitem__("cases", [])):
            with self.subTest(edit=getattr(edit, "__name__", "extra")):
                self.receipt_rejected(edit, fragment="keys")
        # A duplicated key is not a second reading of the receipt.
        seal = seal_for()
        duplicated = seal.receipt.replace(b'"split": "holdout",', b'"split": "dev",\n  "split": "holdout",', 1)
        self.assertNotEqual(duplicated, seal.receipt)
        self.receipt_rejected(raw=duplicated, fragment="unique keys")
        self.receipt_rejected(raw=b"[]", fragment="JSON object")

    def test_receipt_manifest_field_mismatch(self):
        edits = {
            "split dev": lambda r: r.__setitem__("split", "dev"),
            "case_count": lambda r: r.__setitem__("case_count", 24),
            "case_count float": lambda r: r.__setitem__("case_count", 25.0),
            "dataset_sha256": lambda r: r.__setitem__("dataset_sha256", "0" * 64),
            "scenario": lambda r: r["scenario_counts"].__setitem__("consult_no_action", 2),
            "scenario extra key": lambda r: r["scenario_counts"].__setitem__("unknown", 0),
            "archetype": lambda r: r["archetype_counts"].pop(next(iter(r["archetype_counts"]))),
            "archetype bool": lambda r: r["archetype_counts"].__setitem__("A02", True),
            "final": lambda r: r["final_counts"].__setitem__("answer", 99),
            "final_status": lambda r: r["final_status_counts"].__setitem__("STALE", 1),
            "persona": lambda r: r["persona_counts"].__setitem__("demo-b", 1),
            "virtual_now": lambda r: r.__setitem__("distinct_virtual_now", 6),
            "virtual_now string": lambda r: r.__setitem__("distinct_virtual_now", "1"),
            "contract": lambda r: r["contract_validation"].__setitem__("error_count", 1),
            "plan": lambda r: r["plan_validation"].__setitem__("all_rules_met", False),
            "agent_runs": lambda r: r["author_context"].__setitem__("agent_runs", 1),
            "oracle_runs": lambda r: r["author_context"].__setitem__("oracle_runs", 1),
            "implementation_visible": lambda r: r["author_context"].__setitem__("implementation_visible", True),
            "attestation extra": lambda r: r["author_context"].__setitem__("note", "x"),
        }
        for label, edit in edits.items():
            with self.subTest(edit=label):
                self.receipt_rejected(edit, fragment="split" if label == "split dev" else "disagrees")
        # End to end once: the refusal leaves nothing behind.
        box = Sandbox(self.tmp, receipt_edit=lambda r: r.__setitem__("case_count", 24))
        self.assert_refused_cleanly(box, "disagrees with the sealed manifest on case_count")

    def test_wrong_freeze_commit(self):
        self.receipt_rejected(lambda r: r.__setitem__("freeze_merge_commit", "4057e26" + "0" * 33),
                              fragment="freeze_merge_commit")
        self.receipt_rejected(lambda r: r.__setitem__("freeze_merge_commit", "44efea23ae12fb9ee659139afec152d75768389e"),
                              fragment="freeze_merge_commit")
        self.manifest_rejected(sealed_against_repo_commit="44efea23ae12fb9ee659139afec152d75768389e")

    def test_wrong_bundle_digest(self):
        self.receipt_rejected(lambda r: r.__setitem__("input_bundle_digest", "0" * 64),
                              fragment="input_bundle_digest")
        self.manifest_rejected(input_bundle_digest="7b3d4684cf3fa7425d25a6e842f47876392f6b0c095592f8371d5aa0cad61fc8")
        root = self.loose_copy()
        manifest = dict(MANIFEST, input_bundle_digest="0" * 64)
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.verify_frozen_inputs(root, manifest)

    def test_wrong_file_count(self):
        self.receipt_rejected(lambda r: r.__setitem__("input_file_count", 26), fragment="input_file_count")
        self.receipt_rejected(lambda r: r.__setitem__("input_file_count", "27"), fragment="input_file_count")
        self.manifest_rejected(input_file_count=17)
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.verify_frozen_inputs(self.loose_copy(), dict(MANIFEST, input_file_count=26))

    # -- sealed manifest ---------------------------------------------------------

    def manifest_rejected(self, variant=None, **changes):
        seal = seal_for()
        manifest = dict(seal.manifest, **changes) if variant is None else variant
        loose = self.tmp / ("manifest-" + str(len(list(self.tmp.iterdir()))))
        (loose / "eval/v2").mkdir(parents=True)
        payload = manifest if isinstance(manifest, (bytes, str)) else json.dumps(manifest)
        target = loose / unseal_mod.MANIFEST_RELATIVE
        (target.write_bytes if isinstance(payload, bytes) else target.write_text)(payload)
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.load_manifest(loose, seal.pinned)

    def test_sealed_manifest_refusals(self):
        box = Sandbox(self.tmp)
        self.assertEqual(unseal_mod.load_manifest(box.root, box.pinned), box.seal.manifest)
        seal = seal_for()
        variants = {
            "schema": dict(seal.manifest, schema="v2-sealed-holdout-manifest/1"),
            "status": dict(seal.manifest, status="opened"),
            "case_count": dict(seal.manifest, case_count=24),
            "holdout sha": dict(seal.manifest, holdout_sha256="0" * 64),
            "receipt sha": dict(seal.manifest, author_receipt_sha256="0" * 64),
            "private path": dict(seal.manifest, holdout_path="x"),
            "missing key": {k: v for k, v in seal.manifest.items() if k != "persona_counts"},
            "validation": dict(seal.manifest, plan_validation={"all_rules_met": False, "error_count": 1}),
            "attestation": dict(seal.manifest, author_context=dict(seal.manifest["author_context"], agent_runs=1)),
            "note": dict(seal.manifest, isolation_note="isolated"),
            "array": [seal.manifest],
            "not json": b"{not json",
            "not utf-8": b"\xff\xfe{}",
        }
        for label, variant in variants.items():
            with self.subTest(variant=label):
                self.manifest_rejected(variant)
        with self.assertRaises(unseal_mod.UnsealRefused):
            unseal_mod.load_manifest(self.tmp / "nowhere", seal.pinned)

    def test_committed_manifest_edit_refused_end_to_end(self):
        box = Sandbox(self.tmp)
        path = box.root / unseal_mod.MANIFEST_RELATIVE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(dict(manifest, status="opened")), encoding="utf-8")
        git(box.root, "commit", "-q", "-am", "edit")
        self.assert_refused_cleanly(box, "status")
        git(box.root, "rm", "-q", unseal_mod.MANIFEST_RELATIVE)
        git(box.root, "commit", "-q", "-m", "drop manifest")
        self.assert_refused_cleanly(box, "missing")

    # -- holdout -----------------------------------------------------------------

    def holdout_rejected(self, cases=None, *, raw=None, fragment=None, checker=None):
        seal = seal_for(cases, holdout_bytes=raw) if cases is not None else seal_for(holdout_bytes=raw)
        with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
            unseal_mod.verify_holdout(seal.holdout, seal.manifest, checker or StubPlan(REAL_CHECKER))
        message = str(ctx.exception)
        if fragment:
            self.assertIn(fragment, message)
        for marker in LEAK_MARKERS:
            self.assertNotIn(marker, message)
        return message

    def test_valid_synthetic_holdout_passes_direct_check(self):
        seal = seal_for()
        cases = unseal_mod.verify_holdout(seal.holdout, seal.manifest, StubPlan(REAL_CHECKER))
        self.assertEqual(len(cases), 25)

    def test_contract_invalid_synthetic_holdout(self):
        cases = synthetic_cases()
        cases[5]["notes"] = "extra key"
        self.assertEqual(self.holdout_rejected(cases),
                         "case #5 violates the case contract (schema, 1 error(s))")
        cross = synthetic_cases()
        cross[3]["expected_capabilities"]["forbidden"] = []
        self.assertIn("case #3 violates the case contract (cross-field", self.holdout_rejected(cross))
        self.assert_refused_cleanly(Sandbox(self.tmp, cases=cases), "case #5")

    def test_checker_exception_is_a_refusal(self):
        class Exploding(StubPlan):
            def case_errors(self, case):
                raise KeyError(case["case_id"])
        self.assertEqual(self.holdout_rejected(checker=Exploding(REAL_CHECKER)),
                         "the case contract check raised KeyError at case #0")

    def test_plan_invalid_synthetic_holdout(self):
        # The real frozen plan: fixture cases cover only a few scenarios.
        message = self.holdout_rejected(checker=REAL_CHECKER, fragment="distribution plan")
        self.assertRegex(message, r"^the holdout does not meet the distribution plan \(\d+ rule\(s\) failed\)$")
        self.assert_refused_cleanly(Sandbox(self.tmp), "distribution plan", real_plan=True)

    def test_distribution_mismatch(self):
        base = distributions(synthetic_cases())
        changes = {
            "scenario_counts": dict(base["scenario_counts"], consult_no_action=99),
            "archetype_counts": dict(base["archetype_counts"], A23=1),
            "final_counts": {**base["final_counts"], "answer": base["final_counts"]["answer"] - 1, "refuse": 1},
            "final_status_counts": dict(base["final_status_counts"], STALE=1),
            "persona_counts": {"demo-a": 12, "demo-b": 13},
            "distinct_virtual_now": 6,
        }
        self.assertEqual(set(changes), set(unseal_mod.DISTRIBUTIONS))
        for key, value in changes.items():
            with self.subTest(key=key):
                seal = seal_for(seal={key: value})
                unseal_mod.verify_receipt(seal.receipt, seal.manifest)  # receipt and manifest agree
                with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
                    unseal_mod.verify_holdout(seal.holdout, seal.manifest, StubPlan(REAL_CHECKER))
                self.assertEqual(str(ctx.exception),
                                 key + " recomputed from the holdout differs from the sealed manifest")
        self.assert_refused_cleanly(Sandbox(self.tmp, seal={"distinct_virtual_now": 6}), "distinct_virtual_now")

    def test_duplicate_case_id(self):
        cases = synthetic_cases()
        cases[1]["case_id"] = cases[0]["case_id"]
        self.assertEqual(self.holdout_rejected(cases), "case_id values are not unique (1 duplicate(s))")
        self.assert_refused_cleanly(Sandbox(self.tmp, cases=cases), "not unique")

    def test_case_count_and_shape(self):
        self.holdout_rejected(synthetic_cases(24), fragment="24 cases")
        self.holdout_rejected(synthetic_cases(26), fragment="26 cases")
        self.holdout_rejected(raw=b"{}", fragment="JSON array")
        self.holdout_rejected(raw=b"[1, 2]", fragment="JSON array")
        self.holdout_rejected(raw=b"[{not json", fragment="not UTF-8 JSON")
        self.holdout_rejected(raw=b"\xff\xfe[]", fragment="not UTF-8 JSON")
        self.holdout_rejected(raw=b"\xef\xbb\xbf" + dataset_bytes(synthetic_cases()), fragment="not UTF-8 JSON")

    # -- repository preconditions --------------------------------------------------

    def test_dirty_git_tree_refused(self):
        box = Sandbox(self.tmp)
        (box.root / "stray.txt").write_text("x", encoding="utf-8")
        self.assert_refused_cleanly(box, "not clean")
        (box.root / "stray.txt").unlink()
        manifest = box.root / unseal_mod.MANIFEST_RELATIVE
        original = manifest.read_bytes()
        manifest.write_bytes(original + b"\n")
        self.assert_refused_cleanly(box, "not clean")
        manifest.write_bytes(original)
        (box.root / "eval/v2/notes.json").write_text("{}", encoding="utf-8")
        git(box.root, "add", "eval/v2/notes.json")
        self.assert_refused_cleanly(box, "not clean")

    def test_already_open_destination_refused(self):
        for relative in (unseal_mod.HOLDOUT_DESTINATION, unseal_mod.RECEIPT_DESTINATION):
            with self.subTest(destination=relative):
                tmp = self.tmp / relative.rsplit("/", 1)[-1]
                tmp.mkdir()
                box = Sandbox(tmp)
                (box.root / relative).write_bytes(b"[]")
                git(box.root, "add", relative)
                git(box.root, "commit", "-q", "-m", "x")
                with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
                    box.unseal()
                self.assertIn("already exists", str(ctx.exception))
                self.assertEqual((box.root / relative).read_bytes(), b"[]")
                other = ({unseal_mod.HOLDOUT_DESTINATION, unseal_mod.RECEIPT_DESTINATION} - {relative}).pop()
                self.assertFalse((box.root / other).exists())

    def test_second_opening_refused(self):
        box = Sandbox(self.tmp)
        box.unseal()
        opened = (box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes()
        with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
            box.unseal()
        self.assertIn("not clean", str(ctx.exception))
        self.assertEqual((box.root / unseal_mod.HOLDOUT_DESTINATION).read_bytes(), opened)

    def test_git_history_refused(self):
        def history_box(name, make_history):
            tmp = self.tmp / name
            tmp.mkdir()
            box = Sandbox(tmp)
            make_history(box)
            self.assertEqual(git(box.root, "status", "--porcelain"), "")
            for relative in (unseal_mod.HOLDOUT_DESTINATION, unseal_mod.RECEIPT_DESTINATION):
                self.assertFalse((box.root / relative).exists())
            self.assert_refused_cleanly(box, "has git history")

        def committed_then_removed(box):
            (box.root / unseal_mod.HOLDOUT_DESTINATION).write_bytes(b"[]")
            git(box.root, "add", "-A")
            git(box.root, "commit", "-q", "-m", "open")
            git(box.root, "rm", "-q", unseal_mod.HOLDOUT_DESTINATION)
            git(box.root, "commit", "-q", "-m", "remove")

        def side_branch_only(box):
            base = box.branch()
            git(box.root, "checkout", "-q", "-b", "side")
            (box.root / unseal_mod.RECEIPT_DESTINATION).write_bytes(b"{}")
            git(box.root, "add", "-A")
            git(box.root, "commit", "-q", "-m", "open on a side branch")
            git(box.root, "checkout", "-q", base)

        def tag_only(box):
            side_branch_only(box)
            git(box.root, "tag", "opened", "side")
            git(box.root, "branch", "-q", "-D", "side")

        def merged_away(box):
            base = box.branch()
            git(box.root, "checkout", "-q", "-b", "side")
            (box.root / unseal_mod.HOLDOUT_DESTINATION).write_bytes(b"[]")
            git(box.root, "add", "-A")
            git(box.root, "commit", "-q", "-m", "open")
            git(box.root, "rm", "-q", unseal_mod.HOLDOUT_DESTINATION)
            git(box.root, "commit", "-q", "-m", "remove")
            git(box.root, "checkout", "-q", base)
            git(box.root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
            git(box.root, "branch", "-q", "-D", "side")

        for name, make in (("removed", committed_then_removed), ("side", side_branch_only),
                           ("tag", tag_only), ("merged", merged_away)):
            with self.subTest(history=name):
                history_box(name, make)

    # -- frozen author inputs ------------------------------------------------------

    def loose_copy(self) -> Path:
        root = self.tmp / ("loose-" + str(len(list(self.tmp.iterdir()))))
        shutil.copytree(Sandbox.template, root, ignore=shutil.ignore_patterns(".git"))
        return root

    def inputs_rejected(self, root: Path, fragment=None):
        with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
            unseal_mod.verify_frozen_inputs(root, MANIFEST)
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_frozen_27_inputs_verify(self):
        unseal_mod.verify_frozen_inputs(ROOT, MANIFEST)
        unseal_mod.verify_frozen_inputs(self.loose_copy(), MANIFEST)
        self.assertEqual(len(INPUTS["files"]), 27)

    def test_modified_frozen_input_refused(self):
        for relative in ("eval/v2/stage6_case_contract.py", "eval/v2/spec/stage6-holdout-plan.json",
                         "policy_sources/standard-return.md", "docs/v2/stage6-author-brief.md"):
            with self.subTest(path=relative):
                root = self.loose_copy()
                target = root / relative
                target.write_bytes(target.read_bytes() + b"\n")
                self.inputs_rejected(root, "changed since sealing")
        box = Sandbox(self.tmp)
        checker = box.root / "eval/v2/stage6_dataset_receipt.py"
        checker.write_bytes(checker.read_bytes() + b"# edited after sealing\n")
        git(box.root, "commit", "-q", "-am", "edit a frozen input")
        self.assert_refused_cleanly(box, "changed since sealing")

    def test_missing_frozen_input_refused(self):
        root = self.loose_copy()
        (root / "system_fixtures/aftersales_stage6_seed.sql").unlink()
        self.inputs_rejected(root, "missing")

    def write_inputs(self, root: Path, inputs) -> None:
        (root / unseal_mod.INPUT_MANIFEST_RELATIVE).write_text(json.dumps(inputs), encoding="utf-8")

    def test_input_manifest_forgeries_refused(self):
        root = self.loose_copy()
        forged = copy.deepcopy(INPUTS)
        forged["files"][3]["sha256"] = "0" * 64
        self.write_inputs(root, forged)
        self.inputs_rejected(root, "content digest")
        # Recomputing the digest over the forged entry still fails: it is not the sealed digest.
        forged["content_digest"] = unseal_mod.input_content_digest(forged["files"])
        self.write_inputs(root, forged)
        self.inputs_rejected(root, "content digest")
        # A dropped file with a recomputed digest fails on the count and the digest.
        dropped = copy.deepcopy(INPUTS)
        del dropped["files"][0]
        dropped["content_digest"] = unseal_mod.input_content_digest(dropped["files"])
        self.write_inputs(root, dropped)
        self.inputs_rejected(root, "file count")
        # Byte counts are outside the digest; they are still checked.
        counted = copy.deepcopy(INPUTS)
        counted["files"][0]["bytes"] += 1
        self.write_inputs(root, counted)
        self.inputs_rejected(root, "byte count changed")

    def test_input_manifest_structure_refused(self):
        root = self.loose_copy()
        files = INPUTS["files"]
        variants = {
            "schema": dict(INPUTS, schema="v2-holdout-input-manifest/1"),
            "normalization": dict(INPUTS, hash_normalization="none"),
            "extra key": dict(INPUTS, location="x"),
            "missing key": {k: v for k, v in INPUTS.items() if k != "description"},
            "unsorted": dict(INPUTS, files=[files[1], files[0]] + files[2:]),
            "duplicate": dict(INPUTS, files=[files[0], files[0]] + files[2:]),
            "traversal": dict(INPUTS, files=[dict(files[0], path="../x")] + files[1:]),
            "absolute": dict(INPUTS, files=[dict(files[0], path="/abs.md")] + files[1:]),
            "bool bytes": dict(INPUTS, files=[dict(files[0], bytes=True)] + files[1:]),
            "string bytes": dict(INPUTS, files=[dict(files[0], bytes="12")] + files[1:]),
            "upper sha": dict(INPUTS, files=[dict(files[0], sha256=files[0]["sha256"].upper())] + files[1:]),
            "entry key": dict(INPUTS, files=[dict(files[0], mode="x")] + files[1:]),
            "not a list": dict(INPUTS, files={}),
        }
        for label, variant in variants.items():
            with self.subTest(variant=label):
                self.write_inputs(root, variant)
                self.inputs_rejected(root)
        (root / unseal_mod.INPUT_MANIFEST_RELATIVE).unlink()
        self.inputs_rejected(root, "missing")

    def test_line_endings_do_not_affect_input_hashes(self):
        root = self.loose_copy()
        spec = root / "docs/v2/stage6-domain-spec.md"
        spec.write_bytes(spec.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        unseal_mod.verify_frozen_inputs(root, MANIFEST)

    # -- writing -------------------------------------------------------------------

    def assert_nothing_written(self, box, before):
        self.assertEqual(box.eval_dir(), before)
        self.assertEqual(git(box.root, "status", "--porcelain", "--untracked-files=all"), "")

    def test_interrupted_rename_leaves_nothing(self):
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
        self.assert_nothing_written(box, before)

    def test_corrupted_destination_is_removed(self):
        box = Sandbox(self.tmp)
        real_replace = unseal_mod.os.replace

        def corrupting(src, dst):
            real_replace(src, dst)
            if str(dst).endswith("stage6-holdout.json"):
                with open(dst, "ab") as out:
                    out.write(b" ")

        before = box.eval_dir()
        with patch.object(unseal_mod.os, "replace", corrupting):
            with self.assertRaises(unseal_mod.UnsealRefused) as ctx:
                box.unseal()
        self.assertIn("does not match the sealed bytes", str(ctx.exception))
        self.assert_nothing_written(box, before)

    def test_failed_fsync_leaves_nothing(self):
        box = Sandbox(self.tmp)
        real_fsync = unseal_mod.os.fsync
        calls = []

        def flaky(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("simulated disk error")
            return real_fsync(fd)

        before = box.eval_dir()
        with patch.object(unseal_mod.os, "fsync", flaky):
            with self.assertRaises(OSError):
                box.unseal()
        self.assert_nothing_written(box, before)

    # -- bytecode ------------------------------------------------------------------

    def test_opening_creates_no_pycache(self):
        box = Sandbox(self.tmp)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = False
        try:
            box.unseal()
        finally:
            sys.dont_write_bytecode = previous
        self.assertEqual(pycache_entries(box.root), [])

    def test_fresh_interpreter_without_dash_b_creates_no_pycache(self):
        box = Sandbox(self.tmp)
        pinned = self.tmp / "pinned.json"
        pinned.write_text(json.dumps(box.pinned), encoding="utf-8")
        driver = self.tmp / "driver.py"
        driver.write_text(
            "import json, runpy, sys\n"
            "from pathlib import Path\n"
            "tool, root, holdout, receipt, pinned = sys.argv[1:6]\n"
            "g = runpy.run_path(tool, run_name='stage6_unseal_under_test')\n"
            "try:\n"
            "    g['unseal'](Path(holdout), Path(receipt), root=Path(root),\n"
            "                pinned=json.loads(Path(pinned).read_text(encoding='utf-8')))\n"
            "except g['UnsealRefused'] as exc:\n"
            "    print('refused: ' + str(exc))\n"
            "    sys.exit(3)\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        run = subprocess.run([sys.executable, "-X", "utf8", str(driver), unseal_mod.__file__, str(box.root),
                              str(box.holdout), str(box.receipt), str(pinned)],
                             capture_output=True, text=True, encoding="utf-8", env=env)
        # The real plan runs on every case and refuses the fixture split.
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertIn("distribution plan", run.stdout)
        self.assertEqual(pycache_entries(box.root), [])

    # -- CLI -----------------------------------------------------------------------

    def run_cli(self, box, argv):
        real = unseal_mod.unseal
        out, err = io.StringIO(), io.StringIO()
        with patch.object(unseal_mod, "unseal", lambda h, r: real(h, r, root=box.root, pinned=box.pinned)), \
                patch.object(unseal_mod, "load_checker", stub_checker):
            with redirect_stdout(out), redirect_stderr(err):
                code = unseal_mod.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_output_and_refusals_hide_paths(self):
        box = Sandbox(self.tmp)
        code, out, err = self.run_cli(box, [str(box.holdout), str(box.receipt)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "receipt hash verified", "holdout hash verified", "25 cases validated",
            "wrote eval/v2/stage6-holdout.json", "wrote eval/v2/stage6-holdout.receipt.json",
            "The opened files stay untracked; do not commit them. No evaluation was run."])
        code, out, err = self.run_cli(box, [str(box.holdout), str(box.receipt)])
        self.assertEqual((code, err.strip()), (1, "refused: the working tree is not clean"))
        self.assertNotIn(str(self.tmp), out + err)

    def test_cli_unreadable_file_hides_the_path(self):
        box = Sandbox(self.tmp)
        missing = self.tmp / "sealed" / "missing.bin"
        code, out, err = self.run_cli(box, [str(box.holdout), str(missing)])
        self.assertEqual((code, err.strip()), (1, "refused: FileNotFoundError"))
        self.assertNotIn(str(self.tmp), out + err)
        self.assertFalse((box.root / unseal_mod.HOLDOUT_DESTINATION).exists())

    def test_cli_takes_exactly_two_paths_and_no_reset(self):
        parser = unseal_mod.build_parser()
        self.assertEqual([action.dest for action in parser._actions],
                         ["help", "sealed_holdout_file", "author_receipt_file"])
        for argv in ([], ["a"], ["a", "b", "c"], ["--reset", "a", "b"], ["--force", "a", "b"],
                     ["--delete", "a", "b"], ["--root", "x", "a", "b"]):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    unseal_mod.main(argv)
                self.assertEqual(ctx.exception.code, 2)


# --------------------------------------------------------------------------- the tool itself


def _executable_strings(source: str) -> list[str]:
    tree = ast.parse(source)
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


class ScriptSafetyTests(unittest.TestCase):
    def test_tool_contains_no_private_path(self):
        for pattern in (r"[A-Za-z]:[\\/]", r"/Users/", r"/home/", r"Documents", r"Desktop", r"Downloads",
                        r"OneDrive", r"AppData", r"stage6-author-bundle", r"~", r"\.bin\b"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, SCRIPT))

    def test_tool_performs_no_filesystem_search(self):
        for pattern in (r"\bglob\(", r"\brglob\b", r"os\.walk", r"\bscandir\b", r"\blistdir\b", r"\biterdir\b",
                        r"expanduser", r"Path\.home", r"os\.environ", r"getenv", r"fnmatch", r"getcwd",
                        r"Path\.cwd", r"\bwalk\("):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, SCRIPT))

    def test_no_network_agent_or_llm_dependency(self):
        tree = ast.parse(SCRIPT)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        imported.discard("__future__")
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        for banned in ("socket", "urllib", "http", "ssl", "asyncio", "requests", "httpx", "openai",
                       "llm_provider", "orchestration", "agent", "rag", "aftersales", "eval_v2", "eval", "tools"):
            self.assertNotIn(banned, imported)
        literals = " ".join(_executable_strings(SCRIPT)).lower()
        # agent_runs / oracle_runs are attestation fields; nothing here runs either.
        for token in ("deepseek", "llm", "run_oracle", "stage6_oracle", "stage6_scoring", "stage6_runner",
                      "action_loop", "eval_v2", "stage6-dev", "stage6-validation", "dev.json", "validation.json"):
            with self.subTest(token=token):
                self.assertNotIn(token, literals)

    def test_destinations_are_fixed_and_paths_are_never_persisted(self):
        self.assertEqual(unseal_mod.HOLDOUT_DESTINATION, "eval/v2/stage6-holdout.json")
        self.assertEqual(unseal_mod.RECEIPT_DESTINATION, "eval/v2/stage6-holdout.receipt.json")
        tree = ast.parse(SCRIPT)
        source = ast.get_source_segment(SCRIPT, next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "unseal"))
        self.assertIn("(HOLDOUT_DESTINATION, holdout_bytes", source)
        self.assertIn("(RECEIPT_DESTINATION, receipt_bytes", source)
        self.assertEqual(source.count("read_bytes()"), 2)
        for name in ("write_text", "write_bytes", "dump", "open", "mkdir", "makedirs", "copy", "copyfile"):
            with self.subTest(call=name):
                self.assertFalse([n for n in ast.walk(tree) if isinstance(n, ast.Call)
                                  and getattr(n.func, "attr", getattr(n.func, "id", None)) == name])
        options = [arg.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", None) == "add_argument"
                   for arg in n.args if isinstance(arg, ast.Constant)]
        self.assertEqual(options, ["sealed_holdout_file", "author_receipt_file"])

    def test_stage4_5_seal_is_separate_and_untouched(self):
        self.assertEqual(stage5_unseal.MANIFEST_RELATIVE, "eval/v2/holdout.manifest.json")
        self.assertEqual((stage5_unseal.HOLDOUT_DESTINATION, stage5_unseal.RECEIPT_DESTINATION),
                         ("eval/v2/holdout.json", "eval/v2/holdout.receipt.json"))
        self.assertEqual(stage5_unseal.PINNED["holdout_sha256"],
                         "0d312305e62ffc3bf4c73cf3ee0715a8cbe1b910d44183b9bb8abf9cc2d88e8a")
        self.assertEqual(stage5_unseal.PINNED["input_bundle_digest"],
                         "7b3d4684cf3fa7425d25a6e842f47876392f6b0c095592f8371d5aa0cad61fc8")
        stage4 = json.loads((ROOT / stage5_unseal.MANIFEST_RELATIVE).read_text(encoding="utf-8"))
        self.assertEqual(stage4["schema"], "v2-sealed-holdout-manifest/1")
        self.assertNotIn("unseal_v2_holdout", SCRIPT)


class RepositoryStateTests(unittest.TestCase):
    def test_holdout_not_opened_and_no_dev_or_validation(self):
        for relative in (unseal_mod.HOLDOUT_DESTINATION, unseal_mod.RECEIPT_DESTINATION,
                         "eval/v2/stage6-dev.json", "eval/v2/stage6-validation.json"):
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())
                history = subprocess.run(["git", "-C", str(ROOT), "log", "--all", "--full-history",
                                          "--format=%H", "--", relative], capture_output=True, text=True)
                self.assertEqual((history.returncode, history.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
