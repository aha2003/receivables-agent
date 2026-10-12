"""Clock port (SPEC §3.1, §3.5): system, fixed and simulated clocks in Asia/Dubai."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from receivables.adapters.clock import (
    DUBAI,
    Clock,
    FixedClock,
    SimulatedClock,
    SystemClock,
)

AT = datetime(2026, 10, 31, 9, 30, tzinfo=DUBAI)


def test_dubai_zone() -> None:
    assert ZoneInfo("Asia/Dubai") == DUBAI
    assert AT.utcoffset() == timedelta(hours=4)


@pytest.mark.parametrize("clock", [SystemClock(), FixedClock(AT), SimulatedClock(AT)])
def test_all_clocks_satisfy_the_port(clock: Clock) -> None:
    assert isinstance(clock, Clock)
    now = clock.now()
    assert now.tzinfo == DUBAI
    assert clock.today() == now.date()


def test_system_clock_reads_real_time() -> None:
    before = datetime.now(UTC)
    now = SystemClock().now()
    after = datetime.now(UTC)
    assert before <= now <= after
    assert now.tzinfo == DUBAI


def test_fixed_clock_never_moves() -> None:
    clock = FixedClock(AT)
    assert clock.now() == AT
    assert clock.now() == AT
    assert clock.today() == date(2026, 10, 31)


def test_clock_converts_aware_input_to_dubai() -> None:
    clock = FixedClock(datetime(2026, 10, 31, 22, 0, tzinfo=UTC))
    assert clock.now() == datetime(2026, 11, 1, 2, 0, tzinfo=DUBAI)
    assert clock.now().tzinfo == DUBAI
    assert clock.today() == date(2026, 11, 1)  # Dubai date, not the UTC date


@pytest.mark.parametrize("factory", [FixedClock, SimulatedClock])
def test_naive_datetime_rejected(factory: type[FixedClock] | type[SimulatedClock]) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        factory(datetime(2026, 10, 31, 9, 30))


def test_simulated_clock_advances() -> None:
    clock = SimulatedClock(AT)
    assert clock.now() == AT
    clock.advance(timedelta(days=14))
    assert clock.now() == AT + timedelta(days=14)
    assert clock.today() == date(2026, 11, 14)
    clock.advance(timedelta(0))
    assert clock.now() == AT + timedelta(days=14)


def test_simulated_clock_never_goes_backwards() -> None:
    clock = SimulatedClock(AT)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError, match="backwards"):
        clock.set(AT - timedelta(seconds=1))
    assert clock.now() == AT


def test_simulated_clock_set() -> None:
    clock = SimulatedClock(AT)
    later = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)
    clock.set(later)
    assert clock.now() == later
    assert clock.now().tzinfo == DUBAI
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2027, 1, 1))
