"""Agent API (SPEC §18.11, §3.3; docs/DECISIONS.md D-026).

Run a tick, read its trace, list pending approvals, read or set the kill switch, and re-enter
a password for admin actions. Every route needs a session; state-changing ones need CSRF.
Approve/reject (S3.09) and the playbook route (S4.05) come with the features they expose.
"""

import json
import re
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.agent_store import run_agent_tick
from receivables.adapters.db.controls import (
    REAUTH_WINDOW,
    AdminActionRefused,
    kill_switch_on,
    runtime_kill_switch,
    set_kill_switch,
)
from receivables.adapters.db.repo import get_user, write_audit
from receivables.agent.orchestrator import RunSummary
from receivables.api.auth import error_response
from receivables.api.security import SessionUser, verify_password

router = APIRouter()

REAUTH_SESSION_KEY = "reauth"
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RunRequest(_Strict):
    as_of: str | None = None
    now: str | None = None
    autonomy_level: int | None = Field(default=None, ge=0, le=2)
    dry_run: bool = False


class KillSwitchRequest(_Strict):
    on: bool


class ReauthRequest(_Strict):
    password: str = Field(min_length=1, max_length=1024)


async def _body(request: Request, model: type[_Strict]) -> Any:
    try:
        raw = await request.body()
        return model.model_validate_json(raw or b"{}")
    except ValidationError:
        raise HTTPException(422, "Request body does not match the documented shape.") from None


def _as_of(raw: str | None) -> date | None:
    if raw is None:
        return None
    if not _ISO_DATE.fullmatch(raw):
        raise HTTPException(422, "as_of must be a date written YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, "as_of must be a real calendar date") from None


def _now(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, "now must be an ISO 8601 date-time") from None
    if moment.tzinfo is None:
        raise HTTPException(422, "now must carry a time zone")
    return moment


def summary_json(summary: RunSummary, *, dry_run: bool) -> dict[str, Any]:
    return {
        "run_id": None if dry_run else summary.run_id,  # a dry run's trace was rolled back
        "as_of": summary.as_of.isoformat(),
        "autonomy_level": summary.autonomy_level,
        "planner": summary.planner,
        "fallback": summary.fallback,
        "model": summary.model,
        "cases_considered": summary.cases_considered,
        "steps_executed": summary.steps_executed,
        "steps_denied": summary.steps_denied,
        "case_errors": summary.case_errors,
        "dry_run": dry_run,
    }


@router.post("/api/agent/run")
async def agent_run(request: Request) -> dict[str, Any]:
    body: RunRequest = await _body(request, RunRequest)
    state = request.app.state
    user: SessionUser = request.state.user
    configured = state.config.agent.autonomy_level
    level = configured if body.autonomy_level is None else body.autonomy_level
    if level > configured:  # raising autonomy is a human config change (D-026)
        raise HTTPException(403, f"autonomy_level may not exceed the configured level {configured}")
    now, as_of = _now(body.now), _as_of(body.as_of)
    if level == 2 and not body.dry_run:  # a run that can send uses the server clock only
        if now is not None:
            raise HTTPException(422, "now may only be given for dry runs or below autonomy level 2")
        if as_of is not None and as_of != state.clock.today():
            raise HTTPException(422, "as_of must be today for a level 2 run that can send")
    summary = await run_in_threadpool(
        run_agent_tick,
        state.engine,
        state.clock,
        state.config,
        as_of=as_of,
        autonomy_level=level,
        now_local=now,
        dry_run=body.dry_run,
        requested_by=user.username,
        request=body.model_dump(),
    )
    return summary_json(summary, dry_run=body.dry_run)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _trace(request: Request, run_id: str) -> dict[str, Any] | None:
    with request.app.state.engine.connect() as conn:
        run = (
            conn.execute(text("SELECT * FROM agent_runs WHERE run_id = :r"), {"r": run_id})
            .mappings()
            .first()
        )
        if run is None:
            return None
        steps = (
            conn.execute(
                text("SELECT * FROM agent_steps WHERE run_id = :r ORDER BY seq"), {"r": run_id}
            )
            .mappings()
            .all()
        )
        events = (
            conn.execute(
                text(
                    "SELECT audit_id, ts, actor, action, entity_type, entity_id, after_json"
                    " FROM audit_log WHERE action <> 'agent.step'"
                    " AND (json_extract(after_json, '$.run_id') = :r"
                    "      OR (entity_type = 'agent_run' AND entity_id = :r))"
                    " ORDER BY audit_id"
                ),
                {"r": run_id},
            )
            .mappings()
            .all()
        )
    cases: dict[str, list[dict[str, Any]]] = {}
    for s in steps:
        cases.setdefault(s["invoice_number"], []).append(
            {
                "step_id": s["step_id"],
                "seq": s["seq"],
                "tool": s["tool"],
                "args": _loads(s["args_json"]),
                "reason": s["reason"],
                "policy_allowed": None
                if s["policy_allowed"] is None
                else bool(s["policy_allowed"]),
                "policy_reasons": _loads(s["policy_reasons_json"]),
                "status": s["status"],
                "result": _loads(s["result_json"]),
            }
        )
    return {
        "run": {
            "run_id": run["run_id"],
            "as_of": run["as_of"],
            "now": run["now_ts"],
            "autonomy_level": run["autonomy_level"],
            "planner": run["planner"],
            "fallback": bool(run["fallback"]),
            "model": run["model"],
            "cases_considered": run["cases_considered"],
            "steps_executed": run["steps_executed"],
            "steps_denied": run["steps_denied"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "cases": [{"invoice_number": n, "steps": steps} for n, steps in cases.items()],
        "events": [
            {
                "audit_id": e["audit_id"],
                "ts": e["ts"],
                "action": e["action"],
                "entity_type": e["entity_type"],
                "entity_id": e["entity_id"],
                "detail": _loads(e["after_json"]),
            }
            for e in events
        ],
    }


@router.get("/api/agent/runs/{run_id}")
async def agent_trace(request: Request, run_id: str) -> dict[str, Any]:
    trace = await run_in_threadpool(_trace, request, run_id)
    if trace is None:
        raise HTTPException(404, "No agent run with that id.")
    return trace


def _approvals(request: Request) -> list[dict[str, Any]]:
    with request.app.state.engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT a.approval_id, a.kind, a.step_id, a.payload_json, a.status,"
                    " s.run_id, s.invoice_number"
                    " FROM approvals a JOIN agent_steps s ON s.step_id = a.step_id"
                    " WHERE a.status = 'pending' ORDER BY a.approval_id"
                )
            )
            .mappings()
            .all()
        )
    items = []
    for r in rows:
        payload = json.loads(r["payload_json"])
        items.append(
            {
                "approval_id": r["approval_id"],
                "kind": r["kind"],
                "status": r["status"],
                "run_id": r["run_id"],
                "step_id": r["step_id"],
                "invoice_number": r["invoice_number"],
                "draft_id": payload.get("draft_id"),
                "payment_id": payload.get("payment_id"),
                "policy_allowed": payload.get("policy_allowed"),
                "reasons": payload.get("reasons", []),
            }
        )
    return items


