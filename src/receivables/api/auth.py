"""Authentication gate for every route except /healthz and /login (SPEC §3.3, §9; T-SEC-03)."""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from receivables.adapters.db.repo import get_user
from receivables.api.security import SessionUser, session_user

__all__ = ["PUBLIC_PATHS", "auth_gate", "error_response"]

PUBLIC_PATHS = frozenset({"/healthz", "/login"})


def error_response(status: int, error: str, detail: str) -> JSONResponse:
    """The §9 error format."""
    return JSONResponse({"error": error, "detail": detail}, status_code=status)


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api") and "text/html" in request.headers.get(
        "accept", ""
    )


def _current_user(request: Request) -> SessionUser | None:
    user = session_user(request.session, request.app.state.clock)
    if user is None:
        return None
    with request.app.state.engine.connect() as conn:
        stored = get_user(conn, user.user_id)
    if stored is None or stored.username != user.username or stored.role != user.role:
        return None
    return user


async def auth_gate(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    user = await run_in_threadpool(_current_user, request)
    if user is None:
        request.session.clear()
        if _wants_html(request):
            return RedirectResponse("/login", status_code=303)
        return error_response(401, "unauthenticated", "A valid session is required.")
    request.state.user = user
    return await call_next(request)
