"""Unallocated cash (SPEC §6.6, §6.7; §2 "Unallocated payment"): T-UNA-01 plus edge cases."""

from datetime import date

import pytest

from receivables.core.aging import aging_summary
from receivables.core.config import EngineConfig
from receivables.core.history import customer_history
from receivables.core.kpis import unallocated_cash
from receivables.core.ledger import InvoiceRecord, PaymentRecord, build_ledger
from receivables.core.money import to_fils
from receivables.core.queue import UNALLOCATED_CASH_FLAG, build_queue, unallocated_cash_message
from receivables.core.scoring import EventRecord, score_overdue_invoices

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)
P_005 = PaymentRecord(
    payment_id="P-005",
    invoice_number=None,
    customer_id="CUST-C",
    payment_date=date(2026, 10, 28),
    amount=to_fils("12000.00"),
    payment_type="payment",
)


def _unallocated(
    kind: str, on: date, customer: str | None = "CUST-C", amount: str = "100.00"
) -> PaymentRecord:
    return PaymentRecord(
        payment_id=f"U-{kind}-{on.isoformat()}",
        invoice_number=None,
        customer_id=customer,
        payment_date=on,
        amount=to_fils(amount),
        payment_type=kind,
    )


@pytest.mark.spec("T-UNA-01")
class TestUna01:
    def test_ledger_totals_and_buckets_unchanged(self, golden_ledger_inputs: Inputs) -> None:
        invoices, payments = golden_ledger_inputs
        before = build_ledger(invoices, payments, as_of=AS_OF)
        after = build_ledger(invoices, [*payments, P_005], as_of=AS_OF)
        assert after == before
        assert aging_summary(after) == aging_summary(before)

    def test_unallocated_total(self, golden_ledger_inputs: Inputs) -> None:
        _, payments = golden_ledger_inputs
        result = unallocated_cash([*payments, P_005], AS_OF)
        assert result.total == to_fils("12000.00")
        assert result.by_customer == {"CUST-C": to_fils("12000.00")}

    def test_queue_flags(
        self, golden_ledger_inputs: Inputs, golden_events: list[EventRecord]
    ) -> None:
        invoices, payments = golden_ledger_inputs
        all_payments = [*payments, P_005]
        rows = build_ledger(invoices, all_payments, as_of=AS_OF)
        config = EngineConfig()
        scored = score_overdue_invoices(
            rows, golden_events, customer_history(rows, AS_OF, config), AS_OF, config
        )
        queue = build_queue(
            scored, config, unallocated=unallocated_cash(all_payments, AS_OF).by_customer
        )
        flagged = {
            i.scored.invoice_number: [(f.code, f.amount) for f in i.flags]
            for i in queue.items
            if i.flags
        }
        assert flagged == {
            number: [(UNALLOCATED_CASH_FLAG, to_fils("12000.00"))]
            for number in ("INV-008", "INV-009", "INV-012")
        }
        assert UNALLOCATED_CASH_FLAG == "UNALLOCATED_CASH_ON_ACCOUNT"


def test_queue_has_no_flags_without_unallocated_cash(
    golden_ledger_inputs: Inputs, golden_events: list[EventRecord]
) -> None:
    rows = build_ledger(*golden_ledger_inputs, as_of=AS_OF)
    config = EngineConfig()
    scored = score_overdue_invoices(
        rows, golden_events, customer_history(rows, AS_OF, config), AS_OF, config
    )
    assert all(i.flags == () for i in build_queue(scored, config).items)
    assert all(i.flags == () for i in build_queue(scored, config, unallocated={"CUST-C": 0}).items)


def test_credit_note_counts_and_write_off_does_not() -> None:
    result = unallocated_cash(
        [
            _unallocated("payment", date(2026, 10, 1), amount="100.00"),
            _unallocated("credit_note", date(2026, 10, 2), amount="50.00"),
            _unallocated("write_off", date(2026, 10, 3), amount="999.00"),
        ],
        AS_OF,
    )
    assert result.by_customer == {"CUST-C": to_fils("150.00")}
    assert result.total == to_fils("150.00")


def test_unallocated_after_as_of_ignored_and_on_as_of_counted() -> None:
    result = unallocated_cash(
        [_unallocated("payment", date(2026, 11, 5)), _unallocated("payment", AS_OF)], AS_OF
    )
    assert result.total == to_fils("100.00")


def test_allocated_payments_and_rows_without_customer_are_not_unallocated() -> None:
    allocated = PaymentRecord(
        payment_id="P-1",
        invoice_number="INV-001",
        customer_id="CUST-A",
        payment_date=date(2026, 10, 1),
        amount=500,
        payment_type="payment",
    )
    no_customer = _unallocated("payment", date(2026, 10, 1), customer=None)
    assert unallocated_cash([allocated, no_customer], AS_OF).total == 0


def test_totals_per_customer_sorted() -> None:
    result = unallocated_cash(
        [
            _unallocated("payment", date(2026, 10, 1), customer="CUST-B", amount="10.00"),
            _unallocated("payment", date(2026, 10, 2), customer="CUST-A", amount="20.00"),
        ],
        AS_OF,
    )
    assert list(result.by_customer) == ["CUST-A", "CUST-B"]


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (
            "12000.00",
            "This customer has AED 12,000.00 of unallocated cash. "
            "Check whether it belongs to this invoice before chasing.",
        ),
        (
            "0.50",
            "This customer has AED 0.50 of unallocated cash. "
            "Check whether it belongs to this invoice before chasing.",
        ),
    ],
)
def test_ui_message(amount: str, text: str) -> None:
    assert unallocated_cash_message(to_fils(amount)) == text
