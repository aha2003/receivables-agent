"""The four §19.2 customer archetypes (SPEC §19.2; S4.02; D-048).

Every rule is per invoice (D-048 option 1a): "the first reminder" is the first outbound message
about that invoice. Replies come 2 days after it (option 2a). A claims-paid customer claims only
the invoice its unallocated cash matches; its other invoices pay 45 days after due (option 3a).
"""

import ast
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from receivables.sim.behaviour import (
    CustomerBehaviour,
    CustomerPayment,
    CustomerReply,
    behaviours_for,
)
from receivables.sim.company import SimCompany, SimInvoice, generate_company
from receivables.sim.scenario import Archetype, load_scenario

COMPANY = generate_company(load_scenario())
START = load_scenario().start_date
END = START + timedelta(days=200)  # long enough for every fallback to fall due
INVOICE_REGEX = r"\b[A-Z]{2,6}-\d{2,}\b"  # config.yaml drafts.invoice_number_regex


def customer_no(n: int) -> str:
    return f"SIM-C{n:02d}"


def invoices_of(company: SimCompany, customer_id: str) -> list[SimInvoice]:
    return [i for i in company.invoices if i.customer_id == customer_id]


def run(behaviour: CustomerBehaviour, events: dict[date, list[tuple[str, str]]] | None = None):
    """Ask the behaviour about every day from START to END, applying ``events`` (kind, invoice)
    after the customer's own actions on their day, as the agent's tick would."""
    actions: list[CustomerReply | CustomerPayment] = []
    events = events or {}
    day = START
    while day <= END:
        actions.extend(behaviour.actions_on(day))
        for kind, invoice in events.get(day, []):
            getattr(behaviour, kind)(invoice, day)
        day += timedelta(days=1)
    return actions


def payments(actions: list[CustomerReply | CustomerPayment]) -> dict[str, CustomerPayment]:
    out = [a for a in actions if isinstance(a, CustomerPayment)]
    assert len({p.invoice_number for p in out}) == len(out), "an invoice was paid twice"
    return {p.invoice_number: p for p in out}


def replies(actions: list[CustomerReply | CustomerPayment]) -> list[CustomerReply]:
    return [a for a in actions if isinstance(a, CustomerReply)]


def behaviour(n: int) -> CustomerBehaviour:
    return behaviours_for(COMPANY)[customer_no(n)]


def test_one_behaviour_per_customer_with_its_archetype() -> None:
    all_ = behaviours_for(COMPANY)
    assert list(all_) == [c.customer_id for c in COMPANY.customers]
    assert [b.archetype for b in all_.values()] == [c.archetype for c in COMPANY.customers]


# Responsive -------------------------------------------------------------------------------


@pytest.mark.parametrize(("n", "delay"), [(1, 2), (2, 3), (3, 4)])
def test_responsive_pays_in_full_2_3_or_4_days_after_the_first_reminder(n: int, delay: int) -> None:
    first = invoices_of(COMPANY, customer_no(n))[0]
    reminder = first.due_date + timedelta(days=1)
    again = reminder + timedelta(days=1)  # a second reminder changes nothing
    actions = run(behaviour(n), {reminder: [("message_sent", first.invoice_number)],
                                 again: [("message_sent", first.invoice_number)]})  # fmt: skip
    paid = payments(actions)[first.invoice_number]
    assert paid.day == reminder + timedelta(days=delay)
    assert paid.amount_fils == first.amount_fils and paid.customer_id == customer_no(n)
    assert replies(actions) == []  # a responsive customer pays; it does not write


def test_responsive_without_a_reminder_pays_25_days_after_due() -> None:
    paid = payments(run(behaviour(1)))
    for inv in invoices_of(COMPANY, customer_no(1)):
        assert paid[inv.invoice_number].day == inv.due_date + timedelta(days=25)
        assert paid[inv.invoice_number].amount_fils == inv.amount_fils


def test_a_reminder_is_per_invoice() -> None:
    first, second, *_ = invoices_of(COMPANY, customer_no(1))
    reminder = first.due_date + timedelta(days=1)
    paid = payments(run(behaviour(1), {reminder: [("message_sent", first.invoice_number)]}))
    assert paid[first.invoice_number].day == reminder + timedelta(days=2)
    assert paid[second.invoice_number].day == second.due_date + timedelta(days=25)


