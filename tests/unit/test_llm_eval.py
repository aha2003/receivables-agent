"""T-LLM-01: the classifier smoke gate over the 15 synthetic §6.10 messages (D-034).

The live run is manual (``scripts/eval_classifier.py``). Its ``--record`` option wrote
``tests/cassettes/t_llm_01.json`` (committed, D-035); the gate test below replays that recording
and enforces accuracy >= 0.80. Everything else here uses FakeClient; nothing reaches the
network.
"""

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from llm_capture import CLASSIFICATION

from receivables.adapters.config import load_config
from receivables.adapters.llm.classification import classify_message, to_extraction
from receivables.adapters.llm.evaluation import (
    SMOKE_GATE_ACCURACY,
    evaluate,
    fixture_customer_names,
    labelled_messages,
    macro_f1,
)
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.replay import RecordingClient, ReplayClient, load_cassettes
from receivables.core.ledger import InvoiceRecord, PaymentRecord, build_ledger
from receivables.core.verify import verify_extraction

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
RECORDED = ROOT / "tests" / "cassettes" / "t_llm_01.json"
SCRIPT = ROOT / "scripts" / "eval_classifier.py"
MESSAGES = labelled_messages(FIXTURES)
NAMES = fixture_customer_names(FIXTURES)
EXPECTED = {m.body: m.expected for m in MESSAGES}


def behind_redaction(inner: Any) -> RedactingClient:
    llm = load_config(ROOT / "config.yaml").llm  # the same switches as the manual script
    return RedactingClient(
        inner,
        redact_before_send=llm.redact_before_send,
        pseudonymize_customer_names=llm.pseudonymize_customer_names,
        customer_names=lambda: NAMES,
    )


def labeller(wrong: int = 0) -> FakeClient:
    """Answers each message with its expected label, except the first ``wrong`` ones."""
    order = [m.body for m in MESSAGES]
    redacted = behind_redaction(FakeClient())

    def answer(request: Mapping[str, Any]) -> dict[str, Any]:
        body = next(b for b in order if redacted.prepared_text(b) == request["text"])
        label = "UNCLEAR" if order.index(body) < wrong else EXPECTED[body]
        return {**CLASSIFICATION, "label": label}

    return FakeClient(classify=answer)


def test_the_15_messages_are_loaded() -> None:
    assert len(MESSAGES) == 15
    assert MESSAGES[0].message_id == "M-01" and MESSAGES[0].expected == "PO_AMOUNT_MISMATCH"
    assert "Beta Contracting LLC" in NAMES


def test_macro_f1_by_hand() -> None:
    pairs = [("A", "A"), ("A", "B"), ("B", "B")]
    assert macro_f1(pairs) == Fraction(2, 3)  # A: 2/3, B: 2/3
    assert macro_f1([("A", "B")]) == 0
    assert macro_f1([]) == 0


@pytest.mark.parametrize(
    ("wrong", "accuracy", "passes"),
    [(0, Fraction(1), True), (3, Fraction(12, 15), True), (4, Fraction(11, 15), False)],
)
def test_the_gate_is_accuracy_at_least_0_80(wrong: int, accuracy: Fraction, passes: bool) -> None:
    result = evaluate(behind_redaction(labeller(wrong)), MESSAGES)
    assert result.accuracy == accuracy
    assert result.passes_smoke_gate is passes
    assert Fraction(4, 5) == SMOKE_GATE_ACCURACY
    assert [r[0] for r in result.results] == [m.message_id for m in MESSAGES]


def test_failed_calls_count_as_unclear() -> None:
    result = evaluate(behind_redaction(FakeClient()), MESSAGES)  # nothing scripted
    assert {p for _, _, p in result.results} == {"UNCLEAR"}
    assert result.accuracy == Fraction(1, 15)  # M-14 is expected to be UNCLEAR


