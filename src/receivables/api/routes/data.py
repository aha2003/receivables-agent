"""JSON API: dashboard, queue and imports (SPEC §9)."""

from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.api.imports import ImportRefusal, run_import
from receivables.api.params import parse_as_of, parse_limit
from receivables.api.snapshot import dashboard_json, queue_json, take_snapshot

router = APIRouter()


@router.get("/api/dashboard")
async def dashboard(request: Request, as_of: str | None = None) -> dict[str, Any]:
    day = parse_as_of(request, as_of)
    state = request.app.state
    snap = await run_in_threadpool(take_snapshot, state.engine, day, state.config.engine)
    return dashboard_json(snap)


@router.get("/api/queue")
async def queue(
    request: Request, as_of: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    state = request.app.state
    day = parse_as_of(request, as_of)
    count = parse_limit(limit, state.config.engine.queue_size)
    snap = await run_in_threadpool(take_snapshot, state.engine, day, state.config.engine)
    return queue_json(snap, count)


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
