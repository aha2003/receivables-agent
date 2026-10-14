"""The LLM port (SPEC §3.5).

``LLMClient`` is what the rest of the app depends on. ``FakeClient`` and ``ReplayClient`` (and,
from S3.04, ``AnthropicClient``) implement it. Each method returns a validated Pydantic model;
invalid output raises ``LLMResponseInvalid``. Tests never call the real API.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from receivables.adapters.llm.schemas import Classification, DraftResponse, PlanResponse

__all__ = ["LLMClient"]


class LLMClient(Protocol):
    def classify(self, text: str, language_hint: str | None) -> Classification:
        """Classify one (already redacted) customer message (§7.1, §7.2)."""
        ...

    def plan(self, observation: Mapping[str, Any]) -> PlanResponse:
        """Propose a plan for one case from its observation (§18.6)."""
        ...

    def draft(self, facts: Mapping[str, Any]) -> DraftResponse:
        """Write a draft from the fact block only (§8.1, §8.3)."""
        ...
