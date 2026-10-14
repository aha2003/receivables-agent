"""Draft fact block and deterministic templates (SPEC §8.1, §8.4; docs/DECISIONS.md D-022).

The fact block is built by code from ledger rows, never by the LLM; it is the only source of
invoice numbers, amounts and dates a draft may mention. ``render_template`` is the
deterministic fallback of §8.4, worded exactly as the spec. Pure.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from receivables.core.ledger import LedgerRow
from receivables.core.money import from_fils
from receivables.core.validation import DISPUTE_EVENT_LABELS

__all__ = [
    "DEFAULT_CONTACT_NAME",
    "DISPUTE_SENTENCES",
    "FactBlock",
    "FactInvoice",
    "aed",
    "build_fact_block",
    "render_template",
]

DEFAULT_CONTACT_NAME = "Accounts Team"  # no contact-name field exists yet (D-022)

DISPUTE_SENTENCES = {
    "PO_AMOUNT_MISMATCH": (
        "We are reviewing the difference between the purchase order and the invoice "
        "and will confirm the correct amount."
    ),
    "MISSING_PO": "We will reissue the invoice with the correct purchase order reference.",
    "MISSING_DELIVERY_NOTE": "We will send you the delivery documentation for this invoice.",
    "GOODS_OR_SERVICE_ISSUE": (
        "We are looking into the delivery issue you raised and will come back to you."
    ),
    "PRICE_OR_RATE_DISPUTE": "We are checking the rate against the agreement and will confirm.",
    "WRONG_ENTITY_OR_DETAILS": (
        "Please confirm the correct legal details so we can reissue the invoice."
    ),
}


def aed(fils: int) -> str:
    """``AED 50,000.00`` (§8.4 ``{amount_list}`` format)."""
    return f"AED {from_fils(fils):,}"


@dataclass(frozen=True, slots=True, kw_only=True)
class FactInvoice:
    invoice_number: str
    amount: int  # fils; §8.2 allows invoice amounts as well as open amounts
    open_amount: int  # fils
    due_date: date
    days_overdue: int


@dataclass(frozen=True, slots=True, kw_only=True)
class FactBlock:
    customer_name: str
    contact_name: str
    sender_name: str
    invoices: tuple[FactInvoice, ...]
    as_of: date
    tone: int
    dispute_label: str | None = None
    amount_difference: int | None = None  # fils, from a verified classification (§7)

    def to_json(self) -> dict[str, Any]:
        """The §8.1 JSON (amounts as decimal strings, dates ISO), plus each invoice amount."""
        return {
            "customer_name": self.customer_name,
            "contact_name": self.contact_name,
            "sender_name": self.sender_name,
            "invoices": [
                {
                    "invoice_number": i.invoice_number,
                    "amount": str(from_fils(i.amount)),
                    "open_amount": str(from_fils(i.open_amount)),
                    "due_date": i.due_date.isoformat(),
                    "days_overdue": i.days_overdue,
                }
                for i in self.invoices
            ],
            "as_of": self.as_of.isoformat(),
            "tone": self.tone,
            "dispute_label": self.dispute_label,
            "amount_difference": None
            if self.amount_difference is None
            else str(from_fils(self.amount_difference)),
        }


def build_fact_block(
    rows: Sequence[LedgerRow],
    *,
    customer_name: str,
    sender_name: str,
    as_of: date,
    tone: int,
    contact_name: str = DEFAULT_CONTACT_NAME,
    dispute_label: str | None = None,
    amount_difference: int | None = None,
) -> FactBlock:
    """Facts for one message about overdue invoices of exactly one customer, in order.

    Overdue per §2: open and more than 0 days past due (an invoice due on ``as_of`` is not).
    """
    if not rows:
        raise ValueError("a draft needs at least one invoice")
    numbers = [r.invoice_number for r in rows]
    if len(set(numbers)) != len(numbers):
        raise ValueError("invoices in a draft must be distinct")
    if len({r.customer_id for r in rows}) != 1:
        raise ValueError("invoices in a draft must share one customer")
    if tone not in (1, 2, 3):
        raise ValueError("tone is 1, 2 or 3")
    if dispute_label is not None and dispute_label not in DISPUTE_EVENT_LABELS:
        raise ValueError(f"unknown dispute label {dispute_label!r}")
    if amount_difference is not None and amount_difference <= 0:
        raise ValueError("amount_difference must be above zero")
    invoices: list[FactInvoice] = []
    for r in rows:
        if r.open_amount <= 0 or r.days_overdue is None or r.days_overdue <= 0:
            raise ValueError(f"{r.invoice_number} is not overdue")
        invoices.append(
            FactInvoice(
                invoice_number=r.invoice_number,
                amount=r.amount,
                open_amount=r.open_amount,
                due_date=r.due_date,
                days_overdue=r.days_overdue,
            )
        )
    return FactBlock(
        customer_name=customer_name,
        contact_name=contact_name,
        sender_name=sender_name,
        invoices=tuple(invoices),
        as_of=as_of,
        tone=tone,
        dispute_label=dispute_label,
        amount_difference=amount_difference,
    )


_SECOND_SENTENCE = {
    1: "If payment has been made, please send the remittance details so we can allocate it.",
    2: (
        "This is now {days_overdue} days past the due date. "
        "Please arrange payment this week or tell us the date you expect to pay."
    ),
    3: (
        "This invoice is now {days_overdue} days overdue. Please confirm a payment date within "
        "5 working days, or tell us what is preventing payment so we can resolve it."
    ),
}


def render_template(facts: FactBlock) -> tuple[str, str]:
    """(subject, body) from the §8.4 template for ``facts.tone``.

    With several invoices, ``{due_date}`` is the earliest due date and ``{days_overdue}`` the
    largest (D-022).
    """
    invoice_list = ", ".join(i.invoice_number for i in facts.invoices)
    amount_list = ", ".join(aed(i.open_amount) for i in facts.invoices)
    due_date = min(i.due_date for i in facts.invoices).isoformat()
    days_overdue = max(i.days_overdue for i in facts.invoices)
    sentences = [
        f"Our records show that invoice {invoice_list} ({amount_list}) was due on {due_date} "
        "and is still unpaid.",
        _SECOND_SENTENCE[facts.tone].format(days_overdue=days_overdue),
        "If there is a problem with the invoice, please tell us so we can resolve it.",
    ]
    if facts.dispute_label is not None:
        sentences.append(DISPUTE_SENTENCES[facts.dispute_label])
    subject = f"Payment reminder: invoice {invoice_list}"
    body = (
        f"Dear {facts.contact_name},\n\n{' '.join(sentences)}\n\nKind regards,\n{facts.sender_name}"
    )
    return subject, body
