"""T-DB-01 extended: the agent tables of migration 0002 (SPEC §18.10). Synthetic values only."""

import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, exc, inspect, text

from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import alembic_config, upgrade_to_head

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "src/receivables/adapters/db/migrations/versions"
TS = "2026-10-31T05:00:00Z"
AGENT_TABLES = (
    "cases",
    "agent_runs",
    "agent_steps",
    "approvals",
    "tasks",
    "customer_playbook",
    "mail_outbox",
)

# One valid row per table; tests override single columns. Parents are seeded by the fixture.
VALID: dict[str, dict[str, Any]] = {
    "cases": {
        "invoice_number": "INV-T1",
        "state": "NEW",
        "next_followup": "2026-11-07",
        "updated_at": TS,
    },
    "agent_runs": {
        "run_id": "RUN-2",
        "as_of": "2026-10-31",
        "now_ts": TS,
        "autonomy_level": 1,
        "planner": "rules",
        "started_at": TS,
    },
    "agent_steps": {
        "step_id": "STEP-2",
        "run_id": "RUN-1",
        "invoice_number": "INV-T1",
        "seq": 1,
        "tool": "get_case",
        "args_json": '{"invoice_number": "INV-T1"}',
        "status": "executed",
    },
    "approvals": {
        "approval_id": "APR-1",
        "step_id": "STEP-1",
        "kind": "send_message",
        "payload_json": "{}",
    },
    "tasks": {
        "task_id": "TASK-1",
        "invoice_number": "INV-T1",
        "owner_role": "finance",
        "text": "Call the customer (fictional).",
        "due_date": "2026-11-02",
        "created_by": "agent",
    },
    "customer_playbook": {"customer_id": "C-T", "updated_at": TS},
    "mail_outbox": {
        "mail_id": "MAIL-1",
        "draft_id": "DRAFT-1",
        "to_address": "ap@test.example",
        "subject": "Reminder (synthetic)",
        "body": "Synthetic body.",
        "gateway": "sandbox",
        "status": "delivered_sandbox",
        "created_at": TS,
    },
}

PARENTS = (
    "INSERT INTO customers (customer_id, customer_name, contact_email) "
    "VALUES ('C-T', 'Test Trading LLC (fictional)', 'ap@test.example')",
    "INSERT INTO invoices (invoice_number, customer_id, invoice_date, due_date, amount_fils) "
    "VALUES ('INV-T1', 'C-T', '2026-09-01', '2026-10-01', 100000)",
    "INSERT INTO drafts (draft_id, invoice_numbers_json, customer_id, tone, subject, body, "
    "facts_json, generator, created_at) VALUES ('DRAFT-1', '[\"INV-T1\"]', 'C-T', 1, 's', 'b', "
    "'{}', 'template', '2026-10-31T05:00:00Z')",
    "INSERT INTO agent_runs (run_id, as_of, now_ts, autonomy_level, planner, started_at) "
    "VALUES ('RUN-1', '2026-10-31', '2026-10-31T05:00:00Z', 1, 'rules', '2026-10-31T05:00:00Z')",
    "INSERT INTO agent_steps (step_id, run_id, invoice_number, seq, tool, args_json, status) "
    "VALUES ('STEP-1', 'RUN-1', 'INV-T1', 1, 'get_case', '{}', 'executed')",
)


@pytest.fixture
def url(tmp_path: Path) -> str:
    db_url = f"sqlite:///{tmp_path / 'agent.db'}"
    upgrade_to_head(db_url)
    return db_url


@pytest.fixture
def engine(url: str) -> Iterator[Engine]:
    eng = make_engine(url)
    with eng.begin() as c:
        for sql in PARENTS:
            c.execute(text(sql))
    yield eng
    eng.dispose()


def insert(engine: Engine, table: str, **overrides: Any) -> None:
    row = {**VALID[table], **overrides}
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params})"), row)  # noqa: S608


def fetch(engine: Engine, table: str, key: str, value: str) -> dict[str, Any]:
    with engine.connect() as c:
        row = c.execute(
            text(f"SELECT * FROM {table} WHERE {key} = :v"),  # noqa: S608
            {"v": value},
        ).one()
    return dict(row._mapping)


def test_head_is_0002_on_top_of_0001() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    assert script.get_current_head() == "0002"
    revision = script.get_revision("0002")
    assert revision is not None and revision.down_revision == "0001"


