"""Classifying stored messages, and accepting or rejecting the result (SPEC §7, §9; D-034).

``classify_stored`` reads an inbound message, asks the LLM (behind redaction, with the §7.2
retry and fallback), verifies the extraction against the ledger (§7.4), resolves the promised
date from ``sent_at`` (§7.2) and stores a ``pending`` classification with an audit row. The LLM
call happens outside any transaction; the write re-checks the status.

``accept`` applies the §7.1 rule in one transaction: a dispute label creates
``DISPUTE_OPENED``, ``PAYMENT_PROMISED`` creates ``PROMISE_TO_PAY``, other labels create
nothing; the event is dated on the message's ``sent_at`` day (Asia/Dubai). ``reject`` only
changes the status. Every change writes one audit row; no audit row or log line holds message
text.
"""

import json
import logging
from datetime import date, datetime
from fractions import Fraction
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from receivables.adapters.clock import DUBAI, Clock, utc_timestamp
from receivables.adapters.db.models import (
    Classification,
    Correspondence,
    Customer,
    Event,
    Invoice,
)
from receivables.adapters.db.repo import load_invoices, load_payments, write_audit
from receivables.adapters.llm.classification import classify_message, to_extraction
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.schemas import LLMCallFailed
from receivables.core.dates import resolve_message_date
from receivables.core.injection import injection_signals
from receivables.core.ledger import build_ledger
from receivables.core.money import from_fils
from receivables.core.validation import DISPUTE_EVENT_LABELS, DISPUTE_LABELS
from receivables.core.verify import Flag, verify_extraction

__all__ = [
    "ClassificationError",
    "accept",
    "classification_json",
    "classify_stored",
    "reject",
]

log = logging.getLogger("receivables.classification")
EVENT_PREFIX = "CLS-"  # events created by accepting a classification


class ClassificationError(Exception):
    """A refusal with its HTTP status and error code (never message text)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _message(conn: Connection, message_id: str) -> Any:
    row = conn.execute(
        select(Correspondence.__table__).where(Correspondence.message_id == message_id)
    ).one_or_none()
    if row is None:
        raise ClassificationError(404, "not_found", "No message with that id.")
    return row


def _stored(conn: Connection, message_id: str) -> Any:
    return conn.execute(
        select(Classification.__table__).where(Classification.message_id == message_id)
    ).one_or_none()


def _sent_on(sent_at: str) -> date:
    """The message's calendar day in Asia/Dubai."""
    return datetime.fromisoformat(sent_at).astimezone(DUBAI).date()


def classification_json(row: Any) -> dict[str, Any]:
    """The §9 classification object."""
    extraction = json.loads(row.extraction_json)
    difference = extraction.get("amount_difference_fils")
    return {
        "message_id": row.message_id,
        "label": row.label,
        "confidence": row.confidence,
        "needs_review": bool(row.needs_review),
        "flags": json.loads(row.flags_json),
        "amount_difference": None if difference is None else str(from_fils(difference)),
        "amount_difference_fils": difference,
        "promised_payment_date": extraction.get("resolved_promised_date"),
        "status": row.status,
        "model": row.model,
        "error": extraction.get("error"),
        "invoice_numbers": extraction.get("invoice_numbers", []),
        "matched_invoices": extraction.get("matched_invoices", []),
        "evidence_quote": extraction.get("evidence_quote", ""),
        "summary_en": extraction.get("summary_en", ""),
        "created_at": row.created_at,
    }


