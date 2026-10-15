"""Choosing the LLM client from LLM_MODE (SPEC §3.5; D-029). No test builds a live client."""

import dataclasses
from pathlib import Path

import pytest

from receivables.adapters.config import Config, load_config
from receivables.adapters.llm.anthropic_client import AnthropicClient
from receivables.adapters.llm.factory import LiveModeUnavailable, make_llm_client
from receivables.adapters.llm.fake import FakeClient, FakeScriptExhausted
from receivables.adapters.llm.mode import LlmModeConflict
from receivables.adapters.llm.replay import CassetteError, ReplayClient
from receivables.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def _config(replay: bool, cassette_dir: str = "tests/cassettes") -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    llm = dataclasses.replace(base.llm, replay=replay, cassette_dir=cassette_dir)
    return dataclasses.replace(base, llm=llm)


def test_fake_mode_gives_an_unscripted_fake() -> None:
    client = make_llm_client(Settings(), _config(False), customer_names=tuple).inner
    assert isinstance(client, FakeClient)
    with pytest.raises(FakeScriptExhausted):  # callers take their specified fallback
        client.draft({})


def test_replay_mode_reads_the_cassette_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    client = make_llm_client(Settings(llm_mode="replay"), _config(True), customer_names=tuple).inner
    assert isinstance(client, ReplayClient)


def test_replay_mode_with_no_cassettes_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CassetteError, match="no cassette"):
        make_llm_client(
            Settings(llm_mode="replay"), _config(True, str(tmp_path / "none")), customer_names=tuple
        )


FAKE_KEY = "test-only-not-a-real-anthropic-key-0123456789"
MODELS = {
    "ANTHROPIC_MODEL_PLANNER": "test-only-planner-model-id",
    "ANTHROPIC_MODEL_CLASSIFIER": "test-only-classifier-model-id",
    "ANTHROPIC_MODEL_DRAFTER": "test-only-drafter-model-id",
}


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({}, "ANTHROPIC_API_KEY, ANTHROPIC_MODEL_PLANNER, ANTHROPIC_MODEL_CLASSIFIER, "
             "ANTHROPIC_MODEL_DRAFTER"),
        ({"ANTHROPIC_API_KEY": FAKE_KEY}, "ANTHROPIC_MODEL_PLANNER, ANTHROPIC_MODEL_CLASSIFIER, "
                                          "ANTHROPIC_MODEL_DRAFTER"),
        ({**MODELS}, "ANTHROPIC_API_KEY"),
        ({**MODELS, "ANTHROPIC_API_KEY": ""}, "ANTHROPIC_API_KEY"),
        ({**MODELS, "ANTHROPIC_API_KEY": FAKE_KEY, "ANTHROPIC_MODEL_DRAFTER": ""},
         "ANTHROPIC_MODEL_DRAFTER"),
    ],
)  # fmt: skip
def test_live_mode_needs_the_key_and_all_model_ids(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], missing: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(LiveModeUnavailable) as error:
        make_llm_client(Settings(llm_mode="live"), _config(False), customer_names=tuple)
    assert str(error.value) == f"LLM_MODE=live needs {missing} set"
    assert FAKE_KEY not in str(error.value)


def test_live_mode_builds_the_anthropic_client_without_sending_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    for name, value in MODELS.items():
        monkeypatch.setenv(name, value)
    client = make_llm_client(Settings(llm_mode="live"), _config(False), customer_names=tuple)
    assert isinstance(client.inner, AnthropicClient)  # behind redaction (D-031)
    assert repr(client.inner) == "AnthropicClient()"
    assert client.model == "anthropic"
    # The network guard (tests/network_guard.py) fails the run if building it touched a socket.


@pytest.mark.parametrize(("mode", "replay"), [("fake", True), ("replay", False), ("live", True)])
def test_a_mode_conflict_is_refused_before_any_client_is_built(mode: str, replay: bool) -> None:
    with pytest.raises(LlmModeConflict):
        make_llm_client(Settings(llm_mode=mode), _config(replay), customer_names=tuple)  # type: ignore[arg-type]
