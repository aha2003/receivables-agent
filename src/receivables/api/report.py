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
from receivables.adapters.db.models import AuditLog, Correspondence, Import
from receivables.api.snapshot import TOP_CUSTOMERS, Snapshot, data_quality, dso_formula
from receivables.core.aging import BUCKETS, aging_summary
from receivables.core.config import EngineConfig
from receivables.core.kpis import DSO_LABEL
from receivables.core.money import from_fils
from receivables.core.queue import HEURISTIC_NOTE, unallocated_cash_message
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
    """Inbound messages sent on or before ``as_of`` and, for each, its classification status as
    of that date, read from the append-only audit log (classified, then accepted or rejected
    by a person). Labels are counted only for accepted ones (option 1a). None when no
    correspondence was supplied. Dates are Asia/Dubai, as elsewhere."""
    with engine.connect() as conn:
        if not conn.execute(select(func.count()).select_from(Correspondence)).scalar_one():
            return None
        inbound = conn.execute(
            select(Correspondence.message_id, Correspondence.sent_at).where(
                Correspondence.direction == "inbound"
            )
        ).all()
        events = conn.execute(
            select(AuditLog.entity_id, AuditLog.action, AuditLog.ts, AuditLog.after_json)
            .where(
                AuditLog.entity_type == "message",
                AuditLog.action.in_(
                    ("classification.created", "classification.accepted",
                     "classification.rejected")
                ),
            )
            .order_by(AuditLog.audit_id)
        ).all()  # fmt: skip
    status: dict[str, str] = {}
    labels: dict[str, str] = {}
    for e in events:
        if not _on_or_before(e.ts, as_of):
            continue
        if e.action == "classification.created":
            status[e.entity_id] = "pending"  # re-classifying is only possible while pending
        else:
            status[e.entity_id] = e.action.removeprefix("classification.")
            if e.action == "classification.accepted":
                labels[e.entity_id] = json.loads(e.after_json or "{}").get("label", "")
    sent = [m.message_id for m in inbound if _on_or_before(m.sent_at, as_of)]
    states = Counter(status.get(m, "not_classified") for m in sent)
    accepted = Counter(labels[m] for m in sent if status.get(m) == "accepted")
    return {
        "inbound": len(sent),
        "accepted": sorted(accepted.items(), key=lambda kv: (-kv[1], kv[0])),
        "accepted_total": states["accepted"],
        "rejected": states["rejected"],
        "awaiting_review": states["pending"],
        "not_classified": states["not_classified"],
    }


def _on_or_before(timestamp: str, as_of: date) -> bool:
    try:
        return datetime.fromisoformat(timestamp).astimezone(DUBAI).date() <= as_of
    except ValueError:
        return False


def _import_warnings(engine: Engine) -> list[dict[str, Any]]:
    """Warnings of the latest accepted import of each file kind (option 2a)."""
    with engine.connect() as conn:
        latest = (
            select(Import.file_kind, func.max(Import.created_at).label("at"))
            .group_by(Import.file_kind)  # only accepted imports are stored
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
        warnings = [
            w
            for w in json.loads(r.report_json or "{}").get("warnings", [])
            # Whether payments exist is a fact of the data as of as_of, reported with the data
            # warnings; an import's "no payments yet" would contradict it once payments arrive.
            if w.get("code") != "W_NO_PAYMENTS"
        ]
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
            f"Top {len(top3)} customer{'s account' if len(top3) > 1 else ' accounts'} for "
            f"{share}% of "
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
                "share_pct": _pct(c.amount, k.overdue_total),  # one decimal, as T-RPT-01
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
    overdue = {c.customer_id: c.amount for c in k.overdue_by_customer}
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
        "unallocated_by_customer": [  # §6.7: listed next to the customer's overdue exposure
            {
                "customer_id": cid,
                "customer_name": snap.customer_name(cid),
                "unallocated": amount,
                "overdue": overdue.get(cid, 0),
            }
            for cid, amount in sorted(k.unallocated.by_customer.items())
            if amount > 0
        ],
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
                "flags": [unallocated_cash_message(f.amount) for f in item.flags],  # §6.7
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
            "invoice amount less the payments, credit notes and write-offs applied to it and "
            "dated on or before as_of, never below 0; unallocated payments reduce no invoice, "
            "and invoices dated after as_of are left out (§2).",
        ),
        (
            "Aging buckets",
            "open invoices by days overdue (as_of less due date): current (0 or "
            "less), 1-30, 31-60, 61-90, 90+ (91 or more). Overdue = total open less current.",
        ),
        (
            "Risk score",
            f"raw = {_num(c.weight_overdue)} x min(days overdue, "
            f"{c.overdue_cap_days}) / {c.overdue_cap_days} + {_num(c.weight_history)} x "
            f"history + {_num(c.weight_size)} x size + {_num(c.weight_dispute)} x "
            f"active dispute + {_num(c.weight_promise)} x broken promise; risk score = raw x 100, "
            f"one decimal. Bands: High {_num(c.band_high)} or more, Medium "
            f"{_num(c.band_medium)} or more, Low below. History: the customer's late rate over "
            f"invoices settled in the {c.history_window_days} days to as_of if it has at least "
            f"{c.min_history_invoices} of them; otherwise the portfolio's late rate; "
            f"{_num(c.no_history_fallback_rate)} if no invoice settled in that window. Size: the "
            "number of open invoices with a strictly smaller open amount divided by the number "
            "of open invoices less 1 (0 if only one is open).",
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
