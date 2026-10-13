"""FastAPI application factory (SPEC §3.3, §9).

``create_app`` wires settings, the injected clock, the database engine, the signed session
cookie and the authentication gate. ``app`` is created only when a server asks for it
(``uv run uvicorn receivables.api.main:app``), so importing this module needs no secret.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from receivables.adapters.clock import Clock, SystemClock
from receivables.adapters.db.engine import make_engine
from receivables.api.auth import auth_gate, error_response
from receivables.api.routes import auth as auth_routes
from receivables.api.security import SESSION_TTL, LoginRateLimiter
from receivables.settings import Settings

__all__ = ["MIN_SECRET_LENGTH", "create_app"]

MIN_SECRET_LENGTH = 32
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
_ERROR_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_request",
    429: "rate_limited",
}


def create_app(settings: Settings, *, clock: Clock, engine: Engine | None = None) -> FastAPI:
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"SESSION_SECRET must be set to at least {MIN_SECRET_LENGTH} characters")

    app = FastAPI(
        title="Receivables Collections Agent (SYNTHETIC DEMO DATA)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.clock = clock
    app.state.engine = engine if engine is not None else make_engine(settings.database_url)
    app.state.login_limiter = LoginRateLimiter(clock)
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.include_router(auth_routes.router)
    # Middleware: the last added runs first, so the session is decoded before the auth gate.
    app.middleware("http")(auth_gate)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="session",
        max_age=int(SESSION_TTL.total_seconds()),
        same_site="lax",
        https_only=True,
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> Any:
        return error_response(
            exc.status_code, _ERROR_CODES.get(exc.status_code, "error"), str(exc.detail)
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> Any:
        # Never echo exception text: it may contain data. Details belong in server logs.
        return error_response(500, "internal_error", "Unexpected server error.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> Any:
        return error_response(422, "invalid_request", "Request validation failed.")

    return app


def __getattr__(name: str) -> Any:
    if name == "app":
        return create_app(Settings(), clock=SystemClock())
    raise AttributeError(name)
