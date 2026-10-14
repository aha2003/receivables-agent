"""Policy audit and kill switch (SPEC §18.5, §3.3): T-POL-09 end to end, admin rule, T-AUD-01.

The golden synthetic company at as_of 2026-10-31; now 2026-11-02T10:00 Dubai (Monday).
"""

import dataclasses
import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import Config, load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.controls import (
    REAUTH_WINDOW,
    AdminActionRefused,
    kill_switch_on,
    runtime_kill_switch,
    set_kill_switch,
)
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import UserRecord, write_audit
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
AS_OF = date(2026, 10, 31)
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI)
CLOCK = FixedClock(NOW)
ADMIN = UserRecord(user_id=1, username="admin.user", password_hash="x", role="admin")
USER = UserRecord(user_id=2, username="demo.user", password_hash="x", role="user")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'ks.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def config(**l2: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    l2 = {"allowlisted_customers": ("CUST-A",), **l2}
    return dataclasses.replace(
        base, agent=dataclasses.replace(base.agent, l2=dataclasses.replace(base.agent.l2, **l2))
    )


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def count(engine: Engine, table: str) -> int:
    return rows(engine, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]  # noqa: S608


def tick(engine: Engine, cfg: Config | None = None, level: int = 2) -> Any:
    return run_agent_tick(engine, CLOCK, cfg or config(), as_of=AS_OF, autonomy_level=level)


def assert_nothing_executed(engine: Engine) -> None:
    steps = rows(engine, "SELECT * FROM agent_steps")
    assert steps, "the run still records its planned steps"
    assert {s["status"] for s in steps} == {"denied"}
    assert {s["policy_reasons_json"] for s in steps} == {'["POLICY_KILL_SWITCH"]'}
    assert {s["policy_allowed"] for s in steps} == {0}
    for table in ("cases", "drafts", "tasks", "approvals", "mail_outbox"):
        assert count(engine, table) == 0, table


# --- T-POL-09 end to end ------------------------------------------------------------------------


def test_t_pol_09_kill_switch_in_config_stops_everything(db: Engine) -> None:
    summary = tick(db, config(kill_switch=True))
    assert_nothing_executed(db)
    assert (summary.steps_executed, summary.steps_denied) == (0, count(db, "agent_steps"))


def test_t_pol_09_kill_switch_set_by_an_admin_stops_everything(db: Engine) -> None:
    set_kill_switch(db, CLOCK, ADMIN, on=True, reauthenticated_at=NOW - timedelta(minutes=1))
    tick(db)
    assert_nothing_executed(db)
    with db.connect() as c:
        assert kill_switch_on(c, config()) is True


def test_turning_the_switch_off_lets_the_agent_work_again(db: Engine) -> None:
    reauth = NOW - timedelta(minutes=1)
    set_kill_switch(db, CLOCK, ADMIN, on=True, reauthenticated_at=reauth)
    set_kill_switch(db, CLOCK, ADMIN, on=False, reauthenticated_at=reauth)
    tick(db)
    assert count(db, "mail_outbox") == 1  # INV-003 to CUST-A (allowlisted, tone 1)
    with db.connect() as c:
        assert runtime_kill_switch(c) is False


def test_the_config_switch_cannot_be_overridden_at_runtime(db: Engine) -> None:
    set_kill_switch(db, CLOCK, ADMIN, on=False, reauthenticated_at=NOW)
    with db.connect() as c:
        assert kill_switch_on(c, config(kill_switch=True)) is True


def test_the_kill_switch_beats_shadow_mode(db: Engine) -> None:
    tick(db, config(kill_switch=True), level=0)
    assert_nothing_executed(db)


