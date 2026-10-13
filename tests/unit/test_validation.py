"""Fixed-schema CSV validation (SPEC §4.1-§4.6): T-IMP-01..03, T-UNA-02 plus rule coverage."""

from datetime import date
from typing import Any

import pytest

from receivables.core.kpis import unallocated_cash
from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.money import to_fils
from receivables.core.scoring import EventRecord
from receivables.core.validation import (
    DISPUTE_LABELS,
    CustomerRow,
    References,
    derive_tin,
    validate_csv,
)

TODAY = date(2026, 10, 31)
GOLDEN_CUSTOMERS = frozenset({"CUST-A", "CUST-B", "CUST-C"})


def _codes(result: Any) -> list[tuple[int, str]]:
    return [(e.line, e.code) for e in result.errors]


def _golden_refs(fixture_text: Any) -> References:
    invoices = validate_csv(
        "invoices", fixture_text("invoices.csv"), References(customer_ids=GOLDEN_CUSTOMERS), TODAY
    )
    records = [r for r in invoices.records if isinstance(r, InvoiceRecord)]
    return References(
        customer_ids=GOLDEN_CUSTOMERS,
        invoice_customers={r.invoice_number: r.customer_id for r in records},
        invoice_amounts={r.invoice_number: r.amount for r in records},
    )


class TestImp01:
    def test_bad_invoices_errors_exactly(self, fixture_text: Any) -> None:
        result = validate_csv(
            "invoices",
            fixture_text("bad_invoices.csv"),
            References(customer_ids=frozenset({"C001"})),
            TODAY,
        )
        assert _codes(result) == [
            (3, "E_DUP_KEY"),
            (4, "E_BAD_DATE"),
            (5, "E_DUE_BEFORE_ISSUE"),
            (6, "E_BAD_AMOUNT"),
            (7, "E_FK_MISSING"),
            (8, "E_CURRENCY"),
            (9, "E_REQUIRED_EMPTY"),
            (10, "E_BAD_AMOUNT"),
        ]
        assert result.rows_total == 9
        assert {e.line for e in result.errors}.isdisjoint({2})
        fields = {e.line: e.field for e in result.errors}
        assert fields == {
            3: "invoice_number",
            4: "invoice_date",
            5: "due_date",
            6: "amount",
            7: "customer_id",
            8: "currency",
            9: "invoice_number",
            10: "amount",
        }


class TestImp02:
    CSV = (
        "customer_id,customer_name,trn\n"
        "C-1,Fictional One LLC,100123456700003\n"
        "C-2,Fictional Two LLC,12345\n"
        "C-1,Fictional Three LLC,\n"
    )

    def test_trn_and_duplicate(self) -> None:
        result = validate_csv("customers", self.CSV, References(), TODAY)
        assert [(e.line, e.code, e.field) for e in result.errors] == [
            (3, "E_BAD_TRN", "trn"),
            (4, "E_DUP_KEY", "customer_id"),
        ]

    def test_tin(self) -> None:
        assert derive_tin("100123456700003") == "1001234567"
        assert derive_tin("12345") is None
        assert derive_tin(None) is None
        rows = validate_csv(
            "customers",
            "customer_id,customer_name,trn\nC-1,X,100123456700003\n",
            References(),
            TODAY,
        )
        assert rows.records == (
            CustomerRow(
                customer_id="C-1",
                customer_name="X",
                trn="100123456700003",
                tin="1001234567",
                trade_license_no=None,
                payment_terms_days=None,
                contact_email=None,
            ),
        )


class TestImp03:
    def test_golden_customers_and_invoices_have_no_errors(self, fixture_text: Any) -> None:
        customers = validate_csv("customers", fixture_text("customers.csv"), References(), TODAY)
        assert customers.errors == ()
        assert customers.rows_total == 3
        invoices = validate_csv(
            "invoices",
            fixture_text("invoices.csv"),
            References(customer_ids=GOLDEN_CUSTOMERS),
            TODAY,
        )
        assert invoices.errors == ()
        assert invoices.rows_total == 14
        assert invoices.warnings == ()

    def test_golden_payments_and_events_have_no_errors(self, fixture_text: Any) -> None:
        refs = _golden_refs(fixture_text)
        payments = validate_csv("payments", fixture_text("payments.csv"), refs, TODAY)
        events = validate_csv("events", fixture_text("events.csv"), refs, TODAY)
        assert payments.errors == () and events.errors == ()
        assert len(payments.records) == 4 and len(events.records) == 2
        assert isinstance(events.records[0], EventRecord)
        assert events.records[0].label == "PO_AMOUNT_MISMATCH"


