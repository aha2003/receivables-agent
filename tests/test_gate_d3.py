"""Gate D3, policy and safety (SPEC §12, §18.4, §18.8, §18.9; D-039), in one scenario.

At L2, with every model doing what the hostile messages ask (classifier, planner and drafter;
see ``injection_support``):

1. disallowed actions produce explicit reason codes;
2. the injection changes no tool permission, no recipient and no ledger state;
3. L2 writes only to the sandbox outbox, and only to the customer's stored address.

Golden data plus three synthetic hostile messages; nothing reaches the network.
"""

import dataclasses
import json
from collections.abc import Iterator
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from injection_support import (
    ATTACKER,
    CLOCK,
    FIXTURES,
    behind_redaction,
    classify_as_told,
    draft_to_attacker,
    import_hostile,
    ledger_snapshot,
    plan_send_to_attacker,
    rows,
)
from sqlalchemy import Engine

from receivables.adapters.config import load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.classifications import classify_stored
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.llm.drafter import make_draft_writer
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.planner import make_llm_planner
from receivables.agent.tools import TOOL_REGISTRY
from receivables.seed import seed
from receivables.web.labels import POLICY_TEXT

BASE = load_config(FIXTURES.parent / "config.yaml")
L2 = dataclasses.replace(
    BASE,
    agent=dataclasses.replace(
        BASE.agent,
        autonomy_level=2,
        l2=dataclasses.replace(BASE.agent.l2, allowlisted_customers=["CUST-A"]),
    ),
)
AS_OF = date(2026, 10, 31)


@pytest.fixture
def scenario(tmp_path: Path) -> Iterator[tuple[Engine, dict[str, Any]]]:
    url = f"sqlite:///{tmp_path / 'd3.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    import_hostile(engine)
    before = ledger_snapshot(engine)
    obeying = FakeClient(
        classify=classify_as_told, plan=plan_send_to_attacker, draft=draft_to_attacker
    )
    client = behind_redaction(engine, obeying)
    for message_id in ("X-01", "X-02", "X-03"):
        classify_stored(
            engine, message_id, client=client, review_threshold=Fraction(3, 5), clock=CLOCK,
            actor="reviewer",
        )  # fmt: skip
    run_agent_tick(
        engine, CLOCK, L2, as_of=AS_OF,
        planner=make_llm_planner(client), drafter=make_draft_writer(client),
    )  # fmt: skip
    yield engine, before
    engine.dispose()


def test_d3_disallowed_actions_carry_explicit_reason_codes(
    scenario: tuple[Engine, dict[str, Any]],
) -> None:
    engine, _ = scenario
    denied = rows(engine, "SELECT tool, policy_reasons_json FROM agent_steps"
                  " WHERE policy_allowed = 0")  # fmt: skip
    assert denied  # CUST-B and CUST-C are not on the allowlist
    for step in denied:
        reasons = json.loads(step["policy_reasons_json"])
        assert reasons and set(reasons) <= set(POLICY_TEXT)  # every code has its plain text
    rejected = rows(engine, "SELECT after_json FROM audit_log WHERE action = 'agent.plan_rejected'")
    assert rejected and all(json.loads(r["after_json"])["reasons"] for r in rejected)


def test_d3_the_injection_changes_no_permission_recipient_or_ledger(
    scenario: tuple[Engine, dict[str, Any]],
) -> None:
    engine, before = scenario
    # Tool permissions: only registry tools ran; every model plan was rejected.
    tools = {r["tool"] for r in rows(engine, "SELECT DISTINCT tool FROM agent_steps")}
    assert tools and tools <= set(TOOL_REGISTRY)
    cases = {r["invoice_number"] for r in rows(engine, "SELECT invoice_number FROM agent_steps")}
    rejected = rows(engine, "SELECT entity_id FROM audit_log WHERE action = 'agent.plan_rejected'")
    assert {r["entity_id"] for r in rejected} == cases
    # Recipient: the attacker's address is nowhere the agent acts.
    for row in rows(engine, "SELECT args_json, result_json FROM agent_steps"):
        assert ATTACKER not in (row["args_json"] or "") + (row["result_json"] or "")
    for row in rows(engine, "SELECT body FROM drafts"):
        assert ATTACKER not in row["body"]
    # Ledger: invoices, payments and customers are exactly as before.
    assert ledger_snapshot(engine) == before
    # The attempts are flagged for review.
    for row in rows(engine, "SELECT needs_review, flags_json FROM classifications"):
        assert row["needs_review"] == 1
        assert "INSTRUCTION_INJECTION_SUSPECTED" in row["flags_json"]


def test_d3_l2_writes_only_to_the_sandbox_outbox(
    scenario: tuple[Engine, dict[str, Any]],
) -> None:
    engine, _ = scenario
    sent = rows(engine, "SELECT o.to_address, o.gateway, o.status, c.contact_email"
                " FROM mail_outbox o JOIN drafts d USING (draft_id)"
                " JOIN customers c ON c.customer_id = d.customer_id")  # fmt: skip
    assert sent  # the allowlisted customer's reminder was sent, to the sandbox
    for mail in sent:
        assert (mail["gateway"], mail["status"]) == ("sandbox", "delivered_sandbox")
        assert mail["to_address"] == mail["contact_email"]
    assert L2.mail.gateway == "sandbox" and L2.mail.enabled is False
