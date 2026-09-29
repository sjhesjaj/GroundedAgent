"""Build docs/v2/v1-test-inventory.json from a recorded run and the classification table.

Pure analysis tooling for Stage 4.0. Reads two JSON files, writes one; imports
no runtime module. Every recorded test id must resolve to exactly one category:

- files marked `per_test` must list every one of their test ids, and nothing else;
- `overrides` must name tests that exist;
- every other file resolves through its `default`.

Any gap or stray entry is an error, so the inventory cannot silently drift from
the suite it describes.

Usage (from the repository root):

    .\\.venv\\Scripts\\python.exe -X utf8 docs\\v2\\tools\\build_v1_test_inventory.py ^
        <results.json> docs\\v2\\tools\\v1_test_classification.json docs\\v2\\v1-test-inventory.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

CATEGORIES = ("infrastructure", "v1_business")

# The file-level estimate in docs/v2/stage4-design.md §1.3, restated so the
# inventory can report where the per-test classification disagrees with it.
DESIGN_GROUPS = {
    "clear_infrastructure": (
        "test_llm_provider", "test_llm_provider_live", "test_agent_trace",
        "test_eval_environment", "test_model_comparison", "test_wiki_compiler",
        "test_wiki_fast_compile", "test_wiki_lifecycle", "test_wiki_page_batching",
        "test_wiki_runtime", "test_wiki_adapter", "test_storage",
        "test_knowledge_consistency", "test_upload_upsert", "test_cross_document_supersede",
        "test_streaming", "test_evidence_adapter", "test_evidence_policy",
        "test_answer_validation", "test_quantity_exemptions", "test_premise_binding",
        "test_quantity_scope", "test_unconsumed_meaning", "test_predicate_scopes",
        "test_predicate_completeness", "test_assertion_boundaries", "test_delivery_forms",
    ),
    "mixed": (
        "test_diagnostic_eval", "test_executor", "test_orchestrated_chat",
        "test_retrieval_focus", "test_evidence_budget", "test_closeout_regressions",
    ),
    "clear_v1_business": (
        "test_planner", "test_system_provider", "test_final_rework", "test_route_variants",
        "test_router_generalization", "test_declined_channels", "test_boundary_messages",
        "test_full_budget_coverage", "test_routing", "test_label_revisions",
    ),
}
DESIGN_ESTIMATE = {
    "group_sizes": {"clear_infrastructure": 684, "mixed": 189, "clear_v1_business": 267},
    "infrastructure_range": [760, 810],
    "v1_business_range": [330, 380],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(file_rule: dict, short_id: str, module: str) -> tuple[dict, str]:
    if file_rule.get("per_test"):
        entry = file_rule["tests"].get(short_id)
        if entry is None:
            raise SystemExit(f"{module}: per-test file is missing an entry for {short_id}")
        return entry, "test"
    override = file_rule.get("overrides", {}).get(short_id)
    if override is not None:
        return override, "test_override"
    return {"category": file_rule["default"], "reason": file_rule["reason"]}, "file"


def build(results_path: Path, classification_path: Path, output_path: Path) -> dict:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    table = json.loads(classification_path.read_text(encoding="utf-8"))
    reasons = table["reasons"]
    files = table["files"]

    grouped: dict[str, list[str]] = defaultdict(list)
    for test_id in results["results"]:
        _package, module, short_id = test_id.split(".", 2)
        grouped[module].append(short_id)

    unknown_modules = sorted(set(grouped) - set(files))
    if unknown_modules:
        raise SystemExit(f"no classification rule for: {unknown_modules}")
    stale_modules = sorted(set(files) - set(grouped))
    if stale_modules:
        raise SystemExit(f"classification names modules with no tests: {stale_modules}")

    for module, rule in files.items():
        present = set(grouped[module])
        listed = set(rule.get("tests", {})) | set(rule.get("overrides", {}))
        stray = sorted(listed - present)
        if stray:
            raise SystemExit(f"{module}: entries for tests that do not exist: {stray}")
        if rule.get("per_test") and present - set(rule["tests"]):
            raise SystemExit(f"{module}: unclassified tests: {sorted(present - set(rule['tests']))}")

    records = []
    for test_id, outcome in results["results"].items():
        _package, module, short_id = test_id.split(".", 2)
        rule = files[module]
        entry, level = _resolve(rule, short_id, module)
        category = entry["category"]
        if category not in CATEGORIES:
            raise SystemExit(f"{test_id}: unknown category {category!r}")
        code = entry["reason"]
        if code not in reasons:
            raise SystemExit(f"{test_id}: unknown reason code {code!r}")
        prerequisite = entry.get("v2_prerequisite")
        if prerequisite is not None and category != "v1_business":
            raise SystemExit(f"{test_id}: a v2_prerequisite only applies to v1_business tests")
        if prerequisite == "port_before_delete" and entry.get("port_priority") not in ("must", "should"):
            raise SystemExit(f"{test_id}: port_before_delete needs port_priority must|should")

        record = {
            "test_id": test_id,
            "source_file": f"tests/{module}.py",
            "category": category,
            "classification_reason": reasons[code],
            "reason_code": code,
            "classification_level": level,
            "v1_result": outcome["outcome"],
        }
        for key in ("v2_prerequisite", "port_priority", "port_property", "v2_note"):
            if key in entry:
                record[key] = entry[key]
        if level == "file" and "file_note" in rule:
            record["file_note"] = rule["file_note"]
        records.append(record)

    records.sort(key=lambda item: item["test_id"])
    by_category = Counter(item["category"] for item in records)

    per_file = []
    for module in sorted(grouped):
        items = [r for r in records if r["source_file"] == f"tests/{module}.py"]
        counts = Counter(r["category"] for r in items)
        group = next((name for name, members in DESIGN_GROUPS.items() if module in members), None)
        per_file.append({
            "source_file": f"tests/{module}.py",
            "design_group": group,
            "tests": len(items),
            "infrastructure": counts["infrastructure"],
            "v1_business": counts["v1_business"],
        })

    group_actuals = {}
    for group, members in DESIGN_GROUPS.items():
        rows = [row for row in per_file if row["source_file"][len("tests/"):-3] in members]
        group_actuals[group] = {
            "tests": sum(row["tests"] for row in rows),
            "infrastructure": sum(row["infrastructure"] for row in rows),
            "v1_business": sum(row["v1_business"] for row in rows),
        }
    reclassified_files = [
        row for row in per_file
        if (row["design_group"] == "clear_infrastructure" and row["v1_business"])
        or (row["design_group"] == "clear_v1_business" and row["infrastructure"])
    ]

    port = [r for r in records if r.get("v2_prerequisite") == "port_before_delete"]
    superseded = [r for r in records if r.get("v2_prerequisite") == "superseded_by_v2_semantics"]

    inventory = {
        "schema_version": 1,
        "description": "Stage 4.0 V1 test inventory (docs/v2/stage4-design.md §1.3). Generated by docs/v2/tools/build_v1_test_inventory.py; do not edit by hand.",
        "v1_head": results["git_head"],
        "source_run": {
            "command": results["command"],
            "python": results["python"],
            "platform": results["platform"],
            "tests_run": results["tests_run"],
            "counts": results["counts"],
            "was_successful": results["was_successful"],
            "tracked_tree_status_at_run": results["git_status_short"] or "(clean)",
        },
        "inputs": {
            "classification_table": {
                "path": "docs/v2/tools/v1_test_classification.json",
                "sha256": _sha256(classification_path),
            },
        },
        "categories": table["categories"],
        "prerequisite_kinds": table["prerequisite_kinds"],
        "summary": {
            "total": len(records),
            "infrastructure": by_category["infrastructure"],
            "v1_business": by_category["v1_business"],
            "v1_results": dict(Counter(r["v1_result"] for r in records)),
            "port_before_delete": {
                "total": len(port),
                "must": sum(1 for r in port if r["port_priority"] == "must"),
                "should": sum(1 for r in port if r["port_priority"] == "should"),
            },
            "superseded_by_v2_semantics": len(superseded),
            "design_estimate": DESIGN_ESTIMATE,
            "design_groups_actual": group_actuals,
            "files_reclassified_against_design_group": reclassified_files,
        },
        "per_file": per_file,
        "tests": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return inventory


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: build_v1_test_inventory.py <results.json> <classification.json> <output.json>")
    result = build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    summary = result["summary"]
    print(json.dumps({key: summary[key] for key in (
        "total", "infrastructure", "v1_business", "v1_results",
        "port_before_delete", "superseded_by_v2_semantics", "design_groups_actual",
    )}, ensure_ascii=False, indent=2))