class TestUna02:
    CSV = (
        "payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"
        "P-006,,,2026-10-01,500.00,payment\n"
        "P-007,,CUST-A,2026-10-01,500.00,write_off\n"
        "P-008,INV-001,CUST-B,2026-10-01,500.00,payment\n"
        "P-009,,CUST-C,2026-11-05,900.00,payment\n"
    )

    def test_errors(self, fixture_text: Any) -> None:
        result = validate_csv("payments", self.CSV, _golden_refs(fixture_text), TODAY)
        assert [(e.line, e.code, e.field) for e in result.errors] == [
            (2, "E_REQUIRED_EMPTY", "customer_id"),
            (3, "E_REQUIRED_EMPTY", "invoice_number"),
            (4, "E_CUSTOMER_MISMATCH", "customer_id"),
        ]

    def test_p009_valid_but_ignored_at_as_of(self, fixture_text: Any) -> None:
        only_p009 = (
            "payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"
            + self.CSV.splitlines()[4]
            + "\n"
        )
        result = validate_csv("payments", only_p009, _golden_refs(fixture_text), TODAY)
        assert result.errors == ()
        payment = result.records[0]
        assert isinstance(payment, PaymentRecord)
        assert payment.invoice_number is None and payment.customer_id == "CUST-C"
        assert unallocated_cash([payment], TODAY).total == 0
        assert unallocated_cash([payment], date(2026, 11, 5)).total == to_fils("900.00")
        assert ("W_FUTURE_DATE", "payment_date") in [(w.code, w.field) for w in result.warnings]


# --- general rules (§4) ---------------------------------------------------------------------

INV_HEADER = "invoice_number,customer_id,invoice_date,due_date,amount,currency,po_number\n"
CUST_REFS = References(customer_ids=frozenset({"C001"}))


def _invoice_line(amount: str = "100.00", **kw: str) -> str:
    row = {
        "invoice_number": "INV-1",
        "customer_id": "C001",
        "invoice_date": "2026-09-01",
        "due_date": "2026-10-01",
        "amount": amount,
        "currency": "AED",
        "po_number": "",
    }
    row.update(kw)
    return ",".join(row.values()) + "\n"


def test_spaces_are_stripped_and_blank_currency_is_aed() -> None:
    result = validate_csv(
        "invoices",
        INV_HEADER + " INV-1 , C001 ,2026-09-01, 2026-10-01 , 100.50 ,,\n",
        CUST_REFS,
        TODAY,
    )
    assert result.errors == ()
    inv = result.records[0]
    assert isinstance(inv, InvoiceRecord)
    assert (inv.invoice_number, inv.customer_id, inv.amount, inv.po_number) == (
        "INV-1",
        "C001",
        10050,
        None,
    )


@pytest.mark.parametrize(
    "amount",
    [
        "0",
        "0.00",
        "-5.00",
        "500.123",
        '"1,000.00"',  # quoted cell with a thousands separator
        "1e3",
        "AED 5",
        "",
        "\u0661\u0660\u0660",
        "5.",
        ".5",
        "+5",
        "1 000",
    ],
)
def test_bad_amounts(amount: str) -> None:
    result = validate_csv("invoices", INV_HEADER + _invoice_line(amount=amount), CUST_REFS, TODAY)
    expected = "E_REQUIRED_EMPTY" if amount == "" else "E_BAD_AMOUNT"
    assert [(e.code, e.field) for e in result.errors] == [(expected, "amount")]


@pytest.mark.parametrize("amount", ["5", "5.5", "5.50", "1000000.00"])
def test_good_amounts(amount: str) -> None:
    assert (
        validate_csv("invoices", INV_HEADER + _invoice_line(amount=amount), CUST_REFS, TODAY).errors
        == ()
    )


@pytest.mark.parametrize(
    "bad",
    [
        "2026-9-1",
        "01/09/2026",
        "2026-02-30",
        "2026-09-01T00:00",
        "\u0662\u0660\u0662\u0666-\u0660\u0669-\u0660\u0661",
    ],
)
def test_bad_dates(bad: str) -> None:
    result = validate_csv(
        "invoices", INV_HEADER + _invoice_line(invoice_date=bad), CUST_REFS, TODAY
    )
    assert [(e.code, e.field) for e in result.errors] == [("E_BAD_DATE", "invoice_date")]


def test_missing_column_is_line_1_and_names_are_case_sensitive() -> None:
    header = "invoice_number,Customer_ID,invoice_date,due_date,amount\n"
    result = validate_csv(
        "invoices", header + "INV-1,C001,2026-09-01,2026-10-01,5.00\n", CUST_REFS, TODAY
    )
    assert [(e.line, e.code, e.field) for e in result.errors] == [
        (1, "E_MISSING_COLUMN", "customer_id")
    ]
    assert result.records == ()


