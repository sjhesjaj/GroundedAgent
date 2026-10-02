"""Stage 6.4B: the frozen Stage 6 author bundle, its export and the dataset receipt.

Contract and tooling checks only. No Stage 6 dataset is authored here: the
receipt tests use evaluator fixture cases with the distribution check stubbed,
or deliberately non-conforming inputs.
"""

from __future__ import annotations

import ast
import copy
import hashlib
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

from eval.v2 import stage6_dataset_receipt as receipt_tool
from tools import export_v2_holdout_author_bundle as stage5_bundle
from tools import export_v2_stage6_author_bundle as bundle

from tests import stage6_eval_support as support

ROOT = Path(__file__).resolve().parent.parent
BASE_COMMIT = "44efea23ae12fb9ee659139afec152d75768389e"
DIGEST = "0" * 40


class StubPlan:
    """The real case contract with the distribution check stubbed (no dataset is authored)."""

    def __init__(self):
        self.real = receipt_tool.load_contract()

    def case_errors(self, case):
        return self.real.case_errors(case)

    @staticmethod
    def dataset_plan_errors(cases, split):
        return []


def dataset_bytes(*cases) -> bytes:
    return json.dumps(list(cases), ensure_ascii=False, indent=1).encode("utf-8")


def tree(root: Path) -> list[str]:
    """Every file and directory under root, relative POSIX, sorted."""
    return sorted(p.relative_to(root).as_posix() + ("/" if p.is_dir() else "") for p in root.rglob("*"))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = bundle.load_manifest()

    def test_manifest_verifies(self):
        self.assertEqual(len(bundle.verify_manifest(self.manifest)), len(self.manifest["files"]))

    def test_manifest_is_current_and_deterministic(self):
        self.assertEqual(self.manifest["base_commit"], BASE_COMMIT)
        self.assertEqual(bundle.build_manifest(BASE_COMMIT), self.manifest)
        self.assertEqual(bundle.build_manifest(BASE_COMMIT), bundle.build_manifest(BASE_COMMIT))

    def test_allowed_inputs_exactly(self):
        paths = [entry["path"] for entry in self.manifest["files"]]
        policies = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "policy_sources").glob("*.md"))
        self.assertEqual(paths, sorted(list(bundle.ALLOWED_INPUTS) + policies))
        self.assertEqual((len(bundle.ALLOWED_INPUTS), len(policies), len(paths)), (21, 6, 27))

    def test_content_digest_covers_only_path_and_hash(self):
        files = copy.deepcopy(self.manifest["files"])
        digest = bundle.content_digest(files)
        self.assertEqual(digest, self.manifest["content_digest"])
        for entry in files:
            entry["bytes"] = 0
        self.assertEqual(bundle.content_digest(files), digest)
        self.assertEqual(bundle.build_manifest("1" * 40)["content_digest"], digest)
        files[0]["sha256"] = "0" * 64
        self.assertNotEqual(bundle.content_digest(files), digest)
        # the receipt tool computes the same digest
        self.assertEqual(receipt_tool.content_digest(bundle.MANIFEST_SCHEMA, self.manifest["files"]),
                         self.manifest["content_digest"])

    def test_manifest_has_no_absolute_or_local_paths(self):
        text = (ROOT / bundle.MANIFEST_RELATIVE).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[A-Za-z]:[\\/]|/Users/|/home/|tmp/|\\\\", text))

    def test_stage5_author_inputs_are_untouched(self):
        manifest = stage5_bundle.load_manifest()
        self.assertEqual(len(stage5_bundle.verify_manifest(manifest)), 17)
        self.assertEqual(manifest["content_digest"],
                         "7b3d4684cf3fa7425d25a6e842f47876392f6b0c095592f8371d5aa0cad61fc8")