def test_migration_ddl_is_verbatim_spec() -> None:
    spec_text = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    section = spec_text[spec_text.index("### 18.10") : spec_text.index("### 18.11")]
    match = re.search(r"```sql\n(.*?)```", section, re.S)
    assert match is not None
    expected = tuple(s.strip() + ";" for s in match.group(1).split(";\n") if s.strip())

    path = VERSIONS / "0002_agent_tables.py"
    module_spec = importlib.util.spec_from_file_location("migration_0002", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert expected == module.DDL
    assert len(module.DDL) == len(AGENT_TABLES)


def test_agent_tables_exist_and_are_strict(engine: Engine) -> None:
    assert set(AGENT_TABLES) <= set(inspect(engine).get_table_names())
    with engine.connect() as c:
        strict = set(
            c.execute(text("SELECT name FROM pragma_table_list WHERE strict = 1")).scalars()
        )
    assert set(AGENT_TABLES) <= strict


@pytest.mark.parametrize("table", AGENT_TABLES)
def test_valid_row_accepted(engine: Engine, table: str) -> None:
    insert(engine, table)


def test_ddl_defaults(engine: Engine) -> None:
    for table in ("agent_runs", "agent_steps", "approvals", "tasks", "customer_playbook"):
        insert(engine, table)
    assert fetch(engine, "agent_runs", "run_id", "RUN-2")["fallback"] == 0
    assert fetch(engine, "agent_steps", "step_id", "STEP-2")["policy_reasons_json"] == "[]"
    assert fetch(engine, "approvals", "approval_id", "APR-1")["status"] == "pending"
    assert fetch(engine, "tasks", "task_id", "TASK-1")["status"] == "open"
    playbook = fetch(engine, "customer_playbook", "customer_id", "C-T")
    assert (playbook["promises_counted"], playbook["replies_counted"]) == (0, 0)
    assert playbook["followup_gap_days"] == 7
    assert (playbook["promise_kept_rate"], playbook["avg_days_to_reply"]) == (None, None)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("cases", "invoice_number", "INV-UNKNOWN"),
        ("agent_steps", "invoice_number", "INV-UNKNOWN"),
        ("agent_steps", "run_id", "RUN-UNKNOWN"),
        ("approvals", "step_id", "STEP-UNKNOWN"),
        ("tasks", "invoice_number", "INV-UNKNOWN"),
        ("customer_playbook", "customer_id", "C-UNKNOWN"),
        ("mail_outbox", "draft_id", "DRAFT-UNKNOWN"),
    ],
)
def test_foreign_keys_enforced(engine: Engine, table: str, column: str, value: str) -> None:
    with pytest.raises(exc.IntegrityError, match="FOREIGN KEY"):
        insert(engine, table, **{column: value})


@pytest.mark.parametrize(
    ("table", "column"), [("tasks", "invoice_number"), ("mail_outbox", "draft_id")]
)
def test_optional_references_may_be_null(engine: Engine, table: str, column: str) -> None:
    insert(engine, table, **{column: None})


GOOD_VALUES = [
    ("cases", "state", s)
    for s in (
        "NEW",
        "ASSESSED",
        "AWAITING_REPLY",
        "DISPUTE_OPEN",
        "PROMISE_PENDING",
        "ESCALATED",
        "RESOLVED",
    )
] + [
    ("cases", "next_followup", None),
    ("agent_runs", "autonomy_level", 0),
    ("agent_runs", "autonomy_level", 2),
    ("agent_runs", "planner", "llm"),
    ("agent_runs", "fallback", 1),
    ("agent_steps", "policy_allowed", 0),
    ("agent_steps", "policy_allowed", 1),
    ("agent_steps", "policy_allowed", None),
    ("agent_steps", "policy_reasons_json", '["KILL_SWITCH_ON"]'),
    *[
        ("agent_steps", "status", s)
        for s in ("executed", "denied", "converted_to_approval", "failed", "skipped")
    ],
    ("approvals", "status", "approved"),
    ("approvals", "status", "rejected"),
    *[("tasks", "owner_role", r) for r in ("finance", "sales", "operations", "management")],
    ("tasks", "status", "done"),
    ("tasks", "status", "cancelled"),
    ("tasks", "due_date", None),
    ("mail_outbox", "status", "sent"),
    ("mail_outbox", "status", "rejected"),
]


@pytest.mark.parametrize(("table", "column", "value"), GOOD_VALUES)
def test_allowed_values_accepted(engine: Engine, table: str, column: str, value: Any) -> None:
    insert(engine, table, **{column: value})


