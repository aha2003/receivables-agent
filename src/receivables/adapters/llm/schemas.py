"""Response models for the LLM port (SPEC §7.2, §8.3, §18.6; docs/DECISIONS.md D-029).

Every client returns raw text; ``parse_response`` turns it into one of these models or raises
``LLMResponseInvalid``. The models only check shape: whether an extracted value is true is the
job of deterministic verification (§7.4), and whether a plan may run is ``validate_plan``
(§18.6, D-024). Models refuse unknown keys and never coerce types, so an amount written as a
JSON number (a float) is refused.
"""

import json
import re
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from receivables.core.validation import DISPUTE_LABELS

__all__ = [
    "KINDS",
    "AmountMentioned",
    "Classification",
    "DateMentioned",
    "DraftResponse",
    "LLMResponseInvalid",
    "PlanResponse",
    "PlanStepResponse",
    "parse_response",
]

MAX_EVIDENCE_CHARS = 200  # §7.2
MAX_SUMMARY_WORDS = 40  # §7.2
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")  # a plain amount as the model read it


class LLMResponseInvalid(ValueError):
    """The model's output is not valid JSON for the expected schema (no content is echoed)."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(f"{kind} response invalid: {reason}")
        self.kind = kind
        self.reason = reason


def _plain(value: str) -> str:
    """No control or invisible format characters in a short field."""
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value):
        raise ValueError("control or format characters are not allowed")
    return value


def _label(value: str) -> str:
    if value not in DISPUTE_LABELS:
        raise ValueError("label is not one of the 13 labels of §7.1")
    return value


def _decimal(value: str) -> str:
    if not _DECIMAL.fullmatch(value):
        raise ValueError("amount value must be a plain decimal string such as 47000.00")
    return value


def _words(limit: int) -> Any:
    def check(value: str) -> str:
        if len(value.split()) > limit:
            raise ValueError(f"at most {limit} words")
        return value

    return check


ShortText = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_plain)]
DateText = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_plain)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AmountMentioned(_Strict):
    value: Annotated[str, AfterValidator(_decimal)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    role: Literal["invoice_amount", "po_amount", "paid_amount", "other"]


class DateMentioned(_Strict):
    # What the message says; a date without a year is resolved later by code from sent_at (§7.2).
    value: DateText
    role: Literal["promised_payment", "payment_made", "other"]


class Classification(_Strict):
    """§7.2 classifier output."""

    label: Annotated[str, AfterValidator(_label)]
    confidence: float = Field(ge=0.0, le=1.0)  # uncalibrated model score, not money (§7.4)
    invoice_numbers: list[ShortText] = Field(max_length=20)
    po_numbers: list[ShortText] = Field(max_length=20)
    amounts_mentioned: list[AmountMentioned] = Field(max_length=20)
    dates_mentioned: list[DateMentioned] = Field(max_length=20)
    promised_payment_date: DateText | None
    evidence_quote: Annotated[str, Field(max_length=MAX_EVIDENCE_CHARS)]
    summary_en: Annotated[str, Field(max_length=400), AfterValidator(_words(MAX_SUMMARY_WORDS))]


class PlanStepResponse(_Strict):
    tool: Annotated[str, Field(min_length=1, max_length=64)]
    args: dict[str, Any]
    reason: Annotated[str, Field(max_length=500)]


class PlanResponse(_Strict):
    """§18.6 plan shape; tools, arguments and budgets are checked by ``validate_plan``."""

    invoice_number: ShortText
    steps: list[PlanStepResponse] = Field(max_length=20)


class DraftResponse(_Strict):
    """§8.3 drafter output; facts are checked by the draft validator (§8.2)."""

    subject: Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_plain)]
    body: Annotated[str, Field(min_length=1, max_length=4000)]


KINDS: dict[str, type[_Strict]] = {
    "classify": Classification,
    "plan": PlanResponse,
    "draft": DraftResponse,
}

_FIELD = re.compile(r"[a-z_]{1,40}")


def _location(loc: tuple[Any, ...]) -> str:
    """A field path for the error, without echoing unexpected keys from the model output."""
    parts = [
        str(p) if isinstance(p, int) or (isinstance(p, str) and _FIELD.fullmatch(p)) else "?"
        for p in loc
    ]
    return ".".join(parts) or "<root>"


def parse_response[Model: _Strict](kind: str, raw: str, model: type[Model]) -> Model:
    """Validate the raw output of a ``kind`` call (classify, plan or draft) as ``model``."""
    if KINDS.get(kind) is not model:
        raise ValueError(f"unknown call kind {kind!r} for {model.__name__}")
    try:
        json.loads(raw)
    except (ValueError, TypeError):
        raise LLMResponseInvalid(kind, "not JSON") from None
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        fields = sorted({_location(e["loc"]) for e in error.errors()})
        raise LLMResponseInvalid(kind, f"schema mismatch at {', '.join(fields)}") from None
