"""The Receivables Health Audit (SPEC §11): T-RPT-01 and the report's sections (D-041).

Golden synthetic data as of 2026-10-31 unless a test says otherwise.
"""

import re
from decimal import Decimal
from fractions import Fraction
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.core.scoring import round_half_up
from receivables.seed import seed

from .conftest import csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
AS_OF = "2026-10-31"


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    return client


def report(client: TestClient, as_of: str | None = AS_OF) -> Any:
    return client.get("/api/report", params={"as_of": as_of} if as_of else None)


def section(page: str, name: str) -> str:
    match = re.search(rf'<section id="{name}">(.*?)</section>', page, re.S)
    assert match, name
    return match.group(1)


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


# --- T-RPT-01 -----------------------------------------------------------------------------------


def test_rpt_01_the_report_holds_the_golden_figures_and_labels(user: TestClient) -> None:
    response = report(user)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "AED 193,000.00" in page
    headline = text_of(section(page, "headline"))
    assert (
        "14 invoices dated 2026-05-01 to 2026-10-10; 4 payments; 0 unallocated payments" in headline
    )
    assert "Heuristic score. Not a probability." in section(page, "risk")
    assert "AED 147,000.00" in page
    customers = text_of(section(page, "customers"))
    assert "Beta Contracting LLC (CUST-B) AED 102,000.00 69.4% 69.4%" in customers  # share
    assert "estimated DSO (90-day sales proxy)" in page
    assert "Heuristic score. Not a probability." in page
    assert "7. Definitions and limits" in section(page, "definitions")


# --- the §11 sections ---------------------------------------------------------------------------


def test_the_sections_come_in_the_spec_order_and_say_synthetic(user: TestClient) -> None:
    page = report(user).text
    ids = re.findall(r'<section id="([a-z]+)">', page)
    assert ids == ["headline", "aging", "customers", "risk", "disputes", "queue", "definitions"]
    assert page.count("SYNTHETIC DEMO DATA") >= 3  # title, top banner and footer
    assert "As of <strong>2026-10-31</strong>" in page


def test_the_numbers_match_the_dashboard(user: TestClient) -> None:
    dash = user.get("/api/dashboard", params={"as_of": AS_OF}).json()
    page = report(user).text
    headline = text_of(section(page, "headline"))
    assert f"({dash['overdue_pct']}% of the total)" in headline
    assert f"{dash['estimated_dso']} days" in headline
    aging = text_of(section(page, "aging"))
    amounts = {name: Decimal(v) for name, v in dash["buckets"].items()}
    total = sum(amounts.values())
    assert total == Decimal("193000.00")
    for name, amount in amounts.items():
        pct = round_half_up(Fraction(100 * amount) / Fraction(total), 1)
        assert f"{name} AED {amount:,} {pct}%" in aging
    risk = text_of(section(page, "risk"))
    for band, amount in dash["exposure_by_band"].items():
        assert f"{band} AED {Decimal(amount):,}" in risk


def test_top_customers_have_cumulative_shares_and_unallocated_cash(user: TestClient) -> None:
    rows = re.findall(r"<tr><td>([^<]+)</td>(.*?)</tr>", section(report(user).text, "customers"))
    cells = [(name, re.findall(r">([^<]+)</td>", rest)) for name, rest in rows]
    names = [n for n, _ in cells]
    assert names[0].endswith("(CUST-B)")
    cumulative = [c[2] for _, c in cells]
    assert cumulative[-1] == "100.0%"
    assert [float(c.rstrip("%")) for c in cumulative] == sorted(
        float(c.rstrip("%")) for c in cumulative
    )
    assert all(c[3].startswith("AED ") for _, c in cells)


def test_the_queue_is_the_top_20_in_queue_order(user: TestClient) -> None:
    items = user.get("/api/queue", params={"as_of": AS_OF, "limit": 20}).json()["items"]
    queue = section(report(user).text, "queue")
    order = re.findall(r'<td class="num">(\d+)</td><td class="id">(INV-\d+)</td>', queue)
    assert [n for _, n in order] == [i["invoice_number"] for i in items]
    assert "Heuristic score. Not a probability." in queue


