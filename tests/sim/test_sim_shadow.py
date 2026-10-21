"""Capture-rate mechanics (SPEC §19.3; S4.07; D-054): T-SHD-01, labelled SIMULATED."""

import ast
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from receivables.sim.shadow import (
    LIST_SIZE,
    SIMULATED,
    OverdueInvoice,
    capture_rate,
    largest_first,
    oldest_first,
    pooled_rate,
    rate_text,
)

ROOT = Path(__file__).resolve().parents[2]
WEEK = {"A": 1_000_000, "B": 0, "C": 500_000}  # simulated payments: A 10,000 and C 5,000


@pytest.mark.spec("T-SHD-01")
def test_the_toy_example() -> None:
    # Overdue at week start: A 10,000, B 20,000, C 5,000.
    agent = capture_rate(["A", "B"], WEEK)
    oldest = capture_rate(["C", "B"], WEEK)
    assert (agent, oldest) == (Fraction(2, 3), Fraction(1, 3))
    assert (rate_text(agent), rate_text(oldest)) == ("0.6667", "0.3333")
    assert SIMULATED == "SIMULATED"


def test_the_baselines_rank_by_due_date_and_by_open_amount() -> None:
    overdue = [
        OverdueInvoice("A", date(2026, 10, 1), 1_000_000),
        OverdueInvoice("B", date(2026, 9, 20), 2_000_000),
        OverdueInvoice("C", date(2026, 9, 1), 500_000),
    ]
    assert oldest_first(overdue, 2) == ("C", "B")  # T-SHD-01's oldest-first list
    assert largest_first(overdue, 2) == ("B", "A")
    assert capture_rate(largest_first(overdue, 2), WEEK) == Fraction(2, 3)
    many = [OverdueInvoice(f"I{n:02d}", date(2026, 9, 1), n) for n in range(30)]
    assert len(oldest_first(many)) == len(largest_first(many)) == LIST_SIZE == 20


def test_no_payments_means_no_rate_and_pooling_weights_by_amount() -> None:
    assert capture_rate(["A"], {"A": 0, "B": 0}) is None and rate_text(None) is None
    assert pooled_rate([(1, 3), (2, 2)]) == Fraction(3, 5)  # not the mean of 1/3 and 1
    assert pooled_rate([(0, 0)]) is None


def test_the_shadow_module_is_pure() -> None:
    tree = ast.parse((ROOT / "src/receivables/sim/shadow.py").read_text(encoding="utf-8"))
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    allowed = {"collections.abc", "dataclasses", "datetime", "fractions",
               "receivables.core.scoring"}  # fmt: skip
    assert modules <= allowed, modules


def test_a_scenario_starting_on_a_weekend_still_gets_its_weekly_l0_runs(
    tmp_path: Path,
) -> None:
    from receivables.adapters.config import load_config
    from receivables.sim.runner import run_arms
    from receivables.sim.scenario import load_scenario

    sunday = load_scenario().model_copy(update={"start_date": date(2026, 11, 1), "days": 14})
    run = run_arms(sunday, tmp_path, f"sqlite:///{tmp_path / 'real.db'}",
                   load_config(ROOT / "config.yaml"))  # fmt: skip
    starts = [w["week_start"] for w in run.results["shadow"]["weeks"]]
    assert starts == ["2026-11-01", "2026-11-08"]  # Sundays; no empty week on the last day
