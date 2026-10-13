"""Atomic, idempotent import of the fixed canonical CSV schema (SPEC §4.6, §5, §9).

Validation is pure (``receivables.core.validation``). This adapter reads the reference data,
then either rejects the file (HTTP 422, nothing written), reports a duplicate (same kind, raw
bytes and mapping hash; nothing written) or, in one transaction, upserts the rows and writes
the ``imports`` record and one ``audit_log`` row with timestamps from the injected clock.
The upload route arrives in S1.13 (docs/DECISIONS.md D-013).
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import Connection, Engine, Table, exc, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from receivables.adapters.clock import Clock, utc_timestamp
from receivables.adapters.db.models import AuditLog, Customer, Event, Import, Invoice, Payment
from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.scoring import EventRecord
from receivables.core.validation import (
    FILE_KINDS,
    CustomerRow,
    Issue,
    References,
    ValidationResult,
    validate_csv,
)

__all__ = ["MAPPING_HASH", "MAPPING_VERSION", "ImportResult", "import_csv"]

MAPPING_VERSION = "fixed-schema-v1"
MAPPING_HASH = hashlib.sha256(MAPPING_VERSION.encode()).hexdigest()

Status = Literal["accepted", "duplicate", "rejected"]


def _issue_dict(issue: Issue) -> dict[str, Any]:
    out: dict[str, Any] = {"line": issue.line, "code": issue.code, "field": issue.field}
    if issue.detail is not None:
        out["detail"] = issue.detail
    return out


@dataclass(frozen=True, slots=True)
class ImportResult:
    status: Status
    http_status: int
    import_id: str | None
    rows_total: int
    rows_ok: int
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()

    def to_response(self) -> dict[str, Any]:
        """The §9 ``POST /api/imports/{kind}`` response body."""
        warnings = [_issue_dict(w) for w in self.warnings]
        if self.status == "rejected":
            return {
                "status": "rejected",
                "rows_total": self.rows_total,
                "errors": [_issue_dict(e) for e in self.errors],
                "warnings": warnings,
            }
        return {
            "import_id": self.import_id,
            "status": self.status,
            "rows_total": self.rows_total,
            "rows_ok": self.rows_ok,
            "warnings": warnings,
        }


def import_csv(
    engine: Engine,
    kind: str,
    raw: bytes,
    *,
    filename: str | None,
    clock: Clock,
    actor: str = "system",
) -> ImportResult:
    """Validate and import one fixed-schema CSV file; strict mode, upsert, idempotent."""
    if kind not in FILE_KINDS:
        raise ValueError(f"unsupported file kind {kind!r}")
    sha = hashlib.sha256(raw).hexdigest()
    key = f"{kind}:{sha}:{MAPPING_HASH}"
    try:
        with engine.begin() as conn:
            existing = _existing_import(conn, key)
            if existing is not None:
                return existing
            result = _validate(conn, kind, raw, clock)
            if result.errors:
                return ImportResult(
                    status="rejected",
                    http_status=422,
                    import_id=None,
                    rows_total=result.rows_total,
                    rows_ok=0,
                    errors=result.errors,
                    warnings=result.warnings,
                )
            _upsert(conn, kind, result)
            warnings = result.warnings
            if kind in {"invoices", "payments"} and _payment_count(conn) == 0:
                warnings = (*warnings, Issue(0, "W_NO_PAYMENTS", "payment_id"))
            import_id = uuid.uuid4().hex
            now = utc_timestamp(clock)
            conn.execute(
                insert(Import).values(
                    import_id=import_id,
                    file_kind=kind,
                    filename=filename,
                    sha256=sha,
                    mapping_hash=MAPPING_HASH,
                    idempotency_key=key,
                    rows_total=result.rows_total,
                    rows_ok=result.rows_total,
                    rows_error=0,
                    status="accepted",
                    report_json=json.dumps({"warnings": [_issue_dict(w) for w in warnings]}),
                    created_at=now,
                )
            )
            _write_audit(conn, now, actor, import_id, kind, filename, sha, result.rows_total)
    except exc.IntegrityError as err:
        # A concurrent upload of the same file committed first: report it as a duplicate.
        if "imports.idempotency_key" not in str(err.orig):
            raise
        with engine.connect() as conn:
            duplicate = _existing_import(conn, key)
        if duplicate is None:  # pragma: no cover - defensive; the constraint just fired
            raise
        return duplicate
    return ImportResult(
        status="accepted",
        http_status=200,
        import_id=import_id,
        rows_total=result.rows_total,
        rows_ok=result.rows_total,
        warnings=warnings,
    )


def _existing_import(conn: Connection, key: str) -> ImportResult | None:
    row = conn.execute(
        select(Import.import_id, Import.rows_total, Import.rows_ok).where(
            Import.idempotency_key == key
        )
    ).one_or_none()
    if row is None:
        return None
    return ImportResult(
        status="duplicate",
        http_status=200,
        import_id=row.import_id,
        rows_total=row.rows_total or 0,
        rows_ok=row.rows_ok or 0,
    )


def _validate(conn: Connection, kind: str, raw: bytes, clock: Clock) -> ValidationResult:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ValidationResult(kind, 0, (), (Issue(1, "E_MISSING_COLUMN", "header"),), ())
    return validate_csv(kind, text, _references(conn), clock.today())


def _references(conn: Connection) -> References:
    customers = frozenset(conn.execute(select(Customer.customer_id)).scalars())
    invoices = conn.execute(
        select(Invoice.invoice_number, Invoice.customer_id, Invoice.amount_fils)
    ).all()
    payments = conn.execute(
        select(Payment.payment_id, Payment.invoice_number, Payment.amount_fils)
    ).all()
    return References(
        customer_ids=customers,
        invoice_customers={r.invoice_number: r.customer_id for r in invoices},
        invoice_amounts={r.invoice_number: r.amount_fils for r in invoices},
        existing_payments={r.payment_id: (r.invoice_number, r.amount_fils) for r in payments},
    )


def _payment_count(conn: Connection) -> int:
    return conn.execute(select(func.count()).select_from(Payment)).scalar_one()


def _row(record: object) -> dict[str, Any]:
    if isinstance(record, CustomerRow):
        return {
            "customer_id": record.customer_id,
            "customer_name": record.customer_name,
            "trn": record.trn,
            "tin": record.tin,
            "trade_license_no": record.trade_license_no,
            "payment_terms_days": record.payment_terms_days,
            "contact_email": record.contact_email,
        }
    if isinstance(record, InvoiceRecord):
        return {
            "invoice_number": record.invoice_number,
            "customer_id": record.customer_id,
            "invoice_date": record.invoice_date.isoformat(),
            "due_date": record.due_date.isoformat(),
            "amount_fils": record.amount,
            "currency": "AED",
            "po_number": record.po_number,
            "invoice_type": record.invoice_type,
        }
    if isinstance(record, PaymentRecord):
        return {
            "payment_id": record.payment_id,
            "invoice_number": record.invoice_number,
            "customer_id": record.customer_id,
            "payment_date": record.payment_date.isoformat(),
            "amount_fils": record.amount,
            "payment_type": record.payment_type,
        }
    if isinstance(record, EventRecord):
        return {
            "event_id": record.event_id,
            "invoice_number": record.invoice_number,
            "event_type": record.event_type,
            "event_date": record.event_date.isoformat(),
            "label": record.label,
            "promise_date": record.promise_date.isoformat() if record.promise_date else None,
            "source": "import",
        }
    raise TypeError(f"unexpected record {type(record).__name__}")  # pragma: no cover


_TARGETS: dict[str, tuple[Any, str]] = {
    "customers": (Customer, "customer_id"),
    "invoices": (Invoice, "invoice_number"),
    "payments": (Payment, "payment_id"),
    "events": (Event, "event_id"),
}


def _upsert(conn: Connection, kind: str, result: ValidationResult) -> None:
    """Insert or replace by primary key; ON CONFLICT DO UPDATE never deletes, so FKs hold."""
    if not result.records:
        return
    model, key = _TARGETS[kind]
    table: Table = model.__table__
    rows = [_row(r) for r in result.records]
    stmt = sqlite_insert(table).values(rows)
    update = {name: stmt.excluded[name] for name in rows[0] if name != key}
    conn.execute(stmt.on_conflict_do_update(index_elements=[key], set_=update))


def _write_audit(
    conn: Connection,
    ts: str,
    actor: str,
    import_id: str,
    kind: str,
    filename: str | None,
    sha: str,
    rows_total: int,
) -> None:
    after = {
        "file_kind": kind,
        "filename": filename,
        "sha256": sha,
        "mapping_hash": MAPPING_HASH,
        "rows_total": rows_total,
        "status": "accepted",
    }
    conn.execute(
        insert(AuditLog).values(
            ts=ts,
            actor=actor,
            action="import.accepted",
            entity_type="import",
            entity_id=import_id,
            before_json=None,
            after_json=json.dumps(after, sort_keys=True),
        )
    )
