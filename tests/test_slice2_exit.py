"""Slice 2 exit (SPEC §16) and gate D1 (§12.1) without any LLM.

From an empty database: the real CLI resets the synthetic company and creates the users; the
web app runs a tick from the "Run agent" form. The agent inspects the queue, plans with the
rule planner, calls tools, is denied by policy, reaches waiting states and shows a complete
trace; no database is edited by hand. The clock is 2026-10-31T09:00 Dubai (a Saturday).
"""

import html
import io
import json
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, SimulatedClock
from receivables.api.main import create_app
from receivables.cli import main
from receivables.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SECRET = "test-only-session-secret-0123456789abcdef"  # synthetic, tests only
USER, ADMIN = "demo.user", "admin.user"
PASSWORD = secrets.token_urlsafe(18)  # generated per run; never a real password


@dataclass
class Demo:
    client: TestClient
    engine: Engine

    def csrf(self) -> str:
        me = self.client.get("/api/me")
        if me.status_code == 200:
            return me.json()["csrf_token"]
        token = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get("/login").text)
        assert token
        return token.group(1)

    def login(self, username: str) -> None:
        data = {"username": username, "password": PASSWORD, "csrf_token": self.csrf()}
        assert self.client.post("/login", data=data).status_code == 303

    def run_agent(self) -> str:
        data = {"csrf_token": self.csrf(), "as_of": "2026-10-31"}
        response = self.client.post("/agent/run", data=data)
        assert response.status_code == 303
        return response.headers["location"].removeprefix("/runs/")

    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(text(sql), params)]

    def page(self, path: str) -> str:
        response = self.client.get(path)
        assert response.status_code == 200
        return html.unescape(response.text)


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Demo]:
    db = tmp_path / "demo.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    assert main(["reset", "--as-of", "2026-10-31"]) == 0
    for name, role in ((USER, "user"), (ADMIN, "admin")):
        monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
        assert main(["create-user", name, "--role", role, "--password-stdin"]) == 0
    settings = Settings(session_secret=SecretStr(SECRET))  # DATABASE_URL from the environment
    clock = SimulatedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))
    app = create_app(settings, clock=clock)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        demo = Demo(client, app.state.engine)
        demo.login(USER)
        yield demo
    app.state.engine.dispose()


def test_d1_the_agent_runs_a_multi_step_tick_without_an_llm(demo: Demo) -> None:
    run_id = demo.run_agent()
    run = demo.rows("SELECT * FROM agent_runs WHERE run_id = :r", r=run_id)[0]
    assert (run["planner"], run["model"], run["fallback"]) == ("rules", None, 0)  # no LLM
    assert run["cases_considered"] == 9  # it inspected the queue
    steps = demo.rows("SELECT * FROM agent_steps WHERE run_id = :r ORDER BY seq", r=run_id)
    per_case: dict[str, list[str]] = {}
    for s in steps:
        per_case.setdefault(s["invoice_number"], []).append(s["tool"])
    assert all(len(tools) >= 2 for tools in per_case.values())  # multi-step plans
    tools = {s["tool"] for s in steps}
    assert tools == {
        "request_internal_document",
        "draft_message",
        "schedule_followup",
        "escalate",
        "send_email",
    }
    assert {s["status"] for s in steps if s["tool"] != "send_email"} == {"executed"}


def test_d1_sends_are_denied_by_policy_with_reason_codes(demo: Demo) -> None:
    run_id = demo.run_agent()
    sends = demo.rows(
        "SELECT * FROM agent_steps WHERE run_id = :r AND tool = 'send_email'", r=run_id
    )
    assert {s["invoice_number"] for s in sends} == {"INV-003", "INV-012"}
    for s in sends:
        assert (s["status"], s["policy_allowed"]) == ("converted_to_approval", 0)
        reasons = json.loads(s["policy_reasons_json"])
        assert {"POLICY_NOT_ALLOWLISTED", "POLICY_OUTSIDE_HOURS"} <= set(reasons)
    assert demo.rows("SELECT COUNT(*) AS n FROM mail_outbox") == [{"n": 0}]


def test_d1_every_case_reaches_a_waiting_state(demo: Demo) -> None:
    """Waiting (D-028): a dispute or escalation waiting on a person, or an assessed case
    waiting for its scheduled follow-up date (and, for INV-003/INV-012, an approval)."""
    demo.run_agent()
    cases = {r["invoice_number"]: r for r in demo.rows("SELECT * FROM cases")}
    assert len(cases) == 9
    assert all(c["next_followup"] for c in cases.values())  # each has its follow-up date
    by_state: dict[str, set[str]] = {}
    for n, c in cases.items():
        by_state.setdefault(c["state"], set()).add(n)
    assert by_state == {
        "DISPUTE_OPEN": {"INV-004"},
        "ESCALATED": {"INV-009", "INV-011", "INV-014"},
        "ASSESSED": {"INV-003", "INV-005", "INV-008", "INV-012", "INV-013"},
    }
    escalated = by_state["ESCALATED"]
    tasks = {r["invoice_number"] for r in demo.rows("SELECT invoice_number FROM tasks")}
    assert escalated <= tasks  # every escalation created its human task
    assert cases["INV-004"]["state"] == "DISPUTE_OPEN"
    pending = demo.rows("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'")
    assert pending == [{"n": 2}]


