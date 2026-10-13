"""Aging buckets and totals (SPEC §6.2). Pure: amounts are integer fils."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Protocol

__all__ = ["BUCKETS", "AgingSummary", "aging_summary", "bucket_for"]

BUCKETS: Final = ("current", "1-30", "31-60", "61-90", "90+")


def bucket_for(days_overdue: int) -> str:
    """``current`` (<= 0), ``1-30``, ``31-60``, ``61-90``, ``90+`` (>= 91)."""
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


class _OpenRow(Protocol):
    @property
    def open_amount(self) -> int: ...
    @property
    def bucket(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class AgingSummary:
    total_open: int
    overdue_total: int
    bucket_totals: dict[str, int]


def aging_summary(rows: Iterable[_OpenRow]) -> AgingSummary:
    """Totals over open invoices only; every bucket is present, zero if empty."""
    totals: dict[str, int] = dict.fromkeys(BUCKETS, 0)
    for row in rows:
        if row.open_amount > 0:
            if row.bucket is None:
                raise ValueError("open ledger row without a bucket")
            totals[row.bucket] += row.open_amount
    total_open = sum(totals.values())
    return AgingSummary(
        total_open=total_open,
        overdue_total=total_open - totals["current"],
        bucket_totals=totals,
    )
