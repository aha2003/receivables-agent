"""One consistent read of the database turned into ledger, KPIs and queue for an ``as_of``.

Everything financial is computed by the pure core; this module only loads and assembles, then
serialises for the §9 JSON API. Amounts and scores are decimal strings, never JSON numbers.
"""

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any

from sqlalchemy import Engine

from receivables.adapters.db.repo import load_customers, load_events, load_invoices, load_payments
from receivables.core.aging import aging_summary
from receivables.core.config import EngineConfig
from receivables.core.history import customer_history
from receivables.core.kpis import DSO_LABEL, KpiSummary, compute_kpis
from receivables.core.ledger import LedgerRow, PaymentRecord, build_ledger
from receivables.core.money import from_fils
from receivables.core.queue import (
    HEURISTIC_NOTE,
    Queue,
    QueueItem,
    build_queue,
    unallocated_cash_message,
)
from receivables.core.scoring import (
    EventRecord,
    ScoredInvoice,
    round_half_up,
    score_overdue_invoices,
)
from receivables.core.validation import CustomerRow

__all__ = ["SYNTHETIC_LABEL", "Snapshot", "dashboard_json", "money", "queue_json", "take_snapshot"]

SYNTHETIC_LABEL = "SYNTHETIC DEMO DATA"
TOP_CUSTOMERS = 10


def money(fils: int) -> str:
    """Fils as a two-decimal AED string for JSON, e.g. ``"193000.00"``."""
    return str(from_fils(fils))


@dataclass(frozen=True, slots=True)
class Snapshot:
    as_of: date
    config: EngineConfig
    customers: dict[str, CustomerRow]
    rows: list[LedgerRow]
    payments: list[PaymentRecord]
    events: list[EventRecord]
    scored: list[ScoredInvoice]
    kpis: KpiSummary
    queue: Queue

    def customer_name(self, customer_id: str) -> str:
        customer = self.customers.get(customer_id)
        return customer.customer_name if customer else customer_id

    @property
    def payments_as_of(self) -> list[PaymentRecord]:
        return [p for p in self.payments if p.payment_date <= self.as_of]

    @property
    def unallocated_count(self) -> int:
        return sum(
            1
            for p in self.payments_as_of
            if p.invoice_number is None
            and p.customer_id is not None
            and p.payment_type in {"payment", "credit_note"}
        )


def take_snapshot(engine: Engine, as_of: date, config: EngineConfig) -> Snapshot:
    with engine.connect() as conn, conn.begin():
        customers = {c.customer_id: c for c in load_customers(conn)}
        invoices = load_invoices(conn)
        payments = load_payments(conn)
        events = load_events(conn)
    rows = build_ledger(invoices, payments, as_of=as_of)
    history = customer_history(rows, as_of, config)
    scored = score_overdue_invoices(rows, events, history, as_of, config)
    kpis = compute_kpis(rows, scored, payments, as_of, window_days=config.dso_window_days)
    queue = build_queue(scored, config, unallocated=kpis.unallocated.by_customer)
    return Snapshot(as_of, config, customers, rows, payments, events, scored, kpis, queue)


def _share(share: Fraction) -> str:
    return str(round_half_up(share, 4))


def data_quality(snap: Snapshot) -> dict[str, Any]:
    paid = snap.payments_as_of
    warnings: list[dict[str, Any]] = []
    if not paid:
        warnings.append({"code": "W_NO_PAYMENTS", "detail": None})
    if snap.kpis.unallocated.total > 0:
        detail = f"count={snap.unallocated_count} total={money(snap.kpis.unallocated.total)}"
        warnings.append({"code": "W_UNALLOCATED", "detail": detail})
    return {
        "invoices": len(snap.rows),
        "payments": len(paid),
        "has_payments": bool(paid),
        "warnings": warnings,
    }


def dso_formula(window_days: int) -> str:
    return (
        f"Estimated DSO = open receivables divided by credit sales invoiced in the "
        f"{window_days} days ending on as_of, multiplied by {window_days}. "
        "A proxy, not the company's reported DSO."
    )


def dashboard_json(snap: Snapshot) -> dict[str, Any]:
    k = snap.kpis
    dso = k.dso
    return {
        "as_of": snap.as_of.isoformat(),
        "total_open": money(k.total_open),
        "overdue_total": money(k.overdue_total),
        "overdue_pct": None if k.overdue_pct is None else str(k.overdue_pct),
        "buckets": {name: money(amount) for name, amount in _buckets(snap).items()},
        "estimated_dso": None if dso.value is None else str(dso.value),
        "dso_detail": {
            "label": DSO_LABEL,
            "formula": dso_formula(snap.config.dso_window_days),
            "value": None if dso.value is None else str(dso.value),
            "credit_sales_in_window": money(dso.credit_sales_in_window),
            "window_start": dso.window_start.isoformat(),
            "window_end": dso.window_end.isoformat(),
        },
        "exposure_by_band": {band: money(v) for band, v in k.exposure_by_band.items()},
        "unallocated_total": money(k.unallocated.total),
        "unallocated_by_customer": {c: money(v) for c, v in k.unallocated.by_customer.items()},
        "top_overdue_customers": [
            {
                "customer_id": c.customer_id,
                "customer_name": snap.customer_name(c.customer_id),
                "amount": money(c.amount),
                "share": _share(c.share),
                "share_pct": str(c.share_pct),
            }
            for c in k.overdue_by_customer[:TOP_CUSTOMERS]
        ],
        "data_quality": data_quality(snap),
        "labels": {"synthetic": SYNTHETIC_LABEL, "heuristic": HEURISTIC_NOTE},
    }


def _buckets(snap: Snapshot) -> dict[str, int]:
    return aging_summary(snap.rows).bucket_totals


def item_json(snap: Snapshot, item: QueueItem) -> dict[str, Any]:
    s = item.scored
    return {
        "rank": item.rank,
        "invoice_number": s.invoice_number,
        "customer_id": s.customer_id,
        "customer_name": snap.customer_name(s.customer_id),
        "open_amount": money(s.open_amount),
        "days_overdue": s.days_overdue,
        "risk_score": str(s.risk_score),
        "band": s.band,
        "priority_value": str(s.priority_value),
        "action": item.action,
        "dispute_label": s.dispute_label,
        "broken_promise": s.broken_promise,
        "flags": [
            {
                "code": f.code,
                "amount": money(f.amount),
                "message": unallocated_cash_message(f.amount),
            }
            for f in item.flags
        ],
    }


def queue_json(snap: Snapshot, limit: int) -> dict[str, Any]:
    return {
        "as_of": snap.as_of.isoformat(),
        "note": HEURISTIC_NOTE,
        "items": [item_json(snap, item) for item in snap.queue.items[:limit]],
    }
