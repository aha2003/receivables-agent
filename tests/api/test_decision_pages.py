"""The draft panel and the Approvals page (SPEC §10, §18.4): T-UI-03 and the form flows, and
T-AUD-01 (D-038). Golden data, fake LLM mode, autonomy L1 unless a test says otherwise."""

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.seed import seed

from .conftest import USERNAME, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    return client


def form(client: TestClient, path: str, **fields: str) -> Any:
    return client.post(path, data={"csrf_token": csrf(client), **fields})


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def drafts_section(page: str) -> str:
    match = re.search(r'<section id="drafts">(.*?)</section>', page, re.S)
    assert match
    return match.group(1)


# --- T-UI-03 ------------------------------------------------------------------------------------


@pytest.mark.spec("T-UI-03")
def test_ui_03_l1_draft_panel_has_the_notice_and_no_send_button(user: TestClient) -> None:
    form(user, "/drafts/new", invoice_number="INV-003")
    panel = drafts_section(user.get("/invoices/INV-003").text)
    assert "Review before sending. Nothing is sent automatically." in panel
    buttons = re.findall(r"<button[^>]*>([^<]*)</button>", panel)
    assert "Approve" in buttons and "Reject" in buttons
    assert not [b for b in buttons if re.search(r"\bsend\b", b, re.I)]  # no send button
    assert "/send" not in panel


# --- the draft flow through the forms -----------------------------------------------------------


def test_create_approve_and_mark_sent_from_the_page(user: TestClient, engine: Engine) -> None:
    created = form(user, "/drafts/new", invoice_number="INV-003")
    assert created.status_code == 303
    assert created.headers["location"] == "/invoices/INV-003#drafts"
    (draft,) = rows(engine, "SELECT draft_id FROM drafts")
    assert form(user, f"/drafts/{draft['draft_id']}/approve").status_code == 303
    panel = drafts_section(user.get("/invoices/INV-003").text)
    assert f"Approved by {USERNAME}" in panel and 'name="sent_at"' in panel
    sent = form(user, f"/drafts/{draft['draft_id']}/mark-sent", sent_at="2026-10-31T09:00")
    assert sent.status_code == 303
    stored = rows(engine, "SELECT status, sent_at FROM drafts")
    assert stored == [{"status": "sent_manually", "sent_at": "2026-10-31T05:00:00Z"}]  # Dubai
    assert rows(engine, "SELECT * FROM mail_outbox") == []
    panel = drafts_section(user.get("/invoices/INV-003").text)
    assert "Marked sent at 2026-10-31T05:00:00Z" in panel
    assert ">Approve</button>" not in panel and ">Mark sent</button>" not in panel


def test_the_default_sent_time_is_accepted_right_after_approving(
    user: TestClient, engine: Engine, clock: SimulatedClock
) -> None:
    # Found in the manual T-UI-03 check: a minute-precision default fell before approved_at.
    clock.advance(timedelta(seconds=37))
    form(user, "/drafts/new", invoice_number="INV-003")
    (draft,) = rows(engine, "SELECT draft_id FROM drafts")
    form(user, f"/drafts/{draft['draft_id']}/approve")
    panel = drafts_section(user.get("/invoices/INV-003").text)
    match = re.search(r'name="sent_at" value="([^"]+)"', panel)
    assert match and match.group(1) == "2026-10-31T09:00:37"
    assert 'step="1"' in panel
    assert (
        form(user, f"/drafts/{draft['draft_id']}/mark-sent", sent_at=match.group(1)).status_code
        == 303
    )
    assert rows(engine, "SELECT sent_at FROM drafts") == [{"sent_at": "2026-10-31T05:00:37Z"}]


def test_reject_from_the_page_and_a_refusal_shows_why(user: TestClient, engine: Engine) -> None:
    form(user, "/drafts/new", invoice_number="INV-003")
    (draft,) = rows(engine, "SELECT draft_id FROM drafts")
    assert form(user, f"/drafts/{draft['draft_id']}/reject", reason="Wrong tone").status_code == 303
    refused = form(user, f"/drafts/{draft['draft_id']}/approve")
    assert refused.status_code == 409
    assert "The draft is rejected, not draft." in refused.text
    assert form(user, f"/drafts/{draft['draft_id']}/mark-sent", sent_at="").status_code == 422


def test_the_copy_box_holds_the_draft_text_escaped(user: TestClient, engine: Engine) -> None:
    form(user, "/drafts/new", invoice_number="INV-004")
    panel = drafts_section(user.get("/invoices/INV-004").text)
    assert "<textarea readonly" in panel and "Payment reminder: invoice INV-004" in panel


def test_a_model_draft_with_violations_shows_no_approve_button(
    user: TestClient, engine: Engine
) -> None:
    form(user, "/drafts/new", invoice_number="INV-003")
    with engine.begin() as c:
        c.execute(text("UPDATE drafts SET generator = 'llm', violations_json = :v"),
                  {"v": '["UNKNOWN_AMOUNT"]'})  # fmt: skip
    panel = drafts_section(user.get("/invoices/INV-003").text)
    assert "A model draft with violations cannot be approved." in panel
    assert ">Approve</button>" not in panel


def test_the_approvals_page_decides_items(user: TestClient, engine: Engine) -> None:
    form(user, "/agent/run", as_of="2026-10-31")
    page = user.get("/approvals").text
    ids = re.findall(r'action="/approvals/([^/]+)/approve"', page)
    assert len(ids) == 2  # INV-003 and INV-012
    assert form(user, f"/approvals/{ids[0]}/approve").status_code == 303
    assert form(user, f"/approvals/{ids[1]}/reject").status_code == 303
    assert "No pending approvals." in user.get("/approvals").text
    assert form(user, f"/approvals/{ids[0]}/maybe").status_code == 404
    assert form(user, f"/approvals/{ids[0]}/approve").status_code == 409


def test_the_forms_need_csrf(user: TestClient) -> None:
    assert user.post("/drafts/new", data={"invoice_number": "INV-003"}).status_code == 403


# --- T-AUD-01 -----------------------------------------------------------------------------------


@pytest.mark.spec("T-AUD-01")
def test_aud_01_import_accept_and_approval_each_leave_an_audit_row(
    user: TestClient, app: Any, engine: Engine
) -> None:
    # One import (correspondence), one classification accept, one draft approval.
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
    assert user.post("/api/correspondence/M-02/classify", headers=token).status_code == 200
    assert user.post("/api/classifications/M-02/accept", headers=token).status_code == 200
    draft = user.post("/api/drafts", json={"invoice_numbers": ["INV-003"]}, headers=token).json()
    assert user.post(f"/api/drafts/{draft['draft_id']}/approve", headers=token).status_code == 200

    for action, entity in (
        ("import.accepted", None),
        ("classification.accepted", "M-02"),
        ("draft.approved", draft["draft_id"]),
    ):
        found = rows(engine, "SELECT actor, entity_type, entity_id, before_json, after_json"
                     " FROM audit_log WHERE action = :a", a=action)  # fmt: skip
        assert found, action
        row = found[-1]
        assert row["actor"] == USERNAME and row["entity_type"]
        assert entity is None or row["entity_id"] == entity
        assert json.loads(row["before_json"]) is not None and json.loads(row["after_json"])
