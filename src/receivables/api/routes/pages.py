"""Server-rendered pages (SPEC §10, §18): dashboard, queue, upload, case, approvals and runs.

Pages use the same snapshot as the JSON API, so numbers match exactly. Every page shows the
SYNTHETIC DEMO DATA banner and the ``as_of`` of its numbers; htmx only enhances links and
forms that also work without JavaScript.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.adapters.clock import DUBAI
from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.controls import kill_switch_on
from receivables.api.agent_views import case_runs, pending_approvals, run_trace, send_decisions
from receivables.api.csrf import ensure_token
from receivables.api.imports import ImportRefusal, run_import
from receivables.api.invoice_detail import invoice_detail
from receivables.api.llm_ports import llm_ports
from receivables.api.params import parse_as_of
from receivables.api.snapshot import data_quality, dso_formula, take_snapshot
from receivables.core.aging import BUCKETS, aging_summary
from receivables.core.queue import HEURISTIC_NOTE
from receivables.core.scoring import BANDS
from receivables.core.validation import FILE_KINDS
from receivables.web.labels import flag_text

router = APIRouter()


def _kill_switch(request: Request) -> bool:
    state = request.app.state
    with state.engine.connect() as conn:
        return kill_switch_on(conn, state.config)


async def render(
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
    return await render(request, "dashboard.html", context)


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
    return await render(request, "queue.html", context)


def _flag_list(raw: Any) -> list[Any]:
    """A classification's flags as a list of codes or mappings; anything else shown as is."""
    if isinstance(raw, list):
        return [f if isinstance(f, str | Mapping) else str(f) for f in raw]
    return [raw if isinstance(raw, str | Mapping) else str(raw)]


def _case_flags(snap: Any, row: Any, item: Any, classifications: list[Any]) -> list[str]:
    """Plain-language flags for the case, each once (§10, §6.7, §7.4)."""
    flags: list[Any] = [
        {"code": f.code, "amount": f.amount} for f in (item.flags if item else ())
    ]  # §6.7: only queue items carry UNALLOCATED_CASH_ON_ACCOUNT
    rows = {r.invoice_number: r for r in snap.rows}
    for c in classifications:
        if c["status"] == "rejected":  # a human rejected this classification
            continue
        for flag in _flag_list(c["flags"]):
            data = dict(flag) if isinstance(flag, Mapping) else {"code": flag}
            if data.get("code") == "LEDGER_STILL_OPEN":  # X is the ledger at this page's as_of
                target = rows.get(str(data.get("invoice_number") or row.invoice_number))
                data.pop("amount", None)
                if target is not None and target.open_amount > 0:
                    data["amount"] = target.open_amount
                elif target is not None:
                    data["settled"] = True
            flags.append(data)
    return list(dict.fromkeys(flag_text(f) for f in flags))


@router.get("/invoices/{invoice_number}", response_class=HTMLResponse)
async def invoice_page(request: Request, invoice_number: str, as_of: str | None = None) -> Response:
    """The case page: facts, flags, case state, agent trace, drafts and timeline (§10, §18)."""
    snap = await _snapshot(request, as_of)
    row = next((r for r in snap.rows if r.invoice_number == invoice_number), None)
    if row is None:
        return await render(
            request, "not_found.html", {"what": "invoice", "snap": snap}, status=404
        )
    state = request.app.state
    detail = await run_in_threadpool(invoice_detail, state, invoice_number, snap.as_of, snap)
    runs = await run_in_threadpool(case_runs, state.engine, invoice_number)
    drafts = (detail or {}).get("drafts", [])
    decisions = await run_in_threadpool(
        send_decisions, state.engine, [d["draft_id"] for d in drafts]
    )
    scored = next((s for s in snap.scored if s.invoice_number == invoice_number), None)
    item = next((i for i in snap.queue.items if i.scored.invoice_number == invoice_number), None)
    flags = _case_flags(snap, row, item, (detail or {}).get("classifications", []))
    context = {
        "snap": snap,
        "row": row,
        "scored": scored,
        "item": item,
        "payments": [p for p in snap.payments_as_of if p.invoice_number == invoice_number],
        "events": [
            e
            for e in snap.events
            if e.invoice_number == invoice_number and e.event_date <= snap.as_of
        ],
        "detail": detail,
        "flags": flags,
        "runs": runs,
        "decisions": decisions,
        # Default for the "Mark sent" field: now, in Dubai.
        "now_local": state.clock.now().astimezone(DUBAI).strftime("%Y-%m-%dT%H:%M"),
    }
    return await render(request, "invoice.html", context)


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request) -> Response:
    items = await run_in_threadpool(pending_approvals, request.app.state.engine)
    return await render(request, "approvals.html", {"items": items})


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str) -> Response:
    trace = await run_in_threadpool(run_trace, request.app.state.engine, run_id)
    if trace is None:
        return await render(request, "not_found.html", {"what": "agent run"}, status=404)
    return await render(request, "run.html", {"trace": trace, "summary": None})


@router.post("/agent/run", response_class=HTMLResponse)
async def run_agent_form(
    request: Request,
    dry_run: Annotated[str | None, Form()] = None,
    as_of: Annotated[str | None, Form()] = None,
) -> Response:
    """Run one tick at the configured level and the server's own time (D-026).

    ``as_of`` may differ from today only below level 2 or in a dry run, as in the API.
    """
    state = request.app.state
    is_dry = dry_run == "on"
    day = parse_as_of(request, as_of) if as_of else None  # empty field: today
    level = state.config.agent.autonomy_level
    if day is not None and level == 2 and not is_dry and day != state.clock.today():
        raise HTTPException(422, "as_of must be today for a level 2 run that can send")
    summary = await run_in_threadpool(
        run_agent_tick,
        state.engine,
        state.clock,
        state.config,
        as_of=day,
        dry_run=is_dry,
        requested_by=request.state.user.username,
        **llm_ports(state),  # none in fake mode (D-037)
        request={
            "as_of": None if day is None else day.isoformat(),
            "now": None,
            "autonomy_level": None,
            "dry_run": is_dry,
        },
    )
    if is_dry:  # the trace was rolled back: show the counts only
        return await render(request, "run.html", {"trace": None, "summary": summary})
    return RedirectResponse(f"/runs/{summary.run_id}", status_code=303)


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> Response:
    return await render(request, "upload.html", {"kinds": FILE_KINDS, "result": None, "kind": None})


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
        return await render(request, "upload.html", context, status=result.status)
    context["result"] = result
    return await render(request, "upload.html", context, status=result.http_status)
