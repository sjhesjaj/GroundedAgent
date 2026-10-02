"""Build the author receipt of one Stage 6 dataset (docs/v2/stage6-author-brief.md §6).

    python -B eval/v2/stage6_dataset_receipt.py --split <holdout|dev|validation> \\
        --freeze-commit <40-hex> --expected-bundle-digest <64-hex> --attest-isolated \\
        --out <receipt outside the bundle> <dataset outside the bundle>

Stdlib only, like the case contract beside it, so it runs unchanged from the
exported Stage 6 author bundle. Fail-closed, in this order, nothing written
until everything passes:

1. The trust anchor: the expected input-bundle digest is supplied out of band
   by the person who starts the author (it comes from the reviewed, frozen
   repository manifest). The bundle's own bundle-manifest.json is never its
   own authority: a manifest that drops, adds or re-hashes a file and
   recomputes its digest is refused because the digest no longer matches.
2. The bundle: bundle-manifest.json is strictly validated; the bundle tree
   holds exactly the listed files plus bundle-manifest.json - no other file,
   no other directory, no symlink, no __pycache__ - and every file's
   LF-normalized sha256 and byte length match. The bundle is input-only.
3. Paths: the dataset and the receipt both resolve outside the bundle.
4. The dataset: a JSON array of v2-stage6-case/1 cases; every case passes
   eval/v2/stage6_case_contract.py, case ids are unique, and the split meets
   eval/v2/spec/stage6-holdout-plan.json.
5. The receipt (v2-stage6-dataset-receipt/1): the dataset's raw-byte sha256,
   its counts and distributions, the validation results, the verified bundle
   digest and the author's isolation attestation - never a path, never case
   content.

No bytecode is written (the checkers are loaded with sys.dont_write_bytecode),
so running this tool never adds a file to the bundle. Nothing here reads the
system clock, the network, or anything outside the bundle and the two paths.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

BUNDLE_ROOT = Path(__file__).resolve().parent.parent.parent
CONTRACT_PATH = Path(__file__).resolve().parent / "stage6_case_contract.py"
BUNDLE_MANIFEST_NAME = "bundle-manifest.json"
BUNDLE_SCHEMA = "v2-stage6-author-bundle/1"
INPUT_MANIFEST_SCHEMA = "v2-stage6-holdout-input-manifest/1"
HASH_NORMALIZATION = "crlf-to-lf"
RECEIPT_SCHEMA = "v2-stage6-dataset-receipt/1"
SPLITS = ("holdout", "dev", "validation")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_KEYS = frozenset({"schema", "input_manifest_schema", "base_commit", "hash_normalization",
                           "content_digest", "files"})
FILE_KEYS = frozenset({"path", "sha256", "bytes"})

# The author's isolation statement (brief §1), fixed so receipts are comparable.
ATTESTATION = {
    "fresh_isolated_context": True,
    "frozen_bundle_only": True,
    "implementation_visible": False,
    "other_datasets_visible": False,
    "failure_analysis_visible": False,
    "agent_runs": 0,
    "oracle_runs": 0,
    "external_sources_used": False,
}
RECEIPT_KEYS = (
    "schema", "split", "freeze_merge_commit", "input_bundle_digest", "input_file_count",
    "dataset_sha256", "case_count", "scenario_counts", "archetype_counts", "final_counts",
    "final_status_counts", "persona_counts", "distinct_virtual_now", "contract_validation",
    "plan_validation", "author_context",
)


class ReceiptRefused(RuntimeError):
    """The receipt was refused; nothing was written."""


@contextmanager
def no_bytecode():
    """Load checkers without writing __pycache__ into the input-only bundle."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


