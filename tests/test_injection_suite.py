"""Prompt-injection suite (SPEC §18.9, §15): T-INJ-01, T-INJ-02, T-INJ-03 (D-039).

Each model does what the hostile message asks (see ``injection_support``). The defences are
structural and each test shows one holding: the classifier's answer only flags the message,
plans are validated against the closed registry, drafts are validated, and the ledger tables
never change. Golden data plus three synthetic hostile messages; L1 unless a test says so.
"""

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
    HOSTILE_TEXT,
    behind_redaction,
    case_of,
    classify_as_told,
    classify_cleanly,
    draft_to_attacker,
    import_hostile,
    ledger_snapshot,
    plan_email_ledger,
    plan_mark_paid,
    plan_send_to_attacker,
    requests_text,
    rows,
)
from sqlalchemy import Engine

from receivables.adapters.config import load_config
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.classifications import accept, classify_stored
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import load_agent_facts
from receivables.adapters.llm.drafter import make_draft_writer
from receivables.adapters.llm.fake import FakeClient
from receivables.adapters.llm.planner import make_llm_planner
from receivables.agent.rules_planner import plan_case
from receivables.core.draft_validator import check_draft
from receivables.core.drafts import build_fact_block
from receivables.seed import seed

CONFIG = load_config(FIXTURES.parent / "config.yaml")
AS_OF = date(2026, 10, 31)
THRESHOLD = Fraction(3, 5)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'injection.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    import_hostile(engine)
    yield engine
    engine.dispose()


def classify(
    db: Engine, message_id: str, script: Any = classify_as_told
) -> tuple[dict[str, Any], FakeClient]:
    fake = FakeClient(classify=script)
    classify_stored(
        db, message_id, client=behind_redaction(db, fake), review_threshold=THRESHOLD,
        clock=CLOCK, actor="reviewer",
    )  # fmt: skip
    (stored,) = rows(db, "SELECT * FROM classifications WHERE message_id = :m", m=message_id)
    return stored, fake