def test_a_late_reminder_does_not_delay_the_25_day_payment() -> None:
    first = invoices_of(COMPANY, customer_no(3))[0]  # delay 4
    reminder = first.due_date + timedelta(days=23)
    paid = payments(run(behaviour(3), {reminder: [("message_sent", first.invoice_number)]}))
    assert paid[first.invoice_number].day == first.due_date + timedelta(days=25)


# Disputer ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "label"), [(4, "PO_AMOUNT_MISMATCH"), (5, "MISSING_DELIVERY_NOTE"), (6, "MISSING_PO")]
)
def test_disputer_replies_2_days_after_the_first_reminder_with_its_label(
    n: int, label: str
) -> None:
    first = invoices_of(COMPANY, customer_no(n))[0]
    reminder = first.due_date + timedelta(days=1)
    actions = run(behaviour(n), {reminder: [("message_sent", first.invoice_number)],
                                 reminder + timedelta(days=1):
                                     [("message_sent", first.invoice_number)]})  # fmt: skip
    (reply,) = replies(actions)
    assert reply.day == reminder + timedelta(days=2)
    assert (reply.invoice_number, reply.label, reply.customer_id) == (
        first.invoice_number, label, customer_no(n),
    )  # fmt: skip
    assert first.invoice_number in reply.text


def test_disputer_pays_3_days_after_the_resolution_and_a_response() -> None:
    first = invoices_of(COMPANY, customer_no(4))[0]
    reminder = first.due_date + timedelta(days=1)
    resolved = reminder + timedelta(days=5)
    response = resolved + timedelta(days=2)
    paid = payments(run(behaviour(4), {
        reminder: [("message_sent", first.invoice_number)],
        resolved - timedelta(days=1): [("message_sent", first.invoice_number)],  # too early
        resolved: [("dispute_resolved", first.invoice_number)],
        response: [("message_sent", first.invoice_number)],
    }))  # fmt: skip
    assert paid[first.invoice_number].day == response + timedelta(days=3)


def test_a_response_on_the_resolution_day_counts() -> None:
    first = invoices_of(COMPANY, customer_no(5))[0]
    resolved = first.due_date + timedelta(days=6)
    paid = payments(run(behaviour(5), {
        first.due_date + timedelta(days=1): [("message_sent", first.invoice_number)],
        resolved: [("dispute_resolved", first.invoice_number),
                   ("message_sent", first.invoice_number)],
    }))  # fmt: skip
    assert paid[first.invoice_number].day == resolved + timedelta(days=3)


def test_a_resolution_without_a_response_is_not_enough() -> None:
    first = invoices_of(COMPANY, customer_no(4))[0]
    paid = payments(run(behaviour(4), {
        first.due_date + timedelta(days=1): [("message_sent", first.invoice_number)],
        first.due_date + timedelta(days=5): [("dispute_resolved", first.invoice_number)],
    }))  # fmt: skip
    assert paid[first.invoice_number].day == first.due_date + timedelta(days=60)


def test_disputer_never_resolved_or_never_contacted_pays_60_days_after_due() -> None:
    first = invoices_of(COMPANY, customer_no(6))[0]
    contacted = run(behaviour(6), {first.due_date: [("message_sent", first.invoice_number)]})
    assert payments(contacted)[first.invoice_number].day == first.due_date + timedelta(days=60)
    untouched = payments(run(behaviour(6)))
    for inv in invoices_of(COMPANY, customer_no(6)):
        assert untouched[inv.invoice_number].day == inv.due_date + timedelta(days=60)


def test_a_disputer_does_not_wait_past_60_days_for_a_resolution() -> None:
    first = invoices_of(COMPANY, customer_no(4))[0]
    late = first.due_date + timedelta(days=59)
    paid = payments(run(behaviour(4), {
        first.due_date + timedelta(days=1): [("message_sent", first.invoice_number)],
        late: [("dispute_resolved", first.invoice_number), ("message_sent", first.invoice_number)],
    }))  # fmt: skip
    assert paid[first.invoice_number].day == first.due_date + timedelta(days=60)


