"""Allocating an unallocated payment to one invoice (SPEC §6.7, §9; T-API-09, T-API-10; D-040).

The one ledger change a person makes in the app; nothing automated calls it (the agent may only
propose an allocation, which a person approves). In one ``BEGIN IMMEDIATE`` transaction it sets
the payment's ``invoice_number`` when all of these hold, else it changes nothing and answers
409 (404 for an unknown payment or invoice):

- the payment has no invoice yet (``ALREADY_ALLOCATED``);
- it is dated on or before today (``NOT_YET_RECEIVED``, D-040 option 2a);
- the invoice is of the payment's customer (``CUSTOMER_MISMATCH``);
- the invoice is in today's ledger and the payment is not larger than its open amount today
  (``EXCEEDS_OPEN_AMOUNT``; no splitting, no remainder, §6.7).

An allocation writes ``payment.allocated`` with before and after; a refusal writes
``payment.allocation_refused`` with its reason and is committed although the request fails
(T-API-10). When the invoice is paid in full, its case moves to ``RESOLVED`` at once (§18.2,
option 3a). The actor is the signed-in user.
"""

from typing import Any

from sqlalchemy import Connection, Engine, select, update

from receivables.adapters.clock import Clock
from receivables.adapters.db.agent_store import DbAgentStore
from receivables.adapters.db.models import Invoice, Payment
from receivables.adapters.db.repo import load_invoices, load_payments, write_audit
from receivables.agent.cases import PaidInFull
from receivables.core.ledger import build_ledger
from receivables.core.money import from_fils

__all__ = ["ALLOCATED_ACTION", "AllocationError", "allocate_in", "allocate_payment"]

ALLOCATED_ACTION = "payment.allocated"
REFUSED_ACTION = "payment.allocation_refused"

_DETAIL = {
    "ALREADY_ALLOCATED": "The payment is already allocated to an invoice.",
    "NOT_YET_RECEIVED": "The payment is dated after today.",
    "CUSTOMER_MISMATCH": "The invoice belongs to a different customer than the payment.",
    "NOT_IN_LEDGER": "The invoice is dated after today.",
    "EXCEEDS_OPEN_AMOUNT": (
        "The payment is larger than the invoice's open amount. Choose another invoice; "
        "splitting a payment is not supported."
    ),
}


class AllocationError(Exception):
    """A refusal with its HTTP status, error code and reason."""

    def __init__(self, status: int, code: str, detail: str, reason: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.reason = reason


def _aed(fils: int) -> str:
    return str(from_fils(fils))


def _check(conn: Connection, payment: Any, invoice_number: str, clock: Clock) -> int:
    """The invoice's open amount after allocating, or raises with the refusal reason."""
    today = clock.today()

    def refuse(reason: str) -> AllocationError:
        return AllocationError(409, "conflict", _DETAIL[reason], reason)

    if payment.invoice_number is not None:
        raise refuse("ALREADY_ALLOCATED")
    if payment.payment_date > today.isoformat():
        raise refuse("NOT_YET_RECEIVED")
    customer = conn.execute(
        select(Invoice.customer_id).where(Invoice.invoice_number == invoice_number)
    ).scalar_one()
    if customer != payment.customer_id:
        raise refuse("CUSTOMER_MISMATCH")
    ledger = {
        r.invoice_number: r for r in build_ledger(load_invoices(conn), load_payments(conn), today)
    }
    row = ledger.get(invoice_number)
    if row is None:
        raise refuse("NOT_IN_LEDGER")
    if payment.amount_fils > row.open_amount:
        raise refuse("EXCEEDS_OPEN_AMOUNT")
    return row.open_amount - payment.amount_fils


def allocate_in(
    conn: Connection,
    payment_id: str,
    invoice_number: str,
    *,
    clock: Clock,
    actor: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Allocate in the caller's write transaction. A 409 refusal has already written its audit
    row when it is raised, so the caller commits before failing; a 404 writes nothing."""
    payment = conn.execute(
        select(Payment.__table__).where(Payment.payment_id == payment_id)
    ).one_or_none()
    if payment is None:
        raise AllocationError(404, "not_found", "No payment with that id.")
    known = conn.execute(
        select(Invoice.invoice_number).where(Invoice.invoice_number == invoice_number)
    ).scalar_one_or_none()
    if known is None:
        raise AllocationError(404, "not_found", "No invoice with that number.")
    before = {
        "invoice_number": payment.invoice_number,
        "customer_id": payment.customer_id,
        "amount": _aed(payment.amount_fils),
        "payment_date": payment.payment_date,
    }
    try:
        open_after = _check(conn, payment, invoice_number, clock)
    except AllocationError as error:
        write_audit(
            conn, clock, actor=actor, action=REFUSED_ACTION, entity_type="payment",
            entity_id=payment_id, before=before,
            after={"requested_invoice": invoice_number, "reason": error.reason, **(context or {})},
        )  # fmt: skip
        raise
    conn.execute(
        update(Payment)
        .where(Payment.payment_id == payment_id)
        .values(invoice_number=invoice_number)
    )
    write_audit(
        conn, clock, actor=actor, action=ALLOCATED_ACTION, entity_type="payment",
        entity_id=payment_id, before=before,
        after=before | {"invoice_number": invoice_number,
                        "invoice_open_amount": _aed(open_after), **(context or {})},
    )  # fmt: skip
    if open_after == 0:  # §18.2: the open amount reached 0 (option 3a)
        DbAgentStore(conn, clock).apply_by_person(
            invoice_number, PaidInFull(), actor=actor, context={"payment_id": payment_id}
        )
    return {
        "payment_id": payment_id,
        "invoice_number": invoice_number,
        "customer_id": payment.customer_id,
        "amount": _aed(payment.amount_fils),
        "invoice_open_amount": _aed(open_after),
    }


def allocate_payment(
    engine: Engine, payment_id: str, invoice_number: str, *, clock: Clock, actor: str
) -> dict[str, Any]:
    refusal: AllocationError | None = None
    with engine.connect().execution_options(sqlite_begin="IMMEDIATE") as conn, conn.begin():
        try:
            return allocate_in(conn, payment_id, invoice_number, clock=clock, actor=actor)
        except AllocationError as error:
            if error.reason is None:  # 404: nothing was written
                raise
            refusal = error  # its audit row is committed (T-API-10), then it is raised
    raise refusal
