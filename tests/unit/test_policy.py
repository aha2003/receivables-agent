"""Policy engine (SPEC §18.4, §18.5): T-POL-01 to T-POL-11, edges and no short-circuit.

Facts are the golden data at as_of 2026-10-31; ``now_local`` defaults to 2026-11-02T10:00+04:00
(a Monday); limits are the §18.4 defaults from the config loader; no prior outbound messages
unless stated. The tone comes from the queue's age rule (§6.5).
"""

import dataclasses
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.adapters.clock import DUBAI
from receivables.adapters.config import L2Section, load_config
from receivables.agent.facts import AgentFacts, MessageHeader
from receivables.agent.policy import (
    REASON_CODES,
    PolicyDecision,
    SendDraft,
    SendFacts,
    evaluate_send,
    send_facts_from,
)
from receivables.core.ledger import PaymentRecord
from receivables.core.money import to_fils
from receivables.core.queue import draft_tone

MakeFacts = Callable[..., AgentFacts]  # the make_agent_facts fixture (tests/conftest.py)
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI)  # Monday
DEFAULTS: L2Section = load_config(Path("/nonexistent/config.yaml")).agent.l2


def limits(**changes: object) -> L2Section:
    return dataclasses.replace(DEFAULTS, **changes)  # type: ignore[arg-type]


def draft_for(facts: AgentFacts, *numbers: str, **changes: object) -> SendDraft:
    row = facts.rows[numbers[0]]
    customer = facts.customers[row.customer_id]
    assert row.days_overdue is not None and customer.contact_email is not None
    draft = SendDraft(
        invoice_numbers=numbers,
        tone=draft_tone(max(facts.rows[n].days_overdue or 0 for n in numbers)),
        recipient=customer.contact_email,
        violations=(),
    )
    return dataclasses.replace(draft, **changes)  # type: ignore[arg-type]


def decide(
    facts: AgentFacts,
    *numbers: str,
    allow: tuple[str, ...],
    now: datetime = NOW,
    sends_today: int = 0,
    outbound_dates: tuple[date, ...] | None = (),
    draft_changes: dict[str, object] | None = None,
    **limit_changes: object,
) -> PolicyDecision:
    case = send_facts_from(facts, numbers, sends_today=sends_today, outbound_dates=outbound_dates)
    draft = draft_for(facts, *numbers, **(draft_changes or {}))
    return evaluate_send(case, draft, now, limits(allowlisted_customers=allow, **limit_changes))


@pytest.fixture
def facts(make_agent_facts: MakeFacts) -> AgentFacts:
    return make_agent_facts()


def test_defaults_are_the_spec_values() -> None:
    assert DEFAULTS.allowlisted_customers == ()
    assert DEFAULTS.max_total_open_fils == to_fils("25000")
    assert (DEFAULTS.min_days_between_contacts, DEFAULTS.daily_send_cap) == (7, 20)
    assert DEFAULTS.business_days == ("Mon", "Tue", "Wed", "Thu", "Fri")
    assert DEFAULTS.business_hours == (time(9), time(17))
    assert (DEFAULTS.timezone, DEFAULTS.kill_switch) == ("Asia/Dubai", False)


# --- T-POL-01 to T-POL-11 -------------------------------------------------------------------


def test_t_pol_01_allowed(facts: AgentFacts) -> None:
    assert facts.rows["INV-003"].days_overdue == 30
    assert decide(facts, "INV-003", allow=("CUST-A",)) == PolicyDecision(True, ())


def test_t_pol_02_three_reasons_sorted(facts: AgentFacts) -> None:
    decision = decide(facts, "INV-004", allow=("CUST-B",))
    assert decision == PolicyDecision(
        False, ("POLICY_AMOUNT_LIMIT", "POLICY_DISPUTE_ACTIVE", "POLICY_TONE_NOT_ALLOWED")
    )