BAD_VALUES = [
    ("cases", "state", "OPEN"),
    ("cases", "state", "new"),
    ("cases", "next_followup", "2026-13-01"),
    ("cases", "next_followup", "2026-11-07T09:00:00Z"),
    ("agent_runs", "as_of", "2026-02-30"),
    ("agent_runs", "as_of", "31/10/2026"),
    ("agent_runs", "autonomy_level", 3),
    ("agent_runs", "autonomy_level", -1),
    ("agent_runs", "planner", "claude"),
    ("agent_runs", "fallback", 2),
    ("agent_steps", "args_json", "{not json"),
    ("agent_steps", "policy_allowed", 2),
    ("agent_steps", "policy_reasons_json", "[unclosed"),
    ("agent_steps", "status", "allowed"),
    ("approvals", "payload_json", "nope"),
    ("approvals", "status", "expired"),
    ("tasks", "owner_role", "agent"),
    ("tasks", "status", "pending"),
    ("tasks", "due_date", "2026-11-31"),
    ("mail_outbox", "status", "queued"),
]


@pytest.mark.parametrize(("table", "column", "value"), BAD_VALUES)
def test_check_constraints_reject(engine: Engine, table: str, column: str, value: Any) -> None:
    with pytest.raises(exc.IntegrityError, match="CHECK"):
        insert(engine, table, **{column: value})


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("cases", "state"),
        ("cases", "updated_at"),
        ("agent_runs", "as_of"),
        ("agent_runs", "now_ts"),
        ("agent_runs", "autonomy_level"),
        ("agent_runs", "planner"),
        ("agent_runs", "started_at"),
        ("agent_steps", "run_id"),
        ("agent_steps", "invoice_number"),
        ("agent_steps", "seq"),
        ("agent_steps", "tool"),
        ("agent_steps", "args_json"),
        ("agent_steps", "status"),
        ("approvals", "step_id"),
        ("approvals", "kind"),
        ("approvals", "payload_json"),
        ("tasks", "owner_role"),
        ("tasks", "text"),
        ("tasks", "created_by"),
        ("customer_playbook", "updated_at"),
        ("mail_outbox", "to_address"),
        ("mail_outbox", "subject"),
        ("mail_outbox", "body"),
        ("mail_outbox", "gateway"),
        ("mail_outbox", "status"),
        ("mail_outbox", "created_at"),
        # Columns with a DDL default are still NOT NULL when NULL is written explicitly.
        ("agent_runs", "fallback"),
        ("agent_steps", "policy_reasons_json"),
        ("approvals", "status"),
        ("tasks", "status"),
        ("customer_playbook", "promises_counted"),
        ("customer_playbook", "replies_counted"),
        ("customer_playbook", "followup_gap_days"),
    ],
)
def test_required_columns(engine: Engine, table: str, column: str) -> None:
    with pytest.raises(exc.IntegrityError, match="NOT NULL"):
        insert(engine, table, **{column: None})


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("agent_runs", "autonomy_level", "one"),
        ("agent_steps", "seq", "first"),
        ("customer_playbook", "followup_gap_days", 7.5),
    ],
)
def test_strict_rejects_wrong_type(engine: Engine, table: str, column: str, value: Any) -> None:
    with pytest.raises(exc.IntegrityError, match="cannot store"):
        insert(engine, table, **{column: value})


def test_one_case_per_invoice(engine: Engine) -> None:
    insert(engine, "cases")
    with pytest.raises(exc.IntegrityError, match="UNIQUE"):
        insert(engine, "cases", state="ASSESSED")


def test_downgrade_to_0001_drops_only_agent_tables(url: str) -> None:
    command.downgrade(alembic_config(url), "0001")
    eng = make_engine(url)
    try:
        names = set(inspect(eng).get_table_names())
        assert not names & set(AGENT_TABLES)
        assert {"invoices", "drafts", "audit_log"} <= names
    finally:
        eng.dispose()
    upgrade_to_head(url)
    eng = make_engine(url)
    try:
        assert set(AGENT_TABLES) <= set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_downgrade_with_agent_rows_present(url: str, engine: Engine) -> None:
    for table in AGENT_TABLES:
        if table not in {"agent_runs", "agent_steps"}:
            insert(engine, table)
    engine.dispose()
    command.downgrade(alembic_config(url), "0001")  # children dropped first: no FK failure
