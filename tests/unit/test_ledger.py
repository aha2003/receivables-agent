"""Ledger (SPEC §6.1): T-LED-01, T-LED-02 plus edge cases. Synthetic data only."""

import random
from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.core.ledger import (
    InvoiceRecord,
    LedgerRow,
    OverpaymentError,
    PaymentRecord,
    build_ledger,
)
from receivables.core.money import to_fils

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)


def _by_number(rows: list[LedgerRow]) -> dict[str, LedgerRow]:
    return {r.invoice_number: r for r in rows}


class TestLed01:
    def test_open_invoices_and_amounts(
        self, golden_ledger_inputs: Inputs, golden_results: dict[str, Any]
    ) -> None:
        rows = _by_number(build_ledger(*golden_ledger_inputs, as_of=AS_OF))
        open_rows = {n: r for n, r in rows.items() if r.open_amount > 0}
        expected = golden_results["open_invoices"]
        assert set(open_rows) == set(expected)
        for number, row in open_rows.items():
            assert row.open_amount == to_fils(expected[number]["open_amount"]), number

    def test_closed_invoices_not_open(self, golden_ledger_inputs: Inputs) -> None:
        rows = _by_number(build_ledger(*golden_ledger_inputs, as_of=AS_OF))
        for number in ("INV-001", "INV-002", "INV-007"):
            assert rows[number].open_amount == 0
            assert rows[number].days_overdue is None
            assert rows[number].bucket is None
        assert rows["INV-001"].settled_date == date(2026, 8, 10)
        assert rows["INV-007"].settled_date == date(2026, 8, 14)
        assert len(rows) == 14


class TestLed02:
    def test_time_travel(self, golden_ledger_inputs: Inputs) -> None:
        rows = _by_number(build_ledger(*golden_ledger_inputs, as_of=date(2026, 9, 30)))
        assert len(rows) == 13
        assert "INV-006" not in rows
        assert rows["INV-005"].open_amount == to_fils("30000.00")
        assert rows["INV-005"].settled_date is None


def _invoice(number: str = "INV-T1", amount: str = "1000.00", **kw: Any) -> InvoiceRecord:
    fields: dict[str, Any] = {
        "invoice_number": number,
        "customer_id": "CUST-T",
        "invoice_date": date(2026, 9, 1),
        "due_date": date(2026, 10, 1),
        "amount": to_fils(amount),
    }
    fields.update(kw)
    return InvoiceRecord(**fields)


def _payment(
    pid: str, amount: str, on: date, invoice: str | None = "INV-T1", kind: str = "payment"
) -> PaymentRecord:
    return PaymentRecord(
        payment_id=pid,
        invoice_number=invoice,
        customer_id=None if invoice else "CUST-T",
        payment_date=on,
        amount=to_fils(amount),
        payment_type=kind,
    )


def test_unallocated_payment_never_applied() -> None:
    rows = build_ledger(
        [_invoice()], [_payment("P-U", "1000.00", date(2026, 9, 5), invoice=None)], as_of=AS_OF
    )
    assert rows[0].open_amount == to_fils("1000.00")


def test_payment_for_invoice_outside_ledger_ignored() -> None:
    late = _invoice("INV-LATE", invoice_date=date(2026, 11, 5), due_date=date(2026, 12, 5))
    rows = build_ledger(
        [_invoice(), late],
        [_payment("P-1", "500.00", date(2026, 10, 1), invoice="INV-LATE")],
        as_of=AS_OF,
    )
    assert [r.invoice_number for r in rows] == ["INV-T1"]
    assert rows[0].open_amount == to_fils("1000.00")


def test_payment_after_as_of_ignored() -> None:
    rows = build_ledger([_invoice()], [_payment("P-1", "1000.00", date(2026, 11, 1))], as_of=AS_OF)
    assert rows[0].open_amount == to_fils("1000.00")
    assert rows[0].settled_date is None


def test_payment_on_as_of_counts() -> None:
    rows = build_ledger([_invoice()], [_payment("P-1", "1000.00", AS_OF)], as_of=AS_OF)
    assert rows[0].open_amount == 0
    assert rows[0].settled_date == AS_OF