class AllowListTests(unittest.TestCase):
    def test_only_stdlib_checkers_are_code(self):
        code = sorted(path for path in bundle.expected_paths() if path.endswith(".py"))
        self.assertEqual(code, sorted(bundle.PYTHON_INPUTS))
        for path in code:
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            modules = {alias.name.split(".")[0] for node in ast.walk(tree)
                       if isinstance(node, ast.Import) for alias in node.names}
            modules |= {node.module.split(".")[0] for node in ast.walk(tree)
                        if isinstance(node, ast.ImportFrom) and node.module and not node.level}
            with self.subTest(path=path):
                self.assertTrue(modules <= set(sys.stdlib_module_names) | {"__future__"}, modules)

    def test_no_implementation_design_or_dataset_input(self):
        paths = bundle.expected_paths()
        for path in paths:
            lowered = path.lower()
            with self.subTest(path=path):
                self.assertFalse(lowered.startswith(("eval_v2/", "orchestration/", "tests/", "tools/",
                                                     "eval/v2/results", "eval/runs", "eval/artifacts")))
                self.assertFalse(lowered.startswith("aftersales/") and lowered.endswith(".py"))
                self.assertNotIn("design", lowered)
                self.assertEqual(bundle.denied_tokens(path), set())
        for excluded in ("docs/v2/stage6-design.md", "docs/v2/stage6-4b-design.md", "docs/v2/stage4-design.md",
                         "HANDOFF.md", "AGENTS.md", "eval/v2/spec/holdout-plan.json", "eval/v2/dev.json",
                         "eval/v2/validation.json", "eval/v2/holdout.manifest.json",
                         "eval/v2/stage6-holdout-input.manifest.json"):
            self.assertNotIn(excluded, paths)

    def test_denylist_catches_forbidden_paths(self):
        for path in ("docs/v2/stage6-design.md", "docs/v2/stage6-4b-design.md", "eval_v2/action_loop.py",
                     "eval_v2/stage6_oracle.py", "eval_v2/stage6_scoring.py", "aftersales/guard.py",
                     "eval/v2/stage6-dev.json", "eval/v2/stage6-validation.json", "eval/v2/stage6-holdout.json",
                     "HANDOFF.md", "tests/test_v2_stage6_eval_scoring.py", "eval/v2/results/stage6.json",
                     "docs/v2/prompts.md", "eval/v2/stage6_harness.json"):
            with self.subTest(path=path):
                with self.assertRaises(bundle.BundleRefused):
                    bundle.check_relative(path)
        for path in ("../outside.md", "/abs.md", ".git/config", "a\\b.md", "C:/x.md"):
            with self.subTest(path=path):
                with self.assertRaises(bundle.BundleRefused):
                    bundle.check_relative(path)

    def test_author_documents_name_no_implementation(self):
        identifiers = ("ActionGateway", "GuardStateReader", "Guard.", "LLMNative", "eval_v2", "tool_loop",
                       "action_loop", "SharedGenerator", "DeepSeek", "STAGE6_SYSTEM_PROMPT", "decision_record",
                       "system prompt", "stage6-design", "HANDOFF")
        for path in ("docs/v2/stage6-author-brief.md", "docs/v2/stage6-domain-spec.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            for identifier in identifiers:
                with self.subTest(path=path, identifier=identifier):
                    self.assertNotIn(identifier, text)

    def test_brief_states_the_split_contract(self):
        brief = (ROOT / "docs/v2/stage6-author-brief.md").read_text(encoding="utf-8")
        plan = json.loads((ROOT / "eval/v2/spec/stage6-holdout-plan.json").read_text(encoding="utf-8"))
        for split, rules in plan["splits"].items():
            self.assertRegex(brief, "`" + split + "` \\| " + str(rules["total_cases"]) + " \\|")
        for value in plan["required_final_values"]:
            self.assertIn("`" + value + "`", brief)
        for phrase in ("--attest-isolated", "bundle-manifest.json", "从不放进任何仓库", "数据集内容、case 文本或文件位置"):
            self.assertIn(phrase, brief)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_export_matches_manifest_exactly(self):
        out = self.tmp / "bundle"
        result = bundle.export_bundle(out)
        manifest = bundle.load_manifest()
        written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        self.assertEqual(written, sorted([f["path"] for f in manifest["files"]] + [bundle.BUNDLE_MANIFEST_NAME]))
        for entry in manifest["files"]:
            self.assertEqual(bundle.sha256_hex((out / entry["path"]).read_bytes()), entry["sha256"])
        on_disk = json.loads((out / bundle.BUNDLE_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(on_disk, result)
        for absent in (".git", "tests", "tools", "eval_v2", "orchestration", "HANDOFF.md"):
            self.assertFalse((out / absent).exists(), absent)

    def test_exported_bundle_is_self_contained(self):
        out = self.tmp / "bundle"
        bundle.export_bundle(out)
        cases = [support.exchange_case("fixture-a"), support.a23()]
        invalid = support.exchange_case("fixture-b")
        invalid["expected_answerability"]["final"] = "answer"
        script = r'''
import importlib.util, json, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
c6 = load("c6", "eval/v2/stage6_case_contract.py")
tool = load("tool", "eval/v2/stage6_dataset_receipt.py")
cases, invalid, expected = json.load(sys.stdin)
digest, count = tool.verify_bundle(expected_digest=expected)
class Stub:
    case_errors = staticmethod(c6.case_errors)
    @staticmethod
    def dataset_plan_errors(cases, split): return []
built = tool.build_receipt(json.dumps(cases).encode("utf-8"), split="dev", freeze_merge_commit="0" * 40,
                           input_bundle_digest=digest, input_file_count=count, contract=Stub)
print(json.dumps({"vocab": c6.vocabulary_errors(), "valid": [c6.case_errors(c) for c in cases],
                  "invalid": c6.case_errors(invalid), "digest": digest, "count": count,
                  "receipt_cases": built["case_count"], "personas": c6.persona_customer_ids(),
                  "foreign": sorted(m.split(".")[0] for m in sys.modules
                                    if m.split(".")[0] not in sys.stdlib_module_names
                                    and m not in ("__main__", "c6", "tool")),
                  "path": sys.path}))
'''
        expected = bundle.load_manifest()["content_digest"]
        before = tree(out)
        run = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script], cwd=out,
                             input=json.dumps([cases, invalid, expected]), capture_output=True, text=True,
                             encoding="utf-8", timeout=120)
        self.assertEqual(tree(out), before)  # the bundle is input-only
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["vocab"], [])
        self.assertEqual(result["valid"], [[], []])
        self.assertTrue(result["invalid"])
        self.assertEqual((result["digest"], result["count"]), (bundle.load_manifest()["content_digest"], 27))
        self.assertEqual(result["receipt_cases"], 2)
        self.assertEqual(result["personas"], {"demo-a": "CUST-001", "demo-b": "CUST-002"})
        self.assertEqual(result["foreign"], [])
        repo = str(ROOT.resolve()).lower()
        self.assertFalse(any(p and str(Path(p).resolve()).lower().startswith(repo) for p in result["path"]))

    def test_export_is_deterministic(self):
        first, second = self.tmp / "a", self.tmp / "b"
        bundle.export_bundle(first)
        bundle.export_bundle(second)
        read = lambda d: {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
        self.assertEqual(read(first), read(second))

    def test_refuses_destination_inside_repo(self):
        for target in (ROOT, ROOT / "tmp" / "stage6-author-bundle", ROOT / "eval" / "v2", ROOT.parent):
            with self.subTest(target=target.name):
                with self.assertRaises(bundle.BundleRefused):
                    bundle.check_destination(target)
        self.assertFalse((ROOT / "tmp" / "stage6-author-bundle").exists())

    def test_refuses_non_empty_destination(self):
        (self.tmp / "existing.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(bundle.BundleRefused):
            bundle.export_bundle(self.tmp)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["existing.txt"])

    def fake_root(self) -> Path:
        root = self.tmp / "repo"
        for entry in bundle.load_manifest()["files"] + [{"path": bundle.MANIFEST_RELATIVE}]:
            target = root / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / entry["path"], target)
        return root

    def test_refuses_tampered_source_and_writes_nothing(self):
        root = self.fake_root()
        spec = root / "eval/v2/spec/stage6-holdout-plan.json"
        spec.write_bytes(spec.read_bytes() + b"\n")
        out = self.tmp / "out"
        with self.assertRaises(bundle.BundleRefused):
            bundle.export_bundle(out, root)
        self.assertFalse(out.exists() and any(out.iterdir()))

    def test_refuses_manifest_with_extra_or_missing_path(self):
        root = self.fake_root()
        manifest = bundle.load_manifest(root)
        extra = copy.deepcopy(manifest)
        extra["files"].append({"path": "zz/extra.md", "sha256": "0" * 64, "bytes": 0})
        extra["content_digest"] = bundle.content_digest(extra["files"])
        missing = copy.deepcopy(manifest)
        missing["files"].pop(0)
        missing["content_digest"] = bundle.content_digest(missing["files"])
        for variant in (extra, missing):
            with self.assertRaises(bundle.BundleRefused):
                bundle.verify_manifest(variant, root)

    def test_line_endings_do_not_change_hashes(self):
        root = self.fake_root()
        spec = root / "docs/v2/stage6-domain-spec.md"
        data = spec.read_bytes().replace(b"\r\n", b"\n")
        spec.write_bytes(data.replace(b"\n", b"\r\n"))
        bundle.verify_manifest(bundle.load_manifest(root), root)

    def test_cli_refusal_exit_code(self):
        (self.tmp / "x").write_text("x", encoding="utf-8")
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            self.assertEqual(bundle.main([str(self.tmp)]), 1)
        self.assertIn("refused", err.getvalue())
        self.assertEqual(out.getvalue(), "")


