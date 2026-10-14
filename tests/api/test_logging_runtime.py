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
from llm_capture import CapturingClient
from log_capture import json_logs
from sqlalchemy import Engine

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
        (call,) = [line for line in lines if line["event"] == "llm.call"]
        assert set(call) == LLM_FIELDS
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
