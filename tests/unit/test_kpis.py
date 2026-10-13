"""KPIs (SPEC §6.6): T-KPI-01 plus DSO window, overdue percentage and summary."""

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from receivables.core.aging import aging_summary
from receivables.core.config import EngineConfig
from receivables.core.history import customer_history
from receivables.core.kpis import (
    compute_kpis,
    estimated_dso,
    overdue_by_customer,
    overdue_pct,
)
from receivables.core.ledger import InvoiceRecord, PaymentRecord, build_ledger
from receivables.core.money import to_fils
from receivables.core.scoring import EventRecord, score_overdue_invoices

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)


class TestKpi01:
    def test_estimated_dso(
        self, golden_ledger_inputs: Inputs, golden_results: dict[str, Any]
    ) -> None:
        rows = build_ledger(*golden_ledger_inputs, as_of=AS_OF)
        dso = estimated_dso(rows, aging_summary(rows).total_open, AS_OF)
        assert dso.value == Decimal(golden_results["estimated_dso"]["value"]) == Decimal("149.7")
        assert dso.credit_sales_in_window == to_fils(
            golden_results["estimated_dso"]["credit_sales_in_window"]
        )
        assert (dso.window_start, dso.window_end) == (date(2026, 8, 3), AS_OF)

    def test_overdue_by_customer(
        self, golden_ledger_inputs: Inputs, golden_results: dict[str, Any]
    ) -> None:
        result = overdue_by_customer(build_ledger(*golden_ledger_inputs, as_of=AS_OF))
        expected = golden_results["overdue_by_customer"]
        assert [r.customer_id for r in result] == ["CUST-B", "CUST-C", "CUST-A"]
        for got, exp in zip(result, expected, strict=True):
            assert got.customer_id == exp["customer_id"]
            assert got.amount == to_fils(exp["amount"])
            assert abs(got.share - Fraction(Decimal(exp["share_pct"])) / 100) <= Fraction(1, 10_000)
            assert got.share_pct == Decimal(exp["share_pct"])
        assert sum(r.share for r in result) == 1


def _invoice(n: int, on: date, amount: str = "1000.00", due_days: int = 30) -> InvoiceRecord:
    return InvoiceRecord(
        invoice_number=f"INV-{n}",
        customer_id="CUST-T",
        invoice_date=on,
        due_date=on + timedelta(days=due_days),
        amount=to_fils(amount),
    )


def test_dso_window_is_as_of_minus_89_inclusive() -> None:
    inside = _invoice(1, AS_OF - timedelta(days=89))
    outside = _invoice(2, AS_OF - timedelta(days=90))
    rows = build_ledger([inside, outside], [], as_of=AS_OF)
    dso = estimated_dso(rows, aging_summary(rows).total_open, AS_OF)
    assert dso.credit_sales_in_window == to_fils("1000.00")
    assert dso.value == Decimal("180.0")  # 2,000 open / 1,000 sales * 90


def test_dso_counts_closed_invoices_as_sales() -> None:
    inv = _invoice(1, AS_OF - timedelta(days=10))
    paid = PaymentRecord(
        payment_id="P-1",
        invoice_number="INV-1",
        customer_id=None,
        payment_date=AS_OF,
        amount=to_fils("1000.00"),
    )
    rows = build_ledger([inv], [paid], as_of=AS_OF)
    dso = estimated_dso(rows, 0, AS_OF)
    assert dso.credit_sales_in_window == to_fils("1000.00")
    assert dso.value == Decimal("0.0")


def test_dso_none_without_sales_in_window() -> None:
    rows = build_ledger([_invoice(1, AS_OF - timedelta(days=200))], [], as_of=AS_OF)
    dso = estimated_dso(rows, aging_summary(rows).total_open, AS_OF)
    assert dso.credit_sales_in_window == 0
    assert dso.value is None


def test_dso_rounds_half_up() -> None:
    # 1,005 open / 1,000 sales * 90 = 90.45 exactly -> 90.5 (D-012), not banker's 90.4
    rows = build_ledger([_invoice(1, AS_OF - timedelta(days=5))], [], as_of=AS_OF)
    dso = estimated_dso(rows, to_fils("1005.00"), AS_OF)
    assert dso.value == Decimal("90.5")


@pytest.mark.parametrize(
    ("overdue", "total", "expected"),
    [
        ("147000.00", "193000.00", Decimal("76.2")),
        ("0.00", "500.00", Decimal("0.0")),
        ("500.00", "500.00", Decimal("100.0")),
        ("1.25", "10.00", Decimal("12.5")),
        ("0.00", "0.00", None),  # N/A, not 0%
    ],
)
def test_overdue_pct(overdue: str, total: str, expected: Decimal | None) -> None:
    assert overdue_pct(to_fils(overdue), to_fils(total)) == expected


def test_overdue_by_customer_excludes_not_yet_due_and_ties_by_id() -> None:
    a = InvoiceRecord(
        invoice_number="INV-A",
        customer_id="CUST-Z",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        amount=500,
    )
    b = InvoiceRecord(
        invoice_number="INV-B",
        customer_id="CUST-Y",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        amount=500,
    )
    due_today = InvoiceRecord(
        invoice_number="INV-C",
        customer_id="CUST-X",
        invoice_date=date(2026, 10, 1),
        due_date=AS_OF,
        amount=900,
    )
    result = overdue_by_customer(build_ledger([a, b, due_today], [], as_of=AS_OF))
    assert [r.customer_id for r in result] == ["CUST-Y", "CUST-Z"]
    assert [r.share for r in result] == [Fraction(1, 2), Fraction(1, 2)]


def test_overdue_by_customer_empty() -> None:
    assert overdue_by_customer([]) == []


def test_compute_kpis_golden(
    golden_ledger_inputs: Inputs, golden_events: list[EventRecord], golden_results: dict[str, Any]
) -> None:
    invoices, payments = golden_ledger_inputs
    rows = build_ledger(invoices, payments, as_of=AS_OF)
    config = EngineConfig()
    scored = score_overdue_invoices(
        rows, golden_events, customer_history(rows, AS_OF, config), AS_OF, config
    )
    kpis = compute_kpis(rows, scored, payments, AS_OF)
    assert kpis.total_open == to_fils("193000.00")
    assert kpis.overdue_total == to_fils("147000.00")
    assert kpis.overdue_pct == Decimal("76.2")
    assert kpis.dso.value == Decimal("149.7")
    assert [c.customer_id for c in kpis.overdue_by_customer] == ["CUST-B", "CUST-C", "CUST-A"]
    assert kpis.exposure_by_band == {
        k: to_fils(v) for k, v in golden_results["exposure_by_band"].items()
    }
    assert kpis.unallocated.total == 0
    assert kpis.unallocated.by_customer == {}


def test_dso_window_includes_invoices_dated_on_as_of() -> None:
    """§6.6: the window ends on ``as_of`` inclusive."""
    rows = build_ledger(
        [_invoice(1, AS_OF), _invoice(2, AS_OF - timedelta(days=90))], [], as_of=AS_OF
    )
    dso = estimated_dso(rows, aging_summary(rows).total_open, AS_OF)
    assert dso.credit_sales_in_window == to_fils("1000.00")
