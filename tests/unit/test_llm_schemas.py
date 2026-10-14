"""LLM response models (SPEC §7.2, §8.3, §18.6): shape checks and error messages.

Synthetic message content only. No client and no network are involved.
"""

import json
from typing import Any

import pytest

from receivables.adapters.llm.schemas import (
    Classification,
    DraftResponse,
    LLMResponseInvalid,
    PlanResponse,
    parse_response,
)
from receivables.core.validation import DISPUTE_LABELS

# The §7.2 example, with values made concrete.
CLASSIFICATION: dict[str, Any] = {
    "label": "PO_AMOUNT_MISMATCH",
    "confidence": 0.82,
    "invoice_numbers": ["INV-004"],
    "po_numbers": ["PO-B-004"],
    "amounts_mentioned": [{"value": "47000", "currency": "AED", "role": "po_amount"}],
    "dates_mentioned": [{"value": "20 Oct", "role": "promised_payment"}],
    "promised_payment_date": None,
    "evidence_quote": "our PO says AED 47,000 not 50,000",
    "summary_en": "Customer says the invoice amount does not match the purchase order.",
}
PLAN: dict[str, Any] = {  # the §18.6 example
    "invoice_number": "INV-003",
    "steps": [
        {
            "tool": "draft_message",
            "args": {"invoice_numbers": ["INV-003"], "tone": 1},
            "reason": "r",
        },
        {"tool": "send_email", "args": {"draft_ref": "step:1"}, "reason": "tone 1 reminder"},
    ],
}
DRAFT: dict[str, Any] = {
    "subject": "Payment reminder: invoice INV-003",
    "body": "Dear Accounts Team, ...",
}


def classify(changes: dict[str, Any]) -> Classification:
    return parse_response("classify", json.dumps({**CLASSIFICATION, **changes}), Classification)


def test_the_section_7_2_example_parses() -> None:
    result = parse_response("classify", json.dumps(CLASSIFICATION), Classification)
    assert result.label == "PO_AMOUNT_MISMATCH"
    assert result.amounts_mentioned[0].value == "47000"
    assert result.dates_mentioned[0].value == "20 Oct"  # year resolved later by code (§7.2)


@pytest.mark.parametrize("label", sorted(DISPUTE_LABELS))
def test_every_label_is_accepted(label: str) -> None:
    assert classify({"label": label}).label == label


@pytest.mark.parametrize("confidence", [0, 1, 0.0, 0.6])
def test_confidence_bounds_accepted(confidence: float) -> None:
    assert classify({"confidence": confidence}).confidence == confidence


@pytest.mark.parametrize(
    ("changes", "where"),
    [
        ({"label": "PAID"}, "label"),
        ({"confidence": 1.5}, "confidence"),
        ({"confidence": -0.1}, "confidence"),
        ({"confidence": "0.8"}, "confidence"),
        ({"confidence": True}, "confidence"),
        (
            {"amounts_mentioned": [{"value": 47000, "currency": "AED", "role": "other"}]},
            "amounts_mentioned.0.value",
        ),
        (
            {"amounts_mentioned": [{"value": 47000.5, "currency": "AED", "role": "other"}]},
            "amounts_mentioned.0.value",
        ),
        (
            {"amounts_mentioned": [{"value": "47,000", "currency": "AED", "role": "other"}]},
            "amounts_mentioned.0.value",
        ),
        (
            {"amounts_mentioned": [{"value": "1.005", "currency": "AED", "role": "other"}]},
            "amounts_mentioned.0.value",
        ),
        (
            {"amounts_mentioned": [{"value": "1", "currency": "aed", "role": "other"}]},
            "amounts_mentioned.0.currency",
        ),
        (
            {"amounts_mentioned": [{"value": "1", "currency": "AED", "role": "tax"}]},
            "amounts_mentioned.0.role",
        ),
        ({"dates_mentioned": [{"value": "2026-10-20", "role": "due"}]}, "dates_mentioned.0.role"),
        ({"invoice_numbers": ["INV\\u202e004"]}, "invoice_numbers.0"),
        ({"invoice_numbers": [""]}, "invoice_numbers.0"),
        ({"invoice_numbers": ["INV-1"] * 21}, "invoice_numbers"),
        ({"evidence_quote": "x" * 201}, "evidence_quote"),
        ({"summary_en": " ".join(["word"] * 41)}, "summary_en"),
        ({"promised_payment_date": 20261020}, "promised_payment_date"),
        ({"surprise": "x"}, "surprise"),
    ],
)
def test_bad_classifications_are_rejected(changes: dict[str, Any], where: str) -> None:
    raw = json.dumps({**CLASSIFICATION, **changes}).replace("\\\\u202e", "\\u202e")
    with pytest.raises(LLMResponseInvalid) as error:
        parse_response("classify", raw, Classification)
    assert error.value.kind == "classify"
    assert where in error.value.reason


