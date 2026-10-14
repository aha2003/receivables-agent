"""Case detail, trace, approvals and run pages (SPEC §10, §18): T-UI-04 and the trace UI.

Synthetic golden data only. The app clock starts at 2026-10-31T09:00 Dubai (a Saturday).
"""

import html
import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, SimulatedClock
from receivables.adapters.db.controls import set_kill_switch
from receivables.adapters.db.repo import UserRecord
from receivables.agent.policy import REASON_CODES
from receivables.api.main import create_app
from receivables.seed import seed
from receivables.settings import Settings
from receivables.web.labels import POLICY_TEXT, flag_text, policy_text

from .conftest import SECRET, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def seeded(engine: Engine, clock: SimulatedClock) -> Engine:
    seed(engine, FIXTURES, clock)
    return engine


@pytest.fixture
def user(client: TestClient, seeded: Engine) -> TestClient:
    assert login(client) == 303
    return client


def run_agent(client: TestClient, dry: bool = False) -> Any:
    data = {"csrf_token": csrf(client)} | ({"dry_run": "on"} if dry else {})
    return client.post("/agent/run", data=data)


def page(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return html.unescape(response.text)


# --- the case page after an L1 run --------------------------------------------------------------


def test_case_page_shows_plan_tools_policy_and_results(user: TestClient) -> None:
    response = run_agent(user)
    assert response.status_code == 303
    body = page(user, "/invoices/INV-004?as_of=2026-10-31")
    tools = re.findall(
        r"<code>(request_internal_document|draft_message|schedule_followup)</code>", body
    )
    assert tools == ["request_internal_document", "draft_message", "schedule_followup"]
    assert "Open dispute: po amount mismatch." in body
    assert "dispute open" in body  # case state
    assert "2026-11-03" in body  # next follow-up
    assert '"doc_type": "po_copy"' in body  # arguments
    assert "122 days overdue, active dispute PO_AMOUNT_MISMATCH" in body  # reason
    assert "Review before sending. Nothing is sent automatically." in body  # L1 draft panel
    assert "Payment reminder: invoice INV-004" in body  # the draft


def test_a_denied_send_shows_its_reasons_in_plain_language(user: TestClient) -> None:
    run_agent(user)
    body = page(user, "/invoices/INV-003?as_of=2026-10-31")
    assert "<code>send_email</code>" in body
    assert "waiting for human approval" in body
    for code in ("POLICY_NOT_ALLOWLISTED", "POLICY_OUTSIDE_HOURS"):  # Saturday, empty allowlist
        assert code in body and POLICY_TEXT[code] in body


def test_before_any_run_the_case_page_says_so(user: TestClient) -> None:
    body = page(user, "/invoices/INV-004")
    assert "No case yet" in body
    assert "The agent has not planned this invoice yet." in body
    assert "No drafts yet." in body


# --- T-UI-04: flags in plain language ------------------------------------------------------------


def test_t_ui_04_flags_are_visible_in_plain_language(user: TestClient, seeded: Engine) -> None:
    with seeded.begin() as c:
        c.execute(
            text(
                "INSERT INTO payments (payment_id, customer_id, payment_date, amount_fils)"
                " VALUES ('P-005', 'CUST-C', '2026-10-28', 1200000)"
            )
        )
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, invoice_number, customer_id, direction,"
                " sent_at, subject, body) VALUES ('M-01', 'INV-012', 'CUST-C', 'inbound',"
                " '2026-10-29T06:00:00Z', 'Paid already', 'Synthetic message body')"
            )
        )
        flags = [
            {"code": "UNMATCHED_INVOICE", "invoice_number": "INV-9999"},
            {"code": "LEDGER_STILL_OPEN", "invoice_number": "INV-012", "amount": "9000.00"},
        ]
        c.execute(
            text(
                "INSERT INTO classifications (message_id, label, confidence, extraction_json,"
                " flags_json, needs_review, created_at) VALUES ('M-01', 'ALREADY_PAID', 0.9,"
                " '{}', :f, 1, '2026-10-29T06:00:00Z')"
            ),
            {"f": json.dumps(flags)},
        )
    body = page(user, "/invoices/INV-012?as_of=2026-10-31")
    assert (
        "This customer has AED 12,000.00 of unallocated cash. "
        "Check whether it belongs to this invoice before chasing." in body
    )
    assert (
        "Customer says paid but the ledger shows AED 9,000.00 open. Check the bank statement."
        in body
    )
    assert "The message mentions invoice INV-9999, which is not in the ledger." in body
    assert "UNMATCHED_INVOICE" not in body.split("<h2>Flags</h2>")[1].split("</section>")[0]


@pytest.mark.parametrize(
    ("flag", "text_part"),
    [
        ("UNMATCHED_INVOICE", "not in the ledger"),
        ({"code": "CUSTOMER_INVOICE_MISMATCH"}, "different customer"),
        ({"code": "E_EVIDENCE_NOT_VERBATIM"}, "word for word"),
        ({"code": "UNVERIFIED_EXTRACTION"}, "could not be verified"),
        ({"code": "LEDGER_STILL_OPEN", "amount": 2000000}, "AED 20,000.00 open"),
        ({"code": "LEDGER_STILL_OPEN", "amount": "abc"}, "LEDGER_STILL_OPEN"),
        ({"code": "SOMETHING_NEW"}, "SOMETHING_NEW"),  # never hidden
        ({}, "?"),
    ],
)
def test_flag_wording(flag: object, text_part: str) -> None:
    assert text_part in flag_text(flag)


