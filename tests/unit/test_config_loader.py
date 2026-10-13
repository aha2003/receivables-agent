"""config.yaml loader (SPEC §5): strict, no floats, engine values as exact fractions."""

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
