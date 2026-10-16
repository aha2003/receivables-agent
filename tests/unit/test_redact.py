"""Redaction and pseudonymisation before any text reaches the LLM (SPEC §7.5; D-030).

T-RED-01 plus the edges of each pattern. All values are synthetic: the TRN, IBAN, phone numbers
and addresses below belong to nobody.
"""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.core.redact import prepare_for_llm, pseudonymize, redact

T_RED_01_IN = (
    "Contact ahmed.k@beta.example or +971 50 123 4567 / 050 123 4567. "
    "TRN 100000000100003 applies. IBAN AE070331234567890123456."
)
T_RED_01_OUT = "Contact [EMAIL] or [PHONE] / [PHONE]. TRN [TRN] applies. IBAN [IBAN]."


@pytest.mark.spec("T-RED-01")
def test_red_01_output_matches_the_spec_exactly() -> None:
    assert redact(T_RED_01_IN) == T_RED_01_OUT


# --- TRN ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TRN 100000000100003", "TRN [TRN]"),
        ("TRN:100000000100003.", "TRN:[TRN]."),
        ("(100000000100003)", "([TRN])"),
        ("012345678901234", "[TRN]"),  # 15 digits starting with 0: TRN comes first in §7.5
        ("009715012345678", "[TRN]"),  # likewise, although it also looks like a phone number
        ("\u0661" * 15, "[TRN]"),  # Arabic-Indic digits
        ("10000000010000", "10000000010000"),  # 14 digits
        ("1000000001000031", "1000000001000031"),  # 16 digits: not a TRN
    ],
)
def test_trn(text: str, expected: str) -> None:
    assert redact(text) == expected


# --- IBAN --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("IBAN AE070331234567890123456", "IBAN [IBAN]"),
        ("AE070331234567890123456.", "[IBAN]."),
        ("IBAN AE07 0331 2345 6789 0123 456 thanks", "IBAN [IBAN] thanks"),  # printed groups
        ("iban ae070331234567890123456", "iban [IBAN]"),
        ("AE07033123456789012345", "AE07033123456789012345"),  # 20 digits
        ("AE0703312345678901234567", "AE0703312345678901234567"),  # 22 digits
        ("IBANAE070331234567890123456", "IBAN[IBAN]"),  # glued to a word
        ("AE07-0331-2345-6789-0123-456", "[IBAN]"),
        ("AE07\u00a00331\u00a02345\u00a06789\u00a00123\u00a0456", "[IBAN]"),
        ("AE07  0331 2345 6789 0123 456", "[IBAN]"),  # double space
        ("AE07   0331 2345 6789 0123 456", "[IBAN]"),
        ("IBAN AE07\n0331 2345 6789 0123 456", "IBAN [IBAN]"),  # wrapped across lines
        ("GB070331234567890123456", "GB070331234567890123456"),  # other countries: limit
    ],
)
def test_iban(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_a_grouped_trn_is_not_a_trn() -> None:
    # §7.5 TRNs are 15 digits in a row; grouped digits are a stated limit (D-030). The phone
    # rule may still mask part of them, which only reduces exposure.
    assert "[TRN]" not in redact("TRN 100 000 000 100 003")


def test_iban_digits_are_never_taken_for_a_trn() -> None:
    assert redact("AE070331234567890123456") == "[IBAN]"
    assert "[TRN]" not in redact("AE07 0331 2345 6789 0123 456")


# --- email -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ahmed.k@beta.example", "[EMAIL]"),
        ("Write to ar+ops@finance.beta.example.", "Write to [EMAIL]."),
        ("<accounts_team@gamma-trading.example>", "<[EMAIL]>"),
        ("a@b", "a@b"),  # no domain dot: not an address
        ("@beta.example", "@beta.example"),
        ("o'brien@beta.example", "[EMAIL]"),
        ("first.last+tag@sub.beta.example", "[EMAIL]"),
    ],
)
def test_email(text: str, expected: str) -> None:
    assert redact(text) == expected


