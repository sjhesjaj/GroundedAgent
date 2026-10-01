"""End-to-end orchestration and citation scoring of the shared generation layer.

One case, strictly in this order:

    record     = run_case(case, policy, max_steps=...)      control (frozen policy)
    state      = derive_evidence_state(record)              label-free enrichment
    delivered  = delivered_user_messages(case, record)      user_turns text only,
                                                            checked against the
                                                            record's SHA-256s
    generation = generator.generate(delivered, record, state)
    ---------------- only now are expected_* labels read ----------------
    control    = score_case(case, record, state)            unchanged control score
    citation   = score_citations(expected_evidence, state, generation)
    e2e        = the conjunction below

Citation grounding (citation_grounding_ok)
    For a generated answer, the cited subset of EvidenceState.evidence_items -
    the items whose ref is in citation_refs - goes through the frozen
    expected-evidence matcher (`score_evidence`). Grounding holds only when
    every all_of requirement and every any_of group is matched by a CITED item
    and no cited item matches a forbidden spec. Forbidden evidence that was
    merely retrieved and not cited stays a control diagnostic and does not fail
    grounding; citing it does. A fixed (non-answer) rendering cites nothing and
    needs no citation. A run with no final disposition is not generated and is
    not grounded.

    This checks which evidence the answer relies on. It is not semantic answer
    correctness or factual accuracy: a sentence can still misuse correctly
    cited evidence.

E2E score
    e2e_grounded_success = control_success AND generation_ok AND
    citation_grounding_ok. control_success is copied from the control score and
    never redefined. A GenerationProtocolError is recorded as generation_ok =
    False with its stable code; provider / network errors propagate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from .control import ControlPolicy, UserMessage, canonical_json
from .evidence import EvidenceState, derive_evidence_state
from .generation import (
    STATUS_FIXED,
    STATUS_GENERATED,
    GenerationProtocolError,
    GenerationResult,
    SharedGenerator,
)
from .runner import CaseRunRecord, run_case, text_sha256
from .scoring import CaseScore, EvidenceScore, score_case, score_evidence

E2E_SCORE_SCHEMA = "v2-e2e-score/1"


class E2EIntegrityError(ValueError):
    """The delivered text, record and evidence state do not belong together."""


# --------------------------------------------------------------------------
# Delivered user text
# --------------------------------------------------------------------------


def delivered_user_messages(case: Mapping, record: CaseRunRecord) -> tuple[UserMessage, ...]:
    """The exact user text the run delivered, from case["user_turns"] only.

    Each turn is located by UserMessageRecord.case_turn_index and must hash to
    the record's text_sha256. Nothing but user_turns is read from the case.
    """
    if type(record) is not CaseRunRecord:
        raise E2EIntegrityError("record must be a CaseRunRecord")
    turns = case["user_turns"]
    messages = []
    for delivered in record.user_messages:
        index = delivered.case_turn_index
        if (isinstance(index, bool) or not isinstance(index, int)
                or not 0 <= index < len(turns)):
            raise E2EIntegrityError("a delivered turn index is outside the case turns")
        text = turns[index]["text"]
        if not isinstance(text, str) or text_sha256(text) != delivered.text_sha256:
            raise E2EIntegrityError("delivered user text does not match the recorded SHA-256")
        messages.append(UserMessage(turn_index=delivered.turn_index, text=text))
    return tuple(messages)


# --------------------------------------------------------------------------
# Citation scoring (labels: post-generation only)
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CitationScore:
    generation_status: str | None
    citation_required: bool
    cited_refs: tuple[str, ...]
    evidence: EvidenceScore | None
    forbidden_citation_used: bool
    citation_grounding_ok: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "generation_status": self.generation_status,
            "citation_required": self.citation_required,
            "cited_refs": list(self.cited_refs),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "forbidden_citation_used": self.forbidden_citation_used,
            "citation_grounding_ok": self.citation_grounding_ok,
        }


def score_citations(expected_evidence: Mapping, state: EvidenceState,
                    generation: GenerationResult | None) -> CitationScore:
    """Score what the answer cites against the frozen expected evidence."""
    if not isinstance(state, EvidenceState):
        raise E2EIntegrityError("state must be an EvidenceState")
    if generation is None:  # the generation broke protocol: nothing to ground
        return CitationScore(generation_status=None, citation_required=True, cited_refs=(),
                             evidence=None, forbidden_citation_used=False,
                             citation_grounding_ok=False)
    if generation.status == STATUS_FIXED:
        return CitationScore(generation_status=generation.status, citation_required=False,
                             cited_refs=(), evidence=None, forbidden_citation_used=False,
                             citation_grounding_ok=not generation.citation_refs)
    if generation.status != STATUS_GENERATED:
        return CitationScore(generation_status=generation.status, citation_required=False,
                             cited_refs=(), evidence=None, forbidden_citation_used=False,
                             citation_grounding_ok=False)
    known = {item.ref for item in state.evidence_items}
    cited = set(generation.citation_refs)
    if not cited <= known:
        raise E2EIntegrityError("a citation ref is not in the evidence state")
    cited_items = tuple(item for item in state.evidence_items if item.ref in cited)
    evidence = score_evidence(expected_evidence, cited_items)
    forbidden_used = evidence.forbidden_evidence_present
    return CitationScore(
        generation_status=generation.status,
        citation_required=True,
        cited_refs=generation.citation_refs,
        evidence=evidence,
        forbidden_citation_used=forbidden_used,
        citation_grounding_ok=evidence.evidence_ok and not forbidden_used,
    )


# --------------------------------------------------------------------------
# E2E score
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class E2EScore:
    schema: str
    control_success: bool
    generation_status: str | None
    generation_error: str | None
    generation_ok: bool
    citation_grounding_ok: bool
    forbidden_citation_used: bool
    e2e_grounded_success: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "control_success": self.control_success,
            "generation_status": self.generation_status,
            "generation_error": self.generation_error,
            "generation_ok": self.generation_ok,
            "citation_grounding_ok": self.citation_grounding_ok,
            "forbidden_citation_used": self.forbidden_citation_used,
            "e2e_grounded_success": self.e2e_grounded_success,
        }


@dataclass(frozen=True, kw_only=True)
class E2ECaseResult:
    control_record: CaseRunRecord
    evidence_state: EvidenceState
    generation: GenerationResult | None
    control_score: CaseScore
    citation_score: CitationScore
    e2e_score: E2EScore

    def to_dict(self) -> dict[str, object]:
        """Plain data; the control record and evidence state appear as hashes."""
        return {
            "case_id": self.control_score.case_id,
            "control_run_sha256": self.evidence_state.control_run_sha256,
            "evidence_state_sha256": self.evidence_state.sha256(),
            "generation": None if self.generation is None else self.generation.to_dict(),
            "control_score": self.control_score.to_dict(),
            "citation_score": self.citation_score.to_dict(),
            "e2e_score": self.e2e_score.to_dict(),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def e2e_score(control: CaseScore, generation: GenerationResult | None,
              generation_error: str | None, citation: CitationScore) -> E2EScore:
    generation_ok = generation is not None and generation.status in (STATUS_GENERATED,
                                                                      STATUS_FIXED)
    return E2EScore(
        schema=E2E_SCORE_SCHEMA,
        control_success=control.control_success,
        generation_status=None if generation is None else generation.status,
        generation_error=generation_error,
        generation_ok=generation_ok,
        citation_grounding_ok=citation.citation_grounding_ok,
        forbidden_citation_used=citation.forbidden_citation_used,
        e2e_grounded_success=(control.control_success and generation_ok
                              and citation.citation_grounding_ok),
    )


# --------------------------------------------------------------------------
# One case, end to end
# --------------------------------------------------------------------------


def evaluate_e2e_record(case: Mapping, record: CaseRunRecord, *,
                        generator: SharedGenerator) -> E2ECaseResult:
    """Enrich, generate, and only then score. The generator never sees the case."""
    if not isinstance(generator, SharedGenerator):
        raise TypeError("generator must be a SharedGenerator")
    state = derive_evidence_state(record)
    delivered = delivered_user_messages(case, record)
    try:
        generation = generator.generate(delivered, record, state)
        generation_error = None
    except GenerationProtocolError as error:
        generation, generation_error = None, error.code
    # ---- labels are read only below this line ----
    labels = json.loads(canonical_json(case))  # a private copy; the caller's case is untouched
    control = score_case(labels, record, state)
    citation = score_citations(labels["expected_evidence"], state, generation)
    return E2ECaseResult(
        control_record=record,
        evidence_state=state,
        generation=generation,
        control_score=control,
        citation_score=citation,
        e2e_score=e2e_score(control, generation, generation_error, citation),
    )


def evaluate_e2e_case(case: Mapping, policy: ControlPolicy, *, generator: SharedGenerator,
                      max_steps: int) -> E2ECaseResult:
    """Run one case under a frozen control policy, then the shared E2E layer."""
    record = run_case(case, policy, max_steps=max_steps)
    return evaluate_e2e_record(case, record, generator=generator)


def summarize_e2e(results: Sequence[E2ECaseResult]) -> dict[str, object]:
    """Counts and rates of the E2E flags, in case order."""
    total = len(results)
    summary: dict[str, object] = {"case_count": total}
    for flag in ("control_success", "generation_ok", "citation_grounding_ok",
                 "forbidden_citation_used", "e2e_grounded_success"):
        count = sum(1 for result in results if getattr(result.e2e_score, flag) is True)
        summary[flag + "_count"] = count
        summary[flag + "_rate"] = round(count / total, 6) if total else 0.0
    return summary
