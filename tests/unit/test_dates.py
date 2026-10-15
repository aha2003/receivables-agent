"""Dates as written in a message (SPEC §7.2; D-034): the year is resolved by code from sent_at."""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.core.dates import resolve_message_date

SENT = date(2026, 10, 14)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-20", date(2026, 10, 20)),
        ("20 October", date(2026, 10, 20)),
        ("20 october 2027", date(2027, 10, 20)),
        ("20th of October", date(2026, 10, 20)),
        ("20 Oct.", date(2026, 10, 20)),
        ("October 20", date(2026, 10, 20)),
        ("Oct 20, 2026", date(2026, 10, 20)),
        ("  20   October ", date(2026, 10, 20)),
        ("20/10", date(2026, 10, 20)),  # day first (UAE)
        ("20.10.2026", date(2026, 10, 20)),
        ("5 October", date(2026, 10, 5)),  # nearest: 9 days back, not 356 ahead
        ("20 أكتوبر", date(2026, 10, 20)),  # Arabic month
        ("٢٠ أكتوبر", date(2026, 10, 20)),  # and digits
        ("٢٠ اكتوبر 2026", date(2026, 10, 20)),
    ],
)
def test_dates_resolve(value: str, expected: date) -> None:
    assert resolve_message_date(value, SENT) == expected


@pytest.mark.parametrize(
    ("value", "sent", "expected"),
    [
        ("5 January", date(2026, 12, 28), date(2027, 1, 5)),  # across the new year
        ("28 December", date(2027, 1, 3), date(2026, 12, 28)),  # and back
        ("29 February", date(2027, 6, 1), date(2028, 2, 29)),  # the nearest one that exists
        # 2027-04-14 and 2028-04-14 are 366 days apart; 2027-10-14 is 183 days from each.
        ("14 April", date(2027, 10, 14), date(2028, 4, 14)),  # a tie goes to the future
    ],
)
def test_the_year_is_the_nearest_occurrence(value: str, sent: date, expected: date) -> None:
    assert resolve_message_date(value, sent) == expected


@pytest.mark.parametrize(
    "value",
    ["", "next week", "soon", "31 February 2026", "2026-02-30", "13/13", "0/10", "20", "Oct",
     "20 Octember", "2026-10-20 at noon", "20/10/26"],
)  # fmt: skip
def test_anything_else_is_unresolved(value: str) -> None:
    assert resolve_message_date(value, SENT) is None


@given(st.dates(min_value=date(2001, 1, 1), max_value=date(2098, 12, 31)), st.dates(
    min_value=date(2001, 1, 1), max_value=date(2098, 12, 31)))  # fmt: skip
def test_a_yearless_date_is_never_more_than_half_a_year_away(target: date, sent: date) -> None:
    if (target.month, target.day) == (2, 29):
        return
    resolved = resolve_message_date(f"{target.day} {target.strftime('%B')}", sent)
    assert resolved is not None
    assert (resolved.month, resolved.day) == (target.month, target.day)
    assert abs((resolved - sent).days) <= 183
