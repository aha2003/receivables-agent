"""Rule planner (SPEC §18.6): T-AGT-08, arguments per rule, boundaries and §6.5 agreement."""

import dataclasses
from collections.abc import Callable
from datetime import date

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from receivables.agent.facts import AgentFacts, PlaybookRecord
from receivables.agent.rules_planner import (
    DOCUMENT_DISPUTES,
    TASK_DISPUTES,
    Plan,
    PlanStep,
    plan_case,
)
from receivables.core.queue import draft_tone, next_action
from receivables.core.scoring import EventRecord

MakeFacts = Callable[..., AgentFacts]
AS_OF = date(2026, 10, 31)
EXPECTED_T_AGT_08 = {
    "INV-004": ("request_internal_document", "draft_message", "schedule_followup"),
    "INV-011": ("draft_message", "escalate", "schedule_followup"),
    "INV-009": ("draft_message", "escalate", "schedule_followup"),
    "INV-014": ("draft_message", "escalate", "schedule_followup"),
    "INV-005": ("draft_message", "schedule_followup"),
    "INV-013": ("draft_message", "schedule_followup"),
    "INV-008": ("draft_message", "schedule_followup"),
    "INV-003": ("draft_message", "send_email", "schedule_followup"),
    "INV-012": ("draft_message", "send_email", "schedule_followup"),
}


@pytest.fixture
def facts(make_agent_facts: MakeFacts) -> AgentFacts:
    return make_agent_facts()


def test_t_agt_08_tool_sequences_for_the_golden_queue(facts: AgentFacts) -> None:
    assert set(facts.queue_items) == set(EXPECTED_T_AGT_08)
    assert {n: plan_case(facts, n).tools for n in facts.queue_items} == EXPECTED_T_AGT_08


def test_golden_plan_arguments(facts: AgentFacts) -> None:
    assert plan_case(facts, "INV-004").to_json()["steps"] == [
        {
            "tool": "request_internal_document",
            "args": {"invoice_number": "INV-004", "doc_type": "po_copy", "owner_role": "finance"},
            "reason": "active dispute PO_AMOUNT_MISMATCH: po_copy needed from finance",
        },
        {
            "tool": "draft_message",
            "args": {
                "invoice_numbers": ["INV-004"],
                "tone": 3,
                "dispute_label": "PO_AMOUNT_MISMATCH",
            },
            "reason": "122 days overdue, active dispute PO_AMOUNT_MISMATCH",
        },
        {
            "tool": "schedule_followup",
            "args": {"invoice_number": "INV-004", "days": 3},
            "reason": "check again in 3 days",
        },
    ]
    escalate = plan_case(facts, "INV-011")
    assert [s.json_args() for s in escalate.steps] == [
        {"invoice_numbers": ["INV-011"], "tone": 3},
        {"invoice_number": "INV-011", "to_role": "finance"},
        {"invoice_number": "INV-011", "days": 3},
    ]
    assert [s.json_args() for s in plan_case(facts, "INV-005").steps] == [
        {"invoice_numbers": ["INV-005"], "tone": 2},
        {"invoice_number": "INV-005", "days": 5},
    ]
    promise = plan_case(facts, "INV-008")  # 42 days overdue, broken promise
    assert [s.json_args() for s in promise.steps] == [
        {"invoice_numbers": ["INV-008"], "tone": 2},
        {"invoice_number": "INV-008", "days": 3},
    ]
    assert [s.json_args() for s in plan_case(facts, "INV-003").steps] == [
        {"invoice_numbers": ["INV-003"], "tone": 1},
        {"draft_ref": "step:1"},
        {"invoice_number": "INV-003", "days": 7},
    ]


def test_plan_json_has_the_section_18_6_shape(facts: AgentFacts) -> None:
    plan = plan_case(facts, "INV-003").to_json()
    assert set(plan) == {"invoice_number", "steps"}
    assert all(set(step) == {"tool", "args", "reason"} for step in plan["steps"])


# --- every dispute label --------------------------------------------------------------------------