# --- §3.3 admin rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "reauth", "code"),
    [
        (USER, NOW, "NOT_ADMIN"),
        (ADMIN, None, "REAUTH_REQUIRED"),
        (ADMIN, NOW - REAUTH_WINDOW - timedelta(seconds=1), "REAUTH_REQUIRED"),
        (ADMIN, NOW + timedelta(seconds=1), "REAUTH_INVALID"),
        (ADMIN, datetime(2026, 11, 2, 10, 0), "REAUTH_INVALID"),  # no time zone
    ],
)
def test_only_a_recently_reauthenticated_admin_may_switch(
    db: Engine, user: UserRecord, reauth: datetime | None, code: str
) -> None:
    with pytest.raises(AdminActionRefused) as refused:
        set_kill_switch(db, CLOCK, user, on=True, reauthenticated_at=reauth)
    assert refused.value.code == code
    with db.connect() as c:
        assert runtime_kill_switch(c) is False
    audit = rows(db, "SELECT * FROM audit_log WHERE action = 'kill_switch.refused'")
    assert len(audit) == 1
    assert (audit[0]["actor"], json.loads(audit[0]["after_json"])) == (
        user.username,
        {"requested_on": True, "reason": code},
    )


def test_reauthentication_exactly_five_minutes_ago_is_accepted(db: Engine) -> None:
    assert set_kill_switch(db, CLOCK, ADMIN, on=True, reauthenticated_at=NOW - REAUTH_WINDOW)
    with db.connect() as c:
        assert runtime_kill_switch(c) is True


# --- T-AUD-01 (the parts built so far) ----------------------------------------------------------


def _complete(row: dict[str, Any]) -> bool:
    return all(row[k] for k in ("actor", "action", "entity_type", "entity_id", "after_json")) and (
        row["before_json"] is not None
    )


def test_t_aud_01_kill_switch_changes_have_actor_entity_before_and_after(db: Engine) -> None:
    reauth = NOW - timedelta(minutes=2)
    set_kill_switch(db, CLOCK, ADMIN, on=True, reauthenticated_at=reauth)
    set_kill_switch(db, CLOCK, ADMIN, on=False, reauthenticated_at=reauth)
    changes = rows(db, "SELECT * FROM audit_log WHERE action = 'kill_switch.set' ORDER BY audit_id")
    assert all(_complete(r) for r in changes)
    assert [(json.loads(r["before_json"]), json.loads(r["after_json"])) for r in changes] == [
        ({"on": False}, {"on": True}),
        ({"on": True}, {"on": False}),
    ]
    assert {(r["actor"], r["entity_type"], r["entity_id"]) for r in changes} == {
        ("admin.user", "control", "kill_switch")
    }


def test_t_aud_01_every_step_executed_or_denied_is_audited(db: Engine) -> None:
    tick(db, config(allowlisted_customers=("CUST-A",)))
    steps = rows(db, "SELECT * FROM agent_steps")
    audits = {
        r["entity_id"]: r for r in rows(db, "SELECT * FROM audit_log WHERE action = 'agent.step'")
    }
    assert set(audits) == {s["step_id"] for s in steps}
    for s in steps:
        audit = audits[s["step_id"]]
        assert _complete(audit) and audit["actor"] == "agent"
        after = json.loads(audit["after_json"])
        assert (after["tool"], after["status"], after["invoice_number"]) == (
            s["tool"],
            s["status"],
            s["invoice_number"],
        )
        assert after["policy_reasons"] == json.loads(s["policy_reasons_json"])
        assert after["policy_allowed"] == (
            None if s["policy_allowed"] is None else bool(s["policy_allowed"])
        )
    statuses = {json.loads(a["after_json"])["status"] for a in audits.values()}
    assert statuses == {"executed", "converted_to_approval"}  # INV-003 sent, INV-012 denied
    denied = next(
        a for a in audits.values() if json.loads(a["after_json"])["policy_allowed"] is False
    )
    assert json.loads(denied["after_json"])["policy_reasons"] == ["POLICY_NOT_ALLOWLISTED"]


def test_step_audits_show_the_case_state_around_the_step(db: Engine) -> None:
    tick(db)
    escalate = next(
        r
        for r in rows(db, "SELECT * FROM audit_log WHERE action = 'agent.step'")
        if json.loads(r["after_json"])["tool"] == "escalate"
    )
    assert json.loads(escalate["before_json"]) == {"case_state": "ASSESSED"}
    assert json.loads(escalate["after_json"])["case_state"] == "ESCALATED"


