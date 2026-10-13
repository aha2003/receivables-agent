"""Risk score (SPEC §6.4, §2): T-RSK-01, T-RSK-02 plus events, rounding and band edges."""

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
from receivables.core.money import to_fils
from receivables.core.scoring import (
    EventRecord,
    ScoredInvoice,
    active_dispute_label,
    band_for,
    exposure_by_band,
    is_broken_promise,
    round_half_up,
    score_overdue_invoices,
)

Inputs = tuple[list[InvoiceRecord], list[PaymentRecord]]
AS_OF = date(2026, 10, 31)
MIN_HISTORY_2 = replace(EngineConfig(), min_history_invoices=2)


def score(
    inputs: Inputs, events: list[EventRecord], config: EngineConfig
) -> dict[str, ScoredInvoice]:
    rows = build_ledger(*inputs, as_of=AS_OF)
    history = customer_history(rows, AS_OF, config)
    scored = score_overdue_invoices(rows, events, history, AS_OF, config)
    return {s.invoice_number: s for s in scored}


class TestRsk01:
    def test_default_config_matches_golden_table(
        self,
        golden_ledger_inputs: Inputs,
        golden_events: list[EventRecord],
        golden_results: dict[str, Any],
    ) -> None:
        scored = score(golden_ledger_inputs, golden_events, EngineConfig())
        rows = golden_results["queue_default"]["rows"]
        assert set(scored) == {r["invoice_number"] for r in rows}
        for r in rows:
            s = scored[r["invoice_number"]]
            assert s.risk_score == Decimal(r["risk"]), r["invoice_number"]
            assert s.band == r["band"], r["invoice_number"]

    def test_min_history_2_changes_only_two_rows(
        self,
        golden_ledger_inputs: Inputs,
        golden_events: list[EventRecord],
        golden_results: dict[str, Any],
    ) -> None:
        default = score(golden_ledger_inputs, golden_events, EngineConfig())
        alt = score(golden_ledger_inputs, golden_events, MIN_HISTORY_2)
        assert alt["INV-003"].risk_score == Decimal("47.3")
        assert alt["INV-013"].risk_score == Decimal("38.8")
        changed = golden_results["queue_min_history_2"]["changed_rows"]
        for number, s in alt.items():
            if number in changed:
                assert s.risk_score == Decimal(changed[number]["risk"])
            else:
                assert s.risk_score == default[number].risk_score, number
                assert s.band == default[number].band, number


class TestRsk02:
    @pytest.mark.parametrize("config", [EngineConfig(), MIN_HISTORY_2])
    def test_exposure_by_band(
        self, golden_ledger_inputs: Inputs, golden_events: list[EventRecord], config: EngineConfig
    ) -> None:
        scored = score(golden_ledger_inputs, golden_events, config)
        assert exposure_by_band(scored.values()) == {
            "High": to_fils("57000.00"),
            "Medium": to_fils("81000.00"),
            "Low": to_fils("9000.00"),
        }


def test_components_for_inv_004(
    golden_ledger_inputs: Inputs, golden_events: list[EventRecord]
) -> None:
    s = score(golden_ledger_inputs, golden_events, EngineConfig())["INV-004"]
    assert (s.f_overdue, s.f_history, s.f_size, s.f_dispute, s.f_promise) == (
        Fraction(1),
        Fraction(2, 3),
        Fraction(1),
        1,
        0,
    )
    assert s.dispute_label == "PO_AMOUNT_MISMATCH"
    assert s.raw == Fraction(49, 60)


def test_only_overdue_invoices_scored(
    golden_ledger_inputs: Inputs, golden_events: list[EventRecord]
) -> None:
    scored = score(golden_ledger_inputs, golden_events, EngineConfig())
    assert "INV-006" not in scored  # not yet due
    assert "INV-010" not in scored  # due on as_of: not overdue (§2)
    assert "INV-001" not in scored  # closed


def test_f_size_counts_not_yet_due_invoices(
    golden_ledger_inputs: Inputs, golden_events: list[EventRecord]
) -> None:
    # 11 open invoices; INV-005 (20,000) is larger than 7 of them, including INV-010 (not overdue).
    s = score(golden_ledger_inputs, golden_events, EngineConfig())["INV-005"]
    assert s.f_size == Fraction(7, 10)


def test_single_open_invoice_has_zero_size_factor() -> None:
    inv = InvoiceRecord(
        invoice_number="INV-1",
        customer_id="CUST-T",
        invoice_date=date(2026, 9, 1),
        due_date=date(2026, 10, 1),
        amount=100000,
    )
    s = score(([inv], []), [], EngineConfig())["INV-1"]
    assert s.f_size == 0
    assert s.f_history == Fraction(1, 2)  # no settled invoices: no_history_fallback_rate


