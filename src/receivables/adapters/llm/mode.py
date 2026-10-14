"""The effective LLM mode (SPEC §3.5; docs/DECISIONS.md D-029).

``LLM_MODE`` is the single source of truth. ``config.yaml`` ``llm.replay`` is only a cross-check:
if it disagrees (``true`` without replay mode, or replay mode with ``false``) the app refuses to
start rather than guess.
"""

from receivables.adapters.config import Config
from receivables.settings import LlmMode, Settings

__all__ = ["LlmModeConflict", "resolve_llm_mode"]


class LlmModeConflict(RuntimeError):
    """``LLM_MODE`` and ``config.yaml`` ``llm.replay`` disagree."""


def resolve_llm_mode(settings: Settings, config: Config) -> LlmMode:
    mode = settings.llm_mode
    if config.llm.replay != (mode == "replay"):
        raise LlmModeConflict(
            f"LLM_MODE is {mode!r} but config.yaml llm.replay is "
            f"{str(config.llm.replay).lower()}; make them agree (D-029)"
        )
    return mode
