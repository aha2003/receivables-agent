"""scripts/record_demo.py records classifications, plans and drafts in one file that replay
mode then serves for the demo run (D-037). Nothing here reaches a model: a scripted FakeClient
stands in for the live client; the last test replays the maintainer's committed recording."""

import importlib.util
import json
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from llm_capture import CLASSIFICATION
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.drafter import make_draft_writer
from receivables.adapters.llm.evaluation import labelled_messages
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.planner import make_llm_planner
from receivables.adapters.llm.redacting import RedactingClient
from receivables.adapters.llm.replay import ReplayClient, load_cassettes
from receivables.seed import seed

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
SCRIPT = ROOT / "scripts" / "record_demo.py"
RECORDED = ROOT / "tests" / "cassettes" / "live_demo.json"
CONFIG = load_config(ROOT / "config.yaml")
CLOCK = FixedClock(datetime(2026, 10, 31, 10, 0, tzinfo=DUBAI))
EXPECTED = {m.body: m.expected for m in labelled_messages(FIXTURES)}


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def classify(request: Mapping[str, Any]) -> dict[str, Any]:
    # The bodies hold no customer names, so the redacted text is the body itself.
    return {**CLASSIFICATION, "label": EXPECTED.get(request["text"], "UNCLEAR")}


def plan(request: Mapping[str, Any]) -> dict[str, Any]:
    invoice = request["observation"]["case"]["invoice_number"]
    return {"invoice_number": invoice, "steps": [
        {"tool": "draft_message", "args": {"invoice_numbers": [invoice], "tone": 2},
         "reason": "firm reminder"},
        {"tool": "schedule_followup", "args": {"invoice_number": invoice, "days": 5},
         "reason": "check back"}]}  # fmt: skip


def draft(request: Mapping[str, Any]) -> dict[str, str]:
    facts = request["facts"]
    first = facts["invoices"][0]
    return {
        "subject": f"Payment reminder: invoice {first['invoice_number']}",
        "body": f"Dear {facts['contact_name']},\n\nInvoice {first['invoice_number']} was due on "
        f"{first['due_date']}. Please arrange payment this week.\n\nKind regards,\n"
        f"{facts['sender_name']}",
    }


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'replay.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def replay_run(db: Engine, cassette: Path, clock: FixedClock = CLOCK) -> dict[str, Any]:
    """The demo's first run after a reset, as of 2026-10-31, served only from ``cassette``."""
    client = RedactingClient(
        ReplayClient(cassette),
        redact_before_send=CONFIG.llm.redact_before_send,
        pseudonymize_customer_names=CONFIG.llm.pseudonymize_customer_names,
        customer_names=customer_names(db),
    )
    summary = run_agent_tick(
        db,
        clock,
        CONFIG,
        as_of=date(2026, 10, 31),
        planner=make_llm_planner(client),
        drafter=make_draft_writer(client),
    )
    with db.connect() as c:
        plans = [
            json.loads(r)["plan"]
            for r in c.execute(
                text("SELECT after_json FROM audit_log WHERE action = 'agent.plan_received'")
            ).scalars()
        ]
        generators = list(c.execute(text("SELECT generator FROM drafts")).scalars())
    return {"summary": summary, "plans": plans, "generators": generators}


