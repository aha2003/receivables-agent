"""Dashboard KPIs and unallocated cash (SPEC §6.6, §6.7; §2 definitions). Pure.

Amounts are integer fils. Ratios are exact ``Fraction``s; displayed figures are ``Decimal``
rounded half up (docs/DECISIONS.md D-012).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from receivables.core.aging import aging_summary
from receivables.core.ledger import LedgerRow, PaymentRecord
from receivables.core.scoring import ScoredInvoice, exposure_by_band, round_half_up

__all__ = [
    "DSO_LABEL",
    "DSO_WINDOW_DAYS",
    "CustomerOverdue",
    "DsoResult",
    "KpiSummary",
    "UnallocatedCash",
    "compute_kpis",
    "estimated_dso",
    "overdue_by_customer",
    "overdue_pct",
    "unallocated_cash",
]

DSO_WINDOW_DAYS = 90
DSO_LABEL = "estimated DSO (90-day sales proxy)"
_UNALLOCATED_TYPES = frozenset({"payment", "credit_note"})


@dataclass(frozen=True, slots=True)
class DsoResult:
    value: Decimal | None
    credit_sales_in_window: int
    window_start: date
    window_end: date


@dataclass(frozen=True, slots=True)
class CustomerOverdue:
    customer_id: str
    amount: int
    share: Fraction
    share_pct: Decimal


@dataclass(frozen=True, slots=True)
class UnallocatedCash:
    by_customer: dict[str, int]
    total: int


@dataclass(frozen=True, slots=True)
class KpiSummary:
    total_open: int
    overdue_total: int
    overdue_pct: Decimal | None
    dso: DsoResult
    overdue_by_customer: list[CustomerOverdue]
    exposure_by_band: dict[str, int]
    unallocated: UnallocatedCash


def estimated_dso(
    rows: Iterable[LedgerRow], total_open: int, as_of: date, window_days: int = DSO_WINDOW_DAYS
) -> DsoResult:
    """``total_open / credit_sales_in_window * window`` over invoices dated in the window.

    The window is the ``window_days`` ending on ``as_of`` inclusive (§6.6: 90 days).
    """
    start = as_of - timedelta(days=window_days - 1)
    sales = sum(r.amount for r in rows if start <= r.invoice_date <= as_of)
    value = round_half_up(Fraction(total_open, sales) * window_days, 1) if sales > 0 else None
    return DsoResult(
        value=value, credit_sales_in_window=sales, window_start=start, window_end=as_of
    )


def overdue_pct(overdue_total: int, total_open: int) -> Decimal | None:
    """``100 * overdue_total / total_open`` to one decimal.

    ``None`` (displayed as N/A, never 0%) when nothing is open.
    """
    if total_open == 0:
        return None
    return round_half_up(Fraction(100 * overdue_total, total_open), 1)


def overdue_by_customer(rows: Iterable[LedgerRow]) -> list[CustomerOverdue]:
    """Overdue open amount per customer with its share of total overdue, largest first."""
    totals: dict[str, int] = {}
    for row in rows:
        if row.open_amount > 0 and row.days_overdue is not None and row.days_overdue > 0:
            totals[row.customer_id] = totals.get(row.customer_id, 0) + row.open_amount
    overall = sum(totals.values())
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [
        CustomerOverdue(
            customer_id=customer,
            amount=amount,
            share=Fraction(amount, overall),
            share_pct=round_half_up(Fraction(100 * amount, overall), 2),
        )
        for customer, amount in ordered
    ]


def unallocated_cash(payments: Iterable[PaymentRecord], as_of: date) -> UnallocatedCash:
    """Unallocated ``payment`` and ``credit_note`` amounts dated on or before ``as_of``.

    An unallocated payment has no invoice number and a customer (§2); it never changes any
    invoice's open amount (§6.7).
    """
    totals: dict[str, int] = {}
    for p in payments:
        if (
            p.invoice_number is None
            and p.customer_id is not None
            and p.payment_type in _UNALLOCATED_TYPES
            and p.payment_date <= as_of
        ):
            totals[p.customer_id] = totals.get(p.customer_id, 0) + p.amount
    by_customer = {c: totals[c] for c in sorted(totals)}
    return UnallocatedCash(by_customer=by_customer, total=sum(by_customer.values()))


def compute_kpis(
    rows: list[LedgerRow],
    scored: Iterable[ScoredInvoice],
    payments: Iterable[PaymentRecord],
    as_of: date,
    window_days: int = DSO_WINDOW_DAYS,
) -> KpiSummary:
    summary = aging_summary(rows)
    return KpiSummary(
        total_open=summary.total_open,
        overdue_total=summary.overdue_total,
        overdue_pct=overdue_pct(summary.overdue_total, summary.total_open),
        dso=estimated_dso(rows, summary.total_open, as_of, window_days),
        overdue_by_customer=overdue_by_customer(rows),
        exposure_by_band=exposure_by_band(scored),
        unallocated=unallocated_cash(payments, as_of),
    )