@pytest.mark.parametrize("kind", ["payment", "credit_note", "write_off"])
def test_every_payment_type_reduces_open_amount(kind: str) -> None:
    rows = build_ledger(
        [_invoice()], [_payment("P-1", "400.00", date(2026, 9, 20), kind=kind)], as_of=AS_OF
    )
    assert rows[0].open_amount == to_fils("600.00")


def test_split_payment_settled_on_last_part() -> None:
    payments = [
        _payment("P-2", "600.00", date(2026, 10, 20)),
        _payment("P-1", "400.00", date(2026, 10, 2)),
    ]
    row = build_ledger([_invoice()], payments, as_of=AS_OF)[0]
    assert row.open_amount == 0
    assert row.settled_date == date(2026, 10, 20)


def test_partial_payment_keeps_days_and_bucket() -> None:
    row = build_ledger([_invoice()], [_payment("P-1", "1.00", date(2026, 9, 2))], as_of=AS_OF)[0]
    assert row.open_amount == to_fils("999.00")
    assert row.days_overdue == 30
    assert row.bucket == "1-30"
    assert row.settled_date is None


def test_overpayment_raises() -> None:
    with pytest.raises(OverpaymentError, match="INV-T1"):
        build_ledger([_invoice()], [_payment("P-1", "1000.01", date(2026, 9, 5))], as_of=AS_OF)


def test_invoice_on_as_of_included_and_after_excluded() -> None:
    on = _invoice("INV-ON", invoice_date=AS_OF, due_date=date(2026, 11, 30))
    after = _invoice("INV-AFTER", invoice_date=date(2026, 11, 1), due_date=date(2026, 11, 30))
    rows = build_ledger([on, after], [], as_of=AS_OF)
    assert [r.invoice_number for r in rows] == ["INV-ON"]
    assert rows[0].days_overdue == -30
    assert rows[0].bucket == "current"


def test_optional_fields_pass_through() -> None:
    inv = _invoice(display_invoice_number="Inv 1", invoice_type="tax_invoice", po_number="PO-T")
    row = build_ledger([inv], [], as_of=AS_OF)[0]
    assert (row.display_invoice_number, row.invoice_type, row.po_number) == (
        "Inv 1",
        "tax_invoice",
        "PO-T",
    )
    assert row.amount == to_fils("1000.00")
    assert row.customer_id == "CUST-T"


def test_duplicate_invoice_number_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate invoice_number"):
        build_ledger([_invoice(), _invoice()], [], as_of=AS_OF)


def test_deterministic_regardless_of_input_order(golden_ledger_inputs: Inputs) -> None:
    invoices, payments = golden_ledger_inputs
    expected = build_ledger(invoices, payments, as_of=AS_OF)
    rng = random.Random(42)  # noqa: S311 - test shuffling, not security
    for _ in range(5):
        inv, pay = invoices[:], payments[:]
        rng.shuffle(inv)
        rng.shuffle(pay)
        assert build_ledger(inv, pay, as_of=AS_OF) == expected
    assert [r.invoice_number for r in expected] == sorted(r.invoice_number for r in expected)


@given(
    amount=st.integers(min_value=1, max_value=10**12),
    parts=st.lists(st.integers(min_value=1, max_value=10**12), max_size=6),
    days=st.integers(min_value=-400, max_value=400),
)
def test_open_amount_bounded(amount: int, parts: list[int], days: int) -> None:
    applied: list[int] = []
    for p in parts:
        if sum(applied) + p <= amount:
            applied.append(p)
    inv = InvoiceRecord(
        invoice_number="INV-H",
        customer_id="CUST-H",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 1),
        amount=amount,
    )
    pays = [
        PaymentRecord(
            payment_id=f"P-{i}",
            invoice_number="INV-H",
            customer_id=None,
            payment_date=date(2026, 1, 2),
            amount=a,
            payment_type="payment",
        )
        for i, a in enumerate(applied)
    ]
    as_of = date(2026, 1, 2) + timedelta(days=max(days, 0))
    row = build_ledger([inv], pays, as_of=as_of)[0]
    assert 0 <= row.open_amount <= amount
    assert row.open_amount == amount - sum(applied)
    assert (row.settled_date is None) == (row.open_amount > 0)