# Silent -----------------------------------------------------------------------------------


def test_silent_never_replies_and_pays_35_days_after_due() -> None:
    first = invoices_of(COMPANY, customer_no(7))[0]
    actions = run(behaviour(7), {first.due_date + timedelta(days=d): [
        ("message_sent", first.invoice_number)] for d in (1, 8, 15)})  # fmt: skip
    assert replies(actions) == []
    paid = payments(actions)
    for inv in invoices_of(COMPANY, customer_no(7)):
        assert paid[inv.invoice_number].day == inv.due_date + timedelta(days=35)


def test_silent_pays_10_days_after_escalation_when_that_is_sooner() -> None:
    first, second, *_ = invoices_of(COMPANY, customer_no(8))
    soon = first.due_date + timedelta(days=20)  # +10 = day 30 < day 35
    late = second.due_date + timedelta(days=30)  # +10 = day 40 > day 35
    paid = payments(run(behaviour(8), {soon: [("escalated", first.invoice_number)],
                                       late: [("escalated", second.invoice_number)]}))  # fmt: skip
    assert paid[first.invoice_number].day == soon + timedelta(days=10)
    assert paid[second.invoice_number].day == second.due_date + timedelta(days=35)


# Claims paid ------------------------------------------------------------------------------


def claimed(n: int) -> SimInvoice:
    """The invoice the customer's unallocated cash matches (S4.01: its oldest)."""
    (cash,) = [p for p in COMPANY.payments if p.customer_id == customer_no(n)]
    oldest = invoices_of(COMPANY, customer_no(n))[0]
    assert cash.amount_fils == oldest.amount_fils
    return oldest


@pytest.mark.parametrize("n", [10, 11, 12])
def test_claims_paid_replies_already_paid_and_closes_on_the_allocation(n: int) -> None:
    inv = claimed(n)
    reminder = inv.due_date + timedelta(days=1)
    confirmed = reminder + timedelta(days=4)
    actions = run(behaviour(n), {
        reminder: [("message_sent", inv.invoice_number)],
        confirmed: [("allocation_confirmed", inv.invoice_number)],
    })  # fmt: skip
    (reply,) = replies(actions)
    assert reply.day == reminder + timedelta(days=2) and reply.label == "ALREADY_PAID"
    assert inv.invoice_number in reply.text and "2026-11-02" in reply.text
    # The cash already received closes it once allocated: no second payment, ever.
    assert inv.invoice_number not in payments(actions)


def test_claims_paid_contacted_but_never_allocated_does_not_pay_twice() -> None:
    inv = claimed(10)
    actions = run(behaviour(10), {inv.due_date: [("message_sent", inv.invoice_number)]})
    assert inv.invoice_number not in payments(actions)  # it believes it has paid


def test_claims_paid_without_contact_pays_45_days_after_due() -> None:
    paid = payments(run(behaviour(11)))
    for inv in invoices_of(COMPANY, customer_no(11)):
        assert paid[inv.invoice_number].day == inv.due_date + timedelta(days=45)


def test_claims_paid_other_invoices_pay_45_days_after_due_contacted_or_not() -> None:
    inv = claimed(12)
    others = invoices_of(COMPANY, customer_no(12))[1:]
    events = {o.due_date + timedelta(days=1): [("message_sent", o.invoice_number)] for o in others}
    actions = run(behaviour(12), events)
    assert replies(actions) == []  # only the claimed invoice gets an ALREADY_PAID reply
    paid = payments(actions)
    for o in others:
        assert paid[o.invoice_number].day == o.due_date + timedelta(days=45)
    assert paid[inv.invoice_number].day == inv.due_date + timedelta(days=45)  # never contacted


def test_an_allocation_before_any_contact_also_closes_it() -> None:
    inv = claimed(10)
    actions = run(behaviour(10), {START: [("allocation_confirmed", inv.invoice_number)],
                                  inv.due_date + timedelta(days=1):
                                      [("message_sent", inv.invoice_number)]})  # fmt: skip
    assert inv.invoice_number not in payments(actions) and replies(actions) == []


