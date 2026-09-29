"""Published policy selection, precedence, structured evidence and linkage.

Reads the published pointer once per operation; no draft fallback, wall clock,
or model interpretation. Selection and rendering are separate contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path

from orchestration.contracts import Evidence, SourceType, ToolResult, ToolStatus
from orchestration.wiki_adapter import tokenize
from wiki_maintenance.repository import CURRENT_FILENAME, WikiRepository

from .clock import require_aware
from .policy import (POLICY_TOOL_NAME, PolicyRecord, PolicyRuleType, is_policy_in_effect,
                     policy_applies_to_category, policy_ref)
from .policy_lifecycle import load_policy_sources
from .policy_source import canonical


DEFAULT_AFTERSALES_POLICY_ROOT = Path(__file__).resolve().parent.parent / "wiki_pages" / "aftersales_frozen"


class PolicyCatalogUnavailable(RuntimeError):
    """Missing, unreadable, or incompatible publication; never an empty match."""


class PolicyPrecedenceConflict(RuntimeError):
    """Top-priority applicable rules disagree. No arbitrary winner is usable."""


def select_policies(records, *, as_of: datetime, rule_type: PolicyRuleType,
                    category: str | None = None) -> tuple[PolicyRecord, ...]:
    """None category selects general rules only, never guesses an item category."""
    require_aware("as_of", as_of)
    if not isinstance(rule_type, PolicyRuleType):
        raise ValueError("rule_type must be PolicyRuleType")
    if category is not None and (not isinstance(category, str) or not category.strip()):
        raise ValueError("category must be non-empty text or None")
    applicable = [r for r in records if r.rule_type is rule_type and is_policy_in_effect(r, as_of)
                  and (not r.scope if category is None else policy_applies_to_category(r, category))]
    if not applicable:
        return ()
    priority = max(r.priority for r in applicable)
    top = tuple(sorted((r for r in applicable if r.priority == priority), key=policy_ref))
    if len({canonical(dict(r.params)) for r in top}) != 1:
        raise PolicyPrecedenceConflict("top-priority policy params disagree")
    return top


@dataclass(frozen=True)
class CatalogSnapshot:
    build_id: str
    records: tuple[PolicyRecord, ...]
    # Detached immutable source descriptors; not a second set of business params.
    source_versions: tuple[tuple[str, str, str], ...]
    bodies: tuple[tuple[str, str], ...]
    provenance: tuple[tuple[str, str], ...]

    def select(self, *, as_of, rule_type, category=None):
        return select_policies(self.records, as_of=as_of, rule_type=rule_type, category=category)

    def lookup(self, ref: str) -> PolicyRecord:
        for record in self.records:
            if policy_ref(record) == ref:
                return record
        raise ValueError("policy reference is not in this published snapshot")


class PublishedPolicyCatalog:
    def __init__(self, root: str | Path = DEFAULT_AFTERSALES_POLICY_ROOT):
        self.root = Path(root)

    def snapshot(self) -> CatalogSnapshot:
        if not (self.root / CURRENT_FILENAME).exists():
            raise PolicyCatalogUnavailable("no published policy build")
        try:
            repository = WikiRepository(self.root, create_directories=False)
            build = repository.load_current_build()
            if build is None:
                raise PolicyCatalogUnavailable("publication was retracted")
            pairs = load_policy_sources(repository, build)
            return CatalogSnapshot(
                build.build_id,
                tuple(s.record(build.build_id) for s, _ in pairs),
                tuple((s.document_id, s.version, s.content_hash) for _, s in pairs),
                tuple((s.header["policy_id"], s.body) for s, _ in pairs),
                tuple((s.header["policy_id"], canonical(s.header["provenance"])) for s, _ in pairs))
        except (OSError, ValueError, RuntimeError) as exc:
            raise PolicyCatalogUnavailable("published policy build cannot be read") from exc

    def select(self, *, as_of, rule_type, category=None):
        return self.snapshot().select(as_of=as_of, rule_type=rule_type, category=category)

    def search(self, query: str, *, as_of: datetime):
        """Lexical retrieval identifies topics; precedence sees ALL rules of each
        topic, not just lexical matches. A standard-rule phrase cannot suppress
        an active promotion. General consultations expose category alternatives
        with their scope, without claiming a category for an unknown order.
        """
        require_aware("as_of", as_of)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be non-empty text")
        snapshot = self.snapshot()
        terms = set(tokenize(query))
        bodies = dict(snapshot.bodies)
        types = {r.rule_type for r in snapshot.records
                 if terms & set(tokenize(r.title + " " + bodies[r.policy_id] + " " + r.rule_type.value))}
        categories = sorted({c for r in snapshot.records for c in r.scope})
        mentioned = [c for c in categories if c in query]
        targets = mentioned if mentioned else [None, *categories]
        selected = {}
        for rule_type in sorted(types, key=lambda t: t.value):
            for category in targets:
                for record in snapshot.select(as_of=as_of, rule_type=rule_type, category=category):
                    ref = policy_ref(record)
                    if ref not in selected:
                        selected[ref] = (record, [])
                    selected[ref][1].append(category)
        return snapshot, tuple(selected[k] for k in sorted(selected))


def policy_fields(record: PolicyRecord) -> dict:
    return {"title": record.title, "rule_type": record.rule_type.value, "scope": list(record.scope),
            "priority": record.priority, "effective_from": record.effective_from,
            "effective_to": record.effective_to, **dict(record.params)}


def policy_evidence(record: PolicyRecord, *, as_of: datetime, source_version: str,
                    source_digest: str, provenance: dict, selected_categories=()) -> tuple[Evidence, ...]:
    """Render authoritative front-matter fields; no downstream prose parsing."""
    require_aware("as_of", as_of)
    metadata = {"policy_id": record.policy_id, "version": record.version,
                "policy_ref": policy_ref(record), "build_id": record.build_id,
                "rule_type": record.rule_type.value, "source_doc": record.source_doc,
                "source_locator": record.locator, "source_version": source_version,
                "source_digest": source_digest, "provenance": provenance,
                "effective_from": record.effective_from, "effective_to": record.effective_to,
                "priority": record.priority, "scope": list(record.scope),
                "selected_categories": list(selected_categories), "authority_scope": "policy"}
    return tuple(Evidence(
        content=record.title + "：" + field + " = " + canonical(value),
        source_type=SourceType.DOCUMENT, source=record.source_doc,
        locator="policy:" + record.policy_id + "#" + field, version=record.version,
        observed_at=as_of.isoformat(), authority=90,
        metadata={**metadata, "field": field, "value": value})
        for field, value in sorted(policy_fields(record).items()))


class PublishedPolicyAdapter:
    def __init__(self, catalog: PublishedPolicyCatalog | None = None):
        self.catalog = PublishedPolicyCatalog() if catalog is None else catalog

    def search(self, query: str, *, as_of: datetime) -> ToolResult:
        snapshot, selected = self.catalog.search(query, as_of=as_of)
        versions = {doc: (version, sha) for doc, version, sha in snapshot.source_versions}
        provenance = dict(snapshot.provenance)
        evidence = tuple(e for record, categories in selected for e in policy_evidence(
            record, as_of=as_of, source_version=versions[record.source_doc][0],
            source_digest=versions[record.source_doc][1],
            provenance=json.loads(provenance[record.policy_id]), selected_categories=categories))
        return ToolResult(tool_name=POLICY_TOOL_NAME,
                          status=ToolStatus.OK if evidence else ToolStatus.EMPTY, evidence=evidence)


def validate_policy_refs(refs: tuple[str, ...], *, snapshot: CatalogSnapshot,
                         evidence: tuple[Evidence, ...]) -> None:
    """Fail closed unless every cited build/version has its structured fields.

    Pass DerivedEvidence.policy_refs and the snapshot/evidence from the same
    catalog read. This checks provenance correspondence, not eligibility.
    """
    versions = {doc: (version, digest) for doc, version, digest in snapshot.source_versions}
    provenance = dict(snapshot.provenance)
    for ref in refs:
        record = snapshot.lookup(ref)
        if (record.build_id != snapshot.build_id or record.source_doc not in versions
                or record.policy_id not in provenance):
            raise ValueError("derived policy reference lacks snapshot provenance")
        source_version, source_digest = versions[record.source_doc]
        policy_provenance = json.loads(provenance[record.policy_id])
        candidates = [e for e in evidence if e.metadata.get("policy_ref") == ref]
        for field, value in policy_fields(record).items():
            matches = [e for e in candidates if e.locator == "policy:" + record.policy_id + "#" + field]
            if not matches:
                raise ValueError("derived policy reference lacks structured evidence: " + field)
            for item in matches:
                expected = {"policy_ref": ref, "policy_id": record.policy_id, "version": record.version,
                            "build_id": record.build_id, "rule_type": record.rule_type.value,
                            "source_doc": record.source_doc, "source_locator": record.locator,
                            "source_version": source_version,
                            "source_digest": source_digest,
                            "provenance": policy_provenance,
                            "priority": record.priority, "effective_from": record.effective_from,
                            "effective_to": record.effective_to, "scope": list(record.scope),
                            "field": field, "value": value}
                if (item.source_type is not SourceType.DOCUMENT or item.source != record.source_doc
                    or item.version != record.version or not expected.keys() <= item.metadata.keys() or any(
                        canonical(item.metadata.get(k)) != canonical(v) for k, v in expected.items())):
                    raise ValueError("derived policy reference/evidence provenance mismatch")
