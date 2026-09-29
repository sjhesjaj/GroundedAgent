"""GroundedAgent V2 after-sales domain (Stage 4.1: domain foundation).

Clock, schema, trusted context, the five read-only tools, and the registry /
executor that runs them. See docs/v2/stage4-design.md.

Re-exports only. This package performs no work at import time.
"""

from __future__ import annotations

from .clock import BUSINESS_TIMEZONE, Clock, FixedClock, SystemClock
from .context import Persona, TrustedExecutionContext
from .errors import RecordIntegrityError, SideEffectForbidden, ToolNotReady
from .executor import ReadOnlyViolation, execute_tool
from .policy import PolicyAdapterNotReady, PolicyRecord, PolicyRuleType, PolicySearchAdapter
from .registry import (
    RUNTIME_TOOL_NAMES,
    ParameterSpec,
    ToolKind,
    ToolRegistry,
    ToolSpec,
    build_runtime_registry,
)

__all__ = [
    "BUSINESS_TIMEZONE",
    "RUNTIME_TOOL_NAMES",
    "Clock",
    "FixedClock",
    "ParameterSpec",
    "Persona",
    "PolicyAdapterNotReady",
    "PolicyRecord",
    "PolicyRuleType",
    "PolicySearchAdapter",
    "ReadOnlyViolation",
    "RecordIntegrityError",
    "SideEffectForbidden",
    "SystemClock",
    "ToolKind",
    "ToolNotReady",
    "ToolRegistry",
    "ToolSpec",
    "TrustedExecutionContext",
    "build_runtime_registry",
    "execute_tool",
]
