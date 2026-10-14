"""Fact block and deterministic templates (SPEC §8.1, §8.4): T-DRF-04 and template checks."""

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from receivables.agent.facts import AgentFacts
from receivables.core.draft_validator import DEFAULT_INVOICE_REGEX, check_draft
from receivables.core.drafts import (
    DISPUTE_SENTENCES,
    FactBlock,
    build_fact_block,
    render_template,
)
from receivables.core.queue import draft_tone
from receivables.core.validation import DISPUTE_EVENT_LABELS

ROOT = Path(__file__).resolve().parents[2]
SPEC = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
SECTION_84 = SPEC[SPEC.index("### 8.4") : SPEC.index("## 9. ")]
SENDER = "Accounts Receivable Team"
MakeFacts = Callable[..., AgentFacts]


@pytest.fixture
def facts(make_agent_facts: MakeFacts) -> AgentFacts:
    return make_agent_facts()


def block(facts: AgentFacts, *numbers: str, tone: int, label: str | None = None) -> FactBlock:
    rows = [facts.rows[n] for n in numbers]
    return build_fact_block(
        rows,
        customer_name=facts.customers[rows[0].customer_id].customer_name,
        sender_name=SENDER,
        as_of=facts.as_of,
        tone=tone,
        dispute_label=label,
    )


@pytest.mark.parametrize(("days", "tone"), [(30, 1), (31, 2), (60, 2), (61, 3)])
def test_t_drf_04_tone_mapping(days: int, tone: int) -> None:
    assert draft_tone(days) == tone


# --- the template is the spec text --------------------------------------------------------------


def _spec_tone_1() -> tuple[str, str]:
    template = re.search(r"Tone 1:\n```\n(.*?)```", SECTION_84, re.S)
    assert template is not None
    subject_line, body = template.group(1).rstrip("\n").split("\n", 1)
    return subject_line.removeprefix("Subject: "), body


def _spec_sentence(tone: int) -> str:
    match = re.search(rf"Tone {tone} replaces .*?: `([^`]+)`", SECTION_84)
    assert match is not None
    return match.group(1)


def _spec_dispute_sentences() -> dict[str, str]:
    return dict(re.findall(r"^\| `([A-Z_]+)` \| `([^`]+)` \|$", SECTION_84, re.M))


def test_dispute_sentences_are_the_spec_table() -> None:
    assert _spec_dispute_sentences() == DISPUTE_SENTENCES
    assert set(DISPUTE_SENTENCES) == DISPUTE_EVENT_LABELS


def _expected(tone: int, label: str | None, **values: str) -> tuple[str, str]:
    subject, body = _spec_tone_1()
    if tone != 1:
        second = (
            "If payment has been made, please send the remittance details so we can allocate it."
        )
        assert second in body
        body = body.replace(second, _spec_sentence(tone))
    if label is not None:
        last = "please tell us so we can resolve it."
        body = body.replace(last, f"{last} {DISPUTE_SENTENCES[label]}", 1)
    for key, value in values.items():
        subject = subject.replace(f"{{{key}}}", value)
        body = body.replace(f"{{{key}}}", value)
    assert "{" not in subject + body
    return subject, body


@pytest.mark.parametrize("tone", [1, 2, 3])
@pytest.mark.parametrize("label", [None, *sorted(DISPUTE_EVENT_LABELS)])
def test_template_matches_the_spec_wording(facts: AgentFacts, tone: int, label: str | None) -> None:
    rendered = render_template(block(facts, "INV-004", tone=tone, label=label))
    assert rendered == _expected(
        tone,
        label,
        invoice_list="INV-004",
        amount_list="AED 50,000.00",
        due_date="2026-07-01",
        days_overdue="122",
        contact_name="Accounts Team",
        sender_name=SENDER,
    )


def test_template_for_inv_004_reads_as_expected(facts: AgentFacts) -> None:
    subject, body = render_template(block(facts, "INV-004", tone=3, label="PO_AMOUNT_MISMATCH"))
    assert subject == "Payment reminder: invoice INV-004"
    assert body == (
        "Dear Accounts Team,\n\n"
        "Our records show that invoice INV-004 (AED 50,000.00) was due on 2026-07-01 and is "
        "still unpaid. This invoice is now 122 days overdue. Please confirm a payment date within "
        "5 working days, or tell us what is preventing payment so we can resolve it. If there is "
        "a problem with the invoice, please tell us so we can resolve it. We are reviewing the "
        "difference between the purchase order and the invoice and will confirm the correct "
        "amount.\n\nKind regards,\nAccounts Receivable Team"
    )