def test_d1_the_plan_and_trace_are_visible(demo: Demo) -> None:
    run_id = demo.run_agent()
    run_page = demo.page(f"/runs/{run_id}")
    assert run_page.count('<h2><a href="/invoices/') == 9
    case = demo.page("/invoices/INV-003?as_of=2026-10-31")
    for fragment in (
        "<code>draft_message</code>",
        "<code>send_email</code>",
        "<code>schedule_followup</code>",
        "waiting for human approval",
        "This customer is not on the autopilot allowlist.",
        "Outside business hours",
        "Review before sending. Nothing is sent automatically.",
    ):
        assert fragment in case
    approvals = demo.page("/approvals")
    for invoice in ("INV-003", "INV-012"):  # DEMO step 5
        assert f"Payment reminder: invoice {invoice}" in approvals
    assert approvals.count("This customer is not on the autopilot allowlist.") == 2


def test_demo_steps_2_3_and_6_show_what_docs_demo_says(demo: Demo) -> None:
    demo.run_agent()
    inv4 = demo.page("/invoices/INV-004?as_of=2026-10-31")  # step 2
    for fragment in (
        "Open dispute: po amount mismatch.",
        "dispute open",
        "2026-11-03",
        '"doc_type": "po_copy"',
        '"owner_role": "finance"',
        '"dispute_label": "PO_AMOUNT_MISMATCH"',
        '"tone": 3',
        "Payment reminder: invoice INV-004",
    ):
        assert fragment in inv4
    inv11 = demo.page("/invoices/INV-011?as_of=2026-10-31")  # step 3
    assert "escalated" in inv11 and "<code>escalate</code>" in inv11
    assert '"to_role": "finance"' in inv11 and '"task_id": "RUN-' in inv11
    data = {"csrf_token": demo.csrf(), "as_of": "2026-10-31", "dry_run": "on"}  # step 6
    dry = demo.client.post("/agent/run", data=data)
    assert dry.status_code == 200
    text_ = html.unescape(dry.text)
    assert "Nothing was saved." in text_
    # Since S4.06 every case waits for the follow-up the first run scheduled (D-053).
    assert "0 cases, 0 steps would execute, 0 denied by policy" in text_
    assert "9 cases wait for their follow-up date" in text_
    assert len(demo.rows("SELECT run_id FROM agent_runs")) == 1


def test_d1_the_run_is_audited(demo: Demo) -> None:
    run_id = demo.run_agent()
    requested = demo.rows(
        "SELECT actor, after_json FROM audit_log WHERE action = 'agent.run_requested'"
    )
    assert len(requested) == 1 and requested[0]["actor"] == USER
    assert json.loads(requested[0]["after_json"])["request"]["as_of"] == "2026-10-31"
    steps = demo.rows("SELECT COUNT(*) AS n FROM agent_steps WHERE run_id = :r", r=run_id)
    step_audits = demo.rows("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'agent.step'")
    assert step_audits == steps  # every step, executed or denied, is audited
    transitions = demo.rows("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'case.transition'")
    assert transitions[0]["n"] >= 9 * 2  # each case created and assessed, at least
    run_page = demo.page(f"/runs/{run_id}")
    assert "agent.run_requested" in run_page and "case.transition" in run_page


def test_d1_the_kill_switch_stops_the_next_tick(demo: Demo) -> None:
    demo.client.post("/logout", data={"csrf_token": demo.csrf()})
    demo.login(ADMIN)
    headers = {"x-csrf-token": demo.csrf()}
    assert (
        demo.client.post("/api/reauth", json={"password": PASSWORD}, headers=headers).status_code
        == 200
    )
    switched = demo.client.post("/api/agent/kill-switch", json={"on": True}, headers=headers)
    assert switched.json()["on"] is True
    run_id = demo.run_agent()
    statuses = {
        s["status"] for s in demo.rows("SELECT status FROM agent_steps WHERE run_id = :r", r=run_id)
    }
    assert statuses == {"denied"}
    assert "KILL SWITCH ON" in demo.page("/")
    assert "The kill switch is on: nothing is sent." in demo.page(f"/runs/{run_id}")
    assert demo.rows("SELECT COUNT(*) AS n FROM cases") == [{"n": 0}]  # nothing changed
