"""Measure how much the blind holdout overlaps the sets it must be independent of.

A holdout that merely paraphrases the development set does not measure
generalization; it measures memorization of a template. This script compares
every blind-holdout question against every question in the Dev and Validation V1
sets and refuses to certify the holdout if they are too close.

Three views, because each misses something the others catch:

- **SequenceMatcher** on the normalized character stream - overall edit
  similarity, sensitive to word order.
- **Character-bigram Jaccard** - bag-of-bigrams overlap, which still fires when
  the clauses of a sentence have been reordered.
- **Entity-masked signature** - SKUs, numbers, and corpus entity names replaced
  by placeholders. Two questions that differ only in *which* SKU or *which*
  policy they name collapse onto the same signature. This is the check that
  catches template farming, and neither similarity score reliably does: swapping
  a long policy name can drag raw similarity below any threshold while leaving
  the sentence structurally identical.

Read-only. Touches no model, no network, and no production module.

Usage:
    python check_evaluation_overlap.py [--report <json>]
    python check_evaluation_overlap.py --m3-dataset <json> [--report <json>]

M3 compares each customer turn only with its referenced complete gold sections.
It does not open other question sets. Scores >= 0.85 block freezing; scores
>= 0.80 are reported, with no elevated-fraction gate. M3 console output contains
aggregate counts only; --report explicitly opts into writing pair-level text.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):  # pragma: no cover
        pass

ROOT = Path(__file__).resolve().parent

# (holdout, [sets it must stay independent of])
COMPARISONS = (
    (
        "route_holdout",
        ROOT / "eval_orchestrated_routes_holdout.json",
        (
            ("route_dev", ROOT / "eval_orchestrated_routes_dev.json"),
            ("route_validation_v1", ROOT / "eval_orchestrated_routes_validation_v1.json"),
        ),
    ),
    (
        "answerability_holdout",
        ROOT / "eval_answerability_holdout.json",
        (
            ("answerability_dev", ROOT / "eval_answerability_dev.json"),
            (
                "answerability_validation_v1",
                ROOT / "eval_answerability_validation_v1.json",
            ),
        ),
    ),
)

CORPUS_PATH = ROOT / "sample_company_rules.md"
WIKI_PATH = ROOT / "wiki_pages" / "sample_company_wiki.json"

# --- Blind Holdout V2 -----------------------------------------------------
V2_ROUTE_PATH = ROOT / "eval_orchestrated_routes_blind_v2.json"
V2_ANSWER_PATH = ROOT / "eval_answerability_blind_v2.json"

# Everything V2 must stay independent of: every previously scored dataset, plus
# the M8A regression tables, which were written from the V1 audit's findings.
V2_REFERENCE_DATASETS = (
    ("route_dev", ROOT / "eval_orchestrated_routes_dev.json"),
    ("route_validation_v1", ROOT / "eval_orchestrated_routes_validation_v1.json"),
    ("route_audit_v1", ROOT / "eval_orchestrated_routes_holdout.json"),
    ("answerability_dev", ROOT / "eval_answerability_dev.json"),
    ("answerability_validation_v1", ROOT / "eval_answerability_validation_v1.json"),
    ("answerability_audit_v1", ROOT / "eval_answerability_holdout.json"),
)
V2_REFERENCE_TESTS = (
    ("m8a_regression_tests", ROOT / "tests" / "test_router_generalization.py"),
)

# Per-holdout allowance for the elevated band, as an absolute count.
V2_ELEVATED_ALLOWANCE = {"route_v2": 4, "answerability_v2": 2}
# The two V2 files may share phrasing with each other only within this fraction.
V2_CROSS_ELEVATED_FRACTION = 0.05

_CJK = re.compile(r"[一-鿿]")

# Freeze gates.
THRESHOLD_BLOCKING = 0.85          # no pair may reach this
THRESHOLD_ELEVATED = 0.80          # at most this fraction of a holdout may reach it
MAX_ELEVATED_FRACTION = 0.05
# Masked similarity at or above this, on a pair that is not already flagged by
# raw similarity, is treated as an entity swap.
THRESHOLD_MASKED_TEMPLATE = 0.90

TOP_PAIRS_REPORTED = 20

_PUNCTUATION = re.compile(
    r"[\s，。！？；：、,.!?;:~～…\-—\"'“”‘’()（）【】《》\[\]{}<>/\\|@#$%^&*+=_`]"
)
_SKU = re.compile(r"sku[-_ ]?[a-z]\d{3}", re.IGNORECASE)
_NUMBER = re.compile(r"[0-9]+|[〇零一二三四五六七八九十百千万两]+")


def normalize(text: str) -> str:
    """Lowercase, drop punctuation and whitespace. Matching only."""
    return _PUNCTUATION.sub("", text).lower()


def _corpus_entities() -> list[str]:
    """Entity names to mask, taken from the corpus rather than from the planner.

    Deriving them from the data keeps this checker independent of routing
    internals - it must judge the *questions*, not the implementation.
    """
    entities: set[str] = set()
    if CORPUS_PATH.exists():
        for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                entities.add(line[3:].strip())
    if WIKI_PATH.exists():
        try:
            wiki = json.loads(WIKI_PATH.read_text(encoding="utf-8"))
        except ValueError:
            wiki = {}
        for page in wiki.get("pages", []):
            if isinstance(page, dict):
                if isinstance(page.get("title"), str):
                    entities.add(page["title"].strip())
                for alias in page.get("aliases", []) or []:
                    if isinstance(alias, str):
                        entities.add(alias.strip())
    # Business objects the System channel deals in. Written out because they are
    # the nouns most likely to be swapped to fake a "new" question.
    entities.update(
        {
            "订单", "库存", "余额", "物流", "审批", "工单", "账户", "积分",
            "额度", "排班", "考勤", "申请记录",
        }
    )
    # Longest first so "账号与权限" masks before "账号".
    return sorted((e for e in entities if e), key=len, reverse=True)


ENTITIES: list[str] | None = None


def mask(text: str) -> str:
    """Replace SKUs, entity names, and numbers with placeholders."""
    global ENTITIES
    if ENTITIES is None:
        # Legacy masking is not used by M3. Defer its source reads so invoking
        # M3 does not read the legacy corpus at module import time.
        ENTITIES = _corpus_entities()
    masked = _SKU.sub("§S", text)
    for entity in ENTITIES:
        masked = masked.replace(entity, "§E")
    masked = _NUMBER.sub("§N", masked)
    return normalize(masked)


def bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)} or {text}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def load_questions(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing")
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError(f"{path}: expected a JSON array")
    records = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or "question" not in case:
            raise ValueError(f"{path}: cases[{index}] has no question")
        text = case["question"]
        norm = normalize(text)
        records.append(
            {
                "id": case.get("id", f"cases[{index}]"),
                "question": text,
                "normalized": norm,
                "bigrams": bigrams(norm),
                "masked": mask(text),
            }
        )
    return records


def compare(holdout: list[dict], references: list[tuple[str, list[dict]]]) -> list[dict]:
    """For each holdout question, its single closest question anywhere else."""
    results = []
    for item in holdout:
        best = None
        for ref_name, ref_items in references:
            for other in ref_items:
                seq = ratio(item["normalized"], other["normalized"])
                jac = jaccard(item["bigrams"], other["bigrams"])
                masked = ratio(item["masked"], other["masked"])
                score = max(seq, jac)
                if best is None or score > best["similarity"]:
                    best = {
                        "id": item["id"],
                        "question": item["question"],
                        "match_dataset": ref_name,
                        "match_id": other["id"],
                        "match_question": other["question"],
                        "sequence_ratio": seq,
                        "bigram_jaccard": jac,
                        "masked_ratio": masked,
                        "similarity": score,
                    }
        # A pair can be structurally identical while reading as different text,
        # which is exactly what swapping an entity produces.
        best["entity_swap_suspect"] = (
            best["masked_ratio"] >= THRESHOLD_MASKED_TEMPLATE
            and best["similarity"] < THRESHOLD_BLOCKING
        )
        results.append(best)
    return results


def internal_templates(holdout: list[dict]) -> list[dict]:
    """Masked signatures used more than twice inside one holdout."""
    buckets: dict[str, list[str]] = {}
    for item in holdout:
        buckets.setdefault(item["masked"], []).append(item["id"])
    return [
        {"signature": signature, "ids": ids, "count": len(ids)}
        for signature, ids in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        if len(ids) > 2
    ]


def evaluate(name: str, holdout_path: Path, refs: tuple) -> dict:
    holdout = load_questions(holdout_path)
    references = [(ref_name, load_questions(path)) for ref_name, path in refs]
    results = compare(holdout, references)
    results.sort(key=lambda r: -r["similarity"])

    blocking = [r for r in results if r["similarity"] >= THRESHOLD_BLOCKING]
    elevated = [r for r in results if r["similarity"] >= THRESHOLD_ELEVATED]
    suspects = [r for r in results if r["entity_swap_suspect"]]
    templates = internal_templates(holdout)
    allowed_elevated = int(len(holdout) * MAX_ELEVATED_FRACTION)

    gates = [
        {
            "name": f"{name}: pairs >= {THRESHOLD_BLOCKING:.2f}",
            "value": len(blocking),
            "limit": 0,
            "passed": len(blocking) == 0,
        },
        {
            "name": f"{name}: pairs >= {THRESHOLD_ELEVATED:.2f}",
            "value": len(elevated),
            "limit": allowed_elevated,
            "passed": len(elevated) <= allowed_elevated,
        },
        {
            "name": f"{name}: entity-swap suspects",
            "value": len(suspects),
            "limit": 0,
            "passed": len(suspects) == 0,
        },
        {
            "name": f"{name}: internal templates used > 2x",
            "value": len(templates),
            "limit": 0,
            "passed": len(templates) == 0,
        },
    ]

    print()
    print("=" * 78)
    print(f"{name}  ({len(holdout)} questions vs "
          f"{sum(len(items) for _, items in references)} reference questions)")
    print("=" * 78)
    print(f"Top {min(TOP_PAIRS_REPORTED, len(results))} most similar pairs:")
    for entry in results[:TOP_PAIRS_REPORTED]:
        print(
            f"  {entry['similarity']:.3f}  seq={entry['sequence_ratio']:.3f} "
            f"jac={entry['bigram_jaccard']:.3f} masked={entry['masked_ratio']:.3f}"
            f"  {entry['id']}  vs  {entry['match_dataset']}/{entry['match_id']}"
        )
        print(f"          H: {entry['question']}")
        print(f"          R: {entry['match_question']}")

    print()
    print(f"  pairs >= {THRESHOLD_BLOCKING:.2f} : {len(blocking)} (limit 0)")
    print(
        f"  pairs >= {THRESHOLD_ELEVATED:.2f} : {len(elevated)} "
        f"(limit {allowed_elevated} = {MAX_ELEVATED_FRACTION:.0%} of {len(holdout)})"
    )
    print(f"  entity-swap suspects : {len(suspects)} (limit 0)")
    print(f"  internal templates >2x: {len(templates)} (limit 0)")
    if suspects:
        print("  suspected entity swaps:")
        for entry in suspects:
            print(
                f"    {entry['id']} masked={entry['masked_ratio']:.3f} vs "
                f"{entry['match_dataset']}/{entry['match_id']}"
            )
            print(f"      H: {entry['question']}")
            print(f"      R: {entry['match_question']}")
    if templates:
        print("  internal template reuse:")
        for entry in templates:
            print(f"    x{entry['count']}: {', '.join(entry['ids'])}")

    return {
        "name": name,
        "holdout": str(holdout_path),
        "holdout_size": len(holdout),
        "reference_sets": [ref_name for ref_name, _ in refs],
        "max_similarity": results[0]["similarity"] if results else 0.0,
        "count_blocking": len(blocking),
        "count_elevated": len(elevated),
        "allowed_elevated": allowed_elevated,
        "entity_swap_suspects": suspects,
        "internal_templates": templates,
        "top_pairs": results[:TOP_PAIRS_REPORTED],
        "all_pairs": results,
        "gates": gates,
    }


def questions_from_test_module(path: Path) -> list[dict]:
    """Pull table-driven questions out of a test file without importing it.

    Parsed with `ast`, never executed: importing a test module would run its
    module-level code, and this check must stay inert.
    """
    if not path.exists():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def collect(node: ast.AST) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                text = child.value.strip()
                # Table rows are Chinese questions; ids, filenames, and prose
                # docstrings are not what this comparison is about.
                if text and _CJK.search(text) and "\n" not in text:
                    found.append(text)

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            collect(node)

    ordered: list[dict] = []
    seen: set[str] = set()
    for index, text in enumerate(found):
        if text in seen:
            continue
        seen.add(text)
        norm = normalize(text)
        ordered.append(
            {
                "id": f"{path.stem}[{index}]",
                "question": text,
                "normalized": norm,
                "bigrams": bigrams(norm),
                "masked": mask(text),
            }
        )
    return ordered


def evaluate_v2() -> dict:
    """Compare both Blind Holdout V2 files against everything already seen."""
    references: list[tuple[str, list[dict]]] = []
    for name, path in V2_REFERENCE_DATASETS:
        if path.exists():
            references.append((name, load_questions(path)))
    for name, path in V2_REFERENCE_TESTS:
        extracted = questions_from_test_module(path)
        if extracted:
            references.append((name, extracted))

    reference_total = sum(len(items) for _, items in references)
    reports = []
    gates = []

    holdouts = (
        ("route_v2", V2_ROUTE_PATH),
        ("answerability_v2", V2_ANSWER_PATH),
    )
    loaded = {name: load_questions(path) for name, path in holdouts}

    for name, path in holdouts:
        items = loaded[name]
        results = compare(items, references)
        results.sort(key=lambda r: -r["similarity"])
        blocking = [r for r in results if r["similarity"] >= THRESHOLD_BLOCKING]
        elevated = [r for r in results if r["similarity"] >= THRESHOLD_ELEVATED]
        suspects = [r for r in results if r["entity_swap_suspect"]]
        templates = internal_templates(items)
        allowance = V2_ELEVATED_ALLOWANCE[name]

        gates.extend(
            [
                {
                    "name": f"{name}: pairs >= {THRESHOLD_BLOCKING:.2f}",
                    "value": len(blocking), "limit": 0,
                    "passed": len(blocking) == 0,
                },
                {
                    "name": f"{name}: pairs >= {THRESHOLD_ELEVATED:.2f}",
                    "value": len(elevated), "limit": allowance,
                    "passed": len(elevated) <= allowance,
                },
                {
                    "name": f"{name}: entity-swap suspects",
                    "value": len(suspects), "limit": 0,
                    "passed": len(suspects) == 0,
                },
                {
                    "name": f"{name}: internal templates used > 2x",
                    "value": len(templates), "limit": 0,
                    "passed": len(templates) == 0,
                },
            ]
        )

        print()
        print("=" * 78)
        print(f"{name}  ({len(items)} questions vs {reference_total} reference questions)")
        print("=" * 78)
        for entry in results[:TOP_PAIRS_REPORTED]:
            print(
                f"  {entry['similarity']:.3f}  seq={entry['sequence_ratio']:.3f} "
                f"jac={entry['bigram_jaccard']:.3f} masked={entry['masked_ratio']:.3f}"
                f"  {entry['id']}  vs  {entry['match_dataset']}/{entry['match_id']}"
            )
            print(f"          V2: {entry['question']}")
            print(f"          RF: {entry['match_question']}")
        print()
        print(f"  max similarity        : {results[0]['similarity']:.3f}")
        print(f"  pairs >= {THRESHOLD_BLOCKING:.2f}       : {len(blocking)} (limit 0)")
        print(f"  pairs >= {THRESHOLD_ELEVATED:.2f}       : {len(elevated)} (limit {allowance})")
        print(f"  entity-swap suspects  : {len(suspects)} (limit 0)")
        print(f"  internal templates >2x: {len(templates)} (limit 0)")

        reports.append(
            {
                "name": name,
                "holdout": str(path),
                "holdout_size": len(items),
                "reference_sets": [n for n, _ in references],
                "reference_questions": reference_total,
                "max_similarity": results[0]["similarity"],
                "count_blocking": len(blocking),
                "count_elevated": len(elevated),
                "allowed_elevated": allowance,
                "entity_swap_suspects": suspects,
                "internal_templates": templates,
                "top_pairs": results[:TOP_PAIRS_REPORTED],
                "all_pairs": results,
            }
        )

    # The two V2 files must also not be paraphrases of each other.
    cross = compare(loaded["route_v2"], [("answerability_v2", loaded["answerability_v2"])])
    cross.sort(key=lambda r: -r["similarity"])
    cross_block = [r for r in cross if r["similarity"] >= THRESHOLD_BLOCKING]
    cross_elev = [r for r in cross if r["similarity"] >= THRESHOLD_ELEVATED]
    cross_suspect = [r for r in cross if r["entity_swap_suspect"]]
    cross_allowance = int(len(loaded["route_v2"]) * V2_CROSS_ELEVATED_FRACTION)

    print()
    print("=" * 78)
    print("route_v2 vs answerability_v2 (internal cross-check)")
    print("=" * 78)
    for entry in cross[:TOP_PAIRS_REPORTED]:
        print(
            f"  {entry['similarity']:.3f}  masked={entry['masked_ratio']:.3f}  "
            f"{entry['id']}  vs  {entry['match_id']}"
        )
        print(f"          R: {entry['question']}")
        print(f"          A: {entry['match_question']}")
    print()
    print(f"  max similarity        : {cross[0]['similarity']:.3f}")
    print(f"  pairs >= {THRESHOLD_BLOCKING:.2f}       : {len(cross_block)} (limit 0)")
    print(
        f"  pairs >= {THRESHOLD_ELEVATED:.2f}       : {len(cross_elev)} "
        f"(limit {cross_allowance} = {V2_CROSS_ELEVATED_FRACTION:.0%})"
    )
    print(f"  entity-swap suspects  : {len(cross_suspect)} (limit 0)")

    gates.extend(
        [
            {
                "name": f"route_v2 x answerability_v2: pairs >= {THRESHOLD_BLOCKING:.2f}",
                "value": len(cross_block), "limit": 0, "passed": len(cross_block) == 0,
            },
            {
                "name": f"route_v2 x answerability_v2: pairs >= {THRESHOLD_ELEVATED:.2f}",
                "value": len(cross_elev), "limit": cross_allowance,
                "passed": len(cross_elev) <= cross_allowance,
            },
            {
                "name": "route_v2 x answerability_v2: entity-swap suspects",
                "value": len(cross_suspect), "limit": 0,
                "passed": len(cross_suspect) == 0,
            },
        ]
    )

    reports.append(
        {
            "name": "route_v2_x_answerability_v2",
            "holdout_size": len(loaded["route_v2"]),
            "reference_sets": ["answerability_v2"],
            "max_similarity": cross[0]["similarity"],
            "count_blocking": len(cross_block),
            "count_elevated": len(cross_elev),
            "allowed_elevated": cross_allowance,
            "entity_swap_suspects": cross_suspect,
            "internal_templates": [],
            "top_pairs": cross[:TOP_PAIRS_REPORTED],
            "all_pairs": cross,
        }
    )

    return {"comparisons": reports, "gates": gates}


_M3_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_M3_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _m3_document(path: Path, source: str, doc_id: str, version: str) -> dict[str, str]:
    """Read one explicitly referenced source, without importing product code."""
    if not path.is_file():
        raise ValueError(f"{source}/{doc_id}: source document is missing ({path})")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{path}: JSON front matter must start with ---")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError(f"{path}: front matter closing --- is missing") from None
    try:
        metadata = json.loads("\n".join(lines[1:end]))
    except ValueError as error:
        raise ValueError(f"{path}: invalid JSON front matter: {error}") from None
    id_key = "doc_id" if source == "knowledge_base" else "policy_id"
    if not isinstance(metadata, dict) or metadata.get(id_key) != doc_id:
        raise ValueError(f"{path}: {id_key} does not match gold doc_id {doc_id!r}")
    if metadata.get("version") != version:
        raise ValueError(f"{path}: gold version {version!r} does not match source version "
                         f"{metadata.get('version')!r}")

    sections: dict[str, str] = {}
    heading: str | None = None
    body: list[str] = []
    fenced = False

    def finish_section() -> None:
        if heading is None:
            return
        text = " ".join(body).strip()
        if not normalize(text):
            raise ValueError(f"{path}: section {heading!r} has no body text")
        sections[heading] = text

    for line in lines[end + 1:]:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        match = None if fenced else _M3_HEADING.fullmatch(line)
        if match:
            finish_section()
            heading = match.group(2).strip()
            if heading in sections:
                raise ValueError(f"{path}: duplicate section heading {heading!r}")
            body = []
        elif stripped:
            if heading is None:
                raise ValueError(f"{path}: body text must follow a Markdown heading")
            body.append(stripped)
    finish_section()
    if not sections:
        raise ValueError(f"{path}: no Markdown sections found")
    return sections


def evaluate_m3(dataset_path: Path, corpus_dir: Path = ROOT / "knowledge_base",
                policy_dir: Path = ROOT / "policy_sources") -> dict:
    """Compare every turn with each complete section explicitly in its gold.

    Empty gold is valid for a turn requiring no policy answer. No dataset other
    than dataset_path is discovered or opened, including any holdout file.
    """
    dataset_path, corpus_dir, policy_dir = map(Path, (dataset_path, corpus_dir, policy_dir))
    cases = json.loads(dataset_path.read_text(encoding="utf-8-sig"))
    if not isinstance(cases, list):
        raise ValueError(f"{dataset_path}: expected a JSON array of cases")
    sources = {"knowledge_base": corpus_dir, "policy_sources": policy_dir}
    documents: dict[tuple[str, str, str], dict[str, str]] = {}
    case_ids: set[str] = set()
    pairs: list[dict] = []
    turn_count = empty_gold_turn_count = empty_gold_case_count = 0

    for case_index, case in enumerate(cases):
        location = f"{dataset_path}: cases[{case_index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{location}: expected an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"{location}: id must be a non-empty string")
        if case_id in case_ids:
            raise ValueError(f"{location}: duplicate case id {case_id!r}")
        case_ids.add(case_id)
        turns = case.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{location}: turns must be a non-empty array")
        case_has_gold = False
        for turn_index, turn in enumerate(turns, start=1):
            turn_location = f"{location} ({case_id}), turn {turn_index}"
            if not isinstance(turn, dict):
                raise ValueError(f"{turn_location}: expected an object")
            question = turn.get("question")
            if not isinstance(question, str) or not normalize(question):
                raise ValueError(f"{turn_location}: question must contain non-empty text")
            gold = turn.get("gold")
            if not isinstance(gold, list):
                raise ValueError(f"{turn_location}: gold must be an array (empty is allowed)")
            turn_count += 1
            if not gold:
                empty_gold_turn_count += 1
            else:
                case_has_gold = True
            question_normalized = normalize(question)
            question_bigrams = bigrams(question_normalized)
            for gold_index, reference in enumerate(gold, start=1):
                gold_location = f"{turn_location}, gold {gold_index}"
                if not isinstance(reference, dict):
                    raise ValueError(f"{gold_location}: expected an object")
                source, doc_id, version = (reference.get(key) for key in ("source", "doc_id", "version"))
                if not isinstance(source, str) or source not in sources:
                    raise ValueError(f"{gold_location}: source must be knowledge_base or policy_sources")
                for key, value in (("doc_id", doc_id), ("version", version)):
                    if not isinstance(value, str) or not _M3_SAFE_ID.fullmatch(value):
                        raise ValueError(f"{gold_location}: {key} must be a safe non-empty string")
                headings = reference.get("sections")
                if (not isinstance(headings, list) or not headings
                        or any(not isinstance(item, str) or not item.strip() for item in headings)
                        or len(set(headings)) != len(headings)):
                    raise ValueError(f"{gold_location}: sections must be a non-empty array of unique exact headings")
                key = source, doc_id, version
                if key not in documents:
                    documents[key] = _m3_document(sources[source] / (doc_id + ".md"), source, doc_id, version)
                sections = documents[key]
                for heading in headings:
                    if heading not in sections:
                        raise ValueError(f"{gold_location}: section {heading!r} is missing from {source}/{doc_id}@{version}")
                    gold_text = sections[heading]
                    gold_normalized = normalize(gold_text)
                    sequence_ratio = ratio(question_normalized, gold_normalized)
                    bigram_jaccard = jaccard(question_bigrams, bigrams(gold_normalized))
                    similarity = max(sequence_ratio, bigram_jaccard)
                    pairs.append({
                        "case_id": case_id, "turn_index": turn_index, "gold_index": gold_index,
                        "source": source, "doc_id": doc_id, "version": version, "section": heading,
                        "question": question, "gold_text": gold_text,
                        "sequence_ratio": sequence_ratio, "bigram_jaccard": bigram_jaccard,
                        "similarity": similarity,
                        "band": ("blocking" if similarity >= THRESHOLD_BLOCKING else
                                 "elevated" if similarity >= THRESHOLD_ELEVATED else "below_elevated"),
                    })
        if not case_has_gold:
            empty_gold_case_count += 1

    pairs.sort(key=lambda pair: -pair["similarity"])
    blocking = [pair for pair in pairs if pair["similarity"] >= THRESHOLD_BLOCKING]
    elevated = [pair for pair in pairs if pair["similarity"] >= THRESHOLD_ELEVATED]
    blocking_turns = {(pair["case_id"], pair["turn_index"]) for pair in blocking}
    elevated_turns = {(pair["case_id"], pair["turn_index"]) for pair in elevated}
    summary = {
        "case_count": len(cases), "turn_count": turn_count, "pair_count": len(pairs),
        "empty_gold_case_count": empty_gold_case_count,
        "empty_gold_turn_count": empty_gold_turn_count,
        "max_similarity": pairs[0]["similarity"] if pairs else 0.0,
        "blocking_pair_count": len(blocking), "blocking_turn_count": len(blocking_turns),
        # Counts >= 0.80 include blocking pairs, matching the legacy band convention.
        "elevated_pair_count": len(elevated), "elevated_turn_count": len(elevated_turns),
        "elevated_only_pair_count": len(elevated) - len(blocking),
        "passed": not blocking,
    }
    return {
        "mode": "m3_question_gold_sections", "dataset": str(dataset_path),
        "corpus_dir": str(corpus_dir), "policy_dir": str(policy_dir),
        "thresholds": {"blocking": THRESHOLD_BLOCKING, "elevated": THRESHOLD_ELEVATED,
                       "elevated_is_failure": False},
        "algorithm": "max(SequenceMatcher(normalize(question), normalize(section)), character-bigram Jaccard)",
        "summary": summary, "all_pairs": pairs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        default=None,
        help="Where to write the full pair-level report",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--v2",
        action="store_true",
        help="Check Blind Holdout V2 against every previously seen question set",
    )
    modes.add_argument("--m3-dataset", type=Path,
                       help="Compare only this M3 dataset with its explicitly referenced gold sections")
    parser.add_argument("--corpus-dir", type=Path, default=ROOT / "knowledge_base",
                        help="M3 knowledge_base source directory")
    parser.add_argument("--policy-dir", type=Path, default=ROOT / "policy_sources",
                        help="M3 policy_sources source directory")
    args = parser.parse_args(argv)

    if args.m3_dataset is not None:
        try:
            report = evaluate_m3(args.m3_dataset, args.corpus_dir, args.policy_dir)
            if args.report:
                report_path = Path(args.report)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as error:
            print(f"M3 overlap error: {error}", file=sys.stderr)
            return 2
        print("M3 question / complete gold-section overlap")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        if args.report:
            print(f"Wrote {report_path}")
        return 0 if report["summary"]["passed"] else 1

    if args.v2:
        outcome = evaluate_v2()
        reports, gates = outcome["comparisons"], outcome["gates"]
        default_report = "evaluation_runs/blind-v2-overlap-report.json"
    else:
        reports = []
        gates = []
        for name, holdout_path, refs in COMPARISONS:
            report = evaluate(name, holdout_path, refs)
            reports.append(report)
            gates.extend(report["gates"])
        default_report = "evaluation_runs/overlap-report.json"

    print()
    print("=" * 78)
    print("Gates")
    print("=" * 78)
    for gate in gates:
        print(
            f"  [{'PASS' if gate['passed'] else 'FAIL'}] {gate['name']:<46}"
            f"{gate['value']} (limit {gate['limit']})"
        )

    report_path = Path(args.report or default_report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "thresholds": {
                    "blocking": THRESHOLD_BLOCKING,
                    "elevated": THRESHOLD_ELEVATED,
                    "max_elevated_fraction": MAX_ELEVATED_FRACTION,
                    "masked_template": THRESHOLD_MASKED_TEMPLATE,
                },
                "comparisons": reports,
                "gates": gates,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"Wrote {report_path}")

    return 0 if all(gate["passed"] for gate in gates) else 1


if __name__ == "__main__":
    sys.exit(main())
