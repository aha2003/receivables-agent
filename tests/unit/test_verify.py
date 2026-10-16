"""Deterministic verification of every extraction (SPEC §7.4; D-033).

T-VER-01 to T-VER-05 and T-LLM-03 against the golden ledger at 2026-10-31, plus the edges of
each rule. All messages and values are synthetic.
"""

from datetime import date
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.adapters.llm.classification import classify_message, to_extraction
from receivables.adapters.llm.fake import FakeClient
from receivables.core.ledger import InvoiceRecord, LedgerRow, PaymentRecord, build_ledger
from receivables.core.money import to_fils
from receivables.core.verify import AmountClaim, DateClaim, Extraction, Flag, verify_extraction

AS_OF = date(2026, 10, 31)
THRESHOLD = Fraction(3, 5)
AR_50000 = "\u0665\u0660\u066c\u0660\u0660\u0660"  # 50,000 in Arabic-Indic digits
AR_47000 = "\u0664\u0667\u066c\u0660\u0660\u0660"  # 47,000
M01 = (
    "We cannot process invoice INV-004 because purchase order PO-B-004 was issued for "
    "AED 47,000, but the invoice is for AED 50,000. Please correct."
)


@pytest.fixture
def ledger(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
) -> list[LedgerRow]:
    return build_ledger(*golden_ledger_inputs, AS_OF)


def extraction(**fields: Any) -> Extraction:
    base: dict[str, Any] = {
        "label": "NO_ACTION_NEEDED",
        "confidence": 0.9,
        "invoice_numbers": (),
        "po_numbers": (),
        "amounts": (),
        "dates": (),
        "promised_payment_date": None,
        "evidence_quote": "",
    }
    return Extraction(**(base | fields))


def verify(
    ex: Extraction,
    message: str,
    ledger: list[LedgerRow],
    *,
    customer: str | None = None,
    sent: str | None = None,
) -> Any:
    return verify_extraction(
        ex,
        message=message,
        sent_text=sent,
        ledger=ledger,
        message_customer_id=customer,
        review_threshold=THRESHOLD,
    )


def codes(result: Any) -> list[str]:
    return [f.code for f in result.flags]


# --- T-VER-01 to T-VER-05 ------------------------------------------------------------------------


@pytest.mark.spec("T-VER-01")
def test_ver_01_po_amount_difference(ledger: list[LedgerRow]) -> None:
    ex = extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=("INV-004",),
        po_numbers=("PO-B-004",),
        amounts=(
            AmountClaim("47000", "AED", "po_amount"),
            AmountClaim("50000", "AED", "invoice_amount"),
        ),
        evidence_quote="purchase order PO-B-004 was issued for AED 47,000",
    )
    result = verify(ex, M01, ledger, customer="CUST-B")
    assert result.amount_difference == to_fils("3000.00")
    assert result.flags == ()
    assert result.needs_review is False
    assert result.matched == ("INV-004",)


@pytest.mark.spec("T-VER-02")
def test_ver_02_unknown_invoice(ledger: list[LedgerRow]) -> None:
    message = "Invoice INV-9999 has no PO number on it."
    ex = extraction(
        label="MISSING_PO", invoice_numbers=("INV-9999",), evidence_quote="no PO number"
    )
    result = verify(ex, message, ledger)
    assert result.flags == (Flag("UNMATCHED_INVOICE", invoice_number="INV-9999"),)
    assert result.needs_review is True


@pytest.mark.spec("T-VER-03")
def test_ver_03_already_paid_against_the_ledger(ledger: list[LedgerRow]) -> None:
    open_one = verify(
        extraction(
            label="ALREADY_PAID", invoice_numbers=("INV-005",), evidence_quote="paid INV-005"
        ),
        "We paid INV-005 last week.",
        ledger,
    )
    assert open_one.flags == (
        Flag("LEDGER_STILL_OPEN", invoice_number="INV-005", amount=to_fils("20000.00")),
    )
    settled = verify(
        extraction(
            label="ALREADY_PAID", invoice_numbers=("INV-001",), evidence_quote="paid INV-001"
        ),
        "We paid INV-001 in August.",
        ledger,
    )
    assert settled.flags == ()


