"""Draft validator (SPEC §8.2): T-DRF-01, T-DRF-02 and detection edges."""

import dataclasses
from datetime import date

import pytest

from receivables.core.draft_validator import (
    BODY_TOO_LONG,
    FORBIDDEN_PHRASE,
    UNKNOWN_AMOUNT,
    UNKNOWN_DATE,
    UNKNOWN_INVOICE,
    check_draft,
    validate_draft,
)
from receivables.core.drafts import FactBlock, FactInvoice
from receivables.core.money import to_fils

# T-DRF-01/02 facts: invoices [INV-004], amounts [50000.00], dates [2026-07-01, 2026-10-31].
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
)


def test_t_drf_01_valid_text_has_no_violations() -> None:
    text = (
        "Dear Ahmed, invoice INV-004 (AED 50,000.00) was due on 2026-07-01 and remains unpaid "
        "as of 31 October 2026. Please send remittance details if payment has been made."
    )
    assert validate_draft(text, FACTS) == ()


def test_t_drf_02_exactly_the_four_violations() -> None:
    text = (
        "Invoice INV-005 for AED 51,000 due 2026-07-02 is unpaid; "
        "we will take legal action and go to court."
    )
    assert set(validate_draft(text, FACTS)) == {
        UNKNOWN_INVOICE,
        UNKNOWN_AMOUNT,
        UNKNOWN_DATE,
        FORBIDDEN_PHRASE,
    }
    assert validate_draft(text, FACTS) == tuple(sorted(validate_draft(text, FACTS)))


# --- invoices -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "violations"),
    [
        ("Invoice INV-004 is open.", ()),
        ("Invoices INV-004 and INV-004 again.", ()),
        ("Invoice INV-0040 is open.", (UNKNOWN_INVOICE,)),
        ("Invoice ABCDEF-12 is open.", (UNKNOWN_INVOICE,)),
        ("See PO-B-004.", ()),  # not invoice-like under the default regex
        ("Invoice inv-005 is open.", ()),  # the default regex is case-sensitive
    ],
)
def test_invoice_tokens(text: str, violations: tuple[str, ...]) -> None:
    assert validate_draft(text, FACTS) == violations


def _invoice(number: str, amount: str = "50000.00", open_amount: str = "50000.00") -> FactInvoice:
    return FactInvoice(
        invoice_number=number,
        amount=to_fils(amount),
        open_amount=to_fils(open_amount),
        due_date=date(2026, 7, 1),
        days_overdue=122,
    )


def test_custom_invoice_regex_with_groups_checks_the_whole_token() -> None:
    regex = r"\b(INV)/(\d{4})\b"
    facts = dataclasses.replace(FACTS, invoices=(_invoice("INV/0004"),))
    assert validate_draft("Invoice INV/0004 is open.", facts, regex) == ()
    assert validate_draft("Invoice INV/0005 is open.", facts, regex) == (UNKNOWN_INVOICE,)


# --- amounts --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "violations"),
    [
        ("AED 50,000.00", ()),
        ("AED 50000", ()),
        ("AED50,000", ()),
        ("aed 50,000.0", ()),
        ("AED 50,000.00.", ()),  # sentence full stop
        ("AED 51,000", (UNKNOWN_AMOUNT,)),
        ("aed 51,000", (UNKNOWN_AMOUNT,)),
        ("AED 50,000.01", (UNKNOWN_AMOUNT,)),
        ("AED 50,000.005", (UNKNOWN_AMOUNT,)),  # not silently read as 50,000.00
        ("AED 5,0000", (UNKNOWN_AMOUNT,)),  # unreadable grouping
        ("AED 50,000.00 and AED 1", (UNKNOWN_AMOUNT,)),
        ("a fee of 51,000 dirhams", ()),  # amounts are detected only after AED (§8.2)
    ],
)
def test_amounts(text: str, violations: tuple[str, ...]) -> None:
    assert validate_draft(text, FACTS) == violations


def test_invoice_amount_and_amount_difference_are_allowed() -> None:
    facts = dataclasses.replace(
        FACTS,
        invoices=(_invoice("INV-005", amount="30000.00", open_amount="20000.00"),),
        amount_difference=to_fils("3000.00"),
    )
    text = "Invoice INV-005 was AED 30,000.00; AED 20,000.00 is open; the difference is AED 3,000."
    assert validate_draft(text, facts) == ()
    assert validate_draft("AED 3,000.01", facts) == (UNKNOWN_AMOUNT,)


# --- dates ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "violations"),
    [
        ("due 2026-07-01", ()),
        ("as of 31 October 2026", ()),
        ("as of 31st Oct. 2026", ()),
        ("as of October 31, 2026", ()),
        ("due 1 July 2026", ()),
        ("due 01/07/2026", ()),  # day first
        ("due 2026-07-02", (UNKNOWN_DATE,)),
        ("due 2 July 2026", (UNKNOWN_DATE,)),
        ("due July 2, 2026", (UNKNOWN_DATE,)),
        ("due 07/01/2026", (UNKNOWN_DATE,)),  # 7 January, day first
        ("due 2026-13-01", (UNKNOWN_DATE,)),
        ("due 31 February 2026", (UNKNOWN_DATE,)),
        ("due 32/07/2026", (UNKNOWN_DATE,)),
    ],
)
def test_dates(text: str, violations: tuple[str, ...]) -> None:
    assert validate_draft(text, FACTS) == violations


# --- forbidden phrases -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We may take LEGAL ACTION.",
        "a lawsuit",
        "we will sue",
        "we sued before",
        "the Court",
        "small claims courts",
        "call the police",
        "a criminal matter",
        "arrest",
        "you will be blacklisted",
    ],
)
def test_forbidden_phrases(text: str) -> None:
    assert validate_draft(text, FACTS) == (FORBIDDEN_PHRASE,)


@pytest.mark.parametrize(
    "text", ["the issue you raised", "we pursue payment", "with courtesy", "Sue from accounts"]
)
def test_forbidden_phrases_match_whole_words(text: str) -> None:
    expected = (FORBIDDEN_PHRASE,) if "Sue" in text else ()
    assert validate_draft(text, FACTS) == expected


# --- length -----------------------------------------------------------------------------------


def test_body_word_limit() -> None:
    assert check_draft("Reminder: INV-004", " ".join(["word"] * 140), FACTS, max_words=140) == ()
    assert check_draft("Reminder", " ".join(["word"] * 141), FACTS, max_words=140) == (
        BODY_TOO_LONG,
    )


def test_check_draft_reads_the_subject_too() -> None:
    assert check_draft("Invoice INV-005", "Body.", FACTS, max_words=140) == (UNKNOWN_INVOICE,)
