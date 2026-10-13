"""Session login and route protection (SPEC §3.3, §9): T-SEC-03, T-SEC-10."""

from datetime import timedelta
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text
from starlette.routing import Mount

from receivables.adapters.clock import SimulatedClock
from receivables.api.main import create_app
from receivables.api.security import hash_password, verify_password
from receivables.settings import Settings

from .conftest import PASSWORD, SECRET, USERNAME, csrf, login

PUBLIC = {"/healthz", "/login"}


def _all_routes(routes: list[Any], prefix: str = "") -> list[tuple[str, str]]:
    """Every (method, path), walking FastAPI's included routers with their prefixes.

    Fails on any route type it does not understand, so a new kind of route (a static mount,
    a plain Starlette route, a websocket) cannot silently escape T-SEC-03.
    """
    out: list[tuple[str, str]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.extend((m, prefix + route.path) for m in sorted(route.methods or ()))
        elif hasattr(route, "original_router"):
            include_prefix = getattr(getattr(route, "include_context", None), "prefix", "")
            out.extend(_all_routes(route.original_router.routes, prefix + include_prefix))
        elif (
            isinstance(route, Mount)
            and prefix + route.path == "/static"
            and isinstance(route.app, StaticFiles)
        ):
            continue  # public self-hosted assets; tested in test_web_security.py
        else:
            raise AssertionError(f"T-SEC-03 route walker does not understand {route!r}")
    return out


def _protected_routes(app: FastAPI) -> list[tuple[str, str]]:
    return [(m, p) for m, p in _all_routes(list(app.routes)) if p not in PUBLIC]


def test_route_discovery_finds_the_public_routes_too(app: FastAPI) -> None:
    found = set(_all_routes(list(app.routes)))
    assert {("GET", "/healthz"), ("GET", "/login"), ("POST", "/login")} <= found


class TestSec03:
    def test_every_protected_route_rejects_without_session(
        self, app: FastAPI, client: TestClient
    ) -> None:
        routes = _protected_routes(app)
        assert (
            ("GET", "/") in routes
            and ("POST", "/logout") in routes
            and ("GET", "/api/me") in routes
        )
        for method, path in routes:
            api = client.request(method, path, headers={"accept": "application/json"})
            assert api.status_code == 401, (method, path)
            assert api.json()["error"] == "unauthenticated", (method, path)

    def test_html_pages_redirect_to_login(self, app: FastAPI, client: TestClient) -> None:
        for method, path in _protected_routes(app):
            if method == "GET" and not path.startswith("/api"):
                page = client.get(path, headers={"accept": "text/html"})
                assert page.status_code == 303, path
                assert page.headers["location"] == "/login"

    def test_api_paths_never_redirect(self, client: TestClient) -> None:
        response = client.get("/api/me", headers={"accept": "text/html"})
        assert response.status_code == 401

    def test_unknown_paths_are_401_not_404(self, client: TestClient) -> None:
        assert client.get("/api/does-not-exist").status_code == 401
        assert client.post("/whatever").status_code == 401

    def test_public_routes(self, client: TestClient) -> None:
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        page = client.get("/login")
        assert page.status_code == 200
        assert "SYNTHETIC DEMO DATA" in page.text


class TestSec10:
    def test_sixth_failed_login_within_a_minute_is_429(self, client: TestClient) -> None:
        codes = [login(client, password="wrong-password") for _ in range(6)]
        assert codes == [401, 401, 401, 401, 401, 429]

    def test_correct_password_is_also_blocked_while_limited(self, client: TestClient) -> None:
        for _ in range(5):
            login(client, password="wrong-password")
        assert login(client) == 429

    def test_limit_expires_after_a_minute(self, client: TestClient, clock: SimulatedClock) -> None:
        for _ in range(5):
            login(client, password="wrong-password")
        assert login(client) == 429
        clock.advance(timedelta(seconds=61))
        assert login(client) == 303

    def test_limit_is_per_username(self, client: TestClient) -> None:
        for _ in range(6):
            login(client, password="wrong-password")
        assert login(client, username="someone.else", password="x" * 12) == 401


def test_login_sets_a_hardened_session_cookie(client: TestClient) -> None:
    response = client.post(
        "/login", data={"csrf_token": csrf(client), "username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"].lower()
    for flag in ("secure", "httponly", "samesite=lax", "max-age=28800", "path=/"):
        assert flag in cookie, flag


def test_logged_in_access_and_logout(client: TestClient) -> None:
    assert login(client) == 303
    home = client.get("/")
    assert home.status_code == 200 and "SYNTHETIC DEMO DATA" in home.text and USERNAME in home.text
    me = client.get("/api/me").json()
    assert {k: me[k] for k in ("username", "role")} == {"username": USERNAME, "role": "user"}
    assert set(me) == {"username", "role", "csrf_token"} and len(me["csrf_token"]) >= 40
    out = client.post("/logout", data={"csrf_token": csrf(client)})
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert client.get("/api/me").status_code == 401


def test_session_expires_after_eight_hours(client: TestClient, clock: SimulatedClock) -> None:
    assert login(client) == 303
    clock.advance(timedelta(hours=8))
    assert client.get("/api/me").status_code == 200
    clock.advance(timedelta(seconds=1))
    assert client.get("/api/me").status_code == 401


def test_tampered_cookie_is_rejected(client: TestClient) -> None:
    assert login(client) == 303
    real = next(c for c in client.cookies.jar if c.name == "session")
    assert real.value
    client.cookies.clear()
    tampered = real.value[:-4] + ("AAAA" if not real.value.endswith("AAAA") else "BBBB")
    client.cookies.set("session", tampered, domain=real.domain)
    assert client.get("/api/me").status_code == 401


def test_deleted_user_loses_access(client: TestClient, engine: Engine) -> None:
    assert login(client) == 303
    with engine.begin() as c:
        c.execute(text("DELETE FROM users WHERE username = :u"), {"u": USERNAME})
    assert client.get("/api/me").status_code == 401


def test_wrong_password_and_unknown_user_look_identical(client: TestClient) -> None:
    wrong = client.post(
        "/login",
        data={"csrf_token": csrf(client), "username": USERNAME, "password": "not-the-password"},
    )
    unknown = client.post(
        "/login",
        data={
            "csrf_token": csrf(client),
            "username": "nobody.here",
            "password": "not-the-password",
        },
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.text == unknown.text
    assert "Invalid username or password" in wrong.text


def test_missing_form_fields_are_401_not_500(client: TestClient) -> None:
    assert client.post("/login", data={"csrf_token": csrf(client)}).status_code == 401


def test_session_does_not_carry_the_password(client: TestClient) -> None:
    response = client.post(
        "/login", data={"csrf_token": csrf(client), "username": USERNAME, "password": PASSWORD}
    )
    assert PASSWORD not in response.headers["set-cookie"]


def test_passwords_are_argon2id() -> None:
    stored = hash_password("synthetic-pass-123")
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "synthetic-pass-123")
    assert not verify_password(stored, "synthetic-pass-124")
    assert not verify_password(None, "anything")
    assert not verify_password("not-a-hash", "anything")
    assert not PasswordHasher().check_needs_rehash(stored)


@pytest.mark.parametrize("secret", [None, "too-short"])
def test_app_refuses_to_start_without_a_strong_secret(
    engine: Engine, clock: SimulatedClock, secret: str | None
) -> None:
    value = SecretStr(secret) if secret is not None else None
    settings = Settings(session_secret=value, database_url=str(engine.url))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        create_app(settings, clock=clock, engine=engine)


def test_api_errors_use_the_json_error_format(client: TestClient) -> None:
    assert login(client) == 303
    missing = client.get("/api/does-not-exist")
    assert missing.status_code == 404
    assert missing.json() == {"error": "not_found", "detail": "Not Found"}


def test_validation_errors_use_the_json_error_format(app: FastAPI, client: TestClient) -> None:
    @app.get("/api/test-only-number")
    def number(n: int) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return {"n": n}

    assert login(client) == 303
    bad = client.get("/api/test-only-number", params={"n": "not-a-number"})
    assert bad.status_code == 422
    assert bad.json() == {"error": "invalid_request", "detail": "Request validation failed."}


def test_uvicorn_entry_point_builds_the_app_lazily(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import receivables.api.main as main_module

    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'lazy.db'}")
    assert isinstance(main_module.app, FastAPI)
    with pytest.raises(AttributeError):
        _ = main_module.not_an_attribute  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(
    "session",
    [
        {},
        {"uid": "x", "username": "u", "role": "user", "iat": "2026-10-31T09:00:00+04:00"},
        {"uid": 1, "username": "u", "role": "user", "iat": "2026-10-31T09:00:00"},  # naive
        {"uid": 1, "username": "u", "role": "user", "iat": "2026-10-31T10:00:00+04:00"},  # future
        {"uid": 1, "username": "u", "role": "user", "iat": "yesterday"},
    ],
)
def test_malformed_sessions_are_rejected(session: dict[str, Any], clock: SimulatedClock) -> None:
    from receivables.api.security import session_user

    assert session_user(session, clock) is None


# --- spec review regressions ---------------------------------------------------------------


def test_route_walker_fails_on_unknown_route_types() -> None:
    from starlette.routing import Route

    with pytest.raises(AssertionError, match="does not understand"):
        _all_routes([Mount("/static", routes=[])])  # a /static mount that is not StaticFiles
    with pytest.raises(AssertionError, match="does not understand"):
        _all_routes([Mount("/uploads", app=StaticFiles(directory="."))])
    with pytest.raises(AssertionError, match="does not understand"):
        _all_routes([Route("/plain", endpoint=lambda request: None)])


def test_concurrent_failed_logins_cannot_exceed_the_limit(client: TestClient) -> None:
    from concurrent.futures import ThreadPoolExecutor

    token = csrf(client)  # one anonymous session, shared by all threads
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(
            pool.map(lambda _: login(client, password="wrong-password", token=token), range(20))
        )
    assert codes.count(401) == 5
    assert codes.count(429) == 15


def test_limiter_forgets_expired_keys(clock: SimulatedClock) -> None:
    from receivables.api.security import LoginRateLimiter

    limiter = LoginRateLimiter(clock)
    for n in range(50):
        assert limiter.acquire(("host", f"user{n}"))
    assert limiter.key_count() == 50
    clock.advance(timedelta(minutes=2))
    assert limiter.acquire(("host", "fresh"))
    assert limiter.key_count() == 1


def test_limiter_fails_closed_when_full(clock: SimulatedClock) -> None:
    from receivables.api.security import LoginRateLimiter

    limiter = LoginRateLimiter(clock, max_keys=3)
    assert all(limiter.acquire(("host", f"u{n}")) for n in range(3))
    assert not limiter.acquire(("host", "one-too-many"))


def test_success_releases_the_reservation(clock: SimulatedClock) -> None:
    from receivables.api.security import LoginRateLimiter

    limiter = LoginRateLimiter(clock, limit=1)
    assert limiter.acquire(("h", "u"))
    limiter.clear(("h", "u"))
    assert limiter.acquire(("h", "u"))


def test_overlong_username_is_a_cheap_failure(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf(client),
            "username": "u" * 10_000,
            "password": "whatever-password",
        },
    )
    assert response.status_code == 401


def test_limit_is_per_client(app: FastAPI) -> None:
    with (
        TestClient(
            app, base_url="https://testserver", follow_redirects=False, client=("10.0.0.1", 1)
        ) as a,
        TestClient(
            app, base_url="https://testserver", follow_redirects=False, client=("10.0.0.2", 1)
        ) as b,
    ):
        assert [login(a, password="wrong-password") for _ in range(6)][-1] == 429
        assert login(b, password="wrong-password") == 401
        assert login(b) == 303


def test_role_change_ends_the_session(client: TestClient, engine: Engine) -> None:
    assert login(client) == 303
    with engine.begin() as c:
        c.execute(text("UPDATE users SET role = 'admin' WHERE username = :u"), {"u": USERNAME})
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_and_options_are_gated(client: TestClient, method: str) -> None:
    assert client.request(method, "/api/me").status_code == 401
    assert client.request(method, "/").status_code == 401


def test_login_errors_are_json_for_api_clients(client: TestClient) -> None:
    json_headers = {"accept": "application/json"}
    bad = client.post(
        "/login",
        data={"csrf_token": csrf(client), "username": USERNAME, "password": "wrong-password"},
        headers=json_headers,
    )
    assert bad.status_code == 401
    assert bad.json() == {"error": "invalid_credentials", "detail": "Invalid username or password."}
    for _ in range(5):
        client.post(
            "/login",
            data={"csrf_token": csrf(client), "username": USERNAME, "password": "wrong-password"},
            headers=json_headers,
        )
    limited = client.post(
        "/login",
        data={"csrf_token": csrf(client), "username": USERNAME, "password": "wrong-password"},
        headers=json_headers,
    )
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"


def test_unexpected_errors_are_json(app: FastAPI) -> None:
    @app.get("/api/test-only-boom")
    def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("simulated failure with a secret-looking value sk-test-123")

    with TestClient(
        app, base_url="https://testserver", follow_redirects=False, raise_server_exceptions=False
    ) as c:
        assert login(c) == 303
        response = c.get("/api/test-only-boom")
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error", "detail": "Unexpected server error."}


def _audit_actions(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as c:
        return [
            tuple(r)
            for r in c.execute(
                text("SELECT action, actor, entity_id FROM audit_log ORDER BY audit_id")
            )
        ]


def test_login_logout_and_lockout_are_audited(client: TestClient, engine: Engine) -> None:
    assert login(client) == 303
    client.post("/logout", data={"csrf_token": csrf(client)})
    for _ in range(7):
        login(client, password="wrong-password")
    actions = [a for a, _, _ in _audit_actions(engine) if a != "user.created"]
    assert actions == ["login.succeeded", "logout", "login.locked"]  # one lockout row per window
    rows = {a: (actor, entity) for a, actor, entity in _audit_actions(engine)}
    assert rows["login.succeeded"][0] == USERNAME
    with engine.connect() as c:
        dumped = " ".join(str(v) for r in c.execute(text("SELECT * FROM audit_log")) for v in r)
    assert PASSWORD not in dumped and "wrong-password" not in dumped