def _with_dispute(make_agent_facts: MakeFacts, invoice: str, label: str) -> AgentFacts:
    dispute = EventRecord(
        event_id="E-TEST",
        invoice_number=invoice,
        event_type="DISPUTE_OPENED",
        event_date=date(2026, 10, 20),
        label=label,
    )
    return make_agent_facts(extra_events=(dispute,))


@pytest.mark.parametrize(("label", "doc"), sorted(DOCUMENT_DISPUTES.items()))
def test_document_disputes(make_agent_facts: MakeFacts, label: str, doc: tuple[str, str]) -> None:
    facts = _with_dispute(make_agent_facts, "INV-003", label)
    plan = plan_case(facts, "INV-003")
    assert plan.tools == ("request_internal_document", "draft_message", "schedule_followup")
    assert (plan.steps[0].args["doc_type"], plan.steps[0].args["owner_role"]) == doc
    assert plan.steps[1].json_args() == {
        "invoice_numbers": ["INV-003"],
        "tone": 1,
        "dispute_label": label,
    }
    assert plan.steps[2].args["days"] == 3


def test_document_dispute_table_is_the_spec_table() -> None:
    assert DOCUMENT_DISPUTES == {
        "PO_AMOUNT_MISMATCH": ("po_copy", "finance"),
        "MISSING_PO": ("po_copy", "sales"),
        "MISSING_DELIVERY_NOTE": ("delivery_note", "operations"),
        "PRICE_OR_RATE_DISPUTE": ("contract_rate", "sales"),
    }


def test_task_dispute_owners_are_the_d_023_table() -> None:
    assert TASK_DISPUTES == {
        "GOODS_OR_SERVICE_ISSUE": "operations",
        "WRONG_ENTITY_OR_DETAILS": "finance",
    }


@pytest.mark.parametrize(("label", "owner"), sorted(TASK_DISPUTES.items()))
def test_task_disputes(make_agent_facts: MakeFacts, label: str, owner: str) -> None:
    facts = _with_dispute(make_agent_facts, "INV-011", label)
    plan = plan_case(facts, "INV-011")  # 62 days overdue: the dispute rule still wins
    assert plan.tools == ("create_task", "draft_message", "schedule_followup")
    task = plan.steps[0].args
    assert (task["owner_role"], task["due_date"]) == (owner, "2026-11-03")
    assert "INV-011" in task["text"]
    assert plan.steps[1].json_args() == {
        "invoice_numbers": ["INV-011"],
        "tone": 3,
        "dispute_label": label,
    }


def test_dispute_beats_broken_promise(make_agent_facts: MakeFacts) -> None:
    facts = _with_dispute(make_agent_facts, "INV-008", "MISSING_PO")
    assert plan_case(facts, "INV-008").tools[0] == "request_internal_document"


def test_resolved_dispute_falls_through_to_age(make_agent_facts: MakeFacts) -> None:
    resolved = EventRecord(
        event_id="E-RES",
        invoice_number="INV-004",
        event_type="DISPUTE_RESOLVED",
        event_date=date(2026, 10, 25),
    )
    facts = make_agent_facts(extra_events=(resolved,))
    assert plan_case(facts, "INV-004").tools == ("draft_message", "escalate", "schedule_followup")


def test_broken_promise_beats_age(make_agent_facts: MakeFacts) -> None:
    promise = EventRecord(
        event_id="E-PRM",
        invoice_number="INV-011",
        event_type="PROMISE_TO_PAY",
        event_date=date(2026, 10, 1),
        promise_date=date(2026, 10, 15),
    )
    facts = make_agent_facts(extra_events=(promise,))
    plan = plan_case(facts, "INV-011")
    assert plan.tools == ("draft_message", "schedule_followup")
    assert plan.steps[0].args["tone"] == 3 and plan.steps[1].args["days"] == 3


# --- age boundaries -------------------------------------------------------------------------------


def _aged(facts: AgentFacts, days: int) -> AgentFacts:
    row = facts.rows["INV-003"]
    rows = {**facts.rows, "INV-003": dataclasses.replace(row, days_overdue=days)}
    return dataclasses.replace(facts, rows=rows)


