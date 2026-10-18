"""The simulated customers' behaviour (SPEC §19.2, behaviours [A]; S4.02; D-048).

Pure and deterministic: no clock, randomness or I/O. A ``CustomerBehaviour`` is told what the
collections side did (``message_sent``, ``dispute_resolved``, ``escalated``,
``allocation_confirmed``) and asked, day by day, what the customer does (``actions_on``):
replies and payments. Every rule is per invoice: "the first reminder" is the first outbound
message about that invoice (D-048 option 1a), and a reply comes 2 days after it (option 2a).

- Responsive pays in full 2, 3 or 4 days after the first reminder (by ``archetype_index``), or
  25 days after due without one.
- Disputer replies with a dispute (labels cycle PO_AMOUNT_MISMATCH, MISSING_DELIVERY_NOTE,
  MISSING_PO by ``archetype_index``) and pays 3 days after the later of the dispute's
  resolution and a message sent on or after it; otherwise 60 days after due.
- Silent never replies; it pays 35 days after due or 10 days after escalation, if sooner.
- Claims paid replies ALREADY_PAID about the invoice its unallocated cash matches (S4.01: its
  oldest), which then closes only when the allocation is confirmed: it believes it has paid.
  Without contact that invoice is paid 45 days after due, as are its other invoices, contacted
  or not (option 3a).

An event dated D changes only what the customer does after D: the customer's actions on D are
asked first (the agent ticks at 10:00, after them), so an event must be dated the last day
asked. Days asked never go backwards; a skipped day loses nothing, as each action keeps its own
date. Nothing happens to an invoice once it is paid or allocated.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from receivables.core.money import from_fils
from receivables.sim.company import SimCompany, SimCustomer, SimInvoice, SimPayment
from receivables.sim.scenario import Archetype

__all__ = ["CustomerBehaviour", "CustomerPayment", "CustomerReply", "behaviours_for"]

REPLY_AFTER_DAYS = 2
RESPONSIVE_DELAYS = (2, 3, 4)
DISPUTE_LABELS = ("PO_AMOUNT_MISMATCH", "MISSING_DELIVERY_NOTE", "MISSING_PO")
NO_CONTACT_DAYS = {
    Archetype.RESPONSIVE: 25,
    Archetype.DISPUTER: 60,
    Archetype.SILENT: 35,
    Archetype.CLAIMS_PAID: 45,
}
PAY_AFTER_RESOLUTION_DAYS = 3
PAY_AFTER_ESCALATION_DAYS = 10

_TEXTS = {
    "PO_AMOUNT_MISMATCH": "We cannot approve invoice {inv}: its amount does not match purchase "
    "order {po}. Please send a corrected invoice.",
    "MISSING_DELIVERY_NOTE": "We have not received the delivery note for invoice {inv}, so we "
    "cannot approve it for payment.",
    "MISSING_PO": "Invoice {inv} does not quote a purchase order we can match. Please send the "
    "purchase order reference.",
    "ALREADY_PAID": "We paid invoice {inv} in full on {paid_on} by bank transfer, "
    "AED {amount}.",
}


@dataclass(frozen=True, slots=True)
class CustomerReply:
    message_id: str
    customer_id: str
    invoice_number: str
    day: date
    label: str  # what the customer means: the simulation's ground truth, not a classification
    text: str


@dataclass(frozen=True, slots=True)
class CustomerPayment:
    payment_id: str
    customer_id: str
    invoice_number: str
    day: date
    amount_fils: int


Action = CustomerReply | CustomerPayment


@dataclass(slots=True)
class _Invoice:
    invoice: SimInvoice
    claimed: bool  # the invoice a claims-paid customer's cash matches
    contact: date | None = None
    resolved: date | None = None
    response: date | None = None
    escalated: date | None = None
    allocated: date | None = None
    replied: bool = False
    paid: bool = False

    @property
    def closed(self) -> bool:
        return self.paid or self.allocated is not None


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def _suffix(invoice_number: str) -> str:
    """``SIM-007`` -> ``007``: ids built on it (``SIM-M007``) never match an invoice number."""
    return invoice_number.rsplit("-", 1)[1]


class CustomerBehaviour:
    def __init__(
        self,
        customer: SimCustomer,
        invoices: Sequence[SimInvoice],
        cash: SimPayment | None = None,
    ) -> None:
        self.customer = customer
        self.archetype = customer.archetype
        self._cash = cash
        claimed = None
        if self.archetype is Archetype.CLAIMS_PAID and cash is not None and invoices:
            claimed = min(invoices, key=lambda i: (i.due_date, i.invoice_number)).invoice_number
        self._invoices = {
            i.invoice_number: _Invoice(i, claimed=i.invoice_number == claimed) for i in invoices
        }
        self._last_day: date | None = None

    # What the collections side did -----------------------------------------------------------

    def message_sent(self, invoice_number: str, day: date) -> None:
        """An outbound message about the invoice: a reminder, or a response to its dispute."""
        state = self._event(invoice_number, day)
        if state is None:
            return
        if state.contact is None:
            state.contact = day
        if state.resolved is not None and state.response is None and day >= state.resolved:
            state.response = day

    def dispute_resolved(self, invoice_number: str, day: date) -> None:
        state = self._event(invoice_number, day)
        if state is not None and state.resolved is None:
            state.resolved = day

    def escalated(self, invoice_number: str, day: date) -> None:
        state = self._event(invoice_number, day)
        if state is not None and state.escalated is None:
            state.escalated = day

    def allocation_confirmed(self, invoice_number: str, day: date) -> None:
        state = self._event(invoice_number, day)
        if state is not None:
            state.allocated = day

    def _event(self, invoice_number: str, day: date) -> _Invoice | None:
        state = self._invoices.get(invoice_number)
        if state is None:
            raise ValueError(f"{invoice_number} is not an invoice of {self.customer.customer_id}")
        if self._last_day is not None and day < self._last_day:
            raise ValueError("an event cannot be dated before an earlier day already asked")
        if self._last_day != day:
            raise ValueError("ask for the customer's actions on that day before its events")
        return None if state.closed else state

    # What the customer does --------------------------------------------------------------------

    def actions_on(self, day: date) -> tuple[Action, ...]:
        """Everything the customer does up to ``day`` not yet returned, each with its own date
        (only ``day``'s actions when asked every day)."""
        if self._last_day is not None and day < self._last_day:
            raise ValueError("days must not go back to an earlier day")
        self._last_day = day
        actions: list[Action] = []
        for state in self._invoices.values():
            if state.closed:
                continue
            pay = self._pay_day(state)
            reply = self._reply_day(state)
            if (
                not state.replied
                and reply is not None
                and reply <= day
                and (pay is None or reply < pay)
                and (state.allocated is None or state.allocated >= reply)
            ):
                state.replied = True
                actions.append(self._reply(state, reply))
            if pay is not None and pay <= day:
                state.paid = True
                actions.append(self._payment(state, pay))
        return tuple(sorted(actions, key=lambda a: (a.day, a.invoice_number)))

    def _reply_day(self, state: _Invoice) -> date | None:
        writes = self.archetype is Archetype.DISPUTER or state.claimed
        return state.contact + _days(REPLY_AFTER_DAYS) if writes and state.contact else None

    def _pay_day(self, state: _Invoice) -> date | None:
        due = state.invoice.due_date
        fallback = due + _days(NO_CONTACT_DAYS[self.archetype])
        if self.archetype is Archetype.RESPONSIVE and state.contact is not None:
            delay = RESPONSIVE_DELAYS[self.customer.archetype_index % len(RESPONSIVE_DELAYS)]
            return min(fallback, state.contact + _days(delay))
        if self.archetype is Archetype.DISPUTER and state.contact is not None:
            if state.resolved is not None and state.response is not None:
                later = max(state.resolved, state.response)
                return min(fallback, later + _days(PAY_AFTER_RESOLUTION_DAYS))
        if self.archetype is Archetype.SILENT and state.escalated is not None:
            return min(fallback, state.escalated + _days(PAY_AFTER_ESCALATION_DAYS))
        if state.claimed and state.contact is not None and state.contact < fallback:
            return None  # it believes its cash already paid this invoice
        return fallback

    def _reply(self, state: _Invoice, day: date) -> CustomerReply:
        inv = state.invoice
        if state.claimed and self._cash is not None:
            label = "ALREADY_PAID"
            text = _TEXTS[label].format(
                inv=inv.invoice_number,
                paid_on=self._cash.payment_date.isoformat(),
                amount=f"{from_fils(self._cash.amount_fils):,}",
            )
        else:
            label = DISPUTE_LABELS[self.customer.archetype_index % len(DISPUTE_LABELS)]
            text = _TEXTS[label].format(inv=inv.invoice_number, po=inv.po_number)
        return CustomerReply(
            message_id=f"SIM-M{_suffix(inv.invoice_number)}",
            customer_id=self.customer.customer_id,
            invoice_number=inv.invoice_number,
            day=day,
            label=label,
            text=text,
        )

    def _payment(self, state: _Invoice, day: date) -> CustomerPayment:
        inv = state.invoice
        return CustomerPayment(
            payment_id=f"SIM-R{_suffix(inv.invoice_number)}",
            customer_id=self.customer.customer_id,
            invoice_number=inv.invoice_number,
            day=day,
            amount_fils=inv.amount_fils,
        )


def behaviours_for(company: SimCompany) -> Mapping[str, CustomerBehaviour]:
    """A fresh behaviour for each customer of the company, in customer order."""
    cash = {p.customer_id: p for p in company.payments}
    return {
        c.customer_id: CustomerBehaviour(
            c,
            [i for i in company.invoices if i.customer_id == c.customer_id],
            cash.get(c.customer_id),
        )
        for c in company.customers
    }
