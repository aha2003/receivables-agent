"""The customer playbook (SPEC §18.7; S4.05; D-052): T-LRN-01, T-LRN-02, T-LRN-03."""

import ast
from datetime import date, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from receivables.agent.facts import PlaybookRecord
from receivables.agent.playbook import (
    allowlist_suggestion,
    build_playbook,
    followup_gap_days,
    kept_rate,
    playbooks_from,
    reply_delays,
)
from receivables.agent.rules_planner import plan_case
from receivables.core.ledger import PaymentRecord
from receivables.core.scoring import EventRecord

ROOT = Path(__file__).resolve().parents[2]
DUBAI = ZoneInfo("Asia/Dubai")
AS_OF = date(2026, 10, 31)


def at(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=DUBAI)


@pytest.mark.spec("T-LRN-01")
def test_kept_rate_three_kept_one_broken_and_two_kept() -> None:
    assert kept_rate(3, 1) == Fraction(3, 4)
    assert build_playbook("CUST-A", kept=3, broken=1, delays=()).promise_kept_rate == 0.75
    assert kept_rate(2, 0) is None
    assert build_playbook("CUST-B", kept=2, broken=0, delays=()).promise_kept_rate is None
    assert build_playbook("CUST-B", kept=2, broken=0, delays=()).promises_counted == 2


def _pairs(delays: list[int]) -> tuple[list[datetime], list[datetime]]:
    outbound = [at(AS_OF - timedelta(days=60 - 20 * n)) for n in range(len(delays))]
    inbound = [o + timedelta(days=d, hours=1) for o, d in zip(outbound, delays, strict=True)]
    return outbound, inbound


@pytest.mark.spec("T-LRN-02")
@pytest.mark.parametrize(
    ("delays", "gap"), [([1, 2, 3], 4), ([5, 6, 7], 12), ([10, 10, 10], 14), ([1, 2], 7)]
)
def test_followup_gap_from_reply_delays(delays: list[int], gap: int) -> None:
    outbound, inbound = _pairs(delays)
    measured = reply_delays(outbound, inbound, AS_OF, DUBAI)
    assert measured == delays
    playbook = build_playbook("CUST-A", kept=0, broken=0, delays=measured)
    assert playbook.followup_gap_days == gap
    assert playbook.replies_counted == len(delays)
    assert playbook.avg_days_to_reply == (None if len(delays) < 3 else sum(delays) / 3)


def test_the_gap_rounds_halves_up_and_is_clamped() -> None:
    assert followup_gap_days(Fraction(9, 4)) == 5  # 4.5 -> 5 (Python's round() gives 4)
    assert followup_gap_days(Fraction(1, 2)) == 3  # 1 -> clamped to 3
    assert followup_gap_days(Fraction(0)) == 3
    assert followup_gap_days(Fraction(29, 4)) == 14  # 14.5 -> 15 -> clamped to 14
    assert followup_gap_days(None) == 7


def test_a_reply_after_14_days_or_before_the_message_does_not_pair() -> None:
    sent = at(date(2026, 10, 1))
    assert reply_delays([sent], [sent + timedelta(days=15)], AS_OF, DUBAI) == []
    assert reply_delays([sent], [sent + timedelta(days=14)], AS_OF, DUBAI) == [14]
    assert reply_delays([sent], [sent - timedelta(hours=1)], AS_OF, DUBAI) == []
    assert reply_delays([sent], [sent + timedelta(hours=2)], AS_OF, DUBAI) == [0]  # same day


def test_two_reminders_before_one_reply_make_two_pairs() -> None:
    first, second = at(date(2026, 10, 1)), at(date(2026, 10, 4))
    reply = at(date(2026, 10, 6))
    assert reply_delays([first, second], [reply], AS_OF, DUBAI) == [5, 2]


def test_only_the_last_365_days_count() -> None:
    old = at(AS_OF - timedelta(days=365))  # outside: the window is (as_of - 365, as_of]
    inside = at(AS_OF - timedelta(days=364))
    replies = [old + timedelta(days=1), inside + timedelta(days=1)]
    assert reply_delays([old, inside], replies, AS_OF, DUBAI) == [1]
    future = at(AS_OF + timedelta(days=1))
    assert reply_delays([future], [future + timedelta(days=1)], AS_OF, DUBAI) == []
    late_reply = at(AS_OF) + timedelta(days=1)  # dated after as_of: not known yet
    assert reply_delays([at(AS_OF)], [late_reply], AS_OF, DUBAI) == []


def _promise(event_id: str, invoice: str, made: date, due: date) -> EventRecord:
    return EventRecord(event_id=event_id, invoice_number=invoice, event_type="PROMISE_TO_PAY",
                       event_date=made, promise_date=due)  # fmt: skip


