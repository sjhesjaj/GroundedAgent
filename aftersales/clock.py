"""The only source of *business* time in the V2 after-sales domain.

Every "what time is it for the business" question - deadlines, whether a rule
is in force, an observation's `observed_at` - is answered by a `Clock` that the
composition root injects. Nothing else in the domain may read the system clock
(docs/v2/stage4-design.md §3.2); `tests/test_v2_clock.py` enforces that with an
AST scan whose allowlist is this one file.

Audit time (trace start, storage created_at, build created_at) and perf timers
are deliberately *not* routed through here: they record when the system did
something, not what time it is for the business.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

# The demo domain's timezone: every business timestamp is ISO-8601 at +08:00.
BUSINESS_TIMEZONE = timezone(timedelta(hours=8))


def require_aware(path: str, value: object) -> datetime:
    """Return `value` if it is a timezone-aware datetime, else raise."""
    if not isinstance(value, datetime):
        raise ValueError(path + " must be a datetime, got " + type(value).__name__)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(path + " must be timezone-aware")
    return value


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """The current business instant, always timezone-aware."""
        ...


class SystemClock:
    """Wall-clock business time, expressed in the business timezone."""

    def __init__(self, tz: timezone = BUSINESS_TIMEZONE) -> None:
        self._tz = tz

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock:
    """A frozen `virtual_now`: demos and eval cases do not drift with real time."""

    def __init__(self, virtual_now: datetime) -> None:
        self._now = require_aware("FixedClock.virtual_now", virtual_now)

    def now(self) -> datetime:
        return self._now

    def __repr__(self) -> str:
        return "FixedClock(" + self._now.isoformat() + ")"