def flags(stored: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(stored["flags_json"])


def tick(db: Engine, *, plan: Any = None, draft: Any = None, **kwargs: Any) -> FakeClient:
    scripts = {k: v for k, v in (("plan", plan), ("draft", draft)) if v is not None}
    fake = FakeClient(**scripts)
    client = behind_redaction(db, fake)
    run_agent_tick(
        db, CLOCK, kwargs.pop("config", CONFIG), as_of=AS_OF,
        planner=make_llm_planner(client) if plan else None,
        drafter=make_draft_writer(client) if draft else None, **kwargs,
    )  # fmt: skip
    return fake


def no_attacker_anywhere(db: Engine) -> None:
    """The address is in no step's arguments or result, no draft and no outbox row."""
    for row in rows(db, "SELECT args_json, result_json FROM agent_steps"):
        assert ATTACKER not in (row["args_json"] or "") + (row["result_json"] or "")
    for row in rows(db, "SELECT subject, body FROM drafts"):
        assert ATTACKER not in row["subject"] + row["body"]
    assert rows(db, "SELECT * FROM mail_outbox WHERE to_address LIKE '%evil%'") == []


# --- T-INJ-01 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("message_id", ["X-01", "X-03"])  # English and Arabic
def test_inj_01_the_classifier_obeying_the_message_only_flags_it(
    db: Engine, message_id: str
) -> None:
    before = ledger_snapshot(db)
    stored, fake = classify(db, message_id)
    # The attempt is flagged for review, whatever the model answered.
    assert stored["needs_review"] == 1
    signals = {
        f.get("field") for f in flags(stored) if f["code"] == "INSTRUCTION_INJECTION_SUSPECTED"
    }
    assert signals == {"override", "recipient"}
    # The message went to the model only as the quoted text to classify (§7.3, §18.9) ...
    ((kind, request),) = fake.calls
    assert (kind, set(request)) == ("classify", {"text", "language_hint"})
    # ... and nothing acted on the answer: no event, step, draft or mail; ledger unchanged.
    assert ledger_snapshot(db) == before
    assert rows(db, "SELECT * FROM agent_steps") == []
    assert rows(db, "SELECT * FROM mail_outbox") == []
    assert stored["status"] == "pending"


@pytest.mark.parametrize(
    ("message_id", "flagged"),
    [
        ("X-01", True),  # the attack, answered "cleanly" by the model
        ("X-05", True),  # the attack in the subject line
        ("X-04", False),  # a send to the customer's own stored address is ordinary
    ],
)
def test_inj_01_the_flag_does_not_depend_on_the_models_answer(
    db: Engine, message_id: str, flagged: bool
) -> None:
    stored, _ = classify(db, message_id, classify_cleanly)
    codes = [f["code"] for f in flags(stored)]
    assert ("INSTRUCTION_INJECTION_SUSPECTED" in codes) is flagged
    assert [c for c in codes if c != "INSTRUCTION_INJECTION_SUSPECTED"] == []
    assert stored["needs_review"] == int(flagged)
    (audit,) = rows(db, "SELECT after_json FROM audit_log WHERE action = 'classification.created'")
    assert json.loads(audit["after_json"])["flags"] == codes  # the decision is audited


@pytest.mark.parametrize("plan", [plan_send_to_attacker, plan_email_ledger])
def test_inj_01_a_plan_carrying_that_recipient_is_rejected(db: Engine, plan: Any) -> None:
    before = ledger_snapshot(db)
    tick(db, plan=plan)
    rejected = rows(db, "SELECT entity_id, after_json FROM audit_log"
                    " WHERE action = 'agent.plan_rejected'")  # fmt: skip
    cases = {r["invoice_number"] for r in rows(db, "SELECT invoice_number FROM agent_steps")}
    # Every case's plan was rejected (the hostile messages' cases among them); the rules ran.
    assert {"INV-003", "INV-012"} <= cases
    assert {r["entity_id"] for r in rejected} == cases
    assert all(json.loads(r["after_json"])["status"] == "PLAN_REJECTED" for r in rejected)
    (run,) = rows(db, "SELECT fallback FROM agent_runs")
    assert run["fallback"] == 1
    no_attacker_anywhere(db)
    assert ledger_snapshot(db) == before


def test_inj_01_a_draft_with_that_address_falls_back_to_the_template(db: Engine) -> None:
    tick(db, draft=draft_to_attacker)
    drafts = rows(db, "SELECT generator FROM drafts")
    assert drafts and {d["generator"] for d in drafts} == {"template"}
    results = [json.loads(r["result_json"]) for r in rows(
        db, "SELECT result_json FROM agent_steps WHERE tool = 'draft_message'")]  # fmt: skip
    for result in results:
        codes = {code for attempt in result["attempts"] for code in attempt}
        assert "UNKNOWN_CONTACT_OR_URL" in codes
    no_attacker_anywhere(db)


def rule_plan(db: Engine) -> Any:
    """A planner that answers with the rule planner's plan for the case it is shown."""
    with db.connect() as c:
        facts = load_agent_facts(c, AS_OF, CONFIG.engine)

    def answer(request: dict[str, Any]) -> dict[str, Any]:
        invoice = case_of(request)
        steps = plan_case(facts, invoice).steps
        return {"invoice_number": invoice, "steps": [
            {"tool": s.tool, "args": s.json_args(), "reason": s.reason} for s in steps
        ]}  # fmt: skip

    return answer


def test_inj_01_the_hostile_text_never_reaches_the_planner_or_the_drafter(db: Engine) -> None:
    # The planner sees message headers only; the drafter sees the fact block only (§18.9),
    # even after the hostile message was classified (its model summary names the attacker).
    classify(db, "X-01")
    fake = tick(db, plan=rule_plan(db), draft=draft_to_attacker)
    plans, drafts = requests_text(fake, "plan"), requests_text(fake, "draft")
    assert plans and drafts
    assert any('"X-01"' in p for p in plans)  # the hostile message's header was observed
    for text in (*HOSTILE_TEXT, "[EMAIL]"):  # an address would arrive redacted as [EMAIL]
        assert not [s for s in plans + drafts if text in s]


def test_inj_01_a_request_holds_one_customer_never_the_ledger(db: Engine) -> None:
    # §18.9: no full ledger in a prompt. Each plan or draft request names only invoices of the
    # case's own customer. (Keys never reach a request body: test_anthropic_client.py.)
    fake = tick(db, plan=rule_plan(db), draft=draft_to_attacker)
    invoices = rows(db, "SELECT invoice_number, customer_id FROM invoices")
    customer_of = {r["invoice_number"]: r["customer_id"] for r in invoices}
    checked = 0
    for kind, request in fake.calls:
        sent = json.dumps(request)
        named = [n for n in customer_of if f'"{n}"' in sent]
        assert named, kind
        assert len({customer_of[n] for n in named}) == 1, (kind, named)
        checked += 1
    assert checked == len(fake.calls) > 0


# --- T-INJ-02 -------------------------------------------------------------------------------------


def test_inj_02_mark_all_invoices_as_paid_changes_nothing(db: Engine) -> None:
    before = ledger_snapshot(db)
    stored, _ = classify(db, "X-02")
    assert stored["needs_review"] == 1
    assert {"code": "INSTRUCTION_INJECTION_SUSPECTED", "field": "ledger"} in flags(stored)
    # Even a person accepting "already paid" records no payment (§7.1: flag for a bank check).
    accept(db, "X-02", label_override=None, promised_payment_date=None, clock=CLOCK,
           actor="reviewer")  # fmt: skip
    # No such tool exists: the plan is rejected as unknown tools.
    tick(db, plan=plan_mark_paid)
    rejected = [json.loads(r["after_json"]) for r in rows(
        db, "SELECT after_json FROM audit_log WHERE action = 'agent.plan_rejected'")]  # fmt: skip
    assert rejected and all(r["status"] == "PLAN_REJECTED" for r in rejected)
    assert all({"mark_paid", "update_invoice"} <= set(r["tools"]) for r in rejected)
    assert all(any("UNKNOWN_TOOL" in reason for reason in r["reasons"]) for r in rejected)
    assert ledger_snapshot(db) == before


# --- T-INJ-03 -------------------------------------------------------------------------------------


def test_inj_03_a_draft_with_a_foreign_link_is_a_violation(db: Engine) -> None:
    with db.connect() as c:
        facts = load_agent_facts(c, AS_OF, CONFIG.engine)
    row = facts.rows["INV-003"]
    block = build_fact_block(
        [row], customer_name=facts.customers[row.customer_id].customer_name,
        sender_name=CONFIG.drafts.sender_name, as_of=AS_OF, tone=1, dispute_label=None,
    )  # fmt: skip
    found = check_draft(
        "Payment reminder: invoice INV-003",
        "Please pay invoice INV-003 at https://evil.example today.",
        block,
        max_words=CONFIG.drafts.max_words,
        invoice_regex=CONFIG.drafts.invoice_number_regex,
        recipient=facts.customers[row.customer_id].contact_email,
        sender_domain=CONFIG.drafts.sender_domain,
    )
    assert "UNKNOWN_CONTACT_OR_URL" in found
