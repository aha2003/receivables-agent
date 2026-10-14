"""Agent API (SPEC §18.11, §9, §3.3): T-API-04, run, trace, approvals, kill switch, re-auth.

Synthetic golden data only. The app clock starts at 2026-10-31T09:00 Dubai.
"""

import secrets
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.repo import insert_user
from receivables.api.main import create_app
from receivables.api.security import hash_password
from receivables.seed import seed
from receivables.settings import Settings

from .conftest import SECRET, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ADMIN = "admin.user"
ADMIN_PASSWORD = secrets.token_urlsafe(18)  # generated per run; never a real password


@pytest.fixture
def seeded(engine: Engine, clock: SimulatedClock) -> Engine:
    seed(engine, FIXTURES, clock)
    with engine.begin() as c:
        insert_user(
            c,
            clock,
            username=ADMIN,
            password_hash=hash_password(ADMIN_PASSWORD),
            role="admin",
            actor="test",
        )
    return engine


@pytest.fixture
def user(client: TestClient, seeded: Engine) -> TestClient:
    assert login(client) == 303
    return client


@pytest.fixture
def admin(client: TestClient, seeded: Engine) -> TestClient:
    assert login(client, ADMIN, ADMIN_PASSWORD) == 303
    return client


def post(client: TestClient, path: str, body: Any) -> Any:
    return client.post(path, json=body, headers={"x-csrf-token": csrf(client)})


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


# --- T-API-04 ---------------------------------------------------------------------------------


def test_t_api_04_invoice_detail(user: TestClient) -> None:
    body = user.get("/api/invoices/INV-004", params={"as_of": "2026-10-31"}).json()
    assert body["po_number"] == "PO-B-004"
    assert body["payments"] == []
    assert [(e["event_id"], e["event_type"], e["label"]) for e in body["events"]] == [
        ("E-001", "DISPUTE_OPENED", "PO_AMOUNT_MISMATCH")
    ]
    assert (body["open_amount"], body["days_overdue"], body["customer_id"]) == (
        "50000.00",
        122,
        "CUST-B",
    )
    assert body["audit"], "audit rows"
    kinds = {a["after"]["file_kind"] for a in body["audit"] if a["action"] == "import.accepted"}
    assert kinds == {"invoices", "events"}  # INV-004 has events but no payments
    assert all(a["actor"] and a["entity_type"] for a in body["audit"])
    assert (body["messages"], body["classifications"], body["drafts"], body["case"]) == (
        [],
        [],
        [],
        None,
    )


def test_invoice_detail_after_a_run_shows_case_drafts_and_agent_audit(user: TestClient) -> None:
    assert post(user, "/api/agent/run", {"as_of": "2026-10-31"}).status_code == 200
    body = user.get("/api/invoices/INV-004", params={"as_of": "2026-10-31"}).json()
    assert body["case"]["state"] == "DISPUTE_OPEN"
    assert len(body["drafts"]) == 1 and body["drafts"][0]["violations"] == []
    actions = {a["action"] for a in body["audit"]}
    assert {"case.transition", "agent.step", "import.accepted"} <= actions
    steps = [a for a in body["audit"] if a["action"] == "agent.step"]
    assert {a["after"]["invoice_number"] for a in steps} == {"INV-004"}


def test_invoice_detail_payments_and_as_of(user: TestClient) -> None:
    inv5 = user.get("/api/invoices/INV-005", params={"as_of": "2026-10-31"}).json()
    assert [(p["payment_id"], p["amount"]) for p in inv5["payments"]] == [("P-003", "10000.00")]
    assert inv5["open_amount"] == "20000.00"
    earlier = user.get("/api/invoices/INV-005", params={"as_of": "2026-09-30"}).json()
    assert (earlier["payments"], earlier["open_amount"]) == ([], "30000.00")
    assert user.get("/api/invoices/INV-006", params={"as_of": "2026-09-30"}).status_code == 404
    assert user.get("/api/invoices/INV-999").status_code == 404
    assert user.get("/api/invoices/INV-004", params={"as_of": "31/10/2026"}).status_code == 422


