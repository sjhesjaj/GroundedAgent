"""Exceptions with a fixed meaning at the V2 tool boundary.

The executor maps each one to a stable error code. Their messages name a tool,
a parameter path, or a field - never a parameter value, identity, or SQL.
"""

from __future__ import annotations


class ToolNotReady(RuntimeError):
    """A registered tool whose backing adapter is not wired yet.

    Distinct from "found nothing": a missing adapter must never be reported as
    an absence of rules or records.
    """


class ToolTimeout(RuntimeError):
    """A tool call did not complete within its time budget.

    A stable production type, not an eval device: whatever enforces the budget
    raises it at the tool boundary, and the executor reports `tool_timeout`.
    Like every class here, its message names the tool at most - never an
    argument value, an identity, SQL, or a provider / database payload.
    """


class RecordIntegrityError(RuntimeError):
    """The data source returned something a valid source cannot return.

    For example two rows for a primary-key lookup, or a timestamp without an
    offset. This is a data fault, not an empty result.
    """


class SideEffectForbidden(PermissionError):
    """A tool declaring `side_effect=True` was asked to run on a read-only path."""
