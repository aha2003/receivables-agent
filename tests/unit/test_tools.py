"""Closed tool registry (SPEC §18.3): T-AGT-03, argument validation and the read tools."""

import csv
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from receivables.agent.facts import AgentFacts, MessageHeader, PlaybookRecord, build_agent_facts
from receivables.agent.tools import LEDGER_TABLES, MAX_PLAN_STEPS, TOOL_REGISTRY, ToolResult
from receivables.core.config import EngineConfig
from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.money import to_fils
from receivables.core.scoring import EventRecord
from receivables.core.validation import CustomerRow

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 10, 31)


def _spec_tool_names() -> list[str]:
    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    section = spec[spec.index("### 18.3") : spec.index("### 18.4")]
    return re.findall(r"^\| `([a-z_]+)` \|", section, re.M)


def _customers() -> list[CustomerRow]:
    return [
        CustomerRow(
            customer_id=r["customer_id"],
            customer_name=r["customer_name"],
            trn=r["trn"],
            tin=r["trn"][:10],
            trade_license_no=r["trade_license_no"],
            payment_terms_days=int(r["payment_terms_days"]),
            contact_email=r["contact_email"],
        )
        for r in csv.DictReader((ROOT / "fixtures/customers.csv").open(encoding="utf-8"))
    ]


def facts_for(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
    *,
    as_of: date = AS_OF,
    extra_payments: tuple[PaymentRecord, ...] = (),
    playbooks: tuple[PlaybookRecord, ...] = (),
    messages: tuple[MessageHeader, ...] = (),
) -> AgentFacts:
    invoices, payments = golden_ledger_inputs
    return build_agent_facts(
        customers=_customers(),
        invoices=invoices,
        payments=[*payments, *extra_payments],
        events=golden_events,
        playbooks=playbooks,
        messages=messages,
        as_of=as_of,
        config=EngineConfig(),
    )


@pytest.fixture
def facts(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
) -> AgentFacts:
    return facts_for(golden_ledger_inputs, golden_events)


def run(name: str, args: dict[str, Any], facts: AgentFacts) -> ToolResult:
    spec = TOOL_REGISTRY[name]
    assert spec.handler is not None
    return spec.handler(spec.args_model.model_validate(args), facts)


# --- T-AGT-03 -------------------------------------------------------------------------------


class TestAgt03:
    def test_registry_names_are_exactly_the_eleven_spec_tools(self) -> None:
        names = _spec_tool_names()
        assert len(names) == 11
        assert set(TOOL_REGISTRY) == set(names)
        assert all(spec.name == name for name, spec in TOOL_REGISTRY.items())

    def test_registry_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            TOOL_REGISTRY["wire_transfer"] = TOOL_REGISTRY["get_case"]  # type: ignore[index]

    def test_no_tool_writes_ledger_tables(self) -> None:
        for spec in TOOL_REGISTRY.values():
            assert not spec.writes & LEDGER_TABLES, spec.name
            assert "audit_log" not in spec.writes  # appended by the audit writer only

    def test_no_argument_carries_amounts_recipients_sql_or_paths(self) -> None:
        exact = {"to", "cc", "bcc", "recipient", "recipients", "email", "address", "to_address"}
        parts = ("amount", "fils", "price", "balance", "total", "sql", "query", "path", "file")
        parts += ("url", "email", "recipient")
        for spec in TOOL_REGISTRY.values():
            for name in spec.args_model.model_fields:
                assert name not in exact, (spec.name, name)
                assert not any(p in name for p in parts), (spec.name, name)

    def test_only_send_email_is_external_and_policy_gated(self) -> None:
        external = {n for n, s in TOOL_REGISTRY.items() if s.kind == "external"}
        gated = {n for n, s in TOOL_REGISTRY.items() if s.policy_gated}
        assert external == gated == {"send_email"}

    def test_kinds_and_minimum_autonomy_match_the_spec(self) -> None:
        expected = {
            "get_case": ("read", 0),
            "get_customer_profile": ("read", 0),
            "get_unallocated_cash": ("read", 0),
            "classify_message": ("analysis", 0),
            "draft_message": ("prepare", 1),
            "request_internal_document": ("prepare", 1),
            "propose_allocation": ("prepare", 1),
            "create_task": ("internal_write", 1),
            "schedule_followup": ("internal_write", 1),
            "escalate": ("internal_write", 1),
            "send_email": ("external", 2),
        }
        assert {n: (s.kind, s.min_autonomy) for n, s in TOOL_REGISTRY.items()} == expected

    def test_read_tools_write_nothing_and_have_handlers(self) -> None:
        for spec in TOOL_REGISTRY.values():
            if spec.kind == "read":
                assert spec.writes == frozenset() and spec.handler is not None, spec.name
            else:
                assert spec.writes, spec.name

    def test_tools_module_has_no_database_or_network_access(self) -> None:
        source = (ROOT / "src/receivables/agent/tools.py").read_text(encoding="utf-8")
        for forbidden in ("sqlalchemy", "receivables.adapters", "INSERT", "UPDATE ", "DELETE"):
            assert forbidden not in source