def load_contract():
    with no_bytecode():
        spec = importlib.util.spec_from_file_location("v2_stage6_case_contract_receipt", CONTRACT_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def content_digest(schema: str, files: list) -> str:
    """The export tool's digest: the input manifest schema and (path, sha256) pairs only."""
    payload = {"schema": schema, "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def require_digest(value: object, what: str = "the expected bundle digest") -> str:
    if not isinstance(value, str) or not HEX64.match(value):
        raise ReceiptRefused(what + " must be exactly 64 lowercase hex characters")
    return value


def _plain_relative(path: object) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        return False
    pure = PurePosixPath(path)
    return not pure.is_absolute() and all(
        part not in ("", ".", "..") and not part.startswith(".") for part in pure.parts)


def _tree(root: Path) -> tuple[set[str], set[str]]:
    """(files, directories) under root, relative POSIX; any symlink is refused."""
    files, directories = set(), set()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirnames:
            path = base / name
            if path.is_symlink():
                raise ReceiptRefused("the bundle contains a symlink")
            directories.add(path.relative_to(root).as_posix())
        for name in filenames:
            path = base / name
            if path.is_symlink():
                raise ReceiptRefused("the bundle contains a symlink")
            files.add(path.relative_to(root).as_posix())
    return files, directories


def verify_bundle(root: Path = BUNDLE_ROOT, *, expected_digest: str) -> tuple[str, int]:
    """(verified digest, file count) of the exact frozen bundle, or ReceiptRefused."""
    expected_digest = require_digest(expected_digest)
    path = root / BUNDLE_MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        raise ReceiptRefused("run from the root of an exported Stage 6 author bundle")
    try:
        manifest = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ReceiptRefused("bundle-manifest.json is not UTF-8 JSON") from None
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_KEYS:
        raise ReceiptRefused("bundle-manifest.json has the wrong fields")
    if (manifest["schema"] != BUNDLE_SCHEMA or manifest["input_manifest_schema"] != INPUT_MANIFEST_SCHEMA
            or manifest["hash_normalization"] != HASH_NORMALIZATION):
        raise ReceiptRefused("not a Stage 6 author bundle manifest")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ReceiptRefused("the bundle manifest lists no files")
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != FILE_KEYS:
            raise ReceiptRefused("a bundle manifest entry has the wrong fields")
        if not _plain_relative(entry["path"]) or entry["path"] == BUNDLE_MANIFEST_NAME:
            raise ReceiptRefused("a bundle manifest path is not a plain relative path")
        if not isinstance(entry["sha256"], str) or not HEX64.match(entry["sha256"]):
            raise ReceiptRefused("a bundle manifest sha256 is not 64 lowercase hex characters")
        size = entry["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ReceiptRefused("a bundle manifest byte count is not a non-negative integer")
    paths = [entry["path"] for entry in files]
    if paths != sorted(set(paths)):
        raise ReceiptRefused("bundle manifest paths must be unique and sorted")
    # The trust anchor: never the manifest's own claim.
    digest = content_digest(INPUT_MANIFEST_SCHEMA, files)
    if manifest["content_digest"] != digest:
        raise ReceiptRefused("the bundle manifest digest does not recompute")
    if digest != expected_digest:
        raise ReceiptRefused("the bundle is not the frozen bundle: its digest differs from the "
                             "expected bundle digest")
    # Exactly the listed files, nothing else - the bundle is input-only.
    actual_files, actual_directories = _tree(root)
    wanted = set(paths) | {BUNDLE_MANIFEST_NAME}
    if actual_files != wanted:
        extra, missing = sorted(actual_files - wanted), sorted(wanted - actual_files)
        raise ReceiptRefused("the bundle file set is not exact (" + str(len(extra)) + " extra, "
                             + str(len(missing)) + " missing)")
    parents = {parent.as_posix() for entry in paths for parent in PurePosixPath(entry).parents
               if parent.as_posix() != "."}
    if actual_directories != parents:
        raise ReceiptRefused("the bundle has an extra directory")
    for entry in files:
        data = normalized((root / entry["path"]).read_bytes())
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ReceiptRefused("a bundle file was changed: " + entry["path"])
        if len(data) != entry["bytes"]:
            raise ReceiptRefused("a bundle file's byte count does not match: " + entry["path"])
    return digest, len(files)


def require_outside(path: Path, what: str, root: Path = BUNDLE_ROOT) -> Path:
    """The resolved path (symlinks and .. followed) must lie outside the bundle."""
    resolved = Path(os.path.realpath(path))
    bundle = Path(os.path.realpath(root))
    if resolved == bundle or resolved.is_relative_to(bundle):
        raise ReceiptRefused(what + " must be outside the bundle; the bundle is input-only")
    return resolved


def _counts(values) -> dict:
    out: dict = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return {key: out[key] for key in sorted(out)}


def build_receipt(data: bytes, *, split: str, freeze_merge_commit: str, input_bundle_digest: str,
                  input_file_count: int, contract=None) -> dict:
    """The receipt of one dataset's raw bytes. Refuses unless every check passes.

    `input_bundle_digest` must be the digest verify_bundle() verified against
    the externally supplied expected digest.
    """
    if split not in SPLITS:
        raise ReceiptRefused("split must be one of " + ", ".join(SPLITS))
    if not isinstance(freeze_merge_commit, str) or not COMMIT.match(freeze_merge_commit):
        raise ReceiptRefused("the freeze commit must be a full 40-character lowercase sha")
    require_digest(input_bundle_digest, "the input bundle digest")
    contract = contract or load_contract()
    try:
        cases = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ReceiptRefused("the dataset is not UTF-8 JSON") from None
    if not isinstance(cases, list) or not cases or not all(isinstance(case, dict) for case in cases):
        raise ReceiptRefused("the dataset must be a non-empty JSON array of cases")
    with no_bytecode():
        invalid = [index for index, case in enumerate(cases) if contract.case_errors(case)]
        if invalid:
            raise ReceiptRefused(str(len(invalid)) + " case(s) fail the case contract, first at index "
                                 + str(invalid[0]))
        ids = [case["case_id"] for case in cases]
        if len(set(ids)) != len(ids):
            raise ReceiptRefused("case ids are not unique")
        plan_errors = contract.dataset_plan_errors(cases, split)
    if plan_errors:
        raise ReceiptRefused("the split does not meet the distribution plan: " + "; ".join(plan_errors))
    expected = [case["expected_action"] for case in cases]
    return {
        "schema": RECEIPT_SCHEMA,
        "split": split,
        "freeze_merge_commit": freeze_merge_commit,
        "input_bundle_digest": input_bundle_digest,
        "input_file_count": input_file_count,
        "dataset_sha256": hashlib.sha256(data).hexdigest(),
        "case_count": len(cases),
        "scenario_counts": _counts(case["scenario"] for case in cases),
        "archetype_counts": _counts(case["archetype"] for case in cases),
        "final_counts": _counts(case["expected_answerability"]["final"] for case in cases),
        "final_status_counts": _counts(item["final_status"] for item in expected if item is not None),
        "persona_counts": _counts(case["initial_state"]["trusted_context"]["persona_id"] for case in cases),
        "distinct_virtual_now": len({case["virtual_now"] for case in cases}),
        "contract_validation": {"all_cases_valid": True, "error_count": 0},
        "plan_validation": {"all_rules_met": True, "error_count": 0},
        "author_context": dict(ATTESTATION),
    }


def receipt_bytes(receipt: dict) -> bytes:
    return (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    with no_bytecode():  # this run never writes bytecode into the bundle
        return _main(argv)


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--split", required=True)
    parser.add_argument("--freeze-commit", required=True)
    parser.add_argument("--expected-bundle-digest", required=True)
    parser.add_argument("--attest-isolated", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.attest_isolated:
            raise ReceiptRefused("--attest-isolated is required (brief §1, §6)")
        expected = require_digest(args.expected_bundle_digest)
        dataset = require_outside(args.dataset, "the dataset")
        out = None if args.out is None else require_outside(args.out, "the receipt")
        if out is not None and out.exists():
            raise ReceiptRefused("the receipt file already exists")
        digest, count = verify_bundle(expected_digest=expected)
        receipt = build_receipt(dataset.read_bytes(), split=args.split,
                                freeze_merge_commit=args.freeze_commit,
                                input_bundle_digest=digest, input_file_count=count)
    except (ReceiptRefused, OSError) as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 1
    data = receipt_bytes(receipt)
    if out is not None:
        out.write_bytes(data)
    else:
        sys.stdout.write(data.decode("utf-8"))
    print(json.dumps({"split": receipt["split"], "case_count": receipt["case_count"],
                      "input_bundle_digest": receipt["input_bundle_digest"],
                      "dataset_sha256": receipt["dataset_sha256"],
                      "receipt_sha256": hashlib.sha256(data).hexdigest()}),
          file=sys.stderr if out is None else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
