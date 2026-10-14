"""``GET /api/invoices/{invoice_number}`` (SPEC §9; T-API-04; docs/DECISIONS.md D-026).

The ledger row as of ``as_of`` plus its payments, events, messages, classifications, drafts,
case and audit rows. Financial facts come from the core through the dashboard snapshot;
amounts are decimal strings.
"""

import json
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

from receivables.api.snapshot import money, take_snapshot

__all__ = ["invoice_detail"]


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _audit(conn: Connection, invoice: str, kinds: list[str]) -> list[dict[str, Any]]:
    """Rows about this invoice (D-026): its case and agent steps, and the imports that loaded
    the file kinds holding its rows (the audit log has no per-row import link)."""
    rows = conn.execute(
        text(
            "SELECT * FROM audit_log WHERE entity_id = :n"
            " OR (action = 'agent.step' AND json_extract(after_json, '$.invoice_number') = :n)"
            " OR (action = 'import.accepted'"
            "     AND json_extract(after_json, '$.file_kind') IN (SELECT value FROM json_each(:k)))"
            " ORDER BY audit_id"
        ),
        {"n": invoice, "k": json.dumps(kinds)},
    ).mappings()
    return [
        {
            "audit_id": r["audit_id"],
            "ts": r["ts"],
            "actor": r["actor"],
            "action": r["action"],
            "entity_type": r["entity_type"],
            "entity_id": r["entity_id"],
            "before": _loads(r["before_json"]),
            "after": _loads(r["after_json"]),
        }
        for r in rows
    ]


def invoice_detail(state: Any, invoice_number: str, as_of: date) -> dict[str, Any] | None:
    snap = take_snapshot(state.engine, as_of, state.config.engine)
    row = next((r for r in snap.rows if r.invoice_number == invoice_number), None)
    if row is None:
        return None
    payments = [p for p in snap.payments_as_of if p.invoice_number == invoice_number]
    events = sorted(
        (e for e in snap.events if e.invoice_number == invoice_number and e.event_date <= as_of),
        key=lambda e: (e.event_date, e.event_id),
    )
    kinds = ["invoices"] + (["payments"] if payments else []) + (["events"] if events else [])
    with state.engine.connect() as conn:
        messages = (
            conn.execute(
                text(
                    "SELECT message_id, direction, sent_at, language, subject, body"
                    " FROM correspondence WHERE invoice_number = :n ORDER BY sent_at, message_id"
                ),
                {"n": invoice_number},
            )
            .mappings()
            .all()
        )
        classifications = (
            conn.execute(
                text(
                    "SELECT c.message_id, c.label, c.confidence, c.needs_review, c.status"
                    " FROM classifications c JOIN correspondence m ON m.message_id = c.message_id"
                    " WHERE m.invoice_number = :n ORDER BY c.message_id"
                ),
                {"n": invoice_number},
            )
            .mappings()
            .all()
        )
        drafts = (
            conn.execute(
                text(
                    "SELECT draft_id, tone, subject, body, generator, violations_json, status,"
                    " created_at FROM drafts WHERE EXISTS"
                    " (SELECT 1 FROM json_each(invoice_numbers_json) WHERE value = :n)"
                    " ORDER BY created_at, draft_id"
                ),
                {"n": invoice_number},
            )
            .mappings()
            .all()
        )
        case = (
            conn.execute(
                text(
                    "SELECT state, next_followup, updated_at FROM cases WHERE invoice_number = :n"
                ),
                {"n": invoice_number},
            )
            .mappings()
            .first()
        )
        audit = _audit(conn, invoice_number, kinds)
    return {
        "as_of": as_of.isoformat(),
        "invoice_number": row.invoice_number,
        "display_invoice_number": row.display_invoice_number,
        "invoice_type": row.invoice_type,
        "customer_id": row.customer_id,
        "customer_name": snap.customer_name(row.customer_id),
        "invoice_date": row.invoice_date.isoformat(),
        "due_date": row.due_date.isoformat(),
        "amount": money(row.amount),
        "open_amount": money(row.open_amount),
        "days_overdue": row.days_overdue,
        "bucket": row.bucket,
        "settled_date": None if row.settled_date is None else row.settled_date.isoformat(),
        "po_number": row.po_number,
        "case": None if case is None else dict(case),
        "payments": [
            {
                "payment_id": p.payment_id,
                "payment_date": p.payment_date.isoformat(),
                "payment_type": p.payment_type,
                "amount": money(p.amount),
            }
            for p in payments
        ],
        "events": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "event_date": e.event_date.isoformat(),
                "label": e.label,
                "promise_date": None if e.promise_date is None else e.promise_date.isoformat(),
            }
            for e in events
        ],
        "messages": [dict(m) for m in messages],
        "classifications": [
            {**dict(c), "confidence": None if c["confidence"] is None else str(c["confidence"])}
            for c in classifications
        ],
        "drafts": [
            {
                "draft_id": d["draft_id"],
                "tone": d["tone"],
                "subject": d["subject"],
                "body": d["body"],
                "generator": d["generator"],
                "violations": json.loads(d["violations_json"]),
                "status": d["status"],
                "created_at": d["created_at"],
            }
            for d in drafts
        ],
        "audit": audit,
    }
