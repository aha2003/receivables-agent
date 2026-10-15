"""Shared test helpers: loaders for the synthetic golden fixtures (SPEC §6.8 to §6.10), and the
suite-wide guards: no network (``tests/network_guard.py``) and no LLM environment (D-029)."""

import csv
import json
import os
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import network_guard  # tests/network_guard.py; tests/ is on sys.path (rootdir conftest)
import pytest

from receivables.agent.facts import AgentFacts, MessageHeader, PlaybookRecord, build_agent_facts
from receivables.core.config import EngineConfig
from receivables.core.ledger import InvoiceRecord, PaymentRecord
from receivables.core.money import to_fils
from receivables.core.scoring import EventRecord
from receivables.core.validation import CustomerRow

ROOT = Path(__file__).resolve().parents[1]


def pytest_configure(config: pytest.Config) -> None:
    """The anthropic SDK reads ANTHROPIC_LOG once, when imported; nothing has imported it yet,
    so removing it here keeps its own debug logging off for the whole run (D-032)."""
    assert "anthropic" not in sys.modules, "the SDK was imported before conftest could clean up"
    os.environ.pop("ANTHROPIC_LOG", None)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Block the network before any test module is collected or imported (D-029)."""
    network_guard.install()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if any test reached for the network, even if the error was swallowed."""
    attempts = network_guard.unexpected()
    network_guard.uninstall()
    if attempts:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print(f"\nnetwork guard: {len(attempts)} blocked network attempt(s): {attempts[:5]}")


# Variables that could point a test at the live LLM; removed for every test (D-029).
LLM_ENVIRONMENT = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL_PLANNER",
    "ANTHROPIC_MODEL_CLASSIFIER",
    "ANTHROPIC_MODEL_DRAFTER",
    "LLM_MODE",
    "LLM_REPLAY",
    # Read by the anthropic SDK itself (D-032): none may redirect, authenticate or log a call.
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_LOG",
    "ANTHROPIC_WEBHOOK_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def _no_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever the shell exports, tests start in fake mode with no API key or model ids."""
    for name in LLM_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


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


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """Raw text of ``fixtures/<name>`` (for the CSV validator, which parses text)."""
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")


def golden_customers() -> list[CustomerRow]:
    return [
        CustomerRow(
            customer_id=r["customer_id"],
            customer_name=r["customer_name"],
            trn=r["trn"],
            tin=r["trn"][:10],
            trade_license_no=r["trade_license_no"],
            payment_terms_days=int(r["payment_terms_days"]),
            contact_email=r["contact_email"],
        )
        for r in read_fixture_csv("customers.csv")
    ]


MakeFacts = Callable[..., AgentFacts]


@pytest.fixture
def make_agent_facts(
    golden_ledger_inputs: tuple[list[InvoiceRecord], list[PaymentRecord]],
    golden_events: list[EventRecord],
) -> MakeFacts:
    """Agent facts over the golden data (default ``as_of`` 2026-10-31), with optional extras."""

    def make(
        *,
        as_of: date = date(2026, 10, 31),
        extra_payments: tuple[PaymentRecord, ...] = (),
        extra_events: tuple[EventRecord, ...] = (),
        playbooks: tuple[PlaybookRecord, ...] = (),
        messages: tuple[MessageHeader, ...] = (),
    ) -> AgentFacts:
        invoices, payments = golden_ledger_inputs
        return build_agent_facts(
            customers=golden_customers(),
            invoices=invoices,
            payments=[*payments, *extra_payments],
            events=[*golden_events, *extra_events],
            playbooks=playbooks,
            messages=messages,
            as_of=as_of,
            config=EngineConfig(),
        )

    return make
