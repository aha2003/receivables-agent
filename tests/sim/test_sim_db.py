"""The simulator's own database (SPEC §19.2; S4.01; D-047): `sim_<seed>.db`, not the real one."""

import hashlib
import unicodedata
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.models import AuditLog, Import
from receivables.cli import main
from receivables.seed import seed
from receivables.sim import simulator
from receivables.sim.company import generate_company
from receivables.sim.scenario import load_scenario
from receivables.sim.simulator import SimRefused, create_sim_db, sim_db_path

ROOT = Path(__file__).resolve().parents[2]
LEDGER_TABLES = ("customers", "invoices", "payments", "events")
REFUSED = r"is the real database or shares its files; refusing$"


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
            imported = set(conn.execute(select(Import.created_at)).scalars())
    finally:
        engine.dispose()
    # Run start 2026-11-02 10:00 Asia/Dubai is 06:00 UTC; nothing comes from the wall clock.
    assert {row.ts for row in audit} == {"2026-11-02T06:00:00Z"}
    assert imported == {"2026-11-02T06:00:00Z"}  # the ledger tables themselves hold no times
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
    with pytest.raises(SimRefused, match=REFUSED):
        create_sim_db(load_scenario(), tmp_path / "data", url)
    with pytest.raises(SimRefused, match=REFUSED):  # reached through another spelling
        create_sim_db(load_scenario(), tmp_path / "data" / ".." / "data", url)
    assert _sha(real) == before


@pytest.mark.spec("T-SIM-04")
@pytest.mark.parametrize("name", ["SIM_42.db", "Sim_42.db"])
def test_a_real_database_differing_only_in_case_is_refused(tmp_path: Path, name: str) -> None:
    # macOS file names ignore case: sim_42.db would be the real database's file.
    real = tmp_path / name
    upgrade_to_head(f"sqlite:///{real}")
    before = _sha(real)
    with pytest.raises(SimRefused, match=REFUSED):
        create_sim_db(load_scenario(), tmp_path, f"sqlite:///{real}")
    assert _sha(real) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]


@pytest.mark.spec("T-SIM-04")
def test_a_real_database_differing_only_in_unicode_form_is_refused(tmp_path: Path) -> None:
    data = tmp_path / unicodedata.normalize("NFC", "donn\u00e9es")
    other = tmp_path / unicodedata.normalize("NFD", "donn\u00e9es")
    data.mkdir()
    real = data / "sim_42.db"
    upgrade_to_head(f"sqlite:///{real}")
    before = _sha(real)
    with pytest.raises(SimRefused, match=REFUSED):
        create_sim_db(load_scenario(), other, f"sqlite:///{real}")
    assert _sha(real) == before


@pytest.mark.spec("T-SIM-04")
def test_a_real_database_given_as_a_file_uri_is_refused(tmp_path: Path) -> None:
    url = f"sqlite:///file:{tmp_path / 'sim_42.db'}?mode=rwc&uri=true"
    with pytest.raises(SimRefused, match=r"is a file: URI; cannot tell its file, refusing$"):
        create_sim_db(load_scenario(), tmp_path, url)
    assert list(tmp_path.iterdir()) == []


def test_an_in_memory_real_database_has_no_file_to_protect(tmp_path: Path) -> None:
    assert create_sim_db(load_scenario(), tmp_path, "sqlite://").path.is_file()


def test_the_destination_is_checked_again_before_the_swap(
    tmp_path: Path, real_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "data" / "sim_42.db"
    real_import = simulator.import_csv

    def import_then_meddle(*args: object, **kwargs: object) -> object:
        if not target.exists():  # something puts a file there while the run is building
            target.write_text("notes, not a database")
        return real_import(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(simulator, "import_csv", import_then_meddle)
    with pytest.raises(SimRefused, match=r"is not a SQLite database; refusing to replace it$"):
        create_sim_db(load_scenario(), tmp_path / "data", f"sqlite:///{real_db}")
    assert target.read_text() == "notes, not a database"
    assert sorted(p.name for p in target.parent.iterdir()) == ["sim_42.db"]  # staging removed


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
    with pytest.raises(SimRefused, match=REFUSED):
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


@pytest.mark.parametrize("seed", ["-1", "4.2", "\u0664\u0662", "x"])
def test_cli_sim_create_takes_only_an_ascii_whole_number_seed(seed: str) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["sim-create", "--seed", seed])
    assert exit_.value.code == 2  # argparse usage error, before anything is written
