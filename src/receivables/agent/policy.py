"""The L2 send policy engine (SPEC §18.4, §18.5; docs/DECISIONS.md D-021).

``evaluate_send`` checks every rule (no short-circuit) and returns the failing reason codes
sorted alphabetically; a send is allowed only when there are none. Pure: the caller supplies
the facts, the draft, the local time and the limits.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from receivables.agent.facts import AgentFacts, MessageHeader
from receivables.core.scoring import active_dispute_label

__all__ = [
    "REASON_CODES",
    "L2Limits",
    "PolicyDecision",
    "SendDraft",
    "SendFacts",
    "evaluate_send",
    "send_facts_from",
]

REASON_CODES = (
    "POLICY_AMOUNT_LIMIT",
    "POLICY_DAILY_CAP",
    "POLICY_DISPUTE_ACTIVE",
    "POLICY_KILL_SWITCH",
    "POLICY_NOT_ALLOWLISTED",
    "POLICY_OUTSIDE_HOURS",
    "POLICY_RECENT_CONTACT",
    "POLICY_RECIPIENT_NOT_ALLOWED",
    "POLICY_TONE_NOT_ALLOWED",
    "POLICY_UNALLOCATED_CASH",
    "POLICY_VALIDATION_FAILED",
)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class L2Limits(Protocol):
    """The §18.4 limits; ``config.agent.l2`` (adapters.config.L2Section) satisfies it."""

    @property
    def allowlisted_customers(self) -> tuple[str, ...]: ...
    @property
    def max_total_open_fils(self) -> int: ...
    @property
    def min_days_between_contacts(self) -> int: ...
    @property
    def daily_send_cap(self) -> int: ...
    @property
    def business_days(self) -> tuple[str, ...]: ...
    @property
    def business_hours(self) -> tuple[time, time]: ...
    @property
    def timezone(self) -> str: ...
    @property
    def kill_switch(self) -> bool: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SendFacts:
    """What the rules need to know about the customer and the invoices in the message."""

    customer_id: str
    contact_email: str | None
    open_amounts: Mapping[str, int]  # invoice number -> open amount in fils
    disputed: frozenset[str]  # invoices in the message with an active dispute
    unallocated_cash: int  # fils
    outbound_dates: tuple[date, ...]  # local dates of earlier outbound messages
    sends_today: int  # sends already made on now_local's date


@dataclass(frozen=True, slots=True, kw_only=True)
class SendDraft:
    invoice_numbers: tuple[str, ...]
    tone: int
    recipient: str
    violations: tuple[str, ...]  # draft validator codes (§8.4)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_send(
    case_facts: SendFacts, draft: SendDraft, now_local: datetime, config: L2Limits
) -> PolicyDecision:
    """Every §18.5 rule, evaluated without short-circuit; reasons sorted alphabetically."""
    if now_local.tzinfo is None:
        raise ValueError("now_local must carry a time zone")
    local = now_local.astimezone(ZoneInfo(config.timezone))
    start, end = config.business_hours
    today = local.date()
    total_open = sum(case_facts.open_amounts.get(n, 0) for n in draft.invoice_numbers)

    failing = {
        "POLICY_KILL_SWITCH": config.kill_switch,
        "POLICY_NOT_ALLOWLISTED": case_facts.customer_id not in config.allowlisted_customers,
        "POLICY_AMOUNT_LIMIT": total_open > config.max_total_open_fils,
        "POLICY_TONE_NOT_ALLOWED": draft.tone != 1,
        "POLICY_DISPUTE_ACTIVE": any(n in case_facts.disputed for n in draft.invoice_numbers),
        "POLICY_UNALLOCATED_CASH": case_facts.unallocated_cash > 0,
        # Calendar-day difference; a message dated after today also counts as recent.
        "POLICY_RECENT_CONTACT": any(
            (today - d).days < config.min_days_between_contacts for d in case_facts.outbound_dates
        ),
        "POLICY_OUTSIDE_HOURS": _WEEKDAYS[local.weekday()] not in config.business_days
        or not start <= local.time() < end,
        "POLICY_DAILY_CAP": case_facts.sends_today >= config.daily_send_cap,
        # Fail closed: a draft naming no invoice, or one these facts do not cover, is invalid.
        "POLICY_VALIDATION_FAILED": bool(draft.violations)
        or not draft.invoice_numbers
        or not set(draft.invoice_numbers) <= set(case_facts.open_amounts),
        "POLICY_RECIPIENT_NOT_ALLOWED": case_facts.contact_email is None
        or draft.recipient != case_facts.contact_email,
    }
    reasons = tuple(sorted(code for code, fails in failing.items() if fails))
    return PolicyDecision(allowed=not reasons, reasons=reasons)


def send_facts_from(
    facts: AgentFacts,
    invoice_numbers: Iterable[str],
    *,
    sends_today: int,
    timezone: str,
    outbound_dates: Iterable[date] | None = None,
) -> SendFacts:
    """``SendFacts`` for a message about ``invoice_numbers`` (all of one customer).

    ``timezone`` must be the policy's ``config.timezone`` so message dates and ``now_local``
    share one calendar. ``outbound_dates`` defaults to the customer's outbound messages in
    ``facts``, which holds only messages dated on or before ``facts.as_of``: when
    ``now_local`` is later than ``as_of`` the caller must pass every outbound message up to
    now, including sandbox outbox sends (S2.07, S3.09).
    """
    numbers = tuple(invoice_numbers)
    rows = [facts.rows[n] for n in numbers]  # KeyError: an unknown invoice is a caller bug
    customers = {r.customer_id for r in rows}
    if len(customers) != 1:
        raise ValueError("a message covers invoices of exactly one customer")
    customer_id = customers.pop()
    if outbound_dates is None:
        outbound_dates = [
            datetime.fromisoformat(m.sent_at).astimezone(ZoneInfo(timezone)).date()
            for m in facts.messages
            if m.direction == "outbound" and _message_customer(facts, m) == customer_id
        ]
    customer = facts.customers.get(customer_id)
    return SendFacts(
        customer_id=customer_id,
        contact_email=None if customer is None else customer.contact_email,
        open_amounts={r.invoice_number: r.open_amount for r in rows},
        disputed=frozenset(
            n
            for n in numbers
            if active_dispute_label(facts.events.get(n, ()), facts.as_of) is not None
        ),
        unallocated_cash=facts.unallocated_by_customer.get(customer_id, 0),
        outbound_dates=tuple(sorted(outbound_dates)),
        sends_today=sends_today,
    )


def _message_customer(facts: AgentFacts, message: MessageHeader) -> str | None:
    if message.customer_id is not None:
        return message.customer_id
    row = facts.rows.get(message.invoice_number) if message.invoice_number else None
    return None if row is None else row.customer_id
