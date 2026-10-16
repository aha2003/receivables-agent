"""README.md (SPEC §20.1; C1.02; D-044): the setup, controls, synthetic-data notice, known
limitations and demo script are present, and what it tells an evaluator to run or open exists."""

import json
import re
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.config import load_config
from receivables.adapters.db.allocations import allocate_payment
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import import_csv
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.llm.mode import LlmModeConflict, resolve_llm_mode
from receivables.cli import main
from receivables.core.scoring import round_half_up
from receivables.seed import seed
from receivables.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}.*?$(.*?)(?=^## |\Z)", README, re.M | re.S)
    assert match, title
    return match.group(1)


@pytest.mark.parametrize(
    "title",
    ["Quickstart", "LLM modes", "Controls", "Demo script", "Known limitations", "What it does"],
)
def test_the_sections_of_20_1_are_there(title: str) -> None:
    assert section(title).strip()


def test_the_synthetic_data_notice_leads_the_readme() -> None:
    head = README.split("## Progress")[0]
    assert "**Synthetic data only.**" in head
    assert "mechanics, not evidence" in head
    assert "Nothing is ever emailed" in head


def test_every_repository_path_it_names_exists() -> None:
    paths = {
        p.rstrip("/")
        for p in re.findall(
            r"`((?:src|docs|tests|fixtures|scripts)/[^`\s]+|[A-Z_]+\.md|"
            r"config\.yaml|\.env\.example)`",
            README,
        )
    }
    assert paths  # the README does name files
    missing = sorted(p for p in paths if not (ROOT / p).exists())
    assert missing == []


def test_every_cli_command_it_shows_exists() -> None:
    commands = re.findall(r"uv run receivables ([a-z-]+)", README)
    assert {"reset", "create-user"} <= set(commands)
    for command in commands:
        with pytest.raises(SystemExit) as exit_:
            main([command, "--help"])
        assert exit_.value.code == 0, command  # an unknown subcommand exits 2


def test_the_run_command_is_the_one_in_claude_md() -> None:
    run = "uv run --env-file .env uvicorn receivables.api.main:app --reload --no-proxy-headers"
    assert run in README and run in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def test_the_settings_it_names_are_real() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("SESSION_SECRET", "LLM_MODE", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL_PLANNER",
                 "ANTHROPIC_MODEL_CLASSIFIER", "ANTHROPIC_MODEL_DRAFTER"):  # fmt: skip
        assert f"{name}=" in example
    assert "SESSION_SECRET" in README and "ANTHROPIC_MODEL_*" in README
    config = load_config(ROOT / "config.yaml")
    assert config.agent.autonomy_level == 1  # "L1 assist (default)"
    assert not config.agent.l2.allowlisted_customers  # "empty by default"
    assert config.mail.gateway == "sandbox" and config.llm.replay is False
    for key in (
        "agent.autonomy_level",
        "agent.l2.kill_switch",
        "llm.replay",
        "allowlisted_customers",
    ):
        assert key in README


def test_replay_needs_llm_replay_in_the_config_as_it_says() -> None:
    config = load_config(ROOT / "config.yaml")  # llm.replay: false
    with pytest.raises(LlmModeConflict):
        resolve_llm_mode(Settings(llm_mode="replay"), config)
    assert "also set `llm.replay: true`" in README


def test_the_demo_files_and_figures_are_real() -> None:
    script = section("Demo script")
    for path in ("fixtures/correspondence.csv", "tests/fixtures/injection/correspondence.csv",
                 "tests/fixtures/demo/unallocated_payment.csv"):  # fmt: skip
        assert path in script and (ROOT / path).exists()
    golden = json.loads((ROOT / "fixtures" / "expected" / "golden_results.json").read_text())
    quick = section("Quickstart")
    assert f"AED {Decimal(golden['total_open']):,}" in quick  # AED 193,000.00
    assert f"AED {Decimal(golden['total_overdue']):,}" in quick
    top = golden["overdue_by_customer"][0]
    share = round_half_up(Fraction(Decimal(top["share_pct"])), 1)
    assert f"{share}%" in script  # 69.4%, the report's one-decimal share


def test_the_demo_payment_can_be_allocated_to_inv_008(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 10, 31, 10, 0, tzinfo=DUBAI))
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    try:
        seed(engine, ROOT / "fixtures", clock)
        raw = (ROOT / "tests" / "fixtures" / "demo" / "unallocated_payment.csv").read_bytes()
        assert import_csv(engine, "payments", raw, filename="p.csv", clock=clock).status == (
            "accepted"
        )
        result = allocate_payment(engine, "P-011", "INV-008", clock=clock, actor="demo")
        assert result["invoice_open_amount"] == "0.00"
    finally:
        engine.dispose()


def test_known_limitations_link_real_decisions() -> None:
    limits = section("Known limitations")
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    cited = re.findall(r"\[(D-\d{3})\]\(docs/DECISIONS\.md\)", limits)
    assert len(cited) >= 6
    assert all(f"| {d} |" in decisions for d in cited)


def test_no_claim_of_business_value() -> None:
    text = README.lower()
    for claim in ("proves", "guarantee", "increases collections", "reduces dso", "roi"):
        assert not re.search(rf"\b{claim}\b", text), claim
    assert "not evidence of business value" in text
