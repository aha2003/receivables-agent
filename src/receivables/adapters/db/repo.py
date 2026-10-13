"""Repository functions and the audit writer (SPEC §3.4, §5).

Reads map database rows to the pure core records (amounts in integer fils, dates as ``date``).
``write_audit`` is the single way to append to ``audit_log``; its timestamp comes from the
injected clock and the table's triggers keep it append-only.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, Engine, func, insert, select

from receivables.adapters.clock import Clock, utc_timestamp
from receivables.adapters.db.models import AuditLog, Base, Customer, Event, Invoice, Payment, User
from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.scoring import EventRecord
from receivables.core.validation import CustomerRow

__all__ = [
    "LedgerInputs",
    "UserRecord",
    "get_user",
    "get_user_by_username",
    "insert_user",
    "load_customers",
    "load_events",
    "load_invoices",
    "load_ledger_inputs",
    "load_payments",
    "table_counts",
    "write_audit",
]


@dataclass(frozen=True, slots=True)
class LedgerInputs:
    invoices: list[InvoiceRecord]
    payments: list[PaymentRecord]
    events: list[EventRecord]


def load_customers(conn: Connection) -> list[CustomerRow]:
    rows = conn.execute(select(Customer).order_by(Customer.customer_id)).all()
    return [
        CustomerRow(
            customer_id=r.customer_id,
            customer_name=r.customer_name,
            trn=r.trn,
            tin=r.tin,
            trade_license_no=r.trade_license_no,
            payment_terms_days=r.payment_terms_days,
            contact_email=r.contact_email,
        )
        for r in rows
    ]


def load_invoices(conn: Connection) -> list[InvoiceRecord]:
    rows = conn.execute(select(Invoice).order_by(Invoice.invoice_number)).all()
    return [
        InvoiceRecord(
            invoice_number=r.invoice_number,
            customer_id=r.customer_id,
            invoice_date=date.fromisoformat(r.invoice_date),
            due_date=date.fromisoformat(r.due_date),
            amount=r.amount_fils,
            display_invoice_number=r.display_invoice_number,
            invoice_type=r.invoice_type,
            po_number=r.po_number,
        )
        for r in rows
    ]


def load_payments(conn: Connection) -> list[PaymentRecord]:
    rows = conn.execute(select(Payment).order_by(Payment.payment_id)).all()
    return [
        PaymentRecord(
            payment_id=r.payment_id,
            invoice_number=r.invoice_number,
            customer_id=r.customer_id,
            payment_date=date.fromisoformat(r.payment_date),
            amount=r.amount_fils,
            payment_type=r.payment_type,
        )
        for r in rows
    ]


def load_events(conn: Connection) -> list[EventRecord]:
    rows = conn.execute(select(Event).order_by(Event.event_id)).all()
    return [
        EventRecord(
            event_id=r.event_id,
            invoice_number=r.invoice_number,
            event_type=r.event_type,
            event_date=date.fromisoformat(r.event_date),
            label=r.label,
            promise_date=date.fromisoformat(r.promise_date) if r.promise_date else None,
        )
        for r in rows
    ]


def load_ledger_inputs(engine: Engine) -> LedgerInputs:
    """Invoices, payments and events read in one transaction (a consistent snapshot)."""
    with engine.connect() as conn, conn.begin():
        return LedgerInputs(
            invoices=load_invoices(conn),
            payments=load_payments(conn),
            events=load_events(conn),
        )


def table_counts(conn: Connection, tables: Iterable[str] | None = None) -> dict[str, int]:
    """Row count per table: the given tables in the given order, or all §5 tables sorted."""
    names = list(tables) if tables is not None else sorted(Base.metadata.tables)
    return {
        name: conn.execute(
            select(func.count()).select_from(Base.metadata.tables[name])
        ).scalar_one()
        for name in names
    }


def _json(value: Mapping[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def write_audit(
    conn: Connection,
    clock: Clock,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> None:
    """Append one audit row in the caller's transaction (timestamp from the injected clock)."""
    conn.execute(
        insert(AuditLog).values(
            ts=utc_timestamp(clock),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_json=_json(before),
            after_json=_json(after),
        )
    )


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: int
    username: str
    password_hash: str
    role: str


def _user(row: Any) -> UserRecord:
    return UserRecord(
        user_id=row.user_id, username=row.username, password_hash=row.password_hash, role=row.role
    )


def get_user(conn: Connection, user_id: int) -> UserRecord | None:
    row = conn.execute(select(User).where(User.user_id == user_id)).one_or_none()
    return None if row is None else _user(row)


def get_user_by_username(conn: Connection, username: str) -> UserRecord | None:
    row = conn.execute(select(User).where(User.username == username)).one_or_none()
    return None if row is None else _user(row)


def insert_user(
    conn: Connection, clock: Clock, *, username: str, password_hash: str, role: str, actor: str
) -> int:
    """Create a user and audit it; the audit row never contains the password hash."""
    result = conn.execute(
        insert(User).values(
            username=username,
            password_hash=password_hash,
            role=role,
            created_at=utc_timestamp(clock),
        )
    )
    user_id = int(result.inserted_primary_key[0])  # pyright: ignore[reportOptionalSubscript]
    write_audit(
        conn,
        clock,
        actor=actor,
        action="user.created",
        entity_type="user",
        entity_id=str(user_id),
        before=None,
        after={"username": username, "role": role},
    )
    return user_id
