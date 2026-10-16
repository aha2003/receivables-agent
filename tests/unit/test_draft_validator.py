"""Draft validator (SPEC §8.2): T-DRF-01, T-DRF-02 and detection edges."""

import dataclasses
from datetime import date

import pytest

from receivables.core.draft_validator import (
    BODY_TOO_LONG,
    DEFAULT_INVOICE_REGEX,
    FORBIDDEN_PHRASE,
    UNFILLED_PLACEHOLDER,
    UNKNOWN_AMOUNT,
    UNKNOWN_CONTACT_OR_URL,
    UNKNOWN_DATE,
    UNKNOWN_INVOICE,
    check_draft,
    mentions,
    validate_draft,
)
from receivables.core.drafts import FactBlock, FactInvoice
from receivables.core.money import to_fils

RX = DEFAULT_INVOICE_REGEX

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


@pytest.mark.spec("T-DRF-01")
def test_t_drf_01_valid_text_has_no_violations() -> None:
    text = (
        "Dear Ahmed, invoice INV-004 (AED 50,000.00) was due on 2026-07-01 and remains unpaid "
        "as of 31 October 2026. Please send remittance details if payment has been made."
    )
    assert validate_draft(text, FACTS, RX) == ()


@pytest.mark.spec("T-DRF-02")
def test_t_drf_02_exactly_the_four_violations() -> None:
    text = (
        "Invoice INV-005 for AED 51,000 due 2026-07-02 is unpaid; "
        "we will take legal action and go to court."
    )
    assert set(validate_draft(text, FACTS, RX)) == {
        UNKNOWN_INVOICE,
        UNKNOWN_AMOUNT,
        UNKNOWN_DATE,
        FORBIDDEN_PHRASE,
    }
    assert validate_draft(text, FACTS, RX) == tuple(sorted(validate_draft(text, FACTS, RX)))


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
    assert validate_draft(text, FACTS, RX) == violations


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
        (
            "AED \u0665\u0660\u066c\u0660\u0660\u0660\u066b\u0660\u0660",
            (),
        ),  # Arabic-Indic 50,000.00
        ("AED \u0665\u0661\u0660\u0660\u0660", (UNKNOWN_AMOUNT,)),  # Arabic-Indic 51000
        ("AED \uff15\uff11,\uff10\uff10\uff10", (UNKNOWN_AMOUNT,)),  # full-width 51,000
        ("A\u200bED 51,000", (UNKNOWN_AMOUNT,)),  # zero-width space inside the currency
        ("AED  51,000", (UNKNOWN_AMOUNT,)),  # two spaces
        ("AED\n51,000", (UNKNOWN_AMOUNT,)),
        ("XAED 51,000", (UNKNOWN_AMOUNT,)),
        ("AED ,51,000", (UNKNOWN_AMOUNT,)),
    ],
)
def test_amounts(text: str, violations: tuple[str, ...]) -> None:
    assert validate_draft(text, FACTS, RX) == violations


def test_invoice_amount_and_amount_difference_are_allowed() -> None:
    facts = dataclasses.replace(
        FACTS,
        invoices=(_invoice("INV-005", amount="30000.00", open_amount="20000.00"),),
        amount_difference=to_fils("3000.00"),
    )
    text = "Invoice INV-005 was AED 30,000.00; AED 20,000.00 is open; the difference is AED 3,000."
    assert validate_draft(text, facts, RX) == ()
    assert validate_draft("AED 3,000.01", facts, RX) == (UNKNOWN_AMOUNT,)


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
        ("due 2026-07-01T00:00", ()),
        ("due 2026-08-01T00:00", (UNKNOWN_DATE,)),  # touching other characters
        ("on2026-08-01", (UNKNOWN_DATE,)),
        ("due 2026/08/01", (UNKNOWN_DATE,)),
        ("due 1-8-2026", (UNKNOWN_DATE,)),
        ("due 1.8.2026", (UNKNOWN_DATE,)),
        ("due the 1st of August 2026", (UNKNOWN_DATE,)),
        ("due the 1st of July 2026", ()),
        ("due \u0661 August \u0662\u0660\u0662\u0666", (UNKNOWN_DATE,)),  # Arabic-Indic digits
        ("due 2026-08-0\u0661", (UNKNOWN_DATE,)),
        ("within 5 working days", ()),
        ("122 days overdue", ()),
    ],
)
def test_dates(text: str, violations: tuple[str, ...]) -> None:
    assert validate_draft(text, FACTS, RX) == violations


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
        "We may take legal actions.",
        "criminally liable",
        "criminals",
        "the courthouse",
        "a courtroom",
        "policemen",
        "policing",
        "lawsuits",
        "suing",
        "LEGAL\u200b ACTION",
    ],
)
def test_forbidden_phrases(text: str) -> None:
    assert validate_draft(text, FACTS, RX) == (FORBIDDEN_PHRASE,)


@pytest.mark.parametrize(
    "text",
    [
        "the issue you raised",
        "we pursue payment",
        "with courtesy",
        "our policy",
        "Sue from accounts",
    ],
)
def test_forbidden_phrases_match_whole_words(text: str) -> None:
    expected = (FORBIDDEN_PHRASE,) if "Sue" in text else ()
    assert validate_draft(text, FACTS, RX) == expected


# --- length -----------------------------------------------------------------------------------


def test_body_word_limit() -> None:
    assert (
        check_draft(
            "Reminder: INV-004", " ".join(["word"] * 140), FACTS, max_words=140, invoice_regex=RX
        )
        == ()
    )
    assert check_draft(
        "Reminder", " ".join(["word"] * 141), FACTS, max_words=140, invoice_regex=RX
    ) == (BODY_TOO_LONG,)


