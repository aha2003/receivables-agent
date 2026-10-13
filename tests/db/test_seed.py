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


def test_reset_deletes_wal_and_shm_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5: reset itself removes the old database, -wal and -shm before moving the new file in."""
    import receivables.seed as seed_module

    url = f"sqlite:///{tmp_path / 'r.db'}"
    reset(url, FIXTURES, CLOCK)
    stale_wal = tmp_path / "r.db-wal"
    stale_shm = tmp_path / "r.db-shm"
    stale_wal.write_bytes(b"stale wal from an older database")
    stale_shm.write_bytes(b"stale shm")
    removed: list[Path] = []
    real_remove = seed_module._remove

    def recording_remove(paths: tuple[Path, ...]) -> None:
        removed.extend(paths)
        real_remove(paths)

    monkeypatch.setattr(seed_module, "_remove", recording_remove)
    reset(url, FIXTURES, CLOCK)
    assert {tmp_path / "r.db", stale_wal, stale_shm} <= set(removed)
    assert not stale_wal.exists()
    assert not stale_shm.exists()
    assert not (tmp_path / "r.db.reset-tmp").exists()


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
    for name in ("invoices", "payments", "events"):
        (broken / f"{name}.csv").write_bytes((FIXTURES / f"{name}.csv").read_bytes())
    (broken / "customers.csv").write_text("customer_id,customer_name,trn\nC-1,Fictional,12345\n")
    url = f"sqlite:///{tmp_path / 's.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        with pytest.raises(SeedError, match=r"customers\.csv rejected: \[\(2, 'E_BAD_TRN'\)\]"):
            seed(engine, broken, CLOCK)
    finally:
        engine.dispose()


def _golden_db(tmp_path: Path) -> tuple[str, dict[str, list[tuple[object, ...]]]]:
    url = f"sqlite:///{tmp_path / 'keep.db'}"
    reset(url, FIXTURES, CLOCK)
    return url, _snapshot(url)


def test_missing_fixtures_leave_the_database_intact(tmp_path: Path) -> None:
    from receivables.seed import SeedError

    url, before = _golden_db(tmp_path)
    with pytest.raises(SeedError, match="missing fixture"):
        reset(url, tmp_path / "no-such-dir", CLOCK)
    assert _snapshot(url) == before
    assert _audit_actions(url) == ["import.accepted"] * 4 + ["database.reset"]


def test_rejected_fixture_leaves_the_database_intact(tmp_path: Path) -> None:
    from receivables.seed import SeedError

    url, before = _golden_db(tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    for name in ("customers", "invoices", "payments", "events"):
        (broken / f"{name}.csv").write_bytes((FIXTURES / f"{name}.csv").read_bytes())
    (broken / "payments.csv").write_text(
        "payment_id,invoice_number,customer_id,payment_date,amount\nP-1,INV-404,,2026-10-01,5.00\n"
    )
    with pytest.raises(SeedError, match=r"payments\.csv rejected"):
        reset(url, broken, CLOCK)
    assert _snapshot(url) == before
    assert not (tmp_path / "keep.db.reset-tmp").exists()


def test_reset_refuses_while_another_connection_is_writing(tmp_path: Path) -> None:
    import sqlite3

    url, before = _golden_db(tmp_path)
    holder = sqlite3.connect(tmp_path / "keep.db", isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ResetRefused, match="in use"):
            reset(url, FIXTURES, CLOCK)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _snapshot(url) == before
    assert not (tmp_path / "keep.db.reset-tmp").exists()


def test_reset_refuses_an_empty_non_sqlite_file(tmp_path: Path) -> None:
    empty = tmp_path / "README"
    empty.write_bytes(b"")
    with pytest.raises(ResetRefused, match="not a SQLite database"):
        reset(f"sqlite:///{empty}", FIXTURES, CLOCK)
    assert empty.exists() and empty.read_bytes() == b""


def test_reset_refuses_a_symlink(tmp_path: Path) -> None:
    url, before = _golden_db(tmp_path)
    link = tmp_path / "link.db"
    link.symlink_to(tmp_path / "keep.db")
    with pytest.raises(ResetRefused, match="symlink"):
        reset(f"sqlite:///{link}", FIXTURES, CLOCK)
    assert link.is_symlink()
    assert _snapshot(url) == before


def test_reset_refuses_a_fifo(tmp_path: Path) -> None:
    import os

    fifo = tmp_path / "pipe.db"
    os.mkfifo(fifo)
    with pytest.raises(ResetRefused, match="not a regular file"):
        reset(f"sqlite:///{fifo}", FIXTURES, CLOCK)


def test_reset_refuses_a_directory_at_the_wal_path_before_deleting_anything(tmp_path: Path) -> None:
    url, before = _golden_db(tmp_path)
    (tmp_path / "keep.db-wal").mkdir()
    with pytest.raises(ResetRefused, match="not a regular file"):
        reset(url, FIXTURES, CLOCK)
    (tmp_path / "keep.db-wal").rmdir()
    assert _snapshot(url) == before
