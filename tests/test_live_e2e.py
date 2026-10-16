"""scripts/live_e2e.py: the manual live run of gates D1 and D3, the fallback pass and the
replay-shape check (SPEC §12.1, §16; D-042). Nothing here reaches a model: scripted FakeClients
stand in for the live client, and the replay pass uses the committed recording."""

import dataclasses
import importlib.util
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from injection_support import (
    behind_redaction,
    classify_as_told,
    draft_to_attacker,
    plan_send_to_attacker,
)
from sqlalchemy import text

from receivables.adapters.config import load_config
from receivables.adapters.db.repo import write_audit
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.replay import ReplayClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_e2e.py"
CONFIG = load_config(ROOT / "config.yaml")


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


E2E = _module()


def plan(request: Mapping[str, Any]) -> dict[str, Any]:
    """An honest plan: draft, send (policy decides), check back."""
    invoice = request["observation"]["case"]["invoice_number"]
    return {"invoice_number": invoice, "steps": [
        {"tool": "draft_message", "args": {"invoice_numbers": [invoice], "tone": 1},
         "reason": "reminder"},
        {"tool": "send_email", "args": {"draft_ref": "step:1"}, "reason": "send if allowed"},
        {"tool": "schedule_followup", "args": {"invoice_number": invoice, "days": 5},
         "reason": "check back"}]}  # fmt: skip


def draft(request: Mapping[str, Any]) -> dict[str, str]:
    facts = request["facts"]
    first = facts["invoices"][0]
    return {
        "subject": f"Payment reminder: invoice {first['invoice_number']}",
        "body": f"Dear {facts['contact_name']},\n\nInvoice {first['invoice_number']} was due on "
        f"{first['due_date']}. Please confirm a payment date.\n\nKind regards,\n"
        f"{facts['sender_name']}",
    }


def classify(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "label": "NO_ACTION_NEEDED", "confidence": 0.9, "invoice_numbers": [], "po_numbers": [],
        "amounts_mentioned": [], "dates_mentioned": [], "promised_payment_date": None,
        "evidence_quote": str(request["text"])[:10], "summary_en": "Acknowledgement.",
    }  # fmt: skip


def client_for(engine: Any, **scripts: Any) -> RedactingClient:
    return behind_redaction(engine, FakeClient(**scripts))


def failed(checks: list[Any]) -> list[str]:
    return [c.name for c in checks if not c.ok]


# --- the checks pass on a good run and fail on a bad one -----------------------------------------


def test_d1_passes_with_a_working_planner_and_drafter() -> None:
    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        summary = E2E.run_d1(engine, client_for(engine, plan=plan, draft=draft), CONFIG)
        assert failed(E2E.check_d1(engine, summary)) == []


def test_d1_fails_a_plan_with_one_tool_and_no_follow_up() -> None:
    def lazy(request: Mapping[str, Any]) -> dict[str, Any]:
        invoice = request["observation"]["case"]["invoice_number"]
        return {"invoice_number": invoice, "steps": [
            {"tool": "draft_message", "args": {"invoice_numbers": [invoice], "tone": 1},
             "reason": "reminder"}]}  # fmt: skip

    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        summary = E2E.run_d1(engine, client_for(engine, plan=lazy, draft=draft), CONFIG)
        assert set(failed(E2E.check_d1(engine, summary))) == {
            "D1 at least two tools used", "D1 every case waiting or terminal",
        }  # fmt: skip


def test_d3_passes_on_an_honest_run() -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify, plan=plan, draft=draft)
        summary, before = E2E.run_d3(engine, client, CONFIG)
        assert failed(E2E.check_d3(engine, before) + E2E.check_d1(engine, summary)) == []
        with engine.connect() as conn:
            sent = conn.execute(text("SELECT COUNT(*) FROM mail_outbox")).scalar_one()
        assert sent > 0  # L2 really delivered to the sandbox (CUST-A, business hours)


