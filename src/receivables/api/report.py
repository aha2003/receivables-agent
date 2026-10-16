"""The Receivables Health Audit's content (SPEC §11; T-RPT-01; D-041).

Every figure comes from the same snapshot as the dashboard (the pure core); this module only
assembles sections, shares and the templated one-line observations. Nothing here is written by
a model: classification counts are of messages a person accepted (option 1a), and the import
warnings are those of the latest accepted import of each kind plus today's data-quality
warnings (option 2a).
"""

import json
from collections import Counter
from datetime import date, datetime
from fractions import Fraction
from typing import Any

from sqlalchemy import Engine, func, select

from receivables.adapters.clock import DUBAI
from receivables.adapters.db.models import Classification, Correspondence, Import
from receivables.api.snapshot import TOP_CUSTOMERS, Snapshot, data_quality, dso_formula
from receivables.core.aging import BUCKETS, aging_summary
from receivables.core.config import EngineConfig
from receivables.core.kpis import DSO_LABEL
from receivables.core.money import from_fils
from receivables.core.queue import HEURISTIC_NOTE
from receivables.core.scoring import round_half_up

__all__ = ["QUEUE_IN_REPORT", "report_context"]

QUEUE_IN_REPORT = 20  # §11 item 6
BANDS = ("High", "Medium", "Low")

NOT_MEASURED = (
    "Whether customers will pay, or when: the risk score ranks invoices, it does not predict.",
    "The company's own DSO: the estimated DSO here is a proxy from invoices and open amounts.",
    "Cash already received but not yet recorded, and bank balances: only imported payments count.",
    "Credit limits, collateral, guarantees and customer financial health.",
    "The cost of collection and the outcome of past reminders (the simulator, S4, is "
    "illustrative only).",
)


def _pct(part: int, whole: int) -> str | None:
    """``part`` as a percent of ``whole``, one decimal, halves up; None when ``whole`` is 0."""
    return None if whole == 0 else str(round_half_up(Fraction(100 * part, whole), 1))


def _aed(fils: int) -> str:
    return f"AED {from_fils(fils):,}"


def _messages_by_label(engine: Engine, as_of: date) -> dict[str, Any] | None:
    """Accepted classifications per label for messages sent on or before ``as_of`` (Dubai
    dates, as elsewhere); None when no correspondence was supplied."""
    with engine.connect() as conn:
        if not conn.execute(select(func.count()).select_from(Correspondence)).scalar_one():
            return None
        rows = conn.execute(
            select(Correspondence.sent_at, Classification.label, Classification.status)
            .join(Classification, Classification.message_id == Correspondence.message_id)
            .where(Correspondence.direction == "inbound")
        ).all()
        inbound = conn.execute(
            select(Correspondence.sent_at).where(Correspondence.direction == "inbound")
        ).scalars()
        sent = [s for s in inbound if _sent_on(s, as_of)]
    dated = [r for r in rows if _sent_on(r.sent_at, as_of)]
    accepted = Counter(r.label for r in dated if r.status == "accepted")
    return {
        "inbound": len(sent),
        "accepted": sorted(accepted.items(), key=lambda kv: (-kv[1], kv[0])),
        "accepted_total": sum(accepted.values()),
        "awaiting_review": sum(1 for r in dated if r.status == "pending"),
        "not_classified": len(sent) - len(dated),
    }


def _sent_on(sent_at: str, as_of: date) -> bool:
    try:
        return datetime.fromisoformat(sent_at).astimezone(DUBAI).date() <= as_of
    except ValueError:
        return False


def _import_warnings(engine: Engine) -> list[dict[str, Any]]:
    """Warnings of the latest accepted import of each file kind (option 2a)."""
    with engine.connect() as conn:
        latest = (
            select(Import.file_kind, func.max(Import.created_at).label("at"))
            .where(Import.status == "accepted")
            .group_by(Import.file_kind)
            .subquery()
        )
        rows = conn.execute(
            select(Import.file_kind, Import.filename, Import.created_at, Import.report_json)
            .join(
                latest,
                (Import.file_kind == latest.c.file_kind) & (Import.created_at == latest.c.at),
            )
            .order_by(Import.file_kind)
        ).all()
    out = []
    for r in rows:
        warnings = json.loads(r.report_json or "{}").get("warnings", [])
        out.extend({"file_kind": r.file_kind, "imported_at": r.created_at, **w} for w in warnings)
    return out


def _observations(snap: Snapshot, disputes: dict[str, Any]) -> list[str]:
    """One-line observations templated from the numbers (§11), never model-written."""
    k = snap.kpis
    lines: list[str] = []
    top3 = k.overdue_by_customer[:3]
    if k.overdue_total and top3:
        share = _pct(sum(c.amount for c in top3), k.overdue_total)
        lines.append(
            f"Top {len(top3)} customer{'s' if len(top3) > 1 else ''} account for {share}% of "
            "overdue exposure."
        )
    over_90 = aging_summary(snap.rows).bucket_totals["90+"]
    if k.total_open and over_90:
        lines.append(
            f"{_pct(over_90, k.total_open)}% of the open amount is more than 90 days overdue."
        )
    if disputes["count"]:
        lines.append(
            f"{disputes['count']} overdue invoice{'s' if disputes['count'] > 1 else ''} worth "
            f"{_aed(disputes['amount'])} {'have' if disputes['count'] > 1 else 'has'} an "
            "active dispute."
        )
    if k.unallocated.total:
        lines.append(
            f"{_aed(k.unallocated.total)} of unallocated cash is on account; check it "
            "before chasing."
        )
    return lines


