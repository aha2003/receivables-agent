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


# --- create-user (SPEC §3.3: users are created with a CLI command, never through the web UI) ---

SYNTHETIC_PASSWORD = "synthetic-pass-456"


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _users(db: Path) -> list[tuple[str, str, str]]:
    import sqlite3

    if not db.exists():  # rejected before migrating: no database, so no users
        return []
    with sqlite3.connect(db) as c:
        return list(c.execute("SELECT username, role, password_hash FROM users ORDER BY user_id"))


def _audit(db: Path) -> list[tuple[str, str]]:
    import sqlite3

    with sqlite3.connect(db) as c:
        return list(
            c.execute("SELECT action, after_json FROM audit_log WHERE entity_type = 'user'")
        )


def test_create_user_from_stdin(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, SYNTHETIC_PASSWORD + "\n")
    assert main(["create-user", "demo.admin", "--role", "admin", "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert "created user demo.admin (admin)" in out
    users = _users(env)
    assert [(u, r) for u, r, _ in users] == [("demo.admin", "admin")]
    assert users[0][2].startswith("$argon2id$")
    assert SYNTHETIC_PASSWORD not in out
    audit = _audit(env)
    assert [a for a, _ in audit] == ["user.created"]
    assert "argon2" not in audit[0][1] and SYNTHETIC_PASSWORD not in audit[0][1]


def test_create_user_interactive_prompts_twice(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([SYNTHETIC_PASSWORD, SYNTHETIC_PASSWORD])
    monkeypatch.setattr("receivables.cli.getpass", lambda prompt="": next(answers))
    assert main(["create-user", "demo.user"]) == 0
    assert [(u, r) for u, r, _ in _users(env)] == [("demo.user", "user")]


def test_create_user_mismatched_confirmation(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([SYNTHETIC_PASSWORD, SYNTHETIC_PASSWORD + "x"])
    monkeypatch.setattr("receivables.cli.getpass", lambda prompt="": next(answers))
    assert main(["create-user", "demo.user"]) == 1
    assert "do not match" in capsys.readouterr().err
    assert _users(env) == []


def test_create_user_rejects_short_password(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "short-pass\n")
    assert main(["create-user", "demo.user", "--password-stdin"]) == 1
    assert "at least 12" in capsys.readouterr().err
    assert _users(env) == []


def test_create_user_rejects_duplicate(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, SYNTHETIC_PASSWORD + "\n")
    assert main(["create-user", "demo.user", "--password-stdin"]) == 0
    _stdin(monkeypatch, SYNTHETIC_PASSWORD + "\n")
    assert main(["create-user", "demo.user", "--password-stdin"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert len(_users(env)) == 1


@pytest.mark.parametrize("username", ["", "has space", "x" * 65, "semi;colon", "<script>"])
def test_create_user_rejects_bad_usernames(
    env: Path, monkeypatch: pytest.MonkeyPatch, username: str
) -> None:
    _stdin(monkeypatch, SYNTHETIC_PASSWORD + "\n")
    assert main(["create-user", username, "--password-stdin"]) == 1
    assert _users(env) == []


def test_create_user_rejects_unknown_role(env: Path) -> None:
    with pytest.raises(SystemExit) as info:
        main(["create-user", "demo.user", "--role", "root"])
    assert info.value.code != 0


def test_password_is_never_an_argument() -> None:
    with pytest.raises(SystemExit):
        main(["create-user", "demo.user", "--password", SYNTHETIC_PASSWORD])
