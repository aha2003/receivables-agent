"""Escalation and follow-up scheduling in the loop (SPEC §18.2, §18.6; S4.06; D-053).

T-CASE-01 in simulation: one golden invoice walked through real ticks on a simulated clock.
The §18.2 no-reply escalation, the follow-up gate, and one task per entry into ESCALATED. The
golden synthetic company; no LLM.
"""

import dataclasses
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock, SimulatedClock
from receivables.adapters.config import Config, load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.drafts import approve_draft, mark_draft_sent
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.agent.orchestrator import RunSummary
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SEEDED = FixedClock(datetime(2026, 10, 1, 9, 0, tzinfo=DUBAI))
EVENTS = "event_id,invoice_number,event_type,event_date,label,promise_date\n"
PAYMENTS = "payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'loop.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, SEEDED)
    yield engine
    engine.dispose()


def config(**l2: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    return dataclasses.replace(
        base, agent=dataclasses.replace(base.agent, l2=dataclasses.replace(base.agent.l2, **l2))
    )


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def add(engine: Engine, clock: Any, kind: str, csv_text: str) -> None:
    result = import_csv(engine, kind, csv_text.encode(), filename=f"{kind}.csv", clock=clock)
    assert result.status == "accepted", result.errors


def state(engine: Engine, invoice: str) -> str:
    found = rows(engine, "SELECT state FROM cases WHERE invoice_number = :n", n=invoice)
    return found[0]["state"] if found else "none"


def transitions(engine: Engine, invoice: str) -> list[tuple[str, str]]:
    found = rows(engine, "SELECT after_json FROM audit_log WHERE action = 'case.transition'"
                         " AND entity_id = :n ORDER BY audit_id", n=invoice)  # fmt: skip
    return [(json.loads(r["after_json"])["state"], json.loads(r["after_json"])["trigger"])
            for r in found]  # fmt: skip


def escalation_tasks(engine: Engine, invoice: str) -> list[dict[str, Any]]:
    return rows(engine, "SELECT task_id, owner_role, text FROM tasks WHERE invoice_number = :n"
                        " AND text LIKE 'Escalated%'", n=invoice)  # fmt: skip


@pytest.mark.spec("T-CASE-01")
def test_t_case_01_one_case_walked_through_simulated_days(db: Engine) -> None:
    clock = SimulatedClock(datetime(2026, 10, 16, 10, 0, tzinfo=DUBAI))
    n = "INV-012"  # CUST-C, AED 9,000.00, due 2026-10-15

    def tick() -> RunSummary:
        return run_agent_tick(db, clock, config(), autonomy_level=1)  # as_of: the clock's date

    def at(day: date, hour: int = 10) -> None:
        clock.set(datetime(day.year, day.month, day.day, hour, tzinfo=DUBAI))

    tick()  # 1 day overdue: the case is created and assessed; a reminder is drafted
    assert state(db, n) == "ASSESSED"
    at(date(2026, 10, 17), 9)  # a person approves the reminder and sends it
    (draft,) = rows(db, "SELECT draft_id FROM drafts WHERE invoice_numbers_json LIKE '%INV-012%'")
    approve_draft(db, draft["draft_id"], clock=clock, actor="person")
    mark_draft_sent(db, draft["draft_id"], clock.now(), clock=clock, actor="person")
    assert state(db, n) == "AWAITING_REPLY"
    at(date(2026, 10, 20))
    add(db, clock, "events", EVENTS + "E-S1,INV-012,DISPUTE_OPENED,2026-10-20,MISSING_PO,\n")
    tick()
    at(date(2026, 10, 24))
    add(db, clock, "events", EVENTS + "E-S2,INV-012,DISPUTE_RESOLVED,2026-10-24,,\n")
    tick()
    at(date(2026, 10, 26))
    add(db, clock, "events", EVENTS + "E-S3,INV-012,PROMISE_TO_PAY,2026-10-26,,2026-10-28\n")
    tick()
    at(date(2026, 10, 31))  # past 2026-10-28 + 2 days, unpaid: the promise is broken
    tick()
    assert len(escalation_tasks(db, n)) == 1
    at(date(2026, 11, 2))
    payment = "P-S1,INV-012,,2026-11-02,9000.00,payment\n"
    add(db, clock, "payments", PAYMENTS + payment)
    tick()
    assert [s for s, _ in transitions(db, n)] == [
        "NEW", "ASSESSED", "AWAITING_REPLY", "DISPUTE_OPEN", "ASSESSED", "PROMISE_PENDING",
        "ESCALATED", "RESOLVED",
    ]  # fmt: skip
    assert len(escalation_tasks(db, n)) == 1  # one task for the one entry into ESCALATED


def _awaiting(db: Engine, invoice: str, sent: list[str], replies: list[str]) -> None:
    """A case left AWAITING_REPLY, with outbound messages about it and the customer's replies."""
    with db.begin() as c:
        insert = ("INSERT INTO cases (invoice_number, state, updated_at)"
                  " VALUES (:n, 'AWAITING_REPLY', '2026-10-01T06:00:00Z')")  # fmt: skip
        c.execute(text(insert), {"n": invoice})
    lines = ["message_id,direction,sent_at,invoice_number,customer_id,language,subject,body"]
    lines += [f"M-O{i},outbound,{t},{invoice},,en,Reminder,Please pay." for i, t in enumerate(sent)]
    lines += [f"M-I{i},inbound,{t},,CUST-B,en,Re: Reminder,Looking into it."
              for i, t in enumerate(replies)]  # fmt: skip
    add(db, SEEDED, "correspondence", "\n".join(lines) + "\n")


def no_reply_escalations(db: Engine, invoice: str) -> list[tuple[str, str]]:
    return [t for t in transitions(db, invoice) if t[1] == "NoReply"]


def run(db: Engine, as_of: date, *, level: int = 1, cfg: Config | None = None) -> RunSummary:
    clock = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))
    return run_agent_tick(db, clock, cfg or config(), as_of=as_of, autonomy_level=level)


