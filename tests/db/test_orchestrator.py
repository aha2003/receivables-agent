"""Orchestrator tick and trace (SPEC §18.6): T-AGT-01, 02, 04, 05, 07 and autonomy levels.

The golden synthetic company at as_of 2026-10-31; now_local 2026-11-02T10:00 Dubai (Monday, in
business hours). Planners here are scripted test doubles behind the PlanSource port; no LLM.
"""

import dataclasses
import json
import logging
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import Config, load_config
from receivables.adapters.db.agent_store import DbAgentStore, run_agent_tick, tick_config
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import load_agent_facts
from receivables.agent.orchestrator import PlannerError, RunSummary, run_tick
from receivables.agent.rules_planner import plan_case
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
AS_OF = date(2026, 10, 31)
CLOCK = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))
T_AGT_08 = {
    "INV-004": ["request_internal_document", "draft_message", "schedule_followup"],
    "INV-011": ["draft_message", "escalate", "schedule_followup"],
    "INV-009": ["draft_message", "escalate", "schedule_followup"],
    "INV-014": ["draft_message", "escalate", "schedule_followup"],
    "INV-005": ["draft_message", "schedule_followup"],
    "INV-013": ["draft_message", "schedule_followup"],
    "INV-008": ["draft_message", "schedule_followup"],
    "INV-003": ["draft_message", "send_email", "schedule_followup"],
    "INV-012": ["draft_message", "send_email", "schedule_followup"],
}


def make_db(path: Path) -> Engine:
    url = f"sqlite:///{path}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    return engine


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    engine = make_db(tmp_path / "agent.db")
    yield engine
    engine.dispose()