@pytest.mark.spec("T-VER-04")
def test_ver_04_evidence_not_verbatim(ledger: list[LedgerRow]) -> None:
    ex = extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=("INV-004",),
        evidence_quote="the PO says 47,000 and we will not pay",  # not in the message
    )
    result = verify(ex, M01, ledger)
    assert "E_EVIDENCE_NOT_VERBATIM" in codes(result)
    assert result.needs_review is True  # so it is never auto-accepted


@pytest.mark.spec("T-VER-05")
@pytest.mark.parametrize(
    "fields",
    [
        {"amounts": (AmountClaim("45000", "AED", "po_amount"),)},  # amount not in the message
        {"dates": (DateClaim("2026-10-20", "promised_payment"),)},
        {"po_numbers": ("PO-B-999",)},
        {"invoice_numbers": ("INV-004", "INV-005")},  # INV-005 is in the ledger, not the message
        {"promised_payment_date": "20 October"},
    ],
)
def test_ver_05_value_without_a_supporting_quote(
    ledger: list[LedgerRow], fields: dict[str, Any]
) -> None:
    ex = extraction(label="PO_AMOUNT_MISMATCH", evidence_quote="Please correct.", **fields)
    result = verify(ex, M01, ledger, customer="CUST-B")
    assert "UNVERIFIED_EXTRACTION" in codes(result)
    assert result.needs_review is True


@pytest.mark.spec("T-VER-05")
def test_ver_05_invoice_of_another_customer(ledger: list[LedgerRow]) -> None:
    # INV-001 belongs to CUST-A; the message came from CUST-B.
    message = "About INV-001: we dispute the quantity."
    ex = extraction(
        label="GOODS_OR_SERVICE_ISSUE", invoice_numbers=("INV-001",), evidence_quote="dispute"
    )
    result = verify(ex, message, ledger, customer="CUST-B")
    assert result.flags == (Flag("CUSTOMER_INVOICE_MISMATCH", invoice_number="INV-001"),)
    assert result.needs_review is True
    assert verify(ex, message, ledger, customer="CUST-A").flags == ()
    assert verify(ex, message, ledger, customer=None).flags == ()  # no known customer


# --- T-LLM-03: an invented invoice --------------------------------------------------------------


@pytest.mark.spec("T-LLM-03")
def test_llm_03_invented_invoice_is_flagged(ledger: list[LedgerRow]) -> None:
    invented = {
        "label": "PO_AMOUNT_MISMATCH",
        "confidence": 0.95,
        "invoice_numbers": ["INV-777"],
        "po_numbers": ["PO-B-004"],
        "amounts_mentioned": [],
        "dates_mentioned": [],
        "promised_payment_date": None,
        "evidence_quote": "Please correct.",
        "summary_en": "PO and invoice amounts differ.",
    }
    outcome = classify_message(FakeClient(classify=[invented]), M01, "en")
    result = verify(to_extraction(outcome.classification), M01, ledger, customer="CUST-B")
    assert Flag("UNMATCHED_INVOICE", invoice_number="INV-777") in result.flags
    assert result.matched == ()
    assert result.needs_review is True


# --- invoice matching ----------------------------------------------------------------------------


def _with_display(ledger: list[LedgerRow], **display: str) -> list[LedgerRow]:
    from dataclasses import replace

    return [replace(r, display_invoice_number=display.get(r.invoice_number)) for r in ledger]


def test_matching_ignores_case_and_outer_spaces(ledger: list[LedgerRow]) -> None:
    ex = extraction(invoice_numbers=(" inv-004 ",), evidence_quote="Please correct.")
    result = verify(ex, M01.replace("INV-004", "inv-004"), ledger)
    assert result.matched == ("INV-004",) and result.flags == ()


