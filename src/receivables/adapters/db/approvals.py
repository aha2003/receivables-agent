"""Human decisions on the agent's approval items (SPEC §18.11, §18.4, §18.8; D-038).

Approving a ``send_email`` item:

- from an L0 or L1 run (or with L2 no longer configured), marks the draft ``approved``; the
  person sends it from their own email and marks it sent (nothing is sent by the system);
- for an item an L2 run's policy denied, while L2 is configured, it is a deliberate human
  override: the policy is evaluated again now, and the draft is delivered through the sandbox
  gateway (to the customer's stored ``contact_email`` only), audited with the denial reasons
  saved by the run and those of now, and its ``ASSESSED`` cases move to ``AWAITING_REPLY``
  (§18.2). ``POLICY_KILL_SWITCH`` and ``POLICY_RECIPIENT_NOT_ALLOWED`` can never be overridden,
  nor can a draft that no longer matches today's facts (for example, paid or not overdue).

An ``llm`` draft with violations is never approved (§9). Approving an ``allocation`` item waits
for S3.11; rejecting any item only records the decision. The actor is the signed-in user.
"""

import dataclasses
import json
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, select, update

from receivables.adapters.clock import Clock, utc_timestamp
from receivables.adapters.config import Config
from receivables.adapters.db.agent_store import DbAgentStore, tick_config
from receivables.adapters.db.controls import kill_switch_on
from receivables.adapters.db.models import AgentRun, AgentStep, Approval, Draft
from receivables.adapters.db.repo import load_agent_facts, write_audit
from receivables.adapters.mail.sandbox import DELIVERED, SandboxMailGateway
from receivables.agent.orchestrator import recheck_draft
from receivables.agent.policy import SendDraft, evaluate_send, send_facts_from

__all__ = ["NEVER_OVERRIDDEN", "ApprovalError", "decide_approval"]

NEVER_OVERRIDDEN = frozenset({"POLICY_KILL_SWITCH", "POLICY_RECIPIENT_NOT_ALLOWED"})


