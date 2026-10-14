"""Loading the agent's read-tool facts from the database (SPEC §18.3; D-020). Synthetic only."""

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import (
    load_agent_facts,
    load_customers,
    load_events,
    load_invoices,
    load_message_headers,
    load_payments,
    load_playbooks,
)
from receivables.agent.facts import MessageHeader, PlaybookRecord, build_agent_facts
from receivables.agent.tools import TOOL_REGISTRY
from receivables.core.config import EngineConfig
from receivables.seed import seed

CLOCK = FixedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))
AS_OF = date(2026, 10, 31)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'facts.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO customer_playbook (customer_id, promise_kept_rate, promises_counted,"
                " followup_gap_days, updated_at)"
                " VALUES ('CUST-A', 0.5, 4, 5, '2026-10-31T05:00:00Z')"
            )
        )
        c.execute(
            text(
                "INSERT INTO correspondence (message_id, invoice_number, customer_id, direction,"
                " sent_at, language, subject, body) VALUES ('M-001', 'INV-003', 'CUST-A',"
                " 'inbound', '2026-10-20T06:00:00Z', 'en', 'Synthetic subject',"
                " 'Synthetic body text')"
            )
        )
    yield engine
    engine.dispose()


def test_playbooks_and_message_headers_load(db: Engine) -> None:
    with db.connect() as c:
        assert load_playbooks(c) == [
            PlaybookRecord(
                customer_id="CUST-A",
                promise_kept_rate=0.5,
                promises_counted=4,
                avg_days_to_reply=None,
                replies_counted=0,
                followup_gap_days=5,
                updated_at="2026-10-31T05:00:00Z",
            )
        ]
        headers = load_message_headers(c)
    assert headers == [
        MessageHeader(
            message_id="M-001",
            invoice_number="INV-003",
            customer_id="CUST-A",
            direction="inbound",
            sent_at="2026-10-20T06:00:00Z",
            language="en",
        )
    ]


def test_load_agent_facts_equals_the_pure_build(db: Engine) -> None:
    config = EngineConfig()
    with db.connect() as c, c.begin():
        loaded = load_agent_facts(c, AS_OF, config)
        built = build_agent_facts(
            customers=load_customers(c),
            invoices=load_invoices(c),
            payments=load_payments(c),
            events=load_events(c),
            playbooks=load_playbooks(c),
            messages=load_message_headers(c),
            as_of=AS_OF,
            config=config,
        )
    assert loaded == built
    assert len(loaded.rows) == 14
    assert [i.scored.invoice_number for i in loaded.queue_items.values()][:3] == [
        "INV-004",
        "INV-011",
        "INV-005",
    ]


def test_get_case_from_the_database_has_no_subject_or_body(db: Engine) -> None:
    with db.connect() as c, c.begin():
        facts = load_agent_facts(c, AS_OF, EngineConfig())
    spec = TOOL_REGISTRY["get_case"]
    assert spec.handler is not None
    data = spec.handler(spec.args_model.model_validate({"invoice_number": "INV-003"}), facts).data
    assert data is not None
    assert [m["message_id"] for m in data["messages"]] == ["M-001"]
    assert "Synthetic subject" not in str(data) and "Synthetic body" not in str(data)
    assert data["playbook"]["followup_gap_days"] == 5
