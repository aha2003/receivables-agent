"""Shared test helpers: loaders for the synthetic golden fixtures (SPEC §6.8 to §6.10)."""

import csv
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.money import to_fils
from receivables.core.scoring import EventRecord

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def read_fixture_csv(name: str) -> list[dict[str, str]]:
    """Rows of ``fixtures/<name>`` as raw strings (validation belongs to the import layer)."""
    with (FIXTURES / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def load_fixture_csv() -> Callable[[str], list[dict[str, str]]]:
    return read_fixture_csv


@pytest.fixture
def golden_results() -> dict[str, Any]:
    with (FIXTURES / "expected" / "golden_results.json").open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def golden_ledger_inputs() -> tuple[list[InvoiceRecord], list[PaymentRecord]]:
    """The §6.8 golden invoices and payments as core records (the real import is S1.09)."""
    invoices = [
        InvoiceRecord(
            invoice_number=r["invoice_number"],
            customer_id=r["customer_id"],
            invoice_date=date.fromisoformat(r["invoice_date"]),
            due_date=date.fromisoformat(r["due_date"]),
            amount=to_fils(r["amount"]),
            po_number=r["po_number"] or None,
        )
        for r in read_fixture_csv("invoices.csv")
    ]
    payments = [
        PaymentRecord(
            payment_id=r["payment_id"],
            invoice_number=r["invoice_number"] or None,
            customer_id=r["customer_id"] or None,
            payment_date=date.fromisoformat(r["payment_date"]),
            amount=to_fils(r["amount"]),
            payment_type=r["payment_type"],
        )
        for r in read_fixture_csv("payments.csv")
    ]
    return invoices, payments


@pytest.fixture
def golden_events() -> list[EventRecord]:
    """The §6.8 golden events as core records."""
    return [
        EventRecord(
            event_id=r["event_id"],
            invoice_number=r["invoice_number"],
            event_type=r["event_type"],
            event_date=date.fromisoformat(r["event_date"]),
            label=r["label"] or None,
            promise_date=date.fromisoformat(r["promise_date"]) if r["promise_date"] else None,
        )
        for r in read_fixture_csv("events.csv")
    ]
