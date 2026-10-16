"""The Unallocated cash page (SPEC §10, §6.7; D-040). Golden data plus P-005 (T-UNA-01)."""

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.seed import seed

from .conftest import csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
HEADER = b"payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    rows = b"P-005,,CUST-C,2026-10-28,12000.00,payment\nP-009,,CUST-C,2026-11-05,900.00,payment\n"
    upload = client.post(
        "/api/imports/payments",
        files={"file": ("p.csv", HEADER + rows, "text/csv")},
        headers={"x-csrf-token": csrf(client)},
    )
    assert upload.status_code == 200
    return client


def form(client: TestClient, path: str, **fields: str) -> Any:
    return client.post(path, data={"csrf_token": csrf(client), **fields})


def options(page: str, payment_id: str) -> dict[str, bool]:
    """Invoice -> offered (not disabled) in the payment's Allocate form."""
    row = re.search(rf'<tr id="pay-{payment_id}">(.*?)</tr>', page, re.S)
    assert row
    return {
        m.group(1): "disabled" not in m.group(2)
        for m in re.finditer(r'<option value="([^"]+)"([^>]*)>', row.group(1))
    }


def test_the_page_lists_payments_by_customer_with_their_choices(user: TestClient) -> None:
    page = user.get("/unallocated").text
    assert "Gamma Facilities Services LLC" in page
    assert "Unallocated cash AED 12,000.00; overdue exposure" in page  # P-009 is not received
    # INV-007 is paid; INV-009 (5,000.00) and INV-012 (9,000.00) are smaller than the payment.
    assert options(page, "P-005") == {"INV-008": True, "INV-009": False, "INV-012": False}
    assert "(payment is larger)" in page
    assert "Dated after today: it can be allocated once received." in page
    assert 'action="/unallocated/P-009/allocate"' not in page
    assert '<a href="/unallocated">Unallocated cash</a>' in user.get("/").text  # nav and dashboard


def test_allocating_from_the_page(user: TestClient, engine: Engine) -> None:
    response = form(user, "/unallocated/P-005/allocate", invoice_number="INV-008")
    assert response.status_code == 303
    assert response.headers["location"] == "/unallocated#pay-P-005"
    with engine.connect() as c:
        stored = c.execute(text("SELECT invoice_number FROM payments WHERE payment_id = 'P-005'"))
        assert stored.scalar_one() == "INV-008"
    page = user.get("/unallocated").text
    assert 'id="pay-P-005"' not in page
    assert 'id="pay-P-009"' in page  # still waiting


def test_a_refusal_shows_why(user: TestClient, engine: Engine) -> None:
    refused = form(user, "/unallocated/P-005/allocate", invoice_number="INV-012")
    assert refused.status_code == 409
    assert "larger than the invoice&#39;s open amount" in refused.text
    assert 'href="/unallocated#pay-P-005"' in refused.text
    assert form(user, "/unallocated/P-404/allocate", invoice_number="INV-008").status_code == 404


def test_no_unallocated_payments(client: TestClient, engine: Engine, clock: SimulatedClock) -> None:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    assert "No unallocated payments." in client.get("/unallocated").text


def test_the_form_needs_csrf(user: TestClient) -> None:
    response = user.post("/unallocated/P-005/allocate", data={"invoice_number": "INV-008"})
    assert response.status_code == 403
