"""The Inbox page (SPEC §10): classify, accept and reject without JavaScript (D-034).

Forms post to these routes, which call the same services as the JSON API and redirect back
(post, redirect, get). A refusal shows the inbox again with its reason.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.classifications import (
    ClassificationError,
    accept,
    classify_stored,
    inbox_items,
    reject,
)
from receivables.api.routes.pages import render
from receivables.core.validation import DISPUTE_LABELS

router = APIRouter()
LABELS = sorted(DISPUTE_LABELS)


async def _page(request: Request, error: ClassificationError | None = None) -> Response:
    items = await run_in_threadpool(inbox_items, request.app.state.engine)
    context = {"items": items, "labels": LABELS, "error": None if error is None else error.detail}
    return await render(
        request, "inbox.html", context, status=200 if error is None else error.status
    )


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request) -> Response:
    return await _page(request)


@router.post("/inbox/{message_id}/classify", response_class=HTMLResponse)
async def classify_form(request: Request, message_id: str) -> Response:
    state = request.app.state
    try:
        await run_in_threadpool(
            classify_stored,
            state.engine,
            message_id,
            client=state.llm,
            review_threshold=state.config.llm.review_confidence_threshold,
            clock=state.clock,
            actor=request.state.user.username,
        )
    except ClassificationError as error:
        return await _page(request, error)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{message_id}/accept", response_class=HTMLResponse)
async def accept_form(
    request: Request,
    message_id: str,
    label_override: Annotated[str | None, Form()] = None,
    promised_payment_date: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        promised = date.fromisoformat(promised_payment_date) if promised_payment_date else None
    except ValueError:
        return await _page(request, ClassificationError(422, "invalid_request", "Bad date."))
    state = request.app.state
    try:
        await run_in_threadpool(
            accept,
            state.engine,
            message_id,
            label_override=label_override or None,
            promised_payment_date=promised,
            clock=state.clock,
            actor=request.state.user.username,
        )
    except ClassificationError as error:
        return await _page(request, error)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/inbox/{message_id}/reject", response_class=HTMLResponse)
async def reject_form(request: Request, message_id: str) -> Response:
    state = request.app.state
    try:
        await run_in_threadpool(
            reject, state.engine, message_id, clock=state.clock, actor=request.state.user.username
        )
    except ClassificationError as error:
        return await _page(request, error)
    return RedirectResponse("/inbox", status_code=303)
