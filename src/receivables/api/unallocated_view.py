"""The Unallocated cash page's data (SPEC §10, §6.7; D-040).

Unallocated payments by customer, next to that customer's overdue exposure, each with the
customer's open invoices it could be allocated to. A choice the rules would refuse is listed
but not offered, with its reason; the server checks again on submit (allocations.py).
"""

from datetime import date
from typing import Any

from sqlalchemy import Engine, select

from receivables.adapters.db.models import Customer, Payment
from receivables.adapters.db.repo import load_invoices, load_payments
from receivables.core.ledger import build_ledger

__all__ = ["unallocated_view"]


def unallocated_view(engine: Engine, today: date) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        waiting = conn.execute(
            select(Payment)
            .where(Payment.invoice_number.is_(None), Payment.customer_id.is_not(None))
            .order_by(Payment.customer_id, Payment.payment_date, Payment.payment_id)
        ).all()
        names = dict(conn.execute(select(Customer.customer_id, Customer.customer_name)).all())
        invoices, payments = load_invoices(conn), load_payments(conn)
        ledger = build_ledger(invoices, payments, today)
    amounts = {i.invoice_number: i.amount for i in invoices}
    applied: dict[str, int] = {}  # every payment, whatever its date (as allocations.py checks)
    for pay in payments:
        if pay.invoice_number is not None:
            applied[pay.invoice_number] = applied.get(pay.invoice_number, 0) + pay.amount
    customers: dict[str, dict[str, Any]] = {}
    for p in waiting:
        open_rows = [r for r in ledger if r.customer_id == p.customer_id and r.open_amount > 0]
        group = customers.setdefault(
            p.customer_id,
            {
                "customer_id": p.customer_id,
                "customer_name": names.get(p.customer_id, p.customer_id),
                "unallocated": 0,
                "overdue": sum(r.open_amount for r in open_rows if (r.days_overdue or 0) > 0),
                "payments": [],
            },
        )
        received = p.payment_date <= today.isoformat()
        if received:
            group["unallocated"] += p.amount_fils
        group["payments"].append(
            {
                "payment_id": p.payment_id,
                "payment_date": p.payment_date,
                "amount": p.amount_fils,
                "received": received,
                "invoices": [
                    {
                        "invoice_number": r.invoice_number,
                        "open_amount": r.open_amount,
                        "days_overdue": r.days_overdue,
                        "fits": p.amount_fils
                        <= min(
                            r.open_amount,
                            amounts[r.invoice_number] - applied.get(r.invoice_number, 0),
                        ),
                    }
                    for r in open_rows
                ],
            }
        )
    return list(customers.values())
