"""Drafts API (SPEC §9, §18.4): T-API-06 and the draft state machine (D-038).

Golden data as of 2026-10-31, fake LLM mode (template drafts) unless a test scripts a model.
Nothing can be sent: L0 and L1 never touch mail_outbox.
"""

import json
from collections.abc import Mapping
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

from .conftest import USERNAME, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    return client


def post(client: TestClient, path: str, body: Any = None) -> Any:
    return client.post(path, json=body or {}, headers={"x-csrf-token": csrf(client)})


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def new_draft(client: TestClient, *invoices: str, **extra: Any) -> dict[str, Any]:
    response = post(client, "/api/drafts", {"invoice_numbers": list(invoices), **extra})
    assert response.status_code == 200, response.text
    return response.json()


# --- T-API-06 -------------------------------------------------------------------------------------


def test_api_06_create_and_approve(user: TestClient, engine: Engine) -> None:
    draft = new_draft(user, "INV-003")
    assert (draft["status"], draft["generator"], draft["violations"]) == ("draft", "template", [])
    assert draft["tone"] == 1 and draft["customer_id"] == "CUST-A"
    approved = post(user, f"/api/drafts/{draft['draft_id']}/approve", {"approved_by": USERNAME})
    assert approved.status_code == 200
    body = approved.json()
    assert (body["status"], body["approved_by"]) == ("approved", USERNAME)
    assert body["approved_at"] == "2026-10-31T05:00:00Z"
    assert rows(engine, "SELECT * FROM mail_outbox") == []  # at L1 nothing is sent


def test_api_06_a_model_draft_with_violations_is_409(user: TestClient, engine: Engine) -> None:
    draft = new_draft(user, "INV-003")
    with engine.begin() as c:  # the loop never saves one; an older or edited row could exist
        c.execute(
            text("UPDATE drafts SET generator = 'llm', violations_json = '[\"UNKNOWN_AMOUNT\"]'"
                 " WHERE draft_id = :d"),
            {"d": draft["draft_id"]},
        )  # fmt: skip
    response = post(user, f"/api/drafts/{draft['draft_id']}/approve")
    assert response.status_code == 409
    found = rows(engine, "SELECT status FROM drafts WHERE draft_id = :d", d=draft["draft_id"])
    assert found == [{"status": "draft"}]


def test_a_template_draft_with_stored_violations_can_still_be_approved(
    user: TestClient, engine: Engine
) -> None:
    # §9: only generator = llm blocks approval (the fallback template always passes).
    draft = new_draft(user, "INV-003")
    with engine.begin() as c:
        c.execute(text("UPDATE drafts SET violations_json = '[\"STALE_DRAFT\"]'"))
    assert post(user, f"/api/drafts/{draft['draft_id']}/approve").status_code == 200


# --- the rest of the state machine ----------------------------------------------------------------


def test_reject_and_mark_sent(user: TestClient, engine: Engine, clock: SimulatedClock) -> None:
    rejected = new_draft(user, "INV-003")
    r = post(user, f"/api/drafts/{rejected['draft_id']}/reject", {"reason": "Wrong contact"})
    assert r.json()["status"] == "rejected"
    assert post(user, f"/api/drafts/{rejected['draft_id']}/approve").status_code == 409

    sent = new_draft(user, "INV-012")
    path = f"/api/drafts/{sent['draft_id']}"
    # The test clock is 2026-10-31 09:00 Dubai (05:00 UTC); approval happens at that time.
    assert post(user, f"{path}/mark-sent", {"sent_at": "2026-10-31T04:00:00Z"}).status_code == 409
    post(user, f"{path}/approve")
    ok = post(user, f"{path}/mark-sent", {"sent_at": "2026-10-31T09:00:00+04:00"})
    assert ok.status_code == 200
    assert (ok.json()["status"], ok.json()["sent_at"]) == ("sent_manually", "2026-10-31T05:00:00Z")
    assert post(user, f"{path}/reject", {"reason": "late"}).status_code == 409
    assert post(user, f"{path}/mark-sent", {"sent_at": "2026-10-31T05:00:00Z"}).status_code == 409
    assert rows(engine, "SELECT * FROM mail_outbox") == []


@pytest.mark.parametrize(
    ("sent_at", "status"),
    [
        ("2026-10-31T06:00:00", 422),  # no time zone
        ("2026-11-01T06:00:00Z", 422),  # in the future
        ("2026-10-30T06:00:00Z", 422),  # before the approval
        ("yesterday", 422),
    ],
)
def test_mark_sent_checks_the_time(user: TestClient, sent_at: str, status: int) -> None:
    draft = new_draft(user, "INV-003")
    post(user, f"/api/drafts/{draft['draft_id']}/approve")
    assert (
        post(user, f"/api/drafts/{draft['draft_id']}/mark-sent", {"sent_at": sent_at}).status_code
        == status
    )