def test_several_invoices_use_the_earliest_due_date_and_the_largest_age(
    facts: AgentFacts,
) -> None:
    # CUST-A: INV-013 (4,000; due 2026-09-30; 31 days) and INV-003 (15,000; due 2026-10-01; 30 days)
    subject, body = render_template(block(facts, "INV-003", "INV-013", tone=2))
    assert subject == "Payment reminder: invoice INV-003, INV-013"
    assert "invoice INV-003, INV-013 (AED 15,000.00, AED 4,000.00) was due on 2026-09-30" in body
    assert "This is now 31 days past the due date." in body


# --- fact block -----------------------------------------------------------------------------------


def test_fact_block_json_matches_section_8_1(facts: AgentFacts) -> None:
    fb = block(facts, "INV-004", tone=3, label="PO_AMOUNT_MISMATCH")
    assert fb.to_json() == {
        "customer_name": "Beta Contracting LLC",
        "contact_name": "Accounts Team",
        "sender_name": SENDER,
        "invoices": [
            {
                "invoice_number": "INV-004",
                "amount": "50000.00",
                "open_amount": "50000.00",
                "due_date": "2026-07-01",
                "days_overdue": 122,
            }
        ],
        "as_of": "2026-10-31",
        "tone": 3,
        "dispute_label": "PO_AMOUNT_MISMATCH",
        "amount_difference": None,
    }


def test_fact_block_keeps_invoice_and_open_amounts(facts: AgentFacts) -> None:
    fb = block(facts, "INV-005", tone=2)  # 30,000 invoice, 10,000 paid
    assert (fb.invoices[0].amount, fb.invoices[0].open_amount) == (3_000_000, 2_000_000)
    _, body = render_template(fb)
    assert "invoice INV-005 (AED 20,000.00)" in body  # the template shows the open amount


@pytest.mark.parametrize(
    ("numbers", "changes", "message"),
    [
        ((), {}, "at least one"),
        (("INV-003", "INV-003"), {}, "distinct"),
        (("INV-003", "INV-004"), {}, "one customer"),
        (("INV-001",), {}, "not overdue"),  # settled
        (("INV-010",), {}, "not overdue"),  # open but due on as_of: not overdue (§2)
        (("INV-012",), {}, None),  # 16 days overdue
        (("INV-003",), {"tone": 4}, "tone"),
        (("INV-003",), {"dispute_label": "UNCLEAR"}, "dispute label"),
        (("INV-003",), {"amount_difference": 0}, "above zero"),
    ],
)
def test_fact_block_refuses_bad_input(
    facts: AgentFacts, numbers: tuple[str, ...], changes: dict[str, object], message: str | None
) -> None:
    kwargs: dict[str, object] = {
        "customer_name": "x",
        "sender_name": SENDER,
        "as_of": facts.as_of,
        "tone": 1,
        **changes,
    }
    rows = [facts.rows[n] for n in numbers]
    if message is None:
        build_fact_block(rows, **kwargs)  # type: ignore[arg-type]
        return
    with pytest.raises(ValueError, match=message):
        build_fact_block(rows, **kwargs)  # type: ignore[arg-type]


# --- every template passes the validator ----------------------------------------------------------


# make_agent_facts is a pure factory, so sharing it across examples is safe.
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_every_template_passes_the_validator(
    make_agent_facts: MakeFacts, data: st.DataObject
) -> None:
    facts = make_agent_facts()
    queue = sorted(facts.queue_items)
    first = data.draw(st.sampled_from(queue))
    customer = facts.rows[first].customer_id
    same = [n for n in queue if facts.rows[n].customer_id == customer and n != first]
    extra = data.draw(st.lists(st.sampled_from(same), unique=True, max_size=3)) if same else []
    tone = data.draw(st.sampled_from([1, 2, 3]))
    label = data.draw(st.sampled_from([None, *sorted(DISPUTE_EVENT_LABELS)]))
    fb = block(facts, first, *extra, tone=tone, label=label)
    subject, body = render_template(fb)
    assert check_draft(subject, body, fb, max_words=140, invoice_regex=DEFAULT_INVOICE_REGEX) == ()


def test_templates_respect_the_as_of_date(make_agent_facts: MakeFacts) -> None:
    facts = make_agent_facts(as_of=date(2026, 9, 30))
    fb = block(facts, "INV-005", tone=1)
    subject, body = render_template(fb)
    assert (
        "AED 30,000.00" in body
        and check_draft(subject, body, fb, max_words=140, invoice_regex=DEFAULT_INVOICE_REGEX) == ()
    )