# --- run, trace, approvals ---------------------------------------------------------------------


def test_run_trace_and_approvals(user: TestClient, seeded: Engine) -> None:
    summary = post(user, "/api/agent/run", {"as_of": "2026-10-31"}).json()
    assert (summary["planner"], summary["fallback"], summary["autonomy_level"]) == (
        "rules",
        False,
        1,
    )
    assert (summary["cases_considered"], summary["case_errors"], summary["dry_run"]) == (
        9,
        0,
        False,
    )

    trace = user.get(f"/api/agent/runs/{summary['run_id']}").json()
    assert trace["run"]["run_id"] == summary["run_id"]
    assert len(trace["cases"]) == 9
    inv4 = next(c for c in trace["cases"] if c["invoice_number"] == "INV-004")
    assert [s["tool"] for s in inv4["steps"]] == [
        "request_internal_document",
        "draft_message",
        "schedule_followup",
    ]
    assert inv4["steps"][0]["args"] == {
        "invoice_number": "INV-004",
        "doc_type": "po_copy",
        "owner_role": "finance",
    }
    assert {e["action"] for e in trace["events"]} >= {
        "agent.run_started",
        "agent.run_finished",
        "case.transition",
    }

    approvals = user.get("/api/agent/approvals").json()["items"]
    assert {(a["kind"], a["invoice_number"]) for a in approvals} == {
        ("send_email", "INV-003"),
        ("send_email", "INV-012"),
    }
    assert all(a["draft_id"] and a["run_id"] == summary["run_id"] for a in approvals)
    # Saturday 09:00 Dubai: outside business hours, and nobody is allowlisted.
    inv3 = next(a for a in approvals if a["invoice_number"] == "INV-003")
    assert inv3["reasons"] == ["POLICY_NOT_ALLOWLISTED", "POLICY_OUTSIDE_HOURS"]
    assert inv3["policy_allowed"] is False


def test_dry_run_leaves_nothing_behind(user: TestClient, seeded: Engine) -> None:
    before = rows(seeded, "SELECT COUNT(*) AS n FROM audit_log")
    summary = post(user, "/api/agent/run", {"as_of": "2026-10-31", "dry_run": True}).json()
    assert summary["dry_run"] is True and summary["cases_considered"] == 9
    for table in ("agent_runs", "agent_steps", "cases", "drafts", "tasks", "approvals"):
        assert rows(seeded, f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 0}], table  # noqa: S608
    assert rows(seeded, "SELECT COUNT(*) AS n FROM audit_log") == before
    assert user.get(f"/api/agent/runs/{summary['run_id']}").status_code == 404


def test_run_defaults_and_lower_levels(user: TestClient) -> None:
    summary = post(user, "/api/agent/run", {}).json()
    assert summary["as_of"] == "2026-10-31" and summary["autonomy_level"] == 1
    shadow = post(user, "/api/agent/run", {"autonomy_level": 0, "as_of": "2026-10-31"}).json()
    assert shadow["steps_executed"] == 0


def test_autonomy_cannot_be_raised_above_config(user: TestClient) -> None:
    response = post(user, "/api/agent/run", {"autonomy_level": 2})
    assert response.status_code == 403


def test_now_is_accepted_below_level_2(user: TestClient) -> None:
    response = post(user, "/api/agent/run", {"now": "2026-11-02T10:00:00+04:00", "dry_run": True})
    assert response.status_code == 200
    assert response.json()["as_of"] == "2026-11-02"