def test_every_policy_reason_has_wording() -> None:
    assert set(POLICY_TEXT) == set(REASON_CODES)
    assert policy_text("POLICY_SOMETHING_NEW") == "POLICY_SOMETHING_NEW"


# --- approvals, run pages and the run form ------------------------------------------------------


def test_approvals_page_lists_pending_items(user: TestClient) -> None:
    run_agent(user)
    body = page(user, "/approvals")
    assert body.count('href="/invoices/INV-003"') == 1 and 'href="/invoices/INV-012"' in body
    assert "Payment reminder: invoice INV-003" in body
    assert POLICY_TEXT["POLICY_OUTSIDE_HOURS"] in body


def test_run_page_follows_every_case(user: TestClient) -> None:
    response = run_agent(user)
    location = response.headers["location"]
    assert location.startswith("/runs/RUN-")
    body = page(user, location)
    assert body.count('<h2><a href="/invoices/') == 9
    assert "Planner</dt><dd>rules" in body
    assert "agent.run_requested" in body and "case.transition" in body


def test_a_dry_run_from_the_form_saves_nothing(user: TestClient, seeded: Engine) -> None:
    response = run_agent(user, dry=True)
    assert response.status_code == 200
    assert "Nothing was saved." in html.unescape(response.text)
    with seeded.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agent_runs")).scalar_one() == 0


def test_the_run_form_needs_csrf(user: TestClient) -> None:
    response = user.post("/agent/run", data={})
    assert response.status_code == 403


def test_unknown_run_page_is_404(user: TestClient) -> None:
    assert user.get("/runs/RUN-nope").status_code == 404


# --- kill switch, L2 decisions and escaping ----------------------------------------------------


def test_the_kill_switch_banner(user: TestClient, seeded: Engine, clock: SimulatedClock) -> None:
    assert "KILL SWITCH ON" not in page(user, "/")
    admin = UserRecord(user_id=99, username="admin.user", password_hash="x", role="admin")
    set_kill_switch(seeded, clock, admin, on=True, reauthenticated_at=clock.now())
    for path in ("/", "/queue", "/approvals", "/invoices/INV-004"):
        assert "KILL SWITCH ON: the agent executes nothing." in page(user, path)


@pytest.fixture
def l2_client(engine: Engine, clock: SimulatedClock, tmp_path: Path) -> Iterator[TestClient]:
    seed(engine, FIXTURES, clock)
    config = tmp_path / "config.yaml"
    config.write_text(
        "agent:\n  autonomy_level: 2\n  l2:\n    allowlisted_customers: [CUST-A]\n",
        encoding="utf-8",
    )
    settings = Settings(
        session_secret=SecretStr(SECRET), database_url=str(engine.url), config_path=str(config)
    )
    clock.set(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))  # Monday, business hours
    app = create_app(settings, clock=clock, engine=engine)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        assert login(c) == 303
        yield c


def test_at_l2_the_draft_panel_shows_the_policy_decision(l2_client: TestClient) -> None:
    run_agent(l2_client)  # as of Monday 2 Nov: INV-010 (CUST-A, 2 days) is sent; INV-003 is tone 2
    sent = page(l2_client, "/invoices/INV-010")
    assert "Review before sending" not in sent
    assert "Policy decision:" in sent and "allowed" in sent and "done" in sent
    denied = page(l2_client, "/invoices/INV-012")
    assert "Policy decision:" in denied
    assert POLICY_TEXT["POLICY_NOT_ALLOWLISTED"] in denied
    no_send = page(l2_client, "/invoices/INV-003")  # 32 days overdue: no send is planned
    assert "No send has been attempted for this draft." in no_send


def test_planner_text_is_escaped(user: TestClient, seeded: Engine, clock: SimulatedClock) -> None:
    run_agent(user)
    with seeded.begin() as c:
        c.execute(text("UPDATE agent_steps SET reason = '<script>alert(1)</script>' WHERE seq = 1"))
    raw = user.get("/invoices/INV-004?as_of=2026-10-31").text
    assert "<script>alert(1)</script>" not in raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw


def test_case_pages_need_a_session(client: TestClient, seeded: Engine) -> None:
    for path in ("/invoices/INV-004", "/approvals", "/runs/RUN-x"):
        response = client.get(path, headers={"accept": "text/html"})
        assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_old_messages_show_in_the_timeline(user: TestClient, seeded: Engine) -> None:
    with seeded.begin() as c:
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, invoice_number, direction, sent_at,"
                " subject) VALUES ('M-9', 'INV-004', 'outbound', '2026-10-20T06:00:00Z',"
                " 'Earlier reminder')"
            )
        )
    assert '"Earlier reminder"' in page(user, "/invoices/INV-004?as_of=2026-10-31")
    assert "Earlier reminder" not in page(user, "/invoices/INV-004?as_of=2026-10-15")


def test_the_timeline_note(user: TestClient) -> None:
    assert "Outcomes are added to this page in a later step." in page(user, "/invoices/INV-004")


def test_each_flag_is_shown_once(user: TestClient, seeded: Engine) -> None:
    with seeded.begin() as c:
        c.execute(
            text(
                "INSERT INTO payments (payment_id, customer_id, payment_date, amount_fils)"
                " VALUES ('P-005', 'CUST-C', '2026-10-28', 1200000)"
            )
        )
    body = page(user, "/invoices/INV-012?as_of=2026-10-31")
    assert body.count("of unallocated cash. Check whether it belongs to this invoice") == 1
