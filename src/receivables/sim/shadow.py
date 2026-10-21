"""Synthetic evaluation mode (SPEC §19.3; S4.07; D-054): capture-rate mechanics, SIMULATED.

Pure. For one week: the invoices overdue at the week's start, the simulated payments received
on them during the week, and a list of invoices (the agent's top 20 from an L0 run, or a
baseline). The capture rate is the share of that week's payments that landed on invoices in
the list:

    capture rate = payments on listed invoices / payments on all invoices overdue at week start

It reproduces T-SHD-01 (agent {A, B}: 10,000 of 15,000 = 0.6667; oldest-first {C, B}: 5,000 of
15,000 = 0.3333). It is a mechanics test only: every value is labelled SIMULATED and is never
presented as causal or as real collections (§19.3).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from receivables.core.scoring import round_half_up

__all__ = [
    "LIST_SIZE",
    "SIMULATED",
    "OverdueInvoice",
    "capture_rate",
    "captured",
    "largest_first",
    "oldest_first",
    "pooled_rate",
    "rate_text",
]

SIMULATED = "SIMULATED"
LIST_SIZE = 20


@dataclass(frozen=True, slots=True)
class OverdueInvoice:
    invoice_number: str
    due_date: date
    open_amount_fils: int


def oldest_first(overdue: Iterable[OverdueInvoice], size: int = LIST_SIZE) -> tuple[str, ...]:
    """The baseline that chases the oldest due dates first."""
    ranked = sorted(overdue, key=lambda i: (i.due_date, i.invoice_number))
    return tuple(i.invoice_number for i in ranked[:size])


def largest_first(overdue: Iterable[OverdueInvoice], size: int = LIST_SIZE) -> tuple[str, ...]:
    """The baseline that chases the largest open amounts first."""
    ranked = sorted(overdue, key=lambda i: (-i.open_amount_fils, i.invoice_number))
    return tuple(i.invoice_number for i in ranked[:size])


def captured(listed: Iterable[str], payments: Mapping[str, int]) -> int:
    """Fils paid during the week on the listed invoices."""
    chosen = set(listed)
    return sum(amount for invoice, amount in payments.items() if invoice in chosen)


def capture_rate(listed: Iterable[str], payments: Mapping[str, int]) -> Fraction | None:
    """``payments`` maps each invoice overdue at the week's start to the fils paid on it during
    the week; ``None`` when nothing was paid (no rate to report)."""
    total = sum(payments.values())
    return None if total == 0 else Fraction(captured(listed, payments), total)


def rate_text(rate: Fraction | None) -> str | None:
    """Four decimals, halves rounded up (T-SHD-01 shows 0.6667 and 0.3333)."""
    return None if rate is None else str(round_half_up(rate, 4))


def pooled_rate(weeks: Sequence[tuple[int, int]]) -> Fraction | None:
    """(captured, total) per week -> the rate over all weeks together."""
    total = sum(t for _, t in weeks)
    return None if total == 0 else Fraction(sum(c for c, _ in weeks), total)
