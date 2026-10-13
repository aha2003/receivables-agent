"""Collections queue, next action, draft tone and flags (SPEC §6.4, §6.5, §6.7). Pure."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from receivables.core.config import EngineConfig
from receivables.core.money import from_fils
from receivables.core.scoring import ScoredInvoice

__all__ = [
    "HEURISTIC_NOTE",
    "UNALLOCATED_CASH_FLAG",
    "Queue",
    "QueueFlag",
    "QueueItem",
    "build_queue",
    "draft_tone",
    "next_action",
    "unallocated_cash_message",
]

HEURISTIC_NOTE: Final = "Heuristic score. Not a probability."
UNALLOCATED_CASH_FLAG: Final = "UNALLOCATED_CASH_ON_ACCOUNT"


@dataclass(frozen=True, slots=True)
class QueueFlag:
    code: str
    amount: int


@dataclass(frozen=True, slots=True)
class QueueItem:
    rank: int
    scored: ScoredInvoice
    action: str
    tone: int
    flags: tuple[QueueFlag, ...] = ()


@dataclass(frozen=True, slots=True)
class Queue:
    items: tuple[QueueItem, ...]
    note: str = HEURISTIC_NOTE


def next_action(scored: ScoredInvoice) -> str:
    """First matching rule of §6.5."""
    if scored.dispute_label is not None:
        return (
            f"Resolve dispute: {scored.dispute_label}"
            if scored.dispute_label
            else "Resolve dispute"
        )
    if scored.broken_promise:
        return "Follow up broken promise to pay"
    if scored.days_overdue > 60:
        return "Escalate: call and send formal reminder"
    if scored.days_overdue > 30:
        return "Send firm reminder"
    return "Send friendly reminder"


def draft_tone(days_overdue: int) -> int:
    """<= 30 days is tone 1; 31-60 is tone 2; > 60 is tone 3 (§6.5)."""
    if days_overdue <= 30:
        return 1
    if days_overdue <= 60:
        return 2
    return 3


def unallocated_cash_message(amount: int) -> str:
    """The §6.7 UI text for an ``UNALLOCATED_CASH_ON_ACCOUNT`` flag (amount in fils)."""
    return (
        f"This customer has AED {from_fils(amount):,} of unallocated cash. "
        "Check whether it belongs to this invoice before chasing."
    )


def build_queue(
    scored: Iterable[ScoredInvoice],
    config: EngineConfig,
    unallocated: Mapping[str, int] | None = None,
) -> Queue:
    """Priority value descending, ties by invoice number ascending, truncated to queue_size.

    Items whose customer has unallocated cash > 0 carry ``UNALLOCATED_CASH_ON_ACCOUNT`` (§6.7).
    """
    unallocated = unallocated or {}
    ordered = sorted(scored, key=lambda s: (-s.priority_value, s.invoice_number))
    items: list[QueueItem] = []
    for rank, s in enumerate(ordered[: config.queue_size], start=1):
        cash = unallocated.get(s.customer_id, 0)
        flags = (QueueFlag(code=UNALLOCATED_CASH_FLAG, amount=cash),) if cash > 0 else ()
        items.append(
            QueueItem(
                rank=rank,
                scored=s,
                action=next_action(s),
                tone=draft_tone(s.days_overdue),
                flags=flags,
            )
        )
    return Queue(items=tuple(items))
