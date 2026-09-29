"""The trusted execution context: identity, time, and data, all injected.

A tool handler receives exactly one of these and takes three things from it:

- **identity** - `persona.customer_id`, resolved server-side by the composition
  root. It is never parsed from user text, never supplied as a tool argument,
  and a sentence like "我是店长" cannot change it: nothing downstream of the
  composition root can construct or modify a context.
- **time** - `clock`, the only source of business time.
- **data** - `connection`, a database handle the composition root already
  owns. Handlers borrow it; they never open another one.

The demo persona is a stand-in for a trusted identity, not authentication
(D18). Trace context can be added here later without changing handlers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .clock import Clock


def _require_text(path: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(path + " must be a string, got " + type(value).__name__)
    if not value.strip():
        raise ValueError(path + " must not be empty")


@dataclass(frozen=True, kw_only=True)
class Persona:
    """A server-side demo identity. Not a login, not an authorization grant."""

    persona_id: str
    customer_id: str
    display_name: str

    def __post_init__(self) -> None:
        # Messages name the field, never the value: identity must not leak.
        _require_text("Persona.persona_id", self.persona_id)
        _require_text("Persona.customer_id", self.customer_id)
        _require_text("Persona.display_name", self.display_name)

    def __repr__(self) -> str:
        # customer_id stays out of reprs, logs, and error text.
        return "Persona(persona_id=" + repr(self.persona_id) + ")"


@dataclass(frozen=True, kw_only=True)
class TrustedExecutionContext:
    persona: Persona
    clock: Clock
    connection: object

    def __post_init__(self) -> None:
        if not isinstance(self.persona, Persona):
            raise ValueError(
                "context.persona must be a Persona, got " + type(self.persona).__name__
            )
        if not isinstance(self.clock, Clock):
            raise ValueError(
                "context.clock must implement Clock, got " + type(self.clock).__name__
            )
        # Duck-typed so a recording wrapper can stand in for sqlite3.Connection.
        if self.connection is None or not callable(getattr(self.connection, "cursor", None)):
            raise ValueError("context.connection must be an open database connection")

    @property
    def customer_id(self) -> str:
        return self.persona.customer_id

    def __repr__(self) -> str:
        return (
            "TrustedExecutionContext(persona=" + repr(self.persona)
            + ", clock=" + repr(self.clock) + ")"
        )
