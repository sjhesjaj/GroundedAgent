"""The Stage 6 oracle / reference fixture path (docs/v2/stage6-design.md §19.7).

A system-consistency check, not a Baseline and not a control policy: no
model, no prompt, no proposal. The expected action's semantic arguments
(`args`, plus the first value of each `args_any_of` entry) are validated and
handed straight to ActionGateway.start_action with the trusted request
identity; the operator_script runs exactly as on the policy path (a rerun /
new request re-submits the same reference action under req-1 / req-2); then
the frozen final-state comparator, the link invariants, the event outcomes,
the Guard decision and the hard invariants are checked.

    oracle fails                 -> the Guard, the Gateway or the labels are wrong
    oracle passes, policy fails  -> a control problem

With expected_action == null there is no action to submit: the oracle checks
that the no-action final state matches the contract (the database equals
baseline B plus whatever the labels list, which is nothing for a no-action
case).

Reports call this the oracle or the reference fixture, never a Baseline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .control import canonical_json
from .stage6_runner import run_stage6_reference
from .stage6_scoring import HARD_INVARIANTS, Stage6Score, score_stage6_case

ORACLE_SCHEMA = "v2-stage6-oracle/1"

# The system-consistency checks of the reference path. Control-only metrics
# (selection, args, capabilities, clarification, evidence, final) have no
# control run to judge here.
ORACLE_CHECKS = (
    "final_state_ok", "guard_decision_ok", "guard_reason_ok", "approval_state_ok", "resume_ok",
    "rerun_ok", "execution_ok", "idempotency_ok", "action_claim_grounded", "audit_trace_ok",
)


@dataclass(frozen=True, kw_only=True)
class OracleResult:
    schema: str
    case_id: str
    checks: Mapping[str, bool]
    hard_invariants: Mapping[str, bool]
    final_status_ok: bool
    oracle_ok: bool
    score: Stage6Score

    def failed(self) -> tuple[str, ...]:
        failed = [name for name in ORACLE_CHECKS if not self.checks[name]]
        failed += [name for name in HARD_INVARIANTS if not self.hard_invariants[name]]
        if not self.final_status_ok:
            failed.append("final_status_ok")
        return tuple(failed)

    def to_dict(self) -> dict[str, object]:
        return {"schema": self.schema, "case_id": self.case_id, "checks": dict(self.checks),
                "hard_invariants": dict(self.hard_invariants),
                "final_status_ok": self.final_status_ok, "oracle_ok": self.oracle_ok,
                "details": self.score.to_dict()["details"]}

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


def run_oracle(case: Mapping) -> OracleResult:
    """Run the reference fixture path for one case and judge system consistency."""
    labels = json.loads(canonical_json(case))
    result = run_stage6_reference(labels)
    score = score_stage6_case(labels, result)
    checks = {name: score.metrics[name] for name in ORACLE_CHECKS}
    final = score.details["final_status"]
    expected = labels["expected_action"]
    final_status_ok = (final["actual"] is None if expected is None
                       else final["actual"] == final["expected"] and final["actual_code"] == final["expected_code"])
    hard = dict(score.hard_invariants)
    return OracleResult(schema=ORACLE_SCHEMA, case_id=labels["case_id"], checks=checks,
                        hard_invariants=hard, final_status_ok=final_status_ok,
                        oracle_ok=all(checks.values()) and all(hard.values()) and final_status_ok,
                        score=score)
