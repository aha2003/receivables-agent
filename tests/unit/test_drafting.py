"""The drafting loop (SPEC §8.2, §8.4, §18.9): T-DRF-03 and its edges (D-036).

The drafter is a scripted function; nothing reaches a model. All facts are synthetic.
"""

import dataclasses
from collections.abc import Mapping
from datetime import date
from typing import Any

import pytest

from receivables.agent.drafting import (
    INVALID_OUTPUT,
    UNAVAILABLE,
    DraftInvalid,
    DraftUnavailable,
    TemplateViolation,
    generate_draft,
)
from receivables.core.draft_validator import DEFAULT_INVOICE_REGEX
from receivables.core.drafts import FactBlock, FactInvoice, render_template
from receivables.core.money import to_fils

FACTS = FactBlock(
    customer_name="Beta Contracting LLC",
    contact_name="Accounts Team",
    sender_name="Accounts Receivable Team",
    invoices=(
        FactInvoice(
            invoice_number="INV-004",
            amount=to_fils("50000.00"),
            open_amount=to_fils("50000.00"),
            due_date=date(2026, 7, 1),
            days_overdue=122,
        ),
    ),
    as_of=date(2026, 10, 31),
    tone=3,
    dispute_label="PO_AMOUNT_MISMATCH",
    amount_difference=to_fils("3000.00"),
)
GOOD = (
    "Invoice INV-004",
    "Dear Accounts Team,\n\nInvoice INV-004 (AED 50,000.00) was due on 2026-07-01. We are "
    "reviewing the AED 3,000.00 difference for [CUSTOMER]. Please confirm a payment date.\n\n"
    "Kind regards,\nAccounts Receivable Team",
)
VIOLATING = ("Invoice INV-005", "INV-005 for AED 51,000 is unpaid; we will go to court.")


class Script:
    def __init__(self, *answers: tuple[str, str] | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, facts: Mapping[str, Any]) -> tuple[str, str]:
        self.calls.append(facts)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def draft(writer: Any, *, regenerations: int = 2, facts: FactBlock = FACTS) -> Any:
    return generate_draft(
        facts,
        writer,
        max_regenerations=regenerations,
        max_words=140,
        invoice_regex=DEFAULT_INVOICE_REGEX,
        recipient="accounts@beta.example",
        sender_domain=None,
    )


def test_drf_03_three_violating_drafts_give_the_template() -> None:
    writer = Script(VIOLATING, VIOLATING, VIOLATING)
    outcome = draft(writer)
    assert len(writer.calls) == 3  # the first draft and max_regenerations (2) more
    assert outcome.generator == "template"
    assert outcome.violations == ()
    assert (outcome.subject, outcome.body) == render_template(FACTS)
    assert outcome.attempts == (("FORBIDDEN_PHRASE", "UNKNOWN_AMOUNT", "UNKNOWN_INVOICE"),) * 3


def test_a_clean_draft_is_used_and_the_name_put_back() -> None:
    writer = Script(GOOD)
    outcome = draft(writer)
    assert (outcome.generator, outcome.violations, outcome.attempts) == ("llm", (), ())
    assert "for Beta Contracting LLC." in outcome.body and "[CUSTOMER]" not in outcome.body
    assert writer.calls == [FACTS.to_json()]


def test_a_regeneration_can_succeed() -> None:
    outcome = draft(Script(VIOLATING, GOOD))
    assert outcome.generator == "llm"
    assert outcome.attempts == (("FORBIDDEN_PHRASE", "UNKNOWN_AMOUNT", "UNKNOWN_INVOICE"),)


def test_invalid_output_counts_as_an_attempt() -> None:
    outcome = draft(Script(DraftInvalid(), DraftInvalid(), GOOD))
    assert outcome.generator == "llm"
    assert outcome.attempts == ((INVALID_OUTPUT,), (INVALID_OUTPUT,))


def test_an_unavailable_drafter_goes_straight_to_the_template() -> None:
    writer = Script(DraftUnavailable(), GOOD)
    outcome = draft(writer)
    assert (outcome.generator, outcome.attempts) == ("template", ((UNAVAILABLE,),))
    assert len(writer.calls) == 1


@pytest.mark.parametrize(("regenerations", "calls"), [(0, 1), (1, 2), (4, 5)])
def test_the_regeneration_limit(regenerations: int, calls: int) -> None:
    writer = Script(*[VIOLATING] * calls)
    assert draft(writer, regenerations=regenerations).generator == "template"
    assert len(writer.calls) == calls


@pytest.mark.parametrize(
    ("subject", "body", "code"),
    [
        ("Invoice INV-004", " ".join(["word"] * 141), "BODY_TOO_LONG"),
        ("Invoice INV-004", "Pay at https://pay-now.example/inv-004.", "UNKNOWN_CONTACT_OR_URL"),
        ("Invoice INV-004", "Dear [Customer], thank you.", "UNFILLED_PLACEHOLDER"),
        ("Invoice INV-004", "We charge interest from 2026-11-15.", "UNKNOWN_DATE"),
    ],
)
def test_every_rule_sends_a_draft_back(subject: str, body: str, code: str) -> None:
    outcome = draft(Script((subject, body), GOOD))
    assert outcome.generator == "llm" and outcome.attempts == ((code,),)


def test_the_restored_name_is_checked_too() -> None:
    # A customer name that looks like an invoice number would put an unknown invoice in the
    # draft once restored; the draft is checked as it will be saved.
    named = dataclasses.replace(FACTS, customer_name="ABC-12 Trading")
    outcome = draft(Script(GOOD, GOOD, GOOD), facts=named)
    assert outcome.generator == "template"
    assert outcome.attempts == (("UNKNOWN_INVOICE",),) * 3


def test_no_drafter_means_the_template() -> None:
    outcome = draft(None)
    assert (outcome.generator, outcome.attempts) == ("template", ())


@pytest.mark.parametrize("tone", [1, 2, 3])
def test_the_template_always_passes(tone: int) -> None:
    outcome = draft(None, facts=dataclasses.replace(FACTS, tone=tone))
    assert outcome.violations == ()


def test_a_violating_template_is_a_bug_not_a_draft() -> None:
    bad_sender = dataclasses.replace(FACTS, sender_name="Visit www.evil.example")
    with pytest.raises(TemplateViolation, match="UNKNOWN_CONTACT_OR_URL"):
        draft(None, facts=bad_sender)


def test_a_contact_token_is_both_a_contact_and_a_placeholder() -> None:
    outcome = draft(Script(("Invoice INV-004", "Write to [EMAIL] for remittance."), GOOD))
    assert outcome.attempts == (("UNFILLED_PLACEHOLDER", "UNKNOWN_CONTACT_OR_URL"),)


def test_the_name_is_restored_in_the_subject_too() -> None:
    subject = "Invoice INV-004 for [CUSTOMER]"
    outcome = draft(Script((subject, GOOD[1])))
    assert outcome.subject == "Invoice INV-004 for Beta Contracting LLC"
