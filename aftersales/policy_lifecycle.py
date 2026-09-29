"""Policy compilation and review through the existing Wiki repository.

The complete corpus is a replacement set, not an implicit incremental merge.
Headers and bodies share DocumentSnapshot versions and one published pointer.
All lifecycle timestamps remain the Wiki repository's audit wall clock.
"""
from __future__ import annotations

from pathlib import Path

from wiki_maintenance import prompts
from wiki_maintenance.compiler import (
    PagePlan, assemble_page_from_spans, compile_wiki_fast, new_page_id,
)
from wiki_maintenance.diff import diff_builds
from wiki_maintenance.models import WikiBuild
from wiki_maintenance.repository import WikiRepository
from wiki_maintenance.source_spans import (
    build_document_snapshot_from_text, compute_content_hash, compute_document_content_hash,
    compute_span_id, normalize_span_text,
)

from .policy_source import PARSER_VERSION, PolicySource, digest, parse_policy_source

CORPUS_PATH = Path(__file__).resolve().parent.parent / "policy_sources"
DOMAIN = "after_sales_policy"


def read_corpus(path: Path = CORPUS_PATH) -> tuple[PolicySource, ...]:
    return tuple(parse_policy_source(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.md")))


def compile_policy_draft(repository: WikiRepository, sources, *, model=None,
                         model_provenance: dict[str, str] | None = None) -> WikiBuild:
    """Compile a complete corpus; never publish implicitly.

    Without a model, use the existing verbatim compiler with one topic per
    source title. An injected WikiModel uses the existing fast topic planner;
    its provider/model/config descriptor is required for reproducible freezing.
    Neither path lets a model interpret front matter or choose business rules.
    """
    sources = tuple(parse_policy_source(s.render()) for s in sources)
    if not sources:
        raise ValueError("policy corpus must not be empty")
    for field in ("policy_id", "source_doc"):
        values = [s.header[field] for s in sources]
        if len(values) != len(set(values)):
            raise ValueError("duplicate " + field)
    provenance = {"domain": DOMAIN, "parser": PARSER_VERSION,
                  "compiler": "wiki_maintenance.assemble_page_from_spans/1"}
    root = Path(__file__).resolve().parent.parent
    provenance["implementation_digest"] = digest({name: (root / name).read_text(encoding="utf-8")
        for name in ("aftersales/policy_source.py", "aftersales/policy.py",
                     "aftersales/policy_lifecycle.py", "wiki_maintenance/compiler.py",
                     "wiki_maintenance/source_spans.py", "wiki_maintenance/prompts.py")})
    if model is not None:
        if not model_provenance or not {"provider", "model", "config"} <= set(model_provenance):
            raise ValueError("model_provenance requires provider, model, config")
        if not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                   for k, v in model_provenance.items()):
            raise ValueError("model_provenance must contain non-empty strings")
        provenance.update({"model_" + k: v for k, v in model_provenance.items()})
        provenance["compiler"] = "wiki_maintenance.compile_wiki_fast/1"
        provenance["prompt_digest"] = digest(prompts.TOPIC_PLAN_SYSTEM)
    base = repository.load_current_build()
    snapshots = []
    plans = []
    for source in sorted(sources, key=lambda s: s.header["source_doc"]):
        header = source.header
        snapshot = build_document_snapshot_from_text(
            document_id=header["source_doc"], filename=header["source_doc"], text=source.render())
        # Canonical header is exactly the first, unheaded span. All subsequent
        # spans are prose; those alone enter the Wiki compiler.
        body_spans = snapshot.spans[1:]
        if not body_spans:
            raise ValueError("policy source must contain prose")
        snapshots.append(snapshot)
        plans.append(PagePlan(topic=header["title"], source_span_ids=tuple(s.span_id for s in body_spans)))
    spans = tuple(s for snapshot in snapshots for s in snapshot.spans[1:])
    existing = base.pages if base else ()
    if model is None:
        page_index = {p.page_id: p for p in existing}
        span_index = {s.span_id: s for s in spans}
        pages = tuple(assemble_page_from_spans(
            plan, span_index=span_index, existing_page=page_index.get(new_page_id(plan.topic)))
            for plan in plans)
    else:
        pages = compile_wiki_fast(model, spans=spans, existing_pages=existing)
    for snapshot in snapshots:
        repository.save_document_snapshot(snapshot)
    return repository.create_build(
        pages, document_versions={s.document_id: s.version for s in snapshots},
        base_build_id=base.build_id if base else None, provenance=provenance)


def load_policy_sources(repository: WikiRepository, build: WikiBuild):
    """Read only the document versions pinned by this build, with integrity checks."""
    if build.provenance.get("domain") != DOMAIN or build.provenance.get("parser") != PARSER_VERSION:
        raise ValueError("build is not a supported after-sales policy build")
    sources = []
    for entry in build.document_versions:
        snapshot = repository.load_document_snapshot(entry.document_id, entry.version)
        if snapshot.document_id != entry.document_id or snapshot.version != entry.version:
            raise ValueError("snapshot identity mismatch")
        if not snapshot.spans or any(compute_content_hash(s.text) != s.content_hash for s in snapshot.spans):
            raise ValueError("snapshot span content mismatch")
        occurrences = {}
        for ordinal, span in enumerate(snapshot.spans, 1):
            key = (span.heading, normalize_span_text(span.text))
            occurrence = occurrences.get(key, 0)
            expected_id = compute_span_id(entry.document_id, span.heading, span.text, occurrence=occurrence)
            if span.span_id != expected_id or span.ordinal != ordinal or span.source != entry.document_id:
                raise ValueError("snapshot span identity/provenance mismatch")
            occurrences[key] = occurrence + 1
        if compute_document_content_hash([(s.span_id, s.content_hash) for s in snapshot.spans]) != snapshot.content_hash:
            raise ValueError("snapshot digest mismatch")
        body = "\n\n".join(("## " + s.heading + "\n" if s.heading else "") + s.text
                           for s in snapshot.spans[1:])
        source = parse_policy_source(snapshot.spans[0].text + "\n\n" + body)
        if source.header["source_doc"] != entry.document_id:
            raise ValueError("front matter source identity mismatch")
        sources.append((source, snapshot))
    if not sources or len({s.header["policy_id"] for s, _ in sources}) != len(sources):
        raise ValueError("policy build must have unique policy ids and non-empty sources")
    return tuple(sources)


def frozen_manifest(repository: WikiRepository, build_id: str | None = None) -> dict:
    """Freeze a published build. Hash content/provenance, excluding audit time.

    build_id is repository-local identity; content_digest compares independent
    rebuilds. No created_at, published_at, or base pointer enters that digest.
    """
    current = repository.get_current_build_id()
    if current is None or (build_id is not None and build_id != current):
        raise ValueError("freeze requires the current published build")
    build = repository.load_build(current)
    pairs = load_policy_sources(repository, build)
    documents = [{"document_id": s.document_id, "version": s.version, "content_hash": s.content_hash}
                 for _, s in pairs]
    content = {"schema": "policy-freeze/1", "documents": documents,
               "pages": [p.to_dict() for p in sorted(build.pages, key=lambda p: p.page_id)],
               "compiler_provenance": dict(build.provenance)}
    return {"build_id": build.build_id, "content_digest": digest(content),
            "policy_source_digest": digest(documents), **content}


def diff_policy_builds(repository: WikiRepository, old_id: str, new_id: str) -> dict:
    """Policy field values plus complete Wiki before/after text, deterministically."""
    old, new = repository.load_build(old_id), repository.load_build(new_id)
    indexes = [{s.header["policy_id"]: s.header for s, _ in load_policy_sources(repository, b)}
               for b in (old, new)]
    before, after = indexes
    changes = []
    for key in sorted(set(before) | set(after)):
        left, right = before.get(key), after.get(key)
        if left != right:
            changes.append({"policy_id": key, "before": left, "after": right})
    old_pages, new_pages = ({p.page_id: p.to_dict() for p in b.pages} for b in (old, new))
    page_changes = [{"page_id": k, "before": old_pages.get(k), "after": new_pages.get(k)}
                    for k in sorted(set(old_pages) | set(new_pages)) if old_pages.get(k) != new_pages.get(k)]
    return {"wiki": diff_builds(old, new).to_dict(), "policies": changes, "pages": page_changes}


def verify_frozen_manifest(repository: WikiRepository, expected: dict) -> None:
    """Verify the pinned publication and its content against a saved manifest."""
    actual = frozen_manifest(repository)
    if actual != expected:
        raise ValueError("published policy build does not match the frozen manifest")
