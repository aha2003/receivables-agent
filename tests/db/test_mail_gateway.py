"""The sandbox mail gateway (SPEC §18.8): T-MAIL-01 and T-MAIL-02 (D-038). Synthetic data;
nothing can leave the machine."""

import ast
import dataclasses
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import Config, load_config
from receivables.adapters.db.agent_store import DbAgentStore, run_agent_tick
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.mail.sandbox import DELIVERED, REJECTED, SandboxMailGateway
from receivables.seed import seed

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SANDBOX = ROOT / "src" / "receivables" / "adapters" / "mail" / "sandbox.py"
CLOCK = FixedClock(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))  # a Monday, business hours
AS_OF = date(2026, 10, 31)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'mail.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    seed(engine, FIXTURES, CLOCK)
    yield engine
    engine.dispose()


def l2(**changes: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    limits = dataclasses.replace(base.agent.l2, allowlisted_customers=("CUST-A",), **changes)
    return dataclasses.replace(base, agent=dataclasses.replace(base.agent, l2=limits))


def rows(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql))]


def drafts_from_a_run(db: Engine) -> list[dict[str, Any]]:
    run_agent_tick(db, CLOCK, l2(), as_of=AS_OF, autonomy_level=1)
    return rows(db, "SELECT draft_id, customer_id, status FROM drafts ORDER BY draft_id")


# --- T-MAIL-01 ------------------------------------------------------------------------------------


@pytest.mark.spec("T-MAIL-01")
def test_mail_01_an_allowed_l2_send_lands_in_the_sandbox(db: Engine) -> None:
    run_agent_tick(db, CLOCK, l2(), as_of=AS_OF, autonomy_level=2)
    (mail,) = rows(db, "SELECT * FROM mail_outbox")
    assert (mail["status"], mail["gateway"], mail["to_address"]) == (
        DELIVERED, "sandbox", "ap@alpha.example",
    )  # fmt: skip
    (draft,) = rows(db, f"SELECT * FROM drafts WHERE draft_id = '{mail['draft_id']}'")  # noqa: S608
    assert (draft["status"], draft["approved_by"]) == ("approved", "agent")  # D-038 option 3a
    assert draft["approved_at"] == mail["created_at"] == "2026-11-02T06:00:00Z"
    assert (mail["subject"], mail["body"]) == (draft["subject"], draft["body"])


_NETWORK = ("socket", "ssl", "http", "urllib", "smtplib", "imaplib", "poplib", "ftplib",
            "telnetlib", "requests", "httpx", "httpx2", "httpcore", "httpcore2", "aiohttp",
            "anthropic", "asyncio", "subprocess")  # fmt: skip


@pytest.mark.spec("T-MAIL-01")
def test_mail_01_the_sandbox_imports_no_network_client() -> None:
    tree = ast.parse(SANDBOX.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Name) and node.id == "__import__":
            imported.add("__import__")
    bad = [m for m in imported if m == "__import__" or m.split(".")[0] in _NETWORK]
    assert bad == []
    assert imported  # the walk saw the real imports


# --- T-MAIL-02 ------------------------------------------------------------------------------------


@pytest.mark.spec("T-MAIL-02")
@pytest.mark.parametrize("to", ["someone@else.example", "ap@alpha.example.evil", "", "ap"])
def test_mail_02_another_address_is_rejected(db: Engine, to: str) -> None:
    draft = next(d for d in drafts_from_a_run(db) if d["customer_id"] == "CUST-A")
    with db.begin() as c:
        result = SandboxMailGateway(c).send(
            mail_id="M-T", draft_id=draft["draft_id"], to=to, at=CLOCK.now()
        )
    assert (result.status, result.delivered) == (REJECTED, False)
    (mail,) = rows(db, "SELECT status, to_address FROM mail_outbox")
    assert mail == {"status": REJECTED, "to_address": to}


def test_the_stored_address_matches_whatever_its_case(db: Engine) -> None:
    draft = next(d for d in drafts_from_a_run(db) if d["customer_id"] == "CUST-A")
    with db.begin() as c:
        result = SandboxMailGateway(c).send(
            mail_id="M-T", draft_id=draft["draft_id"], to=" AP@Alpha.Example ", at=CLOCK.now()
        )
    assert result.delivered


def test_a_customer_without_an_address_gets_nothing(db: Engine) -> None:
    draft = next(d for d in drafts_from_a_run(db) if d["customer_id"] == "CUST-A")
    with db.begin() as c:
        c.execute(text("UPDATE customers SET contact_email = NULL WHERE customer_id = 'CUST-A'"))
        result = SandboxMailGateway(c).send(
            mail_id="M-T", draft_id=draft["draft_id"], to="", at=CLOCK.now()
        )
    assert result.status == REJECTED


def test_a_rejected_agent_send_is_a_failed_step_and_sends_nothing(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DbAgentStore, "deliver_draft", lambda self, mail_id, **kw: False)
    run_agent_tick(db, CLOCK, l2(), as_of=AS_OF, autonomy_level=2)
    sent = rows(db, "SELECT status, result_json FROM agent_steps WHERE tool = 'send_email'"
                " AND invoice_number = 'INV-003'")  # fmt: skip
    assert sent[0]["status"] == "failed" and "RECIPIENT_REJECTED" in sent[0]["result_json"]
    assert rows(db, "SELECT state FROM cases WHERE invoice_number = 'INV-003'") != [
        {"state": "AWAITING_REPLY"}
    ]
