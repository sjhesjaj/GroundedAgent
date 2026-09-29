"""Execute one registered V2 tool call against the trusted context.

The Stage 4 path is read-only by construction, and the checks are hard:

- **Side effects are refused.** A spec declaring anything but
  `side_effect=False` raises `SideEffectForbidden` before its arguments are
  looked at or its handler is touched - whatever the registry contains.
- **Closed arguments, checked first.** Unknown, missing, non-string, blank, or
  identity-carrying arguments raise `ValueError` before the handler, the
  connection, or a cursor is touched.
- **Identity, time, and data come from the context.** The executor has no
  parameter through which a caller could supply any of them.
- **Failure is not absence.** A database fault or an unwired adapter becomes a
  sanitized `ERROR` result, never `EMPTY`. Only the exception's class name
  survives; its text could carry a query, a value, or a row.
- **Nothing private is published.** A handler's trace must carry no argument
  value, no identity, and no SQL; a business trace must match its pinned
  schema exactly; evidence must not carry the identity. A violation raises
  rather than being repaired, because this output is designed to be persisted.
- **Reads leave the database unchanged.** `total_changes` is compared before
  and after the handler, on normal return *and* on exception; a difference
  raises `ReadOnlyViolation`, which takes priority over any error result.

Evidence and trace are linked to the call through `observation_id`, which the
caller supplies (the trace span of the tool call) or leaves as `None`.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Mapping

from orchestration.contracts import (
    OBSERVATION_ID_KEY,
    BusinessEvidence,
    Evidence,
    ToolResult,
    ToolStatus,
)

from .arguments import validate_arguments
from .business_tools import BUSINESS_TRACE_FIELDS
from .context import TrustedExecutionContext
from .errors import SideEffectForbidden, ToolNotReady
from .registry import ToolKind, ToolRegistry, ToolSpec

# Stable taxonomy codes. Never an adapter's or an exception's own code.
ERROR_CODE_TOOL_ERROR = "tool_error"
ERROR_CODE_NOT_READY = "tool_not_ready"
ERROR_CODE_TOOL_REPORTED = "tool_reported_error"

TRACE_OBSERVATION_ID = "observation_id"

_WHITESPACE = re.compile(r"\s+")

# SQL must never leave the tool that owns it. Matched against string leaves
# after whitespace is collapsed, so newline-separated SQL cannot slip past.
SQL_MARKERS = (
    "select ",
    "insert ",
    "update ",
    "delete ",
    "replace ",
    "drop ",
    "alter ",
    "create ",
    " from ",
    " where ",
    "pragma ",
    "attach ",
)


class ReadOnlyViolation(RuntimeError):
    """A read-only tool call changed the database."""


# --------------------------------------------------------------------------
# Result checks
# --------------------------------------------------------------------------


def _require_tool_result(spec: ToolSpec, result: object) -> None:
    """Mirror the whole ToolResult invariant: it is validated on construction
    but not frozen, so a handler could hand back a mutated one."""
    prefix = spec.name + " result"
    if not isinstance(result, ToolResult):
        raise ValueError(prefix + " is " + type(result).__name__ + ", expected a ToolResult")
    if result.tool_name != spec.name:
        raise ValueError(prefix + ".tool_name does not match the invoked tool")
    if not isinstance(result.status, ToolStatus):
        raise ValueError(prefix + ".status must be a ToolStatus member")
    if not isinstance(result.evidence, tuple) or not all(
        isinstance(item, Evidence) for item in result.evidence
    ):
        raise ValueError(prefix + ".evidence must be a tuple of Evidence")
    if not isinstance(result.trace, Mapping):
        raise ValueError(prefix + ".trace must be a mapping")

    has_error_fields = result.error_code is not None or result.error_message is not None
    if result.status is ToolStatus.OK:
        if not result.evidence:
            raise ValueError(prefix + " is ok but carries no evidence")
        if has_error_fields:
            raise ValueError(prefix + " is ok but carries error fields")
    elif result.status is ToolStatus.EMPTY:
        if result.evidence:
            raise ValueError(prefix + " is empty but carries evidence")
        if has_error_fields:
            raise ValueError(prefix + " is empty but carries error fields")
    else:
        if result.evidence:
            raise ValueError(prefix + " reported an error but carries evidence")
        for name in ("error_code", "error_message"):
            value = getattr(result, name)
            # Describe the violation only; never echo the payload.
            if not isinstance(value, str) or not value.strip():
                raise ValueError(prefix + "." + name + " must be a non-empty string")


def _assert_trace_is_publishable(
    spec: ToolSpec, trace: Mapping, secrets: frozenset[str], safe: frozenset[str]
) -> None:
    """No argument value, no identity, no SQL - at any depth, keys included.

    Strings exactly equal to a published name (the tool, its parameters, the
    trace fields) are skipped: they are fixed by this module, and skipping them
    keeps an argument that happens to equal a name from failing the scan.
    """
    prefix = spec.name + " trace"

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                walk(key)
                walk(value)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node not in safe:
            for secret in secrets:
                if secret in node:
                    raise ValueError(
                        prefix + " carries an argument value or the trusted "
                        "identity; traces must record names only"
                    )
            flattened = _WHITESPACE.sub(" ", node).lower()
            for marker in SQL_MARKERS:
                if marker in flattened:
                    raise ValueError(
                        prefix + " carries SQL text; SQL must not leave the "
                        "tool that owns it"
                    )

    walk(trace)


def _assert_business_trace_schema(
    spec: ToolSpec, trace: Mapping, evidence_count: int
) -> None:
    """Pin the whole shape: a key allowlist alone would still let a payload
    ride inside a legitimate field, and a trace could disagree with its call."""
    prefix = spec.name + " trace"
    missing = sorted(BUSINESS_TRACE_FIELDS - set(trace))
    if missing:
        raise ValueError(prefix + " is missing required field(s): " + ", ".join(missing))
    unknown = sorted(str(key) for key in trace if key not in BUSINESS_TRACE_FIELDS)
    if unknown:
        raise ValueError(
            prefix + " has unpublished field(s): " + ", ".join(unknown)
            + "; a business trace may carry only "
            + ", ".join(sorted(BUSINESS_TRACE_FIELDS))
        )
    if trace["tool"] != spec.name:
        raise ValueError(prefix + ".tool does not match the invoked tool")
    if trace["parameter_names"] != sorted(spec.parameter_names):
        raise ValueError(prefix + ".parameter_names does not match the tool's schema")
    if trace["identity_scoped"] is not spec.identity_scoped:
        raise ValueError(prefix + ".identity_scoped does not match the tool's spec")
    for name in ("records_matched", "evidence_count"):
        value = trace[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(prefix + "." + name + " must be an integer")
        if value < 0:
            raise ValueError(prefix + "." + name + " must not be negative")
    if trace["evidence_count"] != evidence_count:
        raise ValueError(prefix + ".evidence_count disagrees with the evidence returned")
    if (trace["records_matched"] == 0) != (evidence_count == 0):
        raise ValueError(prefix + ".records_matched disagrees with the evidence returned")


def _assert_evidence_is_publishable(
    spec: ToolSpec, evidence: tuple[Evidence, ...], customer_id: str
) -> None:
    for item in evidence:
        if spec.kind is ToolKind.BUSINESS_READ and not isinstance(item, BusinessEvidence):
            raise ValueError(spec.name + " must return BusinessEvidence only")
        rendered = json.dumps(item.to_dict(), ensure_ascii=False, default=str)
        if customer_id in rendered:
            raise ValueError(spec.name + " evidence carries the trusted identity")


# --------------------------------------------------------------------------
# Building results
# --------------------------------------------------------------------------


def _error_result(
    spec: ToolSpec, code: str, message: str, observation_id: str | None,
    exception_type: str | None,
) -> ToolResult:
    return ToolResult(
        tool_name=spec.name,
        status=ToolStatus.ERROR,
        evidence=(),
        error_code=code,
        error_message=message,
        trace={
            "tool": spec.name,
            TRACE_OBSERVATION_ID: observation_id,
            "exception_type": exception_type,
        },
    )


def _linked(item: Evidence, observation_id: str | None) -> Evidence:
    return dataclasses.replace(
        item, metadata={**item.metadata, OBSERVATION_ID_KEY: observation_id}
    )


def _total_changes(connection: object) -> int | None:
    """The connection's write counter, or None if it cannot be read (e.g. closed)."""
    try:
        value = getattr(connection, "total_changes", None)
    except Exception:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _require_unchanged(
    spec: ToolSpec, connection: object, before: int | None,
    cause: BaseException | None = None,
) -> None:
    """Raise ReadOnlyViolation if the handler changed the database.

    Called on *every* exit from a handler - normal return or exception - and
    before an exception is classified, so a write followed by a failure can
    never be laundered into an ordinary ERROR result. The original exception is
    kept as `__cause__`.
    """
    if before is None:
        # The counter was unreadable before the call (a closed connection):
        # nothing could have been written through it.
        return
    after = _total_changes(connection)
    if after == before:
        return
    message = (
        spec.name + " changed the database during a read"
        if after is not None
        else spec.name + " left the database connection unverifiable after a read"
    )
    raise ReadOnlyViolation(message) from cause


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def execute_tool(
    registry: ToolRegistry,
    context: TrustedExecutionContext,
    tool_name: str,
    arguments: Mapping[str, str],
    *,
    observation_id: str | None = None,
) -> ToolResult:
    """Run one tool call. Static errors raise; runtime failures become ERROR."""
    if not isinstance(registry, ToolRegistry):
        raise ValueError("registry must be a ToolRegistry, got " + type(registry).__name__)
    if not isinstance(context, TrustedExecutionContext):
        raise ValueError(
            "context must be a TrustedExecutionContext, got " + type(context).__name__
        )
    spec = registry.get(tool_name)

    # Before anything else about the call is even looked at.
    if spec.side_effect is not False:
        raise SideEffectForbidden(
            spec.name + " declares side_effect=True; the Stage 4 path executes "
            "read-only tools only"
        )

    if observation_id is not None and (
        not isinstance(observation_id, str) or not observation_id.strip()
    ):
        raise ValueError("observation_id must be a non-empty string or None")

    checked = validate_arguments(spec.name, spec.parameter_names, arguments)

    secrets = frozenset(set(checked.values()) | {context.customer_id})
    safe = frozenset(
        {spec.name, *spec.parameter_names, *BUSINESS_TRACE_FIELDS}
    )

    before = _total_changes(context.connection)
    try:
        # A fresh copy: the handler can neither retain nor mutate caller state.
        result = spec.handler(context, dict(checked))
    except Exception as exc:
        # The read-only check outranks every classification below: a write
        # followed by a failure is a ReadOnlyViolation, never a tool_error.
        _require_unchanged(spec, context.connection, before, cause=exc)
        if isinstance(exc, (ValueError, TypeError)):
            # A contract violation between executor and handler is a programmer
            # error; it must not be laundered into "the data source was down".
            raise
        if isinstance(exc, ToolNotReady):
            return _error_result(
                spec, ERROR_CODE_NOT_READY, spec.name + " is not available yet",
                observation_id, "ToolNotReady",
            )
        return _error_result(
            spec, ERROR_CODE_TOOL_ERROR,
            # Class name only. str(exc) is never captured, anywhere.
            spec.name + " failed with " + type(exc).__name__,
            observation_id, type(exc).__name__,
        )
    _require_unchanged(spec, context.connection, before)

    _require_tool_result(spec, result)
    if result.status is ToolStatus.ERROR:
        return _error_result(
            spec, ERROR_CODE_TOOL_REPORTED, spec.name + " returned an error",
            observation_id, None,
        )

    _assert_trace_is_publishable(spec, result.trace, secrets, safe)
    if spec.kind is ToolKind.BUSINESS_READ:
        _assert_business_trace_schema(spec, result.trace, len(result.evidence))
    _assert_evidence_is_publishable(spec, result.evidence, context.customer_id)

    return ToolResult(
        tool_name=spec.name,
        status=result.status,
        evidence=tuple(_linked(item, observation_id) for item in result.evidence),
        trace={**result.trace, TRACE_OBSERVATION_ID: observation_id},
    )