TWO_SENT = ["2026-10-01T10:00:00+04:00", "2026-10-15T10:00:00+04:00"]


def test_two_unanswered_messages_over_60_days_overdue_escalate(db: Engine) -> None:
    _awaiting(db, "INV-011", TWO_SENT, [])  # CUST-B, due 2026-08-30: 62 days at 2026-10-31
    run(db, date(2026, 10, 31))
    assert no_reply_escalations(db, "INV-011") == [("ESCALATED", "NoReply")]
    (task,) = escalation_tasks(db, "INV-011")
    assert task["owner_role"] == "finance" and "no reply to 2 messages" in task["text"]
    created = rows(db, "SELECT after_json FROM audit_log WHERE action = 'task.created'")
    assert [json.loads(c["after_json"])["reason"] for c in created] == ["NO_REPLY"]


@pytest.mark.parametrize(
    ("sent", "replies", "as_of"),
    [
        (TWO_SENT, ["2026-10-10T09:00:00+04:00"], date(2026, 10, 31)),  # a reply in between
        (TWO_SENT[1:], [], date(2026, 10, 31)),  # only one message
        (TWO_SENT, [], date(2026, 10, 29)),  # exactly 60 days overdue
        ([TWO_SENT[0], "2026-11-01T10:00:00+04:00"], [], date(2026, 10, 31)),  # one after as_of
    ],
)
def test_no_escalation_without_two_unanswered_messages_over_60_days(
    db: Engine, sent: list[str], replies: list[str], as_of: date
) -> None:
    _awaiting(db, "INV-011", sent, replies)
    run(db, as_of)
    assert no_reply_escalations(db, "INV-011") == []


@pytest.mark.parametrize(("level", "switch", "as_of"), [
    (0, False, date(2026, 10, 31)), (1, True, date(2026, 10, 31)), (1, False, date(2026, 11, 5)),
])  # fmt: skip
def test_no_reply_escalation_never_at_l0_with_the_switch_or_from_a_future_as_of(
    db: Engine, level: int, switch: bool, as_of: date
) -> None:
    _awaiting(db, "INV-011", TWO_SENT, [])
    run(db, as_of, level=level, cfg=config(kill_switch=switch))
    assert no_reply_escalations(db, "INV-011") == []
    assert [t for t in escalation_tasks(db, "INV-011") if "no reply" in t["text"]] == []


def test_a_case_waits_for_its_follow_up_date(db: Engine) -> None:
    first = run(db, date(2026, 10, 31))
    assert first.cases_considered == 9 and first.waiting == ()
    followups = {r["invoice_number"]: r["next_followup"]
                 for r in rows(db, "SELECT invoice_number, next_followup FROM cases")}  # fmt: skip
    assert followups["INV-003"] == "2026-11-07"  # 7 days, the table value without a playbook
    second = run(db, date(2026, 10, 31))
    assert second.cases_considered == 0 and set(second.waiting) == set(followups)
    assert rows(db, "SELECT * FROM agent_steps WHERE run_id = :r", r=second.run_id) == []
    (finished,) = rows(db, "SELECT after_json FROM audit_log WHERE action = 'agent.run_finished'"
                           " AND entity_id = :r", r=second.run_id)  # fmt: skip
    assert set(json.loads(finished["after_json"])["waiting"]) == set(followups)
    due = run(db, date(2026, 11, 7))  # INV-003 and INV-012 are due again
    assert {"INV-003", "INV-012"}.isdisjoint(due.waiting)
    planned = {r["invoice_number"] for r in rows(
        db, "SELECT invoice_number FROM agent_steps WHERE run_id = :r", r=due.run_id)}  # fmt: skip
    assert {"INV-003", "INV-012"} <= planned