def classify_stored(
    engine: Engine,
    message_id: str,
    *,
    client: RedactingClient,
    review_threshold: Fraction,
    clock: Clock,
    actor: str,
) -> dict[str, Any]:
    with engine.connect() as conn:
        message = _message(conn, message_id)
        existing = _stored(conn, message_id)
        if message.direction != "inbound":
            raise ClassificationError(409, "conflict", "Only inbound messages are classified.")
        if not message.body:
            raise ClassificationError(409, "conflict", "The message has no body to classify.")
        if existing is not None and existing.status != "pending":
            raise ClassificationError(409, "conflict", f"Already {existing.status}.")
        customer = message.customer_id
        if customer is None and message.invoice_number is not None:
            customer = conn.execute(
                select(Invoice.customer_id).where(Invoice.invoice_number == message.invoice_number)
            ).scalar_one_or_none()
        ledger = build_ledger(load_invoices(conn), load_payments(conn), clock.today())
        contact = conn.execute(
            select(Customer.contact_email).where(Customer.customer_id == customer)
        ).scalar_one_or_none()

    # The LLM call holds no database transaction.
    language = message.language if message.language in {"en", "ar"} else None
    outcome = classify_message(client, message.body, language)
    result = outcome.classification
    sent_on = _sent_on(message.sent_at)
    try:
        sent_text: str | None = client.prepared_text(message.body)
    except LLMCallFailed:  # names unreadable: classify already fell back to UNCLEAR
        sent_text = None
    verification = verify_extraction(
        to_extraction(result),
        message=message.body,
        sent_text=sent_text,
        ledger=ledger,
        message_customer_id=customer,
        review_threshold=review_threshold,
    )
    # §18.9, D-039: a deterministic warning on the original subject and body, whatever the
    # model answered. It only routes the message to review; it is not the defence.
    suspected = tuple(
        Flag("INSTRUCTION_INJECTION_SUSPECTED", field=signal)
        for signal in injection_signals(
            f"{message.subject or ''}\n{message.body}", allowed_addresses=[contact or ""]
        )
    )
    flags = verification.flags + suspected
    promised = (
        resolve_message_date(result.promised_payment_date, sent_on)
        if result.promised_payment_date
        else None
    )
    # The model's invoice may carry an event only when the message has none of its own, it is
    # the single match, and code raised no doubt about any invoice reference (D-034).
    doubtful = any(
        f.code in {"UNMATCHED_INVOICE", "CUSTOMER_INVOICE_MISMATCH"}
        or (f.code == "UNVERIFIED_EXTRACTION" and f.field == "invoice_numbers")
        for f in verification.flags
    )
    event_invoice = (
        verification.matched[0] if len(verification.matched) == 1 and not doubtful else None
    )
    extraction = result.model_dump(mode="json") | {
        "resolved_promised_date": promised.isoformat() if promised else None,
        "matched_invoices": list(verification.matched),
        "event_invoice": event_invoice,
        "amount_difference_fils": verification.amount_difference,
        "error": outcome.error,
        "attempts": outcome.attempts,
    }
    values = {
        "message_id": message_id,
        "label": result.label,
        "confidence": result.confidence,
        "extraction_json": json.dumps(extraction, ensure_ascii=False, sort_keys=True),
        "flags_json": json.dumps([f.as_dict() for f in flags], sort_keys=True),
        "needs_review": int(
            verification.needs_review
            or bool(suspected)
            or outcome.needs_review
            # §7.4: a promise without a date needs review; a date code cannot read has none.
            or (result.label == "PAYMENT_PROMISED" and promised is None)
        ),
        "status": "pending",
        "model": client.model,
        "created_at": utc_timestamp(clock),
    }
    with engine.connect().execution_options(sqlite_begin="IMMEDIATE") as conn, conn.begin():
        before = _stored(conn, message_id)
        if before is not None and before.status != "pending":  # accepted or rejected meanwhile
            raise ClassificationError(409, "conflict", f"Already {before.status}.")
        if _message(conn, message_id) != message:  # re-imported during the model call
            raise ClassificationError(
                409, "conflict", "The message changed while it was classified; classify again."
            )
        stmt = sqlite_insert(Classification).values(values)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["message_id"],
                set_={k: stmt.excluded[k] for k in values if k != "message_id"},
            )
        )
        write_audit(
            conn,
            clock,
            actor=actor,
            action="classification.created",
            entity_type="message",
            entity_id=message_id,
            before=None if before is None else _summary(before.label, before),
            after={
                "label": result.label,
                "confidence": result.confidence,
                "needs_review": bool(values["needs_review"]),
                "flags": [f.code for f in verification.flags],
                "error": outcome.error,
                "model": client.model,
            },
        )
        row = _stored(conn, message_id)
    log.info(
        "classification.created",
        extra={
            "message_id": message_id,
            "label": result.label,
            "needs_review": bool(values["needs_review"]),
            "flags": [f.code for f in verification.flags],
        },
    )
    return classification_json(row)


def _summary(label: str, row: Any) -> dict[str, Any]:
    return {"label": label, "status": row.status, "needs_review": bool(row.needs_review)}


def accept(
    engine: Engine,
    message_id: str,
    *,
    label_override: str | None,
    promised_payment_date: date | None,
    clock: Clock,
    actor: str,
) -> dict[str, Any]:
    if label_override is not None and label_override not in DISPUTE_LABELS:
        raise ClassificationError(422, "invalid_request", "label_override is not a §7.1 label.")
    with engine.connect().execution_options(sqlite_begin="IMMEDIATE") as conn, conn.begin():
        message = _message(conn, message_id)
        row = _stored(conn, message_id)
        if row is None:
            raise ClassificationError(409, "conflict", "The message is not classified yet.")
        if row.status != "pending":
            raise ClassificationError(409, "conflict", f"Already {row.status}.")
        extraction = json.loads(row.extraction_json)
        label = label_override or row.label
        event = _event_for(conn, message, extraction, label, promised_payment_date)
        if event is not None:
            conn.execute(insert(Event).values(event))
        conn.execute(
            update(Classification)
            .where(Classification.message_id == message_id)
            .values(status="accepted", label=label)
        )
        write_audit(
            conn,
            clock,
            actor=actor,
            action="classification.accepted",
            entity_type="message",
            entity_id=message_id,
            before=_summary(row.label, row),
            after={
                "label": label,
                "status": "accepted",
                "label_override": label_override is not None,
                "event": event,
            },
        )
        stored = _stored(conn, message_id)
    return {"classification": classification_json(stored), "event": event}


