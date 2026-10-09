"""Current-session pending gateway outcomes, for explanation only."""

from __future__ import annotations

import json
from typing import Callable, Mapping

from aftersales.executor import TRACE_OBSERVATION_ID
from orchestration.contracts import Evidence, SourceType, ToolResult, ToolStatus

PENDING_TOOL_NAME = "get_my_pending_requests"
PENDING_TOOL_PARAMETERS: tuple[str, ...] = ()


def pending_tool_schema() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": PENDING_TOOL_NAME,
            "description": "查询本会话仍待审批的申请及当前状态。订单的历史售后单不能代表这次申请；结果只用于说明进度，不能作为提交新动作的核对依据。",
            "parameters": {"type": "object", "properties": {}, "required": [],
                           "additionalProperties": False},
        },
    }


def pending_tool_result(arguments: Mapping[str, str], *, observation_id: str,
                        read_pending: Callable[[], list[dict]], as_of: str) -> ToolResult:
    trace = {TRACE_OBSERVATION_ID: observation_id, "tool": PENDING_TOOL_NAME}
    if not isinstance(arguments, Mapping) or arguments:
        return ToolResult(tool_name=PENDING_TOOL_NAME, status=ToolStatus.ERROR,
                          error_code="invalid_arguments",
                          error_message="get_my_pending_requests accepts no arguments", trace=trace)
    pending = read_pending()
    trace["pending_requests"] = len(pending)
    if not pending:
        return ToolResult(tool_name=PENDING_TOOL_NAME, status=ToolStatus.EMPTY, trace=trace)
    # Plain Evidence is intentional: no BusinessEvidence record identifiers,
    # relations or state versions can enter the action-grounding ledger.
    evidence = Evidence(
        content="【本会话待审批申请；仅用于解释进度，不能作为新动作依据】\n"
                + json.dumps(pending, ensure_ascii=False, sort_keys=True),
        source_type=SourceType.BUSINESS, source="current_session/gateway_pending_requests",
        locator="current_session:pending_requests", version="1", authority=100,
        observed_at=as_of,
        metadata={"tool": PENDING_TOOL_NAME, TRACE_OBSERVATION_ID: observation_id,
                  "pending_requests": len(pending)},
    )
    return ToolResult(tool_name=PENDING_TOOL_NAME, status=ToolStatus.OK,
                      evidence=(evidence,), trace=trace)
