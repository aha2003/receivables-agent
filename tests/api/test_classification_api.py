"""Classify, accept and reject (SPEC §9, §7.1, §7.2, §7.4): T-API-05 and the §7.1 rules.

The LLM is a FakeClient behind the app's redaction (D-031); nothing reaches the network.
All messages are the synthetic §6.10 fixtures.
"""

import json
from collections.abc import Callable, Mapping
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
        "label": "NO_ACTION_NEEDED",
        "confidence": 0.9,
        "invoice_numbers": [],
        "po_numbers": [],
        "amounts_mentioned": [],
        "dates_mentioned": [],
        "promised_payment_date": None,
        "evidence_quote": "",
        "summary_en": "Synthetic summary.",
    }
    return base | fields


M01 = answer(
    label="PO_AMOUNT_MISMATCH",
    invoice_numbers=["INV-004"],
    po_numbers=["PO-B-004"],
    amounts_mentioned=[
        {"value": "47000", "currency": "AED", "role": "po_amount"},
        {"value": "50000", "currency": "AED", "role": "invoice_amount"},
    ],
    evidence_quote="purchase order PO-B-004 was issued for AED 47,000",
)
M03 = answer(
    label="PAYMENT_PROMISED",
    invoice_numbers=["INV-008"],
    dates_mentioned=[{"value": "20 October", "role": "promised_payment"}],
    promised_payment_date="20 October",
    evidence_quote="Payment for INV-008 will be released on 20 October.",
)


@pytest.fixture
def app_with_messages(app: FastAPI, engine: Engine, clock: SimulatedClock) -> FastAPI:
    seed(engine, FIXTURES, clock)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        assert login(c) == 303
        r = c.post(
            "/api/imports/correspondence",
            files={"file": ("c.csv", (FIXTURES / "correspondence.csv").read_bytes(), "text/csv")},
            headers={"x-csrf-token": csrf(c)},
        )
        assert r.status_code == 200
    return app


Script = Callable[[Mapping[str, Any]], Mapping[str, Any] | str]


def use_llm(app: FastAPI, engine: Engine, script: Script) -> FakeClient:
    fake = FakeClient(classify=script)
    app.state.llm = RedactingClient(
        fake,
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=customer_names(engine),
    )
    return fake


@pytest.fixture
def user(app_with_messages: FastAPI, client: TestClient) -> TestClient:
    assert login(client) == 303
    return client


def post(client: TestClient, path: str, body: Any = None) -> Any:
    kwargs: dict[str, Any] = {"headers": {"x-csrf-token": csrf(client)}}
    if body is not None:
        kwargs["json"] = body
    return client.post(path, **kwargs)


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


# --- T-API-05 ----------------------------------------------------------------------------------


@pytest.mark.spec("T-API-05")
def test_api_05_classify_m01_and_accept(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    classified = post(user, "/api/correspondence/M-01/classify")
    assert classified.status_code == 200
    body = classified.json()
    assert (body["label"], body["status"], body["needs_review"]) == (
        "PO_AMOUNT_MISMATCH",
        "pending",
        False,
    )
    assert body["flags"] == [] and body["amount_difference"] == "3000.00"
    assert body["model"] == "fake"

    accepted = post(user, "/api/classifications/M-01/accept")
    assert accepted.status_code == 200
    event = accepted.json()["event"]
    assert event == {
        "event_id": "CLS-M-01",
        "invoice_number": "INV-004",
        "event_type": "DISPUTE_OPENED",
        "event_date": "2026-10-12",  # the message's sent_at day in Dubai
        "label": "PO_AMOUNT_MISMATCH",
        "promise_date": None,
        "source": "classification",
    }
    stored = rows(engine, "SELECT * FROM events WHERE event_id = 'CLS-M-01'")
    assert stored[0]["event_type"] == "DISPUTE_OPENED"
    assert stored[0]["label"] == "PO_AMOUNT_MISMATCH"
    status = rows(engine, "SELECT status FROM classifications WHERE message_id = 'M-01'")
    assert status == [{"status": "accepted"}]
    audit = rows(
        engine,
        "SELECT actor, before_json, after_json FROM audit_log "
        "WHERE action = 'classification.accepted' AND entity_id = 'M-01'",
    )
    assert len(audit) == 1 and audit[0]["actor"] == "demo.user"
    assert json.loads(audit[0]["before_json"])["status"] == "pending"
    after = json.loads(audit[0]["after_json"])
    assert after["status"] == "accepted" and after["event"]["event_id"] == "CLS-M-01"


# --- classify ----------------------------------------------------------------------------------


def test_the_llm_sees_redacted_text_and_the_evidence_still_verifies(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, invoice_number, customer_id, direction,"
                " sent_at, language, body) VALUES ('M-40', 'INV-004', 'CUST-B', 'inbound',"
                " '2026-10-29T06:00:00Z', 'en', 'Beta Contracting LLC disputes INV-004.')"
            )
        )
    fake = use_llm(
        app_with_messages,
        engine,
        lambda _: answer(
            label="PRICE_OR_RATE_DISPUTE",
            invoice_numbers=["INV-004"],
            evidence_quote="[CUSTOMER] disputes INV-004.",
        ),
    )
    body = post(user, "/api/correspondence/M-40/classify").json()
    assert fake.calls[0][1]["text"] == "[CUSTOMER] disputes INV-004."
    assert body["flags"] == [] and body["needs_review"] is False


