"""Open the sealed Stage 6 holdout exactly once (docs/v2/stage6-4b-design.md §5).

    python -B tools/unseal_v2_stage6_holdout.py <sealed-holdout-file> <author-receipt-file>

Committed before any Stage 6 DEV / VALIDATION authoring and run only for the
final Stage 6 holdout evaluation. Both paths are supplied by a person at
opening time; nothing here knows, searches for, derives or records where the
sealed files live, and neither path is ever printed.

Order, fail-closed at every step, nothing written until everything passes:

1. Repository: the working tree is clean; eval/v2/stage6-holdout.manifest.json
   has the exact schema, status and key set and every pinned value; neither
   destination exists or has history on any ref, so the holdout opens once.
2. Frozen author inputs: eval/v2/stage6-holdout-input.manifest.json lists the
   27 files of the sealed digest, and every file's LF-normalized sha256 and
   byte count match, so the opening uses exactly the contract, specs, seeds
   and policy corpus the author saw.
3. Receipt: the raw bytes are read once and hashed before anything else, then
   parsed from those same bytes against the exact v2-stage6-dataset-receipt/1
   key set (eval/v2/stage6_dataset_receipt.py); every field equals the sealed
   manifest with exact JSON types.
4. Holdout: the raw bytes are read once and hashed first, then parsed from
   those bytes: a UTF-8 JSON array of 25 cases, each passing
   stage6_case_contract.case_errors, unique case ids, the holdout split
   passing dataset_plan_errors, and the safe distributions recomputed from the
   cases equal to the manifest. Refusals name an index, a category or a
   count, never case content.
5. The same raw bytes - never re-serialized - go to eval/v2/stage6-holdout.json
   and eval/v2/stage6-holdout.receipt.json through fsynced temp files and an
   atomic rename, then are re-hashed. On a handled failure partial outputs are
   removed. No git commit is made: the opened files stay untracked.

It does not run the Agent, an LLM, the oracle or any scoring, and opens no
other dataset. Stdlib only; no network. The frozen checker is loaded with
sys.dont_write_bytecode, so opening adds no __pycache__ to the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_RELATIVE = "eval/v2/stage6-holdout.manifest.json"
INPUT_MANIFEST_RELATIVE = "eval/v2/stage6-holdout-input.manifest.json"
CHECKER_RELATIVE = "eval/v2/stage6_case_contract.py"
HOLDOUT_DESTINATION = "eval/v2/stage6-holdout.json"
RECEIPT_DESTINATION = "eval/v2/stage6-holdout.receipt.json"
MANIFEST_SCHEMA = "v2-stage6-sealed-holdout-manifest/1"
INPUT_MANIFEST_SCHEMA = "v2-stage6-holdout-input-manifest/1"
RECEIPT_SCHEMA = "v2-stage6-dataset-receipt/1"
HASH_NORMALIZATION = "crlf-to-lf"
SPLIT = "holdout"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Pinned when the seal was committed; the repo manifest must agree with them.
PINNED = {
    "sealed_against_repo_commit": "f27ee9583a971725b33d579a3d8fceba24b7d768",
    "input_bundle_digest": "15ac3593ce55a0b7d04d4f3522ebabc370bf57c98b1dcdef89472e34e9d5d371",
    "input_file_count": 27,
    "holdout_sha256": "64925a4d8e7d66f2e150a9a8f2286b4bfdcac74e077448a990798ab6d62d3057",
    "author_receipt_sha256": "89e5834fcc1efa8017da54b2dd957002af8861a496d7c58bb6144aeb3915d46b",
    "case_count": 25,
}

# The sealed manifest holds exactly these keys: safe metadata, no path or content.
MANIFEST_KEYS = (
    "schema", "status", "sealed_against_repo_commit", "input_bundle_digest", "input_file_count",
    "holdout_sha256", "author_receipt_sha256", "case_count", "scenario_counts", "archetype_counts",
    "final_counts", "final_status_counts", "persona_counts", "distinct_virtual_now",
    "contract_validation", "plan_validation", "author_context", "isolation_note",
)
DISTRIBUTIONS = ("scenario_counts", "archetype_counts", "final_counts", "final_status_counts",
                 "persona_counts", "distinct_virtual_now")
PASSED = {
    "contract_validation": {"all_cases_valid": True, "error_count": 0},
    "plan_validation": {"all_rules_met": True, "error_count": 0},
}
# The receipt tool's fixed isolation statement (stage6_dataset_receipt.ATTESTATION).
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
ISOLATION_NOTE = ("Process/context isolation, not filesystem access control. Hashes prove "
                  "immutability of the sealed bytes, not that filesystem permissions made the "
                  "files unreadable.")

INPUT_MANIFEST_KEYS = frozenset({"schema", "description", "base_commit", "hash_normalization",
                                 "content_digest", "files"})
INPUT_FILE_KEYS = frozenset({"path", "sha256", "bytes"})

# The frozen receipt contract (stage6_dataset_receipt.RECEIPT_KEYS): exactly
# these top-level keys besides schema and split, each equal to one manifest
# field. No wrappers, no aliases, no other keys.
RECEIPT_FIELDS = {
    "freeze_merge_commit": "sealed_against_repo_commit",
    "input_bundle_digest": "input_bundle_digest",
    "input_file_count": "input_file_count",
    "dataset_sha256": "holdout_sha256",
    "case_count": "case_count",
    "scenario_counts": "scenario_counts",
    "archetype_counts": "archetype_counts",
    "final_counts": "final_counts",
    "final_status_counts": "final_status_counts",
    "persona_counts": "persona_counts",
    "distinct_virtual_now": "distinct_virtual_now",
    "contract_validation": "contract_validation",
    "plan_validation": "plan_validation",
    "author_context": "author_context",
}
RECEIPT_KEYS = frozenset({"schema", "split"} | set(RECEIPT_FIELDS))


class UnsealRefused(RuntimeError):
    """Opening refused; nothing was written."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same(a: object, b: object) -> bool:
    """JSON equality with exact types: True is not 1, 25.0 is not 25, dicts compare key for key."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


@contextmanager
def no_bytecode():
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = previous


# ---------------------------------------------------------------- repository

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def check_repository(root: Path) -> None:
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise UnsealRefused("git status failed")
    if status.stdout.strip():
        raise UnsealRefused("the working tree is not clean")
    for relative in (HOLDOUT_DESTINATION, RECEIPT_DESTINATION):
        destination = root / relative
        if destination.exists() or destination.is_symlink():
            raise UnsealRefused(relative + " already exists: the holdout was already opened")
        # Every ref, and side branches merged away, so deleting a copy never re-arms the gate.
        history = _git(root, "log", "--all", "--full-history", "--format=%H", "--", relative)
        if history.returncode != 0 or history.stdout.strip():
            raise UnsealRefused(relative + " has git history: the holdout was already opened")


def load_manifest(root: Path, pinned: dict) -> dict:
    path = root / MANIFEST_RELATIVE
    if not path.is_file():
        raise UnsealRefused(MANIFEST_RELATIVE + " is missing")
    try:
        manifest = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UnsealRefused("the sealed manifest is not UTF-8 JSON") from None
    if not isinstance(manifest, dict) or not _same(manifest.get("schema"), MANIFEST_SCHEMA):
        raise UnsealRefused("unknown sealed manifest schema")
    if not _same(manifest.get("status"), "sealed"):
        raise UnsealRefused("the sealed manifest status is not 'sealed'")
    if set(manifest) != set(MANIFEST_KEYS):
        raise UnsealRefused("the sealed manifest keys differ from the sealed metadata contract")
    for key, value in pinned.items():
        if not _same(manifest[key], value):
            raise UnsealRefused("the sealed manifest " + key + " differs from the pinned value")
    for key, value in PASSED.items():
        if not _same(manifest[key], value):
            raise UnsealRefused("the sealed manifest " + key + " is not a passing validation")
    if not _same(manifest["author_context"], ATTESTATION):
        raise UnsealRefused("the sealed manifest author_context is not the isolation attestation")
    if not _same(manifest["isolation_note"], ISOLATION_NOTE):
        raise UnsealRefused("the sealed manifest isolation_note differs")
    return manifest


# ---------------------------------------------------------------- frozen author inputs

def _plain_relative(relative: object) -> str:
    if not isinstance(relative, str) or not relative:
        raise UnsealRefused("a frozen input path is not a string")
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or "\\" in relative or ":" in relative
            or any(part in ("", ".", "..") or part.startswith(".") for part in pure.parts)):
        raise UnsealRefused("a frozen input path is not a plain repo-relative path")
    return relative


def input_content_digest(files: list[dict]) -> str:
    """The export tool's digest: the input manifest schema and (path, sha256) pairs only."""
    payload = {"schema": INPUT_MANIFEST_SCHEMA,
               "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files]}
    return sha256_hex(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))