@pytest.mark.parametrize(
    ("days", "tools", "tone", "followup"),
    [
        (1, ("draft_message", "send_email", "schedule_followup"), 1, 7),
        (30, ("draft_message", "send_email", "schedule_followup"), 1, 7),
        (31, ("draft_message", "schedule_followup"), 2, 5),
        (60, ("draft_message", "schedule_followup"), 2, 5),
        (61, ("draft_message", "escalate", "schedule_followup"), 3, 3),
    ],
)
def test_age_boundaries(
    facts: AgentFacts, days: int, tools: tuple[str, ...], tone: int, followup: int
) -> None:
    plan = plan_case(_aged(facts, days), "INV-003")
    assert plan.tools == tools
    assert plan.steps[0].args["tone"] == tone
    assert plan.steps[-1].args["days"] == followup


@pytest.mark.parametrize("number", ["INV-001", "INV-010", "INV-006"])
def test_only_overdue_invoices_are_planned(facts: AgentFacts, number: str) -> None:
    with pytest.raises(ValueError, match="not overdue"):
        plan_case(facts, number)


def test_a_dispute_label_without_a_rule_is_refused(make_agent_facts: MakeFacts) -> None:
    # The importer only accepts the six dispute labels; facts built another way must not slip.
    facts = _with_dispute(make_agent_facts, "INV-003", "UNCLEAR")
    with pytest.raises(ValueError, match="no rule for dispute label"):
        plan_case(facts, "INV-003")


def test_unknown_invoice(facts: AgentFacts) -> None:
    with pytest.raises(KeyError):
        plan_case(facts, "INV-999")


# --- playbook gap ---------------------------------------------------------------------------------


def _playbook(customer: str, avg: float | None, gap: int) -> PlaybookRecord:
    return PlaybookRecord(
        customer_id=customer,
        promise_kept_rate=None,
        promises_counted=0,
        avg_days_to_reply=avg,
        replies_counted=3 if avg is not None else 0,
        followup_gap_days=gap,
        updated_at="2026-10-31T05:00:00Z",
    )


def test_playbook_gap_replaces_the_table_once_measured(make_agent_facts: MakeFacts) -> None:
    measured = make_agent_facts(playbooks=(_playbook("CUST-B", 5.0, 10),))
    assert [plan_case(measured, n).steps[-1].args["days"] for n in ("INV-004", "INV-005")] == [
        10,
        10,
    ]
    unmeasured = make_agent_facts(playbooks=(_playbook("CUST-B", None, 7),))
    assert [plan_case(unmeasured, n).steps[-1].args["days"] for n in ("INV-004", "INV-005")] == [
        3,
        5,
    ]


def test_task_due_date_follows_the_playbook_gap(make_agent_facts: MakeFacts) -> None:
    dispute = EventRecord(
        event_id="E-T",
        invoice_number="INV-011",
        event_type="DISPUTE_OPENED",
        event_date=date(2026, 10, 20),
        label="GOODS_OR_SERVICE_ISSUE",
    )
    facts = make_agent_facts(extra_events=(dispute,), playbooks=(_playbook("CUST-B", 4.0, 8),))
    assert plan_case(facts, "INV-011").steps[0].args["due_date"] == "2026-11-08"


# --- structure ------------------------------------------------------------------------------------


def test_plans_are_validated_against_the_registry() -> None:
    with pytest.raises(KeyError):
        PlanStep("wire_transfer", {}, "no")
    with pytest.raises(ValueError):
        PlanStep("schedule_followup", {"invoice_number": "INV-003", "days": 31}, "too far")
    step = PlanStep("get_case", {"invoice_number": "INV-003"}, "look")
    with pytest.raises(ValueError, match="1 to 5"):
        Plan("INV-003", (step,) * 6)
    with pytest.raises(ValueError, match="1 to 5"):
        Plan("INV-003", ())


def _expected_tools(action: str) -> set[tuple[str, ...]]:
    if action.startswith("Resolve dispute"):
        return {
            ("request_internal_document", "draft_message", "schedule_followup"),
            ("create_task", "draft_message", "schedule_followup"),
        }
    return {
        "Follow up broken promise to pay": {("draft_message", "schedule_followup")},
        "Escalate: call and send formal reminder": {
            ("draft_message", "escalate", "schedule_followup")
        },
        "Send firm reminder": {("draft_message", "schedule_followup")},
        "Send friendly reminder": {("draft_message", "send_email", "schedule_followup")},
    }[action]