def test_extra_column_warns() -> None:
    header = INV_HEADER.strip() + ",notes\n"
    result = validate_csv(
        "invoices", header + _invoice_line().strip() + ",hello\n", CUST_REFS, TODAY
    )
    assert result.errors == ()
    assert [(w.line, w.code, w.field) for w in result.warnings] == [(1, "W_EXTRA_COLUMN", "notes")]


def test_empty_file_is_missing_header() -> None:
    result = validate_csv("customers", "", References(), TODAY)
    assert [(e.line, e.code) for e in result.errors] == [
        (1, "E_MISSING_COLUMN"),
        (1, "E_MISSING_COLUMN"),
    ]
    assert result.rows_total == 0


def test_only_one_error_per_row_first_check_wins() -> None:
    # Duplicate key, bad date and bad amount on one row: duplicate is reported (order of §4.6).
    text = INV_HEADER + _invoice_line() + _invoice_line(invoice_date="2026-13-01", amount="-1")
    result = validate_csv("invoices", text, CUST_REFS, TODAY)
    assert [(e.line, e.code) for e in result.errors] == [(3, "E_DUP_KEY")]


def test_customer_fk_uses_references() -> None:
    result = validate_csv(
        "invoices", INV_HEADER + _invoice_line(customer_id="C999"), CUST_REFS, TODAY
    )
    assert [(e.code, e.field) for e in result.errors] == [("E_FK_MISSING", "customer_id")]


def test_quoted_multiline_field_reports_starting_line() -> None:
    text = (
        "customer_id,customer_name,trn\n"
        'C-1,"Fictional\nTwo Line LLC",100123456700003\n'
        "C-2,Fictional Two,12345\n"
    )
    result = validate_csv("customers", text, References(), TODAY)
    assert [(e.line, e.code) for e in result.errors] == [(4, "E_BAD_TRN")]


# --- customers --------------------------------------------------------------------------------

CUST_HEADER = "customer_id,customer_name,trn,trade_license_no,payment_terms_days,contact_email\n"


@pytest.mark.parametrize(
    ("terms", "code"),
    [
        ("abc", "E_BAD_NUMBER_FORMAT"),
        ("30.5", "E_BAD_NUMBER_FORMAT"),
        ("366", "E_BAD_ENUM"),
        ("-1", "E_BAD_NUMBER_FORMAT"),
    ],
)
def test_bad_payment_terms(terms: str, code: str) -> None:
    result = validate_csv("customers", CUST_HEADER + f"C-1,X,,,{terms},\n", References(), TODAY)
    assert [(e.code, e.field) for e in result.errors] == [(code, "payment_terms_days")]


def test_customer_id_longer_than_64_rejected() -> None:
    result = validate_csv("customers", CUST_HEADER + f"{'C' * 65},X,,,,\n", References(), TODAY)
    assert [(e.code, e.field) for e in result.errors] == [("E_BAD_ENUM", "customer_id")]


def test_bad_email_warns_only() -> None:
    result = validate_csv(
        "customers", CUST_HEADER + "C-1,X,,,30,not-an-email\n", References(), TODAY
    )
    assert result.errors == ()
    assert [(w.line, w.code, w.field) for w in result.warnings] == [
        (2, "W_BAD_EMAIL", "contact_email")
    ]


# --- payments ---------------------------------------------------------------------------------

PAY_HEADER = "payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"
PAY_REFS = References(
    customer_ids=frozenset({"CUST-A", "CUST-B"}),
    invoice_customers={"INV-1": "CUST-A"},
    invoice_amounts={"INV-1": to_fils("1000.00")},
)


def test_payment_type_defaults_and_enum() -> None:
    ok = validate_csv("payments", PAY_HEADER + "P-1,INV-1,,2026-10-01,10.00,\n", PAY_REFS, TODAY)
    assert ok.errors == ()
    assert isinstance(ok.records[0], PaymentRecord) and ok.records[0].payment_type == "payment"
    bad = validate_csv(
        "payments", PAY_HEADER + "P-1,INV-1,,2026-10-01,10.00,refund\n", PAY_REFS, TODAY
    )
    assert [(e.code, e.field) for e in bad.errors] == [("E_BAD_ENUM", "payment_type")]


def test_payment_fk_checks() -> None:
    text = (
        PAY_HEADER + "P-1,INV-404,,2026-10-01,10.00,payment\nP-2,,CUST-Z,2026-10-01,10.00,payment\n"
    )
    result = validate_csv("payments", text, PAY_REFS, TODAY)
    assert [(e.line, e.code, e.field) for e in result.errors] == [
        (2, "E_FK_MISSING", "invoice_number"),
        (3, "E_FK_MISSING", "customer_id"),
    ]


