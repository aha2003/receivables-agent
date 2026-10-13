"""Clock port and adapters (SPEC §3.1, §3.5).

Business code never reads the wall clock; it receives a ``Clock`` (or explicit ``as_of`` and
``now`` values). ``SystemClock`` is the only place that reads real time. All clocks return
timezone-aware datetimes in Asia/Dubai.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

__all__ = ["DUBAI", "Clock", "FixedClock", "SimulatedClock", "SystemClock", "utc_timestamp"]

DUBAI = ZoneInfo("Asia/Dubai")


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant, timezone-aware, in Asia/Dubai."""
        ...

    def today(self) -> date:
        """Current Asia/Dubai calendar date."""
        ...


def _to_dubai(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("clock times must be timezone-aware")
    return at.astimezone(DUBAI)


class SystemClock:
    """Real time. Use only at the application edge (API, CLI); never in tests or simulation."""

    def now(self) -> datetime:
        return datetime.now(DUBAI)

    def today(self) -> date:
        return self.now().date()


class FixedClock:
    """Always returns the same instant."""

    def __init__(self, at: datetime) -> None:
        self._at = _to_dubai(at)

    def now(self) -> datetime:
        return self._at

    def today(self) -> date:
        return self._at.date()


class SimulatedClock:
    """Controlled time for the simulator and tests; it only moves forwards."""

    def __init__(self, start: datetime) -> None:
        self._at = _to_dubai(start)

    def now(self) -> datetime:
        return self._at

    def today(self) -> date:
        return self._at.date()

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("simulated clock cannot move backwards")
        self._at = self._at + delta

    def set(self, at: datetime) -> None:
        target = _to_dubai(at)
        if target < self._at:
            raise ValueError("simulated clock cannot move backwards")
        self._at = target


def utc_timestamp(clock: Clock) -> str:
    """ISO-8601 UTC timestamp (seconds, ``Z`` suffix) for database rows (SPEC §5)."""
    return clock.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
