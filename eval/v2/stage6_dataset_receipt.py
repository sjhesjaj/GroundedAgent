"""Build the author receipt of one Stage 6 dataset (docs/v2/stage6-author-brief.md §6).

    python eval/v2/stage6_dataset_receipt.py --split <holdout|dev|validation> \\
        --freeze-commit <40-hex> --attest-isolated --out <receipt.json> <dataset.json>

Stdlib only, like the case contract beside it, so it runs unchanged from the
exported Stage 6 author bundle. Fail-closed, in this order, nothing written
until everything passes:

1. The bundle: every file listed in the bundle's bundle-manifest.json is
   re-hashed (LF-normalized) and the content digest recomputed, so a receipt
   names exactly the frozen inputs the author used.
2. The dataset: a JSON array of v2-stage6-case/1 cases; every case passes
   eval/v2/stage6_case_contract.py, case ids are unique, and the split meets
   eval/v2/spec/stage6-holdout-plan.json.
3. The receipt (v2-stage6-dataset-receipt/1): the dataset's raw-byte sha256,
   its counts and distributions, the validation results and the author's
   isolation attestation - never a path, never case content.

Nothing here reads the system clock, the network, or anything outside the bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

BUNDLE_ROOT = Path(__file__).resolve().parent.parent.parent
CONTRACT_PATH = Path(__file__).resolve().parent / "stage6_case_contract.py"
BUNDLE_MANIFEST_NAME = "bundle-manifest.json"
BUNDLE_SCHEMA = "v2-stage6-author-bundle/1"
INPUT_MANIFEST_SCHEMA = "v2-stage6-holdout-input-manifest/1"
RECEIPT_SCHEMA = "v2-stage6-dataset-receipt/1"
SPLITS = ("holdout", "dev", "validation")
COMMIT = re.compile(r"^[0-9a-f]{40}$")

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


def load_contract():
    spec = importlib.util.spec_from_file_location("v2_stage6_case_contract_receipt", CONTRACT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def normalized_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def content_digest(schema: str, files: list) -> str:
    """The export tool's digest: the input manifest schema and (path, sha256) pairs only."""
    payload = {"schema": schema, "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def verify_bundle(root: Path = BUNDLE_ROOT) -> tuple[str, int]:
    """(content digest, file count) of an untouched exported bundle, or ReceiptRefused."""
    path = root / BUNDLE_MANIFEST_NAME
    if not path.is_file():
        raise ReceiptRefused("run from the root of an exported Stage 6 author bundle")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != BUNDLE_SCHEMA or manifest.get("input_manifest_schema") != INPUT_MANIFEST_SCHEMA:
        raise ReceiptRefused("not a Stage 6 author bundle manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ReceiptRefused("the bundle manifest lists no files")
    for entry in files:
        relative = entry.get("path")
        source = (root / relative).resolve()
        if not isinstance(relative, str) or not source.is_relative_to(root.resolve()) or not source.is_file():
            raise ReceiptRefused("a bundle file is missing")
        if normalized_sha256(source) != entry.get("sha256"):
            raise ReceiptRefused("a bundle file was changed: " + relative)
    digest = content_digest(INPUT_MANIFEST_SCHEMA, files)
    if digest != manifest.get("content_digest"):
        raise ReceiptRefused("the bundle content digest does not match")
    return digest, len(files)


def _counts(values) -> dict:
    out: dict = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return {key: out[key] for key in sorted(out)}


def build_receipt(data: bytes, *, split: str, freeze_merge_commit: str, input_bundle_digest: str,
                  input_file_count: int, contract=None) -> dict:
    """The receipt of one dataset's raw bytes. Refuses unless every check passes."""
    if split not in SPLITS:
        raise ReceiptRefused("split must be one of " + ", ".join(SPLITS))
    if not isinstance(freeze_merge_commit, str) or not COMMIT.match(freeze_merge_commit):
        raise ReceiptRefused("the freeze commit must be a full 40-character lowercase sha")
    contract = contract or load_contract()
    try:
        cases = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ReceiptRefused("the dataset is not UTF-8 JSON") from None
    if not isinstance(cases, list) or not cases or not all(isinstance(case, dict) for case in cases):
        raise ReceiptRefused("the dataset must be a non-empty JSON array of cases")
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--split", required=True)
    parser.add_argument("--freeze-commit", required=True)
    parser.add_argument("--attest-isolated", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.attest_isolated:
            raise ReceiptRefused("--attest-isolated is required (brief §1, §6)")
        if args.out is not None and args.out.exists():
            raise ReceiptRefused("the receipt file already exists")
        digest, count = verify_bundle()
        receipt = build_receipt(args.dataset.read_bytes(), split=args.split,
                                freeze_merge_commit=args.freeze_commit,
                                input_bundle_digest=digest, input_file_count=count)
    except (ReceiptRefused, OSError) as exc:
        print("refused: " + str(exc), file=sys.stderr)
        return 1
    data = receipt_bytes(receipt)
    if args.out is not None:
        args.out.write_bytes(data)
    else:
        sys.stdout.write(data.decode("utf-8"))
    print(json.dumps({"split": receipt["split"], "case_count": receipt["case_count"],
                      "dataset_sha256": receipt["dataset_sha256"],
                      "receipt_sha256": hashlib.sha256(data).hexdigest()}),
          file=sys.stderr if args.out is None else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
