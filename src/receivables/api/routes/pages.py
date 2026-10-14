"""Server-rendered pages (SPEC §10, §18): dashboard, queue, upload, case, approvals and runs.

Pages use the same snapshot as the JSON API, so numbers match exactly. Every page shows the
SYNTHETIC DEMO DATA banner and the ``as_of`` of its numbers; htmx only enhances links and
forms that also work without JavaScript.
"""

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.controls import kill_switch_on
from receivables.api.agent_views import case_runs, pending_approvals, run_trace
from receivables.api.csrf import ensure_token
from receivables.api.imports import ImportRefusal, run_import
from receivables.api.invoice_detail import invoice_detail
from receivables.api.params import parse_as_of
from receivables.api.snapshot import data_quality, dso_formula, take_snapshot
from receivables.core.aging import BUCKETS, aging_summary
from receivables.core.queue import HEURISTIC_NOTE
from receivables.core.scoring import BANDS
from receivables.core.validation import FILE_KINDS

router = APIRouter()


def _kill_switch(request: Request) -> bool:
    state = request.app.state
    with state.engine.connect() as conn:
        return kill_switch_on(conn, state.config)


async def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> Response:
    base = {
        "user": request.state.user,
        "csrf_token": ensure_token(request.session),
        "heuristic_note": HEURISTIC_NOTE,
        "kill_switch": await run_in_threadpool(_kill_switch, request),
        "autonomy_level": request.app.state.config.agent.autonomy_level,
    }
    return request.app.state.templates.TemplateResponse(
        request, template, base | context, status_code=status
    )


async def _snapshot(request: Request, as_of: str | None) -> Any:
    state = request.app.state
    day = parse_as_of(request, as_of)
    return await run_in_threadpool(take_snapshot, state.engine, day, state.config.engine)


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, as_of: str | None = None) -> Response:
    snap = await _snapshot(request, as_of)
    buckets = aging_summary(snap.rows).bucket_totals
    largest = max(buckets.values(), default=0)
    bars = [
        {
            "name": name,
            "amount": buckets[name],
            "percent": (100 * buckets[name] // largest) if largest else 0,
        }
        for name in BUCKETS
    ]
    context = {
        "snap": snap,
        "kpis": snap.kpis,
        "bars": bars,
        "bands": BANDS,
        "dso_formula": dso_formula(snap.config.dso_window_days),
        "quality": data_quality(snap),
        "top": snap.kpis.overdue_by_customer[:10],
    }
    return await _render(request, "dashboard.html", context)


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(
    request: Request, as_of: str | None = None, band: str | None = None, customer: str | None = None
) -> Response:
    snap = await _snapshot(request, as_of)
    band = band if band in BANDS else None
    customer = customer if customer in snap.customers else None
    items = [
        i
        for i in snap.queue.items
        if (band is None or i.scored.band == band)
        and (customer is None or i.scored.customer_id == customer)
    ]
    context = {"snap": snap, "items": items, "bands": BANDS, "band": band, "customer": customer}
    return await _render(request, "queue.html", context)


def _draft_decisions(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each draft, the latest send step that used it (runs are newest first)."""
    found: dict[str, dict[str, Any]] = {}
    for run in runs:
        for step in run["steps"]:
            draft_id = (step["result"] or {}).get("draft_id")
            if step["tool"] == "send_email" and draft_id and draft_id not in found:
                found[draft_id] = {**step, "autonomy_level": run["autonomy_level"]}
    return found


@router.get("/invoices/{invoice_number}", response_class=HTMLResponse)
async def invoice_page(request: Request, invoice_number: str, as_of: str | None = None) -> Response:
    """The case page: facts, flags, case state, agent trace, drafts and timeline (§10, §18)."""
    snap = await _snapshot(request, as_of)
    row = next((r for r in snap.rows if r.invoice_number == invoice_number), None)
    if row is None:
        return await _render(
            request, "not_found.html", {"what": "invoice", "snap": snap}, status=404
        )
    state = request.app.state
    detail = await run_in_threadpool(invoice_detail, state, invoice_number, snap.as_of)
    runs = await run_in_threadpool(case_runs, state.engine, invoice_number)
    scored = next((s for s in snap.scored if s.invoice_number == invoice_number), None)
    flags: list[Any] = []
    cash = snap.kpis.unallocated.by_customer.get(row.customer_id, 0)
    if cash > 0:
        flags.append({"code": "UNALLOCATED_CASH_ON_ACCOUNT", "amount": cash})
    for c in (detail or {}).get("classifications", []):
        flags.extend(c["flags"])
    context = {
        "snap": snap,
        "row": row,
        "scored": scored,
        "item": next(
            (i for i in snap.queue.items if i.scored.invoice_number == invoice_number), None
        ),
        "payments": [p for p in snap.payments_as_of if p.invoice_number == invoice_number],
        "events": [
            e
            for e in snap.events
            if e.invoice_number == invoice_number and e.event_date <= snap.as_of
        ],
        "detail": detail,
        "flags": flags,
        "runs": runs,
        "decisions": _draft_decisions(runs),
    }
    return await _render(request, "invoice.html", context)


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request) -> Response:
    items = await run_in_threadpool(pending_approvals, request.app.state.engine)
    return await _render(request, "approvals.html", {"items": items})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str) -> Response:
    trace = await run_in_threadpool(run_trace, request.app.state.engine, run_id)
    if trace is None:
        return await _render(request, "not_found.html", {"what": "agent run"}, status=404)
    return await _render(request, "run.html", {"trace": trace, "summary": None})


@router.post("/agent/run", response_class=HTMLResponse)
async def run_agent_form(
    request: Request, dry_run: Annotated[str | None, Form()] = None
) -> Response:
    """Run one tick at the configured level and the server's own time and date (D-026)."""
    state = request.app.state
    is_dry = dry_run == "on"
    summary = await run_in_threadpool(
        run_agent_tick,
        state.engine,
        state.clock,
        state.config,
        dry_run=is_dry,
        requested_by=request.state.user.username,
        request={"as_of": None, "now": None, "autonomy_level": None, "dry_run": is_dry},
    )
    if is_dry:  # the trace was rolled back: show the counts only
        return await _render(request, "run.html", {"trace": None, "summary": summary})
    return RedirectResponse(f"/runs/{summary.run_id}", status_code=303)


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> Response:
    return await _render(
        request, "upload.html", {"kinds": FILE_KINDS, "result": None, "kind": None}
    )


@router.post("/upload/{kind}", response_class=HTMLResponse)
async def upload_submit(
    request: Request, kind: str, file: Annotated[UploadFile | None, File()] = None
) -> Response:
    result = await run_import(request, kind, file)
    context: dict[str, Any] = {
        "kinds": FILE_KINDS,
        "kind": kind[:40],
        "result": None,
        "refusal": None,
    }
    if isinstance(result, ImportRefusal):
        context["refusal"] = result
        return await _render(request, "upload.html", context, status=result.status)
    context["result"] = result
    return await _render(request, "upload.html", context, status=result.http_status)
