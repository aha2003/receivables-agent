"""Human decisions on the agent's approval items (SPEC §18.11, §18.4, §18.8; D-038).

Approving a ``send_email`` item:

- at L0 and L1, marks the draft ``approved``; the person sends it from their own email and
  marks it sent (nothing is sent by the system);
- at L2, it is a deliberate human override of the policy's denial: the draft is delivered
  through the sandbox gateway (to the customer's stored ``contact_email`` only) and the
  decision is audited with the denial reasons. ``POLICY_KILL_SWITCH`` and
  ``POLICY_RECIPIENT_NOT_ALLOWED`` can never be overridden, nor can anything while the kill
  switch is on now.

An ``llm`` draft with violations is never approved (§9). Approving an ``allocation`` item waits
for S3.11; rejecting any item only records the decision. The actor is the signed-in user.
"""

import json
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from receivables.adapters.clock import Clock, utc_timestamp
from receivables.adapters.config import Config
from receivables.adapters.db.controls import kill_switch_on
from receivables.adapters.db.models import Approval, Customer, Draft
from receivables.adapters.db.repo import write_audit
from receivables.adapters.mail.sandbox import SandboxMailGateway

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
        if config.agent.autonomy_level == 2:
            mail = _override(conn, approval_id, draft, payload, config=config, clock=clock)
        if draft.status == "draft":
            conn.execute(
                update(Draft)
                .where(Draft.draft_id == draft.draft_id)
                .values(status="approved", approved_by=actor, approved_at=utc_timestamp(clock))
            )
        _settle(conn, approval_id, "approved", clock=clock, actor=actor)
        _audit(conn, clock, actor, approval_id, "approval.approved", item, payload, mail)
        return result | {"status": "approved", "decided_by": actor, "mail": mail}


def _override(
    conn: Connection,
    approval_id: str,
    draft: Any,
    payload: dict[str, Any],
    *,
    config: Config,
    clock: Clock,
) -> dict[str, Any]:
    """L2: deliver a policy-denied send through the sandbox, unless it can never be overridden."""
    reasons = set(payload.get("reasons") or [])
    if reasons & NEVER_OVERRIDDEN or kill_switch_on(conn, config):
        raise ApprovalError(
            409, "conflict", "The kill switch or the recipient rule can never be overridden."
        )
    to = conn.execute(
        select(Customer.contact_email).where(Customer.customer_id == draft.customer_id)
    ).scalar_one_or_none()
    if not to:
        raise ApprovalError(409, "conflict", "The customer has no stored contact address.")
    sent = SandboxMailGateway(conn).send(
        mail_id=f"{approval_id}-M", draft_id=draft.draft_id, to=to, at=clock.now()
    )
    if not sent.delivered:  # pragma: no cover - the address is the stored one
        raise ApprovalError(409, "conflict", "The gateway refused the recipient.")
    return {"mail_id": sent.mail_id, "status": sent.status, "gateway": "sandbox"}


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
