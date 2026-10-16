"""The LLM as the agent's drafter port (SPEC §8.2, §8.3; D-036).

``make_draft_writer`` wraps the app's client (always behind redaction, so the drafter sees
``[CUSTOMER]`` instead of the name): an answer that is not a valid ``{subject, body}`` is
``DraftInvalid`` (one attempt used), any other failure ``DraftUnavailable`` (template now).
"""

from collections.abc import Mapping
from typing import Any

from receivables.adapters.llm.base import PROMPT_VERSIONS, LLMClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.schemas import LLMCallFailed, LLMResponseInvalid
from receivables.agent.drafting import (
    CUSTOMER_PLACEHOLDER,
    DraftInvalid,
    DraftUnavailable,
    DraftWriter,
)

__all__ = ["make_draft_writer"]


class _LlmDraftWriter:
    """The drafter port, with the model role and prompt version for the trace (§18.6)."""

    prompt_version = PROMPT_VERSIONS["draft"]

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    @property
    def model(self) -> str:
        """The drafter's model role (never an id), whatever the client did last (D-037)."""
        model_for = getattr(self._client, "model_for", None)
        return (
            str(model_for("draft"))
            if callable(model_for)
            else str(getattr(self._client, "model", "unknown"))
        )

    def __call__(self, facts: Mapping[str, Any]) -> tuple[str, str]:
        if isinstance(self._client, RedactingClient) and any(
            CUSTOMER_PLACEHOLDER in self._client.prepared_text(str(facts.get(key) or ""))
            for key in ("sender_name", "contact_name")
        ):
            # A customer name inside the sign-off or greeting would come back as the wrong
            # name when the placeholder is restored: draft from the template instead (D-036).
            raise DraftUnavailable("ambiguous customer placeholder")
        try:
            draft = self._client.draft(facts)
        except LLMResponseInvalid:
            raise DraftInvalid from None
        except LLMCallFailed as error:
            raise DraftUnavailable(type(error).__name__) from None
        return draft.subject, draft.body


def make_draft_writer(client: LLMClient) -> DraftWriter:
    return _LlmDraftWriter(client)
