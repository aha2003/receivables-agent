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
- Planner and drafter prompts arrive in S3.08 and S3.07; until then those calls fail as
  ``LLMCallFailed`` and callers fall back to the rule planner or the template.
"""

import json
from collections.abc import Mapping
from typing import Any

import anthropic
import httpx2
from pydantic import SecretStr

from receivables.adapters.llm.base import RawLLMClient
from receivables.adapters.llm.prompts import CLASSIFICATION_SCHEMA, CLASSIFIER_SYSTEM
from receivables.adapters.llm.schemas import LLMCallFailed, LLMResponseInvalid

__all__ = ["API_BASE_URL", "AnthropicClient", "LLMUnavailable", "PromptNotAvailable"]

API_BASE_URL = "https://api.anthropic.com"
MAX_TOKENS = {"classify": 1024, "plan": 2048, "draft": 1024}
ROLES = {"classify": "classifier", "plan": "planner", "draft": "drafter"}


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
        self._usage: tuple[int | None, int | None] = (None, None)
        self.model = "anthropic"

    def __repr__(self) -> str:
        return "AnthropicClient()"  # never the key or the model ids

    def usage(self) -> tuple[int | None, int | None]:
        return self._usage

    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        self._usage = (None, None)
        self.model = f"anthropic:{ROLES[kind]}"
        if kind != "classify":
            raise PromptNotAvailable(f"no live {kind} prompt yet")
        try:
            message = self._client.messages.create(
                model=self._models[ROLES[kind]],
                max_tokens=MAX_TOKENS[kind],
                system=[
                    {
                        "type": "text",
                        "text": CLASSIFIER_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": _user_turn(payload)}],
                output_config={"format": {"type": "json_schema", "schema": CLASSIFICATION_SCHEMA}},
            )
        except anthropic.APIError as error:
            raise LLMUnavailable(type(error).__name__) from None
        self._usage = (message.usage.input_tokens, message.usage.output_tokens)
        if message.stop_reason != "end_turn":
            raise LLMResponseInvalid(kind, f"stopped: {message.stop_reason}")
        # No text block gives "", which fails validation as "not JSON" like any bad output.
        return "".join(block.text for block in message.content if block.type == "text")


def _user_turn(payload: Mapping[str, Any]) -> str:
    """The customer data as one JSON document, kept apart from the instructions (§7.3)."""
    return json.dumps(
        {"language_hint": payload.get("language_hint"), "message": payload.get("text", "")},
        ensure_ascii=False,
    )
