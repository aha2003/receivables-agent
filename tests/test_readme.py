"""README.md (SPEC §20.1; C1.02; D-044): the setup, controls, synthetic-data notice, known
limitations and demo script are present, and what it tells an evaluator to run or open exists."""

import re
from pathlib import Path

import pytest

from receivables.adapters.config import load_config
from receivables.cli import main

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
    for name in ("SESSION_SECRET", "LLM_MODE", "ANTHROPIC_API_KEY"):
        assert name in README and f"{name}=" in example
    config = load_config(ROOT / "config.yaml")
    assert config.agent.autonomy_level == 1  # "L1 assist (default)"
    assert not config.agent.l2.allowlisted_customers  # "empty by default"
    assert config.mail.gateway == "sandbox" and config.llm.replay is False
    for key in ("agent.autonomy_level", "agent.l2", "agent.l2.kill_switch", "llm.replay"):
        assert key in README


def test_the_demo_files_and_figures_are_real() -> None:
    script = section("Demo script")
    for path in ("fixtures/correspondence.csv", "tests/fixtures/injection/correspondence.csv"):
        assert path in script and (ROOT / path).exists()
    assert "69.4%" in script  # T-RPT-01's CUST-B share
    assert "AED 193,000.00" in section("Quickstart") and "AED 147,000.00" in section("Quickstart")


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