def test_a_new_event_lifts_the_wait(db: Engine) -> None:
    run(db, date(2026, 10, 31))
    add(db, SEEDED, "events", EVENTS + "E-W1,INV-003,DISPUTE_OPENED,2026-10-31,MISSING_PO,\n")
    again = run(db, date(2026, 10, 31))
    assert "INV-003" not in again.waiting and state(db, "INV-003") == "DISPUTE_OPEN"
    assert again.cases_considered == 1


def test_follow_ups_use_the_playbook_gap(db: Engine) -> None:
    lines = ["message_id,direction,sent_at,invoice_number,customer_id,language,subject,body"]
    for i, day in enumerate((1, 10, 19)):
        lines.append(f"M-O{i},outbound,2026-10-{day:02d}T10:00:00+04:00,,CUST-A,en,R,Please pay.")
        lines.append(f"M-I{i},inbound,2026-10-{day + 5:02d}T11:00:00+04:00,,CUST-A,en,R,Noted.")
    add(db, SEEDED, "correspondence", "\n".join(lines) + "\n")
    run(db, date(2026, 10, 31))  # CUST-A replies in 5 days: gap 10
    followups = {r["invoice_number"]: r["next_followup"]
                 for r in rows(db, "SELECT invoice_number, next_followup FROM cases")}  # fmt: skip
    assert followups["INV-003"] == (date(2026, 10, 31) + timedelta(days=10)).isoformat()
    assert followups["INV-012"] == "2026-11-07"  # CUST-C: no measured replies, the table's 7


def test_every_entry_into_escalated_creates_one_task(db: Engine) -> None:
    run(db, date(2026, 10, 31))  # the rule planner escalates INV-009, INV-011 and INV-014
    for n in ("INV-009", "INV-011", "INV-014"):
        assert state(db, n) == "ESCALATED" and len(escalation_tasks(db, n)) == 1
    with db.begin() as c:
        c.execute(text("UPDATE cases SET next_followup = NULL"))
    run(db, date(2026, 10, 31))  # planned again: escalate on an escalated case adds no task
    for n in ("INV-009", "INV-011", "INV-014"):
        assert len(escalation_tasks(db, n)) == 1


def test_61_days_overdue_is_enough(db: Engine) -> None:
    _awaiting(db, "INV-011", TWO_SENT, [])
    run(db, date(2026, 10, 30))  # INV-011 is due 2026-08-30: 61 days
    assert no_reply_escalations(db, "INV-011") == [("ESCALATED", "NoReply")]


def test_messages_before_a_dispute_or_promise_do_not_count(db: Engine) -> None:
    _awaiting(db, "INV-011", TWO_SENT, [])
    add(db, SEEDED, "events", EVENTS + "E-N1,INV-011,DISPUTE_RESOLVED,2026-10-10,,\n")
    run(db, date(2026, 10, 31))  # only the 2026-10-15 message came after the event
    assert no_reply_escalations(db, "INV-011") == []


def test_a_future_dated_run_saves_no_follow_up_dates(db: Engine) -> None:
    preview = run(db, date(2026, 11, 20))  # the real date is 2026-11-02
    assert preview.cases_considered > 0
    assert rows(db, "SELECT * FROM cases WHERE next_followup IS NOT NULL") == []
    results = [json.loads(r["result_json"]) for r in rows(
        db, "SELECT result_json FROM agent_steps WHERE tool = 'schedule_followup'")]  # fmt: skip
    assert results and all(r["saved"] is False for r in results)
    current = run(db, date(2026, 11, 2))  # nothing holds the cases back
    assert current.waiting == () and current.cases_considered > 0


def test_a_new_promise_lifts_the_wait(db: Engine) -> None:
    run(db, date(2026, 10, 31))
    add(db, SEEDED, "events", EVENTS + "E-W2,INV-003,PROMISE_TO_PAY,2026-10-31,,2026-11-05\n")
    again = run(db, date(2026, 10, 31))
    assert "INV-003" not in again.waiting and state(db, "INV-003") == "PROMISE_PENDING"


def test_waiting_cases_take_no_place_in_the_run(db: Engine) -> None:
    three = dataclasses.replace(config(), agent=dataclasses.replace(
        config().agent, max_cases_per_tick=3))  # fmt: skip
    first = run(db, date(2026, 10, 31), cfg=three)
    second = run(db, date(2026, 10, 31), cfg=three)  # the top 3 now wait: the next 3 are planned
    assert len(first.waiting) == 0 and set(second.waiting) >= set(_planned(db, first.run_id))
    assert len(_planned(db, second.run_id)) == 3
    assert _planned(db, second.run_id).isdisjoint(_planned(db, first.run_id))


def _planned(db: Engine, run_id: str) -> set[str]:
    return {r["invoice_number"] for r in rows(
        db, "SELECT invoice_number FROM agent_steps WHERE run_id = :r", r=run_id)}  # fmt: skip
