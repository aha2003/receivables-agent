"""FastAPI application factory (SPEC §3.3, §9, §10).

``create_app`` wires settings, ``config.yaml``, the injected clock, the database engine, the
signed session cookie, CSRF, security headers, the body size limit and the authentication
gate. ``app`` is created only when a server asks for it
(``uv run uvicorn receivables.api.main:app``), so importing this module needs no secret.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from receivables.adapters.clock import Clock, SystemClock
from receivables.adapters.config import load_config
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.repo import customer_names
from receivables.adapters.llm.factory import make_llm_client
from receivables.adapters.llm.mode import resolve_llm_mode
from receivables.adapters.log import RequestIdMiddleware, configure_logging
from receivables.api.auth import auth_gate, error_response
from receivables.api.csrf import CsrfError, csrf_protect
from receivables.api.headers import SECURITY_HEADERS, SecurityHeadersMiddleware
from receivables.api.limits import BodySizeLimitMiddleware
from receivables.api.routes import agent as agent_routes
from receivables.api.routes import auth as auth_routes
from receivables.api.routes import correspondence as correspondence_routes
from receivables.api.routes import data as data_routes
from receivables.api.routes import decision_pages as decision_routes
from receivables.api.routes import drafts as draft_routes
from receivables.api.routes import inbox as inbox_routes
from receivables.api.routes import pages as page_routes
from receivables.api.routes import payments as payment_routes
from receivables.api.routes import simulation as simulation_routes
from receivables.api.security import LoginRateLimiter, ReauthRegistry
from receivables.core.money import from_fils, to_fils
from receivables.core.queue import unallocated_cash_message
from receivables.settings import Settings
from receivables.web.labels import flag_text, policy_text, status_text

__all__ = ["MIN_SECRET_LENGTH", "create_app"]

MIN_SECRET_LENGTH = 32
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_ERROR_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "invalid_request",
    429: "rate_limited",
    501: "not_implemented",
}


def aed(fils: int) -> str:
    """Display format of §10: ``AED 1,234.50``."""
    return f"AED {from_fils(fils):,}"


def aed_text(amount: str) -> str:
    """A decimal amount written as text (the simulator's results) in the §10 format."""
    return aed(to_fils(amount))


def create_app(settings: Settings, *, clock: Clock, engine: Engine | None = None) -> FastAPI:
    secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"SESSION_SECRET must be set to at least {MIN_SECRET_LENGTH} characters")

    app = FastAPI(
        title="Receivables Collections Agent (SYNTHETIC DEMO DATA)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_protect)],
    )
    config = load_config(Path(settings.config_path))  # ConfigError stops the app on bad config
    app.state.config = config
    app.state.llm_mode = resolve_llm_mode(settings, config)  # refuses a mode conflict (D-029)
    app.state.clock = clock
    app.state.engine = engine if engine is not None else make_engine(settings.database_url)
    # One LLM client for the app, always behind redaction (D-031); live needs its settings.
    app.state.llm = make_llm_client(
        settings, config, customer_names=customer_names(app.state.engine)
    )
    app.state.session_ttl = timedelta(minutes=config.app.session_ttl_minutes)
    app.state.login_limiter = LoginRateLimiter(clock, limit=config.app.login_rate_limit_per_minute)
    app.state.reauth = ReauthRegistry()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["aed"] = aed
    templates.env.filters["aed_text"] = aed_text
    templates.env.filters["unallocated_message"] = unallocated_cash_message
    templates.env.filters["policy_text"] = policy_text
    templates.env.filters["status_text"] = status_text
    templates.env.filters["flag_text"] = flag_text
    app.state.templates = templates
    app.state.sim_data_dir = Path(config.app.data_dir)  # the simulator's own files (S4.08)

    app.include_router(auth_routes.router)
    app.include_router(data_routes.router)
    app.include_router(agent_routes.router)
    app.include_router(correspondence_routes.router)
    app.include_router(inbox_routes.router)
    app.include_router(draft_routes.router)
    app.include_router(decision_routes.router)
    app.include_router(payment_routes.router)
    app.include_router(page_routes.router)
    app.include_router(simulation_routes.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=False), name="static")
    # Middleware: the last added runs first, so the session is decoded before the auth gate.
    app.add_middleware(BodySizeLimitMiddleware)  # inside the gate: anonymous requests get 401 first
    app.middleware("http")(auth_gate)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="session",
        max_age=int(app.state.session_ttl.total_seconds()),
        same_site="lax",
        https_only=True,
    )
    app.add_middleware(SecurityHeadersMiddleware)  # every response gets the headers
    app.add_middleware(RequestIdMiddleware)  # outermost: every log line of a request has its id

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> Any:
        return error_response(
            exc.status_code, _ERROR_CODES.get(exc.status_code, "error"), str(exc.detail)
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> Any:
        # Never echo exception text: it may contain data. Details belong in server logs.
        # Starlette sends 500s outside all middleware, so set the security headers here too.
        response = error_response(500, "internal_error", "Unexpected server error.")
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(CsrfError)
    async def csrf_error(_: Request, exc: CsrfError) -> Any:
        return error_response(403, "csrf_failed", "Missing or invalid CSRF token.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> Any:
        return error_response(422, "invalid_request", "Request validation failed.")

    return app


def __getattr__(name: str) -> Any:
    if name == "app":
        app = create_app(Settings(), clock=SystemClock())
        # Only a real server process logs: JSON lines, scrubbed of personal data (NFR-07).
        configure_logging(customer_names(app.state.engine))
        return app
    raise AttributeError(name)