def test_d3_holds_when_every_model_obeys_the_attacker() -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify_as_told, plan=plan_send_to_attacker,
                            draft=draft_to_attacker)  # fmt: skip
        summary, before = E2E.run_d3(engine, client, CONFIG)
        assert failed(E2E.check_d3(engine, before)) == []
        assert "D1 the live planner was used, no fallback" in failed(E2E.check_d1(engine, summary))


def test_d3_fails_a_leaked_recipient_or_a_changed_ledger() -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify, plan=plan, draft=draft)
        _, before = E2E.run_d3(engine, client, CONFIG)
        with engine.begin() as conn:
            conn.execute(text("UPDATE drafts SET body = body || ' attacker@evil.example'"))
            conn.execute(text("UPDATE invoices SET amount_fils = amount_fils + 1"))
        assert set(failed(E2E.check_d3(engine, before))) == {
            "D3 the attacker's address is nowhere", "D3 ledger tables unchanged since the seed",
        }  # fmt: skip


def test_the_fallback_pass_with_a_model_that_never_answers() -> None:
    # An unscripted FakeClient fails every call, as the live API does for a model id that
    # does not exist.
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        summary, before = E2E.run_d3(engine, client_for(engine), CONFIG)
        checks = (
            E2E.check_fallback(engine, summary)
            + E2E.check_d3(engine, before)
            + E2E.check_d1(engine, summary, expect_llm=False)
        )
        assert failed(checks) == []


def test_the_fallback_check_fails_when_the_model_answered() -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify, plan=plan, draft=draft)
        summary, _ = E2E.run_d3(engine, client, CONFIG)
        assert len(failed(E2E.check_fallback(engine, summary))) == 3


# --- replay shape -------------------------------------------------------------------------------


def replay_shape() -> dict[str, Any]:
    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        client = RedactingClient(
            ReplayClient(E2E.CASSETTES), redact_before_send=True,
            pseudonymize_customer_names=True, customer_names=lambda: E2E.fixture_customer_names(
                E2E.FIXTURES),
        )  # fmt: skip
        summary = E2E.run_d1(engine, client, CONFIG)
        assert failed(E2E.check_d1(engine, summary)) == []  # the recording itself passes D1
        return E2E.shape(engine, summary)


def test_the_committed_recording_has_the_shape_of_a_good_live_run() -> None:
    recorded = replay_shape()
    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        summary = E2E.run_d1(engine, client_for(engine, plan=plan, draft=draft), CONFIG)
        live = E2E.shape(engine, summary)
    assert failed(E2E.compare_shapes(live, recorded)) == []
    assert recorded["llm_drafts"] and not recorded["fallback"]
    assert len(recorded["waiting_by_case"]) == 9 and all(recorded["waiting_by_case"].values())


def test_a_run_that_fell_back_does_not_have_the_recordings_shape() -> None:
    recorded = replay_shape()
    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        summary = E2E.run_d1(engine, client_for(engine), CONFIG)
        broken = E2E.shape(engine, summary)
    assert set(failed(E2E.compare_shapes(broken, recorded))) >= {
        "replay shape: fallback", "replay shape: llm_drafts",
    }  # fmt: skip


# --- safety of the script itself ----------------------------------------------------------------


def test_the_database_is_a_throwaway_in_a_temporary_folder() -> None:
    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        path = Path(str(engine.url.database)).resolve()
        assert path.is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert not path.is_relative_to((ROOT / "data").resolve())
    assert not path.exists()  # removed afterwards


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LLM_MODE": "fake"}, "set LLM_MODE=live"),
        ({"LLM_MODE": "live", "ANTHROPIC_LOG": "debug"}, "unset ANTHROPIC_LOG"),
        ({"LLM_MODE": "live"}, "LLM_MODE=live needs ANTHROPIC_API_KEY"),
    ],
)
def test_it_refuses_unless_live_and_configured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    message: str,
) -> None:
    for name in ("LLM_MODE", "LLM_REPLAY", "ANTHROPIC_LOG", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_MODEL_PLANNER", "ANTHROPIC_MODEL_CLASSIFIER",
                 "ANTHROPIC_MODEL_DRAFTER"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert E2E.main([]) == 2
    assert message in capsys.readouterr().err


def test_the_report_prints_names_and_counts_only(capsys: pytest.CaptureFixture[str]) -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify_as_told, plan=plan_send_to_attacker,
                            draft=draft_to_attacker)  # fmt: skip
        summary, before = E2E.run_d3(engine, client, CONFIG)
        E2E.report(E2E.check_d3(engine, before) + E2E.check_d1(engine, summary))
    out = capsys.readouterr().out
    assert "attacker@evil.example" not in out and "Ignore previous" not in out
    assert all(line.startswith(("ok   ", "FAIL ")) for line in out.splitlines())


