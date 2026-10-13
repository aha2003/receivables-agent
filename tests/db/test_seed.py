"""Seed and reset (SPEC §5 purge): reset is idempotent and leaves exactly the golden data."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.seed import ResetRefused, reset, seed

CLOCK = FixedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
DATA_TABLES = ("customers", "invoices", "payments", "events")


def _snapshot(url: str) -> dict[str, list[tuple[object, ...]]]:
    engine = make_engine(url)
    try:
        with engine.connect() as c:
            return {
                t: [tuple(r) for r in c.execute(text(f"SELECT * FROM {t} ORDER BY 1"))]  # noqa: S608
                for t in DATA_TABLES
            }
    finally:
        engine.dispose()


def _audit_actions(url: str) -> list[str]:
    engine = make_engine(url)
    try:
        with engine.connect() as c:
            return list(c.execute(text("SELECT action FROM audit_log ORDER BY audit_id")).scalars())
    finally:
        engine.dispose()


def test_seed_twice_reports_duplicates_and_changes_nothing(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 's.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        first = seed(engine, FIXTURES, CLOCK)
        before = _snapshot(url)
        second = seed(engine, FIXTURES, CLOCK)
    finally:
        engine.dispose()
    assert [r.status for r in first] == ["accepted"] * 4
    assert [r.status for r in second] == ["duplicate"] * 4
    assert _snapshot(url) == before


def test_reset_is_idempotent_and_leaves_exactly_the_golden_data(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'r.db'}"
    first = reset(url, FIXTURES, CLOCK)
    golden = _snapshot(url)
    assert {t: len(rows) for t, rows in golden.items()} == {
        "customers": 3,
        "invoices": 14,
        "payments": 4,
        "events": 2,
    }
    second = reset(url, FIXTURES, CLOCK)
    assert _snapshot(url) == golden
    assert first.counts == second.counts
    assert _audit_actions(url) == ["import.accepted"] * 4 + ["database.reset"]


def test_reset_removes_extra_data(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'r.db'}"
    reset(url, FIXTURES, CLOCK)
    golden = _snapshot(url)
    engine = make_engine(url)
    try:
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO customers (customer_id, customer_name) "
                    "VALUES ('CUST-X', 'Extra Fictional LLC')"
                )
            )
            c.execute(text("UPDATE invoices SET amount_fils = 1 WHERE invoice_number = 'INV-004'"))
    finally:
        engine.dispose()
    assert _snapshot(url) != golden
    reset(url, FIXTURES, CLOCK)
    assert _snapshot(url) == golden


def test_reset_audit_row(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'r.db'}"
    reset(url, FIXTURES, CLOCK, actor="cli")
    engine = make_engine(url)
    try:
        with engine.connect() as c:
            row = (
                c.execute(text("SELECT * FROM audit_log WHERE action = 'database.reset'"))
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    assert (row["actor"], row["entity_type"], row["ts"]) == (
        "cli",
        "database",
        "2026-10-31T05:00:00Z",
    )
    after = json.loads(row["after_json"])
    assert after["row_counts"]["invoices"] == 14
    assert json.loads(row["before_json"]) == {"existing_file": False}


def test_reset_deletes_wal_and_shm_files(tmp_path: Path) -> None:
    db_file = tmp_path / "r.db"
    url = f"sqlite:///{db_file}"
    reset(url, FIXTURES, CLOCK)
    stale_wal = tmp_path / "r.db-wal"
    stale_shm = tmp_path / "r.db-shm"
    stale_wal.write_bytes(b"stale")
    stale_shm.write_bytes(b"stale")
    reset(url, FIXTURES, CLOCK)
    assert not stale_wal.exists() or stale_wal.read_bytes() != b"stale"
    assert not stale_shm.exists() or stale_shm.read_bytes() != b"stale"


def test_reset_creates_missing_directory(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh' / 'demo.db'}"
    reset(url, FIXTURES, CLOCK)
    assert (tmp_path / "fresh" / "demo.db").is_file()


@pytest.mark.parametrize(
    "url",
    ["sqlite:///:memory:", "sqlite://", "postgresql://user@localhost/db"],
)
def test_reset_refuses_non_file_databases(url: str) -> None:
    with pytest.raises(ResetRefused):
        reset(url, FIXTURES, CLOCK)


def test_reset_refuses_a_file_that_is_not_sqlite(tmp_path: Path) -> None:
    precious = tmp_path / "notes.txt"
    precious.write_text("not a database")
    with pytest.raises(ResetRefused, match="not a SQLite database"):
        reset(f"sqlite:///{precious}", FIXTURES, CLOCK)
    assert precious.read_text() == "not a database"


def test_reset_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ResetRefused):
        reset(f"sqlite:///{tmp_path}", FIXTURES, CLOCK)


def test_seed_stops_on_a_rejected_fixture(tmp_path: Path) -> None:
    from receivables.seed import SeedError

    broken = tmp_path / "fixtures"
    broken.mkdir()
    (broken / "customers.csv").write_text("customer_id,customer_name,trn\nC-1,Fictional,12345\n")
    url = f"sqlite:///{tmp_path / 's.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        with pytest.raises(SeedError, match=r"customers\.csv rejected: \[\(2, 'E_BAD_TRN'\)\]"):
            seed(engine, broken, CLOCK)
    finally:
        engine.dispose()