def config(**l2: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    return dataclasses.replace(
        base, agent=dataclasses.replace(base.agent, l2=dataclasses.replace(base.agent.l2, **l2))
    )


class ScriptedPlanner:
    """Returns a fixed raw plan per invoice (or raises); records the observations it saw."""

    model = "scripted-test-planner"

    def __init__(self, plans: Mapping[str, object] | None = None, default: object = None) -> None:
        self.plans = dict(plans or {})
        self.default = default
        self.observations: list[Mapping[str, Any]] = []

    def propose(self, observation: Mapping[str, Any]) -> object:
        self.observations.append(observation)
        invoice = observation["case"]["invoice_number"]
        plan = self.plans.get(invoice, self.default)
        if isinstance(plan, Exception):
            raise plan
        if callable(plan):
            return plan(invoice)
        return plan


def tick(
    engine: Engine, *, level: int = 1, planner: Any = None, cfg: Config | None = None
) -> RunSummary:
    return run_agent_tick(
        engine, CLOCK, cfg or config(), as_of=AS_OF, autonomy_level=level, planner=planner
    )


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def steps(engine: Engine) -> list[dict[str, Any]]:
    return rows(engine, "SELECT * FROM agent_steps ORDER BY seq")


def tools_by_invoice(engine: Engine) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for s in steps(engine):
        found.setdefault(s["invoice_number"], []).append(s["tool"])
    return found


def audit_actions(engine: Engine) -> list[str]:
    return [r["action"] for r in rows(engine, "SELECT action FROM audit_log ORDER BY audit_id")]


# --- the rule planner at L1 --------------------------------------------------------------------


def test_rule_planner_tick_at_l1(db: Engine) -> None:
    summary = tick(db)
    assert (summary.planner, summary.fallback, summary.model) == ("rules", False, None)
    assert summary.cases_considered == 9 and summary.case_errors == 0
    assert tools_by_invoice(db) == T_AGT_08
    trace = steps(db)
    assert [s["seq"] for s in trace] == list(range(1, len(trace) + 1))
    # L1: every send becomes an approval; internal steps execute.
    sends = [s for s in trace if s["tool"] == "send_email"]
    assert {s["status"] for s in sends} == {"converted_to_approval"}
    assert {s["status"] for s in trace if s["tool"] != "send_email"} == {"executed"}
    assert summary.steps_executed == len(trace) - len(sends)
    assert rows(db, "SELECT COUNT(*) AS n FROM mail_outbox") == [{"n": 0}]
    assert rows(db, "SELECT COUNT(*) AS n FROM drafts") == [{"n": 9}]
    assert rows(db, "SELECT COUNT(*) AS n FROM approvals WHERE kind = 'send_email'") == [{"n": 2}]
    tasks = rows(db, "SELECT invoice_number, owner_role FROM tasks ORDER BY invoice_number")
    assert tasks == [
        {"invoice_number": "INV-004", "owner_role": "finance"},  # po_copy request
        {"invoice_number": "INV-009", "owner_role": "finance"},  # escalations
        {"invoice_number": "INV-011", "owner_role": "finance"},
        {"invoice_number": "INV-014", "owner_role": "finance"},
    ]
    states = {
        r["invoice_number"]: (r["state"], r["next_followup"])
        for r in rows(db, "SELECT * FROM cases")
    }
    assert states["INV-004"] == ("DISPUTE_OPEN", "2026-11-03")
    assert states["INV-011"] == ("ESCALATED", "2026-11-03")
    assert states["INV-003"] == ("ASSESSED", "2026-11-07")
    assert states["INV-005"] == ("ASSESSED", "2026-11-05")
    run = rows(db, "SELECT * FROM agent_runs")[0]
    assert (run["as_of"], run["now_ts"], run["autonomy_level"]) == (
        "2026-10-31",
        "2026-11-02T06:00:00Z",
        1,
    )
    assert (run["started_at"], run["finished_at"]) == ("2026-11-02T06:00:00Z",) * 2


def test_drafts_are_validated_templates(db: Engine) -> None:
    tick(db)
    drafts = rows(db, "SELECT * FROM drafts")
    assert {d["generator"] for d in drafts} == {"template"}
    assert {d["violations_json"] for d in drafts} == {"[]"}
    assert {d["status"] for d in drafts} == {"draft"}
    inv4 = next(d for d in drafts if json.loads(d["invoice_numbers_json"]) == ["INV-004"])
    assert inv4["tone"] == 3 and "purchase order" in inv4["body"]


# --- T-AGT-01 ----------------------------------------------------------------------------------


def wire_transfer(invoice: str) -> dict[str, Any]:
    return {
        "invoice_number": invoice,
        "steps": [{"tool": "wire_transfer", "args": {"amount": "50000"}, "reason": "pay now"}],
    }


def test_t_agt_01_unknown_tool_rejected_and_rule_planner_used(db: Engine) -> None:
    summary = tick(db, planner=ScriptedPlanner(default=wire_transfer))
    assert (summary.planner, summary.fallback, summary.model) == (
        "llm",
        True,
        "scripted-test-planner",
    )
    assert tools_by_invoice(db) == T_AGT_08  # the rule planner ran for every case
    assert all(s["tool"] != "wire_transfer" for s in steps(db))
    rejected = rows(
        db, "SELECT entity_id, after_json FROM audit_log WHERE action = 'agent.plan_rejected'"
    )
    assert len(rejected) == 9
    detail = json.loads(rejected[0]["after_json"])
    assert (detail["status"], detail["reasons"], detail["tools"]) == (
        "PLAN_REJECTED",
        ["UNKNOWN_TOOL"],
        ["wire_transfer"],
    )
    assert "50000" not in rejected[0]["after_json"] and "pay now" not in rejected[0]["after_json"]
    run = rows(db, "SELECT planner, fallback, model FROM agent_runs")[0]
    assert run == {"planner": "llm", "fallback": 1, "model": "scripted-test-planner"}


# --- T-AGT-02 ----------------------------------------------------------------------------------


def send_inv_004(_: str) -> dict[str, Any]:
    return {
        "invoice_number": "INV-004",
        "steps": [
            {
                "tool": "draft_message",
                "args": {
                    "invoice_numbers": ["INV-004"],
                    "tone": 3,
                    "dispute_label": "PO_AMOUNT_MISMATCH",
                },
                "reason": "chase",
            },
            {"tool": "send_email", "args": {"draft_ref": "step:1"}, "reason": "send it"},
        ],
    }


def test_t_agt_02_denied_send_becomes_an_approval(db: Engine) -> None:
    planner = ScriptedPlanner({"INV-004": send_inv_004}, default=PlannerError("timeout"))
    summary = tick(db, level=2, planner=planner, cfg=config(allowlisted_customers=("CUST-B",)))
    send = next(
        s for s in steps(db) if s["invoice_number"] == "INV-004" and s["tool"] == "send_email"
    )
    assert send["status"] == "converted_to_approval"
    assert send["policy_allowed"] == 0
    reasons = ["POLICY_AMOUNT_LIMIT", "POLICY_DISPUTE_ACTIVE", "POLICY_TONE_NOT_ALLOWED"]
    assert json.loads(send["policy_reasons_json"]) == reasons
    approval = rows(db, f"SELECT * FROM approvals WHERE step_id = '{send['step_id']}'")[0]  # noqa: S608
    assert json.loads(approval["payload_json"])["reasons"] == reasons
    assert approval["status"] == "pending"
    assert rows(db, "SELECT COUNT(*) AS n FROM mail_outbox") == [{"n": 0}]
    assert summary.steps_denied == 3  # INV-004; INV-003 and INV-012 (customers not allowlisted)
    assert summary.fallback is True  # the other cases fell back after the planner failed


def test_allowed_l2_send_goes_to_the_sandbox_outbox_only(db: Engine) -> None:
    tick(db, level=2, cfg=config(allowlisted_customers=("CUST-A",)))
    mail = rows(db, "SELECT * FROM mail_outbox")
    assert len(mail) == 1
    assert (mail[0]["to_address"], mail[0]["gateway"], mail[0]["status"]) == (
        "ap@alpha.example",
        "sandbox",
        "delivered_sandbox",
    )
    sent = next(
        s for s in steps(db) if s["invoice_number"] == "INV-003" and s["tool"] == "send_email"
    )
    assert (sent["status"], sent["policy_allowed"]) == ("executed", 1)
    state = rows(db, "SELECT state FROM cases WHERE invoice_number = 'INV-003'")
    assert state == [{"state": "AWAITING_REPLY"}]
    other = next(
        s for s in steps(db) if s["invoice_number"] == "INV-012" and s["tool"] == "send_email"
    )
    assert other["status"] == "converted_to_approval"
    assert json.loads(other["policy_reasons_json"]) == ["POLICY_NOT_ALLOWLISTED"]


def test_a_second_tick_the_same_day_sees_the_sandbox_send(db: Engine) -> None:
    cfg = config(allowlisted_customers=("CUST-A",))
    tick(db, level=2, cfg=cfg)
    tick(db, level=2, cfg=cfg)
    second = [
        s for s in steps(db) if s["invoice_number"] == "INV-003" and s["tool"] == "send_email"
    ][-1]
    assert second["status"] == "converted_to_approval"
    assert json.loads(second["policy_reasons_json"]) == ["POLICY_RECENT_CONTACT"]
    assert len(rows(db, "SELECT * FROM mail_outbox")) == 1


# --- T-AGT-04 ----------------------------------------------------------------------------------


def test_t_agt_04_the_run_is_fully_traced_and_logs_hold_no_pii(
    db: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    planner = ScriptedPlanner(default=lambda n: plan_case_json(db, n))
    with caplog.at_level(logging.DEBUG):
        summary = tick(db, level=1, planner=planner)
    run = rows(db, "SELECT * FROM agent_runs")[0]
    assert (run["planner"], run["model"], run["fallback"]) == ("llm", "scripted-test-planner", 0)
    assert (run["cases_considered"], run["steps_executed"]) == (9, summary.steps_executed)
    for s in steps(db):
        assert json.loads(s["args_json"]) is not None and s["reason"]
        assert s["status"] in {"executed", "converted_to_approval"}
        assert json.loads(s["result_json"])  # tool output stored
        if s["tool"] == "send_email":
            assert s["policy_allowed"] in (0, 1)
        else:
            assert s["policy_allowed"] is None and s["policy_reasons_json"] == "[]"
    actions = audit_actions(db)
    assert actions.count("agent.run_started") == 1 and actions.count("agent.run_finished") == 1
    assert actions.count("case.transition") >= 9
    # The planner saw no customer names, emails, TRNs or licence numbers.
    seen = json.dumps(planner.observations)
    pii = ["Alpha Trading", "Beta Contracting", "Gamma Facilities", "@", "100000000", "TL-"]
    assert not [p for p in pii if p in seen]
    logged = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert not [p for p in [*pii, "Dear ", "Kind regards"] if p in logged]


def plan_case_json(engine: Engine, invoice: str) -> dict[str, Any]:
    with engine.connect() as c:
        facts = load_agent_facts(c, AS_OF, config().engine)
    return plan_case(facts, invoice).to_json()


# --- T-AGT-05 ----------------------------------------------------------------------------------


def _normalised(engine: Engine) -> list[dict[str, Any]]:
    run_id = rows(engine, "SELECT run_id FROM agent_runs")[0]["run_id"]
    out = []
    for s in steps(engine):
        s = {k: v for k, v in s.items() if k not in {"step_id", "run_id"}}
        out.append(json.loads(json.dumps(s).replace(run_id, "RUN")))
    return out


def test_t_agt_05_same_inputs_same_steps(tmp_path: Path) -> None:
    traces = []
    for name in ("a.db", "b.db"):
        engine = make_db(tmp_path / name)
        try:
            planner = ScriptedPlanner({"INV-004": send_inv_004}, default=wire_transfer)
            tick(engine, level=2, planner=planner, cfg=config(allowlisted_customers=("CUST-A",)))
            traces.append(_normalised(engine))
        finally:
            engine.dispose()
    assert traces[0] == traces[1]
    assert len(traces[0]) > 20


# --- T-AGT-07 ----------------------------------------------------------------------------------


def _add_overdue_invoices(engine: Engine, count: int) -> None:
    with engine.begin() as c:
        for i in range(count):
            c.execute(
                text(
                    "INSERT INTO invoices (invoice_number, customer_id, invoice_date, due_date,"
                    " amount_fils) VALUES (:n, 'CUST-C', '2026-08-01', :due, :amount)"
                ),
                {
                    "n": f"SYN-{i:03d}",
                    "due": f"2026-09-{i % 28 + 1:02d}",
                    "amount": 100_000 * (i + 1),
                },
            )


def six_steps(invoice: str) -> dict[str, Any]:
    step = {"tool": "get_case", "args": {"invoice_number": invoice}, "reason": "look"}
    return {"invoice_number": invoice, "steps": [step] * 6}


def test_t_agt_07_top_20_cases_and_six_step_plans_rejected(db: Engine) -> None:
    _add_overdue_invoices(db, 21)  # 9 golden + 21 = 30 overdue invoices
    with db.connect() as c:
        facts = load_agent_facts(c, AS_OF, config().engine)
    assert len(facts.scored) == 30
    top = sorted(facts.scored.values(), key=lambda s: (-s.priority_value, s.invoice_number))[:20]
    summary = tick(db, planner=ScriptedPlanner(default=six_steps))
    assert summary.cases_considered == 20
    assert set(tools_by_invoice(db)) == {s.invoice_number for s in top}
    rejected = rows(db, "SELECT after_json FROM audit_log WHERE action = 'agent.plan_rejected'")
    assert len(rejected) == 20
    assert {tuple(json.loads(r["after_json"])["reasons"]) for r in rejected} == {
        ("TOO_MANY_STEPS",)
    }
    assert all(len(t) <= 5 for t in tools_by_invoice(db).values())


def test_the_case_budget_comes_from_config(db: Engine) -> None:
    base = config()
    cfg = dataclasses.replace(base, agent=dataclasses.replace(base.agent, max_cases_per_tick=3))
    assert set(tools_by_invoice(db) or {}) == set()
    tick(db, cfg=cfg)
    assert set(tools_by_invoice(db)) == {"INV-004", "INV-011", "INV-005"}


# --- autonomy, case states, failures ----------------------------------------------------------


def test_l0_records_skipped_steps_and_changes_nothing(db: Engine) -> None:
    summary = tick(db, level=0)
    assert {s["status"] for s in steps(db)} == {"skipped"}
    assert tools_by_invoice(db) == T_AGT_08
    assert summary.steps_executed == 0
    for table in ("cases", "drafts", "tasks", "approvals", "mail_outbox"):
        assert rows(db, f"SELECT COUNT(*) AS n FROM {table}") == [{"n": 0}], table  # noqa: S608


def test_paid_invoices_resolve_their_cases(db: Engine) -> None:
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO cases (invoice_number, state, updated_at)"
                " VALUES ('INV-001', 'AWAITING_REPLY', '2026-10-01T05:00:00Z')"
            )
        )
    tick(db)
    assert rows(db, "SELECT state FROM cases WHERE invoice_number = 'INV-001'") == [
        {"state": "RESOLVED"}
    ]


