"""Which planner and drafter a run uses (D-036, D-037): none in fake mode, the app's client
otherwise. The client here is a scripted FakeClient; nothing reaches a model."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.redacting import RedactingClient
from receivables.seed import seed

from .conftest import csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def schedule_only(request: Mapping[str, Any]) -> dict[str, Any]:
    invoice = request["observation"]["case"]["invoice_number"]
    step = {"tool": "schedule_followup", "args": {"invoice_number": invoice, "days": 4},
            "reason": "check back"}  # fmt: skip
    return {"invoice_number": invoice, "steps": [step]}


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    return client


def run(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/agent/run", json={"as_of": "2026-10-31"}, headers={"x-csrf-token": csrf(client)}
    )
    assert response.status_code == 200
    return response.json()


def test_fake_mode_plans_by_rules_and_drafts_by_template(user: TestClient, engine: Engine) -> None:
    summary = run(user)
    assert (summary["planner"], summary["fallback"]) == ("rules", False)
    with engine.connect() as c:
        generators = set(c.execute(text("SELECT generator FROM drafts")).scalars())
        received = c.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE action = 'agent.plan_received'")
        ).scalar_one()
    assert generators == {"template"} and received == 0


@pytest.mark.parametrize("mode", ["replay", "live"])
def test_other_modes_use_the_apps_client(
    user: TestClient, app: FastAPI, engine: Engine, mode: str
) -> None:
    fake = FakeClient(plan=schedule_only)
    app.state.llm_mode = mode
    app.state.llm = RedactingClient(
        fake,
        redact_before_send=True,
        pseudonymize_customer_names=True,
        customer_names=customer_names(engine),
    )
    summary = run(user)
    assert (summary["planner"], summary["fallback"]) == ("llm", False)
    assert {kind for kind, _ in fake.calls} == {"plan"}  # no draft step in these plans


def test_a_dry_run_in_a_model_mode_makes_no_model_call(
    user: TestClient, app: FastAPI, engine: Engine
) -> None:
    def draft_plan(request: Mapping[str, Any]) -> dict[str, Any]:
        invoice = request["observation"]["case"]["invoice_number"]
        step = {"tool": "draft_message", "args": {"invoice_numbers": [invoice], "tone": 1},
                "reason": "remind"}  # fmt: skip
        return {"invoice_number": invoice, "steps": [step]}

    fake = FakeClient(plan=draft_plan)
    app.state.llm_mode = "replay"
    app.state.llm = fake
    response = user.post(
        "/api/agent/run",
        json={"as_of": "2026-10-31", "dry_run": True},
        headers={"x-csrf-token": csrf(user)},
    )
    assert response.status_code == 200
    assert fake.calls == []  # a preview plans by rules and drafts from templates (D-037)