@pytest.mark.spec("T-LRN-01")
def test_playbooks_from_counts_judged_promises_per_customer(make_agent_facts) -> None:
    paid = PaymentRecord(payment_id="P-T01", invoice_number="INV-010", customer_id=None,
                         payment_date=date(2026, 10, 30), amount=600_000)  # fmt: skip
    facts = make_agent_facts(
        extra_payments=(paid,),
        extra_events=(
            # CUST-A: three kept (paid by the promise date + 2), one broken -> 0.75
            _promise("E-T01", "INV-001", date(2026, 7, 25), date(2026, 8, 8)),  # paid 08-10
            _promise("E-T02", "INV-002", date(2026, 9, 1), date(2026, 9, 20)),  # paid 09-20
            _promise("E-T03", "INV-010", date(2026, 10, 20), date(2026, 10, 30)),  # paid 10-30
            _promise("E-T04", "INV-003", date(2026, 10, 2), date(2026, 10, 10)),  # unpaid
            # CUST-C: one kept and the golden E-002 broken (2 counted) -> None; one pending
            _promise("E-T05", "INV-007", date(2026, 8, 1), date(2026, 8, 14)),  # paid 08-14
            _promise("E-T06", "INV-012", date(2026, 10, 30), date(2026, 11, 15)),  # PENDING
            # Made more than 365 days before as_of: outside the window
            _promise("E-T07", "INV-009", date(2025, 10, 31), date(2025, 11, 5)),
        ),
    )
    books = playbooks_from(facts, grace_days=2, outbound={}, tz=DUBAI)
    assert set(books) == set(facts.customers)
    assert (books["CUST-A"].promise_kept_rate, books["CUST-A"].promises_counted) == (0.75, 4)
    assert (books["CUST-C"].promise_kept_rate, books["CUST-C"].promises_counted) == (None, 2)
    assert (books["CUST-B"].promise_kept_rate, books["CUST-B"].promises_counted) == (None, 0)


def test_reply_pairs_come_from_the_customers_messages(make_agent_facts) -> None:
    from receivables.agent.facts import MessageHeader

    customer = "CUST-A"
    sends = [at(date(2026, 10, d)) for d in (1, 10, 20)]
    messages = tuple(
        MessageHeader(message_id=f"M-T{n}", invoice_number=None, customer_id=customer,
                      direction="inbound", sent_at=(s + timedelta(days=2)).isoformat(),
                      language="en")
        for n, s in enumerate(sends)
    )  # fmt: skip
    facts = make_agent_facts(messages=messages)
    books = playbooks_from(facts, grace_days=2, outbound={customer: sends}, tz=DUBAI)
    assert (books[customer].avg_days_to_reply, books[customer].followup_gap_days) == (2.0, 4)
    assert books["CUST-B"].followup_gap_days == 7


@pytest.mark.spec("T-LRN-03")
def test_the_rule_planner_schedules_the_playbook_gap(make_agent_facts) -> None:
    default = make_agent_facts()
    invoice = "INV-003"  # 1 to 30 days overdue: the table says 7 days
    steps = plan_case(default, invoice).steps
    assert steps[-1].tool == "schedule_followup" and steps[-1].args["days"] == 7
    customer = default.rows[invoice].customer_id
    book = PlaybookRecord(customer_id=customer, promise_kept_rate=None, promises_counted=0,
                          avg_days_to_reply=6.0, replies_counted=3, followup_gap_days=12,
                          updated_at="2026-10-31T06:00:00Z")  # fmt: skip
    learned = make_agent_facts(playbooks=(book,))
    steps = plan_case(learned, invoice).steps
    assert steps[-1].tool == "schedule_followup" and steps[-1].args["days"] == 12


def test_the_allowlist_suggestion_is_only_proposed() -> None:
    good = build_playbook("CUST-A", kept=4, broken=1, delays=())  # 0.8 over 5
    assert "CUST-A" in (allowlist_suggestion(good, ()) or "")
    assert "never changes" in (allowlist_suggestion(good, ()) or "")
    assert allowlist_suggestion(good, ("CUST-A",)) is None  # already allowlisted
    assert allowlist_suggestion(build_playbook("CUST-A", kept=3, broken=1, delays=()), ()) is None
    assert allowlist_suggestion(build_playbook("CUST-A", kept=3, broken=2, delays=()), ()) is None


def test_the_playbook_module_is_pure() -> None:
    tree = ast.parse((ROOT / "src/receivables/agent/playbook.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules <= {"collections.abc", "datetime", "fractions", "receivables.agent.facts",
                       "receivables.agent.outcomes", "receivables.core.scoring"}, modules  # fmt: skip