def test_t_pol_03_tone_two(facts: AgentFacts) -> None:
    assert draft_for(facts, "INV-013").tone == 2
    assert decide(facts, "INV-013", allow=("CUST-A",)).reasons == ("POLICY_TONE_NOT_ALLOWED",)


def test_t_pol_04_empty_allowlist(facts: AgentFacts) -> None:
    assert decide(facts, "INV-012", allow=()).reasons == ("POLICY_NOT_ALLOWLISTED",)


def test_t_pol_05_unallocated_cash(make_agent_facts: MakeFacts) -> None:
    p5 = PaymentRecord(
        payment_id="P-005",
        invoice_number=None,
        customer_id="CUST-C",
        payment_date=date(2026, 10, 28),
        amount=to_fils("12000.00"),
    )
    facts = make_agent_facts(extra_payments=(p5,))
    assert decide(facts, "INV-012", allow=("CUST-C",)).reasons == ("POLICY_UNALLOCATED_CASH",)


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (datetime(2026, 11, 1, 10, 0, tzinfo=DUBAI), False),  # Sunday
        (datetime(2026, 11, 2, 8, 59, tzinfo=DUBAI), False),
        (datetime(2026, 11, 2, 17, 0, tzinfo=DUBAI), False),
        (datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI), True),
    ],
)
def test_t_pol_06_business_hours(facts: AgentFacts, now: datetime, allowed: bool) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), now=now)
    assert decision == (
        PolicyDecision(True, ()) if allowed else PolicyDecision(False, ("POLICY_OUTSIDE_HOURS",))
    )


@pytest.mark.parametrize(
    ("last_outbound", "reasons"),
    [(date(2026, 10, 27), ("POLICY_RECENT_CONTACT",)), (date(2026, 10, 26), ())],
)
def test_t_pol_07_recent_contact(
    facts: AgentFacts, last_outbound: date, reasons: tuple[str, ...]
) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), outbound_dates=(last_outbound,))
    assert decision.reasons == reasons


def test_t_pol_08_daily_cap(facts: AgentFacts) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), sends_today=2, daily_send_cap=2)
    assert decision.reasons == ("POLICY_DAILY_CAP",)


def test_t_pol_09_kill_switch(facts: AgentFacts) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), kill_switch=True)
    assert decision.reasons == ("POLICY_KILL_SWITCH",)


def test_t_pol_10_validation_failed(facts: AgentFacts) -> None:
    # The draft validator is S2.05; here the draft carries its amount violation (D-021).
    decision = decide(
        facts, "INV-003", allow=("CUST-A",), draft_changes={"violations": ("UNKNOWN_AMOUNT",)}
    )
    assert decision.reasons == ("POLICY_VALIDATION_FAILED",)


def test_t_pol_11_recipient(facts: AgentFacts) -> None:
    decision = decide(
        facts,
        "INV-003",
        allow=("CUST-A",),
        draft_changes={"recipient": "attacker@evil.example"},
    )
    assert decision.reasons == ("POLICY_RECIPIENT_NOT_ALLOWED",)


# --- edges ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "reasons"),
    [("15000.00", ()), ("14999.99", ("POLICY_AMOUNT_LIMIT",))],
)
def test_amount_limit_is_exceeded_only_above_it(
    facts: AgentFacts, limit: str, reasons: tuple[str, ...]
) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), max_total_open_fils=to_fils(limit))
    assert decision.reasons == reasons


def test_amount_limit_sums_all_invoices_in_the_message(facts: AgentFacts) -> None:
    # INV-003 15,000 + INV-013 4,000 = 19,000: over a 18,999.99 limit, within 19,000.00.
    for limit, reasons in (("19000.00", ()), ("18999.99", ("POLICY_AMOUNT_LIMIT",))):
        decision = decide(
            facts,
            "INV-003",
            "INV-013",
            allow=("CUST-A",),
            max_total_open_fils=to_fils(limit),
            draft_changes={"tone": 1},
        )
        assert decision.reasons == reasons


