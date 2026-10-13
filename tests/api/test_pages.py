"""Server-rendered pages (SPEC §10): dashboard, queue, invoice, upload.

Automates the checkable parts of T-UI-02 and T-UI-05 (both also checked manually)."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import csrf, login
from .test_dashboard_queue import FIXTURES, upload

GOLDEN_ORDER = [
    "INV-004",
    "INV-011",
    "INV-005",
    "INV-008",
    "INV-003",
    "INV-014",
    "INV-009",
    "INV-012",
    "INV-013",
]


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    assert login(client) == 303
    for kind in ("customers", "invoices", "payments", "events"):
        assert upload(client, kind, (FIXTURES / f"{kind}.csv").read_bytes()).status_code == 200
    return client


def _queue_order(html: str) -> list[str]:
    return re.findall(r'<a href="/invoices/(INV-\d{3})\?as_of=', html)


def test_dashboard_shows_the_golden_figures(seeded: TestClient) -> None:
    page = seeded.get("/", params={"as_of": "2026-10-31"}).text
    for text in (
        "AED 193,000.00",
        "AED 147,000.00",
        "76.2% of open",
        "149.7 days",
        "AED 0.00",
        "AED 46,000.00",
        "AED 55,000.00",
        "Beta Contracting LLC",
        "69.39%",
        "AED 57,000.00",
        "SYNTHETIC DEMO DATA",
        "Heuristic score. Not a probability.",
    ):
        assert text in page, text
    assert 'value="2026-10-31"' in page
    assert re.search(
        r'<abbr title="Estimated DSO = open receivables divided by credit sales[^"]+">', page
    )
    assert "14 invoices and 4 payments dated on or before 2026-10-31" in page


def test_dashboard_as_of_and_empty_states(client: TestClient) -> None:
    assert login(client) == 303
    page = client.get("/", params={"as_of": "2026-10-31"}).text
    assert "N/A" in page and "No overdue invoices as of 2026-10-31." in page
    assert "No payments yet" in page


def test_queue_page_order_matches_the_api(seeded: TestClient) -> None:
    page = seeded.get("/queue", params={"as_of": "2026-10-31"}).text
    assert _queue_order(page) == GOLDEN_ORDER
    api = seeded.get("/api/queue", params={"as_of": "2026-10-31"}).json()["items"]
    assert _queue_order(page) == [i["invoice_number"] for i in api]
    assert (
        page.count("Heuristic score. Not a probability.") >= 3
    )  # intro, risk column, priority column
    assert "40833.3" in page and "Resolve dispute: PO_AMOUNT_MISMATCH" in page


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"band": "High"}, ["INV-004", "INV-014"]),
        ({"band": "Low"}, ["INV-012"]),
        ({"customer": "CUST-A"}, ["INV-003", "INV-013"]),
        ({"band": "Medium", "customer": "CUST-C"}, ["INV-008", "INV-009"]),
        ({"band": "Nonsense"}, GOLDEN_ORDER),
    ],
)
def test_queue_filters(seeded: TestClient, params: dict[str, str], expected: list[str]) -> None:
    page = seeded.get("/queue", params={"as_of": "2026-10-31", **params}).text
    assert _queue_order(page) == expected


def test_queue_flags_are_shown_in_plain_language(seeded: TestClient) -> None:
    extra = (
        FIXTURES / "payments.csv"
    ).read_bytes() + b"P-005,,CUST-C,2026-10-28,12000.00,payment\n"
    upload(seeded, "payments", extra)
    page = seeded.get("/queue", params={"as_of": "2026-10-31"}).text
    assert page.count("This customer has AED 12,000.00 of unallocated cash.") == 3


def test_queue_rows_click_through_to_the_invoice(seeded: TestClient) -> None:
    page = seeded.get("/queue", params={"as_of": "2026-10-31"}).text
    href = re.search(r'href="(/invoices/INV-004\?as_of=2026-10-31)"', page)
    assert href
    detail = seeded.get(href.group(1)).text
    for text in (
        "Invoice INV-004",
        "Beta Contracting LLC",
        "AED 50,000.00",
        "2026-07-01",
        "122 (90+)",
        "PO-B-004",
        "81.7 (High)",
        "40833.3",
        "Resolve dispute: PO_AMOUNT_MISMATCH",
        "2026-09-10: dispute opened (PO_AMOUNT_MISMATCH)",
    ):
        assert text in detail, text


def test_invoice_page_timeline_and_time_travel(seeded: TestClient) -> None:
    now = seeded.get("/invoices/INV-005", params={"as_of": "2026-10-31"}).text
    assert "2026-10-05: payment AED 10,000.00 (P-003)" in now and "AED 20,000.00" in now
    before = seeded.get("/invoices/INV-005", params={"as_of": "2026-09-30"}).text
    assert "P-003" not in before and "AED 30,000.00" in before
    closed = seeded.get("/invoices/INV-001", params={"as_of": "2026-10-31"}).text
    assert "Not open" in closed and "Settled</dt><dd>2026-08-10" in closed


def test_unknown_invoice_is_404(seeded: TestClient) -> None:
    response = seeded.get("/invoices/INV-999")
    assert response.status_code == 404
    assert "No such invoice" in response.text
    assert seeded.get("/invoices/INV-006", params={"as_of": "2026-09-30"}).status_code == 404


def test_html_injection_is_escaped(client: TestClient) -> None:
    assert login(client) == 303
    hostile = b"customer_id,customer_name\nCUST-X,<script>alert(1)</script> Fictional LLC\n"
    assert upload(client, "customers", hostile).status_code == 200
    invoices = (
        b"invoice_number,customer_id,invoice_date,due_date,amount\n"
        b"INV-901,CUST-X,2026-09-01,2026-09-30,100.00\n"
    )
    assert upload(client, "invoices", invoices).status_code == 200
    page = client.get("/queue", params={"as_of": "2026-10-31"}).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Fictional LLC" in page


def test_upload_page_and_report(client: TestClient) -> None:
    assert login(client) == 303
    page = client.get("/upload").text
    for kind in ("customers", "invoices", "payments", "events"):
        assert f'action="/upload/{kind}"' in page
    assert page.count('name="csrf_token"') >= 5  # four upload forms plus sign-out
    ok = client.post(
        "/upload/customers",
        files={"file": ("customers.csv", (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
        data={"csrf_token": csrf(client)},
    )
    assert (
        ok.status_code == 200
        and "customers: accepted" in ok.text
        and "3 rows, 3 imported" in ok.text
    )
    bad = client.post(
        "/upload/invoices",
        files={"file": ("bad.csv", (FIXTURES / "bad_invoices.csv").read_bytes(), "text/csv")},
        data={"csrf_token": csrf(client)},
    )
    assert bad.status_code == 422
    assert "invoices: rejected" in bad.text and "Nothing was written." in bad.text
    assert "<td>3</td><td>E_DUP_KEY</td><td>invoice_number</td>" in bad.text
    again = client.post(
        "/upload/customers",
        files={"file": ("customers.csv", (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
        data={"csrf_token": csrf(client)},
    )
    assert "customers: duplicate" in again.text and "nothing changed" in again.text


def test_upload_page_refusals(client: TestClient) -> None:
    assert login(client) == 303
    response = client.post(
        "/upload/correspondence",
        files={"file": ("m.csv", b"message_id\n", "text/csv")},
        data={"csrf_token": csrf(client)},
    )
    assert response.status_code == 501
    assert "arrives in S3.06" in response.text


def test_every_page_has_banner_nav_and_as_of(seeded: TestClient) -> None:
    for path in ("/", "/queue", "/invoices/INV-004", "/upload"):
        page = seeded.get(path).text
        assert "SYNTHETIC DEMO DATA" in page, path
        assert 'href="/queue"' in page and 'action="/logout"' in page, path
    for path in ("/", "/queue", "/invoices/INV-004"):
        assert "2026-10-31" in seeded.get(path).text, path


def test_pages_are_unreachable_without_a_session(client: TestClient) -> None:
    for path in ("/", "/queue", "/invoices/INV-004", "/upload"):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 303 and response.headers["location"] == "/login", path


def test_templates_dir_has_no_leftovers() -> None:
    templates = Path(__file__).resolve().parents[2] / "src" / "receivables" / "web" / "templates"
    assert not (templates / "home.html").exists()
