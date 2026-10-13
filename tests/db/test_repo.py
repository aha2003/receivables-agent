"""Repository layer and audit writer (SPEC §3.4, §5): T-AUD-01 (import audit rows)."""

import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, exc, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import (
    load_customers,
    load_events,
    load_invoices,
    load_ledger_inputs,
    load_payments,
    table_counts,
    write_audit,
)
from receivables.core.aging import aging_summary
from receivables.core.config import EngineConfig
from receivables.core.history import customer_history
from receivables.core.ledger import InvoiceRecord, PaymentRecord, build_ledger
from receivables.core.money import to_fils
from receivables.core.queue import build_queue
from receivables.core.scoring import EventRecord, score_overdue_invoices
from receivables.seed import seed

CLOCK = FixedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))
AS_OF = date(2026, 10, 31)
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'repo.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def seeded(db: Engine) -> Engine:
    seed(db, FIXTURES, CLOCK)
    return db


class TestAud01:
    def test_import_writes_audit_row_with_actor_entity_before_and_after(self, db: Engine) -> None:
        raw = (FIXTURES / "customers.csv").read_bytes()
        result = import_csv(
            db, "customers", raw, filename="customers.csv", clock=CLOCK, actor="tester"
        )
        with db.connect() as c:
            rows = c.execute(text("SELECT * FROM audit_log")).mappings().all()
        assert len(rows) == 1
        row = rows[0]
        assert row["actor"] == "tester"
        assert (row["action"], row["entity_type"], row["entity_id"]) == (
            "import.accepted",
            "import",
            result.import_id,
        )
        assert row["ts"] == "2026-10-31T05:00:00Z"
        before = json.loads(row["before_json"])
        after = json.loads(row["after_json"])
        assert before["row_counts"]["customers"] == 0
        assert after["row_counts"]["customers"] == 3
        assert after["file_kind"] == "customers" and after["rows_total"] == 3

    def test_every_seed_import_is_audited(self, seeded: Engine) -> None:
        with seeded.connect() as c:
            rows = c.execute(
                text("SELECT action, entity_type, before_json, after_json FROM audit_log")
            ).all()
        assert [r.action for r in rows] == ["import.accepted"] * 4
        assert all(r.before_json and r.after_json for r in rows)
        kinds = [json.loads(r.after_json)["file_kind"] for r in rows]
        assert kinds == ["customers", "invoices", "payments", "events"]


def test_write_audit_uses_clock_and_sorted_json(db: Engine) -> None:
    with db.begin() as c:
        write_audit(
            c,
            CLOCK,
            actor="system",
            action="test.action",
            entity_type="thing",
            entity_id="T-1",
            before={"b": 1, "a": 2},
            after=None,
        )
        row = c.execute(text("SELECT * FROM audit_log")).mappings().one()
    assert row["ts"] == "2026-10-31T05:00:00Z"
    assert row["before_json"] == '{"a": 2, "b": 1}'
    assert row["after_json"] is None


def test_audit_rows_stay_append_only(seeded: Engine) -> None:
    with pytest.raises(exc.DatabaseError, match="append-only"), seeded.begin() as c:
        c.execute(text("DELETE FROM audit_log"))


def test_repository_reads_match_the_fixtures(
    seeded: Engine, golden_ledger_inputs: Any, golden_events: list[EventRecord]
) -> None:
    invoices, payments = golden_ledger_inputs
    with seeded.connect() as c:
        assert load_invoices(c) == sorted(invoices, key=lambda i: i.invoice_number)
        assert load_payments(c) == sorted(payments, key=lambda p: p.payment_id)
        assert load_events(c) == sorted(golden_events, key=lambda e: e.event_id)
        customers = load_customers(c)
    assert [cu.customer_id for cu in customers] == ["CUST-A", "CUST-B", "CUST-C"]
    assert customers[0].tin == "1000000000"


def test_golden_numbers_from_the_database(seeded: Engine, golden_results: dict[str, Any]) -> None:
    inputs = load_ledger_inputs(seeded)
    assert all(isinstance(i, InvoiceRecord) for i in inputs.invoices)
    assert all(isinstance(p, PaymentRecord) for p in inputs.payments)
    rows = build_ledger(inputs.invoices, inputs.payments, as_of=AS_OF)
    summary = aging_summary(rows)
    assert summary.total_open == to_fils("193000.00")
    assert summary.overdue_total == to_fils("147000.00")
    config = EngineConfig()
    scored = score_overdue_invoices(
        rows, inputs.events, customer_history(rows, AS_OF, config), AS_OF, config
    )
    order = [i.scored.invoice_number for i in build_queue(scored, config).items]
    assert order == [r["invoice_number"] for r in golden_results["queue_default"]["rows"]]


def test_table_counts(seeded: Engine) -> None:
    with seeded.connect() as c:
        counts = table_counts(c)
    assert {
        k: counts[k]
        for k in ("customers", "invoices", "payments", "events", "imports", "audit_log")
    } == {
        "customers": 3,
        "invoices": 14,
        "payments": 4,
        "events": 2,
        "imports": 4,
        "audit_log": 4,
    }
