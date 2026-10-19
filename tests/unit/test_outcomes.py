"""Outcome inference (SPEC §18.7; S4.04; D-051): promises KEPT, BROKEN or PENDING with a 2-day
grace, and PAYMENT_RECEIVED and DISPUTE_RESOLVED outcomes, from facts as of ``as_of``."""

import ast
from datetime import date
from pathlib import Path

import pytest

from receivables.agent.outcomes import (
    BROKEN,
    KEPT,
    PENDING,
    infer_outcomes,
    promise_outcome,
    promise_verdicts,
)
from receivables.core.ledger import PaymentRecord
from receivables.core.scoring import EventRecord

ROOT = Path(__file__).resolve().parents[2]
GRACE = 2
P = date(2026, 10, 20)  # INV-008's golden promise date (E-002), amount 12,000


def pay(day: date, amount: int = 1_200_000, pid: str = "P-T01") -> PaymentRecord:
    return PaymentRecord(
        payment_id=pid, invoice_number="INV-008", customer_id=None, payment_date=day,
        amount=amount,
    )  # fmt: skip


@pytest.mark.spec("T-OUT-01")
@pytest.mark.parametrize(
    ("as_of", "paid_on", "expected"),
    [
        (date(2026, 10, 21), None, PENDING),  # unpaid at 2026-10-21
        (date(2026, 10, 31), None, BROKEN),  # unpaid at 2026-10-31
        (date(2026, 10, 31), date(2026, 10, 22), KEPT),  # paid in full on 2026-10-22
        (date(2026, 10, 31), date(2026, 10, 23), BROKEN),  # paid on 2026-10-23
    ],
)
def test_t_out_01_on_the_golden_inv_008(
    make_agent_facts, as_of: date, paid_on: date | None, expected: str
) -> None:
    payments = () if paid_on is None else (pay(paid_on),)
    facts = make_agent_facts(as_of=as_of, extra_payments=payments)
    assert facts.rows["INV-008"].amount == 1_200_000
    assert promise_verdicts(facts, GRACE) == {"E-002": ("INV-008", expected)}


@pytest.mark.spec("T-OUT-01")
@pytest.mark.parametrize(
    ("settled", "as_of", "expected"),
    [
        (None, date(2026, 10, 21), PENDING),
        (None, date(2026, 10, 22), PENDING),  # P + 2 itself is still within the grace
        (None, date(2026, 10, 23), BROKEN),  # as_of > P + 2
        (date(2026, 10, 22), date(2026, 10, 31), KEPT),
        (date(2026, 10, 23), date(2026, 10, 31), BROKEN),  # a late payment stays BROKEN
        (date(2026, 10, 1), date(2026, 10, 31), KEPT),  # settled before the promise date
        (date(2026, 10, 21), date(2026, 10, 21), KEPT),
    ],
)
def test_promise_outcome(settled: date | None, as_of: date, expected: str) -> None:
    assert promise_outcome(P, settled, as_of, GRACE) == expected


def test_the_grace_comes_from_config() -> None:
    assert promise_outcome(P, None, date(2026, 10, 21), 0) == BROKEN
    assert promise_outcome(P, date(2026, 10, 25), date(2026, 10, 31), 5) == KEPT


def test_a_payment_after_as_of_is_not_seen_yet(make_agent_facts) -> None:
    facts = make_agent_facts(as_of=date(2026, 10, 21), extra_payments=(pay(P),))
    assert promise_verdicts(facts, GRACE) == {"E-002": ("INV-008", KEPT)}
    later = make_agent_facts(as_of=date(2026, 10, 19), extra_payments=(pay(P),))
    assert promise_verdicts(later, GRACE) == {"E-002": ("INV-008", PENDING)}


def test_a_partial_payment_does_not_keep_a_promise(make_agent_facts) -> None:
    facts = make_agent_facts(extra_payments=(pay(date(2026, 10, 20), amount=600_000),))
    assert promise_verdicts(facts, GRACE) == {"E-002": ("INV-008", BROKEN)}


def promise(event_id: str, made: date, due: date, invoice: str = "INV-008") -> EventRecord:
    return EventRecord(
        event_id=event_id, invoice_number=invoice, event_type="PROMISE_TO_PAY", event_date=made,
        promise_date=due,
    )  # fmt: skip


