"""Open the sealed V2 holdout exactly once (design §7.4, D11, D23).

    python tools/unseal_v2_holdout.py <sealed-holdout-file> <seal-receipt-file>

Committed before any Stage 4.4 / Stage 5 implementation and not run until
Stage 5 ends. Both paths are supplied by a person at opening time; nothing here
knows, searches for, or derives where the sealed files live.

Order, fail-closed at every step, nothing written until everything passes:

1. Working tree clean (`git status --porcelain` empty), the sealed manifest
   present with the expected schema/status and pinned hashes, and the holdout
   never opened before (no destination file, no history for it).
2. Receipt: sha256 of the raw bytes, then JSON, then its safe fields must
   agree with the repo manifest. The receipt never controls a destination.
3. Holdout: sha256 of the raw bytes, then JSON, then the case contract
   (eval/v2/case_contract.py) and the sealed distribution for every case.
4. Raw bytes are copied - never re-serialized - to eval/v2/holdout.json and
   eval/v2/holdout.receipt.json via fsynced temp files and atomic rename, then
   re-hashed. No git commit is made.

Stdlib only; no Agent, no LLM, no network.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_RELATIVE = "eval/v2/holdout.manifest.json"
HOLDOUT_DESTINATION = "eval/v2/holdout.json"
RECEIPT_DESTINATION = "eval/v2/holdout.receipt.json"
CHECKER_RELATIVE = "eval/v2/case_contract.py"
MANIFEST_SCHEMA = "v2-sealed-holdout-manifest/1"

# Pinned when the seal was committed; the repo manifest must agree with them.
PINNED = {
    "holdout_sha256": "0d312305e62ffc3bf4c73cf3ee0715a8cbe1b910d44183b9bb8abf9cc2d88e8a",
    "seal_receipt_sha256": "1c899e4f8d54a168aef77489f9450f1d08f4166949a9164d935412fbae9ff002",
}

# Receipt field -> accepted key names (matched as a dotted-path suffix, so a
# field may sit at top level or inside a wrapper object).
RECEIPT_FIELDS = {
    "sealed_against_repo_commit": ("sealed_against_repo_commit", "freeze_repo_commit",
                                   "freeze_merge_commit"),
    "input_bundle_digest": ("input_bundle_digest",),
    "input_file_count": ("input_file_count",),
    "holdout_sha256": ("holdout_sha256",),
    "case_count": ("case_count",),
    "archetype_counts": ("archetype_counts",),
    "contract_validation.all_cases_valid": ("contract_validation.all_cases_valid",),
    "contract_validation.error_count": ("contract_validation.error_count",),
    "fixture_integrity.all_cases_valid": ("fixture_integrity.all_cases_valid",),
    "coverage.all_required_final_values_represented": ("coverage.all_required_final_values_represented",),
    "coverage.both_personas_represented": ("coverage.both_personas_represented",),
    "coverage.min_distinct_virtual_now_met": ("coverage.min_distinct_virtual_now_met",),
    "expected_action_all_null": ("expected_action_all_null",),
    "expected_final_state_all_null": ("expected_final_state_all_null",),
    "author_context.agent_runs": ("agent_runs",),
    "author_context.external_sources_used": ("external_sources_used",),
}


class UnsealRefused(RuntimeError):
    """Opening refused; nothing was written."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _get(mapping: dict, dotted: str) -> object:
    node: object = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise UnsealRefused("manifest is missing " + dotted)
        node = node[part]
    return node


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    out = {prefix: node} if prefix else {}
    if isinstance(node, dict):
        for key, value in node.items():
            out.update(_flatten(value, prefix + "." + str(key) if prefix else str(key)))
    return out


def _nonzero(counts: object) -> object:
    if isinstance(counts, dict):
        return {k: v for k, v in counts.items() if v != 0}
    return counts


# ---------------------------------------------------------------- preconditions

def check_repository(root: Path) -> None:
    run = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                         capture_output=True, text=True)
    if run.returncode != 0:
        raise UnsealRefused("git status failed")
    if run.stdout.strip():
        raise UnsealRefused("working tree is not clean")
    for relative in (HOLDOUT_DESTINATION, RECEIPT_DESTINATION):
        if (root / relative).exists():
            raise UnsealRefused(relative + " already exists: the holdout was already opened")
        history = subprocess.run(["git", "-C", str(root), "log", "--all", "--format=%H", "--", relative],
                                 capture_output=True, text=True)
        if history.returncode != 0 or history.stdout.strip():
            raise UnsealRefused(relative + " has git history: the holdout was already opened")


