"""T-DB-01: fresh database after migrations (SPEC §5, §3.5). Synthetic values only."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, exc, inspect, text

from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import downgrade_to_base, upgrade_to_head
from receivables.adapters.db.models import Base
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
        {"invoice_date": "2026-9-1"},  # not ISO
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


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "2026-9-1", "31/10/2026", ""])
@pytest.mark.parametrize("column", ["invoice_date", "due_date"])
def test_invalid_invoice_dates_rejected(engine: Engine, column: str, bad: str) -> None:
    """D-007: every date CHECK rejects invalid dates (NULL from date() must not pass)."""
    _seed_customer(engine)
    overrides = {"invoice_date": "2026-01-01", "due_date": "2026-01-01", column: bad}
    with pytest.raises(exc.IntegrityError, match="CHECK"):
        _insert_invoice(engine, **overrides)


@pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "2026-9-1"])
def test_invalid_payment_and_event_dates_rejected(engine: Engine, bad: str) -> None:
    _seed_customer(engine)
    _insert_invoice(engine)
    statements = [
        "INSERT INTO payments (payment_id, invoice_number, payment_date, amount_fils) "
        "VALUES ('P-T2', 'INV-0001', :d, 500)",
        "INSERT INTO events (event_id, invoice_number, event_type, event_date) "
        "VALUES ('E-T1', 'INV-0001', 'PROMISE_TO_PAY', :d)",
        "INSERT INTO events (event_id, invoice_number, event_type, event_date, promise_date) "
        "VALUES ('E-T2', 'INV-0001', 'PROMISE_TO_PAY', '2026-10-05', :d)",
    ]
    for sql in statements:
        with pytest.raises(exc.IntegrityError, match="CHECK"), engine.begin() as c:
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
