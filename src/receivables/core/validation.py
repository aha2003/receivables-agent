"""Strict validation of the fixed canonical CSV schema (SPEC §4.1-§4.6). Pure.

``validate_csv`` turns CSV text into typed records plus errors and warnings. Each data row
reports at most one error: the first failing check in the §4.6 order (required, duplicate key,
date, due before issue, amount, currency, foreign key), followed by the checks §4.6 does not
order (enum, TRN, customer mismatch, label). Formats are strict ASCII with at most two written
decimals (docs/DECISIONS.md D-005, D-013). Rows split on line feeds only; malformed CSV is
reported as ``E_BAD_NUMBER_FORMAT`` on field ``csv``. Line numbers are 1-based physical lines
including the header; file-level summary warnings use line 0.
"""

import csv
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.money import from_fils, to_fils
from receivables.core.scoring import EventRecord

__all__ = [
    "DISPUTE_EVENT_LABELS",
    "DISPUTE_LABELS",
    "FILE_KINDS",
    "CustomerRow",
    "Issue",
    "MessageRecord",
    "References",
    "ValidationResult",
    "derive_tin",
    "validate_csv",
]

FILE_KINDS = ("customers", "invoices", "payments", "events", "correspondence")

# SPEC §7.1: the 13 labels; events of type DISPUTE_OPENED must carry one.
DISPUTE_LABELS = frozenset(
    {
        "PO_AMOUNT_MISMATCH",
        "MISSING_PO",
        "MISSING_DELIVERY_NOTE",
        "GOODS_OR_SERVICE_ISSUE",
        "PRICE_OR_RATE_DISPUTE",
        "WRONG_ENTITY_OR_DETAILS",
        "ALREADY_PAID",
        "PAYMENT_PROMISED",
        "AWAITING_INTERNAL_APPROVAL",
        "CASHFLOW_DELAY",
        "REQUEST_STATEMENT_OR_COPY",
        "NO_ACTION_NEEDED",
        "UNCLEAR",
    }
)
# The §7.1 labels whose "Creates on accept" is DISPUTE_OPENED: the "dispute label" of §4.4.
DISPUTE_EVENT_LABELS = frozenset(
    {
        "PO_AMOUNT_MISMATCH",
        "MISSING_PO",
        "MISSING_DELIVERY_NOTE",
        "GOODS_OR_SERVICE_ISSUE",
        "PRICE_OR_RATE_DISPUTE",
        "WRONG_ENTITY_OR_DETAILS",
    }
)
PAYMENT_TYPES = frozenset({"payment", "credit_note", "write_off"})
EVENT_TYPES = frozenset({"DISPUTE_OPENED", "DISPUTE_RESOLVED", "PROMISE_TO_PAY"})

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]{1,2})?")  # at most 2 written decimals (§4)
_INTEGER = re.compile(r"[0-9]{1,3}")
_TRN = re.compile(r"[0-9]{15}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9._-]+")

# Columns per kind: (required in the header, optional in the header).
_COLUMNS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "customers": (
        ("customer_id", "customer_name"),
        ("trn", "trade_license_no", "payment_terms_days", "contact_email"),
    ),
    "invoices": (
        ("invoice_number", "customer_id", "invoice_date", "due_date", "amount"),
        ("currency", "po_number", "invoice_type"),
    ),
    "payments": (
        ("payment_id", "invoice_number", "customer_id", "payment_date", "amount"),
        ("payment_type",),
    ),
    "events": (
        ("event_id", "invoice_number", "event_type", "event_date"),
        ("label", "promise_date"),
    ),
    "correspondence": (  # §4.5
        ("message_id", "direction", "sent_at"),
        ("invoice_number", "customer_id", "language", "subject", "body"),
    ),
}
DIRECTIONS = frozenset({"inbound", "outbound"})
LANGUAGES = frozenset({"en", "ar", "auto"})
MAX_ID_CHARS = 64
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 20_000  # a longer body is out of range (E_BAD_ENUM, as for long ids)


