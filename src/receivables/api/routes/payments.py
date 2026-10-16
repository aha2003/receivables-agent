"""Payment allocation (SPEC §6.7, §9; T-API-09, T-API-10; D-040).

``POST /api/payments/{payment_id}/allocate {invoice_number}``: the whole unallocated payment
goes to one invoice of the same customer; 409 otherwise (nothing changes, the refusal is
audited). The actor is the signed-in user.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.allocations import AllocationError, allocate_payment
from receivables.api.auth import error_response
from receivables.api.security import SessionUser

router = APIRouter()


class _Allocate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invoice_number: str = Field(min_length=1, max_length=64)


@router.post("/api/payments/{payment_id}/allocate")
async def allocate(request: Request, payment_id: str) -> Response:
    try:
        body = _Allocate.model_validate_json((await request.body()) or b"{}")
    except ValidationError:
        return error_response(422, "invalid_request", "Expected JSON with: invoice_number.")
    user: SessionUser = request.state.user
    state = request.app.state
    try:
        result = await run_in_threadpool(
            allocate_payment, state.engine, payment_id, body.invoice_number,
            clock=state.clock, actor=user.username,
        )  # fmt: skip
    except AllocationError as error:  # the §9 error shape, plus the refusal reason for a 409
        body_out = {"error": error.code, "detail": error.detail}
        if error.reason:
            body_out["reason"] = error.reason
        return JSONResponse(body_out, status_code=error.status)
    return JSONResponse(result)
