"""The LLM as the agent's drafter port (SPEC §8.2, §8.3; D-036).

``make_draft_writer`` wraps the app's client (always behind redaction, so the drafter sees
``[CUSTOMER]`` instead of the name): an answer that is not a valid ``{subject, body}`` is
``DraftInvalid`` (one attempt used), any other failure ``DraftUnavailable`` (template now).
"""

from collections.abc import Mapping
from typing import Any

from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.schemas import LLMCallFailed, LLMResponseInvalid
from receivables.agent.drafting import DraftInvalid, DraftUnavailable, DraftWriter

__all__ = ["make_draft_writer"]


def make_draft_writer(client: LLMClient) -> DraftWriter:
    def write(facts: Mapping[str, Any]) -> tuple[str, str]:
        try:
            draft = client.draft(facts)
        except LLMResponseInvalid:
            raise DraftInvalid from None
        except LLMCallFailed as error:
            raise DraftUnavailable(type(error).__name__) from None
        return draft.subject, draft.body

    return write