def test_one_recording_holds_all_three_kinds_and_replays_the_demo_run(
    tmp_path: Path, db: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    fake = FakeClient(classify=classify, plan=plan, draft=draft)

    def make(settings: object, config: object, *, customer_names: Any) -> RedactingClient:
        return RedactingClient(
            fake,
            redact_before_send=CONFIG.llm.redact_before_send,
            pseudonymize_customer_names=CONFIG.llm.pseudonymize_customer_names,
            customer_names=customer_names,
        )

    cassette = tmp_path / "live_demo.json"
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setattr(module, "make_llm_client", make)
    monkeypatch.setattr(module, "configure_logging", lambda names: None)
    monkeypatch.setattr(module, "CASSETTE", cassette)
    assert module.main(["--record"]) == 0
    out = capsys.readouterr().out
    assert "accuracy: 1.000" in out and "fallback False" in out
    assert "{'used': 9}" in out and "{'llm': 9}" in out  # all plans used, all drafts the model's

    recorded = json.loads(cassette.read_text(encoding="utf-8"))
    kinds = [i["kind"] for i in recorded["interactions"]]
    assert {k: kinds.count(k) for k in set(kinds)} == {"classify": 15, "plan": 9, "draft": 9}
    assert {i["model"] for i in recorded["interactions"]} == {"fake"}
    assert recorded["synthetic"] is True and len(load_cassettes(cassette)) == 33
    text_seen = cassette.read_text(encoding="utf-8")
    for name in ("Alpha Trading LLC", "Beta Contracting LLC", "Gamma Facilities Services LLC"):
        assert name not in text_seen  # recorded after redaction

    replayed = replay_run(db, cassette)
    assert replayed["summary"].fallback is False
    assert replayed["plans"] == ["used"] * 9
    assert replayed["generators"] == ["llm"] * 9


def test_the_replay_matches_on_any_presentation_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recorded on one day, presented on another: the requests depend on as_of, not on the
    # server's clock, so the run as of 2026-10-31 is still served from the recording.
    module = _module()
    cassette = tmp_path / "live_demo.json"
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setattr(
        module,
        "make_llm_client",
        lambda s, c, *, customer_names: RedactingClient(
            FakeClient(classify=classify, plan=plan, draft=draft),
            redact_before_send=CONFIG.llm.redact_before_send,
            pseudonymize_customer_names=CONFIG.llm.pseudonymize_customer_names,
            customer_names=customer_names,
        ),
    )
    monkeypatch.setattr(module, "configure_logging", lambda names: None)
    monkeypatch.setattr(module, "CASSETTE", cassette)
    assert module.main(["--record"]) == 0
    later = FixedClock(datetime(2026, 12, 7, 15, 30, tzinfo=DUBAI))
    url = f"sqlite:///{tmp_path / 'later.db'}"  # reset (seeded) on the presentation day too
    upgrade_to_head(url)
    presented = make_engine(url)
    try:
        seed(presented, FIXTURES, later)
        replayed = replay_run(presented, cassette, later)
    finally:
        presented.dispose()
    assert replayed["plans"] == ["used"] * 9 and replayed["generators"] == ["llm"] * 9


def test_without_record_nothing_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    cassette = tmp_path / "live_demo.json"
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setattr(
        module,
        "make_llm_client",
        lambda s, c, *, customer_names: RedactingClient(
            FakeClient(classify=classify, plan=plan, draft=draft),
            redact_before_send=True,
            pseudonymize_customer_names=True,
            customer_names=customer_names,
        ),
    )
    monkeypatch.setattr(module, "configure_logging", lambda names: None)
    monkeypatch.setattr(module, "CASSETTE", cassette)
    assert module.main([]) == 0
    assert not cassette.exists()


def _recorded_kinds() -> set[str]:
    return {i["kind"] for i in json.loads(RECORDED.read_text(encoding="utf-8"))["interactions"]}


@pytest.mark.skipif(
    _recorded_kinds() >= {"plan", "draft"},
    reason="the recording already holds plans and drafts",
)
def test_the_recording_awaits_its_plans_and_drafts() -> None:
    # Until the maintainer records the combined run, live_demo.json holds the S3.06 classify
    # answers only. This passes now and turns into the replay test below once recorded.
    assert _recorded_kinds() == {"classify"}


@pytest.mark.skipif(
    not _recorded_kinds() >= {"plan", "draft"},
    reason="plans and drafts not recorded yet: run "
    "`LLM_MODE=live uv run --env-file .env python scripts/record_demo.py --record`",
)
def test_the_committed_recording_serves_the_demo_run(db: Engine) -> None:
    replayed = replay_run(db, RECORDED)
    assert replayed["plans"] and "failed" not in replayed["plans"]  # every call was recorded
    assert replayed["generators"]
