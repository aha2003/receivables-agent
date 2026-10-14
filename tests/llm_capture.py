"""Prompt-capture harness (SPEC NFR-04, T-SEC-01; D-031).

``CapturingClient`` is a FakeClient that keeps the exact canonical request text of every call,
as it reaches the client: after ``RedactingClient`` has prepared it, and in the form every
client (Fake, Replay and, from S3.04, Anthropic) builds its prompt from. Tests assert on
``captured()``; nothing here touches the network.
"""

from collections.abc import Mapping
from typing import Any

from receivables.adapters.llm.base import canonical_request
from receivables.adapters.llm.fake import FakeClient

__all__ = ["CLASSIFICATION", "CapturingClient"]

# A schema-valid classification for M-01 (§6.10); the content does not matter to these tests.
CLASSIFICATION: dict[str, Any] = {
    "label": "PO_AMOUNT_MISMATCH",
    "confidence": 0.9,
    "invoice_numbers": ["INV-004"],
    "po_numbers": ["PO-B-004"],
    "amounts_mentioned": [],
    "dates_mentioned": [],
    "promised_payment_date": None,
    "evidence_quote": "Please correct.",
    "summary_en": "PO amount differs from the invoice.",
}


class CapturingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__(
            classify=lambda _: CLASSIFICATION,
            plan=lambda _: {"invoice_number": "INV-004", "steps": []},
            draft=lambda _: {"subject": "Invoice INV-004", "body": "Dear [CUSTOMER], ..."},
        )
        self._requests: list[str] = []

    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        self._requests.append(canonical_request(kind, payload))
        return super().complete(kind, payload)

    def captured(self) -> str:
        """Every request sent so far, as one text."""
        return "\n".join(self._requests)
