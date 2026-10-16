"""Form routes for the draft panel, the Approvals page and the Unallocated cash page (SPEC §10,
§9, §18.11, §6.7; D-038, D-040).

They call the same services as the JSON API and redirect back (post, redirect, get); a refusal
shows its reason. There is no send button: at L0 and L1 a person sends from their own email and
marks the draft sent; at L2 only an approval of a denied item delivers, through the sandbox.
"""

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.adapters.clock import DUBAI
from receivables.adapters.db.allocations import AllocationError, allocate_payment
from receivables.adapters.db.approvals import ApprovalError, decide_approval
from receivables.adapters.db.drafts import (
    DraftError,
    approve_draft,
    create_draft,
    mark_draft_sent,
    reject_draft,
)
from receivables.api.llm_ports import llm_ports
from receivables.api.routes.pages import render

router = APIRouter()


def _invoice_page(invoice: str) -> str:
    return f"/invoices/{quote(invoice, safe='')}#drafts"


async def _done(request: Request, call: Any, back: str, *args: Any, **kwargs: Any) -> Response:
    try:
        result = await run_in_threadpool(call, *args, **kwargs)
    except (DraftError, ApprovalError, AllocationError) as error:
        context = {"detail": error.detail, "back": back}
        return await render(request, "refused.html", context, status=error.status)
    target = back
    if isinstance(result, dict) and result.get("invoice_numbers"):
        target = _invoice_page(result["invoice_numbers"][0])
    return RedirectResponse(target, status_code=303)


@router.post("/drafts/new", response_class=HTMLResponse)
async def new_draft_form(request: Request, invoice_number: Annotated[str, Form()]) -> Response:
    state = request.app.state
    return await _done(
        request, create_draft, _invoice_page(invoice_number), state.engine, [invoice_number],
        None, config=state.config, clock=state.clock, actor=request.state.user.username,
        drafter=llm_ports(state)["drafter"],
    )  # fmt: skip


@router.post("/drafts/{draft_id}/approve", response_class=HTMLResponse)
async def approve_form(request: Request, draft_id: str) -> Response:
    state = request.app.state
    return await _done(
        request, approve_draft, "/queue", state.engine, draft_id,
        clock=state.clock, actor=request.state.user.username,
    )  # fmt: skip


@router.post("/drafts/{draft_id}/reject", response_class=HTMLResponse)
async def reject_form(
    request: Request, draft_id: str, reason: Annotated[str, Form()] = ""
) -> Response:
    state = request.app.state
    return await _done(
        request, reject_draft, "/queue", state.engine, draft_id, reason,
        clock=state.clock, actor=request.state.user.username,
    )  # fmt: skip


@router.post("/drafts/{draft_id}/mark-sent", response_class=HTMLResponse)
async def mark_sent_form(
    request: Request, draft_id: str, sent_at: Annotated[str, Form()] = ""
) -> Response:
    """``sent_at`` from a date-time field, read as Asia/Dubai local time."""
    try:
        moment = datetime.fromisoformat(sent_at)
    except ValueError:
        context = {"detail": "Give the date and time the email was sent.", "back": "/queue"}
        return await render(request, "refused.html", context, status=422)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=DUBAI)
    state = request.app.state
    return await _done(
        request, mark_draft_sent, "/queue", state.engine, draft_id, moment,
        clock=state.clock, actor=request.state.user.username,
    )  # fmt: skip


@router.post("/approvals/{approval_id}/{decision}", response_class=HTMLResponse)
async def approval_form(request: Request, approval_id: str, decision: str) -> Response:
    if decision not in ("approve", "reject"):
        return await render(request, "not_found.html", {"what": "decision"}, status=404)
    state = request.app.state
    return await _done(
        request, decide_approval, "/approvals", state.engine, approval_id, decision,
        config=state.config, clock=state.clock, actor=request.state.user.username,
    )  # fmt: skip


@router.post("/unallocated/{payment_id:path}/allocate", response_class=HTMLResponse)
async def allocate_form(
    request: Request, payment_id: str, invoice_number: Annotated[str, Form()] = ""
) -> Response:
    """§10 Unallocated cash: allocate the whole payment to the chosen invoice (§6.7)."""
    state = request.app.state
    back = f"/unallocated#pay-{quote(payment_id, safe='')}"
    return await _done(
        request, allocate_payment, back, state.engine, payment_id, invoice_number,
        clock=state.clock, actor=request.state.user.username,
    )  # fmt: skip