def test_a_resolved_dispute_returns_the_case_to_assessed_before_planning(db: Engine) -> None:
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO cases (invoice_number, state, updated_at)"
                " VALUES ('INV-005', 'DISPUTE_OPEN', '2026-10-01T05:00:00Z')"
            )
        )
    tick(db)
    transitions = rows(
        db,
        "SELECT before_json, after_json FROM audit_log"
        " WHERE action = 'case.transition' AND entity_id = 'INV-005'",
    )
    assert [json.loads(t["after_json"])["trigger"] for t in transitions] == ["DisputeResolved"]
    assert rows(db, "SELECT state FROM cases WHERE invoice_number = 'INV-005'") == [
        {"state": "ASSESSED"}
    ]


def test_escalating_twice_creates_one_task(db: Engine) -> None:
    tick(db)
    tick(db)
    escalations = [
        s for s in steps(db) if s["tool"] == "escalate" and s["invoice_number"] == "INV-011"
    ]
    assert [s["status"] for s in escalations] == ["executed", "executed"]
    assert json.loads(escalations[1]["result_json"])["task_id"] is None
    assert rows(db, "SELECT COUNT(*) AS n FROM tasks WHERE invoice_number = 'INV-011'") == [
        {"n": 1}
    ]


def test_a_failing_case_is_rolled_back_and_the_tick_completes(db: Engine) -> None:
    planner = ScriptedPlanner(
        {"INV-011": RuntimeError("boom")}, default=lambda n: plan_case_json(db, n)
    )
    summary = tick(db, planner=planner)
    assert summary.case_errors == 1 and summary.cases_considered == 9
    assert "INV-011" not in tools_by_invoice(db)
    assert rows(db, "SELECT COUNT(*) AS n FROM cases WHERE invoice_number = 'INV-011'") == [
        {"n": 0}
    ]
    failed = rows(db, "SELECT after_json FROM audit_log WHERE action = 'agent.case_failed'")
    assert [json.loads(f["after_json"])["error"] for f in failed] == ["RuntimeError"]
    assert "boom" not in failed[0]["after_json"]
    assert len(tools_by_invoice(db)) == 8


