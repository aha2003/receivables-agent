"""Simulation pages (SPEC §19, §10; S4.08; D-056): the label on every simulation view, the A0 to
A3 comparison, SIMULATED capture rates (T-SHD-01's UI part), the assumptions, a run from the
page, and the 14-day fast-forward. The simulator writes to a temporary folder, never data/."""

import hashlib
import html
import json
import re
import sqlite3
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from receivables.adapters.config import load_config
from receivables.sim.runner import fast_forward, run_arms
from receivables.sim.scenario import load_scenario

from .conftest import csrf, login

ROOT = Path(__file__).resolve().parents[2]
LABEL = "SIMULATION — MECHANICS, NOT EVIDENCE"
CONFIG = load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def results_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Seed 42 run once for the module (about ten seconds)."""
    folder = tmp_path_factory.mktemp("sim")
    run_arms(load_scenario(), folder, f"sqlite:///{folder / 'unrelated.db'}", CONFIG)
    return folder


@pytest.fixture
def user(client: TestClient, tmp_path: Path) -> TestClient:
    client.app.state.sim_data_dir = tmp_path  # type: ignore[attr-defined]
    assert login(client) == 303
    return client


def text_of(page: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))


def section(page: str, name: str) -> str:
    match = re.search(rf'<section id="{name}">(.*?)</section>', page, re.S)
    assert match, name
    return text_of(match.group(1))


def assert_labelled(page: str) -> None:
    assert f'<div role="status" class="banner sim">{LABEL}</div>' in page  # under the top banner
    assert f"· {LABEL}" in re.search(r"<title>(.*?)</title>", page, re.S).group(1)  # type: ignore[union-attr]
    for claim in ("uplift", "forecast", "guarantee", "roi", "proves"):
        assert not re.search(rf"\b{claim}\b", text_of(page).lower()), claim


def test_the_page_is_labelled_without_results(user: TestClient) -> None:
    page = user.get("/simulation").text
    assert_labelled(page)
    assert 'id="no-results"' in page and "No results for seed 42 yet" in page
    assert '<a href="/simulation">Simulation</a>' in page
    assumptions = section(page, "assumptions")
    for archetype in ("Responsive [A]", "Disputer [A]", "Silent [A]", "Claims paid [A]"):
        assert archetype in assumptions
    assert "No LLM is used in the simulation" in assumptions


def test_the_comparison_matches_the_results(user: TestClient, results_dir: Path) -> None:
    user.app.state.sim_data_dir = results_dir  # type: ignore[attr-defined]
    page = user.get("/simulation?seed=42").text
    assert_labelled(page)
    results = json.loads((results_dir / "sim_42_results.json").read_text(encoding="utf-8"))
    comparison = section(page, "comparison")
    assert "As of 2027-01-01 (day 60), seed 42." in comparison
    for arm, m in results["arms"].items():
        row = re.search(rf'<tr id="arm-{arm}">(.*?)</tr>', page, re.S)
        assert row, arm
        cells = text_of(row.group(1))
        amount = f"AED {Decimal(m['overdue_amount']):,}"  # the §10 format, from the exact text
        assert amount in cells and m["mean_days_past_due_at_payment"] in cells
        assert str(m["messages_sent"]) in cells and str(m["approvals_needed"]) in cells
    assumptions = section(page, "assumptions")
    assert "a1 reminder min gap days" in assumptions and "rules (no LLM" in assumptions


@pytest.mark.spec("T-SHD-01")
def test_the_ui_labels_the_capture_rates_simulated(user: TestClient, results_dir: Path) -> None:
    user.app.state.sim_data_dir = results_dir  # type: ignore[attr-defined]
    page = user.get("/simulation?seed=42").text
    capture = section(page, "capture")
    pooled = json.loads((results_dir / "sim_42_results.json").read_text())["shadow"]["pooled"]
    assert "Capture rate: SIMULATED" in capture and "Mechanics only" in capture
    for name in ("agent", "oldest_first", "largest_first"):
        assert f"{pooled[name]} SIMULATED" in capture  # every pooled rate carries the label
    headers = re.findall(r"<th[^>]*>([^<]*)</th>", page.split('id="capture"')[1])
    assert {"Agent (SIMULATED)", "Oldest-first (SIMULATED)", "Largest-first (SIMULATED)"} <= set(
        headers
    )


def test_a_run_from_the_page_writes_the_seeds_results_and_not_the_real_database(
    user: TestClient, engine: Engine, tmp_path: Path
) -> None:
    real = Path(engine.url.database or "")
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    refused = user.post("/simulation/run", data={"seed": "7"})  # no CSRF token
    assert refused.status_code == 403
    done = user.post("/simulation/run", data={"seed": "7", "csrf_token": csrf(user)})
    assert done.status_code == 303 and done.headers["location"] == "/simulation?seed=7"
    assert (tmp_path / "sim_7_results.json").is_file()
    page = user.get("/simulation?seed=7").text
    assert_labelled(page)
    assert 'id="comparison"' in page and "seed 7" in section(page, "comparison")
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before


def test_one_simulation_at_a_time(user: TestClient) -> None:
    from receivables.api.routes import simulation

    assert simulation._running.acquire(blocking=False)
    try:
        busy = user.post("/simulation/run", data={"seed": "7", "csrf_token": csrf(user)})
        assert busy.status_code == 409 and "already running" in busy.text
        assert_labelled(busy.text)
        assert "disabled" in user.get("/simulation").text
    finally:
        simulation._running.release()


def test_the_fast_forward_shows_the_windows_changes(
    user: TestClient, engine: Engine, tmp_path: Path, results_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    scratch = tmp_path / "system-temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))  # where the replay's folder goes
    real = Path(engine.url.database or "")
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    page = user.get("/simulation/fast-forward?seed=42&arm=A2&day=14").text
    assert_labelled(page)
    assert "Fast-forward: A2, days 14 to 28" in text_of(page)
    # Independent check: the full A2 run's database holds the same payments, dated in
    # (day 14, day 28], as the replay (a seed always replays the same way, T-SIM-01).
    start = load_scenario().start_date
    low, high = start + timedelta(days=14), start + timedelta(days=28)
    full = sqlite3.connect(results_dir / "sim_42_a2.db")
    try:
        rows = full.execute("SELECT payment_id, payment_date FROM payments").fetchall()
        expected = {pid for pid, day in rows if low < date.fromisoformat(day) <= high}
    finally:
        full.close()
    shown = set(re.findall(r"<tr><td>(SIM-[A-Z0-9-]+)</td>", page.split('id="payments"')[1]
                           .split("</section>")[0]))  # fmt: skip
    assert expected and shown == expected
    for name in ("outcomes", "playbooks", "followups", "states"):
        assert re.search(r"\((\d+)\)", section(page, name))
    assert "day=28" in page and "day=0" in page  # next and previous windows
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(("sim_", "sim-ff-"))]
    assert leftovers == []  # nothing in the data folder ...
    assert list(scratch.iterdir()) == []  # ... and its temporary folder was deleted
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before


def test_the_first_window_starts_before_day_0(user: TestClient) -> None:
    page = user.get("/simulation/fast-forward?seed=42&arm=A2&day=0").text
    assert_labelled(page)
    assert "the start of 2026-11-02 (before day 0" in text_of(page)


def test_a_refused_run_shows_a_fixed_message_and_no_path(user: TestClient, tmp_path: Path) -> None:
    (tmp_path / "sim_7_a0.db").symlink_to(tmp_path / "elsewhere.db")
    refused = user.post("/simulation/run", data={"seed": "7", "csrf_token": csrf(user)})
    assert refused.status_code == 409
    assert_labelled(refused.text)
    assert "The simulator refused to write its files" in refused.text
    assert str(tmp_path) not in refused.text and "sim_7_a0" not in refused.text
    assert not (tmp_path / "sim_7_results.json").exists()


def test_the_last_window_is_short_and_has_no_next(user: TestClient) -> None:
    page = user.get("/simulation/fast-forward?seed=42&arm=A3&day=56").text
    assert_labelled(page)
    assert "days 56 to 60" in text_of(page) and "Next 14 days" not in page


@pytest.mark.parametrize(
    "query",
    [
        "arm=A9",
        "day=7",
        "day=60",
        "day=-14",
        "seed=x",
        "seed=4.2",
        "day=%D9%A1%D9%A4",
        "seed=" + "9" * 5000,
        "day=" + "0" * 5000,
    ],
)
def test_the_fast_forward_refuses_bad_input(user: TestClient, query: str) -> None:
    assert user.get(f"/simulation/fast-forward?{query}").status_code == 422


def test_simulation_pages_need_a_session(client: TestClient) -> None:
    for path in ("/simulation", "/simulation/fast-forward"):
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == 303 and response.headers["location"] == "/login", path
        assert client.get(path).status_code == 401  # not a browser: the §9 error instead
    assert client.post("/simulation/run", data={"seed": "1"}).status_code in (401, 403)


def test_the_label_stays_in_view_while_scrolling(client: TestClient) -> None:
    css = client.get("/static/css/app.css").text
    rule = re.search(r"\.banner\.sim \{([^}]*)\}", css)
    assert rule and "position: sticky" in rule.group(1) and "top: 0" in rule.group(1)


def test_the_fast_forward_refuses_a_bad_window_directly(tmp_path: Path) -> None:
    for arm, day in (("A9", 0), ("A2", 7), ("A2", 60), ("A2", -14)):
        with pytest.raises(ValueError):
            fast_forward(load_scenario(), tmp_path, f"sqlite:///{tmp_path / 'x.db'}", CONFIG,
                         arm=arm, start_day=day)  # fmt: skip
    assert list(tmp_path.iterdir()) == []  # refused before anything is written