# --- phone -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+971 50 123 4567", "[PHONE]"),
        ("+971501234567", "[PHONE]"),
        ("+971-50-123-4567", "[PHONE]"),
        ("00971 50 123 4567", "[PHONE]"),
        ("050 123 4567", "[PHONE]"),
        ("0501234567", "[PHONE]"),
        ("04 123 4567", "[PHONE]"),  # landline: 8 digits after the 0
        ("+971 4 123 4567", "[PHONE]"),
        ("Call 050 123 4567 today.", "Call [PHONE] today."),
        ("050 123 4567,050 765 4321", "[PHONE],[PHONE]"),
        ("050 123 4567/04 765 4321", "[PHONE]/[PHONE]"),
        ("(050) 123 4567", "(050) 123 4567"),  # a bracketed prefix is not matched: limit
        ("Tel.0501234567", "Tel.[PHONE]"),
        ("+971 (0)50 123 4567", "[PHONE]"),
        ("+971 (0) 4 123 4567", "[PHONE]"),
        ("00 971 50 123 4567", "[PHONE]"),
        ("+ 971 50 123 4567", "[PHONE]"),
        ("050\u2009123\u20094567", "[PHONE]"),  # thin spaces
        ("\u06450501234567", "\u0645[PHONE]"),  # joined to an Arabic letter
        ("\u06480501234567", "\u0648[PHONE]"),  # "and" (waw) written joined
        ("Mob-0501234567", "Mob-0501234567"),  # after a hyphen: treated as a reference (limit)
        ("INV0501234567", "INV0501234567"),
        ("050.123.4567", "050.123.4567"),  # dots: not matched, dates stay intact (limit)
        ("0123 4567", "0123 4567"),  # 7 digits after the 0: too short for a UAE number
        ("05012345678", "05012345678"),  # 10 digits after the 0: too long
        ("+971 50 123 45678", "+971 50 123 45678"),
        ("\u0660\u0665\u0660 \u0661\u0662\u0663 \u0664\u0665\u0666\u0667", "[PHONE]"),
        ("+\u0669\u0667\u0661 \u0665\u0660 \u0661\u0662\u0663 \u0664\u0665\u0666\u0667", "[PHONE]"),
    ],
)
def test_phone(text: str, expected: str) -> None:
    assert redact(text) == expected


# --- what the classifier needs survives (§7.2) --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "INV-004 and PO-B-004 are open.",
        "We paid AED 47,000.00 on 2026-10-28, not 50000.00.",
        "Payment on 01/11/2026 or 01.11.2026, ref 0042.",
        "Invoice INV-0001234567 is disputed.",
        "The total is 1,050,000.00 AED.",
        "Due 2026-07-01; 122 days overdue.",
        "Due 05.10.2026 10:30 as agreed.",
        "سندفع الفاتورة INV-003 الأسبوع القادم",
    ],
)
def test_facts_survive(text: str) -> None:
    assert redact(text) == text


# --- pseudonymisation ---------------------------------------------------------------------------

NAMES = ["Beta Contracting LLC", "Beta", "Gamma Trading (FZE)"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Beta Contracting LLC will pay.", "[CUSTOMER] will pay."),
        ("BETA CONTRACTING llc will pay.", "[CUSTOMER] will pay."),
        ("beta and Beta Contracting LLC", "[CUSTOMER] and [CUSTOMER]"),  # longest name first
        ("Gamma Trading (FZE) disputes it", "[CUSTOMER] disputes it"),  # parentheses are literal
        ("Gamma Trading FZE", "Gamma Trading FZE"),
        ("Alphabeta", "Alpha[CUSTOMER]"),  # every occurrence, as §7.5 says
    ],
)
def test_pseudonymize(text: str, expected: str) -> None:
    assert pseudonymize(text, NAMES) == expected


def test_blank_names_are_ignored() -> None:
    assert pseudonymize("Beta paid", ["", "   "]) == "Beta paid"
    assert pseudonymize("Beta paid", []) == "Beta paid"
    assert pseudonymize("Beta paid", [" Beta "]) == "[CUSTOMER] paid"


# --- the config switches ------------------------------------------------------------------------

TEXT = "Beta Contracting LLC: ahmed.k@beta.example, 050 123 4567"