def test_a_recording_replays_to_the_same_result(tmp_path: Path) -> None:
    recorder = RecordingClient(labeller(wrong=2))
    live = evaluate(behind_redaction(recorder), MESSAGES)
    path = tmp_path / "t_llm_01.json"
    recorder.write(path, "t_llm_01")
    cassette = json.loads(path.read_text(encoding="utf-8"))
    assert cassette["synthetic"] is True and len(cassette["interactions"]) == 15
    assert {i["model"] for i in cassette["interactions"]} == {"fake"}  # a role, never an id
    text = path.read_text(encoding="utf-8")
    assert not [name for name in NAMES if name in text]  # recorded after redaction
    replayed = evaluate(behind_redaction(ReplayClient(path)), MESSAGES)
    assert replayed == live


def test_a_retried_request_keeps_the_response_that_was_used(tmp_path: Path) -> None:
    good = json.dumps({**CLASSIFICATION, "label": "PO_AMOUNT_MISMATCH"})
    recorder = RecordingClient(FakeClient(classify=["{not json", good]))
    client = behind_redaction(recorder)
    first = evaluate(client, MESSAGES[:1])
    path = tmp_path / "c.json"
    recorder.write(path, "retry")
    assert len(load_cassettes(path)) == 1  # one response per request, so it loads
    assert evaluate(behind_redaction(ReplayClient(path)), MESSAGES[:1]) == first


def test_llm_01_smoke_gate_on_the_recorded_run() -> None:
    # The maintainer's live run, committed (D-035); a missing file fails, it does not skip.
    # Re-record with `LLM_MODE=live uv run --env-file .env python scripts/eval_classifier.py
    # --record` whenever the prompt version changes.
    result = evaluate(behind_redaction(ReplayClient(RECORDED)), MESSAGES)
    misses = [r for r in result.results if r[1] != r[2]]
    assert result.passes_smoke_gate, f"accuracy {float(result.accuracy):.3f}; misses {misses}"


# --- the manual script --------------------------------------------------------------------------


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a fixed command: this interpreter and the script
        [sys.executable, str(SCRIPT), "--record"],
        env={**os.environ, **env},
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({}, "set LLM_MODE=live"),
        ({"LLM_MODE": "live", "ANTHROPIC_LOG": "debug"}, "unset ANTHROPIC_LOG"),
        ({"LLM_MODE": "live"}, "ANTHROPIC_API_KEY"),
    ],
)
def test_the_script_refuses_without_live_settings(env: dict[str, str], reason: str) -> None:
    existed = RECORDED.exists()
    done = _run(env)
    assert done.returncode == 2 and reason in done.stderr
    assert RECORDED.exists() is existed  # a refused run records nothing


def test_ci_never_runs_the_script() -> None:
    for workflow in (ROOT / ".github" / "workflows").glob("*.y*ml"):
        assert "eval_classifier" not in workflow.read_text(encoding="utf-8"), workflow.name


def test_the_recorded_m01_answer_now_verifies(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
) -> None:
    # The live run of 2026-10-15 answered M-01 correctly but wrote "47,000" as in the message;
    # the old schema refused it and M-01 fell back to UNCLEAR. Amounts as written (D-035):
    message = MESSAGES[0]
    client = behind_redaction(ReplayClient(RECORDED))
    outcome = classify_message(client, message.body, message.language)
    assert outcome.classification.label == "PO_AMOUNT_MISMATCH"
    assert (outcome.error, outcome.attempts) == (None, 1)
    assert [a.value for a in outcome.classification.amounts_mentioned] == ["47,000", "50,000"]
    invoices, payments = golden_ledger_inputs
    verification = verify_extraction(
        to_extraction(outcome.classification),
        message=message.body,
        sent_text=client.prepared_text(message.body),
        ledger=build_ledger(invoices, payments, date(2026, 10, 31)),
        message_customer_id="CUST-B",
        review_threshold=Fraction(3, 5),
    )
    assert verification.flags == ()
    assert verification.amount_difference == 300_000  # AED 3,000.00 in fils
    assert verification.needs_review is False
