"""Queue and next action (SPEC §6.4, §6.5): T-QUE-01, T-ACT-01 plus ordering edges."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.core.config import EngineConfig
from receivables.core.history import customer_history
from receivables.core.ledger import InvoiceRecord, PaymentRecord, build_ledger
from receivables.core.queue import HEURISTIC_NOTE, Queue, build_queue, draft_tone, next_action
from receivables.core.scoring import EventRecord, ScoredInvoice, score_overdue_invoices

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)
MIN_HISTORY_2 = replace(EngineConfig(), min_history_invoices=2)


def queue_for(inputs: Inputs, events: list[EventRecord], config: EngineConfig) -> Queue:
    rows = build_ledger(*inputs, as_of=AS_OF)
    history = customer_history(rows, AS_OF, config)
    return build_queue(score_overdue_invoices(rows, events, history, AS_OF, config), config)


@pytest.mark.spec("T-QUE-01")
class TestQue01:
    def test_default_config(
        self,
        golden_ledger_inputs: Inputs,
        golden_events: list[EventRecord],
        golden_results: dict[str, Any],
    ) -> None:
        queue = queue_for(golden_ledger_inputs, golden_events, EngineConfig())
        expected = golden_results["queue_default"]["rows"]
        assert [i.rank for i in queue.items] == [r["rank"] for r in expected]
        assert [i.scored.invoice_number for i in queue.items] == [
            r["invoice_number"] for r in expected
        ]
        for item, r in zip(queue.items, expected, strict=True):
            assert item.scored.priority_value == Decimal(r["priority_value"]), r["invoice_number"]
            assert abs(item.scored.priority_value - Decimal(r["priority_value"])) <= Decimal("0.05")
            assert item.action == r["action"], r["invoice_number"]

    def test_min_history_2(
        self,
        golden_ledger_inputs: Inputs,
        golden_events: list[EventRecord],
        golden_results: dict[str, Any],
    ) -> None:
        default = {
            i.scored.invoice_number: i
            for i in queue_for(golden_ledger_inputs, golden_events, EngineConfig()).items
        }
        queue = queue_for(golden_ledger_inputs, golden_events, MIN_HISTORY_2)
        alt = golden_results["queue_min_history_2"]
        assert [i.scored.invoice_number for i in queue.items] == alt["order"]
        for item in queue.items:
            number = item.scored.invoice_number
            expected = (
                Decimal(alt["changed_rows"][number]["priority_value"])
                if number in alt["changed_rows"]
                else default[number].scored.priority_value
            )
            assert item.scored.priority_value == expected, number
            assert item.action == default[number].action, number


@pytest.mark.spec("T-ACT-01")
class TestAct01:
    def test_golden_actions(
        self,
        golden_ledger_inputs: Inputs,
        golden_events: list[EventRecord],
        golden_results: dict[str, Any],
    ) -> None:
        queue = queue_for(golden_ledger_inputs, golden_events, EngineConfig())
        actions = {i.scored.invoice_number: i.action for i in queue.items}
        assert actions == {
            r["invoice_number"]: r["action"] for r in golden_results["queue_default"]["rows"]
        }

    @pytest.mark.parametrize(
        ("days", "action"),
        [
            (1, "Send friendly reminder"),
            (30, "Send friendly reminder"),
            (31, "Send firm reminder"),
            (60, "Send firm reminder"),
            (61, "Escalate: call and send formal reminder"),
            (400, "Escalate: call and send formal reminder"),
        ],
    )
    def test_day_thresholds(self, days: int, action: str) -> None:
        assert next_action(_scored(days=days)) == action

    def test_dispute_beats_promise_beats_days(self) -> None:
        both = _scored(days=90, dispute_label="MISSING_PO", broken_promise=True)
        assert next_action(both) == "Resolve dispute: MISSING_PO"
        assert (
            next_action(_scored(days=90, broken_promise=True)) == "Follow up broken promise to pay"
        )


def _scored(
    number: str = "INV-1",
    days: int = 10,
    priority: str = "100.0",
    dispute_label: str | None = None,
    broken_promise: bool = False,
) -> ScoredInvoice:
    return ScoredInvoice(
        invoice_number=number,
        customer_id="CUST-T",
        open_amount=100000,
        days_overdue=days,
        f_overdue=Fraction(min(days, 90), 90),
        f_history=Fraction(1, 2),
        f_size=Fraction(0),
        f_dispute=1 if dispute_label else 0,
        f_promise=1 if broken_promise else 0,
        raw=Fraction(1, 2),
        risk_score=Decimal("50.0"),
        band="Medium",
        priority_value=Decimal(priority),
        dispute_label=dispute_label,
        broken_promise=broken_promise,
    )


@pytest.mark.parametrize(("days", "tone"), [(1, 1), (30, 1), (31, 2), (60, 2), (61, 3), (200, 3)])
def test_draft_tone(days: int, tone: int) -> None:
    assert draft_tone(days) == tone


def test_ties_broken_by_invoice_number() -> None:
    items = [
        _scored("INV-3", priority="50.0"),
        _scored("INV-1", priority="50.0"),
        _scored("INV-2", priority="70.0"),
    ]
    queue = build_queue(items, EngineConfig())
    assert [i.scored.invoice_number for i in queue.items] == ["INV-2", "INV-1", "INV-3"]


def test_queue_truncated_to_queue_size() -> None:
    items = [_scored(f"INV-{n:02d}", priority=f"{n}.0") for n in range(1, 26)]
    queue = build_queue(items, replace(EngineConfig(), queue_size=20))
    assert len(queue.items) == 20
    assert queue.items[0].scored.invoice_number == "INV-25"
    assert [i.rank for i in queue.items] == list(range(1, 21))


def test_queue_is_labelled_heuristic() -> None:
    queue = build_queue([_scored()], EngineConfig())
    assert queue.note == HEURISTIC_NOTE == "Heuristic score. Not a probability."


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**6), st.integers(min_value=1, max_value=99)
        ),
        max_size=30,
        unique_by=lambda t: t[1],
    )
)
def test_queue_always_sorted(items: list[tuple[int, int]]) -> None:
    scored = [_scored(f"INV-{n:02d}", priority=f"{p}.0") for p, n in items]
    queue = build_queue(scored, EngineConfig())
    keys = [(-i.scored.priority_value, i.scored.invoice_number) for i in queue.items]
    assert keys == sorted(keys)


def test_dispute_without_label() -> None:
    """D-011(4): an active dispute with no label still takes priority."""
    assert next_action(_scored(days=90, dispute_label="")) == "Resolve dispute"