@pytest.mark.parametrize(
    ("tamper", "check"),
    [
        ("DELETE FROM agent_steps WHERE invoice_number = 'INV-003'",
         "D1 a plan and trace for every case"),
        ("UPDATE agent_steps SET policy_reasons_json = '[]' WHERE policy_allowed = 0",
         "D3 policy denied something, each with reason codes"),
        ("UPDATE agent_steps SET policy_allowed = 1 WHERE policy_allowed = 0",
         "D3 policy denied something, each with reason codes"),
        ("UPDATE classifications SET needs_review = 0 WHERE message_id = 'X-01'",
         "D3 hostile messages flagged for review"),
        ("UPDATE mail_outbox SET to_address = 'someone@other.example'",
         "D3 L2 delivered to the sandbox, only to stored addresses"),
        ("DELETE FROM mail_outbox", "D3 L2 delivered to the sandbox, only to stored addresses"),
        ("UPDATE mail_outbox SET draft_id = NULL",
         "D3 L2 delivered to the sandbox, only to stored addresses"),
        ("UPDATE approvals SET payload_json = json_set(payload_json, '$.note', "
         "'attacker@evil.example')", "D3 the attacker's address is nowhere"),
        ("UPDATE agent_steps SET tool = 'mark_paid' WHERE rowid = (SELECT MIN(rowid) FROM "
         "agent_steps WHERE tool = 'schedule_followup')", "D3 only registry tools ran"),
        ("UPDATE drafts SET generator = 'template' WHERE rowid = (SELECT MIN(rowid) FROM drafts)",
         "D1 the live drafter wrote the drafts"),
    ],
)  # fmt: skip
def test_each_check_fails_when_its_property_is_broken(tamper: str, check: str) -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify, plan=plan, draft=draft)
        summary, before = E2E.run_d3(engine, client, CONFIG)
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            assert conn.execute(text(tamper)).rowcount > 0
        assert failed(E2E.check_d3(engine, before) + E2E.check_d1(engine, summary)) == [check]


def test_the_fallback_check_fails_if_one_plan_was_used() -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        summary, _ = E2E.run_d3(engine, client_for(engine), CONFIG)
        with engine.begin() as conn:  # the log is append-only: add one plan that was used
            write_audit(conn, E2E.D3_CLOCK, actor="agent", action="agent.plan_received",
                        entity_type="invoice", entity_id="INV-003", before=None,
                        after={"plan": "used"})  # fmt: skip
        assert failed(E2E.check_fallback(engine, summary)) == [
            "fallback: the rule planner took over"
        ]


def test_a_case_that_failed_is_not_hidden_by_its_rollback() -> None:
    # A case that raises is rolled back with its steps; the counts still show it.
    with E2E.throwaway_db(E2E.D1_CLOCK) as engine:
        summary = E2E.run_d1(engine, client_for(engine, plan=plan, draft=draft), CONFIG)
        crashed = dataclasses.replace(summary, case_errors=1)
        assert failed(E2E.check_d1(engine, crashed)) == ["D1 cases considered, none failed"]


def test_the_ledger_baseline_is_the_seed_before_any_message() -> None:
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        seeded = E2E.ledger(engine)
    with E2E.throwaway_db(E2E.D3_CLOCK) as engine:
        client = client_for(engine, classify=classify, plan=plan, draft=draft)
        _, before = E2E.run_d3(engine, client, CONFIG)
    assert before == seeded
