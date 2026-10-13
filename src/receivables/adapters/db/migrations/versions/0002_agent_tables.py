"""Agent tables: the SPEC §18.10 DDL, verbatim.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Copied verbatim from docs/SPEC.md §18.10 (tests/db/test_agent_schema.py checks they stay identical).
DDL: tuple[str, ...] = (
    """\
CREATE TABLE cases (
  invoice_number TEXT PRIMARY KEY REFERENCES invoices(invoice_number),
  state TEXT NOT NULL CHECK (state IN ('NEW','ASSESSED','AWAITING_REPLY','DISPUTE_OPEN','PROMISE_PENDING','ESCALATED','RESOLVED')),
  next_followup TEXT CHECK (next_followup IS NULL OR date(next_followup) IS next_followup),
  updated_at TEXT NOT NULL) STRICT;""",
    """\
CREATE TABLE agent_runs (
  run_id TEXT PRIMARY KEY, as_of TEXT NOT NULL CHECK (date(as_of) IS as_of), now_ts TEXT NOT NULL,
  autonomy_level INTEGER NOT NULL CHECK (autonomy_level IN (0,1,2)),
  planner TEXT NOT NULL CHECK (planner IN ('llm','rules')),
  fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0,1)), model TEXT,
  cases_considered INTEGER, steps_executed INTEGER, steps_denied INTEGER,
  started_at TEXT NOT NULL, finished_at TEXT) STRICT;""",
    """\
CREATE TABLE agent_steps (
  step_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
  invoice_number TEXT NOT NULL REFERENCES invoices(invoice_number), seq INTEGER NOT NULL,
  tool TEXT NOT NULL, args_json TEXT NOT NULL CHECK (json_valid(args_json)), reason TEXT,
  policy_allowed INTEGER CHECK (policy_allowed IN (0,1)),
  policy_reasons_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(policy_reasons_json)),
  status TEXT NOT NULL CHECK (status IN ('executed','denied','converted_to_approval','failed','skipped')),
  result_json TEXT) STRICT;""",
    """\
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY, step_id TEXT NOT NULL REFERENCES agent_steps(step_id),
  kind TEXT NOT NULL, payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  decided_by TEXT, decided_at TEXT) STRICT;""",
    """\
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY, invoice_number TEXT REFERENCES invoices(invoice_number),
  owner_role TEXT NOT NULL CHECK (owner_role IN ('finance','sales','operations','management')),
  text TEXT NOT NULL, due_date TEXT CHECK (due_date IS NULL OR date(due_date) IS due_date),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')), created_by TEXT NOT NULL) STRICT;""",
    """\
CREATE TABLE customer_playbook (
  customer_id TEXT PRIMARY KEY REFERENCES customers(customer_id),
  promise_kept_rate REAL, promises_counted INTEGER NOT NULL DEFAULT 0,
  avg_days_to_reply REAL, replies_counted INTEGER NOT NULL DEFAULT 0,
  followup_gap_days INTEGER NOT NULL DEFAULT 7, updated_at TEXT NOT NULL) STRICT;""",
    """\
CREATE TABLE mail_outbox (
  mail_id TEXT PRIMARY KEY, draft_id TEXT REFERENCES drafts(draft_id), to_address TEXT NOT NULL,
  subject TEXT NOT NULL, body TEXT NOT NULL, gateway TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('delivered_sandbox','sent','rejected')), created_at TEXT NOT NULL) STRICT;""",
)

# Children before parents, so foreign keys never point at a dropped table.
DROPS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS mail_outbox",
    "DROP TABLE IF EXISTS customer_playbook",
    "DROP TABLE IF EXISTS tasks",
    "DROP TABLE IF EXISTS approvals",
    "DROP TABLE IF EXISTS agent_steps",
    "DROP TABLE IF EXISTS agent_runs",
    "DROP TABLE IF EXISTS cases",
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROPS:
        op.execute(statement)
