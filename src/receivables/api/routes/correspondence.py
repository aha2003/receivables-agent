"""Correspondence and classification API (SPEC §9, §7; docs/DECISIONS.md D-034).

``POST /api/correspondence`` takes JSON ``{messages: [...]}`` (the §4.5 fields) or a CSV
``file`` and runs the same strict, idempotent importer as ``/api/imports/correspondence``.
Classify, accept and reject are thin wrappers over ``adapters.db.classifications``.
"""

import csv
import io
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.classifications import (
    ClassificationError,
    accept,
    classify_stored,
    reject,
)
from receivables.adapters.db.importer import import_csv
from receivables.api.auth import error_response
from receivables.api.imports import safe_filename
from receivables.api.limits import MAX_UPLOAD_BYTES
from receivables.api.security import SessionUser

router = APIRouter()

MAX_MESSAGES = 1000
_FIELDS = (
    "message_id",
    "invoice_number",
    "customer_id",
    "direction",
    "sent_at",
    "language",
    "subject",
    "body",
)


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message_id: str
    invoice_number: str | None = None
    customer_id: str | None = None
    direction: str
    sent_at: str
    language: str | None = None
    subject: str | None = None
    body: str | None = None


class _Messages(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    messages: list[_Message] = Field(min_length=1, max_length=MAX_MESSAGES)


def _as_csv(messages: list[_Message]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_FIELDS)
    for m in messages:
        writer.writerow(["" if getattr(m, name) is None else getattr(m, name) for name in _FIELDS])
    return out.getvalue().encode("utf-8")


@router.post("/api/correspondence")
async def add_correspondence(
    request: Request, file: Annotated[UploadFile | None, File()] = None
) -> Response:
    if file is not None:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return error_response(413, "payload_too_large", "Uploads are limited to 5 MB.")
        filename = safe_filename(file.filename, "correspondence")
    else:
        try:
            body = _Messages.model_validate_json(await request.body())
        except ValidationError:
            return error_response(
                422, "invalid_request", "Send JSON {messages: [...]} or a CSV file."
            )
        raw, filename = _as_csv(body.messages), "api.json"
    user: SessionUser = request.state.user
    state = request.app.state
    result = await run_in_threadpool(
        import_csv,
        state.engine,
        "correspondence",
        raw,
        filename=filename,
        clock=state.clock,
        actor=user.username,
    )
    payload: dict[str, Any] = result.to_response()
    if result.status != "rejected":
        payload["inserted"] = result.rows_ok if result.status == "accepted" else 0
    return JSONResponse(payload, status_code=result.http_status)


# --- classify, accept, reject (§9, §7.1) ---------------------------------------------------------


class _Accept(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label_override: str | None = None
    promised_payment_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


def _refusal(error: ClassificationError) -> Response:
    return error_response(error.status, error.code, error.detail)


@router.post("/api/correspondence/{message_id}/classify")
async def classify(request: Request, message_id: str) -> Response:
    state = request.app.state
    user: SessionUser = request.state.user
    try:
        result = await run_in_threadpool(
            classify_stored,
            state.engine,
            message_id,
            client=state.llm,
            review_threshold=state.config.llm.review_confidence_threshold,
            clock=state.clock,
            actor=user.username,
        )
    except ClassificationError as error:
        return _refusal(error)
    return JSONResponse(result)


@router.post("/api/classifications/{message_id}/accept")
async def accept_classification(request: Request, message_id: str) -> Response:
    raw = await request.body()
    try:
        body = _Accept.model_validate_json(raw) if raw.strip() else _Accept()
        promised = (
            date.fromisoformat(body.promised_payment_date) if body.promised_payment_date else None
        )
    except (ValidationError, ValueError):
        return error_response(
            422, "invalid_request", "Optional fields: label_override, promised_payment_date."
        )
    state = request.app.state
    user: SessionUser = request.state.user
    try:
        result = await run_in_threadpool(
            accept,
            state.engine,
            message_id,
            label_override=body.label_override,
            promised_payment_date=promised,
            clock=state.clock,
            actor=user.username,
        )
    except ClassificationError as error:
        return _refusal(error)
    return JSONResponse(result)


@router.post("/api/classifications/{message_id}/reject")
async def reject_classification(request: Request, message_id: str) -> Response:
    state = request.app.state
    user: SessionUser = request.state.user
    try:
        result = await run_in_threadpool(
            reject, state.engine, message_id, clock=state.clock, actor=user.username
        )
    except ClassificationError as error:
        return _refusal(error)
    return JSONResponse(result)
