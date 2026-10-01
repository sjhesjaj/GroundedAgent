"""Reading back a persisted Guard snapshot, and the stale comparison (docs/v2/stage6-design.md §12).

`parse_snapshot_document` is a closed parser for `s6-guard-snapshot/1`. It does
not trust what is on disk: the digest must match, the document must be in its
own canonical form, every key must be expected and every value well typed, and
the facts must rebuild a valid `GuardFacts`. Anything else is
`SnapshotIntegrityError`, which the gateway reports as FAILED
invariant_violation. Nothing is repaired.

`stale_reason(stored, candidate)` compares only the comparable portion, in the
frozen order: record_set_changed, record_version_changed, policy_changed,
action_policy_changed. It never looks at evaluated_at or at the stored
decision, so time passing alone is never stale.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from .action_errors import SnapshotIntegrityError
from .actions import ACTION_NAMES
from .guard import (
    GUARD_FACT_FIELDS,
    GUARD_SNAPSHOT_SCHEMA,
    REASON_CODES_BY_DECISION,
    GuardDecisionKind,
    GuardFacts,
    GuardSnapshot,
)
from .guard_state import RECORD_TABLES
from .policy_source import canonical

STALE_RECORD_SET_CHANGED = "record_set_changed"
STALE_RECORD_VERSION_CHANGED = "record_version_changed"
STALE_POLICY_CHANGED = "policy_changed"
STALE_ACTION_POLICY_CHANGED = "action_policy_changed"
STALE_GUARD_DECISION_CHANGED = "guard_decision_changed"
STALE_REASON_CODES = (
    STALE_RECORD_SET_CHANGED, STALE_RECORD_VERSION_CHANGED, STALE_POLICY_CHANGED,
    STALE_ACTION_POLICY_CHANGED, STALE_GUARD_DECISION_CHANGED,
)

DOCUMENT_KEYS = frozenset({
    "schema", "action_name", "evaluated_at", "records", "policy_build_id",
    "action_spec_version", "risk_policy_version", "decision",
})
DECISION_KEYS = frozenset({"decision", "reason_code", "facts"})


@dataclass(frozen=True, kw_only=True)
class StoredSnapshot:
    """A verified persisted snapshot: the comparable part and its audit decision."""

    snapshot: GuardSnapshot
    decision: GuardDecisionKind
    reason_code: str
    facts: GuardFacts


def _fail(message: str) -> SnapshotIntegrityError:
    return SnapshotIntegrityError("persisted guard snapshot: " + message)


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(what + " must be a non-empty string")
    return value


def _reject_constant(name: str) -> object:
    raise _fail("NaN / Infinity are not JSON")


def parse_snapshot_document(document: object, *, expected_sha256: object,
                            expected_action: str) -> StoredSnapshot:
    """Verify and parse one persisted `s6-guard-snapshot/1` document."""
    if not isinstance(document, str) or not isinstance(expected_sha256, str):
        raise _fail("document and digest must be strings")
    if hashlib.sha256(document.encode("utf-8")).hexdigest() != expected_sha256:
        raise _fail("snapshot_sha256 does not match the document")
    try:
        data = json.loads(document, parse_constant=_reject_constant)
    except ValueError:
        raise _fail("not JSON") from None
    if not isinstance(data, dict) or set(data) != DOCUMENT_KEYS:
        raise _fail("unexpected top-level keys")
    if canonical(data) != document:
        raise _fail("not in canonical form")
    if data["schema"] != GUARD_SNAPSHOT_SCHEMA:
        raise _fail("unknown schema")
    action_name = data["action_name"]
    if action_name not in ACTION_NAMES or action_name != expected_action:
        raise _fail("action_name does not belong to this pending action")
    evaluated_at = _text(data["evaluated_at"], "evaluated_at")
    try:
        parsed = datetime.fromisoformat(evaluated_at)
    except ValueError:
        raise _fail("evaluated_at is not a timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail("evaluated_at has no offset")

    records = data["records"]
    if not isinstance(records, dict) or set(records) != set(RECORD_TABLES[action_name]):
        raise _fail("records must hold exactly the action's record tables")
    parsed_records = []
    for table in sorted(records):
        rows = records[table]
        if not isinstance(rows, dict):
            raise _fail("records." + table + " must be an object")
        pairs = []
        for key, version in rows.items():
            _text(key, "a record key")
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise _fail("a record version must be a positive integer")
            pairs.append((key, version))
        parsed_records.append((table, tuple(sorted(pairs))))

    decision = data["decision"]
    if not isinstance(decision, dict) or set(decision) != DECISION_KEYS:
        raise _fail("decision must hold exactly decision, reason_code, facts")
    try:
        kind = GuardDecisionKind(decision["decision"])
    except ValueError:
        raise _fail("unknown decision") from None
    if decision["reason_code"] not in REASON_CODES_BY_DECISION[kind]:
        raise _fail("reason_code does not belong to the decision")
    facts = decision["facts"]
    if not isinstance(facts, dict) or tuple(sorted(facts)) != tuple(sorted(GUARD_FACT_FIELDS)):
        raise _fail("facts must hold exactly the closed GuardFacts fields")
    refs = facts["selected_policy_refs"]
    if not isinstance(refs, list) or not all(
            isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], list)
            for entry in refs):
        raise _fail("selected_policy_refs has the wrong shape")
    try:
        rebuilt = GuardFacts(**{**facts, "selected_policy_refs": tuple(
            (entry[0], tuple(entry[1])) for entry in refs)})
    except (TypeError, ValueError):
        raise _fail("facts do not form a valid GuardFacts") from None
    if rebuilt.to_record() != facts:
        raise _fail("facts are not in their closed form")

    snapshot = GuardSnapshot(
        schema=GUARD_SNAPSHOT_SCHEMA,
        action_name=action_name,
        evaluated_at=evaluated_at,
        records=tuple(parsed_records),
        policy_build_id=_text(data["policy_build_id"], "policy_build_id"),
        action_spec_version=_text(data["action_spec_version"], "action_spec_version"),
        risk_policy_version=_text(data["risk_policy_version"], "risk_policy_version"),
    )
    return StoredSnapshot(snapshot=snapshot, decision=kind,
                          reason_code=decision["reason_code"], facts=rebuilt)


def stale_reason(stored: GuardSnapshot, candidate: GuardSnapshot) -> str | None:
    """The first stale reason in the frozen order, or None when nothing changed."""
    if not isinstance(stored, GuardSnapshot) or not isinstance(candidate, GuardSnapshot):
        raise TypeError("stale_reason compares two GuardSnapshots")
    if stored.action_name != candidate.action_name:
        raise SnapshotIntegrityError("snapshots of different actions cannot be compared")
    stored_tables = dict(stored.records)
    candidate_tables = dict(candidate.records)
    if set(stored_tables) != set(candidate_tables):
        raise SnapshotIntegrityError("snapshots cover different record tables")
    tables = sorted(stored_tables)
    # 1. a row inserted, deleted, or no longer visible to the trusted customer
    for table in tables:
        if {key for key, _ in stored_tables[table]} != {key for key, _ in candidate_tables[table]}:
            return STALE_RECORD_SET_CHANGED
    # 2. same rows, another version
    for table in tables:
        if dict(stored_tables[table]) != dict(candidate_tables[table]):
            return STALE_RECORD_VERSION_CHANGED
    # 3. another publication of the rules
    if stored.policy_build_id != candidate.policy_build_id:
        return STALE_POLICY_CHANGED
    # 4. another action contract or risk policy version
    if (stored.action_spec_version != candidate.action_spec_version
            or stored.risk_policy_version != candidate.risk_policy_version):
        return STALE_ACTION_POLICY_CHANGED
    return None
