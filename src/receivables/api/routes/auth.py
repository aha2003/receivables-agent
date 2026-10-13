"""Health, login, logout, home placeholder and the current user (SPEC §3.3, §9)."""

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.repo import get_user_by_username
from receivables.api.security import SessionUser, session_payload, verify_password

router = APIRouter()

_INVALID = "Invalid username or password."
_LIMITED = "Too many failed attempts. Wait a minute and try again."


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


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


@router.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    limiter = request.app.state.login_limiter
    key = (request.client.host if request.client else "unknown", username)
    if limiter.blocked(key):
        return _login_page(request, 429, _LIMITED)
    user = await run_in_threadpool(_authenticate, request, username, password)
    if user is None:
        limiter.record_failure(key)
        return _login_page(request, 401, _INVALID)
    limiter.clear(key)
    request.session.clear()  # no session fixation: start a fresh session on login
    request.session.update(session_payload(user, request.app.state.clock))
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "home.html", {"user": request.state.user})


@router.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    user: SessionUser = request.state.user
    return {"username": user.username, "role": user.role}