@pytest.mark.parametrize(
    ("sends", "reasons"), [(1, ()), (2, ("POLICY_DAILY_CAP",)), (5, ("POLICY_DAILY_CAP",))]
)
def test_daily_cap_boundary(facts: AgentFacts, sends: int, reasons: tuple[str, ...]) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), sends_today=sends, daily_send_cap=2)
    assert decision.reasons == reasons


@pytest.mark.parametrize(
    "outbound",
    [date(2026, 11, 2), date(2026, 11, 5)],  # same day, and a date after now_local
)
def test_same_day_or_later_outbound_is_recent(facts: AgentFacts, outbound: date) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), outbound_dates=(outbound,))
    assert decision.reasons == ("POLICY_RECENT_CONTACT",)


@pytest.mark.parametrize(
    "recipient",
    ["AP@alpha.example", "ap@alpha.example ", " ap@alpha.example", "ap@alpha.example.evil"],
)
def test_recipient_must_match_exactly(facts: AgentFacts, recipient: str) -> None:
    assert facts.customers["CUST-A"].contact_email == "ap@alpha.example"
    decision = decide(facts, "INV-003", allow=("CUST-A",), draft_changes={"recipient": recipient})
    assert decision.reasons == ("POLICY_RECIPIENT_NOT_ALLOWED",)


def test_customer_without_contact_email_is_never_a_valid_recipient(facts: AgentFacts) -> None:
    case = dataclasses.replace(
        send_facts_from(facts, ["INV-003"], sends_today=0, outbound_dates=()), contact_email=None
    )
    draft = draft_for(facts, "INV-003")
    decision = evaluate_send(case, draft, NOW, limits(allowlisted_customers=("CUST-A",)))
    assert decision.reasons == ("POLICY_RECIPIENT_NOT_ALLOWED",)


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), True),  # 09:00 Dubai
        (datetime(2026, 11, 2, 4, 59, tzinfo=UTC), False),  # 08:59 Dubai
        (datetime(2026, 11, 2, 13, 0, tzinfo=UTC), False),  # 17:00 Dubai
        (datetime(2026, 11, 1, 22, 0, tzinfo=UTC), False),  # 02:00 Monday Dubai
        (datetime(2026, 11, 6, 9, 0, tzinfo=DUBAI), True),  # Friday
        (datetime(2026, 11, 7, 10, 0, tzinfo=DUBAI), False),  # Saturday
    ],
)
def test_hours_are_judged_in_dubai_time(facts: AgentFacts, now: datetime, allowed: bool) -> None:
    decision = decide(facts, "INV-003", allow=("CUST-A",), now=now)
    assert decision.allowed is allowed
    assert decision.reasons in ((), ("POLICY_OUTSIDE_HOURS",))


def test_recent_contact_uses_the_local_calendar_date(facts: AgentFacts) -> None:
    # 2026-10-26T21:00Z is 01:00 on 27 Oct in Dubai: 6 calendar days before 2 Nov.
    message = MessageHeader(
        message_id="M-OUT",
        invoice_number="INV-003",
        customer_id=None,
        direction="outbound",
        sent_at="2026-10-26T21:00:00Z",
        language="en",
    )
    inbound = dataclasses.replace(
        message, message_id="M-IN", direction="inbound", sent_at="2026-10-31T06:00:00Z"
    )
    other = dataclasses.replace(
        message, message_id="M-OTHER", invoice_number="INV-012", sent_at="2026-10-31T06:00:00Z"
    )
    account_level = dataclasses.replace(
        message,
        message_id="M-ACCOUNT",
        invoice_number=None,
        customer_id="CUST-A",
        sent_at="2026-10-01T06:00:00Z",
    )
    facts = dataclasses.replace(facts, messages=(account_level, message, inbound, other))
    case = send_facts_from(facts, ["INV-003"], sends_today=0)
    assert case.outbound_dates == (date(2026, 10, 1), date(2026, 10, 27))
    decision = evaluate_send(
        case, draft_for(facts, "INV-003"), NOW, limits(allowlisted_customers=("CUST-A",))
    )
    assert decision.reasons == ("POLICY_RECENT_CONTACT",)


