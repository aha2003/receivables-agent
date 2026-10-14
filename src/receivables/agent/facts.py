"""Facts the agent's read tools see for one ``as_of`` (SPEC §18.3, §18.6; D-020).

Pure: the database adapter loads the records and ``build_agent_facts`` derives everything
financial through the core (ledger, history, scoring, queue, unallocated cash). Nothing here
reads the clock, the database or the network.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo

from receivables.core.config import EngineConfig
from receivables.core.history import HistoryResult, customer_history
from receivables.core.kpis import unallocated_cash
from receivables.core.ledger import InvoiceRecord, LedgerRow, PaymentRecord, build_ledger
from receivables.core.queue import QueueItem, build_queue
from receivables.core.scoring import EventRecord, ScoredInvoice, score_overdue_invoices
from receivables.core.validation import CustomerRow

__all__ = [
    "BUSINESS_TZ",
    "DEFAULT_FOLLOWUP_GAP_DAYS",
    "AgentFacts",
    "MessageHeader",
    "PlaybookRecord",
    "build_agent_facts",
]

DEFAULT_FOLLOWUP_GAP_DAYS = 7  # §18.7 and the customer_playbook DDL default
BUSINESS_TZ = ZoneInfo("Asia/Dubai")  # §3.1 clock; message timestamps are dated locally


@dataclass(frozen=True, slots=True, kw_only=True)
class PlaybookRecord:
    """One ``customer_playbook`` row (§18.7). Rates are scheduling statistics, not money."""

    customer_id: str
    promise_kept_rate: float | None
    promises_counted: int
    avg_days_to_reply: float | None
    replies_counted: int
    followup_gap_days: int
    updated_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageHeader:
    """A correspondence row without subject or body (bodies need §7.5 redaction, S3.02)."""

    message_id: str
    invoice_number: str | None
    customer_id: str | None
    direction: str
    sent_at: str
    language: str | None


@dataclass(frozen=True, slots=True)
class AgentFacts:
    as_of: date
    config: EngineConfig
    customers: Mapping[str, CustomerRow]
    rows: Mapping[str, LedgerRow]
    scored: Mapping[str, ScoredInvoice]
    queue_items: Mapping[str, QueueItem]
    events: Mapping[str, tuple[EventRecord, ...]]
    history: HistoryResult
    unallocated_payments: tuple[PaymentRecord, ...]
    unallocated_by_customer: Mapping[str, int]
    playbooks: Mapping[str, PlaybookRecord]
    messages: tuple[MessageHeader, ...]
    # Messages left out because ``sent_at`` is not an ISO timestamp with an offset.
    invalid_message_ids: tuple[str, ...] = ()


def _sent_at(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def build_agent_facts(
    *,
    customers: Iterable[CustomerRow],
    invoices: Iterable[InvoiceRecord],
    payments: Iterable[PaymentRecord],
    events: Iterable[EventRecord],
    playbooks: Iterable[PlaybookRecord],
    messages: Iterable[MessageHeader],
    as_of: date,
    config: EngineConfig,
    timezone: tzinfo = BUSINESS_TZ,
) -> AgentFacts:
    """Facts as known on ``as_of``: later invoices, payments, events and messages are left out."""
    payment_list = list(payments)
    event_list = [e for e in events if e.event_date <= as_of]
    rows = build_ledger(list(invoices), payment_list, as_of=as_of)
    history = customer_history(rows, as_of, config)
    scored = score_overdue_invoices(rows, event_list, history, as_of, config)
    cash = unallocated_cash(payment_list, as_of)
    queue = build_queue(scored, config, unallocated=cash.by_customer)

    by_invoice: dict[str, list[EventRecord]] = {}
    for e in sorted(event_list, key=lambda e: (e.event_date, e.event_id)):
        by_invoice.setdefault(e.invoice_number, []).append(e)
    unallocated = tuple(
        sorted(
            (
                p
                for p in payment_list
                if p.invoice_number is None
                and p.customer_id is not None
                and p.payment_type in {"payment", "credit_note"}
                and p.payment_date <= as_of
            ),
            key=lambda p: (p.payment_date, p.payment_id),
        )
    )
    timed: list[tuple[datetime, MessageHeader]] = []
    invalid: list[str] = []
    for m in messages:
        moment = _sent_at(m.sent_at)
        if moment is None:
            invalid.append(m.message_id)
        elif moment.astimezone(timezone).date() <= as_of:  # dated in local business time
            timed.append((moment, m))
    timed.sort(key=lambda t: (t[0], t[1].message_id))  # by instant, whatever the offset
    return AgentFacts(
        as_of=as_of,
        config=config,
        customers={c.customer_id: c for c in customers},
        rows={r.invoice_number: r for r in rows},
        scored={s.invoice_number: s for s in scored},
        queue_items={i.scored.invoice_number: i for i in queue.items},
        events={k: tuple(v) for k, v in by_invoice.items()},
        history=history,
        unallocated_payments=unallocated,
        unallocated_by_customer=cash.by_customer,
        playbooks={p.customer_id: p for p in playbooks},
        messages=tuple(m for _, m in timed),
        invalid_message_ids=tuple(sorted(invalid)),
    )
