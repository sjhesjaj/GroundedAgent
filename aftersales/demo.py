"""Demo composition: personas, the demo virtual_now, and the demo database.

This is composition-root code, not tool code: it is the one place in the
domain allowed to open a connection. It resolves identity from a *server-side*
persona id - there is deliberately no function that derives identity from
anything a user typed.

Personas are demo stand-ins for a trusted identity. They are not login or
authorization (D18).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Iterator

from .clock import BUSINESS_TIMEZONE, Clock, FixedClock
from .context import Persona, TrustedExecutionContext
from .schema import SCHEMA_PATH

DEMO_SEED_PATH = (
    Path(__file__).resolve().parent.parent
    / "system_fixtures"
    / "aftersales_demo_seed.sql"
)

# The demo's business "now". Fixed so that "delivered N days ago" does not
# drift with the real date. The seed's timestamps are all at or before it.
DEMO_VIRTUAL_NOW = datetime(2026, 11, 15, 10, 0, tzinfo=BUSINESS_TIMEZONE)

DEMO_PERSONAS = MappingProxyType(
    {
        persona.persona_id: persona
        for persona in (
            Persona(persona_id="demo-a", customer_id="CUST-001", display_name="演示顾客甲"),
            Persona(persona_id="demo-b", customer_id="CUST-002", display_name="演示顾客乙"),
        )
    }
)


def resolve_persona(persona_id: str) -> Persona:
    """Look up a server-side persona. Unknown ids are rejected, not guessed."""
    if not isinstance(persona_id, str):
        raise ValueError("persona_id must be a string, got " + type(persona_id).__name__)
    persona = DEMO_PERSONAS.get(persona_id)
    if persona is None:
        # Not echoed: the id is client input.
        raise ValueError("unknown persona; known personas are: " + ", ".join(DEMO_PERSONAS))
    return persona


@contextmanager
def open_demo_database() -> Iterator[sqlite3.Connection]:
    """A fresh in-memory demo database, closed on exit.

    Loaded from the schema and the seed, then switched to `query_only`, so the
    database itself refuses writes for the rest of the connection's life.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.executescript(DEMO_SEED_PATH.read_text(encoding="utf-8"))
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def build_demo_context(
    persona_id: str,
    connection: sqlite3.Connection,
    clock: Clock | None = None,
) -> TrustedExecutionContext:
    return TrustedExecutionContext(
        persona=resolve_persona(persona_id),
        clock=FixedClock(DEMO_VIRTUAL_NOW) if clock is None else clock,
        connection=connection,
    )