@pytest.mark.parametrize("key", ["label", "confidence", "evidence_quote", "summary_en"])
def test_missing_fields_are_rejected(key: str) -> None:
    body = {k: v for k, v in CLASSIFICATION.items() if k != key}
    with pytest.raises(LLMResponseInvalid, match=key):
        parse_response("classify", json.dumps(body), Classification)


@pytest.mark.parametrize("raw", ["", "not json", "{", "[1, 2", "null", "[]", '"text"'])
def test_non_objects_and_bad_json_are_rejected(raw: str) -> None:
    with pytest.raises(LLMResponseInvalid) as error:
        parse_response("classify", raw, Classification)
    assert error.value.reason in {"not JSON"} or error.value.reason.startswith("schema mismatch")


def test_errors_never_echo_the_model_output() -> None:
    raw = json.dumps({**CLASSIFICATION, "Secret Customer Name": "x", "evidence_quote": "y" * 300})
    with pytest.raises(LLMResponseInvalid) as error:
        parse_response("classify", raw, Classification)
    message = str(error.value)
    assert "Secret Customer Name" not in message and "yyyy" not in message
    assert "evidence_quote" in message and "?" in message


def test_the_section_18_6_plan_parses() -> None:
    plan = parse_response("plan", json.dumps(PLAN), PlanResponse)
    assert [s.tool for s in plan.steps] == ["draft_message", "send_email"]


def test_plans_check_shape_only() -> None:
    # Unknown tools and budgets are rejected later by validate_plan (D-024), not here.
    unknown = {**PLAN, "steps": [{"tool": "wire_transfer", "args": {}, "reason": "r"}]}
    assert (
        parse_response("plan", json.dumps(unknown), PlanResponse).steps[0].tool == "wire_transfer"
    )
    for bad in (
        {**PLAN, "steps": [{"tool": "x", "args": [], "reason": "r"}]},
        {**PLAN, "steps": [{"tool": "x", "args": {}}]},
        {**PLAN, "steps": [{"tool": "x", "args": {}, "reason": "r"}] * 21},
        {"steps": PLAN["steps"]},
    ):
        with pytest.raises(LLMResponseInvalid):
            parse_response("plan", json.dumps(bad), PlanResponse)


def test_drafts() -> None:
    assert parse_response("draft", json.dumps(DRAFT), DraftResponse).subject.startswith("Payment")
    for bad in ({"subject": "", "body": "b"}, {"subject": "s"}, {**DRAFT, "html": "<b>"}):
        with pytest.raises(LLMResponseInvalid):
            parse_response("draft", json.dumps(bad), DraftResponse)


def test_the_kind_must_match_the_model() -> None:
    with pytest.raises(ValueError, match="unknown call kind"):
        parse_response("plan", json.dumps(DRAFT), DraftResponse)
    with pytest.raises(ValueError, match="unknown call kind"):
        parse_response("summarise", "{}", DraftResponse)


def test_models_are_immutable() -> None:
    result = classify({})
    with pytest.raises(ValueError):
        result.label = "UNCLEAR"  # type: ignore[misc]
