"""The simulator's arms A0 to A3 and day-60 metrics (SPEC §19.2; S4.03; D-049, D-050).

One full run of seed 42 is shared by the module (a run takes about 13 s); T-SIM-01 makes a
second. No test calls the Anthropic API: the simulation has no model at all.
"""

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import load_config
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.models import (
    AgentRun,
    AgentStep,
    Approval,
    AuditLog,
    Classification,
    Correspondence,
    Draft,
    Event,
    MailOutbox,
    Payment,
    Task,
)
from receivables.cli import main
from receivables.seed import seed
from receivables.sim.behaviour import behaviours_for
from receivables.sim.company import generate_company
from receivables.sim.runner import ARMS, SimRun, run_arms
from receivables.sim.scenario import Archetype, load_scenario
from receivables.sim.simulator import LABEL

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(ROOT / "config.yaml")
SCENARIO = load_scenario()
COMPANY = generate_company(SCENARIO)
END = SCENARIO.start_date + timedelta(days=SCENARIO.days)
WORKING = {"Mon", "Tue", "Wed", "Thu", "Fri"}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _local(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(DUBAI)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _real_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        seed(engine, ROOT / "fixtures", FixedClock(datetime(2026, 10, 31, 9, tzinfo=DUBAI)))
    finally:
        engine.dispose()
    return path


REAL_SHA: dict[Path, str] = {}  # the real database's hash before any simulation ran


@pytest.fixture(scope="module")
def real_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = _real_db(tmp_path_factory.mktemp("app") / "demo.db")
    REAL_SHA[path] = _sha(path)
    return path


@pytest.fixture(scope="module")
def sim(tmp_path_factory: pytest.TempPathFactory, real_db: Path) -> SimRun:
    return run_arms(SCENARIO, tmp_path_factory.mktemp("sim"), f"sqlite:///{real_db}", CONFIG)


@pytest.fixture(scope="module")
def engines(sim: SimRun):
    made = {arm: make_engine(f"sqlite:///{path}") for arm, path in sim.databases.items()}
    yield made
    for engine in made.values():
        engine.dispose()


def _all(engine: Engine, statement: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(statement).all())


def _fils(text: str) -> int:
    return int(Decimal(text) * 100)


# Reproducibility and the ordering -------------------------------------------------------------


@pytest.mark.spec("T-SIM-01")
def test_seed_42_twice_gives_identical_results_json(
    sim: SimRun, tmp_path: Path, real_db: Path
) -> None:
    again = run_arms(SCENARIO, tmp_path / "again", f"sqlite:///{real_db}", CONFIG)
    assert again.results_path.name == sim.results_path.name == "sim_42_results.json"
    assert _sha(again.results_path) == _sha(sim.results_path)
    assert json.loads(sim.results_path.read_text(encoding="utf-8")) == sim.results


@pytest.mark.spec("T-SIM-02")
def test_sanity_ordering(sim: SimRun) -> None:
    arms = sim.results["arms"]
    overdue = {arm: _fils(arms[arm]["overdue_amount"]) for arm in ARMS}
    late = {arm: Decimal(arms[arm]["mean_days_past_due_at_payment"]) for arm in ARMS}
    assert overdue["A3"] <= overdue["A1"] and overdue["A2"] <= overdue["A1"]
    assert late["A1"] < late["A0"]


def test_the_results_are_labelled_and_complete(sim: SimRun) -> None:
    r = sim.results
    assert r["label"] == LABEL == "SIMULATION — MECHANICS, NOT EVIDENCE"
    assert (r["scenario"], r["seed"], r["start_date"], r["metrics_as_of"]) == (
        "demo_company", 42, "2026-11-02", "2027-01-01",
    )  # fmt: skip
    assert r["company_sha256"] == COMPANY.sha256()
    assert set(r["arms"]) == set(ARMS)
    keys = {"overdue_amount", "mean_days_past_due_at_payment", "invoices_settled",
            "mean_dispute_cycle_days", "disputes_resolved", "disputes_open", "messages_sent",
            "approvals_needed", "policy_denials"}  # fmt: skip
    assert all(set(m) == keys for m in r["arms"].values())
    assert "planner" in r["assumptions"] and "classifier" in r["assumptions"]
    text = sim.results_path.read_text(encoding="utf-8")
    assert "RUN-" not in text and "DRF-" not in text  # no run or draft ids: nothing random


def test_the_overdue_amount_is_the_ledgers(sim: SimRun, engines: dict[str, Engine]) -> None:
    from receivables.adapters.db.repo import load_invoices, load_payments
    from receivables.core.ledger import build_ledger

    for arm in ARMS:
        with engines[arm].connect() as conn:
            rows = build_ledger(load_invoices(conn), load_payments(conn), END)
        overdue = sum(r.open_amount for r in rows if r.open_amount and (r.days_overdue or 0) > 0)
        assert _fils(sim.results["arms"][arm]["overdue_amount"]) == overdue


# T-SIM-03: every outbound message passes the draft validator and the policy engine -----------


@pytest.mark.spec("T-SIM-03")
def test_every_message_passed_the_validator_and_the_policy_engine(
    sim: SimRun, engines: dict[str, Engine]
) -> None:
    for arm in ARMS:
        engine = engines[arm]
        manual = _all(engine, select(Draft).where(Draft.status == "sent_manually"))
        checks = defaultdict(list)
        for row in _all(engine, select(AuditLog).where(AuditLog.action == "sim.policy_checked")):
            checks[row.entity_id].append(json.loads(row.after_json))
        for draft in manual:  # a person's message: validated, and the policy's verdict recorded
            assert draft.generator == "template" and json.loads(draft.violations_json) == []
            (check,) = checks[draft.draft_id]
            assert check["violations"] == [] and check["allowed"] is (check["reasons"] == [])
        sends = {
            json.loads(s.result_json)["draft_id"]: s
            for s in _all(engine, select(AgentStep).where(AgentStep.tool == "send_email",
                                                          AgentStep.status == "executed"))
        }  # fmt: skip
        mails = _all(engine, select(MailOutbox))
        for mail in mails:  # an L2 send leaves only when the policy allowed it
            step = sends[mail.draft_id]
            assert step.policy_allowed == 1 and json.loads(step.policy_reasons_json) == []
            assert mail.gateway == "sandbox"
        assert len(manual) + len(mails) == sim.results["arms"][arm]["messages_sent"]
        assert _all(engine, select(Correspondence).where(Correspondence.direction == "outbound"))\
            == []  # fmt: skip
        if arm in ("A0", "A1", "A2"):
            assert mails == []  # nothing is sent by the system below L2
    assert sim.results["arms"]["A0"]["messages_sent"] == 0


def test_a_policy_denial_never_leaves_the_machine(engines: dict[str, Engine]) -> None:
    for arm in ("A2", "A3"):
        denied = _all(engines[arm], select(AgentStep).where(
            AgentStep.tool == "send_email", AgentStep.policy_allowed == 0))  # fmt: skip
        assert denied, arm
        assert {s.status for s in denied} == {"converted_to_approval"}
        approvals = {a.step_id for a in _all(engines[arm], select(Approval))}
        assert {s.step_id for s in denied} <= approvals


# The simulated human ---------------------------------------------------------------------------


def test_the_human_sends_on_a_later_working_day_at_0930(engines: dict[str, Engine]) -> None:
    for arm in ("A1", "A2", "A3"):
        sent = _all(engines[arm], select(Draft).where(Draft.status == "sent_manually"))
        assert sent, arm
        for draft in sent:
            created, at = _local(draft.created_at), _local(draft.sent_at)
            assert at.date() > created.date()
            assert at.time() == time(9, 30) and _WEEKDAYS[at.weekday()] in WORKING
            assert draft.approved_by == "sim-human"


def test_the_human_never_messages_an_invoice_twice_within_7_days(
    engines: dict[str, Engine],
) -> None:
    for arm in ("A1", "A2", "A3"):
        engine = engines[arm]
        days: dict[str, list[date]] = defaultdict(list)
        for draft in _all(engine, select(Draft).where(Draft.status == "sent_manually")):
            for n in json.loads(draft.invoice_numbers_json):
                days[n].append(_local(draft.sent_at).date())
        for created_at, numbers in _all(engine, select(
                MailOutbox.created_at, Draft.invoice_numbers_json).join(
                Draft, Draft.draft_id == MailOutbox.draft_id)):  # fmt: skip
            for n in json.loads(numbers):
                days[n].append(_local(created_at).date())
        for n, sent in days.items():
            sent.sort()
            assert all((b - a).days >= 7 for a, b in pairwise(sent)), (arm, n)


def test_replies_are_accepted_with_their_true_label_a_working_day_later(
    engines: dict[str, Engine],
) -> None:
    for arm in ("A2", "A3"):
        engine = engines[arm]
        rows = _all(engine, select(Classification))
        assert rows
        for c in rows:
            assert c.model == "fake"  # no model: the reply's text only, never its label
            assert c.status == "accepted"
            assert c.label in {"PO_AMOUNT_MISMATCH", "MISSING_DELIVERY_NOTE", "MISSING_PO",
                               "ALREADY_PAID"}  # fmt: skip
        accepted = _all(engine, select(AuditLog).where(AuditLog.action ==
                                                       "classification.accepted"))  # fmt: skip
        assert {a.actor for a in accepted} == {"sim-human"} and len(accepted) == len(rows)
        sent = {m.message_id: _local(m.sent_at) for m in _all(engine, select(Correspondence))}
        for a in accepted:
            at = _local(a.ts)
            assert at.date() > sent[a.entity_id].date() and at.time() == time(9, 30)
            assert _WEEKDAYS[at.weekday()] in WORKING
    for arm in ("A0", "A1"):  # nobody reads the inbox
        assert _all(engines[arm], select(Classification)) == []
        assert _all(engines[arm], select(Event)) == []


def _working_days_after(first: date, last: date) -> int:
    return sum(
        _WEEKDAYS[(first + timedelta(days=n)).weekday()] in WORKING
        for n in range(1, (last - first).days + 1)
    )


def test_disputes_are_resolved_3_working_days_after_the_document_request(
    engines: dict[str, Engine],
) -> None:
    for arm in ("A2", "A3"):
        engine = engines[arm]
        requested: dict[str, date] = {}
        for invoice, as_of in _all(engine, select(AgentStep.invoice_number, AgentRun.as_of)
                                .join(AgentRun, AgentRun.run_id == AgentStep.run_id)
                                .where(AgentStep.tool == "request_internal_document",
                                       AgentStep.status == "executed")
                                .order_by(AgentRun.as_of)):  # fmt: skip
            requested.setdefault(invoice, date.fromisoformat(as_of))
        resolved = _all(engine, select(Event).where(Event.event_type == "DISPUTE_RESOLVED"))
        assert resolved
        for event in resolved:
            assert (
                _working_days_after(
                    requested[event.invoice_number], date.fromisoformat(event.event_date)
                )
                == 3
            )


def test_claimed_invoices_are_closed_by_the_customers_own_cash(
    engines: dict[str, Engine],
) -> None:
    claimed = {c.customer_id: min((i for i in COMPANY.invoices if i.customer_id == c.customer_id),
                                  key=lambda i: (i.due_date, i.invoice_number)).invoice_number
               for c in COMPANY.customers if c.archetype is Archetype.CLAIMS_PAID}  # fmt: skip
    for arm in ("A2", "A3"):
        cash = {p.payment_id: p.invoice_number
                for p in _all(engines[arm], select(Payment).where(
                    Payment.payment_id.like("SIM-P%")))}  # fmt: skip
        assert sorted(cash.values()) == sorted(claimed.values()), arm
    for arm in ("A0", "A1"):  # nobody allocates the cash
        cash = _all(engines[arm], select(Payment.invoice_number).where(
            Payment.payment_id.like("SIM-P%")))  # fmt: skip
        assert {c.invoice_number for c in cash} == {None}


# The arms ----------------------------------------------------------------------------------------


def test_a0_does_nothing(engines: dict[str, Engine], sim: SimRun) -> None:
    engine = engines["A0"]
    for table in (Draft, Approval, AgentRun, Task, MailOutbox):
        assert _all(engine, select(table)) == [], table.__tablename__
    # Every customer follows its no-contact rule: overdue at day 60 is what those rules leave.
    behaviours = behaviours_for(COMPANY)
    paid = {}
    day = SCENARIO.start_date
    while day <= END:
        for b in behaviours.values():
            paid |= {a.invoice_number: a for a in b.actions_on(day) if hasattr(a, "payment_id")}
        day += timedelta(days=1)
    open_ = sum(i.amount_fils for i in COMPANY.invoices if i.invoice_number not in paid
                and i.due_date < END)  # fmt: skip
    assert _fils(sim.results["arms"]["A0"]["overdue_amount"]) == open_


def test_a1_only_sends_rules_reminders(engines: dict[str, Engine]) -> None:
    engine = engines["A1"]
    for table in (AgentRun, Approval, Task, Event, MailOutbox):
        assert _all(engine, select(table)) == [], table.__tablename__
    drafts = _all(engine, select(Draft))
    assert drafts and {d.generator for d in drafts} == {"template"}
    assert all(len(json.loads(d.invoice_numbers_json)) == 1 for d in drafts)
    created = {a.entity_id: a.actor for a in _all(engine, select(AuditLog).where(
        AuditLog.action == "draft.created"))}  # fmt: skip
    assert set(created.values()) == {"sim-rules"}


def test_a2_is_the_agent_at_l1_and_a3_at_l2_with_rules_and_templates(
    engines: dict[str, Engine],
) -> None:
    for arm, level in (("A2", 1), ("A3", 2)):
        runs = _all(engines[arm], select(AgentRun))
        assert {r.autonomy_level for r in runs} == {level}
        assert {r.planner for r in runs} == {"rules"} and {r.model for r in runs} == {None}
        days = {date.fromisoformat(r.as_of) for r in runs}
        assert all(_WEEKDAYS[d.weekday()] in WORKING for d in days)
        assert len(days) == len(runs) == 45  # every working day from 2026-11-02 to 2027-01-01
        assert {d.generator for d in _all(engines[arm], select(Draft))} == {"template"}
    assert _all(engines["A3"], select(MailOutbox))  # some L2 sends were allowed


def test_the_real_database_is_untouched(sim: SimRun, real_db: Path) -> None:
    assert _sha(real_db) == REAL_SHA[real_db]  # after two full runs against it
    files = sim.results_path.parent.iterdir()  # -wal/-shm: other tests hold connections open
    assert sorted(p.name for p in files if not p.name.endswith(("-wal", "-shm"))) == [
        "sim_42_a0.db", "sim_42_a1.db", "sim_42_a2.db", "sim_42_a3.db", "sim_42_results.json",
    ]  # fmt: skip


def test_cli_sim_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = _real_db(tmp_path / "app" / "demo.db")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{real}")
    assert main(["sim-run", "--seed", "7", "--data-dir", str(tmp_path / "sim")]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC DEMO DATA" in out and LABEL in out
    assert all(f"{arm}: overdue AED" in out for arm in ARMS)
    assert str(tmp_path / "sim" / "sim_7_results.json") in out
