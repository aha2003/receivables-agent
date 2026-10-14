"""Choosing the LLM client (SPEC §3.5; docs/DECISIONS.md D-029).

``make_llm_client`` builds the client for the effective mode: ``fake`` gives a FakeClient with no
scripts (every call raises, so callers take their specified fallback), ``replay`` gives a
ReplayClient over ``llm.cassette_dir``. ``live`` is refused until the Anthropic client exists
(S3.04); no test ever builds a live client.
"""

from pathlib import Path

from receivables.adapters.config import Config
from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.mode import resolve_llm_mode
from receivables.adapters.llm.replay import ReplayClient
from receivables.settings import Settings

__all__ = ["LiveModeUnavailable", "make_llm_client"]


class LiveModeUnavailable(RuntimeError):
    """``LLM_MODE=live`` was asked for before the live client exists."""


def make_llm_client(settings: Settings, config: Config) -> LLMClient:
    mode = resolve_llm_mode(settings, config)  # refuses a mode conflict first
    if mode == "fake":
        return FakeClient()
    if mode == "replay":
        return ReplayClient(Path(config.llm.cassette_dir))  # relative to the working directory
    raise LiveModeUnavailable("LLM_MODE=live is not available until S3.04; use fake or replay")
