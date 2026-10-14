"""LLM settings (SPEC §3.2, §3.5; D-029): LLM_MODE, the LLM_REPLAY synonym, the config
cross-check, and that the API key and model ids are never shown.

The values below are synthetic stand-ins, not real keys or model ids.
"""

import dataclasses
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import Config, load_config
from receivables.adapters.llm.mode import LlmModeConflict, resolve_llm_mode
from receivables.api.main import create_app
from receivables.settings import Settings

FAKE_KEY = "test-only-not-a-real-anthropic-key-0123456789"
FAKE_MODELS = {
    "ANTHROPIC_MODEL_PLANNER": "test-only-planner-model-id",
    "ANTHROPIC_MODEL_CLASSIFIER": "test-only-classifier-model-id",
    "ANTHROPIC_MODEL_DRAFTER": "test-only-drafter-model-id",
}
SECRET = "test-only-session-secret-0123456789abcdef"  # synthetic, tests only
CLOCK = FixedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    for name, value in FAKE_MODELS.items():
        monkeypatch.setenv(name, value)


def test_the_suite_starts_without_llm_environment() -> None:
    # tests/conftest.py removes these for every test, whatever the shell exports.
    for name in ("ANTHROPIC_API_KEY", "LLM_MODE", "LLM_REPLAY", *FAKE_MODELS):
        assert name not in os.environ


def test_the_scrub_holds_even_when_the_shell_exports_them() -> None:
    # A separate pytest process with a live mode and a (synthetic) key exported.
    env = {**os.environ, "LLM_MODE": "live", "LLM_REPLAY": "false", "ANTHROPIC_API_KEY": FAKE_KEY}
    env |= FAKE_MODELS
    target = f"{Path(__file__).resolve()}::test_the_suite_starts_without_llm_environment"
    done = subprocess.run(  # noqa: S603 - a fixed command: this test file under pytest
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout[-2000:]
    assert FAKE_KEY not in done.stdout + done.stderr


# --- LLM_MODE ---------------------------------------------------------------------------------


def test_llm_mode_defaults_to_fake() -> None:
    settings = Settings()
    assert settings.llm_mode == "fake"
    assert settings.anthropic_api_key is None


@pytest.mark.parametrize("mode", ["fake", "replay", "live"])
def test_llm_mode_values(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("LLM_MODE", mode)
    assert Settings().llm_mode == mode


@pytest.mark.parametrize("mode", ["FAKE", "real", "", "anthropic"])
def test_unknown_llm_modes_are_refused(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("LLM_MODE", mode)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("mode", "replay", "expected"),
    [
        (None, "true", "replay"),
        ("replay", "true", "replay"),
        (None, "false", "fake"),
        ("live", "false", "live"),
        ("fake", None, "fake"),
    ],
)
def test_llm_replay_is_a_synonym(
    monkeypatch: pytest.MonkeyPatch, mode: str | None, replay: str | None, expected: str
) -> None:
    if mode is not None:
        monkeypatch.setenv("LLM_MODE", mode)
    if replay is not None:
        monkeypatch.setenv("LLM_REPLAY", replay)
    assert Settings().llm_mode == expected


@pytest.mark.parametrize(
    ("mode", "replay"), [("fake", "true"), ("live", "true"), ("replay", "false")]
)
def test_contradicting_switches_are_refused(
    monkeypatch: pytest.MonkeyPatch, mode: str, replay: str
) -> None:
    monkeypatch.setenv("LLM_MODE", mode)
    monkeypatch.setenv("LLM_REPLAY", replay)
    with pytest.raises(ValidationError, match="contradicts"):
        Settings()


def test_a_dot_env_file_is_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(f"LLM_MODE=live\nANTHROPIC_API_KEY={FAKE_KEY}\n")
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert (settings.llm_mode, settings.anthropic_api_key) == ("fake", None)


# --- the config cross-check (D-029) ------------------------------------------------------------


def _config(replay: bool) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    return dataclasses.replace(base, llm=dataclasses.replace(base.llm, replay=replay))


@pytest.mark.parametrize(
    ("mode", "replay", "ok"),
    [
        ("fake", False, True),
        ("live", False, True),
        ("replay", True, True),
        ("replay", False, False),
        ("fake", True, False),
        ("live", True, False),
    ],
)
def test_config_replay_must_agree_with_llm_mode(mode: str, replay: bool, ok: bool) -> None:
    settings = Settings(llm_mode=mode)  # type: ignore[arg-type]
    config = _config(replay)
    if ok:
        assert resolve_llm_mode(settings, config) == mode
    else:
        with pytest.raises(LlmModeConflict, match="make them agree"):
            resolve_llm_mode(settings, config)


def test_the_app_refuses_to_start_on_a_conflict(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  replay: true\n", encoding="utf-8")
    settings = Settings(
        session_secret=SecretStr(SECRET),
        database_url=f"sqlite:///{tmp_path / 'x.db'}",
        config_path=str(config),
    )
    with pytest.raises(LlmModeConflict):
        create_app(settings, clock=CLOCK)


def test_the_app_records_the_effective_mode(tmp_path: Path) -> None:
    settings = Settings(
        session_secret=SecretStr(SECRET), database_url=f"sqlite:///{tmp_path / 'x.db'}"
    )
    app = create_app(settings, clock=CLOCK)
    assert app.state.llm_mode == "fake"
    app.state.engine.dispose()


# --- the key and model ids are never shown -----------------------------------------------------


@pytest.mark.usefixtures("llm_env")
def test_the_key_and_model_ids_load_from_the_environment() -> None:
    settings = Settings()
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == FAKE_KEY
    assert settings.anthropic_model_planner == FAKE_MODELS["ANTHROPIC_MODEL_PLANNER"]
    assert settings.anthropic_model_classifier == FAKE_MODELS["ANTHROPIC_MODEL_CLASSIFIER"]
    assert settings.anthropic_model_drafter == FAKE_MODELS["ANTHROPIC_MODEL_DRAFTER"]


@pytest.mark.usefixtures("llm_env")
def test_repr_and_str_show_neither_key_nor_model_ids() -> None:
    settings = Settings()
    for shown in (repr(settings), str(settings), f"{settings}", f"{settings!r}"):
        assert FAKE_KEY not in shown
        assert not [m for m in FAKE_MODELS.values() if m in shown]
    assert FAKE_KEY not in settings.model_dump_json()  # SecretStr stays masked when dumped


@pytest.mark.usefixtures("llm_env")
def test_validation_errors_do_not_echo_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "nope")
    with pytest.raises(ValidationError) as error:
        Settings()
    assert FAKE_KEY not in str(error.value)


@pytest.mark.usefixtures("llm_env")
def test_starting_the_app_logs_neither_key_nor_model_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        session_secret=SecretStr(SECRET), database_url=f"sqlite:///{tmp_path / 'x.db'}"
    )
    with caplog.at_level(logging.DEBUG):
        app = create_app(settings, clock=CLOCK)
        logging.getLogger("receivables").debug("settings: %r", settings)
    app.state.engine.dispose()
    out = capsys.readouterr()
    seen = caplog.text + out.out + out.err
    assert FAKE_KEY not in seen
    assert not [m for m in FAKE_MODELS.values() if m in seen]
