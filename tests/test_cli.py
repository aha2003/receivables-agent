"""CLI commands ``seed`` and ``reset`` against a temporary database (never data/)."""

from pathlib import Path

import pytest

from receivables.cli import main

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "cli" / "demo.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    return db


def test_reset_prints_golden_summary(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reset", "--as-of", "2026-10-31"]) == 0
    out = capsys.readouterr().out
    assert env.is_file()
    assert "customers=3 invoices=14 payments=4 events=2" in out
    assert "total open AED 193,000.00" in out
    assert "overdue AED 147,000.00" in out
    assert "queue 9 items" in out
    assert "SYNTHETIC DEMO DATA" in out


def test_seed_after_reset_reports_duplicates(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reset", "--as-of", "2026-10-31"]) == 0
    capsys.readouterr()
    assert main(["seed", "--as-of", "2026-10-31"]) == 0
    out = capsys.readouterr().out
    assert "customers: duplicate" in out
    assert "events: duplicate" in out


def test_seed_on_a_fresh_database_migrates_first(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["seed"]) == 0
    assert "customers: accepted" in capsys.readouterr().out


def test_reset_refusal_is_an_error_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("keep me")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{notes}")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    assert main(["reset"]) == 2
    assert "refused" in capsys.readouterr().err
    assert notes.read_text() == "keep me"


def test_unknown_command_exits_non_zero() -> None:
    with pytest.raises(SystemExit) as info:
        main(["explode"])
    assert info.value.code != 0


def test_bad_as_of_exits_non_zero() -> None:
    with pytest.raises(SystemExit) as info:
        main(["seed", "--as-of", "31/10/2026"])
    assert info.value.code != 0


def test_seed_failure_is_an_error_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "fixtures"
    broken.mkdir()
    (broken / "customers.csv").write_text("customer_id,customer_name,trn\nC-1,Fictional,12345\n")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.setenv("FIXTURES_DIR", str(broken))
    assert main(["seed"]) == 1
    assert "seed failed" in capsys.readouterr().err


def test_missing_fixtures_is_an_error_exit_and_keeps_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "demo.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    assert main(["reset"]) == 0
    size = db.stat().st_size
    capsys.readouterr()
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path / "nowhere"))
    assert main(["reset"]) == 1
    assert "missing fixture" in capsys.readouterr().err
    assert db.stat().st_size == size


def test_os_error_is_an_error_exit(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import receivables.cli as cli

    def boom(*_: object, **__: object) -> None:
        raise PermissionError("simulated: permission denied")

    monkeypatch.setattr(cli, "reset", boom)
    assert main(["reset"]) == 1
    assert "permission denied" in capsys.readouterr().err
