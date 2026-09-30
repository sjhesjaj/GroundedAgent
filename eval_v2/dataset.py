"""The deterministic evaluation pipeline and dataset runner (Stage 4 Eval Completion).

One case, three strictly separated layers, in this order:

    record = run_case(case, policy, max_steps=...)     control: no labels reach the policy
    state  = derive_evidence_state(record)             enrichment: label-free
    score  = score_case(case, record, state)           scoring: the only label reader

A dataset is those cases in authored order, one at a time, each under a fresh
policy from `policy_factory()`. The factory takes no argument - in particular
never the case - so a policy has no way to see a case id or a label, and a
factory that hands back an instance it returned before fails loudly (no
memory carried from case to case). No shuffling, parallelism, randomness,
clock reading or durations: the same cases, factory and max_steps give a
byte-identical DatasetRun. Repeated trials belong to an outer layer.

Only control-layer metrics are summarized. There is no answer generation yet,
so nothing here measures factuality, citations, cost or latency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .control import ControlPolicy, ControlPolicyContractError, canonical_json
from .evidence import EvidenceState, derive_evidence_state, evidence_state_sha256
from .runner import TERMINATIONS, CaseRunRecord, control_run_sha256, run_case
from .scoring import CaseScore, score_case

DATASET_RUN_SCHEMA = "v2-dataset-run/1"

SUMMARY_FLAGS = ("control_success", "capabilities_ok", "clarification_ok", "evidence_ok",
                 "final_ok", "db_ok")


class DatasetRunError(RuntimeError):
    """The dataset or the policy factory breaks the runner's contract."""


# --------------------------------------------------------------------------
# One case
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EvaluatedCase:
    control_record: CaseRunRecord
    evidence_state: EvidenceState
    score: CaseScore


def evaluate_case(case: object, policy: ControlPolicy, *, max_steps: int) -> EvaluatedCase:
    """Run, then enrich, then score. Labels are read only in the last step."""
    record = run_case(case, policy, max_steps=max_steps)
    state = derive_evidence_state(record)
    score = score_case(case, record, state)
    return EvaluatedCase(control_record=record, evidence_state=state, score=score)


# --------------------------------------------------------------------------
# A dataset
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class DatasetCaseResult:
    """One case's outcome. Hashes and the score only - never user text."""

    case_id: str
    control_run_sha256: str
    evidence_state_sha256: str
    score: CaseScore

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "control_run_sha256": self.control_run_sha256,
            "evidence_state_sha256": self.evidence_state_sha256,
            "score": self.score.to_dict(),
        }


def _rate(count: int, total: int) -> float:
    return round(count / total, 6)


def summarize(scores: Sequence[CaseScore]) -> dict[str, object]:
    """Control-layer summary metrics over the scores, in case order."""
    total = len(scores)
    summary: dict[str, object] = {"case_count": total}
    for flag in SUMMARY_FLAGS:
        count = sum(1 for score in scores if getattr(score, flag) is True)
        summary[flag + "_count"] = count
        summary[flag + "_rate"] = _rate(count, total)
    summary["average_control_steps"] = _rate(sum(score.control_steps for score in scores),
                                             total)
    summary["termination_counts"] = {
        name: sum(1 for score in scores if score.termination == name) for name in TERMINATIONS}
    # Diagnostic only: retrieved, not necessarily relied on.
    summary["forbidden_evidence_present_count"] = sum(
        1 for score in scores if score.evidence.forbidden_evidence_present)
    return summary


@dataclass(frozen=True, kw_only=True)
class DatasetRun:
    schema: str
    max_steps: int
    case_count: int
    case_results: tuple[DatasetCaseResult, ...]
    summary: dict[str, object]
    # In-memory only, for diagnostics; not part of the serialized run.
    evaluations: tuple[EvaluatedCase, ...] = field(default=(), repr=False, compare=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "max_steps": self.max_steps,
            "case_count": self.case_count,
            "case_results": [result.to_dict() for result in self.case_results],
            "summary": json.loads(canonical_json(self.summary)),
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def run_dataset(cases: Sequence[object], *, policy_factory: Callable[[], ControlPolicy],
                max_steps: int) -> DatasetRun:
    """Evaluate `cases` in authored order, each under a fresh `policy_factory()`."""
    if not isinstance(cases, (list, tuple)) or not cases:
        raise DatasetRunError("cases must be a non-empty list or tuple")
    if not callable(policy_factory):
        raise DatasetRunError("policy_factory must be a zero-argument callable")
    case_ids = [case.get("case_id") if isinstance(case, dict) else None for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise DatasetRunError("case ids must be unique within a dataset")

    policies: list[object] = []  # kept alive, so identity checks stay meaningful
    evaluations: list[EvaluatedCase] = []
    for case in cases:
        policy = policy_factory()
        if not isinstance(policy, ControlPolicy):
            raise ControlPolicyContractError("policy_factory() must return a ControlPolicy")
        if any(policy is previous for previous in policies):
            raise DatasetRunError("policy_factory() returned a policy instance it had "
                                  "returned before; every case needs a fresh policy")
        policies.append(policy)
        evaluations.append(evaluate_case(case, policy, max_steps=max_steps))

    results = tuple(
        DatasetCaseResult(
            case_id=evaluated.score.case_id,
            control_run_sha256=control_run_sha256(evaluated.control_record),
            evidence_state_sha256=evidence_state_sha256(evaluated.evidence_state),
            score=evaluated.score,
        )
        for evaluated in evaluations)
    return DatasetRun(
        schema=DATASET_RUN_SCHEMA,
        max_steps=max_steps,
        case_count=len(results),
        case_results=results,
        summary=summarize([result.score for result in results]),
        evaluations=tuple(evaluations),
    )


def dataset_run_sha256(run: DatasetRun) -> str:
    if not isinstance(run, DatasetRun):
        raise ValueError("run must be a DatasetRun, got " + type(run).__name__)
    return run.sha256()