def test_check_draft_reads_the_subject_too() -> None:
    assert check_draft("Invoice INV-005", "Body.", FACTS, max_words=140, invoice_regex=RX) == (
        UNKNOWN_INVOICE,
    )


# --- mentions (fixed text such as the sender name) ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("Accounts Receivable Team", ()),
        ("Court Services", (FORBIDDEN_PHRASE,)),
        ("AR-2026 Team", (UNKNOWN_INVOICE,)),
        ("AED 1 Ltd", (UNKNOWN_AMOUNT,)),
        ("Team 1 July 2026", (UNKNOWN_DATE,)),
    ],
)
def test_mentions_flags_any_fact_like_content(text: str, codes: tuple[str, ...]) -> None:
    assert mentions(text, RX) == codes


# --- §18.9: no URL or email address except the recipient's and the sender domain --------------

RECIPIENT = "accounts@beta.example"
DOMAIN = "supplier.example"


def contact(text: str, recipient: str | None = RECIPIENT, domain: str | None = DOMAIN) -> bool:
    found = validate_draft(text, FACTS, RX, recipient=recipient, sender_domain=domain)
    return UNKNOWN_CONTACT_OR_URL in found


@pytest.mark.parametrize(
    "text",
    [
        "Please reply to accounts@beta.example.",
        "Please reply to ACCOUNTS@Beta.Example.",
        "Questions? Write to ar@supplier.example or see https://supplier.example/statement.",
        "Our portal: https://billing.supplier.example/pay and www.supplier.example.",
        "No contact details at all.",
        "Amounts like AED 50,000.00 and e.g. or i.e. are not links.",
        "Due 2026-07-01. Version 1.2 and U.A.E. and No. 5 are not links.",
        "Dear [CUSTOMER], thank you.",  # the name placeholder is restored later (D-031)
    ],
)
def test_allowed_contacts(text: str) -> None:
    assert not contact(text)


@pytest.mark.parametrize(
    "text",
    [
        "Write to someone@else.example instead.",
        "Write to accounts@beta.example.evil.com instead.",  # not the recipient
        "Pay at https://pay-now.example/inv-004 today.",
        "Pay at http://203.0.113.5/pay today.",
        "Visit www.beta-payments.com.",
        "Visit beta-payments.com to pay.",
        "Visit supplier.example.evil.com to pay.",  # the domain must end the host
        "Visit https://supplier.example@evil.example/ now.",  # user-info trick
        "Pay at hxxps://evil.example/x.",  # any scheme counts
        "Fetch ftp://203.0.113.5/remittance today.",  # any scheme, even without a domain name
        "Pay at bit.ly/abc today.",  # bare hosts on any top-level domain (spec review)
        "See evil.ru for details.",
        "Settle online at pay.online/beta.",
        "Visit evil.company now.",
        "Pay via 203.0.113.5/pay now.",
        "Open https:\\\\evil.ru now.",  # a backslash scheme
        "Visit evil\u3002com now.",  # an ideographic full stop
        "Visit evil.c\u03bfm now.",  # a Greek omicron in the top-level domain
        "Open hxxp://198.51.100.7/pay now.",
        "Pay at https://ev​il.example/x.",  # zero-width characters are removed first
        "Call us on [PHONE] or write to [EMAIL].",  # the model tried to add contact details
        "Transfer to [IBAN].",
    ],
)
def test_unknown_contacts_and_urls(text: str) -> None:
    assert contact(text)


def test_without_a_sender_domain_only_the_recipient_is_allowed() -> None:
    assert not contact("Reply to accounts@beta.example.", domain=None)
    assert contact("See https://supplier.example/x.", domain=None)
    assert contact("Reply to accounts@beta.example.", recipient=None, domain=None)


def test_check_draft_passes_the_contacts_through() -> None:
    found = check_draft(
        "Payment reminder: invoice INV-004",
        "Please reply to accounts@beta.example or visit https://elsewhere.example.",
        FACTS,
        max_words=140,
        invoice_regex=RX,
        recipient=RECIPIENT,
        sender_domain=DOMAIN,
    )
    assert found == (UNKNOWN_CONTACT_OR_URL,)


def test_a_sender_name_may_not_hold_a_link() -> None:
    assert mentions("Visit www.example.com", RX) == (UNKNOWN_CONTACT_OR_URL,)


def test_check_draft_allows_the_recipient_it_is_given() -> None:
    body = "Please send remittance details to accounts@beta.example."
    common = {"max_words": 140, "invoice_regex": RX}
    assert check_draft("Invoice INV-004", body, FACTS, recipient=RECIPIENT, **common) == ()
    assert check_draft("Invoice INV-004", body, FACTS, **common) == (UNKNOWN_CONTACT_OR_URL,)


@pytest.mark.parametrize(
    "text",
    ["Dear [Customer], thank you.", "Dear [CUSTOMER_NAME],", "Dear [ CUSTOMER ],",
     "Regards, [Your Name]", "Write to [EMAIL]."],
)  # fmt: skip
def test_an_unfilled_placeholder_is_a_violation(text: str) -> None:
    assert UNFILLED_PLACEHOLDER in validate_draft(text, FACTS, RX)


def test_text_without_brackets_has_no_placeholder() -> None:
    assert UNFILLED_PLACEHOLDER not in validate_draft("Invoice (INV-004) is due.", FACTS, RX)
