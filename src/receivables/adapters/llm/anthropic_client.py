"""The live Claude client (SPEC §3.1, §3.2, §7.2, §7.3; docs/DECISIONS.md D-032).

The only module allowed to import ``anthropic`` (and so ``httpx2``); tests never reach the
network with it (a mock transport stands in). Each call sends the cached system prompt, the
customer data as JSON in a separate user turn, and the §7.2 schema as a structured output; the
raw text then goes through the same Pydantic validation as every other client.

- API key and base URL are passed explicitly, so ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN``
  and profile files in the environment cannot redirect or re-authenticate the calls.
- Timeout and transport retries come from ``llm.timeout_seconds`` and ``llm.max_retries``; the
  schema retry of §7.2 is ``classify_message``'s job.
- This SDK has no temperature setting, so none is sent (§3.2 "where supported").
- Logs carry the model's role (``anthropic:classifier``), never its id.
- Classify (§7.3) and draft (§8.3) have live prompts. The planner prompt arrives in S3.08;
  until then plan calls fail as ``LLMCallFailed`` and callers use the rule planner.
"""

import json
import threading
from collections.abc import Mapping
from typing import Any

import anthropic
import httpx2
from pydantic import SecretStr

from receivables.adapters.llm.base import RawLLMClient
from receivables.adapters.llm.prompts import (
    CLASSIFICATION_SCHEMA,
    CLASSIFIER_SYSTEM,
    DRAFT_SCHEMA,
    DRAFTER_SYSTEM,
)
from receivables.adapters.llm.schemas import LLMCallFailed, LLMResponseInvalid

__all__ = ["API_BASE_URL", "AnthropicClient", "LLMUnavailable", "PromptNotAvailable"]

API_BASE_URL = "https://api.anthropic.com"
MAX_TOKENS = {"classify": 1024, "plan": 2048, "draft": 1024}
ROLES = {"classify": "classifier", "plan": "planner", "draft": "drafter"}
# Live prompts per call kind: (system prompt, output schema). The planner's arrives in S3.08.
PROMPTS: dict[str, tuple[str, dict[str, Any]]] = {
    "classify": (CLASSIFIER_SYSTEM, CLASSIFICATION_SCHEMA),
    "draft": (DRAFTER_SYSTEM, DRAFT_SCHEMA),
}


class LLMUnavailable(LLMCallFailed):
    """The API call failed (timeout, connection, status); only the error type is kept."""


class PromptNotAvailable(LLMCallFailed):
    """No live prompt for this call kind yet (planner S3.08, drafter S3.07)."""


class AnthropicClient(RawLLMClient):
    def __init__(
        self,
        *,
        api_key: SecretStr,
        models: Mapping[str, str],
        timeout_seconds: float,
        max_retries: int,
        http_client: httpx2.Client | None = None,
    ) -> None:
        missing = sorted(role for role in ROLES.values() if not models.get(role))
        if missing:
            raise ValueError(f"missing model id for: {', '.join(missing)}")
        self._models = dict(models)
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            base_url=API_BASE_URL,
            timeout=float(timeout_seconds),
            max_retries=max_retries,
            http_client=http_client,
        )
        # Per-call state is per thread: one client serves concurrent requests (threadpool).
        self._call = threading.local()

    def __repr__(self) -> str:
        return "AnthropicClient()"  # never the key or the model ids

    @property
    def model(self) -> str:
        """The model's role in this thread's last call (never its id)."""
        return getattr(self._call, "model", "anthropic")

    def usage(self) -> tuple[int | None, int | None]:
        return getattr(self._call, "usage", (None, None))

    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        self._call.usage = (None, None)
        self._call.model = f"anthropic:{ROLES[kind]}"
        if kind not in PROMPTS:
            raise PromptNotAvailable(f"no live {kind} prompt yet")
        system, schema = PROMPTS[kind]
        try:
            message = self._client.messages.create(
                model=self._models[ROLES[kind]],
                max_tokens=MAX_TOKENS[kind],
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": _user_turn(kind, payload)}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIError as error:
            raise LLMUnavailable(type(error).__name__) from None
        usage = message.usage
        # With caching, cached prompt tokens are reported apart from input_tokens; all count.
        processed = (
            usage.input_tokens
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0)
        )
        self._call.usage = (processed, usage.output_tokens)
        if message.stop_reason != "end_turn":
            raise LLMResponseInvalid(kind, f"stopped: {message.stop_reason}")
        # No text block gives "", which fails validation as "not JSON" like any bad output.
        return "".join(block.text for block in message.content if block.type == "text")


def _user_turn(kind: str, payload: Mapping[str, Any]) -> str:
    """The data as one JSON document, kept apart from the instructions (§7.3, §8.3)."""
    if kind == "draft":
        return "FACTS " + json.dumps(payload.get("facts", {}), ensure_ascii=False, sort_keys=True)
    return json.dumps(
        {"language_hint": payload.get("language_hint"), "message": payload.get("text", "")},
        ensure_ascii=False,
    )
