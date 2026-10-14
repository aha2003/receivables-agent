"""Case state machine (SPEC §18.2; docs/DECISIONS.md D-019).

A case is one overdue open invoice. ``transition`` applies one trigger to the current state
and returns the result; it never reads the clock or the database. Each trigger corresponds
to one row of the §18.2 table, and the guards of that row (promise date, unanswered messages,
days overdue) are checked here so callers cannot apply them differently.

A trigger that matches no row for the current state leaves the state unchanged and is
reported with ``applied=False`` (events arrive from data in any order; the orchestrator
traces them). Ordering several triggers that happen on the same day is the caller's job.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

__all__ = [
    "Assessed",
    "BecameOverdue",
    "CaseState",
    "CaseTrigger",
    "DisputeOpened",
    "DisputeResolved",
    "EscalateExecuted",
    "MessageSent",
    "NoReply",
    "PaidInFull",
    "PromiseBroken",
    "PromiseMade",
    "Transition",
    "transition",
]

ESCALATION_MIN_UNANSWERED = 2
ESCALATION_MIN_DAYS_OVERDUE = 60  # strictly more than this


class CaseState(StrEnum):
    NEW = "NEW"
    ASSESSED = "ASSESSED"
    AWAITING_REPLY = "AWAITING_REPLY"
    DISPUTE_OPEN = "DISPUTE_OPEN"
    PROMISE_PENDING = "PROMISE_PENDING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class BecameOverdue:
    """The invoice became overdue and has no case yet."""


@dataclass(frozen=True, slots=True)
class Assessed:
    """An agent run assessed the case (risk, flags, playbook)."""


@dataclass(frozen=True, slots=True)
class MessageSent:
    """An outbound message was sent (L2) or marked sent (L1)."""


@dataclass(frozen=True, slots=True)
class DisputeOpened:
    """A ``DISPUTE_OPENED`` event."""


@dataclass(frozen=True, slots=True)
class DisputeResolved:
    """A ``DISPUTE_RESOLVED`` event."""


@dataclass(frozen=True, slots=True)
class PromiseMade:
    """A ``PROMISE_TO_PAY`` event; it counts only if ``promise_date >= as_of``."""

    promise_date: date
    as_of: date


@dataclass(frozen=True, slots=True)
class PromiseBroken:
    """The promise was inferred broken (§18.7)."""


@dataclass(frozen=True, slots=True)
class NoReply:
    """Consecutive outbound messages with no inbound reply, counted by the caller."""

    unanswered_outbound: int
    days_overdue: int


@dataclass(frozen=True, slots=True)
class EscalateExecuted:
    """The ``escalate`` tool was executed (§18.3; row added by D-019)."""


@dataclass(frozen=True, slots=True)
class PaidInFull:
    """The open amount reached 0."""


type CaseTrigger = (
    BecameOverdue
    | Assessed
    | MessageSent
    | DisputeOpened
    | DisputeResolved
    | PromiseMade
    | PromiseBroken
    | NoReply
    | EscalateExecuted
    | PaidInFull
)


@dataclass(frozen=True, slots=True)
class Transition:
    from_state: CaseState | None
    to_state: CaseState | None
    trigger: CaseTrigger
    applied: bool
    # §18.2: entering ESCALATED always creates a human task (the ``escalate`` tool).
    escalation_task: bool


def _target(state: CaseState | None, trigger: CaseTrigger) -> CaseState | None:
    """The §18.2 target state, or None when no row matches."""
    S = CaseState
    match trigger:
        case BecameOverdue():
            return S.NEW if state is None else None
        case _ if state is None:
            return None  # every other row needs an existing case
        case PaidInFull():
            return S.RESOLVED
        case _ if state is S.RESOLVED:
            return None  # RESOLVED is final
        case Assessed():
            return S.ASSESSED if state is S.NEW else None
        case MessageSent():
            return S.AWAITING_REPLY if state is S.ASSESSED else None
        case DisputeOpened():
            return S.DISPUTE_OPEN
        case EscalateExecuted():
            return S.ESCALATED
        case DisputeResolved():
            return S.ASSESSED if state is S.DISPUTE_OPEN else None
        case PromiseMade(promise_date=promise_date, as_of=as_of):
            return S.PROMISE_PENDING if promise_date >= as_of else None
        case PromiseBroken():
            return S.ESCALATED if state is S.PROMISE_PENDING else None
        case NoReply(unanswered_outbound=unanswered, days_overdue=days):
            if (
                state is S.AWAITING_REPLY
                and unanswered >= ESCALATION_MIN_UNANSWERED
                and days > ESCALATION_MIN_DAYS_OVERDUE
            ):
                return S.ESCALATED
            return None


def transition(state: CaseState | None, trigger: CaseTrigger) -> Transition:
    """Apply one trigger to ``state`` (None = the invoice has no case yet)."""
    target = _target(state, trigger)
    if target is None:
        return Transition(state, state, trigger, applied=False, escalation_task=False)
    # One task per entry: running ``escalate`` on an escalated case adds no second task.
    entering_escalation = target is CaseState.ESCALATED and state is not CaseState.ESCALATED
    return Transition(state, target, trigger, applied=True, escalation_task=entering_escalation)