def _event(
    eid: str, kind: str, on: date, label: str | None = None, promise: date | None = None
) -> EventRecord:
    return EventRecord(
        event_id=eid,
        invoice_number="INV-1",
        event_type=kind,
        event_date=on,
        label=label,
        promise_date=promise,
    )


def test_dispute_resolved_is_not_active() -> None:
    events = [
        _event("E-1", "DISPUTE_OPENED", date(2026, 9, 1), "MISSING_PO"),
        _event("E-2", "DISPUTE_RESOLVED", date(2026, 9, 20)),
    ]
    assert active_dispute_label(events, AS_OF) is None


def test_reopened_dispute_is_active_with_latest_label() -> None:
    events = [
        _event("E-1", "DISPUTE_OPENED", date(2026, 9, 1), "MISSING_PO"),
        _event("E-2", "DISPUTE_RESOLVED", date(2026, 9, 20)),
        _event("E-3", "DISPUTE_OPENED", date(2026, 10, 2), "PRICE_OR_RATE_DISPUTE"),
    ]
    assert active_dispute_label(events, AS_OF) == "PRICE_OR_RATE_DISPUTE"


def test_dispute_after_as_of_ignored() -> None:
    events = [_event("E-1", "DISPUTE_OPENED", date(2026, 11, 1), "MISSING_PO")]
    assert active_dispute_label(events, AS_OF) is None
    resolved_later = [
        _event("E-1", "DISPUTE_OPENED", date(2026, 9, 1), "MISSING_PO"),
        _event("E-2", "DISPUTE_RESOLVED", date(2026, 11, 2)),
    ]
    assert active_dispute_label(resolved_later, AS_OF) == "MISSING_PO"


@pytest.mark.parametrize(
    ("promise", "is_open", "broken"),
    [
        (date(2026, 10, 30), True, True),
        (AS_OF, True, False),  # promise_date == as_of is not yet broken
        (date(2026, 11, 5), True, False),
        (date(2026, 10, 30), False, False),  # settled invoice: not broken
        (None, True, False),  # D-011: no promise date, not broken
    ],
)
def test_broken_promise(promise: date | None, is_open: bool, broken: bool) -> None:
    events = [_event("E-1", "PROMISE_TO_PAY", date(2026, 10, 5), promise=promise)]
    assert is_broken_promise(events, AS_OF, is_open=is_open) is broken


def test_latest_promise_wins_and_future_promise_event_ignored() -> None:
    kept_new = [
        _event("E-1", "PROMISE_TO_PAY", date(2026, 10, 1), promise=date(2026, 10, 10)),
        _event("E-2", "PROMISE_TO_PAY", date(2026, 10, 12), promise=date(2026, 11, 15)),
    ]
    assert is_broken_promise(kept_new, AS_OF, is_open=True) is False
    future = [
        _event("E-1", "PROMISE_TO_PAY", date(2026, 10, 1), promise=date(2026, 10, 10)),
        _event("E-2", "PROMISE_TO_PAY", date(2026, 11, 2), promise=date(2026, 11, 30)),
    ]
    assert is_broken_promise(future, AS_OF, is_open=True) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(1225, 10), Decimal("122.5")),
        (Fraction(12345, 100), Decimal("123.5")),  # exact .x5 rounds up
        (Fraction(12344, 100), Decimal("123.4")),
        (Fraction(2450, 3), Decimal("816.7")),
        (Fraction(0), Decimal("0.0")),
        (Fraction(-12345, 100), Decimal("-123.5")),  # half away from zero
    ],
)
def test_round_half_up(value: Fraction, expected: Decimal) -> None:
    result = round_half_up(value, 1)
    assert result == expected
    assert str(result) == str(expected)


@pytest.mark.parametrize(
    ("risk", "band"),
    [
        ("29.9", "Low"),
        ("30.0", "Medium"),
        ("59.9", "Medium"),
        ("60.0", "High"),
        ("0.0", "Low"),
        ("100.0", "High"),
    ],
)
def test_band_edges(risk: str, band: str) -> None:
    assert band_for(Decimal(risk), EngineConfig()) == band


@given(
    amounts=st.lists(st.integers(min_value=1, max_value=10**9), min_size=1, max_size=12),
    days=st.lists(st.integers(min_value=1, max_value=500), min_size=12, max_size=12),
)
def test_raw_is_within_unit_interval(amounts: list[int], days: list[int]) -> None:
    invoices = [
        InvoiceRecord(
            invoice_number=f"INV-{i:03d}",
            customer_id=f"CUST-{i % 3}",
            invoice_date=date(2024, 1, 1),
            due_date=date.fromordinal(AS_OF.toordinal() - days[i]),
            amount=a,
        )
        for i, a in enumerate(amounts)
    ]
    for s in score((invoices, []), [], EngineConfig()).values():
        assert 0 <= s.raw <= 1
        assert Decimal(0) <= s.risk_score <= Decimal(100)