def _event_for(
    conn: Connection,
    message: Any,
    extraction: dict[str, Any],
    label: str,
    promised: date | None,
) -> dict[str, Any] | None:
    """The §7.1 "Creates on accept" event, or ``None`` for labels that create nothing."""
    if label in DISPUTE_EVENT_LABELS:
        event_type = "DISPUTE_OPENED"
    elif label == "PAYMENT_PROMISED":
        event_type = "PROMISE_TO_PAY"
    else:
        return None
    invoice = message.invoice_number or extraction.get("event_invoice")
    if invoice is None:
        raise ClassificationError(
            409, "conflict", "No single invoice to attach the event to; check the references."
        )
    event_date = _sent_on(message.sent_at)
    event: dict[str, Any] = {
        "event_id": f"{EVENT_PREFIX}{message.message_id}",
        "invoice_number": invoice,
        "event_type": event_type,
        "event_date": event_date.isoformat(),
        "label": label if event_type == "DISPUTE_OPENED" else None,
        "promise_date": None,
        "source": "classification",
    }
    if event_type == "PROMISE_TO_PAY":
        resolved = extraction.get("resolved_promised_date")
        promise = promised or (date.fromisoformat(resolved) if resolved else None)
        if promise is None:
            raise ClassificationError(
                422, "invalid_request", "PAYMENT_PROMISED needs a promised_payment_date."
            )
        if promise < event_date:
            raise ClassificationError(
                422, "invalid_request", "The promised date is before the message date."
            )
        event["promise_date"] = promise.isoformat()
    if conn.execute(select(Event.event_id).where(Event.event_id == event["event_id"])).first():
        raise ClassificationError(409, "conflict", "An event for this message already exists.")
    return event


def reject(engine: Engine, message_id: str, *, clock: Clock, actor: str) -> dict[str, Any]:
    with engine.connect().execution_options(sqlite_begin="IMMEDIATE") as conn, conn.begin():
        _message(conn, message_id)
        row = _stored(conn, message_id)
        if row is None:
            raise ClassificationError(409, "conflict", "The message is not classified yet.")
        if row.status != "pending":
            raise ClassificationError(409, "conflict", f"Already {row.status}.")
        conn.execute(
            update(Classification)
            .where(Classification.message_id == message_id)
            .values(status="rejected")
        )
        write_audit(
            conn,
            clock,
            actor=actor,
            action="classification.rejected",
            entity_type="message",
            entity_id=message_id,
            before=_summary(row.label, row),
            after={"label": row.label, "status": "rejected"},
        )
        stored = _stored(conn, message_id)
    return classification_json(stored)


def inbox_items(engine: Engine) -> list[dict[str, Any]]:
    """Inbound messages to work on (§10 Inbox): unclassified first, then pending ones,
    those that need review before the others; accepted and rejected ones are done."""
    c, k = Correspondence.__table__, Classification.__table__
    stmt = (
        select(c, k.c.message_id.label("classified"))
        .select_from(c.outerjoin(k, k.c.message_id == c.c.message_id))
        .where(c.c.direction == "inbound")
        .where((k.c.message_id.is_(None)) | (k.c.status == "pending"))
        .order_by(c.c.sent_at, c.c.message_id)
    )
    items: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for row in conn.execute(stmt).all():
            stored = _stored(conn, row.message_id) if row.classified else None
            items.append(
                {
                    "message_id": row.message_id,
                    "invoice_number": row.invoice_number,
                    "customer_id": row.customer_id,
                    "sent_at": datetime.fromisoformat(row.sent_at).astimezone(DUBAI),
                    "language": row.language,
                    "subject": row.subject,
                    "body": row.body,
                    "classification": None if stored is None else classification_json(stored),
                }
            )

    def order(item: dict[str, Any]) -> int:
        result = item["classification"]
        return 0 if result is None else (1 if result["needs_review"] else 2)

    return sorted(items, key=order)  # stable: sent_at order within each group
