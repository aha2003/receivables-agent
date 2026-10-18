"""Outcome inference (SPEC §18.7; S4.04; D-051). Pure: facts in, outcomes out.

A ``PROMISE_TO_PAY`` with promise date P is KEPT if the invoice is settled on or before
P + grace (``agent.promise_grace_days``, 2 [A]), BROKEN if ``as_of`` is past P + grace and it is
not settled by then (a late payment stays BROKEN), otherwise PENDING. Settlement is the §2
settled date as of ``as_of``, so a payment dated later is not seen yet.

A promise followed by a newer ``PROMISE_TO_PAY`` for the same invoice, dated on or before its
P + grace, is superseded: the customer changed the date in time, so only the newer promise is
judged (D-051 option 1a). This is different from §2's "broken promise", which has no grace and
drives the queue and the rule planner; this inference drives outcomes and the §18.2
``PROMISE_PENDING`` to ``ESCALATED`` transition.

``infer_outcomes`` gives only final outcomes, each with a fixed id derived from its source, so
recording them is idempotent: PROMISE_KEPT or PROMISE_BROKEN (``OUT-PRM-<event>``),
PAYMENT_RECEIVED for each payment applied to an invoice (``OUT-PAY-<payment>``; credit notes
and write-offs are not payments received) and DISPUTE_RESOLVED for each resolution event
(``OUT-DSP-<event>``).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from receivables.agent.facts import AgentFacts
from receivables.core.money import from_fils

__all__ = [
    "BROKEN",
    "KEPT",
    "PENDING",
    "InferredOutcome",
    "infer_outcomes",
    "promise_outcome",
    "promise_verdicts",
]

KEPT = "KEPT"
BROKEN = "BROKEN"
PENDING = "PENDING"


@dataclass(frozen=True, slots=True)
class InferredOutcome:
    outcome_id: str
    invoice_number: str
    outcome_type: str
    note: str


def promise_outcome(
    promise_date: date, settled_date: date | None, as_of: date, grace_days: int
) -> str:
    """§18.7 for one promise; ``settled_date`` must be the settled date as of ``as_of``."""
    deadline = promise_date + timedelta(days=grace_days)
    if settled_date is not None and settled_date <= deadline:
        return KEPT
    if as_of > deadline:
        return BROKEN
    return PENDING


def promise_verdicts(facts: AgentFacts, grace_days: int) -> Mapping[str, tuple[str, str]]:
    """Event id -> (invoice, verdict) for every promise known on ``facts.as_of`` that is not
    superseded."""
    verdicts: dict[str, tuple[str, str]] = {}
    for invoice, events in sorted(facts.events.items()):
        row = facts.rows.get(invoice)
        if row is None:
            continue
        promises = [e for e in events if e.event_type == "PROMISE_TO_PAY" and e.promise_date]
        for n, event in enumerate(promises):  # events are in (date, id) order
            assert event.promise_date is not None  # noqa: S101 - filtered just above
            deadline = event.promise_date + timedelta(days=grace_days)
            if any(later.event_date <= deadline for later in promises[n + 1 :]):
                continue  # replaced in time by a newer promise
            verdicts[event.event_id] = (
                invoice,
                promise_outcome(event.promise_date, row.settled_date, facts.as_of, grace_days),
            )
    return verdicts


def infer_outcomes(facts: AgentFacts, grace_days: int) -> tuple[InferredOutcome, ...]:
    """The final outcomes known on ``facts.as_of`` (PENDING promises give none)."""
    outcomes: list[InferredOutcome] = []
    for event_id, (invoice, verdict) in promise_verdicts(facts, grace_days).items():
        if verdict == PENDING:
            continue
        settled = facts.rows[invoice].settled_date
        outcomes.append(
            InferredOutcome(
                outcome_id=f"OUT-PRM-{event_id}",
                invoice_number=invoice,
                outcome_type=f"PROMISE_{verdict}",
                note=f"promise {event_id}; settled {settled.isoformat() if settled else 'no'}",
            )
        )
    for payment in facts.applied_payments:
        if payment.payment_type != "payment" or payment.invoice_number is None:
            continue
        outcomes.append(
            InferredOutcome(
                outcome_id=f"OUT-PAY-{payment.payment_id}",
                invoice_number=payment.invoice_number,
                outcome_type="PAYMENT_RECEIVED",
                note=f"payment {payment.payment_id} of AED {from_fils(payment.amount):,} on "
                f"{payment.payment_date.isoformat()}",
            )
        )
    for invoice, events in sorted(facts.events.items()):
        if invoice not in facts.rows:
            continue
        for event in events:
            if event.event_type == "DISPUTE_RESOLVED":
                outcomes.append(
                    InferredOutcome(
                        outcome_id=f"OUT-DSP-{event.event_id}",
                        invoice_number=invoice,
                        outcome_type="DISPUTE_RESOLVED",
                        note=f"event {event.event_id} on {event.event_date.isoformat()}",
                    )
                )
    return tuple(sorted(outcomes, key=lambda o: o.outcome_id))