# --- argument validation --------------------------------------------------------------------

VALID_ARGS: dict[str, dict[str, Any]] = {
    "get_case": {"invoice_number": "INV-003"},
    "get_customer_profile": {"customer_id": "CUST-A"},
    "get_unallocated_cash": {"customer_id": "CUST-A"},
    "classify_message": {"message_id": "M-001"},
    "draft_message": {"invoice_numbers": ["INV-004"], "tone": 3, "dispute_label": "MISSING_PO"},
    "request_internal_document": {
        "invoice_number": "INV-004",
        "doc_type": "po_copy",
        "owner_role": "finance",
    },
    "propose_allocation": {"payment_id": "P-005", "invoice_number": "INV-012"},
    "create_task": {
        "owner_role": "operations",
        "text": "Check delivery.",
        "due_date": "2026-11-05",
    },
    "schedule_followup": {"invoice_number": "INV-003", "days": 7},
    "escalate": {"invoice_number": "INV-011", "to_role": "finance"},
    "send_email": {"draft_ref": "step:1"},
}


def test_every_tool_has_valid_example_args() -> None:
    assert set(VALID_ARGS) == set(TOOL_REGISTRY)
    for name, args in VALID_ARGS.items():
        TOOL_REGISTRY[name].args_model.model_validate(args)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_case", {}),
        ("get_case", {"invoice_number": ""}),
        ("get_case", {"invoice_number": " INV-003"}),
        ("get_case", {"invoice_number": "INV-003\n"}),
        ("get_case", {"invoice_number": "X" * 65}),
        ("get_case", {"invoice_number": 3}),
        ("get_case", {"invoice_number": "INV-003", "amount": "1.00"}),
        ("draft_message", {"invoice_numbers": [], "tone": 1}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": 4}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": 0}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": "1"}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": 1.0}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": True}),
        ("draft_message", {"invoice_numbers": ["INV-003", "INV-003"], "tone": 1}),
        ("draft_message", {"invoice_numbers": [f"INV-{i}" for i in range(11)], "tone": 1}),
        ("draft_message", {"invoice_numbers": "INV-003", "tone": 1}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": 1, "dispute_label": "UNCLEAR"}),
        ("draft_message", {"invoice_numbers": ["INV-003"], "tone": 1, "to": "x@evil.example"}),
        ("request_internal_document", {**VALID_ARGS["request_internal_document"], "doc_type": "x"}),
        (
            "request_internal_document",
            {**VALID_ARGS["request_internal_document"], "owner_role": "ceo"},
        ),
        ("propose_allocation", {"payment_id": "P-005"}),
        ("create_task", {**VALID_ARGS["create_task"], "owner_role": "agent"}),
        ("create_task", {**VALID_ARGS["create_task"], "text": ""}),
        ("create_task", {**VALID_ARGS["create_task"], "text": "x" * 501}),
        ("create_task", {**VALID_ARGS["create_task"], "text": "bell\x07"}),
        ("create_task", {**VALID_ARGS["create_task"], "due_date": "2026-13-01"}),
        ("create_task", {**VALID_ARGS["create_task"], "due_date": "05/11/2026"}),
        ("create_task", {**VALID_ARGS["create_task"], "due_date": 1_790_000_000}),
        ("schedule_followup", {"invoice_number": "INV-003", "days": 0}),
        ("schedule_followup", {"invoice_number": "INV-003", "days": 31}),
        ("escalate", {"invoice_number": "INV-011", "to_role": "legal"}),
        ("send_email", {}),
        ("send_email", {"draft_id": "D-1", "draft_ref": "step:1"}),
        ("send_email", {"draft_ref": "step:0"}),
        ("send_email", {"draft_ref": f"step:{MAX_PLAN_STEPS + 1}"}),
        ("send_email", {"draft_ref": "D-1"}),
        ("send_email", {"draft_id": "D-1", "to": "attacker@evil.example"}),
    ],
)
def test_invalid_arguments_rejected(tool: str, args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TOOL_REGISTRY[tool].args_model.model_validate(args)


def test_task_text_may_span_lines_and_send_email_takes_an_existing_draft() -> None:
    TOOL_REGISTRY["create_task"].args_model.model_validate(
        {**VALID_ARGS["create_task"], "text": "Line one.\nLine two."}
    )
    TOOL_REGISTRY["send_email"].args_model.model_validate({"draft_id": "D-1"})


@given(st.sampled_from(sorted(TOOL_REGISTRY)), st.text(min_size=1, max_size=20))
def test_unknown_argument_names_always_refused(tool: str, extra: str) -> None:
    model: type[BaseModel] = TOOL_REGISTRY[tool].args_model
    if extra in model.model_fields:
        return
    with pytest.raises(ValidationError):
        model.model_validate({**VALID_ARGS[tool], extra: "x"})


def test_args_are_immutable() -> None:
    args = TOOL_REGISTRY["schedule_followup"].args_model.model_validate(
        VALID_ARGS["schedule_followup"]
    )
    with pytest.raises(ValidationError):
        args.days = 30  # type: ignore[misc]


# --- read tools on the golden data ----------------------------------------------------------


def test_get_case_disputed_invoice(facts: AgentFacts) -> None:
    result = run("get_case", {"invoice_number": "INV-004"}, facts)
    assert result.ok and result.data is not None
    d = result.data
    assert (d["customer_id"], d["amount"], d["open_amount"]) == ("CUST-B", "50000.00", "50000.00")
    assert (d["days_overdue"], d["bucket"], d["is_open"]) == (122, "90+", True)
    assert d["flags"] == {
        "active_dispute_label": "PO_AMOUNT_MISMATCH",
        "broken_promise": False,
        "unallocated_cash_on_account": None,
    }
    assert d["priority"] == {
        "risk_score": "81.7",
        "band": "High",
        "priority_value": "40833.3",
        "rank": 1,
        "next_action": "Resolve dispute: PO_AMOUNT_MISMATCH",
        "note": "Heuristic score. Not a probability.",
    }
    assert [e["event_id"] for e in d["events"]] == ["E-001"]
    assert d["messages"] == []
    assert d["playbook"]["followup_gap_days"] == 7


def test_get_case_broken_promise_and_closed_invoice(facts: AgentFacts) -> None:
    inv8 = run("get_case", {"invoice_number": "INV-008"}, facts).data
    assert inv8 is not None and inv8["flags"]["broken_promise"] is True
    inv1 = run("get_case", {"invoice_number": "INV-001"}, facts).data
    assert inv1 is not None
    assert (inv1["is_open"], inv1["open_amount"], inv1["settled_date"]) == (
        False,
        "0.00",
        "2026-08-10",
    )
    assert (inv1["days_overdue"], inv1["priority"]) == (None, None)


def test_get_case_respects_as_of(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
) -> None:
    earlier = facts_for(golden_ledger_inputs, golden_events, as_of=date(2026, 9, 30))
    assert run("get_case", {"invoice_number": "INV-006"}, earlier) == ToolResult(
        ok=False, error="UNKNOWN_INVOICE"
    )
    inv5 = run("get_case", {"invoice_number": "INV-005"}, earlier).data
    assert inv5 is not None and inv5["open_amount"] == "30000.00"


def test_get_case_unknown_invoice(facts: AgentFacts) -> None:
    assert run("get_case", {"invoice_number": "INV-999"}, facts) == ToolResult(
        ok=False, error="UNKNOWN_INVOICE"
    )


def test_get_case_shows_only_the_last_five_message_headers(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
) -> None:
    earlier = tuple(
        MessageHeader(
            message_id=f"M-{i:02d}",
            invoice_number="INV-003",
            customer_id="CUST-A",
            direction="inbound" if i % 2 else "outbound",
            sent_at=f"2026-10-{i:02d}T06:00:00Z",
            language="en",
        )
        for i in range(1, 8)
    )
    later = (
        MessageHeader(
            message_id="M-LATER",
            invoice_number="INV-003",
            customer_id="CUST-A",
            direction="inbound",
            sent_at="2026-11-02T06:00:00Z",
            language="en",
        ),
    )
    facts = facts_for(golden_ledger_inputs, golden_events, messages=(*earlier, *later))
    data = run("get_case", {"invoice_number": "INV-003"}, facts).data
    assert data is not None
    assert [m["message_id"] for m in data["messages"]] == [f"M-0{i}" for i in range(3, 8)]
    assert set(data["messages"][0]) == {"message_id", "direction", "sent_at", "language"}


def test_get_customer_profile(facts: AgentFacts) -> None:
    data = run("get_customer_profile", {"customer_id": "CUST-A"}, facts).data
    assert data is not None
    assert data["payment_terms_days"] == 30
    assert (data["history"]["paid"], data["history"]["late"]) == (2, 2)
    assert (data["open_invoices"], data["open_total"]) == (3, "25000.00")
    assert (data["overdue_invoices"], data["overdue_total"]) == (2, "19000.00")
    assert data["unallocated_total"] == "0.00"
    assert run("get_customer_profile", {"customer_id": "CUST-Z"}, facts) == ToolResult(
        ok=False, error="UNKNOWN_CUSTOMER"
    )


def test_customer_profile_without_history(facts: AgentFacts) -> None:
    data = run("get_customer_profile", {"customer_id": "CUST-B"}, facts).data
    assert data is not None
    assert data["history"] == {"window_days": 365, "paid": 0, "late": 0, "late_rate": None}


def test_playbook_row_is_used_when_present(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
) -> None:
    playbook = PlaybookRecord(
        customer_id="CUST-A",
        promise_kept_rate=0.75,
        promises_counted=4,
        avg_days_to_reply=None,
        replies_counted=1,
        followup_gap_days=5,
        updated_at="2026-10-31T05:00:00Z",
    )
    facts = facts_for(golden_ledger_inputs, golden_events, playbooks=(playbook,))
    data = run("get_customer_profile", {"customer_id": "CUST-A"}, facts).data
    assert data is not None
    assert data["playbook"] == {
        "promise_kept_rate": "0.75",
        "promises_counted": 4,
        "avg_days_to_reply": None,
        "replies_counted": 1,
        "followup_gap_days": 5,
    }


def test_get_unallocated_cash(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
    facts: AgentFacts,
) -> None:
    assert run("get_unallocated_cash", {"customer_id": "CUST-C"}, facts).data == {
        "as_of": "2026-10-31",
        "customer_id": "CUST-C",
        "payments": [],
        "total": "0.00",
    }
    p5 = PaymentRecord(  # the T-POL-05 payment
        payment_id="P-005",
        invoice_number=None,
        customer_id="CUST-C",
        payment_date=date(2026, 10, 28),
        amount=to_fils("12000.00"),
    )
    later = PaymentRecord(
        payment_id="P-006",
        invoice_number=None,
        customer_id="CUST-C",
        payment_date=date(2026, 11, 3),
        amount=to_fils("500.00"),
    )
    with_cash = facts_for(golden_ledger_inputs, golden_events, extra_payments=(p5, later))
    data = run("get_unallocated_cash", {"customer_id": "CUST-C"}, with_cash).data
    assert data is not None
    assert data["payments"] == [
        {
            "payment_id": "P-005",
            "payment_date": "2026-10-28",
            "payment_type": "payment",
            "amount": "12000.00",
        }
    ]
    assert data["total"] == "12000.00"
    case = run("get_case", {"invoice_number": "INV-012"}, with_cash).data
    assert case is not None and case["flags"]["unallocated_cash_on_account"] == "12000.00"
    assert run("get_unallocated_cash", {"customer_id": "CUST-Z"}, facts).error == "UNKNOWN_CUSTOMER"


def test_read_tool_outputs_contain_no_customer_pii(facts: AgentFacts) -> None:
    pii = set()
    for c in facts.customers.values():
        pii |= {c.customer_name, c.contact_email or "", c.trn or "", c.trade_license_no or ""}
    pii.discard("")
    outputs = [run("get_case", {"invoice_number": n}, facts).data for n in facts.rows]
    for cid in facts.customers:
        outputs.append(run("get_customer_profile", {"customer_id": cid}, facts).data)
        outputs.append(run("get_unallocated_cash", {"customer_id": cid}, facts).data)
    text = json.dumps(outputs)
    assert not [p for p in pii if p in text]


def test_read_tools_are_deterministic(facts: AgentFacts) -> None:
    for name in ("get_case", "get_customer_profile", "get_unallocated_cash"):
        args = VALID_ARGS[name]
        assert run(name, args, facts) == run(name, args, facts)
