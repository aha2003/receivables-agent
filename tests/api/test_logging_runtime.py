"""T-SEC-04: process M-01 and inspect the logs (SPEC NFR-07; D-031).

With the server's logging in place, M-01 with personal data injected goes through the LLM
client, the agent runs a tick over the golden data and the web app serves requests. The JSON log
output holds no message body, email, phone, TRN, IBAN or customer name, and the LLM call is
logged with its allowed fields only. All values are synthetic.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from llm_capture import CLASSIFICATION, CapturingClient
from log_capture import json_logs
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.repo import customer_names, load_customers
from receivables.adapters.llm.redacting import RedactingClient
from receivables.seed import seed

from .conftest import csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
M01 = (
    "We cannot process invoice INV-004 because purchase order PO-B-004 was issued for "
    "AED 47,000, but the invoice is for AED 50,000. Please correct."
)
INJECTED = (
    "ahmed.k@beta.example",
    "+971 50 123 4567",
    "100000000100003",
    "AE070331234567890123456",
)
LLM_FIELDS = {
    "event", "level", "logger", "timestamp", "kind", "model", "prompt_version", "latency_ms",
    "input_tokens", "output_tokens", "prompt_sha256", "outcome",
}  # fmt: skip


class TestSec04:
    def test_processing_m01_logs_no_body_and_no_personal_data(
        self, client: TestClient, engine: Engine, clock: SimulatedClock
    ) -> None:
        seed(engine, FIXTURES, clock)
        with engine.connect() as conn:
            names = [c.customer_name for c in load_customers(conn)]
        message = (
            f"{M01} {names[1]}: {INJECTED[0]}, {INJECTED[1]}, TRN {INJECTED[2]}, {INJECTED[3]}"
        )
        with json_logs(customer_names(engine)) as logs:
            llm = RedactingClient(
                CapturingClient(),
                redact_before_send=True,
                pseudonymize_customer_names=True,
                customer_names=customer_names(engine),
            )
            assert llm.classify(message, "en").label == "PO_AMOUNT_MISMATCH"
            assert login(client) == 303
            run = client.post(
                "/api/agent/run",
                json={"as_of": "2026-10-31"},
                headers={"X-CSRF-Token": csrf(client)},
            )
            assert run.status_code == 200
            assert client.get("/invoices/INV-004").status_code == 200
        text = logs.text()
        lines = logs.lines()
        assert lines, "nothing was logged"
        assert "We cannot process invoice" not in text and "Please correct" not in text  # body
        assert [v for v in INJECTED if v in text] == []
        assert [n for n in names if n.lower() in text.lower()] == []
        # The LLM call: model, latency, token counts and a prompt hash only (NFR-07).
        calls = [line for line in lines if line["event"] == "llm.call"]
        # Draft calls too (S3.07); those made during a web request also carry its request_id.
        assert calls and all(set(c) - {"request_id"} == LLM_FIELDS for c in calls)
        assert {c["kind"] for c in calls} == {"classify", "draft"}
        (call,) = [c for c in calls if c["kind"] == "classify"]
        assert (call["kind"], call["model"], call["outcome"]) == ("classify", "fake", "ok")
        assert len(call["prompt_sha256"]) == 64
        # The tick is logged, with the request id of the request that ran it.
        (tick,) = [line for line in lines if line["event"] == "agent tick finished"]
        assert tick["request_id"] == run.headers["x-request-id"]

    def test_a_failed_llm_call_logs_its_error_type_only(self, engine: Engine) -> None:
        llm = RedactingClient(
            CapturingClient(),
            redact_before_send=True,
            pseudonymize_customer_names=True,
            customer_names=lambda: (),
        )
        llm.inner._functions.pop("classify")  # type: ignore[attr-defined]  # nothing scripted
        with json_logs(lambda: ()) as logs, pytest.raises(LookupError):
            llm.classify(M01, "en")
        (call,) = logs.lines()
        assert call["outcome"] == "FakeScriptExhausted"
        assert "INV-004" not in logs.text()


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    first, second = client.get("/healthz"), client.get("/login")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_the_server_entry_point_turns_on_scrubbed_json_logs(tmp_path: Path) -> None:
    # A separate process: the server's app object, then one log line naming a customer. The
    # database has no tables yet, so the names cannot be read and the line fails closed.
    code = (
        "import logging, receivables.api.main as m; m.app; "
        "logging.getLogger('receivables.x').info('hello Beta Contracting LLC')"
    )
    env = {
        **os.environ,
        "SESSION_SECRET": "test-only-session-secret-0123456789abcdef",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'empty.db'}",
    }
    done = subprocess.run(  # noqa: S603 - a fixed command: this interpreter and inline code
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    (line,) = [json.loads(x) for x in done.stderr.splitlines() if x.startswith("{")]
    assert line == {
        "event": "[REDACTED]", "level": "info", "logger": "receivables.x",
        "timestamp": line["timestamp"],
    }  # fmt: skip
    assert "Beta" not in done.stdout + done.stderr


def test_sec_04_the_full_classify_path_logs_no_body_or_personal_data(
    client: TestClient, engine: Engine, clock: SimulatedClock
) -> None:
    # D-031 promised T-SEC-04 again once the classification pipeline exists (S3.06).
    from receivables.adapters.llm.fake import FakeClient

    seed(engine, FIXTURES, clock)
    email, phone, trn, iban = INJECTED
    body = f"{M01} Beta Contracting LLC: {email}, {phone}, TRN {trn}, {iban}"
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, invoice_number, customer_id, direction,"
                " sent_at, language, body) VALUES ('M-70', 'INV-004', 'CUST-B', 'inbound',"
                " '2026-10-29T06:00:00Z', 'en', :b)"
            ),
            {"b": body},
        )
    with json_logs(customer_names(engine)) as logs:
        client.app.state.llm = RedactingClient(  # type: ignore[attr-defined]
            FakeClient(classify=lambda _: {**CLASSIFICATION, "evidence_quote": "Please correct."}),
            redact_before_send=True,
            pseudonymize_customer_names=True,
            customer_names=customer_names(engine),
        )
        assert login(client) == 303
        token = csrf(client)
        classified = client.post(
            "/api/correspondence/M-70/classify", headers={"X-CSRF-Token": token}
        )
        accepted = client.post("/api/classifications/M-70/accept", headers={"X-CSRF-Token": token})
        assert (classified.status_code, accepted.status_code) == (200, 200)
    logged = logs.text()
    assert "We cannot process invoice" not in logged and "Please correct" not in logged
    assert [v for v in INJECTED if v in logged] == []
    assert "beta contracting" not in logged.lower()
    events = [line["event"] for line in logs.lines()]
    assert "llm.call" in events and "classification.created" in events