@dataclass(frozen=True, slots=True)
class Issue:
    line: int
    code: str
    field: str
    detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CustomerRow:
    customer_id: str
    customer_name: str
    trn: str | None
    tin: str | None
    trade_license_no: str | None
    payment_terms_days: int | None
    contact_email: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageRecord:
    """A §4.5 correspondence row; ``sent_at`` is timezone-aware and stored in UTC."""

    message_id: str
    invoice_number: str | None
    customer_id: str | None
    direction: str
    sent_at: datetime
    language: str | None
    subject: str | None
    body: str | None


Record = CustomerRow | InvoiceRecord | PaymentRecord | EventRecord | MessageRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class References:
    """Existing database state the file is checked against (foreign keys, overpayment)."""

    customer_ids: frozenset[str] = frozenset()
    invoice_customers: Mapping[str, str] = field(default_factory=dict)
    invoice_amounts: Mapping[str, int] = field(default_factory=dict)
    existing_payments: Mapping[str, tuple[str | None, int]] = field(default_factory=dict)
    # Messages that already have a classification, as stored: their text may not change.
    classified_messages: Mapping[str, "MessageRecord"] = field(default_factory=dict)
    # Payments a person allocated in the app: payment id -> (invoice, customer) as stored. A
    # file row that names another invoice, or none, keeps them (D-040 option 1a).
    kept_allocations: Mapping[str, tuple[str | None, str | None]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    kind: str
    rows_total: int
    records: tuple[Record, ...]
    errors: tuple[Issue, ...]
    warnings: tuple[Issue, ...]


class _MalformedCsv(Exception):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.line = line


class _RowError(Exception):
    def __init__(self, code: str, field: str) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


def derive_tin(trn: str | None) -> str | None:
    """TIN is the first 10 digits of a valid 15-digit TRN (§2, §4.1)."""
    if trn is None or not _TRN.fullmatch(trn):
        return None
    return trn[:10]


def _lines(text: str) -> list[str]:
    """Split on line feeds only; ``str.splitlines`` would also break on U+2028, U+0085 etc."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _rows(text: str) -> Iterable[tuple[int, list[str]]]:
    """(starting physical line, cells) for every non-empty CSV record."""
    reader = csv.reader(_lines(text))
    previous_end = 0
    while True:
        start = previous_end + 1
        try:
            cells = next(reader)
        except StopIteration:
            return
        except csv.Error:
            raise _MalformedCsv(start) from None
        previous_end = reader.line_num
        if cells and any(c.strip() for c in cells):
            yield start, [c.strip() for c in cells]


def _parse_date(value: str, name: str) -> date:
    if not _DATE.fullmatch(value):
        raise _RowError("E_BAD_DATE", name)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _RowError("E_BAD_DATE", name) from None


def _parse_amount(value: str) -> int:
    if not _AMOUNT.fullmatch(value):
        raise _RowError("E_BAD_AMOUNT", "amount")
    try:
        fils = to_fils(value)
    except ValueError:
        raise _RowError("E_BAD_AMOUNT", "amount") from None
    if fils <= 0:
        raise _RowError("E_BAD_AMOUNT", "amount")
    return fils


def _require(row: Mapping[str, str], *names: str) -> None:
    for name in names:
        if not row.get(name):
            raise _RowError("E_REQUIRED_EMPTY", name)


class _Context:
    def __init__(self, refs: References, today: date) -> None:
        self.refs = refs
        self.today = today
        self.seen: set[str] = set()
        self.warnings: list[Issue] = []
        # payments only: (line, invoice_number, payment_id, amount) of allocated rows
        self.allocations: list[tuple[int, str, str, int]] = []
        self.unallocated_count = 0
        self.unallocated_total = 0

    def existing_applied(self) -> dict[str, int]:
        """Applied amounts per invoice from payments already in the database."""
        applied: dict[str, int] = {}
        for invoice_number, amount in self.refs.existing_payments.values():
            if invoice_number is not None:
                applied[invoice_number] = applied.get(invoice_number, 0) + amount
        return applied

    def unique(self, key: str, name: str) -> None:
        if key in self.seen:
            raise _RowError("E_DUP_KEY", name)
        self.seen.add(key)

    def future(self, line: int, name: str, value: date | None) -> None:
        if value is not None and value > self.today:
            self.warnings.append(Issue(line, "W_FUTURE_DATE", name))


def _customer(line: int, row: Mapping[str, str], ctx: _Context) -> CustomerRow:
    _require(row, "customer_id", "customer_name")
    ctx.unique(row["customer_id"], "customer_id")
    if len(row["customer_id"]) > 64:
        raise _RowError("E_BAD_ENUM", "customer_id")
    trn = row.get("trn") or None
    if trn is not None and not _TRN.fullmatch(trn):
        raise _RowError("E_BAD_TRN", "trn")
    terms_raw = row.get("payment_terms_days") or None
    terms: int | None = None
    if terms_raw is not None:
        if not _INTEGER.fullmatch(terms_raw):
            raise _RowError("E_BAD_NUMBER_FORMAT", "payment_terms_days")
        terms = int(terms_raw)
        if terms > 365:
            raise _RowError("E_BAD_ENUM", "payment_terms_days")
    email = row.get("contact_email") or None
    if email is not None and not _EMAIL.fullmatch(email):
        ctx.warnings.append(Issue(line, "W_BAD_EMAIL", "contact_email"))
    return CustomerRow(
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        trn=trn,
        tin=derive_tin(trn),
        trade_license_no=row.get("trade_license_no") or None,
        payment_terms_days=terms,
        contact_email=email,
    )


def _invoice(line: int, row: Mapping[str, str], ctx: _Context) -> InvoiceRecord:
    _require(row, "invoice_number", "customer_id", "invoice_date", "due_date", "amount")
    ctx.unique(row["invoice_number"], "invoice_number")
    invoice_date = _parse_date(row["invoice_date"], "invoice_date")
    due_date = _parse_date(row["due_date"], "due_date")
    if due_date < invoice_date:
        raise _RowError("E_DUE_BEFORE_ISSUE", "due_date")
    amount = _parse_amount(row["amount"])
    if (row.get("currency") or "AED") != "AED":
        raise _RowError("E_CURRENCY", "currency")
    if row["customer_id"] not in ctx.refs.customer_ids:
        raise _RowError("E_FK_MISSING", "customer_id")
    number = row["invoice_number"]
    paid = ctx.existing_applied().get(number)
    if paid is not None:
        if ctx.refs.invoice_customers.get(number) != row["customer_id"]:
            # Re-import would detach existing payments from their customer (§4.3, D-013).
            raise _RowError("E_CUSTOMER_MISMATCH", "customer_id")
        if paid > amount:
            ctx.warnings.append(Issue(line, "W_OVERPAID", "amount", number))
    ctx.future(line, "invoice_date", invoice_date)
    return InvoiceRecord(
        invoice_number=row["invoice_number"],
        customer_id=row["customer_id"],
        invoice_date=invoice_date,
        due_date=due_date,
        amount=amount,
        invoice_type=row.get("invoice_type") or None,
        po_number=row.get("po_number") or None,
    )


def _payment(line: int, row: Mapping[str, str], ctx: _Context) -> PaymentRecord:
    payment_type = row.get("payment_type") or "payment"
    _require(row, "payment_id", "payment_date", "amount")
    invoice_number = row.get("invoice_number") or None
    customer_id = row.get("customer_id") or None
    if invoice_number is None:
        _require(row, "invoice_number" if payment_type == "write_off" else "customer_id")
    ctx.unique(row["payment_id"], "payment_id")
    payment_date = _parse_date(row["payment_date"], "payment_date")
    amount = _parse_amount(row["amount"])
    if invoice_number is not None and invoice_number not in ctx.refs.invoice_customers:
        raise _RowError("E_FK_MISSING", "invoice_number")
    if customer_id is not None and customer_id not in ctx.refs.customer_ids:
        raise _RowError("E_FK_MISSING", "customer_id")
    if payment_type not in PAYMENT_TYPES:
        raise _RowError("E_BAD_ENUM", "payment_type")
    if (
        invoice_number is not None
        and customer_id is not None
        and ctx.refs.invoice_customers[invoice_number] != customer_id
    ):
        raise _RowError("E_CUSTOMER_MISMATCH", "customer_id")

    kept = ctx.refs.kept_allocations.get(row["payment_id"])
    if kept is not None and invoice_number != kept[0]:
        # A person's allocation stands; the checks below see the row as it will be written.
        invoice_number, customer_id = kept
        ctx.warnings.append(Issue(line, "W_ALLOCATION_KEPT", "invoice_number", row["payment_id"]))
    ctx.future(line, "payment_date", payment_date)
    if invoice_number is None:  # unallocated; a write-off was already rejected above
        ctx.unallocated_count += 1
        ctx.unallocated_total += amount
    else:
        ctx.allocations.append((line, invoice_number, row["payment_id"], amount))
    return PaymentRecord(
        payment_id=row["payment_id"],
        invoice_number=invoice_number,
        customer_id=customer_id,
        payment_date=payment_date,
        amount=amount,
        payment_type=payment_type,
    )


def _event(line: int, row: Mapping[str, str], ctx: _Context) -> EventRecord:
    _require(row, "event_id", "invoice_number", "event_type", "event_date")
    event_type = row["event_type"]
    if event_type == "DISPUTE_OPENED":
        _require(row, "label")
    if event_type == "PROMISE_TO_PAY":
        _require(row, "promise_date")
    ctx.unique(row["event_id"], "event_id")
    event_date = _parse_date(row["event_date"], "event_date")
    promise_raw = row.get("promise_date") or None
    promise_date = _parse_date(promise_raw, "promise_date") if promise_raw else None
    if promise_date is not None and promise_date < event_date:
        raise _RowError("E_BAD_DATE", "promise_date")
    if row["invoice_number"] not in ctx.refs.invoice_customers:
        raise _RowError("E_FK_MISSING", "invoice_number")
    if event_type not in EVENT_TYPES:
        raise _RowError("E_BAD_ENUM", "event_type")
    label = row.get("label") or None
    if label is not None and event_type != "PROMISE_TO_PAY" and label not in DISPUTE_EVENT_LABELS:
        raise _RowError("E_BAD_ENUM", "label")
    ctx.future(line, "event_date", event_date)
    return EventRecord(
        event_id=row["event_id"],
        invoice_number=row["invoice_number"],
        event_type=event_type,
        event_date=event_date,
        label=label,
        promise_date=promise_date,
    )


def _parse_timestamp(value: str, name: str) -> datetime:
    """ISO 8601 with an explicit offset or ``Z``; a naive time is ambiguous and refused."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _RowError("E_BAD_DATE", name) from None
    if parsed.tzinfo is None or not _DATE.match(value):
        raise _RowError("E_BAD_DATE", name)
    return parsed.astimezone(UTC)


def _message(line: int, row: Mapping[str, str], ctx: _Context) -> MessageRecord:
    _require(row, "message_id", "direction", "sent_at")
    ctx.unique(row["message_id"], "message_id")
    if len(row["message_id"]) > MAX_ID_CHARS or not _MESSAGE_ID.fullmatch(row["message_id"]):
        raise _RowError("E_BAD_ENUM", "message_id")  # ids appear in URLs: no "/" or spaces
    sent_at = _parse_timestamp(row["sent_at"], "sent_at")
    if row["direction"] not in DIRECTIONS:
        raise _RowError("E_BAD_ENUM", "direction")
    language = row.get("language") or None
    if language is not None and language not in LANGUAGES:
        raise _RowError("E_BAD_ENUM", "language")
    subject = row.get("subject") or None
    if subject is not None and len(subject) > MAX_SUBJECT_CHARS:
        raise _RowError("E_BAD_ENUM", "subject")
    body = row.get("body") or None
    if body is not None and len(body) > MAX_BODY_CHARS:
        raise _RowError("E_BAD_ENUM", "body")
    invoice_number = row.get("invoice_number") or None
    customer_id = row.get("customer_id") or None
    if invoice_number is not None and invoice_number not in ctx.refs.invoice_customers:
        raise _RowError("E_FK_MISSING", "invoice_number")
    if customer_id is not None and customer_id not in ctx.refs.customer_ids:
        raise _RowError("E_FK_MISSING", "customer_id")
    if (
        invoice_number is not None
        and customer_id is not None
        and ctx.refs.invoice_customers[invoice_number] != customer_id
    ):
        raise _RowError("E_CUSTOMER_MISMATCH", "customer_id")
    record = MessageRecord(
        message_id=row["message_id"],
        invoice_number=invoice_number,
        customer_id=customer_id,
        direction=row["direction"],
        sent_at=sent_at,
        language=language,
        subject=subject,
        body=body,
    )
    stored = ctx.refs.classified_messages.get(record.message_id)
    if stored is not None and stored != record:
        # A classified message keeps the text it was classified on (D-034).
        raise _RowError("E_DUP_KEY", "message_id")
    ctx.future(line, "sent_at", sent_at.date())
    return record


def _overpaid_warnings(ctx: _Context) -> list[Issue]:
    """``W_OVERPAID`` on the row where applied payments first exceed the invoice amount.

    Existing payments count unless this file replaces them (same ``payment_id``, upsert).
    """
    file_ids = {pid for _, _, pid, _ in ctx.allocations}
    applied: dict[str, int] = {}
    for pid, (invoice_number, amount) in ctx.refs.existing_payments.items():
        if invoice_number is not None and pid not in file_ids:
            applied[invoice_number] = applied.get(invoice_number, 0) + amount
    warnings: list[Issue] = []
    flagged: set[str] = set()
    for line, invoice_number, _, amount in ctx.allocations:
        applied[invoice_number] = applied.get(invoice_number, 0) + amount
        limit = ctx.refs.invoice_amounts.get(invoice_number)
        if limit is not None and applied[invoice_number] > limit and invoice_number not in flagged:
            flagged.add(invoice_number)
            warnings.append(Issue(line, "W_OVERPAID", "amount", invoice_number))
    return warnings


_PARSERS: dict[str, Callable[[int, Mapping[str, str], _Context], Record]] = {
    "customers": _customer,
    "invoices": _invoice,
    "payments": _payment,
    "events": _event,
    "correspondence": _message,
}


def validate_csv(kind: str, text: str, refs: References, today: date) -> ValidationResult:
    """Validate one fixed-schema CSV file; ``today`` drives ``W_FUTURE_DATE`` only."""
    if kind not in _PARSERS:
        raise ValueError(f"unsupported file kind {kind!r}")
    try:
        return _validate(kind, text, refs, today)
    except _MalformedCsv as err:
        issue = Issue(err.line, "E_BAD_NUMBER_FORMAT", "csv")
        return ValidationResult(kind, max(err.line - 1, 0), (), (issue,), ())


def _validate(kind: str, text: str, refs: References, today: date) -> ValidationResult:
    required, optional = _COLUMNS[kind]
    rows = iter(_rows(text))
    header_line, header = next(rows, (1, []))
    missing = [name for name in required if name not in header]
    if missing:
        errors = tuple(Issue(header_line, "E_MISSING_COLUMN", name) for name in missing)
        return ValidationResult(kind, sum(1 for _ in rows), (), errors, ())

    ctx = _Context(refs, today)
    known = set(required) | set(optional)
    ctx.warnings.extend(
        Issue(header_line, "W_EXTRA_COLUMN", name) for name in header if name not in known
    )
    parse = _PARSERS[kind]
    records: list[Record] = []
    errors_list: list[Issue] = []
    total = 0
    for line, cells in rows:
        total += 1
        row = {name: (cells[i] if i < len(cells) else "") for i, name in enumerate(header)}
        try:
            records.append(parse(line, row, ctx))
        except _RowError as err:
            errors_list.append(Issue(line, err.code, err.field))

    if kind == "payments":
        ctx.warnings.extend(_overpaid_warnings(ctx))
    if kind == "payments" and ctx.unallocated_count:
        detail = f"count={ctx.unallocated_count} total={from_fils(ctx.unallocated_total)}"
        ctx.warnings.append(Issue(0, "W_UNALLOCATED", "invoice_number", detail))
    return ValidationResult(
        kind,
        total,
        tuple(records) if not errors_list else (),
        tuple(errors_list),
        tuple(ctx.warnings),
    )