def test_naive_now_local_is_refused(facts: AgentFacts) -> None:
    with pytest.raises(ValueError, match="time zone"):
        decide(facts, "INV-003", allow=("CUST-A",), now=datetime(2026, 11, 2, 10, 0))


def test_send_facts_need_one_customer(facts: AgentFacts) -> None:
    with pytest.raises(ValueError, match="one customer"):
        send_facts_from(facts, ["INV-003", "INV-004"], sends_today=0)
    with pytest.raises(KeyError):
        send_facts_from(facts, ["INV-999"], sends_today=0)


def test_send_facts_from_golden_data(facts: AgentFacts) -> None:
    case = send_facts_from(facts, ["INV-004", "INV-011"], sends_today=3, outbound_dates=())
    assert case == SendFacts(
        customer_id="CUST-B",
        contact_email="accounts@beta.example",
        open_amounts={"INV-004": to_fils("50000"), "INV-011": to_fils("25000")},
        disputed=frozenset({"INV-004"}),
        unallocated_cash=0,
        outbound_dates=(),
        sends_today=3,
    )


# --- no short-circuit -------------------------------------------------------------------------

# Each rule paired with a change to an allowed T-POL-01 send that makes only that rule fail.
FAILURES: dict[str, dict[str, object]] = {
    "POLICY_KILL_SWITCH": {"limits": {"kill_switch": True}},
    "POLICY_NOT_ALLOWLISTED": {"limits": {"allowlisted_customers": ()}},
    "POLICY_AMOUNT_LIMIT": {"limits": {"max_total_open_fils": to_fils("100")}},
    "POLICY_TONE_NOT_ALLOWED": {"draft": {"tone": 2}},
    "POLICY_DISPUTE_ACTIVE": {"case": {"disputed": frozenset({"INV-003"})}},
    "POLICY_UNALLOCATED_CASH": {"case": {"unallocated_cash": 1}},
    "POLICY_RECENT_CONTACT": {"case": {"outbound_dates": (date(2026, 11, 1),)}},
    "POLICY_OUTSIDE_HOURS": {"now": NOW + timedelta(hours=8)},
    "POLICY_DAILY_CAP": {"case": {"sends_today": 20}},
    "POLICY_VALIDATION_FAILED": {"draft": {"violations": ("UNKNOWN_INVOICE",)}},
    "POLICY_RECIPIENT_NOT_ALLOWED": {"draft": {"recipient": "x@evil.example"}},
}


def test_every_reason_code_has_a_failure_case() -> None:
    assert set(FAILURES) == set(REASON_CODES) and len(REASON_CODES) == 11
    assert list(REASON_CODES) == sorted(REASON_CODES)


@given(st.sets(st.sampled_from(sorted(FAILURES))))
def test_reasons_are_exactly_the_failing_rules(chosen: set[str]) -> None:
    case = SendFacts(
        customer_id="CUST-A",
        contact_email="ap@alpha.example",
        open_amounts={"INV-003": to_fils("15000")},
        disputed=frozenset(),
        unallocated_cash=0,
        outbound_dates=(),
        sends_today=0,
    )
    draft = SendDraft(
        invoice_numbers=("INV-003",), tone=1, recipient="ap@alpha.example", violations=()
    )
    config, now = limits(allowlisted_customers=("CUST-A",)), NOW
    for code in chosen:
        change = FAILURES[code]
        case = dataclasses.replace(case, **change.get("case", {}))  # type: ignore[arg-type]
        draft = dataclasses.replace(draft, **change.get("draft", {}))  # type: ignore[arg-type]
        config = dataclasses.replace(config, **change.get("limits", {}))  # type: ignore[arg-type]
        moved = change.get("now")
        if isinstance(moved, datetime):
            now = moved
    decision = evaluate_send(case, draft, now, config)
    assert decision.reasons == tuple(sorted(chosen))
    assert decision.allowed is (not chosen)
