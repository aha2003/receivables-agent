"""Simulation pages (SPEC §19, §10; S4.08; D-056). SIMULATION — MECHANICS, NOT EVIDENCE.

``/simulation`` shows the seed's A0 to A3 comparison, the SIMULATED capture rates and the
assumptions, and runs the simulator for a seed (the seed is the only control: D-056 option 2a).
``/simulation/fast-forward`` replays one arm and shows what changed in a 14-day window (option
1a). Both write only the simulator's own files in the data folder, never the real database, and
one simulation runs at a time. Every simulation view carries the label.
"""

import json
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.api.routes.pages import render
from receivables.sim.runner import ARMS, WINDOW_DAYS, fast_forward, results_path, run_arms
from receivables.sim.scenario import load_scenario
from receivables.sim.simulator import LABEL, SimRefused

router = APIRouter()

MAX_SEED = 2**31 - 1
DEFAULT_ARM = "A2"  # the agent at L1, the product's default level (option 3a)
ARM_TEXT = {
    "A0": "No action: customers follow their no-contact behaviour.",
    "A1": "Rules-only reminders every 7 days (tone by age), sent by a person the next working "
    "day; no dispute handling, internal requests or escalation.",
    "A2": "The agent at L1 (rule planner, templates); a simulated person approves and sends "
    "drafts the next working day.",
    "A3": "The agent at L2 with every simulated customer allowlisted; denied sends are approved "
    "by the simulated person.",
}
ARCHETYPE_TEXT = (
    ("Responsive", "After the first reminder, pays in full 2, 3 or 4 days later; with no "
     "reminder, 25 days after the due date."),
    ("Disputer", "Replies with a dispute 2 days after the first reminder; pays 3 days after the "
     "dispute is resolved and a response is sent, otherwise 60 days after the due date."),
    ("Silent", "Never replies; pays 35 days after the due date, or 10 days after escalation."),
    ("Claims paid", "Holds unallocated cash equal to its oldest invoice and replies ALREADY_PAID "
     "2 days after the first reminder; that invoice closes when the cash is allocated; with no "
     "contact, pays 45 days after the due date."),
)  # fmt: skip
BUSY = "A simulation is already running. Try again shortly."
_running = threading.Lock()  # one simulation at a time: runs share the seed's files


def _seed(raw: str | None) -> int:
    if raw is None or raw == "":
        return load_scenario().seed
    if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_SEED:
        raise HTTPException(422, "seed must be a whole number from 0")
    return int(raw)


def _data_dir(request: Request) -> Path:
    return Path(request.app.state.sim_data_dir)


def _real_database_url(request: Request) -> str:
    return request.app.state.engine.url.render_as_string(hide_password=False)


def _results(data_dir: Path, seed: int) -> dict[str, Any] | None:
    path = results_path(data_dir, seed)
    if not path.is_file():
        return None
    results = json.loads(path.read_text(encoding="utf-8"))
    return results if results.get("label") == LABEL and results.get("seed") == seed else None


async def _sim_render(request: Request, template: str, context: dict[str, Any],
                      status: int = 200) -> Response:  # fmt: skip
    return await render(request, template, {"sim_label": LABEL} | context, status=status)


@router.get("/simulation", response_class=HTMLResponse)
async def simulation_page(request: Request, seed: str | None = None) -> Response:
    chosen = _seed(seed)
    scenario = load_scenario(seed=chosen)
    results = await run_in_threadpool(_results, _data_dir(request), chosen)
    context = {
        "seed": chosen,
        "scenario": scenario,
        "results": results,
        "arms": ARMS,
        "arm_text": ARM_TEXT,
        "archetypes": ARCHETYPE_TEXT,
        "windows": list(range(0, scenario.days, WINDOW_DAYS)),
        "default_arm": DEFAULT_ARM,
        "busy": _running.locked(),
    }
    return await _sim_render(request, "simulation.html", context)


async def _refused(request: Request, detail: str, seed: int) -> Response:
    context = {"detail": detail, "back": f"/simulation?seed={seed}"}
    return await _sim_render(request, "refused.html", context, status=409)


def _locked(work: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``work`` while holding the simulation lock; None when another one is running."""
    if not _running.acquire(blocking=False):
        return None
    try:
        return work(*args, **kwargs)
    finally:
        _running.release()


@router.post("/simulation/run", response_class=HTMLResponse)
async def simulation_run(request: Request, seed: Annotated[str | None, Form()] = None) -> Response:
    state = request.app.state
    chosen = _seed(seed)
    try:
        done = await run_in_threadpool(
            _locked, run_arms, load_scenario(seed=chosen), _data_dir(request),
            _real_database_url(request), state.config,
        )  # fmt: skip
    except SimRefused as error:
        return await _refused(request, str(error), chosen)
    if done is None:
        return await _refused(request, BUSY, chosen)
    return RedirectResponse(f"/simulation?seed={chosen}", status_code=303)


@router.get("/simulation/fast-forward", response_class=HTMLResponse)
async def simulation_fast_forward(
    request: Request, seed: str | None = None, arm: str = DEFAULT_ARM, day: str = "0"
) -> Response:
    chosen = _seed(seed)
    scenario = load_scenario(seed=chosen)
    if arm not in ARMS:
        raise HTTPException(422, "arm must be A0, A1, A2 or A3")
    if not (day.isascii() and day.isdigit()) or int(day) % WINDOW_DAYS or int(day) >= scenario.days:
        raise HTTPException(422, f"day must be 0, {WINDOW_DAYS}, {2 * WINDOW_DAYS} ... before "
                                 f"day {scenario.days}")  # fmt: skip
    try:
        window = await run_in_threadpool(
            _locked, fast_forward, scenario, _data_dir(request), _real_database_url(request),
            request.app.state.config, arm=arm, start_day=int(day),
        )  # fmt: skip
    except SimRefused as error:
        return await _refused(request, str(error), chosen)
    if window is None:
        return await _refused(request, BUSY, chosen)
    context = {
        "window": window,
        "seed": chosen,
        "arms": ARMS,
        "next_day": window["end_day"] if window["end_day"] < scenario.days else None,
        "previous_day": window["start_day"] - WINDOW_DAYS if window["start_day"] else None,
    }
    return await _sim_render(request, "sim_fast_forward.html", context)