def test_a_promise_is_resolved_from_sent_at_and_accepted(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: M03)
    body = post(user, "/api/correspondence/M-03/classify").json()
    assert body["promised_payment_date"] == "2026-10-20"
    event = post(user, "/api/classifications/M-03/accept").json()["event"]
    assert (event["event_type"], event["invoice_number"], event["promise_date"]) == (
        "PROMISE_TO_PAY",
        "INV-008",
        "2026-10-20",
    )
    assert event["label"] is None


def test_bad_llm_output_becomes_unclear_for_review(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    fake = use_llm(app_with_messages, engine, lambda _: "{not json")
    body = post(user, "/api/correspondence/M-01/classify").json()
    assert (body["label"], body["confidence"], body["needs_review"]) == ("UNCLEAR", 0.0, True)
    assert body["error"] == "schema_invalid"
    assert len(fake.calls) == 2  # one retry (§7.2)


def test_the_default_fake_mode_falls_back_without_an_error(user: TestClient) -> None:
    body = post(user, "/api/correspondence/M-02/classify").json()  # unscripted FakeClient
    assert (body["label"], body["needs_review"], body["error"]) == (
        "UNCLEAR",
        True,
        "FakeScriptExhausted",
    )


def test_flags_are_stored_and_returned(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(
        app_with_messages,
        engine,
        lambda _: answer(
            label="ALREADY_PAID",
            invoice_numbers=["INV-005", "INV-777"],
            evidence_quote="We paid INV-005 in full",
        ),
    )
    body = post(user, "/api/correspondence/M-06/classify").json()
    assert {"code": "LEDGER_STILL_OPEN", "invoice_number": "INV-005", "amount": 2000000} in body[
        "flags"
    ]
    assert {"code": "UNMATCHED_INVOICE", "invoice_number": "INV-777"} in body["flags"]
    assert body["needs_review"] is True


def test_reclassifying_a_pending_message_replaces_it(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: answer(label="UNCLEAR"))
    post(user, "/api/correspondence/M-01/classify")
    use_llm(app_with_messages, engine, lambda _: M01)
    assert post(user, "/api/correspondence/M-01/classify").json()["label"] == "PO_AMOUNT_MISMATCH"
    assert len(rows(engine, "SELECT 1 FROM classifications")) == 1
    actions = rows(engine, "SELECT action FROM audit_log WHERE entity_id = 'M-01'")
    assert [a["action"] for a in actions] == ["classification.created"] * 2


@pytest.mark.parametrize(
    ("setup", "status"),
    [("unknown", 404), ("outbound", 409), ("no_body", 409), ("accepted", 409)],
)
def test_classify_refusals(
    user: TestClient, app_with_messages: FastAPI, engine: Engine, setup: str, status: int
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    message = {"unknown": "M-404", "outbound": "M-50", "no_body": "M-51", "accepted": "M-01"}[setup]
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, direction, sent_at, body) VALUES "
                "('M-50', 'outbound', '2026-10-29T06:00:00Z', 'Synthetic reminder'), "
                "('M-51', 'inbound', '2026-10-29T06:00:00Z', NULL)"
            )
        )
    if setup == "accepted":
        post(user, "/api/correspondence/M-01/classify")
        post(user, "/api/classifications/M-01/accept")
    response = post(user, f"/api/correspondence/{message}/classify")
    assert response.status_code == status


