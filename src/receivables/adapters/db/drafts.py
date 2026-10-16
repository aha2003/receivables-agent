"""Drafts made and decided by people (SPEC §9, §8, §18.4; T-API-06; D-038).

``create_draft`` builds the §8.1 fact block for overdue invoices of one customer (as of the
clock's today), drafts with the drafting loop (the LLM in replay or live mode, else the §8.4
template) and saves it as ``draft``. A person then approves it (never possible for an ``llm``
draft with violations, §9), rejects it, or marks an approved draft as sent from their own email
(L0 and L1: nothing is sent by the system, §18.4). Every change writes one audit row with
before and after; the actor is always the signed-in user (D-038 option 1a). Approving or
rejecting a draft also decides the agent's pending ``send_email`` approval items for it.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from receivables.adapters.clock import Clock, utc_timestamp, utc_timestamp_of
from receivables.adapters.config import Config
from receivables.adapters.db.agent_store import DbAgentStore
from receivables.adapters.db.models import Approval, Draft
from receivables.adapters.db.repo import load_agent_facts, write_audit
from receivables.agent.drafting import DraftWriter, generate_draft
from receivables.core.drafts import build_fact_block
from receivables.core.queue import draft_tone

__all__ = [
    "DraftError",
    "approve_draft",
    "create_draft",
    "decide_send_approvals",
    "draft_json",
    "mark_draft_sent",
    "reject_draft",
]

MAX_INVOICES = 10
MAX_REASON_CHARS = 500


class DraftError(Exception):
    """A refusal with its HTTP status and error code (never draft text)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def draft_json(row: Any) -> dict[str, Any]:
    return {
        "draft_id": row.draft_id,
        "invoice_numbers": json.loads(row.invoice_numbers_json),
        "customer_id": row.customer_id,
        "tone": row.tone,
        "subject": row.subject,
        "body": row.body,
        "generator": row.generator,
        "violations": json.loads(row.violations_json),
        "status": row.status,
        "created_at": row.created_at,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "sent_at": row.sent_at,
    }


def _load(conn: Connection, draft_id: str) -> Any:
    row = conn.execute(select(Draft.__table__).where(Draft.draft_id == draft_id)).one_or_none()
    if row is None:
        raise DraftError(404, "not_found", "No draft with that id.")
    return row


def _summary(row: Any) -> dict[str, Any]:
    return {
        "status": row.status,
        "generator": row.generator,
        "violations": json.loads(row.violations_json),
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "sent_at": row.sent_at,
    }


def _write(conn: Connection) -> Any:
    return conn.execution_options(sqlite_begin="IMMEDIATE")


def create_draft(
    engine: Engine,
    invoice_numbers: list[str],
    tone: int | None,
    *,
    config: Config,
    clock: Clock,
    actor: str,
    drafter: DraftWriter | None,
) -> dict[str, Any]:
    if not 1 <= len(invoice_numbers) <= MAX_INVOICES or len(set(invoice_numbers)) != len(
        invoice_numbers
    ):
        raise DraftError(422, "invalid_request", "Give 1 to 10 distinct invoice numbers.")
    if tone is not None and tone not in (1, 2, 3):
        raise DraftError(422, "invalid_request", "tone is 1, 2 or 3.")
    with engine.connect() as conn:
        facts = load_agent_facts(conn, clock.today(), config.engine)
    missing = [n for n in invoice_numbers if n not in facts.rows]
    if missing:
        raise DraftError(404, "not_found", "Unknown invoice number.")
    rows = [facts.rows[n] for n in invoice_numbers]
    if len({r.customer_id for r in rows}) != 1:
        raise DraftError(422, "invalid_request", "The invoices must share one customer.")
    labels = {facts.scored[n].dispute_label if n in facts.scored else None for n in invoice_numbers}
    label = labels.pop() if len(labels) == 1 else None
    customer = facts.customers[rows[0].customer_id]
    try:
        block = build_fact_block(
            rows,
            customer_name=customer.customer_name,
            sender_name=config.drafts.sender_name,
            as_of=facts.as_of,
            tone=tone or draft_tone(max(r.days_overdue or 0 for r in rows)),
            dispute_label=label,
        )
    except ValueError:
        raise DraftError(
            422, "invalid_request", "Every invoice must be open and overdue."
        ) from None
    # The model call (if any) holds no database transaction.
    outcome = generate_draft(
        block,
        drafter,
        max_regenerations=config.drafts.max_regenerations,
        max_words=config.drafts.max_words,
        invoice_regex=config.drafts.invoice_number_regex,
        recipient=customer.contact_email,
        sender_domain=config.drafts.sender_domain,
    )
    draft_id = f"DRF-{uuid.uuid4().hex[:12]}"
    with _write(engine.connect()) as conn, conn.begin():
        DbAgentStore(conn, clock).add_draft(
            draft_id,
            fields={
                "invoice_numbers": list(invoice_numbers),
                "customer_id": customer.customer_id,
                "tone": block.tone,
                "subject": outcome.subject,
                "body": outcome.body,
                "facts": block.to_json(),
                "generator": outcome.generator,
                "violations": list(outcome.violations),
            },
        )
        write_audit(
            conn,
            clock,
            actor=actor,
            action="draft.created",
            entity_type="draft",
            entity_id=draft_id,
            before=None,
            after={
                "invoice_numbers": list(invoice_numbers),
                "tone": block.tone,
                "generator": outcome.generator,
                "violations": list(outcome.violations),
                "attempts": [list(a) for a in outcome.attempts],
            },
        )
        return draft_json(_load(conn, draft_id))