def test_disputes_and_observations(user: TestClient) -> None:
    page = report(user).text
    disputes = text_of(section(page, "disputes"))
    assert "1 overdue invoice with an active dispute, AED 50,000.00 open." in disputes
    assert "PO_AMOUNT_MISMATCH 1 AED 50,000.00" in disputes
    assert "Customer messages" not in disputes  # no correspondence supplied
    observations = text_of(section(page, "headline"))
    assert "Top 3 customers account for 100.0% of overdue exposure." in observations
    assert "1 overdue invoice worth AED 50,000.00 has an active dispute." in observations


def test_definitions_state_every_formula_the_caveat_and_what_was_not_measured(
    user: TestClient,
) -> None:
    definitions = text_of(section(report(user).text, "definitions"))
    for term in ("Aging buckets", "Risk score", "Priority value", "Estimated DSO", "Open amount"):
        assert term in definitions
    assert "raw = 0.4 x min(days overdue, 90) / 90 + 0.25 x history + 0.15 x size" in definitions
    assert ("otherwise the portfolio's late rate; 0.5 if no invoice settled in that window"
            in definitions)  # fmt: skip
    assert "divided by the number of open invoices less 1 (0 if only one is open)" in definitions
    assert "unallocated payments reduce no invoice" in definitions
    assert "Compare it with the finance team's own DSO" in definitions
    assert "What was not measured" in definitions
    assert "it is not expected cash loss" in definitions