def test_a_display_number_resolves_to_its_invoice(ledger: list[LedgerRow]) -> None:
    rows = _with_display(ledger, **{"INV-004": "SI-1004"})
    ex = extraction(invoice_numbers=("SI-1004",), evidence_quote="Please correct SI-1004.")
    result = verify(ex, "Please correct SI-1004.", rows)
    assert result.matched == ("INV-004",) and result.flags == ()


def test_an_ambiguous_display_number_is_flagged_not_chosen(ledger: list[LedgerRow]) -> None:
    rows = _with_display(ledger, **{"INV-004": "SI-1", "INV-005": "SI-1"})
    ex = extraction(label="ALREADY_PAID", invoice_numbers=("SI-1",), evidence_quote="We paid SI-1.")
    result = verify(ex, "We paid SI-1.", rows)
    assert result.matched == ()
    assert result.flags == (
        Flag("UNMATCHED_INVOICE", invoice_number="SI-1", ambiguous=True, candidates=2),
    )
    assert result.flags[0].as_dict() == {
        "code": "UNMATCHED_INVOICE", "invoice_number": "SI-1", "ambiguous": True, "candidates": 2,
    }  # fmt: skip


def test_the_canonical_id_wins_over_a_display_number(ledger: list[LedgerRow]) -> None:
    rows = _with_display(ledger, **{"INV-005": "INV-004"})  # a display number that collides
    ex = extraction(invoice_numbers=("INV-004",), evidence_quote="Please correct.")
    assert verify(ex, M01, rows).matched == ("INV-004",)


# --- evidence --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quote", "flagged"),
    [
        ("purchase order PO-B-004 was issued for AED 47,000", False),
        ("purchase  order\nPO-B-004   was issued", False),  # whitespace runs are normalised
        ("  Please correct.  ", False),
        ("Purchase order PO-B-004", True),  # case is not normalised
        ("purchase order PO-B-004 was issued for AED 47000", True),  # nor number formats
        ("", True),  # no evidence at all
    ],
)
def test_evidence_quote(ledger: list[LedgerRow], quote: str, flagged: bool) -> None:
    result = verify(extraction(evidence_quote=quote), M01, ledger)
    assert ("E_EVIDENCE_NOT_VERBATIM" in codes(result)) is flagged


def test_a_quote_of_the_redacted_text_the_model_saw_is_verbatim(ledger: list[LedgerRow]) -> None:
    message = "Beta Contracting LLC will pay INV-005 on 20 October."
    sent = "[CUSTOMER] will pay INV-005 on 20 October."
    ex = extraction(
        label="PAYMENT_PROMISED",
        invoice_numbers=("INV-005",),
        dates=(DateClaim("20 October", "promised_payment"),),
        promised_payment_date="20 October",
        evidence_quote="[CUSTOMER] will pay INV-005",
    )
    assert verify(ex, message, ledger, sent=sent).flags == ()
    assert "E_EVIDENCE_NOT_VERBATIM" in codes(verify(ex, message, ledger))  # without what it saw


# --- supporting quotes for values ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "value", "ok"),
    [
        ("We were billed AED 50,000.", "50000", True),
        ("We were billed AED 50,000.00.", "50000.00", True),
        ("We were billed AED 50000.5", "50000.50", True),
        ("We were billed 1,050,000.00 dirhams.", "1050000", True),
        (f"تم إصدار الفاتورة بمبلغ {AR_50000} درهم", "50000", True),  # Arabic-Indic digits
        ("We were billed AED 50,000.", "5000", False),
        ("Invoice INV-004 is wrong.", "4", False),  # digits inside a reference are not amounts
        ("Due 2026-10-28.", "2026", False),  # nor are parts of a date
        ("We were billed AED 50,000.", "not a number", False),
    ],
)
def test_amount_supporting_quote(
    ledger: list[LedgerRow], message: str, value: str, ok: bool
) -> None:
    ex = extraction(amounts=(AmountClaim(value, "AED", "other"),), evidence_quote=message.strip())
    assert ("UNVERIFIED_EXTRACTION" not in codes(verify(ex, message, ledger))) is ok