def verify_frozen_inputs(root: Path, manifest: dict) -> None:
    """The author inputs in the repo are byte for byte (LF-normalized) the sealed ones."""
    try:
        inputs = json.loads((root / INPUT_MANIFEST_RELATIVE).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise UnsealRefused(INPUT_MANIFEST_RELATIVE + " is missing or not UTF-8 JSON") from None
    if not isinstance(inputs, dict) or set(inputs) != INPUT_MANIFEST_KEYS:
        raise UnsealRefused("the frozen input manifest has the wrong fields")
    if not _same(inputs["schema"], INPUT_MANIFEST_SCHEMA):
        raise UnsealRefused("unknown frozen input manifest schema")
    if not _same(inputs["hash_normalization"], HASH_NORMALIZATION):
        raise UnsealRefused("the frozen input manifest hash normalization differs")
    files = inputs["files"]
    if not isinstance(files, list) or not all(isinstance(f, dict) and set(f) == INPUT_FILE_KEYS
                                              for f in files):
        raise UnsealRefused("the frozen input manifest has no well-formed file list")
    if len(files) != manifest["input_file_count"]:
        raise UnsealRefused("the frozen input file count differs from the sealed manifest")
    for entry in files:
        if not isinstance(entry["sha256"], str) or not HEX64.match(entry["sha256"]):
            raise UnsealRefused("a frozen input sha256 is not 64 lowercase hex characters")
        size = entry["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UnsealRefused("a frozen input byte count is not a non-negative integer")
    if not (inputs["content_digest"] == manifest["input_bundle_digest"] == input_content_digest(files)):
        raise UnsealRefused("the frozen input content digest differs from the sealed digest")
    paths = [_plain_relative(f["path"]) for f in files]
    if paths != sorted(set(paths)):
        raise UnsealRefused("the frozen input paths are not unique and sorted")
    repo = root.resolve()
    for entry in files:
        source = (root / entry["path"]).resolve()
        if not source.is_relative_to(repo) or not source.is_file():
            raise UnsealRefused("a frozen input is missing: " + entry["path"])
        data = source.read_bytes().replace(b"\r\n", b"\n")
        if sha256_hex(data) != entry["sha256"]:
            raise UnsealRefused("a frozen input changed since sealing: " + entry["path"])
        if len(data) != entry["bytes"]:
            raise UnsealRefused("a frozen input byte count changed since sealing: " + entry["path"])


# ---------------------------------------------------------------- receipt

def _unique_keys(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def verify_receipt(data: bytes, manifest: dict) -> dict:
    """`data` is the receipt's raw bytes, read once; hashed before it is parsed."""
    if sha256_hex(data) != manifest["author_receipt_sha256"]:
        raise UnsealRefused("author receipt sha256 mismatch")
    try:
        receipt = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, ValueError):
        raise UnsealRefused("the author receipt is not UTF-8 JSON with unique keys") from None
    if not isinstance(receipt, dict):
        raise UnsealRefused("the author receipt must be a JSON object")
    if not _same(receipt.get("schema"), RECEIPT_SCHEMA):
        raise UnsealRefused("the author receipt schema is not " + RECEIPT_SCHEMA)
    if set(receipt) != RECEIPT_KEYS:
        raise UnsealRefused("the author receipt keys differ from the frozen receipt contract")
    if not _same(receipt["split"], SPLIT):
        raise UnsealRefused("the author receipt is not for the holdout split")
    for field, key in RECEIPT_FIELDS.items():
        if not _same(receipt[field], manifest[key]):
            raise UnsealRefused("the author receipt disagrees with the sealed manifest on " + field)
    return receipt


# ---------------------------------------------------------------- holdout

def load_checker(root: Path):
    """The frozen stage6_case_contract.py of this repository, loaded by path."""
    spec = importlib.util.spec_from_file_location("_sealed_stage6_case_contract", root / CHECKER_RELATIVE)
    if spec is None or spec.loader is None:
        raise UnsealRefused("the Stage 6 case contract is missing")
    module = importlib.util.module_from_spec(spec)
    with no_bytecode():
        spec.loader.exec_module(module)
    return module


def _counts(values) -> dict:
    out: dict = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return {key: out[key] for key in sorted(out)}


def safe_distributions(cases: list) -> dict:
    """Recomputed from the cases; the same fields the author receipt records."""
    expected = [case["expected_action"] for case in cases]
    return {
        "scenario_counts": _counts(case["scenario"] for case in cases),
        "archetype_counts": _counts(case["archetype"] for case in cases),
        "final_counts": _counts(case["expected_answerability"]["final"] for case in cases),
        "final_status_counts": _counts(item["final_status"] for item in expected if item is not None),
        "persona_counts": _counts(case["initial_state"]["trusted_context"]["persona_id"] for case in cases),
        "distinct_virtual_now": len({case["virtual_now"] for case in cases}),
    }


def verify_holdout(data: bytes, manifest: dict, checker) -> list:
    """`data` is the holdout's raw bytes, read once; hashed before it is parsed."""
    if sha256_hex(data) != manifest["holdout_sha256"]:
        raise UnsealRefused("sealed holdout sha256 mismatch")
    try:
        cases = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UnsealRefused("the sealed holdout is not UTF-8 JSON") from None
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise UnsealRefused("the sealed holdout must be a JSON array of case objects")
    if len(cases) != manifest["case_count"]:
        raise UnsealRefused("the sealed holdout has " + str(len(cases)) + " cases, the manifest seals "
                            + str(manifest["case_count"]))
    with no_bytecode():
        for index, case in enumerate(cases):
            try:
                errors = checker.case_errors(case)
                kind = "schema" if errors and checker.schema_errors(case) else "cross-field"
            except Exception as exc:  # a checker failure is a refusal, never a pass
                raise UnsealRefused("the case contract check raised " + type(exc).__name__
                                    + " at case #" + str(index)) from None
            if errors:
                raise UnsealRefused("case #" + str(index) + " violates the case contract ("
                                    + kind + ", " + str(len(errors)) + " error(s))")
        ids = [case["case_id"] for case in cases]
        if len(set(ids)) != len(ids):
            raise UnsealRefused("case_id values are not unique (" + str(len(ids) - len(set(ids)))
                                + " duplicate(s))")
        try:
            plan_errors = checker.dataset_plan_errors(cases, SPLIT)
        except Exception as exc:
            raise UnsealRefused("the distribution plan check raised " + type(exc).__name__) from None
    if plan_errors:
        raise UnsealRefused("the holdout does not meet the distribution plan ("
                            + str(len(plan_errors)) + " rule(s) failed)")
    distributions = safe_distributions(cases)
    for key in DISTRIBUTIONS:
        if not _same(distributions[key], manifest[key]):
            raise UnsealRefused(key + " recomputed from the holdout differs from the sealed manifest")
    return cases


# ---------------------------------------------------------------- write

def _stage(root: Path, relative: str, data: bytes) -> Path:
    destination = root / relative
    handle, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp",
                                    dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def write_exact(root: Path, items: list[tuple[str, bytes, str]]) -> None:
    """Every destination appears byte-exact with its sealed sha256, or none does."""
    staged: list[tuple[Path, Path, str]] = []
    placed: list[Path] = []
    try:
        for relative, data, _ in items:
            staged.append((_stage(root, relative, data), root / relative, sha256_hex(data)))
        for (temp, destination, digest), (_, _, sealed) in zip(staged, items):
            if digest != sealed:
                raise UnsealRefused(destination.name + " staged bytes differ from the sealed sha256")
            if destination.exists():
                raise UnsealRefused(destination.name + " appeared during opening")
            os.replace(temp, destination)
            placed.append(destination)
        for (_, destination, _), (_, _, sealed) in zip(staged, items):
            if sha256_hex(destination.read_bytes()) != sealed:
                raise UnsealRefused(destination.name + " does not match the sealed bytes")
    except BaseException:
        for destination in placed:
            destination.unlink(missing_ok=True)
        for temp, _, _ in staged:
            temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- entry points

def unseal(holdout_file: Path, receipt_file: Path, *, root: Path = REPO_ROOT,
           pinned: dict = PINNED) -> dict:
    with no_bytecode():
        check_repository(root)
        manifest = load_manifest(root, pinned)
        verify_frozen_inputs(root, manifest)
        receipt_bytes = Path(receipt_file).read_bytes()  # the only read of the receipt
        verify_receipt(receipt_bytes, manifest)
        holdout_bytes = Path(holdout_file).read_bytes()  # the only read of the holdout
        cases = verify_holdout(holdout_bytes, manifest, load_checker(root))
        write_exact(root, [(HOLDOUT_DESTINATION, holdout_bytes, manifest["holdout_sha256"]),
                           (RECEIPT_DESTINATION, receipt_bytes, manifest["author_receipt_sha256"])])
    return {"cases": len(cases), "destinations": [HOLDOUT_DESTINATION, RECEIPT_DESTINATION]}


def build_parser() -> argparse.ArgumentParser:
    """Exactly two human-supplied paths; no other option, no reset."""
    parser = argparse.ArgumentParser(description="Open the sealed Stage 6 holdout exactly once.")
    parser.add_argument("sealed_holdout_file", type=Path)
    parser.add_argument("author_receipt_file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = unseal(args.sealed_holdout_file, args.author_receipt_file)
    except UnsealRefused as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # never echo a supplied path or case content
        print("refused: " + type(exc).__name__, file=sys.stderr)
        return 1
    print("receipt hash verified")
    print("holdout hash verified")
    print(str(result["cases"]) + " cases validated")
    for relative in result["destinations"]:
        print("wrote " + relative)
    print("The opened files stay untracked; do not commit them. No evaluation was run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
