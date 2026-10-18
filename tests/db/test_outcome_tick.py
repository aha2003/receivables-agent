"""Outcome inference in the tick (SPEC §18.7, §18.2; S4.04; D-051): outcomes recorded once,
never at L0 or with the kill switch, never from a future as_of; a broken promise escalates its
pending case with one task. The golden synthetic company; no LLM."""

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
CLOCK = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))  # today is 2026-11-02


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'outcomes.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def config(**agent: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    return dataclasses.replace(base, agent=dataclasses.replace(base.agent, **agent))


def tick(engine: Engine, as_of: date, *, level: int = 1, cfg: Config | None = None) -> RunSummary:
    return run_agent_tick(engine, CLOCK, cfg or config(), as_of=as_of, autonomy_level=level)


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def add(engine: Engine, kind: str, csv_text: str) -> None:
    result = import_csv(engine, kind, csv_text.encode(), filename=f"{kind}.csv", clock=CLOCK)
    assert result.status == "accepted", result.errors


def promise(event_id: str, invoice: str, made: str, due: str) -> str:
    return (
        "event_id,invoice_number,event_type,event_date,label,promise_date\n"
        f"{event_id},{invoice},PROMISE_TO_PAY,{made},,{due}\n"
    )


def outcomes(engine: Engine) -> dict[str, str]:
    return {r["outcome_id"]: r["outcome_type"]
            for r in rows(engine, "SELECT outcome_id, outcome_type FROM outcomes")}  # fmt: skip


@pytest.mark.spec("T-OUT-01")
def test_the_golden_broken_promise_and_the_payments_are_recorded_once(db: Engine) -> None:
    tick(db, date(2026, 10, 31))
    recorded = outcomes(db)
    assert recorded["OUT-PRM-E-002"] == "PROMISE_BROKEN"  # INV-008, unpaid at 2026-10-31
    paid = rows(db, "SELECT payment_id FROM payments WHERE invoice_number IS NOT NULL"
                    " AND payment_type = 'payment'")  # fmt: skip
    assert {f"OUT-PAY-{p['payment_id']}" for p in paid} <= set(recorded)
    audits = rows(db, "SELECT entity_id, after_json FROM audit_log"
                      " WHERE action = 'outcome.recorded'")  # fmt: skip
    assert sorted(json.loads(a["after_json"])["outcome_id"] for a in audits) == sorted(recorded)
    tick(db, date(2026, 10, 31))
    assert outcomes(db) == recorded  # nothing recorded twice
    assert len(rows(db, "SELECT * FROM audit_log WHERE action = 'outcome.recorded'")) == len(
        recorded
    )


@pytest.mark.parametrize(("level", "switch"), [(0, False), (1, True), (2, True)])
def test_nothing_is_recorded_at_l0_or_with_the_kill_switch(
    db: Engine, level: int, switch: bool
) -> None:
    base = config()
    cfg = dataclasses.replace(
        base, agent=dataclasses.replace(
            base.agent, l2=dataclasses.replace(base.agent.l2, kill_switch=switch)),
    )  # fmt: skip
    tick(db, date(2026, 10, 31), level=level, cfg=cfg)
    assert outcomes(db) == {}


def test_nothing_is_recorded_from_a_future_as_of(db: Engine) -> None:
    tick(db, date(2026, 11, 5))  # a preview below L2 may look ahead (D-026); it records nothing
    assert outcomes(db) == {}
    tick(db, date(2026, 11, 2))  # today itself is fine
    assert "OUT-PRM-E-002" in outcomes(db)


def _states(db: Engine) -> dict[str, str]:
    return {r["invoice_number"]: r["state"]
            for r in rows(db, "SELECT invoice_number, state FROM cases")}  # fmt: skip


def _promise_tasks(db: Engine, invoice: str) -> list[dict[str, Any]]:
    return rows(db, "SELECT task_id, owner_role, text FROM tasks"
                    f" WHERE invoice_number = '{invoice}' AND text LIKE '%promise%'")  # fmt: skip


