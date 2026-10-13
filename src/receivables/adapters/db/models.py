"""SQLAlchemy models mirroring the SPEC §5 and §18.10 schema, for query code.

The Alembic migration is the source of truth for STRICT, CHECK constraints, defaults and
triggers; tests/db/test_schema.py checks these models match the migrated columns, types,
keys, nullability, unique constraints and server defaults. Amounts are integer fils; dates
and timestamps are ISO text.
"""

from sqlalchemy import ForeignKey, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AgentRun",
    "AgentStep",
    "Approval",
    "AuditLog",
    "Base",
    "Case",
    "Classification",
    "Correspondence",
    "Customer",
    "CustomerPlaybook",
    "Draft",
    "Event",
    "Import",
    "Invoice",
    "MailOutbox",
    "Outcome",
    "Payment",
    "Task",
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
    currency: Mapped[str] = mapped_column(server_default="AED")
    po_number: Mapped[str | None]


class Payment(Base):
    __tablename__ = "payments"
    payment_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(ForeignKey("invoices.invoice_number"))
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.customer_id"))
    payment_date: Mapped[str]
    amount_fils: Mapped[int]
    payment_type: Mapped[str] = mapped_column(server_default="payment")


class Event(Base):
    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(ForeignKey("invoices.invoice_number"))
    event_type: Mapped[str]
    event_date: Mapped[str]
    label: Mapped[str | None]
    promise_date: Mapped[str | None]
    source: Mapped[str] = mapped_column(server_default="import")


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
    flags_json: Mapped[str] = mapped_column(server_default="[]")
    needs_review: Mapped[int]
    status: Mapped[str] = mapped_column(server_default="pending")
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
    violations_json: Mapped[str] = mapped_column(server_default="[]")
    status: Mapped[str] = mapped_column(server_default="draft")
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
    __table_args__ = {"sqlite_autoincrement": True}  # noqa: RUF012
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


# --- Agent tables (SPEC §18.10, migration 0002) ---


class Case(Base):
    __tablename__ = "cases"
    invoice_number: Mapped[str] = mapped_column(
        ForeignKey("invoices.invoice_number"), primary_key=True
    )
    state: Mapped[str]
    next_followup: Mapped[str | None]
    updated_at: Mapped[str]


class AgentRun(Base):
    __tablename__ = "agent_runs"
    run_id: Mapped[str] = mapped_column(primary_key=True)
    as_of: Mapped[str]
    now_ts: Mapped[str]
    autonomy_level: Mapped[int]
    planner: Mapped[str]
    fallback: Mapped[int] = mapped_column(server_default=text("0"))
    model: Mapped[str | None]
    cases_considered: Mapped[int | None]
    steps_executed: Mapped[int | None]
    steps_denied: Mapped[int | None]
    started_at: Mapped[str]
    finished_at: Mapped[str | None]


class AgentStep(Base):
    __tablename__ = "agent_steps"
    step_id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"))
    invoice_number: Mapped[str] = mapped_column(ForeignKey("invoices.invoice_number"))
    seq: Mapped[int]
    tool: Mapped[str]
    args_json: Mapped[str]
    reason: Mapped[str | None]
    policy_allowed: Mapped[int | None]
    policy_reasons_json: Mapped[str] = mapped_column(server_default="[]")
    status: Mapped[str]
    result_json: Mapped[str | None]


class Approval(Base):
    __tablename__ = "approvals"
    approval_id: Mapped[str] = mapped_column(primary_key=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("agent_steps.step_id"))
    kind: Mapped[str]
    payload_json: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="pending")
    decided_by: Mapped[str | None]
    decided_at: Mapped[str | None]


class Task(Base):
    __tablename__ = "tasks"
    task_id: Mapped[str] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(ForeignKey("invoices.invoice_number"))
    owner_role: Mapped[str]
    text: Mapped[str]
    due_date: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="open")
    created_by: Mapped[str]


class CustomerPlaybook(Base):
    __tablename__ = "customer_playbook"
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), primary_key=True)
    # Rates and averages for scheduling (§18.7), not money; the core computes them exactly.
    promise_kept_rate: Mapped[float | None]
    promises_counted: Mapped[int] = mapped_column(server_default=text("0"))
    avg_days_to_reply: Mapped[float | None]
    replies_counted: Mapped[int] = mapped_column(server_default=text("0"))
    followup_gap_days: Mapped[int] = mapped_column(server_default=text("7"))
    updated_at: Mapped[str]


class MailOutbox(Base):
    __tablename__ = "mail_outbox"
    mail_id: Mapped[str] = mapped_column(primary_key=True)
    draft_id: Mapped[str | None] = mapped_column(ForeignKey("drafts.draft_id"))
    to_address: Mapped[str]
    subject: Mapped[str]
    body: Mapped[str]
    gateway: Mapped[str]
    status: Mapped[str]
    created_at: Mapped[str]