def test_failing_steps_are_recorded_and_the_case_continues(db: Engine) -> None:
    def bad_send(invoice: str) -> dict[str, Any]:
        return {
            "invoice_number": invoice,
            "steps": [
                {"tool": "send_email", "args": {"draft_id": "D-MISSING"}, "reason": "send"},
                {"tool": "classify_message", "args": {"message_id": "M-1"}, "reason": "read"},
                {"tool": "get_case", "args": {"invoice_number": invoice}, "reason": "look"},
            ],
        }

    tick(
        db, planner=ScriptedPlanner({"INV-003": bad_send}, default=lambda n: plan_case_json(db, n))
    )
    inv3 = [s for s in steps(db) if s["invoice_number"] == "INV-003"]
    assert [(s["tool"], s["status"]) for s in inv3] == [
        ("send_email", "failed"),
        ("classify_message", "failed"),
        ("get_case", "executed"),
    ]
    assert [json.loads(s["result_json"]).get("error") for s in inv3[:2]] == [
        "DRAFT_NOT_AVAILABLE",
        "NOT_AVAILABLE",
    ]


def test_run_tick_refuses_naive_time_and_bad_levels(db: Engine) -> None:
    with db.begin() as c:
        store = DbAgentStore(c, CLOCK)
        with pytest.raises(ValueError, match="time zone"):
            run_tick(store, as_of=AS_OF, now_local=datetime(2026, 11, 2, 10), autonomy_level=1,
                     config=tick_config(config()))  # fmt: skip
        with pytest.raises(ValueError, match="autonomy_level"):
            run_tick(store, as_of=AS_OF, now_local=CLOCK.now(), autonomy_level=3,
                     config=tick_config(config()))  # fmt: skip


