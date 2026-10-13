"""Server-rendered pages (SPEC §10): dashboard, queue, upload and a read-only invoice view.

Pages use the same snapshot as the JSON API, so numbers match exactly. Every page shows the
SYNTHETIC DEMO DATA banner and the ``as_of`` of its numbers; htmx only enhances links and
forms that also work without JavaScript.
"""

from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.api.csrf import ensure_token
from receivables.api.imports import ImportRefusal, run_import
from receivables.api.params import parse_as_of
from receivables.api.snapshot import data_quality, dso_formula, take_snapshot
from receivables.core.aging import BUCKETS, aging_summary
from receivables.core.queue import HEURISTIC_NOTE
from receivables.core.scoring import BANDS
from receivables.core.validation import FILE_KINDS

router = APIRouter()


def _render(
    request: Request, template: str, context: dict[str, Any], status: int = 200
) -> Response:
    base = {
        "user": request.state.user,
        "csrf_token": ensure_token(request.session),
        "heuristic_note": HEURISTIC_NOTE,
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
    return _render(request, "dashboard.html", context)


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
    return _render(request, "queue.html", context)


@router.get("/invoices/{invoice_number}", response_class=HTMLResponse)
async def invoice_page(request: Request, invoice_number: str, as_of: str | None = None) -> Response:
    snap = await _snapshot(request, as_of)
    row = next((r for r in snap.rows if r.invoice_number == invoice_number), None)
    if row is None:
        return _render(request, "not_found.html", {"what": "invoice", "snap": snap}, status=404)
    context = {
        "snap": snap,
        "row": row,
        "scored": next((s for s in snap.scored if s.invoice_number == invoice_number), None),
        "item": next(
            (i for i in snap.queue.items if i.scored.invoice_number == invoice_number), None
        ),
        "payments": [p for p in snap.payments_as_of if p.invoice_number == invoice_number],
        "events": [
            e
            for e in snap.events
            if e.invoice_number == invoice_number and e.event_date <= snap.as_of
        ],
    }
    return _render(request, "invoice.html", context)


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> Response:
    return _render(request, "upload.html", {"kinds": FILE_KINDS, "result": None, "kind": None})


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
        return _render(request, "upload.html", context, status=result.status)
    context["result"] = result
    return _render(request, "upload.html", context, status=result.http_status)