def load_manifest(root: Path, pinned: dict) -> dict:
    path = root / MANIFEST_RELATIVE
    if not path.is_file():
        raise UnsealRefused(MANIFEST_RELATIVE + " is missing")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise UnsealRefused("sealed manifest is not valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise UnsealRefused("unknown sealed manifest schema")
    if manifest.get("status") != "sealed":
        raise UnsealRefused("sealed manifest status is not 'sealed'")
    for key, value in pinned.items():
        if manifest.get(key) != value:
            raise UnsealRefused("sealed manifest " + key + " differs from the pinned value")
    return manifest


# ---------------------------------------------------------------- receipt

def verify_receipt(data: bytes, manifest: dict) -> dict:
    if sha256_hex(data) != manifest["seal_receipt_sha256"]:
        raise UnsealRefused("seal receipt sha256 mismatch")
    try:
        receipt = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise UnsealRefused("seal receipt is not valid JSON") from None
    if not isinstance(receipt, dict):
        raise UnsealRefused("seal receipt must be a JSON object")
    flat = _flatten(receipt)
    for field, aliases in RECEIPT_FIELDS.items():
        expected = _nonzero(_get(manifest, field))
        found = [value for path, value in flat.items()
                 if any(path == alias or path.endswith("." + alias) for alias in aliases)]
        if not found:
            raise UnsealRefused("seal receipt lacks " + field)
        if any(_nonzero(value) != expected or type(value) is not type(_get(manifest, field))
               for value in found):
            raise UnsealRefused("seal receipt disagrees with the manifest on " + field)
    return receipt


# ---------------------------------------------------------------- holdout

def load_checker(root: Path):
    spec = importlib.util.spec_from_file_location("_sealed_case_contract", root / CHECKER_RELATIVE)
    if spec is None or spec.loader is None:
        raise UnsealRefused("case contract checker is missing")
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ inside the repository while opening.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def verify_holdout(data: bytes, manifest: dict, root: Path) -> list:
    if sha256_hex(data) != manifest["holdout_sha256"]:
        raise UnsealRefused("sealed holdout sha256 mismatch")
    try:
        cases = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise UnsealRefused("sealed holdout is not valid JSON") from None
    if not isinstance(cases, list):
        raise UnsealRefused("sealed holdout must be a JSON array")
    if len(cases) != manifest["case_count"]:
        raise UnsealRefused("sealed holdout case count differs from the manifest")

    checker = load_checker(root)
    for index, case in enumerate(cases):
        try:
            errors = checker.case_errors(case)
        except Exception as exc:  # a checker failure is a refusal, never a pass
            raise UnsealRefused("case contract check failed: " + type(exc).__name__) from None
        if errors:
            raise UnsealRefused("case #" + str(index) + " violates the case contract")

    ids = [case["case_id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise UnsealRefused("case_id values are not unique")
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["archetype"]] = counts.get(case["archetype"], 0) + 1
    expected_counts = _nonzero(manifest["archetype_counts"])
    if counts != expected_counts:
        raise UnsealRefused("archetype distribution differs from the manifest")
    if {"A21", "A22", "A23"} & set(counts):
        raise UnsealRefused("Stage 6 archetypes are not allowed in the holdout")

    plan = checker.load_json(checker.SPEC_DIR / "holdout-plan.json")
    personas = {case["initial_state"]["trusted_context"]["persona_id"] for case in cases}
    if not set(plan["required_personas"]) <= personas:
        raise UnsealRefused("not every frozen persona is represented")
    if len({case["virtual_now"] for case in cases}) < plan["min_distinct_virtual_now"]:
        raise UnsealRefused("too few distinct virtual_now values")
    finals = {case["expected_answerability"]["final"] for case in cases}
    if not set(plan["required_final_values"]) <= finals:
        raise UnsealRefused("not every final outcome value is represented")
    if any(case["expected_action"] is not None or case["expected_final_state"] is not None
           for case in cases):
        raise UnsealRefused("expected_action / expected_final_state must be null")
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


def write_exact(root: Path, items: list[tuple[str, bytes]]) -> None:
    """Both destinations appear, byte-exact, or neither does."""
    staged: list[tuple[Path, Path, bytes]] = []
    placed: list[Path] = []
    try:
        for relative, data in items:
            staged.append((_stage(root, relative, data), root / relative, data))
        for temp, destination, _ in staged:
            if destination.exists():
                raise UnsealRefused(destination.name + " appeared during opening")
            os.replace(temp, destination)
            placed.append(destination)
        for _, destination, data in staged:
            if sha256_hex(destination.read_bytes()) != sha256_hex(data):
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
    check_repository(root)
    manifest = load_manifest(root, pinned)
    receipt_bytes = Path(receipt_file).read_bytes()
    verify_receipt(receipt_bytes, manifest)
    holdout_bytes = Path(holdout_file).read_bytes()
    cases = verify_holdout(holdout_bytes, manifest, root)
    write_exact(root, [(HOLDOUT_DESTINATION, holdout_bytes), (RECEIPT_DESTINATION, receipt_bytes)])
    return {"cases": len(cases), "destinations": [HOLDOUT_DESTINATION, RECEIPT_DESTINATION]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the sealed V2 holdout exactly once.")
    parser.add_argument("sealed_holdout_file", type=Path)
    parser.add_argument("seal_receipt_file", type=Path)
    args = parser.parse_args(argv)
    try:
        result = unseal(args.sealed_holdout_file, args.seal_receipt_file)
    except (UnsealRefused, OSError) as exc:
        # Never echo the supplied paths.
        print("refused: " + (str(exc) if isinstance(exc, UnsealRefused) else type(exc).__name__),
              file=sys.stderr)
        return 1
    print("holdout hash verified")
    print("receipt hash verified")
    print(str(result["cases"]) + " cases validated")
    for relative in result["destinations"]:
        print("wrote " + relative)
    print("Commit the opened holdout immediately and record that commit SHA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