def test_a_foreign_currency_must_appear_in_the_message(ledger: list[LedgerRow]) -> None:
    claim = (AmountClaim("50000", "USD", "other"),)
    aed_only = verify(extraction(amounts=claim, evidence_quote="Please correct."), M01, ledger)
    assert Flag("UNVERIFIED_EXTRACTION", field="amounts_mentioned") in aed_only.flags
    message = "We were billed USD 50,000."
    usd = verify(extraction(amounts=claim, evidence_quote=message), message, ledger)
    assert usd.flags == ()


@pytest.mark.parametrize(
    ("message", "fields"),
    [
        ("Payment on 20 October.", {"dates": (DateClaim("20 october", "promised_payment"),)}),
        ("Payment on 20  October.", {"promised_payment_date": "20 October"}),
        ("سندفع يوم \u0662\u0660 أكتوبر", {"promised_payment_date": "20 أكتوبر"}),
        ("See po-b-004.", {"po_numbers": ("PO-B-004",)}),
    ],
)
def test_text_values_are_found_after_normalisation(
    ledger: list[LedgerRow], message: str, fields: dict[str, Any]
) -> None:
    ex = extraction(evidence_quote=message, **fields)
    assert verify(ex, message, ledger).flags == ()


# --- the PO amount role ----------------------------------------------------------------------


def _po(amounts: tuple[AmountClaim, ...]) -> Extraction:
    return extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=("INV-004",),
        amounts=amounts,
        evidence_quote="Please correct.",
    )


def test_a_swapped_role_gives_no_difference(ledger: list[LedgerRow]) -> None:
    # 50,000 is the invoice amount in M-01; calling it the PO amount cannot be verified.
    result = verify(_po((AmountClaim("50000", "AED", "po_amount"),)), M01, ledger)
    assert result.amount_difference is None
    assert Flag("UNVERIFIED_EXTRACTION", field="po_amount") in result.flags


@pytest.mark.parametrize(
    "message",
    [
        "The purchase order PO-B-004 says AED 47,000 for invoice INV-004.",
        "PO PO-B-004: AED 47,000 only. Invoice INV-004 is higher.",
        "Our P.O. was 47,000. INV-004 is wrong.",
        f"الفاتورة INV-004 خاطئة. أمر الشراء PO-B-004 كان بمبلغ {AR_47000} درهم.",
    ],
)
def test_po_role_keywords(ledger: list[LedgerRow], message: str) -> None:
    result = verify(_po((AmountClaim("47000", "AED", "po_amount"),)), message, ledger)
    assert result.amount_difference == to_fils("3000.00")
    assert "UNVERIFIED_EXTRACTION" not in codes(result)


@pytest.mark.parametrize(
    "message",
    [
        # The PO keyword is in the previous sentence; in its own sentence 47,000 follows "invoice".
        "Our purchase order is attached. The invoice INV-004 shows 47,000.",
        # Ambiguous: the nearest mention before 47,000 is the invoice reference. Fail safe.
        "The purchase order for INV-004 says AED 47,000.",
    ],
)
def test_an_unclear_role_gives_no_difference(ledger: list[LedgerRow], message: str) -> None:
    result = verify(_po((AmountClaim("47000", "AED", "po_amount"),)), message, ledger)
    assert result.amount_difference is None
    assert Flag("UNVERIFIED_EXTRACTION", field="po_amount") in result.flags


@pytest.mark.parametrize(
    "invoices", [(), ("INV-004", "INV-005"), ("INV-9999",)], ids=["none", "two", "unknown"]
)
def test_no_difference_without_exactly_one_invoice(
    ledger: list[LedgerRow], invoices: tuple[str, ...]
) -> None:
    message = f"{M01} Also {' '.join(invoices)}."
    ex = extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=invoices,
        amounts=(AmountClaim("47000", "AED", "po_amount"),),
        evidence_quote="Please correct.",
    )
    result = verify(ex, message, ledger)
    assert result.amount_difference is None
    assert result.needs_review is True  # a PO mismatch without its difference is never accepted