def test_every_change_is_audited_with_the_signed_in_user(user: TestClient, engine: Engine) -> None:
    draft = new_draft(user, "INV-003")
    post(user, f"/api/drafts/{draft['draft_id']}/approve")
    post(user, f"/api/drafts/{draft['draft_id']}/mark-sent", {"sent_at": "2026-10-31T05:00:00Z"})
    audit = rows(
        engine,
        "SELECT actor, action, before_json, after_json FROM audit_log"
        " WHERE entity_id = :d ORDER BY rowid",
        d=draft["draft_id"],
    )
    assert [a["action"] for a in audit] == [
        "draft.created",
        "draft.approved",
        "draft.sent_manually",
    ]
    assert {a["actor"] for a in audit} == {USERNAME}
    assert json.loads(audit[1]["before_json"])["status"] == "draft"
    assert json.loads(audit[1]["after_json"])["status"] == "approved"
    assert draft["body"][:20] not in json.dumps(audit)  # no draft text in the audit trail


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"invoice_numbers": ["INV-003", "INV-004"]}, 422),  # two customers
        ({"invoice_numbers": ["INV-404"]}, 404),
        ({"invoice_numbers": ["INV-001"]}, 422),  # settled, not overdue
        ({"invoice_numbers": ["INV-003"], "tone": 4}, 422),
        ({"invoice_numbers": ["INV-003", "INV-003"]}, 422),
        ({"invoice_numbers": []}, 422),
        ({"invoice_numbers": ["INV-003"], "recipient": "x@evil.example"}, 422),  # no recipients
    ],
)
def test_create_refusals(user: TestClient, body: dict[str, Any], status: int) -> None:
    assert post(user, "/api/drafts", body).status_code == status


def test_the_dispute_label_and_tone_come_from_the_data(user: TestClient) -> None:
    draft = new_draft(user, "INV-004")  # 122 days overdue, active PO_AMOUNT_MISMATCH dispute
    assert draft["tone"] == 3
    assert "purchase order" in draft["body"]
    assert new_draft(user, "INV-004", tone=1)["tone"] == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [("approve", {"approved_by": "someone.else"}), ("reject", {"reason": ""}),
     ("reject", {}), ("mark-sent", {})],
)  # fmt: skip
def test_decision_bodies_are_checked(user: TestClient, path: str, body: Any) -> None:
    draft = new_draft(user, "INV-003")
    assert post(user, f"/api/drafts/{draft['draft_id']}/{path}", body).status_code == 422


def test_unknown_drafts_are_404(user: TestClient) -> None:
    assert post(user, "/api/drafts/DRF-nope/approve").status_code == 404


def test_a_model_mode_drafts_with_the_model(user: TestClient, app: FastAPI, engine: Engine) -> None:
    def write(request: Mapping[str, Any]) -> dict[str, str]:
        facts = request["facts"]
        number = facts["invoices"][0]["invoice_number"]
        return {"subject": f"Invoice {number}",
                "body": f"Dear {facts['contact_name']}, invoice {number} is overdue."}  # fmt: skip

    app.state.llm_mode = "replay"
    app.state.llm = RedactingClient(
        FakeClient(draft=write),
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=customer_names(engine),
    )
    assert new_draft(user, "INV-003")["generator"] == "llm"


def test_approving_a_draft_settles_the_agents_pending_send(
    user: TestClient, engine: Engine
) -> None:
    run = post(user, "/api/agent/run", {"as_of": "2026-10-31"})
    assert run.status_code == 200
    pending = rows(
        engine, "SELECT * FROM approvals WHERE kind = 'send_email' ORDER BY approval_id"
    )[0]
    draft_id = json.loads(pending["payload_json"])["draft_id"]
    post(user, f"/api/drafts/{draft_id}/approve")
    settled = rows(
        engine,
        "SELECT status, decided_by FROM approvals WHERE approval_id = :a",
        a=pending["approval_id"],
    )
    assert settled == [{"status": "approved", "decided_by": USERNAME}]


def test_the_routes_need_a_session_and_csrf(client: TestClient) -> None:
    assert client.post("/api/drafts", json={"invoice_numbers": ["INV-003"]}).status_code == 401
    assert login(client) == 303
    assert client.post("/api/drafts", json={"invoice_numbers": ["INV-003"]}).status_code == 403
