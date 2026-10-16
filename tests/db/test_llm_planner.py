"""The LLM planner via FakeClient (SPEC §18.6): T-AGT-01, T-AGT-06, T-AGT-07 and the run record.

The model is a scripted FakeClient behind the app's redaction; nothing reaches the network.
Golden data, L1 at 2026-10-31.
"""

import json
from collections.abc import Callable, Iterator, Mapping
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
from receivables.adapters.llm.anthropic_client import LLMUnavailable
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.planner import make_llm_planner
from receivables.adapters.llm.redacting import RedactingClient
from receivables.agent.orchestrator import RunSummary
from receivables.agent.rules_planner import plan_case
from receivables.seed import seed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CONFIG = load_config(FIXTURES.parent / "config.yaml")
CLOCK = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))
AS_OF = date(2026, 10, 31)
GOLDEN = {"INV-004", "INV-011", "INV-009", "INV-014", "INV-005", "INV-013", "INV-008", "INV-003",
          "INV-012"}  # fmt: skip
NAMES = ("Alpha Trading LLC", "Beta Contracting LLC", "Gamma Facilities Services LLC")
Script = Callable[[Mapping[str, Any]], Any]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'planner.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def run(db: Engine, script: Script, **kwargs: Any) -> tuple[RunSummary, FakeClient]:
    fake = FakeClient(plan=script)
    client = RedactingClient(
        fake,
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=customer_names(db),
    )
    summary = run_agent_tick(
        db, CLOCK, CONFIG, as_of=AS_OF, planner=make_llm_planner(client), **kwargs
    )
    return summary, fake


def rows(db: Engine, sql: str) -> list[dict[str, Any]]:
    with db.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def tools_by_invoice(db: Engine) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in rows(db, "SELECT invoice_number, tool FROM agent_steps ORDER BY seq"):
        out.setdefault(r["invoice_number"], []).append(r["tool"])
    return out


def audits(db: Engine, action: str) -> list[dict[str, Any]]:
    with db.connect() as c:
        found = c.execute(
            text("SELECT entity_id, after_json FROM audit_log WHERE action = :a ORDER BY rowid"),
            {"a": action},
        )
        return [{"invoice": r.entity_id, **json.loads(r.after_json)} for r in found]


def schedule_only(request: Mapping[str, Any]) -> dict[str, Any]:
    invoice = request["observation"]["case"]["invoice_number"]
    step = {"tool": "schedule_followup", "args": {"invoice_number": invoice, "days": 4},
            "reason": "check back"}  # fmt: skip
    return {"invoice_number": invoice, "steps": [step]}


def rule_tools(db: Engine, invoice: str) -> list[str]:
    from receivables.adapters.db.repo import load_agent_facts

    with db.connect() as c:
        facts = load_agent_facts(c, AS_OF, CONFIG.engine)
    return [s.tool for s in plan_case(facts, invoice).steps]


# --- a valid plan is used -------------------------------------------------------------------------


def test_valid_llm_plans_are_used_and_recorded(db: Engine) -> None:
    summary, fake = run(db, schedule_only)
    assert (summary.planner, summary.fallback, summary.model) == ("llm", False, "fake")
    assert tools_by_invoice(db) == {i: ["schedule_followup"] for i in GOLDEN}
    received = audits(db, "agent.plan_received")
    assert len(received) == 9
    for r in received:
        assert (r["plan"], r["model"], r["prompt_version"], r["attempts"], r["outcome"]) == (
            "used", "fake", "plan-v1", 1, "ok",
        )  # fmt: skip
        assert isinstance(r["latency_ms"], float) and r["latency_ms"] >= 0
    sent = json.dumps([payload for _, payload in fake.calls])
    assert not [name for name in NAMES if name in sent]  # §18.6 step 2: redacted observation


# --- T-AGT-01 via the LLM planner -----------------------------------------------------------------


@pytest.mark.spec("T-AGT-01")
def test_t_agt_01_an_llm_plan_with_wire_transfer_is_rejected(db: Engine) -> None:
    def wire(request: Mapping[str, Any]) -> dict[str, Any]:
        invoice = request["observation"]["case"]["invoice_number"]
        step = {"tool": "wire_transfer", "args": {"amount": "50000"}, "reason": "pay now"}
        return {"invoice_number": invoice, "steps": [step]}

    summary, _ = run(db, wire)
    assert (summary.planner, summary.fallback) == ("llm", True)
    assert all("wire_transfer" not in tools for tools in tools_by_invoice(db).values())
    assert {i: tools for i, tools in tools_by_invoice(db).items()} == {
        i: rule_tools(db, i) for i in GOLDEN
    }  # the rule planner ran for every case
    rejected = audits(db, "agent.plan_rejected")
    assert len(rejected) == 9 and {r["status"] for r in rejected} == {"PLAN_REJECTED"}
    assert {r["plan"] for r in audits(db, "agent.plan_received")} == {"rejected"}


# --- T-AGT-06 via the LLM planner -----------------------------------------------------------------