def decide_send_approvals(
    conn: Connection, draft_id: str, status: str, *, clock: Clock, actor: str
) -> list[str]:
    """Settle the agent's pending ``send_email`` items for a draft a person just decided."""
    pending = conn.execute(
        select(Approval.approval_id, Approval.payload_json).where(
            Approval.kind == "send_email", Approval.status == "pending"
        )
    ).all()
    decided = [
        a.approval_id for a in pending if json.loads(a.payload_json).get("draft_id") == draft_id
    ]
    for approval_id in decided:
        conn.execute(
            update(Approval)
            .where(Approval.approval_id == approval_id)
            .values(status=status, decided_by=actor, decided_at=utc_timestamp(clock))
        )
        write_audit(
            conn, clock, actor=actor, action=f"approval.{status}", entity_type="approval",
            entity_id=approval_id, before={"status": "pending"},
            after={"status": status, "draft_id": draft_id, "via": "draft"},
        )  # fmt: skip
    return decided


def approve_draft(engine: Engine, draft_id: str, *, clock: Clock, actor: str) -> dict[str, Any]:
    with _write(engine.connect()) as conn, conn.begin():
        row = _load(conn, draft_id)
        if row.status != "draft":
            raise DraftError(409, "conflict", f"The draft is {row.status}, not draft.")
        if row.generator == "llm" and json.loads(row.violations_json):
            raise DraftError(409, "conflict", "A model draft with violations cannot be approved.")
        conn.execute(
            update(Draft)
            .where(Draft.draft_id == draft_id)
            .values(status="approved", approved_by=actor, approved_at=utc_timestamp(clock))
        )
        after = _load(conn, draft_id)
        write_audit(
            conn, clock, actor=actor, action="draft.approved", entity_type="draft",
            entity_id=draft_id, before=_summary(row), after=_summary(after),
        )  # fmt: skip
        decide_send_approvals(conn, draft_id, "approved", clock=clock, actor=actor)
        return draft_json(after)


def reject_draft(
    engine: Engine, draft_id: str, reason: str, *, clock: Clock, actor: str
) -> dict[str, Any]:
    reason = reason.strip()
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise DraftError(422, "invalid_request", "Give a reason of 1 to 500 characters.")
    with _write(engine.connect()) as conn, conn.begin():
        row = _load(conn, draft_id)
        if row.status not in ("draft", "approved"):
            raise DraftError(409, "conflict", f"The draft is {row.status}.")
        conn.execute(update(Draft).where(Draft.draft_id == draft_id).values(status="rejected"))
        after = _load(conn, draft_id)
        write_audit(
            conn, clock, actor=actor, action="draft.rejected", entity_type="draft",
            entity_id=draft_id, before=_summary(row), after=_summary(after) | {"reason": reason},
        )  # fmt: skip
        decide_send_approvals(conn, draft_id, "rejected", clock=clock, actor=actor)
        return draft_json(after)


def mark_draft_sent(
    engine: Engine, draft_id: str, sent_at: datetime, *, clock: Clock, actor: str
) -> dict[str, Any]:
    if sent_at.tzinfo is None:
        raise DraftError(422, "invalid_request", "sent_at needs a time zone.")
    if sent_at > clock.now():
        raise DraftError(422, "invalid_request", "sent_at may not be in the future.")
    with _write(engine.connect()) as conn, conn.begin():
        row = _load(conn, draft_id)
        if row.status != "approved":
            raise DraftError(
                409, "conflict", f"Only an approved draft is sent; it is {row.status}."
            )
        if row.approved_at and utc_timestamp_of(sent_at) < row.approved_at:
            raise DraftError(422, "invalid_request", "sent_at is before the approval.")
        conn.execute(
            update(Draft)
            .where(Draft.draft_id == draft_id)
            .values(status="sent_manually", sent_at=utc_timestamp_of(sent_at))
        )
        after = _load(conn, draft_id)
        write_audit(
            conn, clock, actor=actor, action="draft.sent_manually", entity_type="draft",
            entity_id=draft_id, before=_summary(row), after=_summary(after),
        )  # fmt: skip
        return draft_json(after)
