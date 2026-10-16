"""The sandbox mail gateway (SPEC §18.8; T-MAIL-01, T-MAIL-02; D-038).

``SandboxMailGateway.send`` writes one ``mail_outbox`` row and opens no network connection: it
imports no network client (a static test checks this). The recipient must equal the customer's
stored ``contact_email``, read here from the database and never taken from the caller's
judgement: anything else is written with status ``rejected`` and delivers nothing. There is no
SMTP gateway: ``config.yaml`` refuses ``mail.enabled`` and ``gateway: smtp`` (CLAUDE.md).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, insert, select

from receivables.adapters.clock import utc_timestamp_of
from receivables.adapters.db.models import Customer, Draft, MailOutbox

__all__ = ["DELIVERED", "REJECTED", "MailResult", "SandboxMailGateway"]

DELIVERED = "delivered_sandbox"
REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class MailResult:
    mail_id: str
    status: str  # DELIVERED or REJECTED

    @property
    def delivered(self) -> bool:
        return self.status == DELIVERED


class SandboxMailGateway:
    name = "sandbox"

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def send(self, *, mail_id: str, draft_id: str, to: str, at: datetime) -> MailResult:
        """Deliver ``draft_id`` to ``to`` in the sandbox, in the caller's transaction."""
        draft = self._conn.execute(select(Draft).where(Draft.draft_id == draft_id)).one()
        allowed = self._conn.execute(
            select(Customer.contact_email).where(Customer.customer_id == draft.customer_id)
        ).scalar_one_or_none()
        ok = bool(allowed) and to.strip().casefold() == str(allowed).strip().casefold()
        status = DELIVERED if ok else REJECTED
        self._conn.execute(
            insert(MailOutbox).values(
                mail_id=mail_id,
                draft_id=draft.draft_id,
                to_address=to,
                subject=draft.subject,
                body=draft.body,
                gateway=self.name,
                status=status,
                created_at=utc_timestamp_of(at),
            )
        )
        return MailResult(mail_id, status)