def test_other_internal_tools(db: Engine) -> None:
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO payments (payment_id, customer_id, payment_date, amount_fils)"
                " VALUES ('P-005', 'CUST-C', '2026-10-28', 1200000)"
            )
        )

    def internal(invoice: str) -> dict[str, Any]:
        return {
            "invoice_number": invoice,
            "steps": [
                {
                    "tool": "create_task",
                    "args": {"owner_role": "sales", "text": "Call AP.", "due_date": "2026-11-04"},
                    "reason": "r",
                },
                {
                    "tool": "propose_allocation",
                    "args": {"payment_id": "P-005", "invoice_number": invoice},
                    "reason": "r",
                },
                {
                    "tool": "propose_allocation",
                    "args": {"payment_id": "P-001", "invoice_number": invoice},
                    "reason": "already allocated",
                },
                {
                    "tool": "draft_message",
                    "args": {"invoice_numbers": [invoice, "INV-007"], "tone": 1},
                    "reason": "INV-007 is paid",
                },
            ],
        }

    tick(
        db, planner=ScriptedPlanner({"INV-012": internal}, default=lambda n: plan_case_json(db, n))
    )
    inv12 = [s for s in steps(db) if s["invoice_number"] == "INV-012"]
    assert [(s["tool"], s["status"]) for s in inv12] == [
        ("create_task", "executed"),
        ("propose_allocation", "executed"),
        ("propose_allocation", "failed"),
        ("draft_message", "failed"),
    ]
    assert [json.loads(s["result_json"]).get("error") for s in inv12[2:]] == [
        "UNKNOWN_PAYMENT",
        "ValueError",
    ]
    task = rows(db, "SELECT * FROM tasks WHERE owner_role = 'sales'")[0]
    assert (task["invoice_number"], task["text"], task["due_date"], task["created_by"]) == (
        "INV-012",
        "Call AP.",
        "2026-11-04",
        "agent",
    )
    approval = rows(db, "SELECT * FROM approvals WHERE kind = 'allocation'")[0]
    assert json.loads(approval["payload_json"]) == {
        "invoice_number": "INV-012",
        "payment_id": "P-005",
    }
    assert approval["step_id"] == inv12[1]["step_id"]


