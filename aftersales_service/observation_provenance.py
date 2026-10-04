"""Observation provenance (M1-A1): what this server itself read, as immutable structured data.

The action grounding gate (action_grounding.py) may rely only on reads made by
the product's own execution path. This module is where those reads are kept:

    register     Conversation._read calls it once the ToolResult is confirmed to
                 belong to the call just made. It keeps the session, the control
                 run, the observation id, the tool name and arguments, the status
                 and the business records the result carries: each record's
                 entity, record id, state version and structural relations, as
                 the evidence's structured fields state them. Nothing else.
    visible_to   Conversation._drive calls it before every policy decision. It
                 fixes the registered reads that decision can see (the
                 observations of its ActionControlState) as an immutable
                 VisibleObservations. An action is grounded in that set only, so
                 a read registered after the proposal can never ratify it.

Trust boundary. Only data produced by the server's read path is registered:
the executor's ToolResult, the evidence metadata (`entity`, `record_id`,
`tool` and the link to this call) and the evidence's `relations` and
`state_version`. Evidence text, locators, error text, customer messages and
model output are never looked at: an observation id or a "verified" claim
inside any text is only text. Evidence that does not carry the structured
fields consistently, or is not linked to this very call, makes the whole
observation malformed; a malformed observation contributes no records.

The ledger is per conversation and in memory. It is append-only; a failed turn
restores the snapshot taken before it, like the rest of the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from aftersales.executor import TRACE_OBSERVATION_ID
from orchestration.contracts import OBSERVATION_ID_KEY, BusinessEvidence, ToolResult, ToolStatus

# The structured evidence metadata a record is identified by (aftersales.business_tools).
METADATA_TOOL = "tool"
METADATA_ENTITY = "entity"
METADATA_RECORD_ID = "record_id"


class ProvenanceError(RuntimeError):
    """An integration error between the read path and the ledger. Never a business outcome."""


def _name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True, kw_only=True)
class ObservedRecord:
    """One business record as a read tool returned it: identity and structure only."""

    entity: str
    record_id: str
    state_version: int
    relations: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "relations", MappingProxyType(dict(self.relations)))


@dataclass(frozen=True, kw_only=True)
class ObservationRecord:
    """One registered read. Built by the ledger only; immutable."""

    session_id: str
    run_index: int
    sequence: int                # position in the conversation's ledger, from 1
    observation_id: str
    tool_name: str
    tool_arguments: Mapping[str, str]
    status: str                  # ToolStatus value: ok / empty / error
    records: tuple[ObservedRecord, ...]
    well_formed: bool            # every evidence item carried consistent structured fields

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_arguments", MappingProxyType(dict(self.tool_arguments)))
        object.__setattr__(self, "records", tuple(self.records))

    @property
    def usable(self) -> bool:
        """A successful read whose records can be relied on."""
        return self.status == ToolStatus.OK.value and self.well_formed

    def record(self, entity: str, record_id: str) -> ObservedRecord | None:
        for item in self.records:
            if item.entity == entity and item.record_id == record_id:
                return item
        return None


def _records(tool_name: str, observation_id: str,
             evidence: Iterable[object]) -> tuple[tuple[ObservedRecord, ...], bool]:
    """The records of one result, from structured fields only; ((), False) if malformed."""
    records: dict[tuple[str, str], ObservedRecord] = {}
    for item in evidence:
        if type(item) is not BusinessEvidence or not isinstance(item.metadata, Mapping):
            return (), False
        metadata = item.metadata
        entity, record_id = metadata.get(METADATA_ENTITY), metadata.get(METADATA_RECORD_ID)
        relations = item.relations
        if (not _name(entity) or not _name(record_id)
                or metadata.get(METADATA_TOOL) != tool_name
                or metadata.get(OBSERVATION_ID_KEY) != observation_id
                or not isinstance(relations, Mapping)
                or not all(_name(key) and _name(value) for key, value in relations.items())):
            return (), False
        record = ObservedRecord(entity=entity, record_id=record_id,
                                state_version=item.state_version, relations=relations)
        seen = records.setdefault((entity, record_id), record)
        # Every field of one record carries the same version and relations.
        if (seen.state_version != record.state_version
                or dict(seen.relations) != dict(record.relations)):
            return (), False
    return tuple(records.values()), True


@dataclass(frozen=True, kw_only=True)
class VisibleObservations:
    """The registered reads one control decision could see, fixed before it was made."""

    session_id: str
    run_index: int
    observations: tuple[ObservationRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        for item in self.observations:
            if type(item) is not ObservationRecord:
                raise ProvenanceError("a visible set holds registered observations only")
            if item.session_id != self.session_id or item.run_index != self.run_index:
                raise ProvenanceError("a decision sees only its own conversation's run")
        sequences = [item.sequence for item in self.observations]
        if sequences != sorted(set(sequences)):
            raise ProvenanceError("a visible set lists each observation once, in ledger order")

    @property
    def observation_ids(self) -> tuple[str, ...]:
        return tuple(item.observation_id for item in self.observations)


class ObservationLedger:
    """The append-only provenance of one conversation's reads."""

    def __init__(self, session_id: str) -> None:
        if not _name(session_id):
            raise ValueError("a ledger belongs to one session")
        self._session_id = session_id
        self._entries: tuple[ObservationRecord, ...] = ()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def entries(self) -> tuple[ObservationRecord, ...]:
        return self._entries

    def register(self, *, run_index: int, observation_id: str, tool_name: str,
                 arguments: Mapping[str, str], result: ToolResult) -> ObservationRecord:
        """Keep one read the server just made, after it was confirmed to be this call's."""
        if isinstance(run_index, bool) or not isinstance(run_index, int) or run_index < 1:
            raise ProvenanceError("a read belongs to one control run")
        if not _name(observation_id) or any(entry.observation_id == observation_id
                                            for entry in self._entries):
            raise ProvenanceError("an observation id is registered once")
        trace = getattr(result, "trace", None)
        if (type(result) is not ToolResult or result.tool_name != tool_name
                or not isinstance(trace, Mapping) or trace.get(TRACE_OBSERVATION_ID) != observation_id):
            raise ProvenanceError("the result does not belong to this call")
        records, well_formed = _records(tool_name, observation_id, result.evidence)
        entry = ObservationRecord(
            session_id=self._session_id, run_index=run_index, sequence=len(self._entries) + 1,
            observation_id=observation_id, tool_name=tool_name, tool_arguments=arguments,
            status=ToolStatus(result.status).value, records=records, well_formed=well_formed)
        self._entries = self._entries + (entry,)
        return entry

    def visible_to(self, *, run_index: int, observation_ids: Iterable[str]) -> VisibleObservations:
        """Fix the reads one decision sees: exactly these registered ids, of this run."""
        by_id = {entry.observation_id: entry for entry in self._entries}
        selected = []
        for observation_id in observation_ids:
            entry = by_id.get(observation_id)
            if entry is None:
                raise ProvenanceError("a decision sees an observation the server never registered")
            selected.append(entry)
        if len({entry.sequence for entry in selected}) != len(selected):
            raise ProvenanceError("a decision sees each observation once")
        return VisibleObservations(session_id=self._session_id, run_index=run_index,
                                   observations=tuple(sorted(selected, key=lambda item: item.sequence)))

    def snapshot(self) -> tuple[ObservationRecord, ...]:
        return self._entries

    def restore(self, saved: tuple[ObservationRecord, ...]) -> None:
        if not isinstance(saved, tuple):
            raise ProvenanceError("a ledger restores its own snapshot")
        self._entries = saved
