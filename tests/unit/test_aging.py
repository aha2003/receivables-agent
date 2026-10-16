"""Aging (SPEC §6.2): T-AGE-01, T-AGE-02 plus properties. Synthetic data only."""

from datetime import date
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.core.aging import BUCKETS, aging_summary, bucket_for
from receivables.core.ledger import InvoiceRecord, LedgerRow, PaymentRecord, build_ledger
from receivables.core.money import to_fils

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)


@pytest.mark.spec("T-AGE-01")
class TestAge01:
    def test_totals_and_bucket_totals(
        self, golden_ledger_inputs: Inputs, golden_results: dict[str, Any]
    ) -> None:
        summary = aging_summary(build_ledger(*golden_ledger_inputs, as_of=AS_OF))
        assert summary.total_open == to_fils("193000.00")
        assert summary.overdue_total == to_fils("147000.00")
        assert summary.bucket_totals == {
            k: to_fils(v) for k, v in golden_results["bucket_totals"].items()
        }


@pytest.mark.spec("T-AGE-02")
class TestAge02:
    def test_days_and_bucket_per_invoice(
        self, golden_ledger_inputs: Inputs, golden_results: dict[str, Any]
    ) -> None:
        rows = {r.invoice_number: r for r in build_ledger(*golden_ledger_inputs, as_of=AS_OF)}
        for number, item in golden_results["open_invoices"].items():
            assert rows[number].days_overdue == item["days_overdue"], number
            assert rows[number].bucket == item["bucket"], number

    @pytest.mark.parametrize(
        ("days", "bucket"),
        [
            (0, "current"),
            (30, "1-30"),
            (31, "31-60"),
            (60, "31-60"),
            (61, "61-90"),
            (90, "61-90"),
            (91, "90+"),
        ],
    )
    def test_bucket_boundaries(self, days: int, bucket: str) -> None:
        assert bucket_for(days) == bucket


@pytest.mark.parametrize(
    ("days", "bucket"), [(-365, "current"), (-1, "current"), (1, "1-30"), (10_000, "90+")]
)
def test_bucket_extremes(days: int, bucket: str) -> None:
    assert bucket_for(days) == bucket


def test_bucket_order_is_spec_order() -> None:
    assert BUCKETS == ("current", "1-30", "31-60", "61-90", "90+")


def test_summary_of_empty_ledger_has_all_buckets() -> None:
    summary = aging_summary([])
    assert summary.total_open == 0
    assert summary.overdue_total == 0
    assert summary.bucket_totals == dict.fromkeys(BUCKETS, 0)


def test_summary_ignores_settled_rows(golden_ledger_inputs: Inputs) -> None:
    rows = build_ledger(*golden_ledger_inputs, as_of=AS_OF)
    open_only = [r for r in rows if r.open_amount > 0]
    assert aging_summary(rows) == aging_summary(open_only)


@given(st.integers(min_value=-1000, max_value=1000), st.integers(min_value=0, max_value=1000))
def test_bucket_is_monotonic(days: int, extra: int) -> None:
    assert BUCKETS.index(bucket_for(days)) <= BUCKETS.index(bucket_for(days + extra))


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=10**9), st.integers(min_value=-200, max_value=400)
        ),
        max_size=30,
    )
)
def test_bucket_totals_sum_to_total_open(items: list[tuple[int, int]]) -> None:
    rows = [
        LedgerRow(
            invoice_number=f"INV-{i}",
            display_invoice_number=None,
            invoice_type=None,
            customer_id="CUST-H",
            invoice_date=date(2026, 1, 1),
            due_date=date(2026, 1, 1),
            amount=amount,
            open_amount=amount,
            days_overdue=days,
            bucket=bucket_for(days),
            settled_date=None,
            po_number=None,
        )
        for i, (amount, days) in enumerate(items)
    ]
    summary = aging_summary(rows)
    assert sum(summary.bucket_totals.values()) == summary.total_open == sum(a for a, _ in items)
    assert summary.overdue_total == summary.total_open - summary.bucket_totals["current"]


def test_open_row_without_bucket_rejected() -> None:
    row = LedgerRow(
        invoice_number="INV-X",
        display_invoice_number=None,
        invoice_type=None,
        customer_id="CUST-X",
        invoice_date=date(2026, 1, 1),
        due_date=date(2026, 1, 1),
        amount=100,
        open_amount=100,
        days_overdue=None,
        bucket=None,
        settled_date=None,
        po_number=None,
    )
    with pytest.raises(ValueError, match="without a bucket"):
        aging_summary([row])
