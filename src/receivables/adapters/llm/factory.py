"""Choosing the LLM client (SPEC §3.5; docs/DECISIONS.md D-029).

``make_llm_client`` builds the client for the effective mode, always wrapped in
``RedactingClient`` (D-031): ``fake`` gives a FakeClient with no
scripts (every call raises, so callers take their specified fallback), ``replay`` gives a
ReplayClient over ``llm.cassette_dir``. ``live`` is refused until the Anthropic client exists
(S3.04); no test ever builds a live client.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from receivables.adapters.config import Config
from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.mode import resolve_llm_mode
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.replay import ReplayClient
from receivables.settings import Settings

__all__ = ["LiveModeUnavailable", "make_llm_client"]


class LiveModeUnavailable(RuntimeError):
    """``LLM_MODE=live`` was asked for before the live client exists."""


def make_llm_client(
    settings: Settings, config: Config, *, customer_names: Callable[[], Iterable[str]]
) -> RedactingClient:
    """The client for the effective mode, always behind redaction (§7.5, NFR-04; D-031)."""
    return RedactingClient(
        _raw_client(settings, config),
        redact_before_send=config.llm.redact_before_send,
        pseudonymize_customer_names=config.llm.pseudonymize_customer_names,
        customer_names=customer_names,
    )


def _raw_client(settings: Settings, config: Config) -> LLMClient:
    mode = resolve_llm_mode(settings, config)  # refuses a mode conflict first
    if mode == "fake":
        return FakeClient()
    if mode == "replay":
        return ReplayClient(Path(config.llm.cassette_dir))  # relative to the working directory
    raise LiveModeUnavailable("LLM_MODE=live is not available until S3.04; use fake or replay")
