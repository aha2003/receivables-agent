"""Validation of untrusted plans (SPEC §18.6 step 4): every rejection reason and sanitising."""

from typing import Any

import pytest

from receivables.agent.plans import PLAN_REJECTED, PlanRejection, validate_plan
from receivables.agent.rules_planner import Plan

GOOD: dict[str, Any] = {
    "invoice_number": "INV-003",
    "steps": [
        {
            "tool": "draft_message",
            "args": {"invoice_numbers": ["INV-003"], "tone": 1},
            "reason": "r",
        },
        {"tool": "send_email", "args": {"draft_ref": "step:1"}, "reason": "tone 1 reminder"},
        {
            "tool": "schedule_followup",
            "args": {"invoice_number": "INV-003", "days": 7},
            "reason": "r",
        },
    ],
}


def step(tool: str = "get_case", **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args or {"invoice_number": "INV-003"}, "reason": "r"}


def test_the_section_18_6_example_is_accepted() -> None:
    plan = validate_plan(GOOD, "INV-003")
    assert isinstance(plan, Plan)
    assert plan.tools == ("draft_message", "send_email", "schedule_followup")
    assert plan.to_json() == GOOD


@pytest.mark.parametrize(
    ("raw", "reasons"),
    [
        ("not a plan", ("NOT_AN_OBJECT",)),
        ([GOOD], ("NOT_AN_OBJECT",)),
        ({**GOOD, "extra": 1}, ("UNEXPECTED_KEYS",)),
        ({**GOOD, "invoice_number": "INV-004"}, ("WRONG_INVOICE",)),
        ({"steps": GOOD["steps"]}, ("UNEXPECTED_KEYS", "WRONG_INVOICE")),
        ({**GOOD, "steps": "draft it"}, ("STEPS_NOT_A_LIST",)),
        ({**GOOD, "steps": []}, ("NO_STEPS",)),
        ({**GOOD, "steps": [step()] * 6}, ("TOO_MANY_STEPS",)),
        ({**GOOD, "steps": [step("wire_transfer", amount="1")]}, ("UNKNOWN_TOOL",)),
        ({**GOOD, "steps": [{"tool": 7, "args": {}, "reason": "r"}]}, ("UNKNOWN_TOOL",)),
        ({**GOOD, "steps": ["get_case"]}, ("INVALID_STEP",)),
        ({**GOOD, "steps": [{"tool": "get_case", "args": {}}]}, ("INVALID_STEP",)),
        ({**GOOD, "steps": [{**step(), "why": "x"}]}, ("INVALID_STEP",)),
        ({**GOOD, "steps": [{"tool": "get_case", "args": [], "reason": "r"}]}, ("INVALID_STEP",)),
        ({**GOOD, "steps": [{**step(), "reason": 5}]}, ("INVALID_STEP",)),
        (
            {**GOOD, "steps": [step("schedule_followup", invoice_number="INV-003", days=31)]},
            ("INVALID_ARGS",),
        ),
        ({**GOOD, "steps": [step("send_email", to="attacker@evil.example")]}, ("INVALID_ARGS",)),
        ({**GOOD, "steps": [step("get_case", invoice_number="INV-004")]}, ("INCONSISTENT_PLAN",)),
        ({**GOOD, "steps": [step("send_email", draft_ref="step:1")]}, ("INCONSISTENT_PLAN",)),
        (
            {**GOOD, "steps": [step("wire_transfer"), step("schedule_followup", days=0)]},
            ("INVALID_ARGS", "UNKNOWN_TOOL"),
        ),
    ],
)
def test_rejections(raw: object, reasons: tuple[str, ...]) -> None:
    result = validate_plan(raw, "INV-003")
    assert isinstance(result, PlanRejection)
    assert (result.reasons, result.status) == (reasons, PLAN_REJECTED)


def test_the_step_budget_can_be_lower_than_five() -> None:
    result = validate_plan(GOOD, "INV-003", max_steps=2)
    assert isinstance(result, PlanRejection) and result.reasons == ("TOO_MANY_STEPS",)


def test_rejections_keep_only_sanitised_tool_names() -> None:
    raw = {
        "invoice_number": "INV-003",
        "steps": [step("wire_transfer; DROP TABLE invoices\n" + "x" * 80)] + [{"no": "tool"}] * 12,
    }
    result = validate_plan(raw, "INV-003")
    assert isinstance(result, PlanRejection)
    assert result.tools[0] == "wire_transferDROPTABLEinvoices" + "x" * 10  # 40 characters
    assert result.tools[1:] == ("?",) * 9  # at most 10 names
    assert len(result.tools) == 10


def test_planner_reasons_are_plain_bounded_text() -> None:
    raw = {**GOOD, "steps": [{**GOOD["steps"][0], "reason": "ok‮\x07" + "y" * 300}]}
    plan = validate_plan(raw, "INV-003")
    assert isinstance(plan, Plan)
    assert plan.steps[0].reason == "ok" + "y" * 198