def test_no_difference_for_other_labels(ledger: list[LedgerRow]) -> None:
    ex = extraction(
        label="PRICE_OR_RATE_DISPUTE",
        invoice_numbers=("INV-004",),
        amounts=(AmountClaim("47000", "AED", "po_amount"),),
        evidence_quote="Please correct.",
    )
    assert verify(ex, M01, ledger).amount_difference is None


# --- needs_review --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "needs_review"),
    [
        ({"confidence": 0.9}, False),
        ({"confidence": 0.6}, False),  # at the threshold is not below it
        ({"confidence": 0.59}, True),
        ({"label": "UNCLEAR"}, True),
        ({"label": "PAYMENT_PROMISED"}, True),  # no promised date
        ({"label": "PAYMENT_PROMISED", "promised_payment_date": "Please correct"}, False),
    ],
)
def test_needs_review(ledger: list[LedgerRow], fields: dict[str, Any], needs_review: bool) -> None:
    ex = extraction(evidence_quote="Please correct.", **fields)
    result = verify(ex, M01, ledger)
    assert result.flags == ()
    assert result.needs_review is needs_review


def test_flags_are_listed_once(ledger: list[LedgerRow]) -> None:
    ex = extraction(
        invoice_numbers=("INV-9999", "inv-9999"),
        amounts=(AmountClaim("1", "AED", "other"), AmountClaim("2", "AED", "other")),
        evidence_quote="Please correct.",
    )
    result = verify(ex, M01, ledger)
    assert len(result.flags) == len(set(result.flags))


def test_the_fallback_classification_needs_review(ledger: list[LedgerRow]) -> None:
    outcome = classify_message(FakeClient(classify=["{bad", "{bad"]), M01, "en")
    result = verify(to_extraction(outcome.classification), M01, ledger)
    assert result.needs_review is True


# --- property: a value that is not in the message is never verified -----------------------------

_WORD = st.text(alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ0123456789-", min_size=3, max_size=12)


@given(_WORD)
def test_an_absent_reference_is_never_verified(reference: str) -> None:
    message = "We will pay next week, thank you."
    ex = extraction(po_numbers=(reference,), evidence_quote="thank you")
    result = verify_extraction(
        ex,
        message=message,
        sent_text=None,
        ledger=[],
        message_customer_id=None,
        review_threshold=THRESHOLD,
    )
    assert Flag("UNVERIFIED_EXTRACTION", field="po_numbers") in result.flags


@given(st.integers(min_value=1, max_value=10**9))
def test_an_absent_amount_is_never_verified(fils: int) -> None:
    message = "We will pay next week, thank you."
    value = f"{fils // 100}.{fils % 100:02d}"
    ex = extraction(amounts=(AmountClaim(value, "AED", "other"),), evidence_quote="thank you")
    result = verify_extraction(
        ex,
        message=message,
        sent_text=None,
        ledger=[],
        message_customer_id=None,
        review_threshold=THRESHOLD,
    )
    assert result.needs_review is True


# --- edges found by mutation testing -------------------------------------------------------------


def test_compatibility_characters_are_normalised(ledger: list[LedgerRow]) -> None:
    message = "See \uff30\uff2f-B-004 for INV-004."  # full-width "PO"
    ex = extraction(po_numbers=("PO-B-004",), invoice_numbers=("INV-004",), evidence_quote="See")
    assert verify(ex, message, ledger).flags == ()


@pytest.mark.parametrize("value", ["sNaN", "NaN", "Infinity", "-Infinity"])
def test_non_finite_amounts_are_unverified_not_errors(ledger: list[LedgerRow], value: str) -> None:
    ex = extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=("INV-004",),
        amounts=(AmountClaim(value, "AED", "po_amount"),),
        evidence_quote="Please correct.",
    )
    result = verify(ex, M01, ledger)
    assert Flag("UNVERIFIED_EXTRACTION", field="amounts_mentioned") in result.flags
    assert result.amount_difference is None