def test_allocated_payment_record_gets_customer_from_invoice() -> None:
    result = validate_csv(
        "payments", PAY_HEADER + "P-1,INV-1,CUST-A,2026-10-01,10.00,credit_note\n", PAY_REFS, TODAY
    )
    assert result.errors == ()
    payment = result.records[0]
    assert isinstance(payment, PaymentRecord)
    assert payment.payment_type == "credit_note"


def test_overpaid_and_unallocated_warnings() -> None:
    text = (
        PAY_HEADER
        + "P-1,INV-1,,2026-10-01,600.00,payment\n"
        + "P-2,INV-1,,2026-10-02,500.00,payment\n"
        + "P-3,,CUST-B,2026-10-03,20.00,payment\n"
        + "P-4,,CUST-B,2026-10-04,30.50,credit_note\n"
    )
    result = validate_csv("payments", text, PAY_REFS, TODAY)
    assert result.errors == ()
    warnings = [(w.line, w.code, w.field, w.detail) for w in result.warnings]
    assert (3, "W_OVERPAID", "amount", "INV-1") in warnings
    assert (0, "W_UNALLOCATED", "invoice_number", "count=2 total=50.50") in warnings


def test_overpaid_counts_existing_payments_but_not_replaced_ones() -> None:
    refs = References(
        customer_ids=PAY_REFS.customer_ids,
        invoice_customers=PAY_REFS.invoice_customers,
        invoice_amounts=PAY_REFS.invoice_amounts,
        existing_payments={
            "P-OLD": ("INV-1", to_fils("900.00")),
            "P-1": ("INV-1", to_fils("999.00")),
        },
    )
    text = PAY_HEADER + "P-1,INV-1,,2026-10-01,50.00,payment\n"
    assert [w.code for w in validate_csv("payments", text, refs, TODAY).warnings] == []
    text2 = PAY_HEADER + "P-1,INV-1,,2026-10-01,150.00,payment\n"
    assert [w.code for w in validate_csv("payments", text2, refs, TODAY).warnings] == ["W_OVERPAID"]


# --- events -----------------------------------------------------------------------------------

EV_HEADER = "event_id,invoice_number,event_type,event_date,label,promise_date\n"
EV_REFS = References(invoice_customers={"INV-1": "CUST-A"})


@pytest.mark.parametrize(
    ("row", "code", "field"),
    [
        ("E-1,INV-1,DISPUTE_OPENED,2026-10-01,,", "E_REQUIRED_EMPTY", "label"),
        ("E-1,INV-1,PROMISE_TO_PAY,2026-10-01,,", "E_REQUIRED_EMPTY", "promise_date"),
        ("E-1,INV-1,DISPUTE_OPENED,2026-10-01,NOT_A_LABEL,", "E_BAD_ENUM", "label"),
        ("E-1,INV-1,PAID,2026-10-01,,", "E_BAD_ENUM", "event_type"),
        ("E-1,INV-1,PROMISE_TO_PAY,2026-10-05,,2026-10-04", "E_BAD_DATE", "promise_date"),
        ("E-1,INV-1,PROMISE_TO_PAY,2026-10-05,,2026-13-04", "E_BAD_DATE", "promise_date"),
        ("E-1,INV-404,DISPUTE_RESOLVED,2026-10-05,,", "E_FK_MISSING", "invoice_number"),
    ],
)
def test_event_rules(row: str, code: str, field: str) -> None:
    result = validate_csv("events", EV_HEADER + row + "\n", EV_REFS, TODAY)
    assert [(e.code, e.field) for e in result.errors] == [(code, field)]


def test_valid_events() -> None:
    text = (
        EV_HEADER
        + "E-1,INV-1,PROMISE_TO_PAY,2026-10-05,,2026-10-05\n"
        + "E-2,INV-1,DISPUTE_RESOLVED,2026-10-06,,\n"
    )
    result = validate_csv("events", text, EV_REFS, TODAY)
    assert result.errors == ()
    assert isinstance(result.records[0], EventRecord)
    assert result.records[0].promise_date == date(2026, 10, 5)


def test_dispute_label_taxonomy_has_13_labels() -> None:
    assert len(DISPUTE_LABELS) == 13
    assert "PO_AMOUNT_MISMATCH" in DISPUTE_LABELS and "UNCLEAR" in DISPUTE_LABELS


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        validate_csv("correspondence", "message_id\n", References(), TODAY)


def test_blank_and_empty_rows_are_skipped() -> None:
    text = INV_HEADER + "\n" + ",,,,,,\n" + _invoice_line() + "   \n"
    result = validate_csv("invoices", text, CUST_REFS, TODAY)
    assert result.errors == ()
    assert result.rows_total == 1
