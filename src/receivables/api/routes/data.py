"""JSON API: dashboard, queue, invoice detail and imports, and the §11 report (SPEC §9)."""

from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.api.imports import ImportRefusal, run_import
from receivables.api.invoice_detail import invoice_detail
from receivables.api.params import parse_as_of, parse_limit
from receivables.api.report import report_context
from receivables.api.snapshot import dashboard_json, queue_json, take_snapshot

router = APIRouter()


@router.get("/api/dashboard")
async def dashboard(request: Request, as_of: str | None = None) -> dict[str, Any]:
    day = parse_as_of(request, as_of)
    state = request.app.state
    snap = await run_in_threadpool(take_snapshot, state.engine, day, state.config.engine)
    return dashboard_json(snap)


@router.get("/api/report", response_class=HTMLResponse)
async def report(request: Request, as_of: str | None = None) -> Response:
    """§11 Receivables Health Audit: a standalone, printable HTML page (D-041)."""
    day = parse_as_of(request, as_of)
    state = request.app.state
    snap = await run_in_threadpool(take_snapshot, state.engine, day, state.config.engine)
    context = await run_in_threadpool(report_context, state.engine, snap)
    return state.templates.TemplateResponse(request, "report.html", {"r": context})


@router.get("/api/queue")
async def queue(
    request: Request, as_of: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    state = request.app.state
    day = parse_as_of(request, as_of)
    count = parse_limit(limit, state.config.engine.queue_size)
    snap = await run_in_threadpool(take_snapshot, state.engine, day, state.config.engine)
    return queue_json(snap, count)


@router.get("/api/invoices/{invoice_number}")
async def invoice(request: Request, invoice_number: str, as_of: str | None = None) -> Any:
    day = parse_as_of(request, as_of)
    detail = await run_in_threadpool(invoice_detail, request.app.state, invoice_number, day)
    if detail is None:
        raise HTTPException(404, "No invoice with that number as of that date.")
    return detail


@router.post("/api/imports/{kind}")
async def import_file(
    request: Request,
    kind: str,
    file: Annotated[UploadFile | None, File()] = None,
    mode: str = "upsert",
    dry_run: str | None = None,
) -> Response:
    result = await run_import(request, kind, file, mode=mode, dry_run=dry_run)
    if isinstance(result, ImportRefusal):
        return result.response()
    return JSONResponse(result.to_response(), status_code=result.http_status)
