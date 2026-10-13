"""Heuristic risk score and priority value (SPEC §6.4; dispute and promise per §2). Pure.

The risk score is a heuristic ranking score, not a probability; the priority value is a
weighting, not expected loss. Arithmetic is exact (``Fraction``); results are ``Decimal``
with one decimal place, rounded half up. Bands use the rounded risk score (D-011).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

from receivables.core.config import EngineConfig
from receivables.core.history import HistoryResult
from receivables.core.ledger import LedgerRow

__all__ = [
    "BANDS",
    "EventRecord",
    "ScoredInvoice",
    "active_dispute_label",
    "band_for",
    "exposure_by_band",
    "is_broken_promise",
    "round_half_up",
    "score_overdue_invoices",
]

BANDS = ("High", "Medium", "Low")
_DISPUTE_TYPES = frozenset({"DISPUTE_OPENED", "DISPUTE_RESOLVED"})


@dataclass(frozen=True, slots=True, kw_only=True)
class EventRecord:
    event_id: str
    invoice_number: str
    event_type: str
    event_date: date
    label: str | None = None
    promise_date: date | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoredInvoice:
    invoice_number: str
    customer_id: str
    open_amount: int
    days_overdue: int
    f_overdue: Fraction
    f_history: Fraction
    f_size: Fraction
    f_dispute: int
    f_promise: int
    raw: Fraction
    risk_score: Decimal
    band: str
    priority_value: Decimal
    dispute_label: str | None
    broken_promise: bool


def round_half_up(value: Fraction, places: int) -> Decimal:
    """Round an exact value to ``places`` decimals, halves away from zero."""
    scaled = abs(value) * 10**places
    whole = int(scaled + Fraction(1, 2))  # int() floors non-negative values
    digits = tuple(int(d) for d in str(whole).rjust(places + 1, "0"))
    return Decimal((1 if value < 0 and whole else 0, digits, -places))


def _latest(
    events: Iterable[EventRecord], as_of: date, types: frozenset[str]
) -> EventRecord | None:
    relevant = [e for e in events if e.event_type in types and e.event_date <= as_of]
    return max(relevant, key=lambda e: (e.event_date, e.event_id), default=None)


def active_dispute_label(events: Iterable[EventRecord], as_of: date) -> str | None:
    """Label of the active dispute, or ``None``. Active: latest dispute event is OPENED (§2)."""
    latest = _latest(events, as_of, _DISPUTE_TYPES)
    if latest is None or latest.event_type != "DISPUTE_OPENED":
        return None
    return latest.label or ""


def is_broken_promise(events: Iterable[EventRecord], as_of: date, *, is_open: bool) -> bool:
    """Latest PROMISE_TO_PAY has ``promise_date < as_of`` and the invoice is open (§2)."""
    latest = _latest(events, as_of, frozenset({"PROMISE_TO_PAY"}))
    if latest is None or latest.promise_date is None or not is_open:
        return False
    return latest.promise_date < as_of


def band_for(risk_score: Decimal, config: EngineConfig) -> str:
    score = Fraction(risk_score)
    if score >= config.band_high:
        return "High"
    if score >= config.band_medium:
        return "Medium"
    return "Low"


def score_overdue_invoices(
    rows: Iterable[LedgerRow],
    events: Iterable[EventRecord],
    history: HistoryResult,
    as_of: date,
    config: EngineConfig,
) -> list[ScoredInvoice]:
    """Score every overdue open invoice (§6.4), ordered by invoice number."""
    open_rows = [r for r in rows if r.open_amount > 0]
    by_invoice: dict[str, list[EventRecord]] = {}
    for event in events:
        by_invoice.setdefault(event.invoice_number, []).append(event)

    scored: list[ScoredInvoice] = []
    for row in sorted(open_rows, key=lambda r: r.invoice_number):
        days = row.days_overdue
        if days is None or days <= 0:
            continue
        f_overdue = Fraction(min(days, config.overdue_cap_days), config.overdue_cap_days)
        f_history = _history_factor(row.customer_id, history, config)
        if len(open_rows) > 1:
            smaller = sum(1 for o in open_rows if o.open_amount < row.open_amount)
            f_size = Fraction(smaller, len(open_rows) - 1)
        else:
            f_size = Fraction(0)
        invoice_events = by_invoice.get(row.invoice_number, [])
        dispute_label = active_dispute_label(invoice_events, as_of)
        broken = is_broken_promise(invoice_events, as_of, is_open=True)
        f_dispute = int(dispute_label is not None)
        f_promise = int(broken)
        raw = (
            config.weight_overdue * f_overdue
            + config.weight_history * f_history
            + config.weight_size * f_size
            + config.weight_dispute * f_dispute
            + config.weight_promise * f_promise
        )
        risk_score = round_half_up(raw * 100, 1)
        scored.append(
            ScoredInvoice(
                invoice_number=row.invoice_number,
                customer_id=row.customer_id,
                open_amount=row.open_amount,
                days_overdue=days,
                f_overdue=f_overdue,
                f_history=f_history,
                f_size=f_size,
                f_dispute=f_dispute,
                f_promise=f_promise,
                raw=raw,
                risk_score=risk_score,
                band=band_for(risk_score, config),
                priority_value=round_half_up(raw * Fraction(row.open_amount, 100), 1),
                dispute_label=dispute_label,
                broken_promise=broken,
            )
        )
    return scored


def _history_factor(customer_id: str, history: HistoryResult, config: EngineConfig) -> Fraction:
    own = history.customers.get(customer_id)
    if own is not None and own.paid >= config.min_history_invoices:
        return own.late_rate
    if history.portfolio_late_rate is not None:
        return history.portfolio_late_rate
    return config.no_history_fallback_rate


def exposure_by_band(scored: Iterable[ScoredInvoice]) -> dict[str, int]:
    """Open amount (fils) of scored overdue invoices per band; every band present."""
    totals: dict[str, int] = dict.fromkeys(BANDS, 0)
    for s in scored:
        totals[s.band] += s.open_amount
    return totals