# --- accept and reject (§7.1) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "event_type"),
    [
        ("MISSING_DELIVERY_NOTE", "DISPUTE_OPENED"),
        ("GOODS_OR_SERVICE_ISSUE", "DISPUTE_OPENED"),
        ("ALREADY_PAID", None),
        ("AWAITING_INTERNAL_APPROVAL", None),
        ("CASHFLOW_DELAY", None),
        ("REQUEST_STATEMENT_OR_COPY", None),
        ("NO_ACTION_NEEDED", None),
        ("UNCLEAR", None),
    ],
)
def test_accept_follows_the_7_1_table(
    user: TestClient,
    app_with_messages: FastAPI,
    engine: Engine,
    label: str,
    event_type: str | None,
) -> None:
    use_llm(app_with_messages, engine, lambda _: answer(label=label, invoice_numbers=["INV-009"]))
    post(user, "/api/correspondence/M-02/classify")
    result = post(user, "/api/classifications/M-02/accept").json()
    events = rows(engine, "SELECT event_type, label FROM events WHERE source = 'classification'")
    if event_type is None:
        assert result["event"] is None and events == []
    else:
        assert events == [{"event_type": event_type, "label": label}]
    assert result["classification"]["status"] == "accepted"


def test_a_label_override_decides_the_event(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: answer(label="UNCLEAR"))
    post(user, "/api/correspondence/M-02/classify")
    result = post(
        user, "/api/classifications/M-02/accept", {"label_override": "MISSING_DELIVERY_NOTE"}
    ).json()
    assert result["event"]["label"] == "MISSING_DELIVERY_NOTE"
    assert result["event"]["invoice_number"] == "INV-009"  # the message's own invoice
    assert result["classification"]["label"] == "MISSING_DELIVERY_NOTE"


def test_a_promise_without_a_date_needs_one_on_accept(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: answer(label="PAYMENT_PROMISED"))
    post(user, "/api/correspondence/M-03/classify")
    refused = post(user, "/api/classifications/M-03/accept")
    assert refused.status_code == 422
    early = post(user, "/api/classifications/M-03/accept", {"promised_payment_date": "2026-10-01"})
    assert early.status_code == 422  # before the message date
    ok = post(user, "/api/classifications/M-03/accept", {"promised_payment_date": "2026-10-25"})
    assert ok.json()["event"]["promise_date"] == "2026-10-25"


def test_an_event_needs_one_invoice(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: answer(label="MISSING_PO"))
    post(user, "/api/correspondence/M-13/classify")  # a message with no invoice
    response = post(user, "/api/classifications/M-13/accept")
    assert response.status_code == 409
    assert rows(engine, "SELECT status FROM classifications") == [{"status": "pending"}]