def test_outbound_dates_skip_unreadable_timestamps(db: Engine) -> None:
    with db.begin() as c:
        for message_id, sent_at in (
            ("M-1", "2026-10-30T21:00:00Z"),  # 31 Oct in Dubai
            ("M-2", "31/10/2026"),
            ("M-3", "2026-10-30T09:00:00"),  # no offset
        ):
            c.execute(
                text(
                    "INSERT INTO correspondence (message_id, invoice_number, direction, sent_at)"
                    " VALUES (:m, 'INV-003', 'outbound', :s)"
                ),
                {"m": message_id, "s": sent_at},
            )
        assert DbAgentStore(c, CLOCK).outbound_dates("CUST-A", "Asia/Dubai") == (
            date(2026, 10, 31),
        )


def test_a_resolved_case_is_not_reopened_or_escalated(db: Engine) -> None:
    # A case resolved earlier whose invoice is overdue again (e.g. a reversed payment).
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO cases (invoice_number, state, updated_at)"
                " VALUES ('INV-011', 'RESOLVED', '2026-10-01T05:00:00Z')"
            )
        )
    tick(db)
    escalate = next(
        s for s in steps(db) if s["invoice_number"] == "INV-011" and s["tool"] == "escalate"
    )
    assert (escalate["status"], json.loads(escalate["result_json"])) == (
        "failed",
        {"error": "CASE_NOT_ESCALATABLE"},
    )
    assert rows(db, "SELECT state FROM cases WHERE invoice_number = 'INV-011'") == [
        {"state": "RESOLVED"}
    ]