@pytest.mark.parametrize(
    ("redact_on", "names_on", "expected"),
    [
        (True, True, "[CUSTOMER]: [EMAIL], [PHONE]"),
        (True, False, "Beta Contracting LLC: [EMAIL], [PHONE]"),
        # Without redaction the name is also replaced inside the address: "every occurrence".
        (False, True, "[CUSTOMER]: ahmed.k@[CUSTOMER].example, 050 123 4567"),
        (False, False, TEXT),
    ],
)
def test_prepare_for_llm_follows_the_switches(
    redact_on: bool, names_on: bool, expected: str
) -> None:
    result = prepare_for_llm(
        TEXT,
        redact_before_send=redact_on,
        pseudonymize_customer_names=names_on,
        customer_names=NAMES,
    )
    assert result == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # §7.5 order: an IBAN's 21 digits hold no 15-digit run, but a TRN inside an address is
        # masked before the address, and a 15-digit run is a TRN before it is a phone.
        ("100000000100003@beta.example", "[EMAIL]"),  # TRN first, then the whole address
        ("AE070331234567890123456@bank.example", "[EMAIL]"),  # IBAN first, likewise
        ("0501234567@beta.example", "[EMAIL]"),
        ("009715012345678", "[TRN]"),
        ("AE070331 234567890123456", "AE070331 [TRN]"),  # TRN before IBAN
    ],
)
def test_the_rules_run_in_spec_order(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_pseudonymisation_keeps_redaction_tokens_whole() -> None:
    text = redact("IBAN AE070331234567890123456, TRN 100000000100003, ahmed.k@beta.example")
    assert (
        pseudonymize(text, ["Iban", "TRN", "email"])
        == "[CUSTOMER] [IBAN], [CUSTOMER] [TRN], [EMAIL]"
    )
    assert pseudonymize("[CUSTOMER] and Beta", ["customer", "Beta"]) == "[CUSTOMER] and [CUSTOMER]"
    assert pseudonymize("[iban]", ["iban"]) == "[[CUSTOMER]]"  # only the exact tokens are kept


def test_redaction_runs_before_pseudonymisation() -> None:
    # A name that looks like a number is still redacted as the number first.
    result = prepare_for_llm(
        "Customer 100000000100003",
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=["100000000100003"],
    )
    assert result == "Customer [TRN]"


# --- properties ---------------------------------------------------------------------------------

_DIGIT = st.sampled_from("0123456789")
_FILLER = st.text(
    alphabet=st.characters(blacklist_categories=("Nd", "Cs"), blacklist_characters="@"),
    max_size=40,
)


@given(_FILLER)
def test_text_without_digits_or_at_signs_is_unchanged(text: str) -> None:
    assert redact(text) == text


@given(_FILLER, st.text(alphabet=_DIGIT, min_size=15, max_size=15), _FILLER)
def test_a_planted_trn_never_survives(before: str, trn: str, after: str) -> None:
    assert trn not in redact(f"{before} {trn} {after}")


@given(_FILLER, st.text(alphabet=_DIGIT, min_size=21, max_size=21), st.booleans(), _FILLER)
def test_a_planted_iban_never_survives(before: str, digits: str, grouped: bool, after: str) -> None:
    iban = "AE" + digits
    if grouped:
        iban = " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
    out = redact(f"{before} {iban} {after}")
    assert "[IBAN]" in out
    assert not re.search(r"\d", out)  # the filler has no digits of its own


@given(
    st.sampled_from(["+971", "00971", "0"]),
    st.sampled_from(["", " ", "-"]),
    st.text(alphabet=_DIGIT, min_size=8, max_size=9),
)
def test_a_planted_phone_never_survives(prefix: str, sep: str, number: str) -> None:
    phone = prefix + sep + sep.join([number[:2], number[2:5], number[5:]])
    assert redact(f"call {phone} now") == "call [PHONE] now"


@given(st.text(max_size=80))
def test_redaction_is_idempotent(text: str) -> None:
    once = redact(text)
    assert redact(once) == once


_NAME = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=12
).filter(lambda name: name.strip() != "")


@given(st.lists(_NAME, max_size=4), st.text(max_size=60), st.data())
def test_no_customer_name_survives(names: list[str], text: str, data: st.DataObject) -> None:
    planted = text
    for name in names:
        variant = data.draw(st.sampled_from([name, name.upper(), name.lower()]))
        planted = f"{planted} {variant} "
    tokens = r"\[(?:TRN|IBAN|EMAIL|PHONE|CUSTOMER)\]"
    pieces = re.split(tokens, pseudonymize(planted, names))  # what is left between tokens
    for name in names:
        pattern = re.compile(re.escape(name.strip()), re.IGNORECASE)
        assert not [piece for piece in pieces if pattern.search(piece)], name
