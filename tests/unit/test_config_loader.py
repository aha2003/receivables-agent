"""config.yaml loader (SPEC §5): strict, no floats, engine values as exact fractions."""

from datetime import time
from fractions import Fraction
from pathlib import Path

import pytest

from receivables.adapters.config import ConfigError, load_config
from receivables.core.config import EngineConfig

ROOT = Path(__file__).resolve().parents[2]


def test_repository_config_matches_the_spec_defaults() -> None:
    config = load_config(ROOT / "config.yaml")
    assert config.engine == EngineConfig()
    assert config.engine.weight_overdue == Fraction(2, 5)
    assert config.app.session_ttl_minutes == 480
    assert config.app.login_rate_limit_per_minute == 5
    assert config.app.base_currency == "AED"
    assert config.app.require_https is True
    assert config.llm.review_confidence_threshold == Fraction(3, 5)
    assert config.drafts.max_words == 140
    assert config.drafts.invoice_number_regex == r"\b[A-Z]{2,6}-\d{2,}\b"


def test_spec_section_5_yaml_block_loads_identically(tmp_path: Path) -> None:
    import re

    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    section = spec[spec.index("## 5. Database schema") : spec.index("## 6. ")]
    block = re.search(r"```yaml\n(.*?)```", section, re.S)
    assert block is not None
    path = tmp_path / "spec.yaml"
    path.write_text(block.group(1), encoding="utf-8")
    assert load_config(path) == load_config(ROOT / "config.yaml")


def test_missing_file_uses_the_spec_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "absent.yaml") == load_config(ROOT / "config.yaml")


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_override_changes_the_engine(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "engine:\n  min_history_invoices: 2\n  queue_size: 5\n"))
    assert config.engine.min_history_invoices == 2
    assert config.engine.queue_size == 5
    assert config.engine.weight_history == Fraction(1, 4)  # untouched keys keep their defaults