@pytest.mark.parametrize(
    "body",
    [
        {"as_of": "31/10/2026"},
        {"as_of": "2026-02-30"},
        {"now": "2026-11-02T10:00:00"},
        {"now": "yesterday"},
        {"autonomy_level": 3},
        {"autonomy_level": "1"},
        {"dry_run": "yes"},
        {"surprise": 1},
        [],
    ],
)
def test_run_rejects_bad_bodies(user: TestClient, body: Any) -> None:
    assert post(user, "/api/agent/run", body).status_code == 422


def test_unknown_run_is_404(user: TestClient) -> None:
    assert user.get("/api/agent/runs/RUN-nope").status_code == 404


# --- L2 configuration: now must come from the server clock ---------------------------------------


@pytest.fixture
def l2_client(engine: Engine, clock: SimulatedClock, tmp_path: Path) -> Iterator[TestClient]:
    seed(engine, FIXTURES, clock)
    config = tmp_path / "config.yaml"
    config.write_text("agent:\n  autonomy_level: 2\n", encoding="utf-8")
    settings = Settings(
        session_secret=SecretStr(SECRET), database_url=str(engine.url), config_path=str(config)
    )
    app = create_app(settings, clock=clock, engine=engine)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        assert login(c) == 303
        yield c


def test_now_is_refused_for_a_real_l2_run(l2_client: TestClient) -> None:
    body = {"now": "2026-11-02T10:00:00+04:00"}
    assert post(l2_client, "/api/agent/run", body).status_code == 422
    assert post(l2_client, "/api/agent/run", {**body, "dry_run": True}).status_code == 200
    assert post(l2_client, "/api/agent/run", {**body, "autonomy_level": 1}).status_code == 200


# --- kill switch and re-authentication ------------------------------------------------------------


def test_kill_switch_state(user: TestClient) -> None:
    assert user.get("/api/agent/kill-switch").json() == {
        "on": False,
        "runtime": False,
        "config": False,
    }


def test_a_user_cannot_set_the_kill_switch(user: TestClient) -> None:
    assert post(user, "/api/reauth", {"password": "wrong"}).status_code == 401
    response = post(user, "/api/agent/kill-switch", {"on": True})
    assert (response.status_code, response.json()["error"]) == (403, "not_admin")


def test_admin_needs_a_recent_password_reentry(
    admin: TestClient, clock: SimulatedClock, seeded: Engine
) -> None:
    refused = post(admin, "/api/agent/kill-switch", {"on": True})
    assert (refused.status_code, refused.json()["error"]) == (403, "reauth_required")

    ok = post(admin, "/api/reauth", {"password": ADMIN_PASSWORD})
    assert ok.status_code == 200 and ok.json()["valid_seconds"] == 300
    on = post(admin, "/api/agent/kill-switch", {"on": True})
    assert on.json() == {"on": True, "runtime": True, "config": False}

    summary = post(admin, "/api/agent/run", {"as_of": "2026-10-31"}).json()
    trace = admin.get(f"/api/agent/runs/{summary['run_id']}").json()
    statuses = {s["status"] for c in trace["cases"] for s in c["steps"]}
    assert statuses == {"denied"}

    clock.advance(timedelta(minutes=5, seconds=1))
    expired = post(admin, "/api/agent/kill-switch", {"on": False})
    assert (expired.status_code, expired.json()["error"]) == (403, "reauth_required")
    actions = [r["action"] for r in rows(seeded, "SELECT action FROM audit_log ORDER BY audit_id")]
    assert actions.count("kill_switch.refused") == 2
    assert actions.count("kill_switch.set") == 1
    assert actions.count("auth.reauth") == 1


def test_a_demoted_admin_is_refused_even_with_a_fresh_reauth(
    admin: TestClient, seeded: Engine
) -> None:
    assert post(admin, "/api/reauth", {"password": ADMIN_PASSWORD}).status_code == 200
    with seeded.begin() as c:
        c.execute(text("UPDATE users SET role = 'user' WHERE username = :u"), {"u": ADMIN})
    # The session is no longer valid: the stored role differs from the signed-in one.
    assert post(admin, "/api/agent/kill-switch", {"on": True}).status_code in (401, 403)
    assert rows(seeded, "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'kill_switch.set'") == [
        {"n": 0}
    ]