class ReceiptTests(unittest.TestCase):
    def build(self, data, **overrides):
        options = dict(split="dev", freeze_merge_commit="a" * 40, input_bundle_digest="d" * 64,
                       input_file_count=27, contract=StubPlan())
        options.update(overrides)
        return receipt_tool.build_receipt(data, **options)

    def test_receipt_contract(self):
        data = dataset_bytes(support.exchange_case("r-1"), support.a23(), support.consult_case("r-3"))
        receipt = self.build(data)
        self.assertEqual(tuple(receipt), receipt_tool.RECEIPT_KEYS)
        self.assertEqual(receipt["schema"], "v2-stage6-dataset-receipt/1")
        self.assertEqual(receipt["dataset_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(receipt["case_count"], 3)
        self.assertEqual(receipt["final_counts"], {"action": 2, "answer": 1})
        self.assertEqual(receipt["final_status_counts"], {"EXECUTED": 1, "REJECTED": 1})
        self.assertEqual(receipt["scenario_counts"], {"approval_rejected": 1, "consult_no_action": 1,
                                                      "exchange_auto_execute": 1})
        self.assertEqual(receipt["author_context"], receipt_tool.ATTESTATION)
        text = json.dumps(receipt, ensure_ascii=False)
        for leaked in ("r-1", "t-a23", "ORD-1001", "帮我", "C:", "/tmp"):
            self.assertNotIn(leaked, text)
        self.assertNotEqual(self.build(data.replace(b"\n", b"\r\n"))["dataset_sha256"], receipt["dataset_sha256"])

    def test_refusals(self):
        good = dataset_bytes(support.exchange_case("r-1"))
        bad_case = support.exchange_case("r-2")
        bad_case["expected_answerability"]["final"] = "answer"
        for label, data, overrides in (
                ("split", good, {"split": "train"}),
                ("commit", good, {"freeze_merge_commit": "abc"}),
                ("not json", b"\xff\xfe", {}),
                ("not an array", json.dumps({"cases": []}).encode(), {}),
                ("empty", b"[]", {}),
                ("invalid case", dataset_bytes(bad_case), {}),
                ("duplicate ids", dataset_bytes(support.exchange_case("r-1"), support.exchange_case("r-1")), {}),
                ("plan", good, {"contract": receipt_tool.load_contract()})):
            with self.subTest(refusal=label):
                with self.assertRaises(receipt_tool.ReceiptRefused):
                    self.build(data, **overrides)

    def test_the_bundle_digest_must_be_well_formed(self):
        for value in ("abc", "D" * 64, "g" * 64, 64, None):
            with self.subTest(value=value):
                with self.assertRaises(receipt_tool.ReceiptRefused):
                    self.build(dataset_bytes(support.exchange_case("r-1")), input_bundle_digest=value)


class BundleAttestationTests(unittest.TestCase):
    """The receipt trusts an out-of-band expected digest and an exact, input-only bundle tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = self.tmp / "bundle"
        exported = bundle.export_bundle(self.bundle)
        self.expected = bundle.load_manifest()["content_digest"]  # the trusted, reviewed value
        self.assertEqual(exported["content_digest"], self.expected)
        self.dataset = self.tmp / "dataset.json"
        self.dataset.write_bytes(dataset_bytes(support.exchange_case("r-1")))
        self.receipt = self.tmp / "receipt.json"

    def cli(self, *, digest=None, out=None, dataset=None, flags=("-B",), attest=True):
        command = [sys.executable, "-I", "-S", "-X", "utf8", *flags, "eval/v2/stage6_dataset_receipt.py",
                   "--split", "holdout", "--freeze-commit", "a" * 40,
                   "--expected-bundle-digest", self.expected if digest is None else digest,
                   "--out", str(self.receipt if out is None else out),
                   str(self.dataset if dataset is None else dataset)]
        if attest:
            command.insert(command.index("--out"), "--attest-isolated")
        return subprocess.run(command, cwd=self.bundle, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)

    def assert_refused(self, run, fragment, out=None):
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn("refused", run.stderr)
        self.assertIn(fragment, run.stderr)
        self.assertFalse((self.receipt if out is None else out).exists())

    def manifest(self) -> dict:
        return json.loads((self.bundle / bundle.BUNDLE_MANIFEST_NAME).read_text(encoding="utf-8"))

    def write_manifest(self, manifest: dict, *, redigest: bool = True) -> None:
        if redigest:  # the attacker recomputes the manifest's own digest
            manifest["content_digest"] = receipt_tool.content_digest(bundle.MANIFEST_SCHEMA, manifest["files"])
        (self.bundle / bundle.BUNDLE_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def entry_for(self, relative: str) -> dict:
        data = (self.bundle / relative).read_bytes().replace(b"\r\n", b"\n")
        return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    # -- the positive path ---------------------------------------------------------

    def test_a_clean_bundle_verifies_against_the_external_digest(self):
        before = tree(self.bundle)
        script = r'''
import importlib.util, json, sys
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
tool = load("tool", "eval/v2/stage6_dataset_receipt.py")
cases, expected = json.load(sys.stdin)
digest, count = tool.verify_bundle(expected_digest=expected)
real = tool.load_contract()
class Stub:
    case_errors = staticmethod(real.case_errors)
    @staticmethod
    def dataset_plan_errors(cases, split): return []
built = tool.build_receipt(json.dumps(cases).encode("utf-8"), split="holdout", freeze_merge_commit="a" * 40,
                           input_bundle_digest=digest, input_file_count=count, contract=Stub)
print(json.dumps({"digest": digest, "count": count, "receipt_digest": built["input_bundle_digest"]}))
'''
        # Programmatic use runs under -B, as the brief says: an importer's own process decides
        # whether the imported tool module is cached. The CLI suppresses bytecode itself
        # (test_the_cli_never_writes_into_the_bundle).
        run = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script], cwd=self.bundle,
                             input=json.dumps([[support.exchange_case("r-1"), support.a23()], self.expected]),
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual((result["digest"], result["receipt_digest"], result["count"]),
                         (self.expected, self.expected, 27))
        self.assertEqual(tree(self.bundle), before)  # nothing was added to the bundle

    def test_the_cli_never_writes_into_the_bundle(self):
        before = tree(self.bundle)
        for flags in (("-B",), ()):  # the tool itself suppresses bytecode
            with self.subTest(flags=flags):
                run = self.cli(flags=flags)
                self.assert_refused(run, "distribution plan")  # got all the way to the contract
                self.assertEqual(tree(self.bundle), before)

    def test_the_external_digest_and_the_attestation_are_required(self):
        run = self.cli(attest=False)
        self.assert_refused(run, "--attest-isolated")
        for value in ("abc", self.expected.upper(), self.expected[:-1]):
            with self.subTest(value=value):
                self.assert_refused(self.cli(digest=value), "64 lowercase hex")
        command = [sys.executable, "-I", "-S", "-B", "eval/v2/stage6_dataset_receipt.py", "--split", "holdout",
                   "--freeze-commit", "a" * 40, "--attest-isolated", "--out", str(self.receipt), str(self.dataset)]
        run = subprocess.run(command, cwd=self.bundle, capture_output=True, text=True, encoding="utf-8",
                             timeout=120)
        self.assertNotEqual(run.returncode, 0)  # --expected-bundle-digest is a required argument
        self.assertFalse(self.receipt.exists())

    # -- A-D: the bundle manifest is not its own authority --------------------------

    def test_a_dropped_input_with_a_recomputed_digest(self):
        manifest = self.manifest()
        dropped = manifest["files"].pop(0)
        (self.bundle / dropped["path"]).unlink()
        self.write_manifest(manifest)
        self.assert_refused(self.cli(), "differs from the expected bundle digest")

    def test_b_added_input_with_a_recomputed_digest(self):
        (self.bundle / "docs/v2/extra-guidance.md").write_text("x", encoding="utf-8")
        manifest = self.manifest()
        manifest["files"] = sorted(manifest["files"] + [self.entry_for("docs/v2/extra-guidance.md")],
                                   key=lambda entry: entry["path"])
        self.write_manifest(manifest)
        self.assert_refused(self.cli(), "differs from the expected bundle digest")

    def test_c_rehashed_input_with_a_recomputed_digest(self):
        target = "eval/v2/spec/stage6-holdout-plan.json"
        (self.bundle / target).write_bytes((self.bundle / target).read_bytes() + b" ")
        manifest = self.manifest()
        manifest["files"] = [self.entry_for(target) if entry["path"] == target else entry
                             for entry in manifest["files"]]
        self.write_manifest(manifest)
        self.assert_refused(self.cli(), "differs from the expected bundle digest")

    def test_d_wrong_expected_digest(self):
        self.assert_refused(self.cli(digest="f" * 64), "differs from the expected bundle digest")

    # -- E-G: the tree is exact ---------------------------------------------------

    def test_e_extra_file_at_the_root(self):
        (self.bundle / "notes.md").write_text("draft", encoding="utf-8")
        self.assert_refused(self.cli(), "file set is not exact")

    def test_f_extra_implementation_file_nested(self):
        (self.bundle / "eval_v2").mkdir()
        (self.bundle / "eval_v2" / "action_loop.py").write_text("x = 1\n", encoding="utf-8")
        self.assert_refused(self.cli(), "file set is not exact")
        shutil.rmtree(self.bundle / "eval_v2")
        for extra in ("eval/v2/stage6-dev.json", "eval/v2/old-receipt.json", "docs/v2/tmp.out"):
            (self.bundle / extra).write_text("{}", encoding="utf-8")
            with self.subTest(extra=extra):
                self.assert_refused(self.cli(), "file set is not exact")
            (self.bundle / extra).unlink()

    def test_g_python_cache(self):
        cache = self.bundle / "eval/v2/__pycache__"
        cache.mkdir()
        self.assert_refused(self.cli(), "extra directory")  # even an empty cache directory
        (cache / "stage6_case_contract.cpython-312.pyc").write_bytes(b"\x00")
        self.assert_refused(self.cli(), "file set is not exact")

    # -- H-I: the bundle is input-only -----------------------------------------------

    def test_h_receipt_inside_the_bundle(self):
        for out in (self.bundle / "receipt.json", self.bundle / "eval" / ".." / "receipt.json",
                    self.bundle / "eval/v2/receipt.json"):
            with self.subTest(out=str(out)):
                self.assert_refused(self.cli(out=out), "outside the bundle", out=out)
        self.assertFalse((self.bundle / "receipt.json").exists())

    def test_i_dataset_inside_the_bundle(self):
        inside = self.bundle / "dataset.json"
        shutil.copyfile(self.dataset, inside)
        self.assert_refused(self.cli(dataset=inside), "outside the bundle")
        traversal = self.bundle / "eval" / ".." / "dataset.json"
        self.assert_refused(self.cli(dataset=traversal), "outside the bundle")

    # -- J-K: the manifest itself is strictly validated -------------------------------

    def test_j_malformed_manifest_entries(self):
        original = self.manifest()
        variants = {
            "traversal": lambda m: m["files"][0].update(path="../outside.md"),
            "absolute": lambda m: m["files"][0].update(path="/abs.md"),
            "backslash": lambda m: m["files"][0].update(path="docs\\x.md"),
            "drive": lambda m: m["files"][0].update(path="C:/x.md"),
            "hidden": lambda m: m["files"][0].update(path=".hidden/x.md"),
            "not a string": lambda m: m["files"][0].update(path=5),
            "manifest name": lambda m: m["files"][0].update(path="bundle-manifest.json"),
            "unsorted": lambda m: m["files"].reverse(),
            "duplicate": lambda m: m["files"].append(dict(m["files"][-1])),
            "upper sha": lambda m: m["files"][0].update(sha256=m["files"][0]["sha256"].upper()),
            "short sha": lambda m: m["files"][0].update(sha256="ab"),
            "extra entry key": lambda m: m["files"][0].update(note="x"),
            "entry not an object": lambda m: m["files"].__setitem__(0, "x"),
            "no files": lambda m: m.update(files=[]),
            "files not a list": lambda m: m.update(files={}),
            "schema": lambda m: m.update(schema="v2-author-bundle/1"),
            "input schema": lambda m: m.update(input_manifest_schema="x"),
            "normalization": lambda m: m.update(hash_normalization="none"),
            "extra field": lambda m: m.update(note="x"),
            "missing field": lambda m: m.pop("base_commit"),
        }
        for label, mutate in variants.items():
            manifest = copy.deepcopy(original)
            mutate(manifest)
            self.write_manifest(manifest, redigest=False)
            with self.subTest(variant=label):
                run = self.cli()
                self.assert_refused(run, "bundle")
                self.assertNotIn("Traceback", run.stderr)
        (self.bundle / bundle.BUNDLE_MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        self.assert_refused(self.cli(), "not UTF-8 JSON")

    def test_k_incorrect_byte_counts(self):
        original = self.manifest()
        for label, value in (("off by one", original["files"][0]["bytes"] + 1), ("string", "12"),
                             ("bool", True), ("negative", -1)):
            manifest = copy.deepcopy(original)
            manifest["files"][0]["bytes"] = value
            self.write_manifest(manifest)  # bytes are not part of the digest
            with self.subTest(variant=label):
                fragment = "byte count does not match" if label == "off by one" else "non-negative integer"
                self.assert_refused(self.cli(), fragment)


class DatasetStateTests(unittest.TestCase):
    def test_no_stage6_dataset_seal_or_unseal_exists(self):
        names = [path.relative_to(ROOT).as_posix() for path in (ROOT / "eval").rglob("*") if path.is_file()]
        names += [path.relative_to(ROOT).as_posix() for path in (ROOT / "tools").glob("*")]
        for name in names:
            lowered = name.lower()
            with self.subTest(name=name):
                if "stage6" in lowered:
                    self.assertFalse(any(token in lowered for token in (
                        "dev.json", "validation.json", "holdout.json", "holdout.manifest", "receipt.json",
                        "unseal", "seal_")), name)


if __name__ == "__main__":
    unittest.main()
