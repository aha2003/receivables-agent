"""Health, login, logout, home placeholder and the current user (SPEC §3.3, §9)."""

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.repo import get_user_by_username, write_audit
from receivables.api.auth import error_response
from receivables.api.security import SessionUser, session_payload, verify_password

router = APIRouter()

_INVALID = "Invalid username or password."
_LIMITED = "Too many failed attempts. Wait a minute and try again."
MAX_USERNAME_LENGTH = 64


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _login_error(request: Request, status: int, error: str, message: str) -> Response:
    if _wants_json(request):
        return error_response(status, error, message)
    return _login_page(request, status, message)


def _login_page(request: Request, status: int = 200, message: str | None = None) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "login.html", {"message": message}, status_code=status
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    return _login_page(request)


def _authenticate(request: Request, username: str, password: str) -> SessionUser | None:
    with request.app.state.engine.connect() as conn:
        stored = get_user_by_username(conn, username) if username else None
    ok = verify_password(stored.password_hash if stored else None, password)
    if stored is None or not ok:
        return None
    return SessionUser(stored.user_id, stored.username, stored.role)


def _audit(request: Request, *, actor: str, action: str, entity_type: str, entity_id: str) -> None:
    with request.app.state.engine.begin() as conn:
        write_audit(
            conn,
            request.app.state.clock,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=None,
            after={"client": request.client.host if request.client else "unknown"},
        )


@router.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    limiter = request.app.state.login_limiter
    name = username[:MAX_USERNAME_LENGTH]
    key = (request.client.host if request.client else "unknown", name)
    if not limiter.acquire(key):
        if limiter.lockout_started(key):
            await run_in_threadpool(
                _audit,
                request,
                actor=name or "-",
                action="login.locked",
                entity_type="login",
                entity_id=name or "-",
            )
        return _login_error(request, 429, "rate_limited", _LIMITED)
    user = None
    if len(username) <= MAX_USERNAME_LENGTH:
        user = await run_in_threadpool(_authenticate, request, username, password)
    if user is None:  # the reserved attempt stays counted as a failure
        return _login_error(request, 401, "invalid_credentials", _INVALID)
    limiter.clear(key)
    request.session.clear()  # no session fixation: start a fresh session on login
    request.session.update(session_payload(user, request.app.state.clock))
    await run_in_threadpool(
        _audit,
        request,
        actor=user.username,
        action="login.succeeded",
        entity_type="user",
        entity_id=str(user.user_id),
    )
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    user: SessionUser = request.state.user
    request.session.clear()
    _audit(
        request,
        actor=user.username,
        action="logout",
        entity_type="user",
        entity_id=str(user.user_id),
    )
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "home.html", {"user": request.state.user})


@router.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    user: SessionUser = request.state.user
    return {"username": user.username, "role": user.role}
