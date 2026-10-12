"""Shared test helpers: loaders for the synthetic golden fixtures (SPEC §6.8 to §6.10)."""

import csv
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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
