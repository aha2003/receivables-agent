"""Deciding the agent's approval items (SPEC §18.11, §18.4, §18.8; D-038). Synthetic data; the
test clock is Saturday 2026-10-31 09:00 Dubai, so at L2 every send is denied (business hours)
and becomes an approval item."""

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.controls import carry_kill_switch
from receivables.seed import seed

from .conftest import USERNAME, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def post(client: TestClient, path: str, body: Any = None) -> Any:
    return client.post(path, json=body or {}, headers={"x-csrf-token": csrf(client)})


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def at_level(app: FastAPI, level: int, **l2: Any) -> None:
    config = app.state.config
    limits = dataclasses.replace(config.agent.l2, allowlisted_customers=("CUST-A",), **l2)
    agent = dataclasses.replace(config.agent, autonomy_level=level, l2=limits)
    app.state.config = dataclasses.replace(config, agent=agent)


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    return client


def run_and_items(client: TestClient, engine: Engine) -> dict[str, dict[str, Any]]:
    assert post(client, "/api/agent/run", {"as_of": "2026-10-31"}).status_code == 200
    items = rows(engine, "SELECT a.*, s.invoice_number FROM approvals a"
                 " JOIN agent_steps s ON s.step_id = a.step_id")  # fmt: skip
    return {i["invoice_number"]: i for i in items}


def draft_of(engine: Engine, item: dict[str, Any]) -> dict[str, Any]:
    draft_id = json.loads(item["payload_json"])["draft_id"]
    return rows(engine, "SELECT * FROM drafts WHERE draft_id = :d", d=draft_id)[0]


# --- L1: approving marks the draft approved; nothing is sent -----------------------------------


def test_l1_approve_marks_the_draft_approved_and_sends_nothing(
    user: TestClient, engine: Engine
) -> None:
    items = run_and_items(user, engine)
    item = items["INV-003"]
    response = post(user, f"/api/agent/approvals/{item['approval_id']}/approve")
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["mail"]) == ("approved", None)
    draft = draft_of(engine, item)
    assert (draft["status"], draft["approved_by"]) == ("approved", USERNAME)
    assert rows(engine, "SELECT * FROM mail_outbox") == []
    settled = rows(engine, "SELECT status, decided_by FROM approvals WHERE approval_id = :a",
                   a=item["approval_id"])  # fmt: skip
    assert settled == [{"status": "approved", "decided_by": USERNAME}]


def test_reject_records_the_decision_only(user: TestClient, engine: Engine) -> None:
    item = run_and_items(user, engine)["INV-012"]
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/reject").json()["status"] == (
        "rejected"
    )
    assert draft_of(engine, item)["status"] == "draft"  # the draft itself is untouched
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").status_code == 409


# --- L2: a deliberate human override through the sandbox -----------------------------------------


