"""GroundedAgent V2 eval runtime (Stage 4.4.0: deterministic foundation).

Executes V2 eval cases. Kept apart from `eval/v2/`, which holds the frozen case
contract and spec that the holdout author received; this package reuses that
contract unchanged and never touches the holdout or its unseal tool.

Re-exports only. This package performs no work at import time.
"""

from __future__ import annotations

from .runtime import (
    DELETE_ORDER,
    EXPECTED_TOOL_NAMES,
    FAULT_GATEWAY_MESSAGE,
    INSERT_ORDER,
    OVERLAY_PRIMARY_KEYS,
    UPDATE_ORDER,
    DatabaseChanged,
    EvalCaseInvalid,
    EvalFixtureError,
    EvalRuntimeDrift,
    EvalRuntimeError,
    FaultGatewayRequired,
    IncompleteLogisticsObservation,
    V2CaseRuntime,
    complete_delivered_at_evidence,
    database_content_sha256,
    derive_item_window_from_logistics_result,
    execute_observation,
)

__all__ = [
    "DELETE_ORDER",
    "EXPECTED_TOOL_NAMES",
    "FAULT_GATEWAY_MESSAGE",
    "INSERT_ORDER",
    "OVERLAY_PRIMARY_KEYS",
    "UPDATE_ORDER",
    "DatabaseChanged",
    "EvalCaseInvalid",
    "EvalFixtureError",
    "EvalRuntimeDrift",
    "EvalRuntimeError",
    "FaultGatewayRequired",
    "IncompleteLogisticsObservation",
    "V2CaseRuntime",
    "complete_delivered_at_evidence",
    "database_content_sha256",
    "derive_item_window_from_logistics_result",
    "execute_observation",
]
