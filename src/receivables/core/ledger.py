"""Ledger with as-of time travel (SPEC §6.1, definitions in §2). Pure: amounts are integer fils.

Applied amount: payments of any type (``payment``, ``credit_note``, ``write_off``) dated on or
before ``as_of``. Open amount: ``max(0, invoice amount - applied amount)``. Settled date: the
date cumulative ``payment`` plus ``credit_note`` amounts first reach the invoice amount;
write-offs never create one. Unallocated payments are never applied (docs/DECISIONS.md D-010).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from receivables.core.aging import bucket_for

__all__ = ["SETTLING_TYPES", "InvoiceRecord", "LedgerRow", "PaymentRecord", "build_ledger"]


# Payment types that count towards the settled date (§2); write-offs do not.
SETTLING_TYPES = frozenset({"payment", "credit_note"})


@dataclass(frozen=True, slots=True, kw_only=True)
class InvoiceRecord:
    invoice_number: str
    customer_id: str
    invoice_date: date
    due_date: date
    amount: int
    display_invoice_number: str | None = None
    invoice_type: str | None = None
    po_number: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PaymentRecord:
    payment_id: str
    invoice_number: str | None
    customer_id: str | None
    payment_date: date
    amount: int
    payment_type: str = "payment"


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerRow:
    invoice_number: str
    display_invoice_number: str | None
    invoice_type: str | None
    customer_id: str
    invoice_date: date
    due_date: date
    amount: int
    open_amount: int
    days_overdue: int | None
    bucket: str | None
    settled_date: date | None
    po_number: str | None


def build_ledger(
    invoices: Iterable[InvoiceRecord], payments: Iterable[PaymentRecord], as_of: date
) -> list[LedgerRow]:
    """One row per invoice with ``invoice_date <= as_of``, ordered by invoice number."""
    in_ledger: dict[str, InvoiceRecord] = {}
    for inv in invoices:
        if inv.invoice_date > as_of:
            continue
        if inv.invoice_number in in_ledger:
            raise ValueError(f"duplicate invoice_number {inv.invoice_number!r}")
        in_ledger[inv.invoice_number] = inv

    applied: dict[str, list[PaymentRecord]] = {number: [] for number in in_ledger}
    for pay in payments:
        if pay.invoice_number is None or pay.payment_date > as_of:
            continue
        if pay.invoice_number in applied:
            applied[pay.invoice_number].append(pay)

    rows: list[LedgerRow] = []
    for number in sorted(in_ledger):
        inv = in_ledger[number]
        applied_amount = 0
        settling_amount = 0
        settled: date | None = None
        for pay in sorted(applied[number], key=lambda p: (p.payment_date, p.payment_id)):
            applied_amount += pay.amount
            if pay.payment_type in SETTLING_TYPES:
                settling_amount += pay.amount
                if settled is None and settling_amount >= inv.amount:
                    settled = pay.payment_date
        open_amount = max(0, inv.amount - applied_amount)
        is_open = open_amount > 0
        days = (as_of - inv.due_date).days if is_open else None
        rows.append(
            LedgerRow(
                invoice_number=number,
                display_invoice_number=inv.display_invoice_number,
                invoice_type=inv.invoice_type,
                customer_id=inv.customer_id,
                invoice_date=inv.invoice_date,
                due_date=inv.due_date,
                amount=inv.amount,
                open_amount=open_amount,
                days_overdue=days,
                bucket=bucket_for(days) if days is not None else None,
                settled_date=settled,
                po_number=inv.po_number,
            )
        )
    return rows
