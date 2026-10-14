"""Choosing the LLM client from LLM_MODE (SPEC §3.5; D-029). No test builds a live client."""

import dataclasses
from pathlib import Path

import pytest

from receivables.adapters.config import Config, load_config
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
    client = make_llm_client(Settings(), _config(False))
    assert isinstance(client, FakeClient)
    with pytest.raises(FakeScriptExhausted):  # callers take their specified fallback
        client.draft({})


def test_replay_mode_reads_the_cassette_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    client = make_llm_client(Settings(llm_mode="replay"), _config(True))
    assert isinstance(client, ReplayClient)


def test_replay_mode_with_no_cassettes_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CassetteError, match="no cassette"):
        make_llm_client(Settings(llm_mode="replay"), _config(True, str(tmp_path / "none")))


def test_live_mode_is_refused_until_the_live_client_exists() -> None:
    with pytest.raises(LiveModeUnavailable, match=r"S3\.04"):
        make_llm_client(Settings(llm_mode="live"), _config(False))


@pytest.mark.parametrize(("mode", "replay"), [("fake", True), ("replay", False), ("live", True)])
def test_a_mode_conflict_is_refused_before_any_client_is_built(mode: str, replay: bool) -> None:
    with pytest.raises(LlmModeConflict):
        make_llm_client(Settings(llm_mode=mode), _config(replay))  # type: ignore[arg-type]
