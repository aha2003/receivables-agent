"""Synthetic evaluation mode (SPEC §19.3; S4.07; D-054): T-SHD-02, an L0 run has no side effect.

The golden synthetic company, left "dirty" first by an L1 run so that an L0 run would have
plenty to change if it could: a promise past its grace on a pending case, measured customer
replies (a playbook that would change), unanswered messages, follow-ups due. No LLM.
"""

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, inspect, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CLOCK = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))
CONFIG = load_config(Path("/nonexistent/config.yaml"))
TRACE_TABLES = {"agent_runs", "agent_steps", "audit_log"}
TRACE_ACTIONS = {"agent.run_started", "agent.step", "agent.run_finished"}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'l0.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def add(engine: Engine, kind: str, csv_text: str) -> None:
    result = import_csv(engine, kind, csv_text.encode(), filename=f"{kind}.csv", clock=CLOCK)
    assert result.status == "accepted", result.errors


def snapshot(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    names = sorted(inspect(engine).get_table_names())
    with engine.connect() as c:
        return {t: sorted(map(tuple, c.execute(text(f'SELECT * FROM "{t}"')).all()))  # noqa: S608
                for t in names}  # fmt: skip


def dirty(engine: Engine) -> None:
    add(engine, "events", "event_id,invoice_number,event_type,event_date,label,promise_date\n"
                          "E-L1,INV-003,PROMISE_TO_PAY,2026-10-28,,2026-10-29\n")  # fmt: skip
    run_agent_tick(engine, CLOCK, CONFIG, as_of=date(2026, 10, 29), autonomy_level=1)
    lines = ["message_id,direction,sent_at,invoice_number,customer_id,language,subject,body"]
    for i, day in enumerate((1, 10, 19)):
        lines.append(f"M-O{i},outbound,2026-10-{day:02d}T10:00:00+04:00,,CUST-A,en,R,Please pay.")
        lines.append(f"M-I{i},inbound,2026-10-{day + 5:02d}T11:00:00+04:00,,CUST-A,en,R,Noted.")
    lines += ["M-O7,outbound,2026-10-01T10:00:00+04:00,INV-011,,en,R,Please pay.",
              "M-O8,outbound,2026-10-15T10:00:00+04:00,INV-011,,en,R,Please pay."]  # fmt: skip
    add(engine, "correspondence", "\n".join(lines) + "\n")
    with engine.begin() as c:  # every follow-up is due, so the L0 run plans every case
        c.execute(text("UPDATE cases SET next_followup = NULL"))
        c.execute(
            text("UPDATE cases SET state = 'AWAITING_REPLY' WHERE invoice_number = 'INV-011'")
        )


@pytest.mark.spec("T-SHD-02")
def test_an_l0_run_adds_only_run_and_step_trace_rows(db: Engine) -> None:
    dirty(db)
    before = snapshot(db)
    audits_before = len(before["audit_log"])
    summary = run_agent_tick(db, CLOCK, CONFIG, as_of=date(2026, 11, 2), autonomy_level=0)
    after = snapshot(db)
    assert summary.cases_considered > 0 and summary.waiting == ()
    changed = {t for t in after if after[t] != before[t]}
    assert changed <= TRACE_TABLES  # no draft, message, mail, task, approval, case, outcome,
    # playbook, follow-up date, invoice, payment, event or import changed
    with db.connect() as c:
        steps = c.execute(text("SELECT status FROM agent_steps WHERE run_id = :r"),
                          {"r": summary.run_id}).scalars().all()  # fmt: skip
        actions = c.execute(text("SELECT action FROM audit_log ORDER BY audit_id")).scalars().all()
        level = c.execute(text("SELECT autonomy_level FROM agent_runs WHERE run_id = :r"),
                          {"r": summary.run_id}).scalar_one()  # fmt: skip
    assert steps and set(steps) == {"skipped"} and level == 0
    assert set(actions[audits_before:]) <= TRACE_ACTIONS
    runs_before = {r[0] for r in before["agent_runs"]}
    assert {r[0] for r in after["agent_runs"]} - runs_before == {summary.run_id}


def test_the_same_run_at_l1_would_have_changed_things(db: Engine) -> None:
    dirty(db)  # proves the scenario above is not trivially empty
    before = snapshot(db)
    run_agent_tick(db, CLOCK, CONFIG, as_of=date(2026, 11, 2), autonomy_level=1)
    after = snapshot(db)
    changed = {t for t in after if after[t] != before[t]}
    assert {"cases", "drafts", "tasks", "outcomes", "customer_playbook"} <= changed
