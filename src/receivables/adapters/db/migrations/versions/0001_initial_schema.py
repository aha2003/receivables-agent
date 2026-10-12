"""Initial schema: the SPEC §5 DDL, verbatim.

Revision ID: 0001
Revises:
Create Date: 2026-10-12
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Copied verbatim from docs/SPEC.md §5 (tests/db/test_schema.py checks they stay identical).
DDL: tuple[str, ...] = (
    """\
CREATE TABLE users (
  user_id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user','admin')), created_at TEXT NOT NULL) STRICT;""",
    """\
CREATE TABLE customers (
  customer_id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, trn TEXT, tin TEXT, trade_license_no TEXT,
  payment_terms_days INTEGER CHECK (payment_terms_days BETWEEN 0 AND 365), contact_email TEXT) STRICT;""",
    """\
CREATE TABLE invoices (
  invoice_number TEXT PRIMARY KEY,            -- canonical stable source invoice ID; preserve source formatting
  display_invoice_number TEXT, invoice_type TEXT,
  customer_id TEXT NOT NULL REFERENCES customers(customer_id),
  invoice_date TEXT NOT NULL CHECK (date(invoice_date) IS invoice_date),
  due_date TEXT NOT NULL CHECK (date(due_date) IS due_date),
  amount_fils INTEGER NOT NULL CHECK (amount_fils > 0),
  currency TEXT NOT NULL DEFAULT 'AED' CHECK (currency = 'AED'), po_number TEXT,
  CHECK (due_date >= invoice_date)) STRICT;""",
    """\
CREATE TABLE payments (
  payment_id TEXT PRIMARY KEY,
  invoice_number TEXT REFERENCES invoices(invoice_number),   -- NULL = unallocated payment
  customer_id TEXT REFERENCES customers(customer_id),        -- required when invoice_number is NULL
  payment_date TEXT NOT NULL CHECK (date(payment_date) IS payment_date),
  amount_fils INTEGER NOT NULL CHECK (amount_fils > 0),
  payment_type TEXT NOT NULL DEFAULT 'payment' CHECK (payment_type IN ('payment','credit_note','write_off')),
  CHECK (invoice_number IS NOT NULL OR customer_id IS NOT NULL),
  CHECK (payment_type <> 'write_off' OR invoice_number IS NOT NULL)) STRICT;""",
    """\
CREATE TABLE events (
  event_id TEXT PRIMARY KEY, invoice_number TEXT NOT NULL REFERENCES invoices(invoice_number),
  event_type TEXT NOT NULL CHECK (event_type IN ('DISPUTE_OPENED','DISPUTE_RESOLVED','PROMISE_TO_PAY')),
  event_date TEXT NOT NULL CHECK (date(event_date) IS event_date), label TEXT,
  promise_date TEXT CHECK (promise_date IS NULL OR date(promise_date) IS promise_date),
  source TEXT NOT NULL DEFAULT 'import') STRICT;""",
    """\
CREATE TABLE correspondence (
  message_id TEXT PRIMARY KEY, invoice_number TEXT REFERENCES invoices(invoice_number),
  customer_id TEXT REFERENCES customers(customer_id),
  direction TEXT NOT NULL CHECK (direction IN ('inbound','outbound')),
  sent_at TEXT NOT NULL, language TEXT, subject TEXT, body TEXT) STRICT;""",
    """\
CREATE TABLE classifications (
  message_id TEXT PRIMARY KEY REFERENCES correspondence(message_id), label TEXT NOT NULL, confidence REAL,
  extraction_json TEXT NOT NULL CHECK (json_valid(extraction_json)),
  flags_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(flags_json)),
  needs_review INTEGER NOT NULL CHECK (needs_review IN (0,1)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  model TEXT, created_at TEXT NOT NULL) STRICT;""",
    """\
CREATE TABLE drafts (
  draft_id TEXT PRIMARY KEY, invoice_numbers_json TEXT NOT NULL CHECK (json_valid(invoice_numbers_json)),
  customer_id TEXT NOT NULL REFERENCES customers(customer_id),
  tone INTEGER NOT NULL CHECK (tone IN (1,2,3)), subject TEXT NOT NULL, body TEXT NOT NULL,
  facts_json TEXT NOT NULL CHECK (json_valid(facts_json)),
  generator TEXT NOT NULL CHECK (generator IN ('llm','template')),
  violations_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(violations_json)),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','rejected','sent_manually')),
  created_at TEXT NOT NULL, approved_by TEXT, approved_at TEXT, sent_at TEXT) STRICT;""",
    """\
CREATE TABLE outcomes (
  outcome_id TEXT PRIMARY KEY, invoice_number TEXT NOT NULL REFERENCES invoices(invoice_number),
  recorded_at TEXT NOT NULL,
  outcome_type TEXT NOT NULL CHECK (outcome_type IN
    ('PAYMENT_RECEIVED','PROMISE_KEPT','PROMISE_BROKEN','DISPUTE_RESOLVED','NO_RESPONSE')), note TEXT) STRICT;""",
    """\
CREATE TABLE audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, before_json TEXT, after_json TEXT) STRICT;""",
    """\
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;""",
    """\
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;""",
    """\
CREATE TABLE imports (
  import_id TEXT PRIMARY KEY, file_kind TEXT NOT NULL, filename TEXT, sha256 TEXT NOT NULL,
  mapping_hash TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, reprocess_of TEXT,
  rows_total INTEGER, rows_ok INTEGER, rows_error INTEGER,
  status TEXT NOT NULL CHECK (status IN ('accepted','rejected','duplicate')),
  report_json TEXT, created_at TEXT NOT NULL) STRICT;""",
)

DROPS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS imports",
    "DROP TRIGGER IF EXISTS audit_log_no_delete",
    "DROP TRIGGER IF EXISTS audit_log_no_update",
    "DROP TABLE IF EXISTS audit_log",
    "DROP TABLE IF EXISTS outcomes",
    "DROP TABLE IF EXISTS drafts",
    "DROP TABLE IF EXISTS classifications",
    "DROP TABLE IF EXISTS correspondence",
    "DROP TABLE IF EXISTS events",
    "DROP TABLE IF EXISTS payments",
    "DROP TABLE IF EXISTS invoices",
    "DROP TABLE IF EXISTS customers",
    "DROP TABLE IF EXISTS users",
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROPS:
        op.execute(statement)