def test_logout_clears_the_reauthentication(
    client: TestClient, admin: TestClient, seeded: Engine
) -> None:
    assert post(admin, "/api/reauth", {"password": ADMIN_PASSWORD}).status_code == 200
    admin.post("/logout", data={"csrf_token": csrf(admin)})
    assert login(client, ADMIN, ADMIN_PASSWORD) == 303
    refused = post(client, "/api/agent/kill-switch", {"on": True})
    assert (refused.status_code, refused.json()["error"]) == (403, "reauth_required")


def test_reauth_is_rate_limited_and_audited(admin: TestClient, seeded: Engine) -> None:
    codes = [post(admin, "/api/reauth", {"password": "wrong"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    assert post(admin, "/api/reauth", {"password": ADMIN_PASSWORD}).status_code == 429
    audits = rows(seeded, "SELECT after_json FROM audit_log WHERE action = 'auth.reauth'")
    assert [a["after_json"] for a in audits] == ['{"ok": false}'] * 5


@pytest.mark.parametrize("body", [{}, {"on": "true"}, {"on": True, "extra": 1}])
def test_kill_switch_rejects_bad_bodies(admin: TestClient, body: Any) -> None:
    assert post(admin, "/api/agent/kill-switch", body).status_code == 422


@pytest.mark.parametrize("body", [{}, {"password": ""}, {"password": 5}])
def test_reauth_rejects_bad_bodies(admin: TestClient, body: Any) -> None:
    assert post(admin, "/api/reauth", body).status_code == 422


# --- session and CSRF on every route -------------------------------------------------------------

ROUTES = [
    ("POST", "/api/agent/run"),
    ("GET", "/api/agent/runs/RUN-x"),
    ("GET", "/api/agent/approvals"),
    ("GET", "/api/agent/kill-switch"),
    ("POST", "/api/agent/kill-switch"),
    ("POST", "/api/reauth"),
    ("GET", "/api/invoices/INV-004"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_needs_a_session(
    client: TestClient, seeded: Engine, method: str, path: str
) -> None:
    response = client.request(method, path, json={}, headers={"x-csrf-token": csrf(client)})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if r[0] == "POST"])
def test_every_state_change_needs_csrf(user: TestClient, method: str, path: str) -> None:
    response = user.request(method, path, json={})
    assert (response.status_code, response.json()["error"]) == (403, "csrf_failed")


# --- defensive paths ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = session


@pytest.mark.parametrize(
    "record",
    [None, "2026-10-31", {"uid": 2, "at": "2026-10-31T09:00:00+04:00"}, {"uid": 1, "at": "x"}],
)
def test_only_a_well_formed_reauth_for_the_same_user_counts(record: Any) -> None:
    from receivables.api.routes.agent import _reauthenticated_at
    from receivables.api.security import SessionUser

    request: Any = _FakeRequest({} if record is None else {"reauth": record})
    assert _reauthenticated_at(request, SessionUser(1, "admin.user", "admin")) is None


def test_a_user_removed_mid_request_is_unauthenticated(
    admin: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import receivables.api.routes.agent as agent_routes

    monkeypatch.setattr(agent_routes, "_stored_user", lambda *_: None)
    response = post(admin, "/api/agent/kill-switch", {"on": True})
    assert response.status_code == 401


def test_a_failed_reentry_cancels_an_earlier_one(admin: TestClient) -> None:
    assert post(admin, "/api/reauth", {"password": ADMIN_PASSWORD}).status_code == 200
    assert post(admin, "/api/reauth", {"password": "wrong"}).status_code == 401
    refused = post(admin, "/api/agent/kill-switch", {"on": True})
    assert (refused.status_code, refused.json()["error"]) == (403, "reauth_required")
