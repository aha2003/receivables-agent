"""The simulator's own database (SPEC §19.2; S4.01; D-047): ``sim_<seed>.db``, never the real one."""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.models import AuditLog
from receivables.cli import main
from receivables.seed import seed
from receivables.sim.company import generate_company
from receivables.sim.scenario import load_scenario
from receivables.sim.simulator import SimRefused, create_sim_db, sim_db_path

ROOT = Path(__file__).resolve().parents[2]
LEDGER_TABLES = ("customers", "invoices", "payments", "events")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def real_db(tmp_path: Path) -> Path:
    """A real (golden) database, as the app would have in data/demo.db."""
    path = tmp_path / "app" / "demo.db"
    path.parent.mkdir()
    url = f"sqlite:///{path}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        seed(engine, ROOT / "fixtures", FixedClock(datetime(2026, 10, 31, 9, tzinfo=DUBAI)))
    finally:
        engine.dispose()
    return path


def _rows(engine: Engine, table: str) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY 1"))]  # noqa: S608


def _ledger(path: Path) -> dict[str, list[tuple[object, ...]]]:
    engine = make_engine(f"sqlite:///{path}")
    try:
        return {t: _rows(engine, t) for t in LEDGER_TABLES}
    finally:
        engine.dispose()


def test_the_path_is_sim_seed_db_in_the_data_dir(tmp_path: Path) -> None:
    assert sim_db_path(tmp_path, 42) == tmp_path / "sim_42.db"


def test_it_writes_the_generated_company_through_the_strict_importer(
    tmp_path: Path, real_db: Path
) -> None:
    scenario = load_scenario()
    result = create_sim_db(scenario, tmp_path / "data", f"sqlite:///{real_db}")
    assert result.path == tmp_path / "data" / "sim_42.db" and result.path.is_file()
    assert [r.status for r in result.imports] == ["accepted"] * 3
    assert all(r.errors == () for r in result.imports)
    assert {k: result.counts[k] for k in LEDGER_TABLES} == {
        "customers": 12, "invoices": 60, "payments": 3, "events": 0,
    }  # fmt: skip
    assert result.company == generate_company(scenario)
    ledger = _ledger(result.path)
    assert [r[0] for r in ledger["customers"]] == [f"SIM-C{n:02d}" for n in range(1, 13)]
    assert not {"CUST-A", "CUST-B", "CUST-C"} & {r[0] for r in ledger["customers"]}


def test_every_row_is_stamped_with_the_simulated_clock(tmp_path: Path, real_db: Path) -> None:
    result = create_sim_db(load_scenario(), tmp_path / "data", f"sqlite:///{real_db}")
    engine = make_engine(f"sqlite:///{result.path}")
    try:
        with engine.connect() as conn:
            audit = conn.execute(select(AuditLog.ts, AuditLog.action, AuditLog.after_json)).all()
    finally:
        engine.dispose()
    # Run start 2026-11-02 10:00 Asia/Dubai is 06:00 UTC; nothing comes from the wall clock.
    assert {row.ts for row in audit} == {"2026-11-02T06:00:00Z"}
    created = [row for row in audit if row.action == "sim.created"]
    assert len(created) == 1
    assert '"seed": 42' in created[0].after_json
    assert result.company.sha256() in created[0].after_json


@pytest.mark.spec("T-SIM-01")
def test_the_same_seed_gives_identical_ledger_tables(tmp_path: Path, real_db: Path) -> None:
    url = f"sqlite:///{real_db}"
    first = create_sim_db(load_scenario(), tmp_path / "one", url)
    second = create_sim_db(load_scenario(), tmp_path / "two", url)
    assert _ledger(first.path) == _ledger(second.path)
    other = create_sim_db(load_scenario(seed=43), tmp_path / "two", url)
    assert other.path.name == "sim_43.db"
    assert _ledger(other.path) != _ledger(first.path)


