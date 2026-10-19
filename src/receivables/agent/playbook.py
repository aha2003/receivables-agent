"""The customer playbook (SPEC §18.7; S4.05; D-052). Pure: facts and contact times in, rows out.

Recomputed at every tick from the last 365 days up to ``as_of``:

- ``promise_kept_rate = kept / (kept + broken)`` over the promises made in the window (event
  date, option 2a), judged by ``agent.outcomes`` (PENDING and superseded promises do not count);
  ``None`` when fewer than 3 are counted.
- ``avg_days_to_reply``: each outbound contact in the window pairs with the first inbound reply
  from that customer after it, within 14 days (option 1a: two reminders before one reply make
  two pairs); days are Asia/Dubai calendar days; ``None`` when fewer than 3 pairs.
- ``followup_gap_days = clamp(round(avg_days_to_reply * 2), 3, 14)``, rounding halves up as the
  rest of the code does; 7 when there is no average.

Rates are scheduling statistics, not money: they are stored as REAL (§5), rounded to 4 and 2
places; the gap is computed from the exact mean. ``allowlist_suggestion`` is the §18.7 policy
suggestion, proposed for a person to apply in config and never applied (option 3a).
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, tzinfo
from fractions import Fraction

from receivables.agent.facts import DEFAULT_FOLLOWUP_GAP_DAYS, AgentFacts, PlaybookRecord
from receivables.agent.outcomes import BROKEN, KEPT, promise_verdicts
from receivables.core.scoring import round_half_up

__all__ = [
    "WINDOW_DAYS",
    "allowlist_suggestion",
    "build_playbook",
    "followup_gap_days",
    "kept_rate",
    "playbooks_from",
    "reply_delays",
    "same_playbook",
]

WINDOW_DAYS = 365
REPLY_WITHIN_DAYS = 14
MIN_PROMISES = 3
MIN_PAIRS = 3
GAP_MIN, GAP_MAX = 3, 14
SUGGEST_MIN_RATE = Fraction(4, 5)
SUGGEST_MIN_PROMISES = 5


def kept_rate(kept: int, broken: int) -> Fraction | None:
    """``kept / (kept + broken)``; ``None`` under 3 counted promises."""
    counted = kept + broken
    return None if counted < MIN_PROMISES else Fraction(kept, counted)


def reply_delays(
    outbound: Iterable[datetime], inbound: Iterable[datetime], as_of: date, tz: tzinfo
) -> list[int]:
    """Days from each outbound contact in the window to the first later reply within 14 days."""
    start = as_of - timedelta(days=WINDOW_DAYS)
    replies = sorted(m for m in inbound if m.astimezone(tz).date() <= as_of)
    delays: list[int] = []
    for sent in sorted(outbound):
        sent_on = sent.astimezone(tz).date()
        if not start < sent_on <= as_of:
            continue
        reply = next((r for r in replies if r > sent), None)
        if reply is None:
            continue
        days = (reply.astimezone(tz).date() - sent_on).days
        if days <= REPLY_WITHIN_DAYS:
            delays.append(days)
    return delays


def followup_gap_days(avg_days_to_reply: Fraction | None) -> int:
    if avg_days_to_reply is None:
        return DEFAULT_FOLLOWUP_GAP_DAYS
    doubled = int(round_half_up(avg_days_to_reply * 2, 0))
    return min(GAP_MAX, max(GAP_MIN, doubled))


def build_playbook(
    customer_id: str, *, kept: int, broken: int, delays: Sequence[int], updated_at: str = ""
) -> PlaybookRecord:
    rate = kept_rate(kept, broken)
    avg = Fraction(sum(delays), len(delays)) if len(delays) >= MIN_PAIRS else None
    return PlaybookRecord(
        customer_id=customer_id,
        promise_kept_rate=None if rate is None else float(round_half_up(rate, 4)),
        promises_counted=kept + broken,
        avg_days_to_reply=None if avg is None else float(round_half_up(avg, 2)),
        replies_counted=len(delays),
        followup_gap_days=followup_gap_days(avg),
        updated_at=updated_at,
    )


def _customer_of(facts: AgentFacts, customer_id: str | None, invoice: str | None) -> str | None:
    if customer_id is not None:
        return customer_id
    row = facts.rows.get(invoice) if invoice else None
    return None if row is None else row.customer_id


def playbooks_from(
    facts: AgentFacts,
    *,
    grace_days: int,
    outbound: Mapping[str, Sequence[datetime]],
    tz: tzinfo,
) -> dict[str, PlaybookRecord]:
    """Every customer's playbook as of ``facts.as_of`` (``updated_at`` left empty). ``outbound``
    holds each customer's outbound contact times: outbound messages, sandbox mails and drafts a
    person marked sent."""
    start = facts.as_of - timedelta(days=WINDOW_DAYS)
    counts: dict[str, list[int]] = {c: [0, 0] for c in facts.customers}
    events = {e.event_id: e for es in facts.events.values() for e in es}
    for event_id, (invoice, verdict) in promise_verdicts(facts, grace_days).items():
        if verdict not in (KEPT, BROKEN) or not start < events[event_id].event_date:
            continue
        customer = facts.rows[invoice].customer_id
        counts.setdefault(customer, [0, 0])[0 if verdict == KEPT else 1] += 1
    inbound: dict[str, list[datetime]] = {}
    for m in facts.messages:
        customer = _customer_of(facts, m.customer_id, m.invoice_number)
        if m.direction == "inbound" and customer is not None:
            inbound.setdefault(customer, []).append(datetime.fromisoformat(m.sent_at))
    return {
        customer: build_playbook(
            customer,
            kept=kept,
            broken=broken,
            delays=reply_delays(
                outbound.get(customer, ()), inbound.get(customer, ()), facts.as_of, tz
            ),  # fmt: skip
        )
        for customer, (kept, broken) in sorted(counts.items())
    }


def same_playbook(a: PlaybookRecord | None, b: PlaybookRecord) -> bool:
    """Equal apart from ``updated_at``."""
    if a is None:
        return False
    keys = ("promise_kept_rate", "promises_counted", "avg_days_to_reply", "replies_counted",
            "followup_gap_days")  # fmt: skip
    return all(getattr(a, k) == getattr(b, k) for k in keys)


def allowlist_suggestion(playbook: PlaybookRecord, allowlisted: Iterable[str]) -> str | None:
    """§18.7: proposed, never applied; a person changes ``agent.l2.allowlisted_customers``."""
    rate = playbook.promise_kept_rate
    if (
        rate is None
        or playbook.customer_id in set(allowlisted)
        or playbook.promises_counted < SUGGEST_MIN_PROMISES
        or Fraction(str(rate)) < SUGGEST_MIN_RATE
    ):
        return None
    return (
        f"Consider adding {playbook.customer_id} to agent.l2.allowlisted_customers: "
        f"promise_kept_rate {rate} over {playbook.promises_counted} promises. "
        "A person decides and edits config.yaml; the agent never changes it."
    )
