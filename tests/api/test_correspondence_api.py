"""Correspondence in (SPEC §9 ``POST /api/correspondence``, §4.5, §4.6; D-034). Synthetic data."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.seed import seed

from .conftest import csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
MESSAGE = {
    "message_id": "M-90",
    "invoice_number": "INV-004",
    "customer_id": "CUST-B",
    "direction": "inbound",
    "sent_at": "2026-10-29T10:00:00+04:00",
    "language": "en",
    "subject": "Re: invoice INV-004",
    "body": "Synthetic text: please call about INV-004.",
}


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    return client


def post_json(client: TestClient, body: Any) -> Any:
    return client.post(
        "/api/correspondence",
        content=json.dumps(body),
        headers={"x-csrf-token": csrf(client), "content-type": "application/json"},
    )


def post_csv(client: TestClient, content: bytes, path: str = "/api/correspondence") -> Any:
    return client.post(
        path,
        files={"file": ("messages.csv", content, "text/csv")},
        headers={"x-csrf-token": csrf(client)},
    )


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def test_the_fixture_imports_through_both_routes(user: TestClient, engine: Engine) -> None:
    content = (FIXTURES / "correspondence.csv").read_bytes()
    first = post_csv(user, content, "/api/imports/correspondence")
    assert first.status_code == 200 and first.json()["rows_ok"] == 15
    again = post_csv(user, content)  # same bytes: a duplicate, nothing written
    assert again.json() == {**again.json(), "status": "duplicate", "inserted": 0}
    stored = rows(engine, "SELECT * FROM correspondence WHERE message_id = 'M-12'")
    assert stored[0]["sent_at"] == "2026-10-24T05:05:00Z"  # stored in UTC
    assert stored[0]["body"] == "تم سداد الفاتورة INV-005 بتاريخ 20 أكتوبر"


def test_json_messages_are_inserted(user: TestClient, engine: Engine) -> None:
    response = post_json(user, {"messages": [MESSAGE, {**MESSAGE, "message_id": "M-91"}]})
    assert response.status_code == 200
    assert response.json()["inserted"] == 2
    assert len(rows(engine, "SELECT 1 FROM correspondence")) == 2


def test_json_rows_are_validated_like_a_file(user: TestClient, engine: Engine) -> None:
    response = post_json(user, {"messages": [{**MESSAGE, "customer_id": "CUST-A"}]})
    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"line": 2, "code": "E_CUSTOMER_MISMATCH", "field": "customer_id"}
    ]
    assert rows(engine, "SELECT 1 FROM correspondence") == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": []},
        {"messages": [{**MESSAGE, "extra": "x"}]},
        {"messages": [{**MESSAGE, "sent_at": 20261029}]},
        {"messages": "M-90"},
        [MESSAGE],
    ],
)
def test_malformed_json_is_refused(user: TestClient, body: Any) -> None:
    response = post_json(user, body)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_the_audit_row_holds_no_message_text(user: TestClient, engine: Engine) -> None:
    post_json(user, {"messages": [MESSAGE]})
    audit = rows(engine, "SELECT actor, before_json, after_json FROM audit_log "
                 "WHERE action = 'import.accepted' ORDER BY rowid DESC LIMIT 1")  # fmt: skip
    assert audit[0]["actor"] == "demo.user"
    assert "please call" not in audit[0]["before_json"] + audit[0]["after_json"]


def test_it_needs_a_session_and_a_csrf_token(client: TestClient) -> None:
    anonymous = client.post("/api/correspondence", json={"messages": [MESSAGE]})
    assert anonymous.status_code == 401
    assert login(client) == 303
    no_token = client.post("/api/correspondence", json={"messages": [MESSAGE]})
    assert no_token.status_code == 403
