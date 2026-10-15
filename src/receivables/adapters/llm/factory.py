"""Choosing the LLM client (SPEC §3.5; docs/DECISIONS.md D-029, D-032).

``make_llm_client`` builds the client for the effective mode, always wrapped in
``RedactingClient`` (D-031): ``fake`` gives a FakeClient with no scripts (every call raises, so
callers take their specified fallback), ``replay`` gives a ReplayClient over
``llm.cassette_dir`` and ``live`` gives an AnthropicClient, which needs the API key and the three
model ids. Building a client sends nothing; no test ever calls the live API.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from receivables.adapters.config import Config
from receivables.adapters.llm.anthropic_client import AnthropicClient
from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.mode import resolve_llm_mode
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.replay import ReplayClient
from receivables.settings import Settings

__all__ = ["LiveModeUnavailable", "make_llm_client"]


class LiveModeUnavailable(RuntimeError):
    """``LLM_MODE=live`` without the API key or a model id (their values are never shown)."""


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
    models = {
        "planner": settings.anthropic_model_planner,
        "classifier": settings.anthropic_model_classifier,
        "drafter": settings.anthropic_model_drafter,
    }
    missing = [f"ANTHROPIC_MODEL_{role.upper()}" for role, model in models.items() if not model]
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        missing.insert(0, "ANTHROPIC_API_KEY")
    if missing or settings.anthropic_api_key is None:
        raise LiveModeUnavailable(f"LLM_MODE=live needs {', '.join(missing)} set")
    return AnthropicClient(
        api_key=settings.anthropic_api_key,
        models={role: model for role, model in models.items() if model},
        timeout_seconds=config.llm.timeout_seconds,
        max_retries=config.llm.max_retries,
    )