def test_an_allowed_send_at_l1_still_becomes_an_approval(db: Engine) -> None:
    tick(db, level=1, cfg=config(allowlisted_customers=("CUST-A",)))
    send = next(
        s for s in steps(db) if s["invoice_number"] == "INV-003" and s["tool"] == "send_email"
    )
    assert (send["status"], send["policy_allowed"], send["policy_reasons_json"]) == (
        "converted_to_approval",
        1,
        "[]",
    )
    assert rows(db, "SELECT COUNT(*) AS n FROM mail_outbox") == [{"n": 0}]


def test_the_daily_cap_counts_sends_made_earlier_in_the_tick(db: Engine) -> None:
    tick(db, level=2, cfg=config(allowlisted_customers=("CUST-A", "CUST-C"), daily_send_cap=1))
    sends = {s["invoice_number"]: s for s in steps(db) if s["tool"] == "send_email"}
    assert sends["INV-003"]["status"] == "executed"  # rank 5, first
    assert sends["INV-012"]["status"] == "converted_to_approval"  # rank 8, cap reached
    assert json.loads(sends["INV-012"]["policy_reasons_json"]) == ["POLICY_DAILY_CAP"]
    assert len(rows(db, "SELECT * FROM mail_outbox")) == 1


def test_send_email_only_uses_a_draft_for_this_case(db: Engine) -> None:
    tick(db)
    drafts = {
        json.loads(d["invoice_numbers_json"])[0]: d["draft_id"]
        for d in rows(db, "SELECT draft_id, invoice_numbers_json FROM drafts")
    }

    def send(draft_id: str) -> Any:
        return lambda invoice: {
            "invoice_number": invoice,
            "steps": [{"tool": "send_email", "args": {"draft_id": draft_id}, "reason": "r"}],
        }

    planner = ScriptedPlanner(
        {
            "INV-003": send(drafts["INV-012"]),  # another customer's draft
            "INV-013": send(drafts["INV-003"]),  # same customer, another invoice
            "INV-012": send(drafts["INV-012"]),  # its own draft from the first tick
        },
        default=lambda n: plan_case_json(db, n),
    )
    tick(db, level=1, planner=planner)
    results = {  # insertion order: the second tick's send steps come last
        s["invoice_number"]: (s["status"], json.loads(s["result_json"]).get("error"))
        for s in rows(db, "SELECT * FROM agent_steps WHERE tool = 'send_email' ORDER BY rowid")
    }
    assert results["INV-003"] == ("failed", "DRAFT_NOT_AVAILABLE")
    assert results["INV-013"] == ("failed", "DRAFT_NOT_AVAILABLE")
    assert results["INV-012"] == ("converted_to_approval", None)


class FailingStore(DbAgentStore):
    """Fails when writing the second step of INV-011, after its first step executed."""

    def add_step(self, step: Any) -> None:
        if step.invoice_number == "INV-011" and step.tool == "escalate":
            raise RuntimeError("disk full")
        super().add_step(step)


def test_a_case_failing_midway_leaves_no_trace_and_no_count(db: Engine) -> None:
    with db.begin() as c:
        summary = run_tick(
            FailingStore(c, CLOCK),
            as_of=AS_OF,
            now_local=CLOCK.now(),
            autonomy_level=1,
            config=tick_config(config()),
        )
    trace = steps(db)
    assert "INV-011" not in {s["invoice_number"] for s in trace}
    assert rows(
        db, "SELECT COUNT(*) AS n FROM drafts WHERE invoice_numbers_json LIKE '%INV-011%'"
    ) == [{"n": 0}]
    assert summary.case_errors == 1
    assert summary.steps_executed == sum(s["status"] == "executed" for s in trace)
    assert [s["seq"] for s in trace] == list(range(1, len(trace) + 1))
