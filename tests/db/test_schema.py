"""T-DB-01: fresh database after migrations (SPEC §5, §3.5). Synthetic values only."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import DefaultClause, Engine, TextClause, exc, inspect, text
from sqlalchemy.orm import Session

from receivables.adapters.db import engine as engine_module
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import downgrade_to_base, upgrade_to_head
from receivables.adapters.db.models import Base, Invoice
from receivables.settings import Settings

TABLES = {
    "users",
    "customers",
    "invoices",
    "payments",
    "events",
    "correspondence",
    "classifications",
    "drafts",
    "outcomes",
    "audit_log",
    "imports",
}
# SPEC §18.10, migration 0002 (constraints in tests/db/test_agent_schema.py).
AGENT_TABLES = {
    "cases",
    "agent_runs",
    "agent_steps",
    "approvals",
    "tasks",
    "customer_playbook",
    "mail_outbox",
}
TABLES = TABLES | AGENT_TABLES
TS = "2026-10-31T05:00:00Z"


@pytest.fixture
def url(tmp_path: Path) -> str:
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    upgrade_to_head(db_url)
    return db_url


@pytest.fixture
def engine(url: str) -> Iterator[Engine]:
    eng = make_engine(url)
    yield eng
    eng.dispose()


def _seed_customer(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO customers (customer_id, customer_name, payment_terms_days) "
                "VALUES ('C-TEST', 'Test Trading LLC (fictional)', 30)"
            )
        )


def _insert_invoice(engine: Engine, **overrides: object) -> None:
    row: dict[str, object] = {
        "invoice_number": "INV-0001",
        "customer_id": "C-TEST",
        "invoice_date": "2026-09-01",
        "due_date": "2026-10-01",
        "amount_fils": 100000,
    }
    row.update(overrides)
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    with engine.begin() as c:
        c.execute(text(f"INSERT INTO invoices ({cols}) VALUES ({params})"), row)  # noqa: S608


def _audit_row(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO audit_log (ts, actor, action, entity_type, entity_id) "
                "VALUES (:ts, 'system', 'test', 'invoice', 'INV-0001')"
            ),
            {"ts": TS},
        )


class TestDb01:
    def test_migrations_create_all_tables_and_triggers(self, engine: Engine) -> None:
        names = set(inspect(engine).get_table_names())
        assert names >= TABLES
        assert names - TABLES == {"alembic_version"}
        with engine.connect() as c:
            triggers = set(
                c.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars()
            )
            strict = set(
                c.execute(text("SELECT name FROM pragma_table_list WHERE strict = 1")).scalars()
            )
        assert triggers == {"audit_log_no_update", "audit_log_no_delete"}
        assert strict >= TABLES

    def test_foreign_keys_on_for_every_connection(self, engine: Engine) -> None:
        conns = [engine.connect() for _ in range(3)]
        try:
            for c in conns:
                assert c.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        finally:
            for c in conns:
                c.close()
        engine.dispose()  # brand-new DBAPI connections after the pool is emptied
        with engine.connect() as c:
            assert c.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert c.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
            assert c.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000

    def test_invoice_with_unknown_customer_rejected(self, engine: Engine) -> None:
        with pytest.raises(exc.IntegrityError, match="FOREIGN KEY"):
            _insert_invoice(engine, customer_id="C-UNKNOWN")

    def test_audit_log_update_and_delete_raise(self, engine: Engine) -> None:
        _audit_row(engine)
        with pytest.raises(exc.DatabaseError, match="append-only"), engine.begin() as c:
            c.execute(text("UPDATE audit_log SET actor = 'someone-else'"))
        with pytest.raises(exc.DatabaseError, match="append-only"), engine.begin() as c:
            c.execute(text("DELETE FROM audit_log"))
        with engine.connect() as c:
            assert c.execute(text("SELECT actor FROM audit_log")).scalars().all() == ["system"]

    def test_bad_date_rejected(self, engine: Engine) -> None:
        _seed_customer(engine)
        with pytest.raises(exc.IntegrityError, match="CHECK"):
            _insert_invoice(engine, invoice_date="2026-13-01", due_date="2026-13-01")

    def test_zero_amount_rejected(self, engine: Engine) -> None:
        _seed_customer(engine)
        with pytest.raises(exc.IntegrityError, match="CHECK"):
            _insert_invoice(engine, amount_fils=0)


def test_valid_invoice_accepted(engine: Engine) -> None:
    _seed_customer(engine)
    _insert_invoice(engine)
    with engine.connect() as c:
        row = c.execute(text("SELECT amount_fils, currency FROM invoices")).one()
    assert tuple(row) == (100000, "AED")


@pytest.mark.parametrize(
    "overrides",
    [
        {"due_date": "2026-08-31"},  # due before invoice date
        {
            "invoice_date": "2026-9-1",
            "due_date": "2099-12-31",
        },  # not ISO; only the date CHECK fails
        {"currency": "USD"},
        {"amount_fils": -5},
    ],
)
def test_invoice_checks(engine: Engine, overrides: dict[str, object]) -> None:
    _seed_customer(engine)
    with pytest.raises(exc.IntegrityError, match="CHECK"):
        _insert_invoice(engine, **overrides)


def test_strict_rejects_wrong_type(engine: Engine) -> None:
    _seed_customer(engine)
    with pytest.raises(exc.IntegrityError, match="cannot store TEXT value in INTEGER column"):
        _insert_invoice(engine, amount_fils="one hundred")


def test_json_columns_validated(engine: Engine) -> None:
    sql = (
        "INSERT INTO correspondence (message_id, direction, sent_at) "
        "VALUES ('M-T1', 'inbound', :ts)"
    )
    with engine.begin() as c:
        c.execute(text(sql), {"ts": TS})
    with pytest.raises(exc.IntegrityError, match="CHECK"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO classifications (message_id, label, extraction_json, needs_review, "
                "created_at) VALUES ('M-T1', 'UNCLEAR', '{not json', 1, :ts)"
            ),
            {"ts": TS},
        )


@pytest.mark.parametrize(
    ("invoice_number", "customer_id", "payment_type"),
    [(None, None, "payment"), (None, "C-TEST", "write_off")],
)
def test_payment_checks(
    engine: Engine, invoice_number: str | None, customer_id: str | None, payment_type: str
) -> None:
    _seed_customer(engine)
    with pytest.raises(exc.IntegrityError, match="CHECK"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO payments (payment_id, invoice_number, customer_id, payment_date, "
                "amount_fils, payment_type) VALUES ('P-T1', :inv, :cust, '2026-10-01', 500, :pt)"
            ),
            {"inv": invoice_number, "cust": customer_id, "pt": payment_type},
        )


def test_no_time_defaults_in_schema(engine: Engine) -> None:
    insp = inspect(engine)
    for table in TABLES:
        for col in insp.get_columns(table):
            default = str(col.get("default") or "").upper()
            assert "CURRENT_" not in default, (table, col["name"])
            assert "NOW" not in default, (table, col["name"])


def test_models_match_migrated_schema(engine: Engine) -> None:
    insp = inspect(engine)
    assert set(Base.metadata.tables) == TABLES
    for name, table in Base.metadata.tables.items():
        db_cols = {c["name"]: c for c in insp.get_columns(name)}
        assert set(db_cols) == {c.name for c in table.columns}, name
        for col in table.columns:
            if col.primary_key:  # SQLite reports PK columns as nullable; see test below
                continue
            assert bool(db_cols[col.name]["nullable"]) == col.nullable, (name, col.name)
        assert set(insp.get_pk_constraint(name)["constrained_columns"]) == {
            c.name for c in table.primary_key.columns
        }, name
        db_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"])
            for fk in insp.get_foreign_keys(name)
        }
        model_fks = {((fk.parent.name,), fk.column.table.name) for fk in table.foreign_keys}
        assert db_fks == model_fks, name
        for col in table.columns:
            db_type = db_cols[col.name]["type"].python_type
            assert db_type is col.type.python_type, (name, col.name, db_type)
            db_default = db_cols[col.name]["default"]
            model_default = col.server_default
            if model_default is None:
                assert db_default is None, (name, col.name, db_default)
            else:
                assert isinstance(model_default, DefaultClause)
                arg = model_default.arg
                expected = arg.text if isinstance(arg, TextClause) else f"'{arg}'"  # 0 vs '0'
                assert db_default == expected, (name, col.name, db_default)
        db_unique = {tuple(u["column_names"]) for u in insp.get_unique_constraints(name)}
        model_unique = {(c.name,) for c in table.columns if c.unique}
        assert db_unique == model_unique, name
        with engine.connect() as c:
            ddl = c.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :n"),
                {"n": name},
            ).scalar_one()
        assert ("AUTOINCREMENT" in ddl) == bool(table.kwargs.get("sqlite_autoincrement")), name


def test_orm_insert_uses_ddl_defaults(engine: Engine) -> None:
    _seed_customer(engine)
    with Session(engine) as session:
        session.add(
            Invoice(
                invoice_number="INV-ORM1",
                customer_id="C-TEST",
                invoice_date="2026-09-01",
                due_date="2026-10-01",
                amount_fils=250000,
            )
        )
        session.commit()
        invoice = session.get(Invoice, "INV-ORM1")
        assert invoice is not None
        assert invoice.currency == "AED"


def test_migration_connection_gets_pragmas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, str]] = []
    original = engine_module._apply_pragmas

    def recording(dbapi_connection: Any, record: Any) -> None:
        original(dbapi_connection, record)
        cur = dbapi_connection.cursor()
        seen.append(
            (
                cur.execute("PRAGMA foreign_keys").fetchone()[0],
                cur.execute("PRAGMA journal_mode").fetchone()[0],
            )
        )
        cur.close()

    monkeypatch.setattr(engine_module, "_apply_pragmas", recording)
    upgrade_to_head(f"sqlite:///{tmp_path / 'mig.db'}")
    assert seen
    assert all(s == (1, "wal") for s in seen)


def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'fresh' / 'clone' / 'demo.db'}"
    upgrade_to_head(db_url)
    assert (tmp_path / "fresh" / "clone" / "demo.db").is_file()


def test_downgrade_then_upgrade(url: str) -> None:
    downgrade_to_base(url)
    eng = make_engine(url)
    try:
        assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
    finally:
        eng.dispose()
    upgrade_to_head(url)
    eng = make_engine(url)
    try:
        assert set(inspect(eng).get_table_names()) >= TABLES
    finally:
        eng.dispose()


def test_settings_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings().database_url == "sqlite:///data/demo.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///elsewhere/other.db")
    assert Settings().database_url == "sqlite:///elsewhere/other.db"


def test_settings_secrets_are_not_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "synthetic-test-secret-value")
    settings = Settings()
    assert settings.session_secret is not None
    assert "synthetic-test-secret-value" not in repr(settings)
    assert settings.session_secret.get_secret_value() == "synthetic-test-secret-value"


def test_strict_primary_keys_reject_null(engine: Engine) -> None:
    with pytest.raises(exc.IntegrityError, match="NOT NULL"), engine.begin() as c:
        c.execute(
            text("INSERT INTO customers (customer_id, customer_name) VALUES (NULL, 'Fictional Co')")
        )


def test_migration_ddl_is_verbatim_spec() -> None:
    import importlib.util
    import re

    root = Path(__file__).resolve().parents[2]
    spec_text = (root / "docs" / "SPEC.md").read_text(encoding="utf-8")
    section = spec_text[spec_text.index("## 5. Database schema") : spec_text.index("## 6. ")]
    match = re.search(r"```sql\n(.*?)```", section, re.S)
    assert match is not None
    expected = tuple(s.strip() + ";" for s in match.group(1).split(";\n") if s.strip())

    path = root / "src/receivables/adapters/db/migrations/versions/0001_initial_schema.py"
    module_spec = importlib.util.spec_from_file_location("migration_0001", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert expected == module.DDL


def _date_check(column: str) -> str:
    """The exact CHECK expression SQLite reports, so no other constraint can satisfy the test."""
    expr = re.escape(f"date({column}) IS {column}")
    nullable = re.escape(f"{column} IS NULL OR ")
    return f"CHECK constraint failed: (?:{nullable})?{expr}(?:\n|$)"


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "2026-9-1", "31/10/2026", ""])
@pytest.mark.parametrize(
    ("column", "other"),
    [("invoice_date", {"due_date": "2099-12-31"}), ("due_date", {"invoice_date": "2000-01-01"})],
)
def test_invalid_invoice_dates_rejected(
    engine: Engine, column: str, other: dict[str, object], bad: str
) -> None:
    """D-007: every date CHECK rejects invalid dates (NULL from date() must not pass)."""
    _seed_customer(engine)
    with pytest.raises(exc.IntegrityError, match=_date_check(column)):
        _insert_invoice(engine, **other, **{column: bad})


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "2026-9-1"])
def test_invalid_payment_and_event_dates_rejected(engine: Engine, bad: str) -> None:
    _seed_customer(engine)
    _insert_invoice(engine)
    statements = [
        (
            "payment_date",
            "INSERT INTO payments (payment_id, invoice_number, payment_date, amount_fils) "
            "VALUES ('P-T2', 'INV-0001', :d, 500)",
        ),
        (
            "event_date",
            "INSERT INTO events (event_id, invoice_number, event_type, event_date) "
            "VALUES ('E-T1', 'INV-0001', 'PROMISE_TO_PAY', :d)",
        ),
        (
            "promise_date",
            "INSERT INTO events (event_id, invoice_number, event_type, event_date, promise_date) "
            "VALUES ('E-T2', 'INV-0001', 'PROMISE_TO_PAY', '2026-10-05', :d)",
        ),
    ]
    for column, sql in statements:
        with pytest.raises(exc.IntegrityError, match=_date_check(column)), engine.begin() as c:
            c.execute(text(sql), {"d": bad})


def test_null_promise_date_allowed(engine: Engine) -> None:
    _seed_customer(engine)
    _insert_invoice(engine)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO events (event_id, invoice_number, event_type, event_date) "
                "VALUES ('E-T3', 'INV-0001', 'DISPUTE_OPENED', '2026-10-05')"
            )
        )


def test_engine_rejects_non_sqlite_and_unknown_begin_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only SQLite"):
        make_engine("postgresql://user@localhost/db")
    eng = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        with (
            pytest.raises(ValueError, match="invalid sqlite_begin"),
            eng.connect().execution_options(sqlite_begin="SOMETIMES") as conn,
            conn.begin(),
        ):
            pass
    finally:
        eng.dispose()


def test_in_memory_engine_needs_no_directory() -> None:
    eng = make_engine("sqlite:///:memory:")
    try:
        with eng.connect() as c:
            assert c.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        eng.dispose()


def test_errors_never_show_bound_values(tmp_path: Path) -> None:
    # NFR-07: a failing statement's message (which uvicorn would log) holds no message body.
    eng = make_engine(f"sqlite:///{tmp_path / 'p.db'}")
    body = "We cannot pay INV-004, call Beta Contracting LLC on 050 123 4567"
    try:
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE t (body TEXT NOT NULL UNIQUE) STRICT"))
            conn.execute(text("INSERT INTO t VALUES (:b)"), {"b": body})
        with pytest.raises(exc.IntegrityError) as error, eng.begin() as conn:
            conn.execute(text("INSERT INTO t VALUES (:b)"), {"b": body})
        assert "hidden due to hide_parameters" in str(error.value)
        assert body not in str(error.value) and "Beta" not in str(error.value)
    finally:
        eng.dispose()
