"""The Inbox page (SPEC §10; T-UI-04 for classification flags; D-034). Synthetic data only."""

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.seed import seed

from .conftest import csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def answer(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "label": "NO_ACTION_NEEDED", "confidence": 0.9, "invoice_numbers": [], "po_numbers": [],
        "amounts_mentioned": [], "dates_mentioned": [], "promised_payment_date": None,
        "evidence_quote": "", "summary_en": "Synthetic summary.",
    }  # fmt: skip
    return base | fields


@pytest.fixture
def user(app: FastAPI, client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    response = client.post(
        "/api/imports/correspondence",
        files={"file": ("c.csv", (FIXTURES / "correspondence.csv").read_bytes(), "text/csv")},
        headers={"x-csrf-token": csrf(client)},
    )
    assert response.status_code == 200
    return client


def use_llm(app: FastAPI, engine: Engine, result: dict[str, Any]) -> None:
    app.state.llm = RedactingClient(
        FakeClient(classify=lambda _: result),
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=customer_names(engine),
    )


def form(client: TestClient, path: str, **fields: str) -> Any:
    return client.post(path, data={"csrf_token": csrf(client), **fields})


def card(page: str, message_id: str) -> str:
    match = re.search(rf'<article class="card" id="msg-{message_id}">(.*?)</article>', page, re.S)
    assert match, f"no card for {message_id}"
    return match.group(1)


def test_the_inbox_lists_unclassified_messages(user: TestClient) -> None:
    page = user.get("/inbox").text
    assert page.count('<article class="card"') == 15
    m11 = card(page, "M-11")
    assert 'dir="auto"' in m11 and "INV-009" in m11
    assert 'action="/inbox/M-11/classify"' in m11
    assert 'href="/inbox"' in page  # in the navigation


def test_ui_04_flags_in_plain_language(user: TestClient, app: FastAPI, engine: Engine) -> None:
    use_llm(
        app,
        engine,
        answer(
            label="ALREADY_PAID",
            invoice_numbers=["INV-005", "INV-777"],
            evidence_quote="We paid INV-005 in full",
        ),
    )
    assert form(user, "/inbox/M-06/classify").status_code == 303
    m06 = card(user.get("/inbox").text, "M-06")
    assert "Customer says paid but the ledger shows AED 20,000.00 open." in m06
    assert "The message mentions invoice INV-777, which is not in the ledger." in m06
    assert "Needs review before accepting." in m06
    assert "LEDGER_STILL_OPEN" not in m06 and "UNMATCHED_INVOICE" not in m06


def test_accept_from_the_page_creates_the_event(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    use_llm(
        app,
        engine,
        answer(
            label="PAYMENT_PROMISED",
            invoice_numbers=["INV-008"],
            promised_payment_date="20 October",
            evidence_quote="Payment for INV-008 will be released on 20 October.",
        ),
    )
    form(user, "/inbox/M-03/classify")
    m03 = card(user.get("/inbox").text, "M-03")
    assert 'value="2026-10-20"' in m03  # the resolved promise, ready to accept
    assert form(user, "/inbox/M-03/accept", promised_payment_date="2026-10-20").status_code == 303
    with engine.connect() as c:
        event = c.execute(text("SELECT * FROM events WHERE event_id = 'CLS-M-03'")).one()
    assert (event.event_type, event.promise_date) == ("PROMISE_TO_PAY", "2026-10-20")
    assert 'id="msg-M-03"' not in user.get("/inbox").text  # done


def test_reject_from_the_page(user: TestClient, app: FastAPI, engine: Engine) -> None:
    use_llm(app, engine, answer())
    form(user, "/inbox/M-13/classify")
    assert form(user, "/inbox/M-13/reject").status_code == 303
    assert 'id="msg-M-13"' not in user.get("/inbox").text


def test_a_refusal_is_shown_on_the_page(user: TestClient, app: FastAPI, engine: Engine) -> None:
    use_llm(app, engine, answer(label="MISSING_PO"))
    form(user, "/inbox/M-13/classify")  # no invoice to attach the dispute to
    response = form(user, "/inbox/M-13/accept")
    assert response.status_code == 409
    assert "No single invoice to attach the event to" in response.text
    bad = form(user, "/inbox/M-13/accept", promised_payment_date="31/10/2026")
    assert bad.status_code == 422


def test_review_items_come_before_settled_ones(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    use_llm(app, engine, answer(label="NO_ACTION_NEEDED", evidence_quote="Thanks, noted."))
    form(user, "/inbox/M-13/classify")  # verified, no review
    use_llm(app, engine, answer(label="UNCLEAR"))
    form(user, "/inbox/M-14/classify")  # needs review
    page = user.get("/inbox").text
    ids = re.findall(r'id="msg-(M-\d+)"', page)
    assert ids.index("M-14") < ids.index("M-13")
    assert ids[0] == "M-06"  # unclassified ones first, in sent_at order (M-06 was sent first)


def test_message_text_is_escaped(user: TestClient, engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, direction, sent_at, body) VALUES"
                " ('M-99', 'inbound', '2026-10-29T06:00:00Z', '<script>alert(1)</script>')"
            )
        )
    page = user.get("/inbox").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_the_forms_need_csrf(user: TestClient) -> None:
    response = user.post("/inbox/M-01/classify", data={})
    assert response.status_code == 403


def test_an_empty_inbox_points_to_upload(client: TestClient) -> None:
    assert login(client) == 303
    assert "Upload <code>correspondence.csv</code>" in client.get("/inbox").text


def test_the_amount_difference_is_formatted(user: TestClient, app: FastAPI, engine: Engine) -> None:
    use_llm(
        app,
        engine,
        answer(
            label="PO_AMOUNT_MISMATCH",
            invoice_numbers=["INV-004"],
            amounts_mentioned=[{"value": "47000", "currency": "AED", "role": "po_amount"}],
            evidence_quote="Please correct.",
        ),
    )
    form(user, "/inbox/M-01/classify")
    assert "AED 3,000.00 (invoice minus PO" in card(user.get("/inbox").text, "M-01")  # §10 format