@pytest.mark.parametrize(
    ("message", "difference"),
    [
        # Arabic keywords alone, without a PO or invoice reference next to the amount.
        (f"INV-004: أمر الشراء بمبلغ {AR_47000}", "3000.00"),
        (f"INV-004: PO-B-004 صحيح لكن الفاتورة بمبلغ {AR_47000}", None),
        # A sentence end separates the keyword from the amount.
        ("Purchase order PO-B-004 is attached. The total was 47,000. INV-004 is wrong.", None),
    ],
)  # fmt: skip
def test_role_keywords_in_arabic_and_sentence_ends(
    ledger: list[LedgerRow], message: str, difference: str | None
) -> None:
    result = verify(_po((AmountClaim("47000", "AED", "po_amount"),)), message, ledger)
    assert result.amount_difference == (to_fils(difference) if difference else None)


def test_canonical_ids_match_whatever_their_case(ledger: list[LedgerRow]) -> None:
    from dataclasses import replace

    rows = [replace(r, invoice_number=r.invoice_number.lower()) for r in ledger]
    ex = extraction(invoice_numbers=("INV-004",), evidence_quote="Please correct.")
    assert verify(ex, M01, rows).matched == ("inv-004",)


def test_the_difference_uses_the_invoice_amount_not_the_open_amount(
    ledger: list[LedgerRow],
) -> None:
    # INV-005: amount 30,000, of which 10,000 is paid (open 20,000).
    message = "Invoice INV-005 is wrong: purchase order PO-B-005 was for AED 27,000."
    ex = extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=("INV-005",),
        amounts=(AmountClaim("27000", "AED", "po_amount"),),
        evidence_quote="Invoice INV-005 is wrong",
    )
    assert verify(ex, message, ledger).amount_difference == to_fils("3000.00")


def test_the_same_invoice_twice_is_one_match(ledger: list[LedgerRow]) -> None:
    ex = extraction(
        label="PO_AMOUNT_MISMATCH",
        invoice_numbers=("INV-004", "inv-004"),
        amounts=(AmountClaim("47000", "AED", "po_amount"),),
        evidence_quote="Please correct.",
    )
    result = verify(ex, M01, ledger)
    assert result.matched == ("INV-004",)
    assert result.amount_difference == to_fils("3000.00")


def test_flags_serialise_to_the_stored_shape() -> None:
    assert Flag("LEDGER_STILL_OPEN", invoice_number="INV-005", amount=2000000).as_dict() == {
        "code": "LEDGER_STILL_OPEN",
        "invoice_number": "INV-005",
        "amount": 2000000,
    }
    assert Flag("UNVERIFIED_EXTRACTION", field="po_amount").as_dict() == {
        "code": "UNVERIFIED_EXTRACTION",
        "field": "po_amount",
    }


# --- regressions from the spec review ------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "fields", "field"),
    [
        ("Please check INV-0012, the PO is missing.", {"invoice_numbers": ("INV-001",)},
         "invoice_numbers"),
        ("Paid on 2026-10-28 for INV-004.", {"dates": (DateClaim("2026-10-2", "other"),)},
         "dates_mentioned"),
        ("Paid for INV-004 under PO-B-004.", {"po_numbers": ("PO-B",)}, "po_numbers"),
        ("Paid for INV-004 under PO-B-004.", {"po_numbers": ("B-004",)}, "po_numbers"),
    ],
)  # fmt: skip
def test_a_partial_reference_is_not_verified(
    ledger: list[LedgerRow], message: str, fields: dict[str, Any], field: str
) -> None:
    result = verify(extraction(evidence_quote=message, **fields), message, ledger)
    assert Flag("UNVERIFIED_EXTRACTION", field=field) in result.flags
    assert result.needs_review is True


@pytest.mark.parametrize(
    "message",
    [
        "Our PO says 47,000 but you invoiced 50,000 on INV-004.",
        "Our PO says 47,000 but you billed 50,000 on INV-004.",
        "Our PO says 47,000 but you charged 50,000 on INV-004.",
        "INV-004: PO 47,000 agreed, 50,000 now.",  # another amount between "PO" and 50,000
    ],
)
def test_a_swapped_role_is_caught_in_one_sentence(ledger: list[LedgerRow], message: str) -> None:
    result = verify(_po((AmountClaim("50000", "AED", "po_amount"),)), message, ledger)
    assert result.amount_difference is None
    assert Flag("UNVERIFIED_EXTRACTION", field="po_amount") in result.flags
    assert result.needs_review is True