def test_a_promise_makes_the_case_pending_and_a_broken_one_escalates_it(db: Engine) -> None:
    add(db, "events", promise("E-T10", "INV-003", "2026-10-28", "2026-10-29"))
    tick(db, date(2026, 10, 29))
    assert _states(db)["INV-003"] == "PROMISE_PENDING"
    tick(db, date(2026, 10, 31))  # P + 2 = 2026-10-31: still PENDING
    assert _states(db)["INV-003"] == "PROMISE_PENDING" and _promise_tasks(db, "INV-003") == []
    tick(db, date(2026, 11, 1))  # past the grace, unpaid: BROKEN
    assert _states(db)["INV-003"] == "ESCALATED"
    (task,) = _promise_tasks(db, "INV-003")
    assert task["owner_role"] == "finance" and "2026-10-29" in task["text"]
    assert outcomes(db)["OUT-PRM-E-T10"] == "PROMISE_BROKEN"
    transitions = [json.loads(r["after_json"]) for r in rows(
        db, "SELECT after_json FROM audit_log WHERE action = 'case.transition'"
            " AND entity_id = 'INV-003' ORDER BY audit_id")]  # fmt: skip
    assert [t["trigger"] for t in transitions][-2:] == ["PromiseMade", "PromiseBroken"]
    tick(db, date(2026, 11, 2))
    assert len(_promise_tasks(db, "INV-003")) == 1  # one task per entry into ESCALATED


def test_a_kept_promise_resolves_and_never_escalates(db: Engine) -> None:
    add(db, "events", promise("E-T11", "INV-003", "2026-10-28", "2026-10-29"))
    tick(db, date(2026, 10, 29))
    amount = rows(db, "SELECT amount_fils FROM invoices WHERE invoice_number = 'INV-003'")
    aed = f"{amount[0]['amount_fils'] // 100}.{amount[0]['amount_fils'] % 100:02d}"
    add(db, "payments", "payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"
                        f"P-T11,INV-003,,2026-10-31,{aed},payment\n")  # fmt: skip
    tick(db, date(2026, 11, 2))
    assert _states(db)["INV-003"] == "RESOLVED"
    assert outcomes(db)["OUT-PRM-E-T11"] == "PROMISE_KEPT"
    assert outcomes(db)["OUT-PAY-P-T11"] == "PAYMENT_RECEIVED"
    assert _promise_tasks(db, "INV-003") == []


def test_a_promise_first_seen_after_its_date_makes_no_pending_case(db: Engine) -> None:
    add(db, "events", promise("E-T12", "INV-003", "2026-10-28", "2026-10-29"))
    tick(db, date(2026, 11, 1))
    assert _states(db)["INV-003"] != "PROMISE_PENDING"
    assert _promise_tasks(db, "INV-003") == []
    assert outcomes(db)["OUT-PRM-E-T12"] == "PROMISE_BROKEN"  # the outcome is still recorded


def test_a_broken_promise_escalates_a_case_the_tick_does_not_select(db: Engine) -> None:
    add(db, "events", promise("E-T13", "INV-003", "2026-10-28", "2026-10-29"))
    tick(db, date(2026, 10, 29))
    assert _states(db)["INV-003"] == "PROMISE_PENDING"
    one = config(max_cases_per_tick=1)  # INV-003 is not the top case
    summary = tick(db, date(2026, 11, 1), cfg=one)
    assert summary.cases_considered == 1
    steps = rows(db, f"SELECT invoice_number FROM agent_steps WHERE run_id = '{summary.run_id}'")  # noqa: S608
    assert "INV-003" not in {s["invoice_number"] for s in steps}
    assert _states(db)["INV-003"] == "ESCALATED" and len(_promise_tasks(db, "INV-003")) == 1


def test_a_resolved_dispute_is_recorded(db: Engine) -> None:
    add(db, "events", "event_id,invoice_number,event_type,event_date,label,promise_date\n"
                      "E-T14,INV-004,DISPUTE_RESOLVED,2026-10-30,,\n")  # fmt: skip
    tick(db, date(2026, 10, 31))
    assert outcomes(db)["OUT-DSP-E-T14"] == "DISPUTE_RESOLVED"