def test_l2_approve_overrides_the_denial_through_the_sandbox(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    assert json.loads(item["payload_json"])["reasons"] == ["POLICY_OUTSIDE_HOURS"]
    assert rows(engine, "SELECT * FROM mail_outbox") == []  # the policy denied it
    response = post(user, f"/api/agent/approvals/{item['approval_id']}/approve")
    assert response.status_code == 200
    assert response.json()["mail"]["status"] == "delivered_sandbox"
    (mail,) = rows(engine, "SELECT to_address, status, gateway FROM mail_outbox")
    assert mail == {"to_address": "ap@alpha.example", "status": "delivered_sandbox",
                    "gateway": "sandbox"}  # fmt: skip
    audit = rows(engine, "SELECT actor, before_json, after_json FROM audit_log"
                 " WHERE action = 'approval.approved'")  # fmt: skip
    assert audit[0]["actor"] == USERNAME
    assert json.loads(audit[0]["before_json"])["reasons"] == ["POLICY_OUTSIDE_HOURS"]
    assert json.loads(audit[0]["after_json"])["override"] is True
    assert json.loads(audit[0]["after_json"])["mail"]["reasons_now"] == ["POLICY_OUTSIDE_HOURS"]
    assert draft_of(engine, item)["status"] == "approved"
    # §18.2: the sent message moves the case; the draft change has its own audit row.
    assert rows(engine, "SELECT state FROM cases WHERE invoice_number = 'INV-003'") == [
        {"state": "AWAITING_REPLY"}
    ]
    moved = rows(engine, "SELECT actor, before_json, after_json FROM audit_log"
                 " WHERE action = 'case.transition' AND entity_id = 'INV-003'"
                 " ORDER BY audit_id DESC LIMIT 1")  # fmt: skip
    assert moved[0]["actor"] == USERNAME
    assert json.loads(moved[0]["before_json"]) == {"state": "ASSESSED"}
    assert json.loads(moved[0]["after_json"])["trigger"] == "MessageSent"
    approved = rows(engine, "SELECT actor FROM audit_log WHERE action = 'draft.approved'"
                    " AND entity_id = :d", d=draft_of(engine, item)["draft_id"])  # fmt: skip
    assert approved == [{"actor": USERNAME}]


def test_a_delivered_draft_is_neither_rejected_nor_marked_sent(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    post(user, f"/api/agent/approvals/{item['approval_id']}/approve")
    draft_id = draft_of(engine, item)["draft_id"]
    assert post(user, f"/api/drafts/{draft_id}/reject", {"reason": "x"}).status_code == 409
    sent_at = {"sent_at": "2026-10-31T05:00:00Z"}
    assert post(user, f"/api/drafts/{draft_id}/mark-sent", sent_at).status_code == 409
    page = user.get("/invoices/INV-003").text
    assert "Delivered to the sandbox outbox" in page
    assert ">Mark sent</button>" not in page and ">Reject</button>" not in page
    detail = user.get("/api/invoices/INV-003").json()
    assert [d["delivered_sandbox"] for d in detail["drafts"]] == [True]


def test_an_item_from_an_l1_run_is_only_marked_approved_even_at_l2(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    at_level(app, 2)
    body = {"as_of": "2026-10-31", "autonomy_level": 1}
    assert post(user, "/api/agent/run", body).status_code == 200
    (item,) = rows(engine, "SELECT a.* FROM approvals a JOIN agent_steps s USING (step_id)"
                   " WHERE s.invoice_number = 'INV-003'")  # fmt: skip
    response = post(user, f"/api/agent/approvals/{item['approval_id']}/approve")
    assert (response.status_code, response.json()["mail"]) == (200, None)
    assert rows(engine, "SELECT * FROM mail_outbox") == []
    assert draft_of(engine, item)["status"] == "approved"


def test_an_l2_item_is_only_marked_approved_once_l2_is_off(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    at_level(app, 1)
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").json()["mail"] is None
    assert rows(engine, "SELECT * FROM mail_outbox") == []


def test_l2_no_override_once_the_draft_no_longer_matches_today(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    with engine.begin() as c:  # the due date moved: the invoice is no longer overdue
        c.execute(text("UPDATE invoices SET due_date = '2026-12-31'"
                       " WHERE invoice_number = 'INV-003'"))  # fmt: skip
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").status_code == 409
    assert rows(engine, "SELECT * FROM mail_outbox") == []
    assert draft_of(engine, item)["status"] == "draft"


@pytest.mark.parametrize("reason", ["POLICY_KILL_SWITCH", "POLICY_RECIPIENT_NOT_ALLOWED"])
def test_l2_some_denials_can_never_be_overridden(
    user: TestClient, app: FastAPI, engine: Engine, reason: str
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    with engine.begin() as c:
        c.execute(
            text("UPDATE approvals SET payload_json = json_set(payload_json, '$.reasons',"
                 " json_array(:r)) WHERE approval_id = :a"),
            {"r": reason, "a": item["approval_id"]},
        )  # fmt: skip
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").status_code == 409
    assert rows(engine, "SELECT * FROM mail_outbox") == []
    assert draft_of(engine, item)["status"] == "draft"


def test_l2_nothing_is_overridden_while_the_kill_switch_is_on(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    at_level(app, 2, kill_switch=True)  # switched on after the run
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").status_code == 409
    assert rows(engine, "SELECT * FROM mail_outbox") == []


def test_l2_nothing_is_overridden_after_the_runtime_kill_switch(
    user: TestClient, app: FastAPI, engine: Engine, clock: SimulatedClock
) -> None:
    at_level(app, 2)
    item = run_and_items(user, engine)["INV-003"]
    with engine.begin() as c:
        carry_kill_switch(c, clock, actor=USERNAME, reason="test")  # on at runtime
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").status_code == 409
    assert rows(engine, "SELECT * FROM mail_outbox") == []


# --- refusals -------------------------------------------------------------------------------------


def test_a_model_draft_with_violations_is_never_approved(user: TestClient, engine: Engine) -> None:
    item = run_and_items(user, engine)["INV-003"]
    with engine.begin() as c:
        c.execute(
            text("UPDATE drafts SET generator = 'llm', violations_json = :v WHERE draft_id = :d"),
            {"v": '["UNKNOWN_AMOUNT"]', "d": json.loads(item["payload_json"])["draft_id"]},
        )  # fmt: skip
    assert post(user, f"/api/agent/approvals/{item['approval_id']}/approve").status_code == 409


def test_an_allocation_waits_for_s3_11_but_can_be_rejected(
    user: TestClient, engine: Engine
) -> None:
    item = run_and_items(user, engine)["INV-003"]
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO approvals (approval_id, step_id, kind, payload_json) VALUES"
                 " ('A-ALLOC', :s, 'allocation', :p)"),
            {"s": item["step_id"], "p": '{"payment_id": "P-X"}'},
        )  # fmt: skip
    assert post(user, "/api/agent/approvals/A-ALLOC/approve").status_code == 409
    assert post(user, "/api/agent/approvals/A-ALLOC/reject").status_code == 200


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/agent/approvals/NOPE/approve", {}, 404),
        ("/api/agent/approvals/NOPE/maybe", {}, 404),
        ("/api/agent/approvals/{id}/approve", {"decided_by": "someone.else"}, 422),
        ("/api/agent/approvals/{id}/approve", {"extra": 1}, 422),
    ],
)
def test_decision_refusals(
    user: TestClient, engine: Engine, path: str, body: Any, status: int
) -> None:
    item = run_and_items(user, engine)["INV-003"]
    assert post(user, path.replace("{id}", item["approval_id"]), body).status_code == status


def test_decisions_need_a_session_and_csrf(client: TestClient) -> None:
    assert client.post("/api/agent/approvals/X/approve").status_code == 401
    assert login(client) == 303
    assert client.post("/api/agent/approvals/X/approve").status_code == 403