@pytest.mark.parametrize(
    "text",
    [
        "engine:\n  unknown_key: 1\n",
        "surprise:\n  x: 1\n",
        "engine:\n  weights: {overdue: 0.5, history: 0.25, size: 0.15,"
        " dispute: 0.10, promise: 0.10}\n",
        "engine:\n  band_high: 20\n",
        "engine:\n  queue_size: 0\n",
        "engine:\n  weights: {overdue: -0.1, history: 0.45, size: 0.45,"
        " dispute: 0.10, promise: 0.10}\n",
        "app:\n  session_ttl_minutes: lots\n",
        "app:\n  base_currency: USD\n",
        "- just\n- a list\n",
        "engine: [1, 2]\n",
        "engine:\n  grace_days: 1e3\n",
        "engine:\n  band_high: abc\n",
        "engine:\n  band_high: 6e1\n",
        "engine:\n  band_high: .inf\n",
        "engine:\n  no_history_fallback_rate: 1.5\n",
        "llm:\n  review_confidence_threshold: 1.5\n",
        "engine: {unclosed\n",
        "engine:\n  queue_size: 5\n  queue_size: 50\n",
        "engine:\n  queue_size: 5\nengine:\n  grace_days: 1\n",
        "llm:\n  max_retries: '1.0'\n",
        "app:\n  session_ttl_minutes: 480.5\n",
        "drafts:\n  max_words: 1e2\n",
        "drafts:\n  invoice_number_regex: '[unclosed'\n",
        "engine:\n  dso_window_days: 30\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_yaml_is_read_without_floats_or_tags(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "engine: !!python/object/apply:os.system ['echo nope']\n"))
    config = load_config(_write(tmp_path, "engine:\n  no_history_fallback_rate: 0.3333\n"))
    assert config.engine.no_history_fallback_rate == Fraction(3333, 10000)


# --- §18.10 agent and mail keys ---


def test_agent_and_mail_defaults() -> None:
    config = load_config(ROOT / "config.yaml")
    agent = config.agent
    assert (agent.autonomy_level, agent.max_cases_per_tick, agent.max_steps_per_case) == (1, 20, 5)
    assert (agent.llm_timeout_seconds, agent.promise_grace_days) == (30, 2)
    l2 = agent.l2
    assert l2.allowlisted_customers == ()
    assert l2.max_total_open_fils == 2_500_000
    assert (l2.min_days_between_contacts, l2.daily_send_cap) == (7, 20)
    assert l2.business_days == ("Mon", "Tue", "Wed", "Thu", "Fri")
    assert l2.business_hours == (time(9, 0), time(17, 0))
    assert l2.timezone == "Asia/Dubai"
    assert l2.kill_switch is False
    assert (config.mail.gateway, config.mail.enabled) == ("sandbox", False)


def test_spec_section_18_10_yaml_block_matches_the_repository_config(tmp_path: Path) -> None:
    import re

    spec = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
    sec5 = spec[spec.index("## 5. Database schema") : spec.index("## 6. ")]
    sec18 = spec[spec.index("### 18.10") : spec.index("### 18.11")]
    blocks = [re.search(r"```yaml\n(.*?)```", s, re.S) for s in (sec5, sec18)]
    assert all(blocks)
    path = tmp_path / "spec.yaml"
    path.write_text("".join(b.group(1) for b in blocks if b), encoding="utf-8")
    assert load_config(path) == load_config(ROOT / "config.yaml")
    assert load_config(path).agent == load_config(tmp_path / "absent.yaml").agent


def test_agent_overrides(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            "agent:\n  autonomy_level: 2\n  l2:\n    allowlisted_customers: [CUST-A, CUST-C]\n"
            "    max_total_open_amount: 1234.50\n    business_days: [Sun, Mon]\n"
            "    business_hours: ['08:30', '23:59']\n    timezone: UTC\n    kill_switch: true\n"
            "mail:\n  gateway: smtp\n  enabled: true\n",
        )
    )
    assert config.agent.autonomy_level == 2
    assert config.agent.max_steps_per_case == 5  # untouched keys keep their defaults
    l2 = config.agent.l2
    assert l2.allowlisted_customers == ("CUST-A", "CUST-C")
    assert l2.max_total_open_fils == 123_450
    assert l2.business_days == ("Sun", "Mon")
    assert l2.business_hours == (time(8, 30), time(23, 59))
    assert (l2.timezone, l2.kill_switch) == ("UTC", True)
    assert (config.mail.gateway, config.mail.enabled) == ("smtp", True)


@pytest.mark.parametrize(
    "text",
    [
        "agent:\n  autonomy_level: 3\n",
        "agent:\n  autonomy_level: -1\n",
        "agent:\n  autonomy_level: '1.0'\n",
        "agent:\n  max_cases_per_tick: 0\n",
        "agent:\n  max_steps_per_case: 0\n",
        "agent:\n  llm_timeout_seconds: 0\n",
        "agent:\n  promise_grace_days: -2\n",
        "agent:\n  unknown: 1\n",
        "agent:\n  l2:\n    unknown: 1\n",
        "agent:\n  l2:\n    allowlisted_customers: [CUST-A, CUST-A]\n",
        "agent:\n  l2:\n    allowlisted_customers: ['']\n",
        "agent:\n  l2:\n    allowlisted_customers: CUST-A\n",
        "agent:\n  l2:\n    max_total_open_amount: 0\n",
        "agent:\n  l2:\n    max_total_open_amount: -5\n",
        "agent:\n  l2:\n    max_total_open_amount: 10.005\n",
        "agent:\n  l2:\n    max_total_open_amount: 2.5e4\n",
        "agent:\n  l2:\n    max_total_open_amount: 25,000\n",
        "agent:\n  l2:\n    min_days_between_contacts: -1\n",
        "agent:\n  l2:\n    daily_send_cap: 2.5\n",
        "agent:\n  l2:\n    business_days: []\n",
        "agent:\n  l2:\n    business_days: [Monday]\n",
        "agent:\n  l2:\n    business_days: [Mon, Mon]\n",
        "agent:\n  l2:\n    business_hours: ['17:00', '09:00']\n",
        "agent:\n  l2:\n    business_hours: ['09:00', '09:00']\n",
        "agent:\n  l2:\n    business_hours: ['9:00', '17:00']\n",
        "agent:\n  l2:\n    business_hours: ['09:00', '24:00']\n",
        "agent:\n  l2:\n    business_hours: ['09:00']\n",
        "agent:\n  l2:\n    timezone: Mars/Olympus\n",
        "agent:\n  l2:\n    timezone: ../../etc/passwd\n",
        "agent:\n  l2:\n    kill_switch: maybe\n",
        "mail:\n  gateway: sendgrid\n",
        "mail:\n  enabled: perhaps\n",
        "mail:\n  to: someone@example.com\n",
    ],
)
def test_invalid_agent_and_mail_config_is_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))
