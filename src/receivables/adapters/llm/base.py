"""The LLM port (SPEC §3.5; docs/DECISIONS.md D-029).

``LLMClient`` is what the rest of the app depends on. ``FakeClient`` and ``ReplayClient`` (and,
from S3.04, ``AnthropicClient``) implement it through ``RawLLMClient``: each turns a call into
a canonical request, obtains the raw response text and validates it with ``parse_response``,
so every client applies exactly the same checks. Invalid output raises
``LLMResponseInvalid``. Tests never call the real API.
"""

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Protocol

from receivables.adapters.llm.schemas import (
    Classification,
    DraftResponse,
    LLMResponseInvalid,
    PlanResponse,
    parse_response,
)

__all__ = ["PROMPT_VERSIONS", "LLMClient", "RawLLMClient", "canonical_request", "request_key"]

log = logging.getLogger("receivables.llm")

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
        prompt_sha256 = request_key(kind, payload)  # also refuses a request that is not JSON
        started = time.perf_counter()
        outcome = "ok"
        try:
            return self.complete(kind, payload)
        except Exception as error:
            outcome = type(error).__name__  # the type only: a message may hold data
            raise
        finally:
            input_tokens, output_tokens = self.usage()
            # NFR-07: model, latency, token counts and a prompt hash only; never the content.
            log.info(
                "llm.call",
                extra={
                    "kind": kind,
                    "model": str(getattr(self, "model", "unknown")),
                    "prompt_version": PROMPT_VERSIONS[kind],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "prompt_sha256": prompt_sha256,
                    "outcome": outcome,
                },
            )

    def usage(self) -> tuple[int | None, int | None]:
        """Input and output tokens of the last call, when the client knows them (S3.04)."""
        return None, None

    @staticmethod
    def _parse[M: Classification | PlanResponse | DraftResponse](
        kind: str, payload: Mapping[str, Any], raw: str, model: type[M]
    ) -> M:
        """Validate the response; a refusal is logged by field path only (D-035)."""
        try:
            return parse_response(kind, raw, model)
        except LLMResponseInvalid as error:
            # ``llm.call`` says the API answered; this says the answer was refused, and where.
            log.warning(
                "llm.response_invalid",
                extra={
                    "kind": kind,
                    "prompt_version": PROMPT_VERSIONS[kind],
                    "prompt_sha256": request_key(kind, payload),
                    "reason": error.reason,
                },
            )
            raise

    @abstractmethod
    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        """The raw text the model returned for this request."""

    def classify(self, text: str, language_hint: str | None) -> Classification:
        payload = {"text": text, "language_hint": language_hint}
        return self._parse("classify", payload, self._complete("classify", payload), Classification)

    def plan(self, observation: Mapping[str, Any]) -> PlanResponse:
        payload = {"observation": observation}
        return self._parse("plan", payload, self._complete("plan", payload), PlanResponse)

    def draft(self, facts: Mapping[str, Any]) -> DraftResponse:
        payload = {"facts": facts}
        return self._parse("draft", payload, self._complete("draft", payload), DraftResponse)
