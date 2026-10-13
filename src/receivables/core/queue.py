"""Collections queue, next action and draft tone (SPEC §6.4, §6.5). Pure."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from receivables.core.config import EngineConfig
from receivables.core.scoring import ScoredInvoice

__all__ = ["HEURISTIC_NOTE", "Queue", "QueueItem", "build_queue", "draft_tone", "next_action"]

HEURISTIC_NOTE: Final = "Heuristic score. Not a probability."


@dataclass(frozen=True, slots=True)
class QueueItem:
    rank: int
    scored: ScoredInvoice
    action: str
    tone: int


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


def build_queue(scored: Iterable[ScoredInvoice], config: EngineConfig) -> Queue:
    """Priority value descending, ties by invoice number ascending, truncated to queue_size."""
    ordered = sorted(scored, key=lambda s: (-s.priority_value, s.invoice_number))
    items = tuple(
        QueueItem(rank=i, scored=s, action=next_action(s), tone=draft_tone(s.days_overdue))
        for i, s in enumerate(ordered[: config.queue_size], start=1)
    )
    return Queue(items=items)