@router.get("/api/agent/approvals")
async def agent_approvals(request: Request) -> dict[str, Any]:
    return {"items": await run_in_threadpool(_approvals, request)}


def _switch_state(request: Request) -> dict[str, bool]:
    config = request.app.state.config
    with request.app.state.engine.connect() as conn:
        runtime = runtime_kill_switch(conn)
        effective = kill_switch_on(conn, config)
    return {"on": effective, "runtime": runtime, "config": config.agent.l2.kill_switch}


@router.get("/api/agent/kill-switch")
async def kill_switch_state(request: Request) -> dict[str, bool]:
    return await run_in_threadpool(_switch_state, request)


def _reauthenticated_at(request: Request, user: SessionUser) -> datetime | None:
    """Only the server-side re-entry for this same user, matched by the cookie's nonce."""
    record = request.session.get(REAUTH_SESSION_KEY)
    if not isinstance(record, dict) or record.get("uid") != user.user_id:
        return None
    return request.app.state.reauth.reauthenticated_at(user.user_id, record.get("nonce"))


def _stored_user(request: Request, user: SessionUser) -> Any:
    """The user as stored now (role re-read, not taken from the cookie)."""
    with request.app.state.engine.connect() as conn:
        return get_user(conn, user.user_id)


@router.post("/api/agent/kill-switch")
async def kill_switch_set(request: Request) -> Response:
    body: KillSwitchRequest = await _body(request, KillSwitchRequest)
    state = request.app.state
    session_user: SessionUser = request.state.user
    stored = await run_in_threadpool(_stored_user, request, session_user)
    if stored is None:
        return error_response(401, "unauthenticated", "A valid session is required.")
    reauth = _reauthenticated_at(request, session_user)
    try:
        await run_in_threadpool(
            set_kill_switch,
            state.engine,
            state.clock,
            stored,
            on=body.on,
            reauthenticated_at=reauth,
        )
    except AdminActionRefused as refused:
        return error_response(
            403,
            refused.code.lower(),
            "Admin role and a recent re-entry of your password are required.",
        )
    return JSONResponse(await run_in_threadpool(_switch_state, request))


def _check_password(request: Request, user: SessionUser, password: str) -> bool:
    stored = _stored_user(request, user)
    return stored is not None and verify_password(stored.password_hash, password)


def _audit_reauth(
    request: Request, user: SessionUser, ok: bool, *, rate_limited: bool = False
) -> None:
    with request.app.state.engine.begin() as conn:
        write_audit(
            conn,
            request.app.state.clock,
            actor=user.username,
            action="auth.reauth",
            entity_type="user",
            entity_id=str(user.user_id),
            before=None,
            after={"ok": ok, "rate_limited": True} if rate_limited else {"ok": ok},
        )


@router.post("/api/reauth")
async def reauth(request: Request) -> Response:
    """Password re-entry for admin actions (§3.3); valid for ``REAUTH_WINDOW``."""
    body: ReauthRequest = await _body(request, ReauthRequest)
    user: SessionUser = request.state.user
    limiter = request.app.state.login_limiter
    key = (request.client.host if request.client else "unknown", f"reauth:{user.username}")
    if not limiter.acquire(key):
        if limiter.lockout_started(key):  # audited once per lockout, like login
            await run_in_threadpool(_audit_reauth, request, user, False, rate_limited=True)
        return error_response(429, "rate_limited", "Too many failed attempts. Wait a minute.")
    ok = await run_in_threadpool(_check_password, request, user, body.password)
    await run_in_threadpool(_audit_reauth, request, user, ok)
    registry = request.app.state.reauth
    if not ok:  # the reserved attempt stays counted as a failure; any earlier re-entry ends
        registry.forget(user.user_id)
        request.session.pop(REAUTH_SESSION_KEY, None)
        return error_response(401, "invalid_credentials", "Password not accepted.")
    limiter.clear(key)
    at = request.app.state.clock.now()
    nonce = registry.record(user.user_id, at)
    request.session[REAUTH_SESSION_KEY] = {"uid": user.user_id, "nonce": nonce}
    return JSONResponse(
        {"reauthenticated_at": at.isoformat(), "valid_seconds": int(REAUTH_WINDOW.total_seconds())}
    )