# make_agent_facts is a pure factory, so sharing it across examples is safe.
@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    invoice=st.sampled_from(sorted(EXPECTED_T_AGT_08)),
    label=st.one_of(st.none(), st.sampled_from(sorted({*DOCUMENT_DISPUTES, *TASK_DISPUTES}))),
    broken=st.booleans(),
)
def test_planner_agrees_with_the_queue_next_action(
    make_agent_facts: MakeFacts, invoice: str, label: str | None, broken: bool
) -> None:
    extra: list[EventRecord] = []
    if label is not None:
        extra.append(
            EventRecord(
                event_id="E-D",
                invoice_number=invoice,
                event_type="DISPUTE_OPENED",
                event_date=date(2026, 10, 20),
                label=label,
            )
        )
    if broken:
        extra.append(
            EventRecord(
                event_id="E-P",
                invoice_number=invoice,
                event_type="PROMISE_TO_PAY",
                event_date=date(2026, 10, 21),
                promise_date=date(2026, 10, 25),
            )
        )
    facts = make_agent_facts(extra_events=tuple(extra))
    scored = facts.scored[invoice]
    plan = plan_case(facts, invoice)
    assert plan.tools in _expected_tools(next_action(scored))
    draft = next(step for step in plan.steps if step.tool == "draft_message")
    assert draft.args["tone"] == draft_tone(scored.days_overdue)  # §6.5 tone by age, every rule
    assert plan == plan_case(facts, invoice)  # deterministic


def _draft(*numbers: str) -> PlanStep:
    return PlanStep("draft_message", {"invoice_numbers": list(numbers), "tone": 1}, "draft")


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ((PlanStep("send_email", {"draft_ref": "step:1"}, "send"),), "earlier draft step"),
        ((_draft("INV-003"), PlanStep("send_email", {"draft_ref": "step:2"}, "s")), "earlier"),
        ((_draft("INV-003"), PlanStep("send_email", {"draft_ref": "step:3"}, "s")), "earlier"),
        (
            (
                PlanStep("get_case", {"invoice_number": "INV-003"}, "look"),
                PlanStep("send_email", {"draft_ref": "step:1"}, "send"),
            ),
            "earlier draft step",
        ),
        ((PlanStep("get_case", {"invoice_number": "INV-004"}, "look"),), "another invoice"),
        ((_draft("INV-013"),), "plan's invoice"),
    ],
)
def test_plan_consistency_is_enforced(steps: tuple[PlanStep, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Plan("INV-003", steps)


def test_valid_draft_ref_and_shared_draft_pass() -> None:
    Plan(
        "INV-003",
        (
            _draft("INV-003", "INV-013"),
            PlanStep("send_email", {"draft_ref": "step:1"}, "send"),
            PlanStep("send_email", {"draft_id": "D-1"}, "existing draft"),
        ),
    )


def test_step_arguments_are_frozen(facts: AgentFacts) -> None:
    plan = plan_case(facts, "INV-003")
    with pytest.raises(TypeError):
        plan.steps[0].args["tone"] = 3  # type: ignore[index]
    exported = plan.to_json()
    exported["steps"][0]["args"]["invoice_numbers"].append("INV-999")
    exported["steps"][0]["args"]["tone"] = 3
    assert plan.steps[0].json_args() == {"invoice_numbers": ["INV-003"], "tone": 1}


def test_step_arguments_are_copied_on_construction() -> None:
    args = {"invoice_numbers": ["INV-003"], "tone": 1}
    step = PlanStep("draft_message", args, "draft")
    args["invoice_numbers"].append("INV-999")
    assert step.json_args() == {"invoice_numbers": ["INV-003"], "tone": 1}


def test_a_dispute_without_a_label_is_refused(make_agent_facts: MakeFacts) -> None:
    facts = _with_dispute(make_agent_facts, "INV-003", "")
    with pytest.raises(ValueError, match="no rule for dispute label"):
        plan_case(facts, "INV-003")
