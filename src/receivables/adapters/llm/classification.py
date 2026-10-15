"""Classifying one message with the §7.2 fallback (SPEC §7.2; T-LLM-02; D-032).

``classify_message`` asks the client once, and once more if the output fails schema validation.
If both fail, or the call fails any other way (timeout, API error, nothing recorded, names
unreadable), it returns ``UNCLEAR`` with confidence 0, ``needs_review`` and an error code. No
exception from the LLM layer escapes. Whether the extracted values are true is checked next by
``verify_extraction`` (§7.4, S3.05).
"""

from dataclasses import dataclass

from receivables.adapters.llm.base import LLMClient
from receivables.adapters.llm.schemas import Classification, LLMCallFailed, LLMResponseInvalid

__all__ = ["ClassifyOutcome", "classify_message", "unclear"]


@dataclass(frozen=True, slots=True)
class ClassifyOutcome:
    classification: Classification
    needs_review: bool
    error: str | None  # an error code, never model output or message text
    attempts: int


def unclear() -> Classification:
    """The §7.2 fallback classification."""
    return Classification(
        label="UNCLEAR",
        confidence=0.0,
        invoice_numbers=[],
        po_numbers=[],
        amounts_mentioned=[],
        dates_mentioned=[],
        promised_payment_date=None,
        evidence_quote="",
        summary_en="",
    )


def classify_message(client: LLMClient, text: str, language_hint: str | None) -> ClassifyOutcome:
    error = "unknown"
    for attempt in (1, 2):
        try:
            result = client.classify(text, language_hint)
        except LLMResponseInvalid:
            error = "schema_invalid"
            continue  # §7.2: one retry when the output fails schema validation
        except LLMCallFailed as failure:
            return ClassifyOutcome(unclear(), True, type(failure).__name__, attempt)
        return ClassifyOutcome(result, False, None, attempt)
    return ClassifyOutcome(unclear(), True, error, 2)
