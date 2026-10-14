"""The LLM port (SPEC §3.5; docs/DECISIONS.md D-029).

``LLMClient`` is what the rest of the app depends on. ``FakeClient`` and ``ReplayClient`` (and,
from S3.04, ``AnthropicClient``) implement it through ``RawLLMClient``: each turns a call into
a canonical request, obtains the raw response text and validates it with ``parse_response``,
so every client applies exactly the same checks. Invalid output raises
``LLMResponseInvalid``. Tests never call the real API.
"""

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Protocol

from receivables.adapters.llm.schemas import (
    Classification,
    DraftResponse,
    PlanResponse,
    parse_response,
)

__all__ = ["PROMPT_VERSIONS", "LLMClient", "RawLLMClient", "canonical_request", "request_key"]

# Bumped whenever a prompt changes, so recorded cassettes stop matching (S3.04, S3.06-S3.08).
PROMPT_VERSIONS = {"classify": "classify-v1", "plan": "plan-v1", "draft": "draft-v1"}


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


def canonical_request(kind: str, payload: Mapping[str, Any]) -> str:
    """The request as stable JSON: kind, prompt version and the call's inputs."""
    document = {"kind": kind, "prompt_version": PROMPT_VERSIONS[kind], "request": payload}
    try:
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError):
        # A date, Decimal or NaN must be turned into a string by the caller first.
        raise TypeError(f"the {kind} request is not plain JSON") from None


def request_key(kind: str, payload: Mapping[str, Any]) -> str:
    """The cassette key of a request: SHA-256 of its canonical JSON."""
    return hashlib.sha256(canonical_request(kind, payload).encode("utf-8")).hexdigest()


class RawLLMClient(ABC):
    """Shared request building and validation; subclasses only supply raw response text.

    Every request is checked to be plain JSON before any client sees it, so a payload that
    replay could not key is refused by FakeClient too.
    """

    def _complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        canonical_request(kind, payload)
        return self.complete(kind, payload)

    @abstractmethod
    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        """The raw text the model returned for this request."""

    def classify(self, text: str, language_hint: str | None) -> Classification:
        raw = self._complete("classify", {"text": text, "language_hint": language_hint})
        return parse_response("classify", raw, Classification)

    def plan(self, observation: Mapping[str, Any]) -> PlanResponse:
        raw = self._complete("plan", {"observation": observation})
        return parse_response("plan", raw, PlanResponse)

    def draft(self, facts: Mapping[str, Any]) -> DraftResponse:
        raw = self._complete("draft", {"facts": facts})
        return parse_response("draft", raw, DraftResponse)