def test_a_promise_replaced_within_its_grace_is_superseded(make_agent_facts) -> None:
    # E-002 promises 10-20; on 10-19 the customer promises 10-30 instead and pays on 10-29.
    facts = make_agent_facts(
        extra_events=(promise("E-T01", date(2026, 10, 19), date(2026, 10, 30)),),
        extra_payments=(pay(date(2026, 10, 29)),),
    )
    assert promise_verdicts(facts, GRACE) == {"E-T01": ("INV-008", KEPT)}


def test_a_promise_replaced_on_the_last_day_of_its_grace_is_superseded(make_agent_facts) -> None:
    facts = make_agent_facts(
        extra_events=(promise("E-T01", date(2026, 10, 22), date(2026, 10, 30)),),  # P + 2
    )
    assert promise_verdicts(facts, GRACE) == {"E-T01": ("INV-008", PENDING)}  # E-002 replaced


def test_a_new_promise_after_the_grace_does_not_rescue_the_old_one(make_agent_facts) -> None:
    facts = make_agent_facts(
        extra_events=(promise("E-T01", date(2026, 10, 23), date(2026, 10, 30)),),
        extra_payments=(pay(date(2026, 10, 29)),),
    )
    assert promise_verdicts(facts, GRACE) == {
        "E-002": ("INV-008", BROKEN),
        "E-T01": ("INV-008", KEPT),
    }


def test_a_promise_dated_after_as_of_is_not_known_yet(make_agent_facts) -> None:
    facts = make_agent_facts(as_of=date(2026, 10, 4))  # E-002 is dated 2026-10-05
    assert promise_verdicts(facts, GRACE) == {}


def test_infer_outcomes_gives_only_final_outcomes_with_fixed_ids(make_agent_facts) -> None:
    facts = make_agent_facts(as_of=date(2026, 10, 31))
    outcomes = infer_outcomes(facts, GRACE)
    by_id = {o.outcome_id: o for o in outcomes}
    assert len(by_id) == len(outcomes)
    assert by_id["OUT-PRM-E-002"].outcome_type == "PROMISE_BROKEN"
    assert by_id["OUT-PRM-E-002"].invoice_number == "INV-008"
    received = {o.invoice_number for o in outcomes if o.outcome_type == "PAYMENT_RECEIVED"}
    applied = {p.invoice_number for p in facts.applied_payments if p.payment_type == "payment"}
    assert received == applied and received  # one per payment applied to an invoice
    assert all(o.outcome_id.startswith("OUT-PAY-") for o in outcomes
               if o.outcome_type == "PAYMENT_RECEIVED")  # fmt: skip
    pending = infer_outcomes(make_agent_facts(as_of=date(2026, 10, 21)), GRACE)
    assert "OUT-PRM-E-002" not in {o.outcome_id for o in pending}  # PENDING records nothing


def test_resolved_disputes_and_payments_produce_outcomes(make_agent_facts) -> None:
    resolved = EventRecord(
        event_id="E-T02", invoice_number="INV-004", event_type="DISPUTE_RESOLVED",
        event_date=date(2026, 10, 25),
    )  # fmt: skip
    facts = make_agent_facts(extra_events=(resolved,), extra_payments=(pay(date(2026, 10, 22)),))
    by_id = {o.outcome_id: o for o in infer_outcomes(facts, GRACE)}
    assert by_id["OUT-DSP-E-T02"].outcome_type == "DISPUTE_RESOLVED"
    assert by_id["OUT-DSP-E-T02"].invoice_number == "INV-004"
    assert by_id["OUT-PAY-P-T01"].outcome_type == "PAYMENT_RECEIVED"
    assert by_id["OUT-PRM-E-002"].outcome_type == "PROMISE_KEPT"


def test_credit_notes_and_write_offs_are_not_payments_received(make_agent_facts) -> None:
    credit = PaymentRecord(
        payment_id="CN-T01", invoice_number="INV-008", customer_id=None,
        payment_date=date(2026, 10, 22), amount=100_000, payment_type="credit_note",
    )  # fmt: skip
    ids = {o.outcome_id for o in infer_outcomes(make_agent_facts(extra_payments=(credit,)), GRACE)}
    assert "OUT-PAY-CN-T01" not in ids


def test_the_outcome_module_is_pure() -> None:
    tree = ast.parse((ROOT / "src/receivables/agent/outcomes.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules <= {"collections.abc", "dataclasses", "datetime", "receivables.agent.facts",
                       "receivables.core.money"}, modules  # fmt: skip