class ApprovalError(Exception):
    """A refusal with its HTTP status and error code."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _approval(conn: Connection, approval_id: str) -> Any:
    row = conn.execute(
        select(Approval.__table__).where(Approval.approval_id == approval_id)
    ).one_or_none()
    if row is None:
        raise ApprovalError(404, "not_found", "No approval item with that id.")
    if row.status != "pending":
        raise ApprovalError(409, "conflict", f"Already {row.status}.")
    return row


def _run_level(conn: Connection, step_id: str) -> int:
    """The autonomy level of the run that created the item (not today's configuration)."""
    return conn.execute(
        select(AgentRun.autonomy_level)
        .join(AgentStep, AgentStep.run_id == AgentRun.run_id)
        .where(AgentStep.step_id == step_id)
    ).scalar_one()


def _settle(conn: Connection, approval_id: str, status: str, *, clock: Clock, actor: str) -> None:
    conn.execute(
        update(Approval)
        .where(Approval.approval_id == approval_id)
        .values(status=status, decided_by=actor, decided_at=utc_timestamp(clock))
    )


def decide_approval(
    engine: Engine,
    approval_id: str,
    decision: str,
    *,
    config: Config,
    clock: Clock,
    actor: str,
) -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise ValueError(decision)
    with engine.connect().execution_options(sqlite_begin="IMMEDIATE") as conn, conn.begin():
        item = _approval(conn, approval_id)
        payload = json.loads(item.payload_json)
        result: dict[str, Any] = {"approval_id": approval_id, "kind": item.kind, "mail": None}
        if decision == "reject":
            _settle(conn, approval_id, "rejected", clock=clock, actor=actor)
            _audit(conn, clock, actor, approval_id, "approval.rejected", item, payload, None)
            return result | {"status": "rejected", "decided_by": actor}
        if item.kind != "send_email":
            raise ApprovalError(409, "conflict", "Approving an allocation arrives with S3.11.")
        draft = conn.execute(
            select(Draft.__table__).where(Draft.draft_id == payload["draft_id"])
        ).one_or_none()
        if draft is None or draft.status not in ("draft", "approved"):
            raise ApprovalError(409, "conflict", "The draft is no longer available to send.")
        if draft.generator == "llm" and json.loads(draft.violations_json):
            raise ApprovalError(
                409, "conflict", "A model draft with violations cannot be approved."
            )
        mail = None
        refused = False
        override = (
            config.agent.autonomy_level == 2
            and _run_level(conn, item.step_id) == 2
            and payload.get("policy_allowed") is False
        )
        if override:
            mail = _override(conn, approval_id, draft, payload, config=config, clock=clock)
            # Never expected (the address is the stored one): the gateway's rejected row is
            # kept (option 2a), the item stays pending and the refusal is raised after commit.
            refused = mail["status"] != DELIVERED
            if refused:  # pragma: no cover
                _audit(conn, clock, actor, approval_id, "approval.override_refused", item,
                       payload, mail)  # fmt: skip
        if not refused:
            if draft.status == "draft":
                _approve_draft(conn, draft, clock=clock, actor=actor, approval_id=approval_id)
            if mail is not None:  # §18.2: a sent message moves ASSESSED to AWAITING_REPLY
                DbAgentStore(conn, clock).message_sent(
                    json.loads(draft.invoice_numbers_json), actor=actor, draft_id=draft.draft_id
                )
            _settle(conn, approval_id, "approved", clock=clock, actor=actor)
            _audit(conn, clock, actor, approval_id, "approval.approved", item, payload, mail)
            return result | {"status": "approved", "decided_by": actor, "mail": mail}
    raise ApprovalError(409, "conflict", "The gateway refused the recipient.")  # pragma: no cover


def _approve_draft(
    conn: Connection, draft: Any, *, clock: Clock, actor: str, approval_id: str
) -> None:
    approved_at = utc_timestamp(clock)
    conn.execute(
        update(Draft)
        .where(Draft.draft_id == draft.draft_id)
        .values(status="approved", approved_by=actor, approved_at=approved_at)
    )
    write_audit(
        conn, clock, actor=actor, action="draft.approved", entity_type="draft",
        entity_id=draft.draft_id, before={"status": "draft"},
        after={"status": "approved", "approved_by": actor, "approved_at": approved_at,
               "approval_id": approval_id},
    )  # fmt: skip


def _override(
    conn: Connection,
    approval_id: str,
    draft: Any,
    payload: dict[str, Any],
    *,
    config: Config,
    clock: Clock,
) -> dict[str, Any]:
    """L2: deliver a policy-denied send through the sandbox, unless it can never be overridden.

    The policy is evaluated again now (with the runtime kill switch), so a reason that arose
    after the run also counts; the draft must still match today's facts.
    """
    store = DbAgentStore(conn, clock)
    record = store.get_draft(draft.draft_id)
    if record is None:  # pragma: no cover - read in the same transaction
        raise ApprovalError(409, "conflict", "The draft is no longer available to send.")
    facts = load_agent_facts(conn, clock.today(), config.engine)
    if recheck_draft(facts, record, tick_config(config)):
        raise ApprovalError(
            409, "conflict", "The draft no longer matches today's facts (paid, or not overdue)."
        )
    to = facts.customers[record.customer_id].contact_email
    if not to:
        raise ApprovalError(409, "conflict", "The customer has no stored contact address.")
    l2 = dataclasses.replace(config.agent.l2, kill_switch=kill_switch_on(conn, config))
    now = clock.now()
    local_today = now.astimezone(ZoneInfo(l2.timezone)).date()
    case = send_facts_from(
        facts,
        record.invoice_numbers,
        sends_today=store.sends_on(local_today, l2.timezone),
        timezone=l2.timezone,
        outbound_dates=store.outbound_dates(record.customer_id, l2.timezone),
    )
    send = SendDraft(
        invoice_numbers=record.invoice_numbers, tone=record.tone, recipient=to, violations=()
    )
    reasons_now = evaluate_send(case, send, now, l2).reasons
    reasons = set(payload.get("reasons") or []) | set(reasons_now)
    if reasons & NEVER_OVERRIDDEN:
        raise ApprovalError(
            409, "conflict", "The kill switch or the recipient rule can never be overridden."
        )
    sent = SandboxMailGateway(conn).send(
        mail_id=f"{approval_id}-M", draft_id=draft.draft_id, to=to, at=now
    )
    return {
        "mail_id": sent.mail_id,
        "status": sent.status,
        "gateway": "sandbox",
        "reasons_now": list(reasons_now),
    }


def _audit(
    conn: Connection,
    clock: Clock,
    actor: str,
    approval_id: str,
    action: str,
    item: Any,
    payload: dict[str, Any],
    mail: dict[str, Any] | None,
) -> None:
    write_audit(
        conn,
        clock,
        actor=actor,
        action=action,
        entity_type="approval",
        entity_id=approval_id,
        before={"status": "pending", "kind": item.kind, "reasons": payload.get("reasons", [])},
        after={
            "status": action.removeprefix("approval."),
            "draft_id": payload.get("draft_id"),
            "override": mail is not None,  # an L2 human override of the policy's denial
            "mail": mail,
        },
    )