def test_conflicting_po_amounts_are_never_chosen(ledger: list[LedgerRow]) -> None:
    message = "INV-004: PO 47,000 per PO-B-004, revised PO 45,000."
    claims = (AmountClaim("47000", "AED", "po_amount"), AmountClaim("45000", "AED", "po_amount"))
    result = verify(_po(claims), message, ledger)
    assert result.amount_difference is None
    assert Flag("UNVERIFIED_EXTRACTION", field="po_amount") in result.flags


def test_the_same_po_amount_twice_still_gives_the_difference(ledger: list[LedgerRow]) -> None:
    claims = (AmountClaim("47000", "AED", "po_amount"), AmountClaim("47000.00", "AED", "po_amount"))
    result = verify(_po(claims), M01, ledger)
    assert result.amount_difference == to_fils("3000.00")
    assert result.flags == ()


@pytest.mark.parametrize(("message", "ok"), [("Ref INV004 total due", False), ("AED47,000", False)])
def test_digits_glued_to_letters_are_not_amounts(
    ledger: list[LedgerRow], message: str, ok: bool
) -> None:
    value = "4" if "INV004" in message else "47000"
    ex = extraction(amounts=(AmountClaim(value, "AED", "other"),), evidence_quote=message)
    assert ("UNVERIFIED_EXTRACTION" not in codes(verify(ex, message, ledger))) is ok


@pytest.mark.parametrize("verb", ["invoiced", "billed", "charged", "invoicing"])
def test_inflected_invoice_words_end_a_po_context(ledger: list[LedgerRow], verb: str) -> None:
    message = f"Our PO is attached but you {verb} 50,000 for INV-004."  # no amount in between
    result = verify(_po((AmountClaim("50000", "AED", "po_amount"),)), message, ledger)
    assert result.amount_difference is None


def test_one_unverified_po_claim_blocks_the_difference(ledger: list[LedgerRow]) -> None:
    # 47,000 is verified as the PO amount; 50,000 is not (it is the invoice amount in M-01).
    claims = (AmountClaim("47000", "AED", "po_amount"), AmountClaim("50000", "AED", "po_amount"))
    result = verify(_po(claims), M01, ledger)
    assert result.amount_difference is None
    assert result.needs_review is True


def test_letters_glued_after_a_number_make_it_a_reference(ledger: list[LedgerRow]) -> None:
    message = "Total 47000AED for INV-004."
    ex = extraction(amounts=(AmountClaim("47000", "AED", "other"),), evidence_quote="Total")
    assert "UNVERIFIED_EXTRACTION" in codes(verify(ex, message, ledger))


def test_a_blank_reference_is_not_verified(ledger: list[LedgerRow]) -> None:
    result = verify(extraction(po_numbers=("   ",), evidence_quote="Please correct."), M01, ledger)
    assert Flag("UNVERIFIED_EXTRACTION", field="po_numbers") in result.flags


def test_a_huge_po_amount_is_never_verified_and_never_crashes(ledger: list[LedgerRow]) -> None:
    big = "9" * 29
    message = f"Invoice INV-004: purchase order PO-B-004 was issued for AED {big}."
    result = verify(_po((AmountClaim(big, "AED", "po_amount"),)), message, ledger)
    assert result.amount_difference is None
    assert result.needs_review is True


def test_a_european_number_in_the_message_is_not_an_amount(ledger: list[LedgerRow]) -> None:
    # "47.000" could be 47 or 47,000: neither may verify a claim, so it goes to review.
    message = "Invoice INV-004: purchase order PO-B-004 was issued for AED 47.000, invoice 50,000."
    for value in ("47", "47.00", "47000"):
        result = verify(_po((AmountClaim(value, "AED", "po_amount"),)), message, ledger)
        assert result.amount_difference is None, value
        assert Flag("UNVERIFIED_EXTRACTION", field="amounts_mentioned") in result.flags