# Across archetypes --------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 4, 7, 10])
def test_nothing_happens_once_an_invoice_is_paid(n: int) -> None:
    inv = invoices_of(COMPANY, customer_no(n))[1]  # not the claimed one
    no_contact = payments(run(behaviour(n)))[inv.invoice_number].day
    after = no_contact + timedelta(days=1)
    actions = run(behaviour(n), {after: [("message_sent", inv.invoice_number),
                                         ("escalated", inv.invoice_number),
                                         ("dispute_resolved", inv.invoice_number)]})  # fmt: skip
    assert payments(actions)[inv.invoice_number].day == no_contact
    assert [r for r in replies(actions) if r.invoice_number == inv.invoice_number] == []


def test_every_action_is_dated_the_day_it_is_returned_and_ids_are_unique() -> None:
    seen: set[str] = set()
    for b in behaviours_for(COMPANY).values():
        day = START
        while day <= END:
            for action in b.actions_on(day):
                assert action.day == day
                key = (
                    action.payment_id if isinstance(action, CustomerPayment) else action.message_id
                )
                assert key not in seen
                seen.add(key)
            day += timedelta(days=1)
    assert len(seen) == 60  # without contact every invoice is paid once, no one writes


def test_identifiers_never_look_like_invoice_numbers() -> None:
    first = invoices_of(COMPANY, customer_no(4))[0]
    actions = run(behaviour(4), {first.due_date: [("message_sent", first.invoice_number)]})
    for action in actions:
        key = action.payment_id if isinstance(action, CustomerPayment) else action.message_id
        assert not re.search(INVOICE_REGEX, key), key


def test_the_same_inputs_give_the_same_actions() -> None:
    first = invoices_of(COMPANY, customer_no(5))[0]
    events = {first.due_date: [("message_sent", first.invoice_number)]}
    assert run(behaviours_for(COMPANY)[customer_no(5)], events) == run(
        behaviours_for(generate_company(load_scenario()))[customer_no(5)], events
    )


def test_days_must_move_forwards_and_events_cannot_be_backdated() -> None:
    b = behaviour(1)
    first = invoices_of(COMPANY, customer_no(1))[0]
    b.actions_on(START + timedelta(days=5))
    with pytest.raises(ValueError, match="earlier day"):
        b.actions_on(START + timedelta(days=4))
    with pytest.raises(ValueError, match="earlier day"):
        b.message_sent(first.invoice_number, START + timedelta(days=4))
    with pytest.raises(ValueError, match="before its events"):
        b.message_sent(first.invoice_number, START + timedelta(days=6))  # not asked yet
    b.message_sent(first.invoice_number, START + timedelta(days=5))  # the day asked is fine


def test_an_event_needs_its_day_asked_first() -> None:
    first = invoices_of(COMPANY, customer_no(1))[0]
    with pytest.raises(ValueError, match="before its events"):
        behaviour(1).message_sent(first.invoice_number, START)


def test_an_unknown_or_another_customers_invoice_is_refused() -> None:
    b = behaviour(1)
    other = invoices_of(COMPANY, customer_no(2))[0]
    for invoice in ("SIM-999", other.invoice_number):
        with pytest.raises(ValueError, match="not an invoice of SIM-C01"):
            b.message_sent(invoice, START)


def test_skipped_days_lose_no_action() -> None:
    every_day = run(behaviour(2))
    b = behaviour(2)
    weekly = []
    day = START
    while day <= END:
        weekly.extend(b.actions_on(day))
        day += timedelta(days=7)
    weekly.extend(b.actions_on(END))
    assert sorted(weekly, key=lambda a: a.day) == every_day  # each keeps its own date


def test_the_engine_is_pure() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/receivables/sim/behaviour.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {
        "collections.abc", "dataclasses", "datetime", "typing", "receivables.core.money",
        "receivables.sim.company", "receivables.sim.scenario",
    }  # fmt: skip
    assert imported <= allowed, imported - allowed  # no clock, randomness, I/O or adapters
    assert Archetype.CLAIMS_PAID in {b.archetype for b in behaviours_for(COMPANY).values()}
