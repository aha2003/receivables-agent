"""Customer history (SPEC §6.3, §2 "Late"): T-HIS-01 plus window and grace edges."""

from dataclasses import replace
from datetime import date, timedelta
from fractions import Fraction

import pytest

from receivables.core.config import EngineConfig
from receivables.core.history import CustomerHistory, customer_history
from receivables.core.ledger import InvoiceRecord, PaymentRecord, build_ledger

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)


@pytest.mark.spec("T-HIS-01")
class TestHis01:
    def test_golden_history(self, golden_ledger_inputs: Inputs) -> None:
        result = customer_history(
            build_ledger(*golden_ledger_inputs, as_of=AS_OF), AS_OF, EngineConfig()
        )
        assert result.customers == {
            "CUST-A": CustomerHistory(paid=2, late=2),
            "CUST-C": CustomerHistory(paid=1, late=0),
        }
        assert "CUST-B" not in result.customers
        assert result.customers["CUST-A"].late_rate == Fraction(1)
        assert result.customers["CUST-C"].late_rate == Fraction(0)
        assert result.portfolio_late_rate == Fraction(2, 3)


def _settled(due: date, paid_on: date, customer: str = "CUST-T", n: int = 1) -> Inputs:
    inv = InvoiceRecord(
        invoice_number=f"INV-{n}",
        customer_id=customer,
        invoice_date=due - timedelta(days=30),
        due_date=due,
        amount=1000,
    )
    pay = PaymentRecord(
        payment_id=f"P-{n}",
        invoice_number=f"INV-{n}",
        customer_id=None,
        payment_date=paid_on,
        amount=1000,
        payment_type="payment",
    )
    return [inv], [pay]


def _history(inputs: Inputs, config: EngineConfig | None = None):
    return customer_history(build_ledger(*inputs, as_of=AS_OF), AS_OF, config or EngineConfig())


def test_no_settled_invoices_gives_no_portfolio_rate() -> None:
    result = customer_history([], AS_OF, EngineConfig())
    assert result.customers == {}
    assert result.portfolio_late_rate is None


def test_paid_on_due_date_is_not_late() -> None:
    result = _history(_settled(date(2026, 10, 1), date(2026, 10, 1)))
    assert result.customers["CUST-T"] == CustomerHistory(paid=1, late=0)


def test_paid_one_day_after_due_is_late() -> None:
    result = _history(_settled(date(2026, 10, 1), date(2026, 10, 2)))
    assert result.customers["CUST-T"] == CustomerHistory(paid=1, late=1)


def test_grace_days_boundary() -> None:
    config = replace(EngineConfig(), grace_days=3)
    on_grace = _history(_settled(date(2026, 10, 1), date(2026, 10, 4)), config)
    past_grace = _history(_settled(date(2026, 10, 1), date(2026, 10, 5)), config)
    assert on_grace.customers["CUST-T"].late == 0
    assert past_grace.customers["CUST-T"].late == 1


def test_history_window_is_inclusive() -> None:
    window_start = AS_OF - timedelta(days=365)
    inside = _history(_settled(window_start - timedelta(days=5), window_start))
    outside = _history(_settled(window_start - timedelta(days=5), window_start - timedelta(days=1)))
    assert inside.customers["CUST-T"].paid == 1
    assert outside.customers == {}


def test_write_off_does_not_count_as_paid() -> None:
    invoices, _ = _settled(date(2026, 10, 1), date(2026, 10, 1))
    write_off = PaymentRecord(
        payment_id="W-1",
        invoice_number="INV-1",
        customer_id=None,
        payment_date=date(2026, 10, 1),
        amount=1000,
        payment_type="write_off",
    )
    assert _history((invoices, [write_off])).customers == {}