def test_running_again_replaces_the_sim_database(tmp_path: Path, real_db: Path) -> None:
    url = f"sqlite:///{real_db}"
    first = create_sim_db(load_scenario(), tmp_path / "data", url)
    before = _ledger(first.path)
    second = create_sim_db(load_scenario(), tmp_path / "data", url)
    assert second.path == first.path and _ledger(second.path) == before
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["sim_42.db"]


@pytest.mark.spec("T-SIM-04")
def test_the_real_database_is_byte_identical_after_a_run(tmp_path: Path, real_db: Path) -> None:
    siblings = sorted(p.name for p in real_db.parent.iterdir())
    before = _sha(real_db)
    create_sim_db(load_scenario(), tmp_path / "data", f"sqlite:///{real_db}")
    create_sim_db(load_scenario(), real_db.parent, f"sqlite:///{real_db}")  # even beside it
    assert _sha(real_db) == before
    assert sorted(p.name for p in real_db.parent.iterdir()) == sorted([*siblings, "sim_42.db"])


@pytest.mark.spec("T-SIM-04")
def test_it_refuses_to_write_over_the_real_database(tmp_path: Path) -> None:
    real = tmp_path / "data" / "sim_42.db"  # a real database that happens to have this name
    real.parent.mkdir()
    url = f"sqlite:///{real}"
    upgrade_to_head(url)
    before = _sha(real)
    refused = r"is the real database or shares its files; refusing$"
    with pytest.raises(SimRefused, match=refused):
        create_sim_db(load_scenario(), tmp_path / "data", url)
    with pytest.raises(SimRefused, match=refused):  # reached through another spelling
        create_sim_db(load_scenario(), tmp_path / "data" / ".." / "data", url)
    assert _sha(real) == before


def test_it_refuses_a_symlink_or_a_file_that_is_not_sqlite(tmp_path: Path, real_db: Path) -> None:
    # The messages are matched in full: tmp_path itself contains the test's name.
    data = tmp_path / "data"
    data.mkdir()
    url = f"sqlite:///{real_db}"
    other = tmp_path / "other.db"
    upgrade_to_head(f"sqlite:///{other}")
    (data / "sim_42.db").symlink_to(other)
    with pytest.raises(SimRefused, match=r"sim_42\.db is a symlink; refusing to replace it$"):
        create_sim_db(load_scenario(), data, url)
    (data / "sim_42.db").unlink()
    (data / "sim_42.db").symlink_to(real_db)  # a link to the real database
    with pytest.raises(SimRefused, match=r"is the real database or shares its files; refusing$"):
        create_sim_db(load_scenario(), data, url)
    (data / "sim_42.db").unlink()
    (data / "sim_42.db").write_text("notes, not a database")
    with pytest.raises(SimRefused, match=r"is not a SQLite database; refusing to replace it$"):
        create_sim_db(load_scenario(), data, url)
    assert (data / "sim_42.db").read_text() == "notes, not a database"
    (data / "sim_42.db").unlink()
    (data / "sim_42.db-wal").mkdir()  # a companion that is not a regular file
    with pytest.raises(SimRefused, match=r"-wal is not a regular file; refusing to replace it$"):
        create_sim_db(load_scenario(), data, url)


def test_cli_sim_create(
    tmp_path: Path, real_db: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{real_db}")
    before = _sha(real_db)
    assert main(["sim-create", "--seed", "7", "--data-dir", str(tmp_path / "sim")]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC DEMO DATA" in out and "SIMULATION — MECHANICS, NOT EVIDENCE" in out
    assert str(tmp_path / "sim" / "sim_7.db") in out
    assert "customers=12 invoices=60 payments=3" in out
    assert (tmp_path / "sim" / "sim_7.db").is_file()
    assert _sha(real_db) == before


def test_cli_sim_create_refusal_is_an_error_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "sim_42.db"
    upgrade_to_head(f"sqlite:///{real}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{real}")
    assert main(["sim-create", "--data-dir", str(tmp_path)]) == 2
    assert "sim-create refused:" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sim_42.db"]
