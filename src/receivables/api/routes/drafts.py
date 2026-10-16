"""Drafts API (SPEC §9; T-API-06; D-038): create, approve, reject and mark sent.

Thin wrappers over ``adapters.db.drafts``. The actor is the signed-in user; ``approved_by``, if
sent, must be that user (D-038 option 1a). Nothing here sends mail.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.drafts import (
    DraftError,
    approve_draft,
    create_draft,
    mark_draft_sent,
    reject_draft,
)
from receivables.api.auth import error_response
from receivables.api.llm_ports import llm_ports
from receivables.api.security import SessionUser

router = APIRouter()


class _Create(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invoice_numbers: list[str] = Field(min_length=1, max_length=10)
    tone: int | None = None


class _Approve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved_by: str | None = None


class _Reject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str


class _MarkSent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sent_at: str


async def _body[M: BaseModel](request: Request, model: type[M]) -> M | Response:
    raw = await request.body()
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError:
        fields = ", ".join(model.model_fields)
        return error_response(422, "invalid_request", f"Expected JSON with: {fields}.")


async def _call(func: Any, *args: Any, **kwargs: Any) -> Response:
    try:
        return JSONResponse(await run_in_threadpool(func, *args, **kwargs))
    except DraftError as error:
        return error_response(error.status, error.code, error.detail)


@router.post("/api/drafts")
async def new_draft(request: Request) -> Response:
    body = await _body(request, _Create)
    if isinstance(body, Response):
        return body
    state = request.app.state
    user: SessionUser = request.state.user
    return await _call(
        create_draft,
        state.engine,
        body.invoice_numbers,
        body.tone,
        config=state.config,
        clock=state.clock,
        actor=user.username,
        drafter=llm_ports(state)["drafter"],
    )


@router.post("/api/drafts/{draft_id}/approve")
async def approve(request: Request, draft_id: str) -> Response:
    body = await _body(request, _Approve)
    if isinstance(body, Response):
        return body
    user: SessionUser = request.state.user
    if body.approved_by is not None and body.approved_by != user.username:
        return error_response(422, "invalid_request", "approved_by must be the signed-in user.")
    state = request.app.state
    return await _call(
        approve_draft, state.engine, draft_id, clock=state.clock, actor=user.username
    )


@router.post("/api/drafts/{draft_id}/reject")
async def reject(request: Request, draft_id: str) -> Response:
    body = await _body(request, _Reject)
    if isinstance(body, Response):
        return body
    state = request.app.state
    user: SessionUser = request.state.user
    return await _call(
        reject_draft, state.engine, draft_id, body.reason, clock=state.clock, actor=user.username
    )


@router.post("/api/drafts/{draft_id}/mark-sent")
async def mark_sent(request: Request, draft_id: str) -> Response:
    body = await _body(request, _MarkSent)
    if isinstance(body, Response):
        return body
    try:
        sent_at = datetime.fromisoformat(body.sent_at)
    except ValueError:
        return error_response(422, "invalid_request", "sent_at must be an ISO 8601 date and time.")
    state = request.app.state
    user: SessionUser = request.state.user
    return await _call(
        mark_draft_sent, state.engine, draft_id, sent_at, clock=state.clock, actor=user.username
    )