def test_step_audits_hold_no_free_text(db: Engine) -> None:
    tick(db)
    audits = rows(db, "SELECT after_json, before_json FROM audit_log WHERE action = 'agent.step'")
    text_seen = json.dumps(audits)
    for fragment in ("Dear", "@", "Alpha Trading", "Escalated:", "Provide the", "reminder"):
        assert fragment not in text_seen


def test_denied_steps_under_the_kill_switch_are_audited(db: Engine) -> None:
    tick(db, config(kill_switch=True))
    audits = rows(db, "SELECT after_json FROM audit_log WHERE action = 'agent.step'")
    assert audits and all(
        json.loads(a["after_json"])["policy_reasons"] == ["POLICY_KILL_SWITCH"] for a in audits
    )
    assert count(db, "agent_steps") == len(audits)


# --- spec-review regressions --------------------------------------------------------------------


class RecordingPlanner:
    model = "recording"

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, observation: Any) -> object:
        self.calls += 1
        return {}


def test_existing_cases_are_untouched_under_the_switch(db: Engine) -> None:
    tick(db, config(allowlisted_customers=("CUST-A",)))  # builds cases, drafts, a sandbox send
    before = {t: rows(db, f"SELECT * FROM {t}") for t in ("cases", "drafts", "tasks", "approvals")}  # noqa: S608
    audits_before = count(db, "audit_log")
    with db.begin() as c:  # INV-005 is paid and a dispute is resolved: events to apply
        c.execute(
            text(
                "INSERT INTO payments (payment_id, invoice_number, payment_date, amount_fils)"
                " VALUES ('P-X', 'INV-005', '2026-10-30', 2000000)"
            )
        )
        c.execute(
            text(
                "INSERT INTO events (event_id, invoice_number, event_type, event_date)"
                " VALUES ('E-X', 'INV-004', 'DISPUTE_RESOLVED', '2026-10-30')"
            )
        )
    planner = RecordingPlanner()
    run_agent_tick(
        db, CLOCK, config(kill_switch=True), as_of=AS_OF, autonomy_level=2, planner=planner
    )
    after = {t: rows(db, f"SELECT * FROM {t}") for t in ("cases", "drafts", "tasks", "approvals")}  # noqa: S608
    assert after == before
    assert count(db, "mail_outbox") == 1  # only the send from the first tick
    new_actions = {
        r["action"]
        for r in rows(db, f"SELECT action FROM audit_log WHERE audit_id > {audits_before}")  # noqa: S608
    }
    assert new_actions == {"agent.run_started", "agent.step", "agent.run_finished"}
    assert planner.calls == 0  # no planner (LLM) call while the switch is on


def test_step_audits_under_the_switch_claim_no_case_state(db: Engine) -> None:
    tick(db, config(kill_switch=True))
    audits = rows(db, "SELECT before_json, after_json FROM audit_log WHERE action = 'agent.step'")
    assert {a["before_json"] for a in audits} == {'{"case_state": null}'}
    assert {json.loads(a["after_json"])["case_state"] for a in audits} == {None}


def test_a_refused_off_leaves_the_switch_on(db: Engine) -> None:
    set_kill_switch(db, CLOCK, ADMIN, on=True, reauthenticated_at=NOW)
    with pytest.raises(AdminActionRefused):
        set_kill_switch(db, CLOCK, USER, on=False, reauthenticated_at=NOW)
    with pytest.raises(AdminActionRefused):
        set_kill_switch(db, CLOCK, ADMIN, on=False, reauthenticated_at=None)
    with db.connect() as c:
        assert runtime_kill_switch(c) is True


def test_only_the_kill_switch_entity_sets_the_switch(db: Engine) -> None:
    with db.begin() as c:
        write_audit(
            c,
            CLOCK,
            actor="x",
            action="kill_switch.set",
            entity_type="invoice",
            entity_id="INV-003",
            before=None,
            after={"on": True},
        )
        assert runtime_kill_switch(c) is False