@pytest.mark.parametrize(
    "body",
    [
        {"label_override": "PAID"},
        {"promised_payment_date": "20 October"},
        {"promised_payment_date": "2026-02-30"},
        {"extra": 1},
    ],
)
def test_accept_refuses_bad_bodies(
    user: TestClient, app_with_messages: FastAPI, engine: Engine, body: dict[str, Any]
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    post(user, "/api/correspondence/M-01/classify")
    assert post(user, "/api/classifications/M-01/accept", body).status_code == 422


def test_reject_and_the_state_rules(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    assert post(user, "/api/classifications/M-01/reject").status_code == 409  # not classified
    post(user, "/api/correspondence/M-01/classify")
    rejected = post(user, "/api/classifications/M-01/reject")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert post(user, "/api/classifications/M-01/accept").status_code == 409
    assert post(user, "/api/classifications/M-01/reject").status_code == 409
    assert post(user, "/api/classifications/M-404/reject").status_code == 404
    assert rows(engine, "SELECT 1 FROM events WHERE source = 'classification'") == []
    audit = rows(engine, "SELECT 1 FROM audit_log WHERE action = 'classification.rejected'")
    assert len(audit) == 1


def test_audit_rows_hold_no_message_text(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    post(user, "/api/correspondence/M-01/classify")
    post(user, "/api/classifications/M-01/accept")
    audit = rows(engine, "SELECT before_json, after_json FROM audit_log WHERE entity_id = 'M-01'")
    text_seen = json.dumps(audit)
    assert "Please correct" not in text_seen and "purchase order" not in text_seen
    assert "Synthetic summary" not in text_seen


def test_the_routes_need_a_session_and_csrf(client: TestClient) -> None:
    for path in (
        "/api/correspondence/M-01/classify",
        "/api/classifications/M-01/accept",
        "/api/classifications/M-01/reject",
    ):
        assert client.post(path).status_code == 401
    assert login(client) == 303
    for path in ("/api/correspondence/M-01/classify", "/api/classifications/M-01/accept"):
        assert client.post(path).status_code == 403


# --- edges found by mutation testing -------------------------------------------------------------


def _insert(engine: Engine, message_id: str, invoice: str | None, customer: str | None,
            body: str, language: str = "en") -> None:  # fmt: skip
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, invoice_number, customer_id, direction,"
                " sent_at, language, body) VALUES (:m, :i, :c, 'inbound', '2026-10-29T06:00:00Z',"
                " :l, :b)"
            ),
            {"m": message_id, "i": invoice, "c": customer, "l": language, "b": body},
        )


def test_an_accepted_message_is_not_sent_to_the_model_again(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    post(user, "/api/correspondence/M-01/classify")
    post(user, "/api/classifications/M-01/accept")
    fake = use_llm(app_with_messages, engine, lambda _: M01)
    assert post(user, "/api/correspondence/M-01/classify").status_code == 409
    assert fake.calls == []  # refused before any LLM call


def test_an_accept_during_the_model_call_wins(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    use_llm(app_with_messages, engine, lambda _: M01)
    post(user, "/api/correspondence/M-01/classify")

    def accept_meanwhile(_: Mapping[str, Any]) -> Mapping[str, Any]:
        with engine.begin() as c:  # someone accepts while the model is thinking
            c.execute(text("UPDATE classifications SET status = 'accepted'"))
        return answer(label="UNCLEAR")

    use_llm(app_with_messages, engine, accept_meanwhile)
    assert post(user, "/api/correspondence/M-01/classify").status_code == 409
    stored = rows(engine, "SELECT label, status FROM classifications")
    assert stored == [{"label": "PO_AMOUNT_MISMATCH", "status": "accepted"}]  # not overwritten


def test_the_known_customer_can_come_from_the_messages_invoice(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    # No customer_id, but the message is about INV-001 (CUST-A); INV-004 belongs to CUST-B.
    _insert(engine, "M-60", "INV-001", None, "About INV-004: wrong quantity.")
    use_llm(
        app_with_messages,
        engine,
        lambda _: answer(label="GOODS_OR_SERVICE_ISSUE", invoice_numbers=["INV-004"],
                         evidence_quote="wrong quantity"),
    )  # fmt: skip
    flags = post(user, "/api/correspondence/M-60/classify").json()["flags"]
    assert {"code": "CUSTOMER_INVOICE_MISMATCH", "invoice_number": "INV-004"} in flags


def test_the_event_goes_to_the_messages_own_invoice(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    # Filed under INV-009 (CUST-C); the text names INV-011, which belongs to CUST-B. The model's
    # reading never moves an event onto another customer's invoice (spec review, D-034).
    _insert(engine, "M-61", "INV-009", "CUST-C", "INV-011 has no PO number.")
    use_llm(
        app_with_messages,
        engine,
        lambda _: answer(label="MISSING_PO", invoice_numbers=["INV-011"],
                         evidence_quote="INV-011 has no PO number."),
    )  # fmt: skip
    body = post(user, "/api/correspondence/M-61/classify").json()
    assert {"code": "CUSTOMER_INVOICE_MISMATCH", "invoice_number": "INV-011"} in body["flags"]
    event = post(user, "/api/classifications/M-61/accept").json()["event"]
    assert event["invoice_number"] == "INV-009"


def test_without_its_own_invoice_a_clean_single_match_carries_the_event(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    _insert(engine, "M-63", None, "CUST-B", "INV-011 has no PO number.")
    use_llm(
        app_with_messages,
        engine,
        lambda _: answer(label="MISSING_PO", invoice_numbers=["INV-011"],
                         evidence_quote="INV-011 has no PO number."),
    )  # fmt: skip
    post(user, "/api/correspondence/M-63/classify")
    assert post(user, "/api/classifications/M-63/accept").json()["event"]["invoice_number"] == (
        "INV-011"
    )


@pytest.mark.parametrize(
    ("customer", "body", "extracted"),
    [
        ("CUST-C", "INV-011 has no PO number.", "INV-011"),  # another customer's invoice
        ("CUST-B", "Our invoice has no PO number.", "INV-011"),  # not in the text
        ("CUST-B", "INV-011 and INV-777 have no PO.", "INV-011"),  # a doubtful reference too
    ],
)
def test_a_doubtful_match_never_carries_an_event(
    user: TestClient,
    app_with_messages: FastAPI,
    engine: Engine,
    customer: str,
    body: str,
    extracted: str,
) -> None:
    _insert(engine, "M-64", None, customer, body)
    invoices = [extracted, "INV-777"] if "INV-777" in body else [extracted]
    use_llm(
        app_with_messages,
        engine,
        lambda _: answer(label="MISSING_PO", invoice_numbers=invoices, evidence_quote=body),
    )
    post(user, "/api/correspondence/M-64/classify")
    assert post(user, "/api/classifications/M-64/accept").status_code == 409
    assert rows(engine, "SELECT 1 FROM events WHERE source = 'classification'") == []


def test_an_unreadable_promise_date_needs_review(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    _insert(engine, "M-65", "INV-008", "CUST-C", "We will pay INV-008 next Tuesday.")
    use_llm(
        app_with_messages,
        engine,
        lambda _: answer(label="PAYMENT_PROMISED", invoice_numbers=["INV-008"],
                         promised_payment_date="next Tuesday",
                         evidence_quote="We will pay INV-008 next Tuesday."),
    )  # fmt: skip
    body = post(user, "/api/correspondence/M-65/classify").json()
    assert body["flags"] == [] and body["promised_payment_date"] is None
    assert body["needs_review"] is True


def test_a_message_changed_during_the_model_call_is_not_classified(
    user: TestClient, app_with_messages: FastAPI, engine: Engine
) -> None:
    def edit_meanwhile(_: Mapping[str, Any]) -> Mapping[str, Any]:
        with engine.begin() as c:
            c.execute(text("UPDATE correspondence SET body = 'Edited' WHERE message_id = 'M-01'"))
        return M01

    use_llm(app_with_messages, engine, edit_meanwhile)
    assert post(user, "/api/correspondence/M-01/classify").status_code == 409
    assert rows(engine, "SELECT 1 FROM classifications") == []


def test_unreadable_customer_names_fall_back_cleanly(
    user: TestClient, app_with_messages: FastAPI
) -> None:
    def broken() -> tuple[str, ...]:
        raise RuntimeError("database is locked")

    app_with_messages.state.llm = RedactingClient(
        FakeClient(classify=lambda _: M01),
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=broken,
    )
    response = post(user, "/api/correspondence/M-01/classify")
    assert response.status_code == 200
    body = response.json()
    assert (body["label"], body["needs_review"], body["error"]) == (
        "UNCLEAR",
        True,
        "RedactionUnavailable",
    )


@pytest.mark.parametrize(("language", "hint"), [("auto", None), ("ar", "ar"), (None, None)])
def test_the_language_hint(
    user: TestClient, app_with_messages: FastAPI, engine: Engine, language: str | None,
    hint: str | None,
) -> None:  # fmt: skip
    _insert(engine, "M-62", None, None, "Thanks, noted.", language or "")
    if language is None:
        with engine.begin() as c:
            c.execute(text("UPDATE correspondence SET language = NULL WHERE message_id = 'M-62'"))
    fake = use_llm(app_with_messages, engine, lambda _: answer())
    post(user, "/api/correspondence/M-62/classify")
    assert fake.calls[0][1]["language_hint"] == hint