@pytest.mark.spec("T-AGT-06")
def test_t_agt_06_timeout_then_invalid_json_falls_back_and_the_queue_completes(
    db: Engine,
) -> None:
    calls: list[str] = []

    def flaky(request: Mapping[str, Any]) -> Any:
        invoice = request["observation"]["case"]["invoice_number"]
        calls.append(invoice)
        if len(calls) == 1:
            raise LLMUnavailable("APITimeoutError")  # the first case times out
        if len(calls) in (2, 3):
            return "{not json"  # the second case: invalid JSON twice
        return schedule_only(request)

    summary, _ = run(db, flaky)
    assert (summary.planner, summary.fallback) == ("llm", True)
    assert summary.cases_considered == 9
    assert set(tools_by_invoice(db)) == GOLDEN  # the whole queue is still processed
    first, second = calls[0], calls[1]
    assert calls[2] == second  # one retry for the invalid output
    assert tools_by_invoice(db)[first] == rule_tools(db, first)
    assert tools_by_invoice(db)[second] == rule_tools(db, second)
    received = {r["invoice"]: r for r in audits(db, "agent.plan_received")}
    assert (received[first]["plan"], received[first]["outcome"], received[first]["attempts"]) == (
        "failed", "LLMUnavailable", 1,
    )  # fmt: skip
    assert (received[second]["plan"], received[second]["attempts"]) == ("failed", 2)
    assert received[second]["outcome"] == "invalid_output"
    others = [r for i, r in received.items() if i not in (first, second)]
    assert others and {r["plan"] for r in others} == {"used"}
    run_row = rows(db, "SELECT planner, fallback FROM agent_runs")[0]
    assert run_row == {"planner": "llm", "fallback": 1}


def test_an_invalid_answer_then_a_valid_one_is_used(db: Engine) -> None:
    answers: dict[str, int] = {}

    def second_try(request: Mapping[str, Any]) -> Any:
        invoice = request["observation"]["case"]["invoice_number"]
        answers[invoice] = answers.get(invoice, 0) + 1
        return "{not json" if answers[invoice] == 1 else schedule_only(request)

    summary, _ = run(db, second_try)
    assert summary.fallback is False
    assert {r["attempts"] for r in audits(db, "agent.plan_received")} == {2}
    assert {r["outcome"] for r in audits(db, "agent.plan_received")} == {"ok"}


# --- T-AGT-07 via the LLM planner -----------------------------------------------------------------


@pytest.mark.spec("T-AGT-07")
def test_t_agt_07_top_20_and_a_six_step_llm_plan_is_rejected(db: Engine) -> None:
    with db.begin() as c:
        for i in range(21):  # 9 golden + 21 = 30 overdue invoices
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

    def six(request: Mapping[str, Any]) -> dict[str, Any]:
        invoice = request["observation"]["case"]["invoice_number"]
        step = {"tool": "get_case", "args": {"invoice_number": invoice}, "reason": "look"}
        return {"invoice_number": invoice, "steps": [step] * 6}

    summary, fake = run(db, six)
    assert summary.cases_considered == 20 and len(fake.calls) == 20
    rejected = audits(db, "agent.plan_rejected")
    assert len(rejected) == 20 and {tuple(r["reasons"]) for r in rejected} == {("TOO_MANY_STEPS",)}
    assert all(len(tools) <= 5 for tools in tools_by_invoice(db).values())


# --- no planner call when nothing may happen ------------------------------------------------------


def test_no_planner_call_with_the_kill_switch(db: Engine) -> None:
    import dataclasses

    l2 = dataclasses.replace(CONFIG.agent.l2, kill_switch=True)
    config = dataclasses.replace(CONFIG, agent=dataclasses.replace(CONFIG.agent, l2=l2))
    fake = FakeClient(plan=schedule_only)
    run_agent_tick(db, CLOCK, config, as_of=AS_OF, planner=make_llm_planner(fake))
    assert fake.calls == []


# --- spec review of S3.08 -------------------------------------------------------------------------


def test_a_plan_over_five_steps_is_plan_rejected_not_failed(db: Engine) -> None:
    def many(request: Mapping[str, Any]) -> dict[str, Any]:
        invoice = request["observation"]["case"]["invoice_number"]
        step = {"tool": "get_case", "args": {"invoice_number": invoice}, "reason": "look"}
        return {"invoice_number": invoice, "steps": [step] * 21}

    run(db, many)
    rejected = audits(db, "agent.plan_rejected")
    assert len(rejected) == 9 and {tuple(r["reasons"]) for r in rejected} == {("TOO_MANY_STEPS",)}
    assert audits(db, "agent.planner_failed") == []


def test_a_case_that_fails_before_the_call_carries_no_other_cases_timing(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from receivables.agent import orchestrator

    real = orchestrator._observation  # pyright: ignore[reportPrivateUsage]
    seen: list[str] = []

    def observation(tick: Any, invoice: str) -> dict[str, Any]:
        seen.append(invoice)
        if len(seen) == 2:
            raise RuntimeError("cannot build this observation")
        return real(tick, invoice)

    monkeypatch.setattr(orchestrator, "_observation", observation)
    run(db, schedule_only)
    received = {r["invoice"]: r for r in audits(db, "agent.plan_received")}
    broken = received[seen[1]]
    assert broken["plan"] == "failed"
    assert "latency_ms" not in broken and "outcome" not in broken  # never the previous case's
    assert received[seen[0]]["outcome"] == "ok"


def test_an_unexpected_planner_error_is_recorded_by_type(db: Engine) -> None:
    def broken(_: Mapping[str, Any]) -> Any:
        raise ZeroDivisionError

    run(db, broken)
    received = audits(db, "agent.plan_received")
    assert {(r["plan"], r["outcome"]) for r in received} == {("failed", "ZeroDivisionError")}


@pytest.mark.spec("T-AGT-07")
def test_t_agt_07_the_twenty_are_the_top_twenty_by_priority(db: Engine) -> None:
    from receivables.adapters.db.repo import load_agent_facts

    with db.begin() as c:
        for i in range(21):
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
    with db.connect() as c:
        facts = load_agent_facts(c, AS_OF, CONFIG.engine)
    top = sorted(facts.scored.values(), key=lambda s: (-s.priority_value, s.invoice_number))[:20]
    run(db, schedule_only)
    assert set(tools_by_invoice(db)) == {s.invoice_number for s in top}