def report_context(engine: Engine, snap: Snapshot) -> dict[str, Any]:
    k = snap.kpis
    buckets = aging_summary(snap.rows).bucket_totals
    cumulative = 0
    top = []
    for c in k.overdue_by_customer[:TOP_CUSTOMERS]:
        cumulative += c.amount
        top.append(
            {
                "customer_id": c.customer_id,
                "customer_name": snap.customer_name(c.customer_id),
                "amount": c.amount,
                "share_pct": str(c.share_pct),
                "cumulative_pct": _pct(cumulative, k.overdue_total),
                "unallocated": k.unallocated.by_customer.get(c.customer_id, 0),
            }
        )
    disputed = [s for s in snap.scored if s.dispute_label]  # overdue, with an active dispute
    by_label: dict[str, list[int]] = {}
    for s in disputed:
        by_label.setdefault(str(s.dispute_label), []).append(s.open_amount)
    disputes: dict[str, Any] = {
        "count": len(disputed),
        "amount": sum(s.open_amount for s in disputed),
        "by_label": [
            {"label": label, "count": len(amounts), "amount": sum(amounts)}
            for label, amounts in sorted(by_label.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
    }
    dates = [r.invoice_date for r in snap.rows]
    quality = data_quality(snap)
    return {
        "as_of": snap.as_of.isoformat(),
        "total_open": k.total_open,
        "overdue_total": k.overdue_total,
        "overdue_pct": None if k.overdue_pct is None else str(k.overdue_pct),
        "dso": None if k.dso.value is None else str(k.dso.value),
        "dso_label": DSO_LABEL,
        "dso_formula": dso_formula(snap.config.dso_window_days),
        "dso_window": (k.dso.window_start.isoformat(), k.dso.window_end.isoformat()),
        "credit_sales_in_window": k.dso.credit_sales_in_window,
        "coverage": {
            "invoices": len(snap.rows),
            "first_invoice": min(dates).isoformat() if dates else None,
            "last_invoice": max(dates).isoformat() if dates else None,
            "payments": quality["payments"],
            "has_payments": quality["has_payments"],
            "unallocated_count": snap.unallocated_count,
            "unallocated_total": k.unallocated.total,
        },
        "buckets": [
            {"name": b, "amount": buckets[b], "pct": _pct(buckets[b], k.total_open)}
            for b in BUCKETS
        ],
        "top_customers": top,
        "bands": [
            {
                "band": b,
                "amount": k.exposure_by_band.get(b, 0),
                "pct": _pct(k.exposure_by_band.get(b, 0), k.overdue_total),
            }
            for b in BANDS
        ],
        "disputes": disputes,
        "messages": _messages_by_label(engine, snap.as_of),
        "queue": [
            {
                "rank": item.rank,
                "invoice_number": item.scored.invoice_number,
                "customer_name": snap.customer_name(item.scored.customer_id),
                "open_amount": item.scored.open_amount,
                "days_overdue": item.scored.days_overdue,
                "risk_score": str(item.scored.risk_score),
                "band": item.scored.band,
                "priority_value": str(item.scored.priority_value),
                "action": item.action,
            }
            for item in snap.queue.items[:QUEUE_IN_REPORT]
        ],
        "observations": _observations(snap, disputes),
        "data_warnings": quality["warnings"],
        "import_warnings": _import_warnings(engine),
        "not_measured": NOT_MEASURED,
        "heuristic_note": HEURISTIC_NOTE,
        "formulas": _formulas(snap.config),
    }


def _num(value: Fraction) -> str:
    return str(round_half_up(value, 2)).rstrip("0").rstrip(".")


def _formulas(c: EngineConfig) -> list[tuple[str, str]]:
    """Every formula the report uses (§6.2, §6.4, §6.6), with the configured values."""
    return [
        (
            "Open amount",
            "invoice amount less payments, credit notes and write-offs dated on or "
            "before as_of, never below 0 (§2).",
        ),
        (
            "Aging buckets",
            "open invoices by days overdue (as_of less due date): current (0 or "
            "less), 1-30, 31-60, 61-90, 90+ (91 or more). Overdue = total open less current.",
        ),
        (
            "Risk score",
            f"raw = {_num(c.weight_overdue)} x min(days overdue, "
            f"{c.overdue_cap_days}) / {c.overdue_cap_days} + {_num(c.weight_history)} x the "
            f"customer's late rate (portfolio rate if fewer than {c.min_history_invoices} settled "
            f"invoices in {c.history_window_days} days, else {_num(c.no_history_fallback_rate)}) + "
            f"{_num(c.weight_size)} x size rank among open invoices + {_num(c.weight_dispute)} x "
            f"active dispute + {_num(c.weight_promise)} x broken promise; risk score = raw x 100, "
            f"one decimal. Bands: High {_num(c.band_high)} or more, Medium "
            f"{_num(c.band_medium)} or more, Low below.",
        ),
        (
            "Priority value",
            "raw x open amount, one decimal. A weighting that favours large, "
            "risky, old invoices; it is not expected cash loss.",
        ),
        ("Estimated DSO", dso_formula(c.dso_window_days)),
        (
            "Unallocated cash",
            "payments and credit notes with a customer but no invoice, dated "
            "on or before as_of. They reduce no invoice's open amount until allocated.",
        ),
    ]
