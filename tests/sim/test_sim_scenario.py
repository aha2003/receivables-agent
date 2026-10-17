"""The simulator's scenario config (SPEC §19.2; S4.01): strict, and §19.2's values by default."""

import json
from datetime import date, time
from pathlib import Path

import pytest
from pydantic import ValidationError

from receivables.sim.scenario import (
    DEFAULT_SCENARIO,
    Archetype,
    Scenario,
    load_scenario,
)


def test_the_default_scenario_is_the_one_in_19_2() -> None:
    s = load_scenario()
    assert s.seed == 42
    assert s.start_date == date(2026, 11, 2) and s.start_date.weekday() == 0  # a Monday
    assert s.days == 60
    assert s.tick_time == time(10, 0) and s.timezone == "Asia/Dubai"
    assert s.working_days == ("Mon", "Tue", "Wed", "Thu", "Fri")
    assert s.customers == 12 and s.invoices_per_customer == 5
    assert s.payment_terms_days == 30
    assert (s.amount_step_aed, s.amount_units_min, s.amount_units_max) == (100, 50, 600)
    assert (s.due_offset_min_days, s.due_offset_max_days) == (0, 30)
    assert s.archetypes == (
        Archetype.RESPONSIVE,
        Archetype.DISPUTER,
        Archetype.SILENT,
        Archetype.CLAIMS_PAID,
    )
    assert s.customers_per_archetype == 3


def test_the_seed_can_be_overridden() -> None:
    assert load_scenario(seed=7).seed == 7
    assert load_scenario(seed=7).model_copy(update={"seed": 42}) == load_scenario()


def _raw() -> dict[str, object]:
    return json.loads(DEFAULT_SCENARIO.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "change",
    [
        {"unknown_field": 1},
        {"seed": "42"},  # no coercion
        {"seed": -1},
        {"days": 0},
        {"customers": 11},  # must equal archetypes x customers_per_archetype
        {"timezone": "UTC"},
        {"working_days": ["Mon", "Funday"]},
        {"working_days": []},
        {"amount_units_min": 700},  # min above max
        {"due_offset_min_days": 31},  # min above max
        {"archetypes": ["responsive", "responsive", "silent", "claims_paid"]},
        {"payment_terms_days": 0},
        {"start_date": "2026-13-01"},
    ],
)
def test_a_bad_scenario_is_refused(tmp_path: Path, change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        load_scenario(_write(tmp_path, _raw() | change))


def test_a_bad_seed_override_is_refused() -> None:
    with pytest.raises(ValidationError):
        load_scenario(seed=-3)


def test_the_scenario_is_immutable() -> None:
    s = load_scenario()
    with pytest.raises(ValidationError):
        s.seed = 1  # type: ignore[misc]
    assert isinstance(s, Scenario)
