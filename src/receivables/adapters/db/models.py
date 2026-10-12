"""SQLAlchemy models mirroring the SPEC §5 schema, for query code.

The Alembic migration is the source of truth for STRICT, CHECK constraints, defaults and
triggers; tests/db/test_schema.py checks these models match the migrated columns, keys and
nullability. Amounts are integer fils; dates and timestamps are ISO text.
"""

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AuditLog",
    "Base",
    "Classification",
    "Correspondence",
    "Customer",
    "Draft",
    "Event",
    "Import",
    "Invoice",
    "Outcome",
    "Payment",
    "User",
]


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    user_id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    role: Mapped[str]
    created_at: Mapped[str]


class Customer(Base):
    __tablename__ = "customers"
    customer_id: Mapped[str] = mapped_column(primary_key=True)
    customer_name: Mapped[str]
    trn: Mapped[str | None]
    tin: Mapped[str | None]
    trade_license_no: Mapped[str | None]
    payment_terms_days: Mapped[int | None]
    contact_email: Mapped[str | None]


class Invoice(Base):
    __tablename__ = "invoices"
    invoice_number: Mapped[str] = mapped_column(primary_key=True)
    display_invoice_number: Mapped[str | None]
    invoice_type: Mapped[str | None]
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    invoice_date: Mapped[str]
    due_date: Mapped[str]
    amount_fils: Mapped[int]
    currency: Mapped[str]
    po_number: Mapped[str | None]


class Payment(Base):
    __tablename__ = "payments"
    payment_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(ForeignKey("invoices.invoice_number"))
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.customer_id"))
    payment_date: Mapped[str]
    amount_fils: Mapped[int]
    payment_type: Mapped[str]


class Event(Base):
    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(ForeignKey("invoices.invoice_number"))
    event_type: Mapped[str]
    event_date: Mapped[str]
    label: Mapped[str | None]
    promise_date: Mapped[str | None]
    source: Mapped[str]


class Correspondence(Base):
    __tablename__ = "correspondence"
    message_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(ForeignKey("invoices.invoice_number"))
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.customer_id"))
    direction: Mapped[str]
    sent_at: Mapped[str]
    language: Mapped[str | None]
    subject: Mapped[str | None]
    body: Mapped[str | None]


class Classification(Base):
    __tablename__ = "classifications"
    message_id: Mapped[str] = mapped_column(
        ForeignKey("correspondence.message_id"), primary_key=True
    )
    label: Mapped[str]
    confidence: Mapped[float | None]  # model confidence score, not money
    extraction_json: Mapped[str]
    flags_json: Mapped[str]
    needs_review: Mapped[int]
    status: Mapped[str]
    model: Mapped[str | None]
    created_at: Mapped[str]


class Draft(Base):
    __tablename__ = "drafts"
    draft_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_numbers_json: Mapped[str]
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    tone: Mapped[int]
    subject: Mapped[str]
    body: Mapped[str]
    facts_json: Mapped[str]
    generator: Mapped[str]
    violations_json: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[str]
    approved_by: Mapped[str | None]
    approved_at: Mapped[str | None]
    sent_at: Mapped[str | None]


class Outcome(Base):
    __tablename__ = "outcomes"
    outcome_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(ForeignKey("invoices.invoice_number"))
    recorded_at: Mapped[str]
    outcome_type: Mapped[str]
    note: Mapped[str | None]


class AuditLog(Base):
    __tablename__ = "audit_log"
    audit_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str]
    actor: Mapped[str]
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[str]
    before_json: Mapped[str | None]
    after_json: Mapped[str | None]


class Import(Base):
    __tablename__ = "imports"
    import_id: Mapped[str] = mapped_column(primary_key=True)
    file_kind: Mapped[str]
    filename: Mapped[str | None]
    sha256: Mapped[str]
    mapping_hash: Mapped[str]
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    reprocess_of: Mapped[str | None]
    rows_total: Mapped[int | None]
    rows_ok: Mapped[int | None]
    rows_error: Mapped[int | None]
    status: Mapped[str]
    report_json: Mapped[str | None]
    created_at: Mapped[str]
