"""LLM drafts in a real agent tick (SPEC §8.2, §8.3, §18.9; D-036).

The drafter is a FakeClient behind the app's redaction; nothing reaches a model. Golden data,
L1 at 2026-10-31.
"""

import json
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.drafter import make_draft_writer
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ROOT = FIXTURES.parent
CLOCK = FixedClock(datetime(2026, 10, 31, 10, 0, tzinfo=DUBAI))
CONFIG = load_config(ROOT / "config.yaml")


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'drafts.db'}"
    upgrade_to_head(url)
    eng = make_engine(url)
    seed(eng, FIXTURES, CLOCK)
    yield eng
    eng.dispose()


def good_draft(request: Mapping[str, Any]) -> dict[str, str]:
    """A draft that uses only the facts, and the name placeholder the drafter receives."""
    facts = request["facts"]
    first = facts["invoices"][0]
    return {
        "subject": f"Payment reminder: invoice {first['invoice_number']}",
        "body": f"Dear {facts['contact_name']},\n\nInvoice {first['invoice_number']} for "
        f"{facts['customer_name']} was due on {first['due_date']}. Please confirm a payment "
        f"date.\n\nKind regards,\n{facts['sender_name']}",
    }


def drafter(engine: Engine, script: Any) -> tuple[FakeClient, Any]:
    fake = FakeClient(draft=script)
    client = RedactingClient(
        fake,
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=customer_names(engine),
    )
    return fake, make_draft_writer(client)


def drafts(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as c:
        rows = c.execute(text("SELECT draft_id, subject, body, generator, violations_json"
                              " FROM drafts ORDER BY draft_id"))  # fmt: skip
        return [dict(r._mapping) for r in rows]


def draft_steps(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT result_json FROM agent_steps WHERE tool = 'draft_message'")
        ).scalars()
        return [json.loads(r) for r in rows]


def test_a_real_run_saves_llm_drafts_with_the_name_restored(engine: Engine) -> None:
    fake, write = drafter(engine, good_draft)
    run_agent_tick(engine, CLOCK, CONFIG, as_of=date(2026, 10, 31), drafter=write)
    saved = drafts(engine)
    assert saved and {d["generator"] for d in saved} == {"llm"}
    assert all(d["violations_json"] == "[]" for d in saved)
    assert any("for Beta Contracting LLC was due" in d["body"] for d in saved)
    assert not any("[CUSTOMER]" in d["body"] for d in saved)
    sent = json.dumps([payload for _, payload in fake.calls])
    assert fake.calls and "[CUSTOMER]" in sent
    for name in ("Alpha Trading LLC", "Beta Contracting LLC", "Gamma Facilities Services LLC"):
        assert name not in sent  # the drafter never saw a customer name (D-031)
    assert all(
        step["generator"] == "llm" and step["attempts"] == [] for step in draft_steps(engine)
    )


def test_a_violating_drafter_falls_back_per_draft(engine: Engine) -> None:
    def bad(_: Mapping[str, Any]) -> dict[str, str]:
        return {"subject": "Reminder", "body": "Pay INV-999 now or we go to court."}

    fake, write = drafter(engine, bad)
    run_agent_tick(engine, CLOCK, CONFIG, as_of=date(2026, 10, 31), drafter=write)
    saved = drafts(engine)
    assert {d["generator"] for d in saved} == {"template"}
    assert all(d["violations_json"] == "[]" for d in saved)
    steps = draft_steps(engine)
    assert all(step["attempts"] == [["FORBIDDEN_PHRASE", "UNKNOWN_INVOICE"]] * 3 for step in steps)
    assert len(fake.calls) == 3 * len(saved)  # max_regenerations is 2


def test_a_dry_run_never_calls_the_drafter(engine: Engine) -> None:
    fake, write = drafter(engine, good_draft)
    summary = run_agent_tick(
        engine, CLOCK, CONFIG, as_of=date(2026, 10, 31), drafter=write, dry_run=True
    )
    assert summary.steps_executed > 0
    assert fake.calls == []
    assert drafts(engine) == []  # rolled back


def test_without_a_drafter_the_run_is_unchanged(engine: Engine) -> None:
    run_agent_tick(engine, CLOCK, CONFIG, as_of=date(2026, 10, 31))
    assert {d["generator"] for d in drafts(engine)} == {"template"}
    assert all(step["attempts"] == [] for step in draft_steps(engine))
