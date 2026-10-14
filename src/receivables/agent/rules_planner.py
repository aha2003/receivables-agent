"""Deterministic rule planner (SPEC §18.6; docs/DECISIONS.md D-023).

Mirrors the next-action rules of §6.5: the first matching rule wins. Every step's arguments
are validated by the tool's own argument model (§18.3), so a rule plan is always a plan the
registry accepts. Pure: no clock, database or LLM.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from receivables.agent.facts import AgentFacts
from receivables.agent.tools import MAX_PLAN_STEPS, TOOL_REGISTRY
from receivables.core.queue import draft_tone
from receivables.core.scoring import active_dispute_label, is_broken_promise

__all__ = ["DOCUMENT_DISPUTES", "TASK_DISPUTES", "Plan", "PlanStep", "plan_case"]

# §18.6 rule 1: dispute label -> (doc_type, owner_role) for request_internal_document.
DOCUMENT_DISPUTES = {
    "PO_AMOUNT_MISMATCH": ("po_copy", "finance"),
    "MISSING_PO": ("po_copy", "sales"),
    "MISSING_DELIVERY_NOTE": ("delivery_note", "operations"),
    "PRICE_OR_RATE_DISPUTE": ("contract_rate", "sales"),
}
# §18.6 rule 2: dispute label -> owner_role of the create_task step ("operations or finance").
TASK_DISPUTES = {
    "GOODS_OR_SERVICE_ISSUE": "operations",
    "WRONG_ENTITY_OR_DETAILS": "finance",
}
_TASK_TEXT = {
    "GOODS_OR_SERVICE_ISSUE": "Check the goods or service issue the customer raised on {n}.",
    "WRONG_ENTITY_OR_DETAILS": "Confirm the customer's legal details for {n} and reissue it.",
}


@dataclass(frozen=True, slots=True)
class PlanStep:
    tool: str
    args: dict[str, Any]
    reason: str

    def __post_init__(self) -> None:
        TOOL_REGISTRY[self.tool].args_model.model_validate(self.args)  # KeyError or ValidationError


@dataclass(frozen=True, slots=True)
class Plan:
    invoice_number: str
    steps: tuple[PlanStep, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 1 <= len(self.steps) <= MAX_PLAN_STEPS:
            raise ValueError(f"a plan has 1 to {MAX_PLAN_STEPS} steps")

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(s.tool for s in self.steps)

    def to_json(self) -> dict[str, Any]:
        """The §18.6 plan schema."""
        return {
            "invoice_number": self.invoice_number,
            "steps": [{"tool": s.tool, "args": s.args, "reason": s.reason} for s in self.steps],
        }


def _followup(facts: AgentFacts, customer_id: str, table_days: int) -> int:
    """The table value, replaced by the playbook gap once replies have been measured (§18.7)."""
    playbook = facts.playbooks.get(customer_id)
    if playbook is None or playbook.avg_days_to_reply is None:
        return table_days
    return playbook.followup_gap_days


def plan_case(facts: AgentFacts, invoice_number: str) -> Plan:
    """The rule plan for one overdue invoice, as of ``facts.as_of``."""
    row = facts.rows.get(invoice_number)
    if row is None:
        raise KeyError(invoice_number)
    days = row.days_overdue
    if row.open_amount <= 0 or days is None or days <= 0:
        raise ValueError(f"{invoice_number} is not overdue")
    events = facts.events.get(invoice_number, ())
    label = active_dispute_label(events, facts.as_of)
    n = invoice_number

    def draft(tone: int, reason: str, with_label: bool = False) -> PlanStep:
        args: dict[str, Any] = {"invoice_numbers": [n], "tone": tone}
        if with_label:
            args["dispute_label"] = label
        return PlanStep("draft_message", args, reason)

    def followup(table_days: int) -> PlanStep:
        gap = _followup(facts, row.customer_id, table_days)
        return PlanStep(
            "schedule_followup", {"invoice_number": n, "days": gap}, f"check again in {gap} days"
        )

    def due(days_ahead: int) -> date:
        return facts.as_of + timedelta(days=days_ahead)

    age = f"{days} days overdue"
    if label in DOCUMENT_DISPUTES:
        doc_type, owner = DOCUMENT_DISPUTES[label]
        steps = (
            PlanStep(
                "request_internal_document",
                {"invoice_number": n, "doc_type": doc_type, "owner_role": owner},
                f"active dispute {label}: {doc_type} needed from {owner}",
            ),
            draft(draft_tone(days), f"{age}, active dispute {label}", with_label=True),
            followup(3),
        )
    elif label in TASK_DISPUTES:
        owner = TASK_DISPUTES[label]
        gap = _followup(facts, row.customer_id, 3)
        steps = (
            PlanStep(
                "create_task",
                {
                    "owner_role": owner,
                    "text": _TASK_TEXT[label].format(n=n),
                    "due_date": due(gap).isoformat(),
                },
                f"active dispute {label}: {owner} to act",
            ),
            draft(draft_tone(days), f"{age}, active dispute {label}", with_label=True),
            followup(3),
        )
    elif label is not None:
        raise ValueError(f"no rule for dispute label {label!r}")
    elif is_broken_promise(events, facts.as_of, is_open=True):
        steps = (draft(draft_tone(days), f"{age}, broken promise to pay"), followup(3))
    elif days > 60:
        steps = (
            draft(3, f"{age}, formal reminder"),
            PlanStep("escalate", {"invoice_number": n, "to_role": "finance"}, f"{age}"),
            followup(3),
        )
    elif days > 30:
        steps = (draft(2, f"{age}, firm reminder"), followup(5))
    else:
        steps = (
            draft(1, f"{age}, no blockers"),
            PlanStep("send_email", {"draft_ref": "step:1"}, "tone 1 reminder"),
            followup(7),
        )
    return Plan(invoice_number=n, steps=steps)