def test_messages_count_only_accepted_classifications(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    # Option 1a: model output nobody confirmed is never counted as a finding.
    upload = user.post(
        "/api/imports/correspondence",
        files={"file": ("c.csv", (FIXTURES / "correspondence.csv").read_bytes(), "text/csv")},
        headers={"x-csrf-token": csrf(user)},
    )
    assert upload.status_code == 200
    app.state.llm = RedactingClient(
        FakeClient(classify=lambda _: {
            "label": "MISSING_DELIVERY_NOTE", "confidence": 0.9, "invoice_numbers": ["INV-009"],
            "po_numbers": [], "amounts_mentioned": [], "dates_mentioned": [],
            "promised_payment_date": None, "evidence_quote": "delivery note",
            "summary_en": "No delivery note."}),
        redact_before_send=True, pseudonymize_customer_names=True,
        customer_names=customer_names(engine),
    )  # fmt: skip
    token = {"x-csrf-token": csrf(user)}
    for message_id in ("M-02", "M-11", "M-12"):
        assert (
            user.post(f"/api/correspondence/{message_id}/classify", headers=token).status_code
            == 200
        )
    assert user.post("/api/classifications/M-02/accept", headers=token).status_code == 200
    assert user.post("/api/classifications/M-12/reject", headers=token).status_code == 200
    messages = text_of(section(report(user).text, "disputes"))
    assert (
        "15 inbound messages on or before 2026-10-31: as of that date, 1 classified and "
        "accepted by a person, 1 rejected by a person, " in messages
    )
    assert "1 awaiting review, 12 not classified." in messages  # 15 in all
    assert "MISSING_DELIVERY_NOTE 1" in messages
    # The day before they were classified and decided (31 Oct, the test clock), M-02 (sent
    # 13 Oct) counts as sent but not classified: statuses are as of the report's date.
    earlier = text_of(section(report(user, "2026-10-30").text, "disputes"))
    assert ("15 inbound messages on or before 2026-10-30: as of that date, 0 classified and "
            "accepted by a person, 0 rejected by a person, 0 awaiting review, 15 not "
            "classified.") in earlier  # fmt: skip
    assert "Label (accepted)" not in earlier  # no accepted labels yet


def test_import_warnings_and_data_warnings(user: TestClient) -> None:
    extra = (
        FIXTURES / "payments.csv"
    ).read_bytes() + b"P-005,,CUST-C,2026-10-28,12000.00,payment\n"
    user.post(
        "/api/imports/payments",
        files={"file": ("p.csv", extra, "text/csv")},
        headers={"x-csrf-token": csrf(user)},
    )
    definitions = text_of(section(report(user).text, "definitions"))
    assert "Data as of 2026-10-31: W_UNALLOCATED (count=1 total=12000.00)" in definitions
    assert "Latest payments import" in definitions and "W_UNALLOCATED" in definitions
    page = report(user).text
    observations = text_of(section(page, "headline"))
    assert "AED 12,000.00 of unallocated cash is on account" in observations
    customers = text_of(section(page, "customers"))
    assert "(CUST-C) AED 26,000.00 17.7% 87.1% AED 12,000.00" in customers
    assert (
        "Unallocated cash by customer Customer Unallocated cash Overdue exposure Gamma "
        "Facilities Services LLC (CUST-C) AED 12,000.00 AED 26,000.00" in customers
    )
    queue = section(page, "queue")
    flagged = re.findall(
        r'<td class="id">(INV-\d+)</td>(?:(?!</tr>).)*'
        r"This customer has AED 12,000.00 of unallocated cash",
        queue,
        re.S,
    )
    assert sorted(flagged) == ["INV-008", "INV-009", "INV-012"]  # T-UNA-01's flagged items


def test_an_empty_ledger_says_n_a(client: TestClient) -> None:
    assert login(client) == 303
    page = report(client).text
    assert "AED 0.00 (N/A of the total)" in text_of(section(page, "headline"))
    assert "Nothing is overdue." in page
    assert "N/A (no sales in the window)" in page


def test_as_of_defaults_to_today_and_is_checked(user: TestClient) -> None:
    assert "As of <strong>2026-10-31</strong>" in report(user, None).text  # the test clock
    assert report(user, "31/10/2026").status_code == 422


def test_the_report_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/report").status_code == 401


def test_the_dashboard_links_to_the_report(user: TestClient) -> None:
    assert 'href="/api/report?as_of=2026-10-31"' in user.get("/", params={"as_of": AS_OF}).text


def test_names_are_escaped(user: TestClient) -> None:
    # A synthetic customer whose name holds markup appears as text, never as markup.
    head = (FIXTURES / "customers.csv").read_text(encoding="utf-8").splitlines()[0]
    fields = head.split(",")
    row = {f: "" for f in fields} | {
        "customer_id": "CUST-X", "customer_name": "<script>alert(1)</script> Trading",
    }  # fmt: skip
    if "payment_terms_days" in row:
        row["payment_terms_days"] = "30"
    body = (head + "\n" + ",".join(row[f] for f in fields) + "\n").encode()
    token = {"x-csrf-token": csrf(user)}
    assert user.post("/api/imports/customers", files={"file": ("c.csv", body, "text/csv")},
                     headers=token).status_code == 200  # fmt: skip
    invoice = (b"invoice_number,customer_id,invoice_date,due_date,amount,currency,po_number\n"
               b"INV-950,CUST-X,2026-08-01,2026-08-31,100.00,AED,\n")  # fmt: skip
    assert user.post("/api/imports/invoices", files={"file": ("i.csv", invoice, "text/csv")},
                     headers=token).status_code == 200  # fmt: skip
    page = report(user).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Trading" in page


def test_the_golden_report_has_no_stale_no_payments_warning(user: TestClient) -> None:
    # The seed imports invoices before payments; that import's "no payments yet" is stale.
    page = report(user).text
    assert "W_NO_PAYMENTS" not in page
    assert "None." in text_of(section(page, "definitions"))


def test_one_overdue_customer_reads_in_the_singular(user: TestClient) -> None:
    dash = user.get("/api/dashboard", params={"as_of": "2026-07-15"}).json()
    assert len(dash["top_overdue_customers"]) == 1
    headline = text_of(section(report(user, "2026-07-15").text, "headline"))
    assert "Top 1 customer accounts for 100.0% of overdue exposure." in headline
