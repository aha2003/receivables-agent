"""The customer playbook in the tick (SPEC §18.7; S4.05; D-052): recomputed at every tick, used
at once by the rule planner, saved and audited only when it changes, never at L0, with the kill
switch or from a future as_of. The golden synthetic company; no LLM."""

import dataclasses
import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import Config, load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.agent.orchestrator import RunSummary
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CLOCK = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))
AS_OF = date(2026, 10, 31)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'playbook.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def config(**l2: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    return dataclasses.replace(
        base, agent=dataclasses.replace(base.agent, l2=dataclasses.replace(base.agent.l2, **l2))
    )


def tick(engine: Engine, as_of: date = AS_OF, *, level: int = 1, cfg: Config | None = None
         ) -> RunSummary:  # fmt: skip
    return run_agent_tick(engine, CLOCK, cfg or config(), as_of=as_of, autonomy_level=level)


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def books(engine: Engine) -> dict[str, dict[str, Any]]:
    return {r["customer_id"]: r for r in rows(engine, "SELECT * FROM customer_playbook")}


def followups(engine: Engine, run_id: str) -> list[dict[str, Any]]:
    steps = rows(engine, "SELECT run_id, invoice_number, args_json, status, tool FROM agent_steps")
    return [s for s in steps if s["run_id"] == run_id and s["tool"] == "schedule_followup"]


def replies(engine: Engine, customer: str, delays: list[int]) -> None:
    """Outbound messages on 10-01, 10-10 and 10-19, each answered ``delay`` days later."""
    lines = ["message_id,direction,sent_at,invoice_number,customer_id,language,subject,body"]
    for n, (day, delay) in enumerate(zip((1, 10, 19), delays, strict=True)):
        lines.append(f"M-O{n},outbound,2026-10-{day:02d}T10:00:00+04:00,,{customer},en,Reminder,"
                     "Please pay.")  # fmt: skip
        lines.append(f"M-I{n},inbound,2026-10-{day + delay:02d}T11:00:00+04:00,,{customer},en,"
                     "Re: Reminder,We will check.")  # fmt: skip
    raw = ("\n".join(lines) + "\n").encode()
    assert import_csv(engine, "correspondence", raw, filename="m.csv", clock=CLOCK).status == (
        "accepted"
    )


def test_every_customer_gets_a_playbook_and_each_change_is_audited(db: Engine) -> None:
    tick(db)
    saved = books(db)
    assert set(saved) == {"CUST-A", "CUST-B", "CUST-C"}
    assert saved["CUST-C"]["promises_counted"] == 1  # the golden E-002, broken
    assert {b["followup_gap_days"] for b in saved.values()} == {7}
    audits = rows(db, "SELECT entity_id, before_json, after_json FROM audit_log"
                      " WHERE action = 'playbook.updated' ORDER BY entity_id")  # fmt: skip
    assert [a["entity_id"] for a in audits] == ["CUST-A", "CUST-B", "CUST-C"]
    assert all(a["before_json"] is None for a in audits)
    tick(db)  # nothing changed: nothing written
    assert len(rows(db, "SELECT * FROM audit_log WHERE action = 'playbook.updated'")) == 3
    assert books(db) == saved


@pytest.mark.spec("T-LRN-03")
def test_measured_replies_change_the_gap_and_the_same_ticks_followups(db: Engine) -> None:
    tick(db)
    replies(db, "CUST-A", [5, 5, 5])
    with db.begin() as c:  # the follow-ups are due, so the next tick plans the cases again
        c.execute(text("UPDATE cases SET next_followup = NULL"))
    summary = tick(db)
    a = books(db)["CUST-A"]
    assert (a["avg_days_to_reply"], a["replies_counted"], a["followup_gap_days"]) == (5.0, 3, 10)
    (audit,) = rows(db, "SELECT before_json, after_json FROM audit_log"
                        " WHERE action = 'playbook.updated' AND entity_id = 'CUST-A'"
                        " AND before_json IS NOT NULL")  # fmt: skip
    assert json.loads(audit["before_json"])["followup_gap_days"] == 7
    assert json.loads(audit["after_json"])["followup_gap_days"] == 10
    days = {f["invoice_number"]: json.loads(f["args_json"])["days"]
            for f in followups(db, summary.run_id)}  # fmt: skip
    assert days["INV-003"] == 10  # CUST-A: the playbook's gap, not the table's 7
    assert days["INV-004"] != 10  # CUST-B keeps its table value


@pytest.mark.parametrize(("level", "switch", "as_of"), [
    (0, False, AS_OF), (1, True, AS_OF), (1, False, date(2026, 11, 5)),
])  # fmt: skip
def test_nothing_is_saved_at_l0_with_the_kill_switch_or_from_a_future_as_of(
    db: Engine, level: int, switch: bool, as_of: date
) -> None:
    replies(db, "CUST-A", [5, 5, 5])
    summary = tick(db, as_of, level=level, cfg=config(kill_switch=switch))
    assert books(db) == {}
    assert rows(db, "SELECT * FROM audit_log WHERE action = 'playbook.updated'") == []
    if not switch:  # still computed and used in memory (the switch plans by rules, D-025)
        days = {f["invoice_number"]: json.loads(f["args_json"])["days"]
                for f in followups(db, summary.run_id)}  # fmt: skip
        assert days["INV-003"] == 10


def test_an_l0_shadow_plans_with_the_fresh_playbook_in_memory(db: Engine) -> None:
    replies(db, "CUST-A", [5, 5, 5])
    summary = tick(db, level=0)
    steps = followups(db, summary.run_id)
    days = {s["invoice_number"]: json.loads(s["args_json"])["days"] for s in steps}
    assert days["INV-003"] == 10 and {s["status"] for s in steps} == {"skipped"}
    assert books(db) == {}
