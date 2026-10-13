"""Web security baseline (SPEC §3.3): T-SEC-09 (CSRF) and T-SEC-11 (security headers), plus
static assets served locally (no CDN) and templates that respect the CSP."""

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import USERNAME, csrf, login

WEB = Path(__file__).resolve().parents[2] / "src" / "receivables" / "web"
EXPECTED_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


def _assert_security_headers(response: object, where: str) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    csp = headers.get("content-security-policy", "")
    directives = {d.split()[0]: d for d in (p.strip() for p in csp.split(";")) if d}
    assert directives.get("default-src") == "default-src 'self'", where
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp, where
    assert directives.get("script-src") == "script-src 'self'", where
    assert directives.get("object-src") == "object-src 'none'", where
    assert directives.get("frame-ancestors") == "frame-ancestors 'none'", where
    for name, value in EXPECTED_HEADERS.items():
        assert headers.get(name) == value, (where, name)


class TestSec09:
    def test_post_without_then_wrong_then_right_token(self, client: TestClient) -> None:
        assert login(client) == 303
        no_token = client.post("/logout")
        assert no_token.status_code == 403
        assert no_token.json() == {
            "error": "csrf_failed",
            "detail": "Missing or invalid CSRF token.",
        }
        wrong = client.post("/logout", data={"csrf_token": "not-the-token"})
        assert wrong.status_code == 403
        assert client.get("/api/me").status_code == 200  # still signed in after both refusals
        right = client.post("/logout", data={"csrf_token": csrf(client)})
        assert right.status_code == 303
        assert client.get("/api/me").status_code == 401

    def test_header_token_works_and_wrong_header_fails(self, client: TestClient) -> None:
        assert login(client) == 303
        assert client.post("/logout", headers={"x-csrf-token": "wrong"}).status_code == 403
        assert client.post("/logout", headers={"x-csrf-token": csrf(client)}).status_code == 303


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_state_changing_method_needs_the_token(
    app: FastAPI, client: TestClient, method: str
) -> None:
    @app.api_route("/api/test-only-mutation", methods=["POST", "PUT", "PATCH", "DELETE"])
    def mutate() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    assert login(client) == 303
    assert client.request(method, "/api/test-only-mutation").status_code == 403
    ok = client.request(method, "/api/test-only-mutation", headers={"x-csrf-token": csrf(client)})
    assert ok.status_code == 200 and ok.json() == {"ok": True}


def test_safe_methods_need_no_token(client: TestClient) -> None:
    assert login(client) == 303
    assert client.get("/api/me").status_code == 200


def test_unauthenticated_post_is_401_before_csrf(client: TestClient) -> None:
    assert client.post("/logout").status_code == 401


def test_login_requires_a_token_and_a_missing_token_costs_no_attempt(client: TestClient) -> None:
    for _ in range(10):
        response = client.post("/login", data={"username": USERNAME, "password": "wrong-password"})
        assert response.status_code == 403
    assert login(client) == 303  # the ten refusals did not consume the rate limit


def test_token_rotates_at_login(client: TestClient) -> None:
    anonymous = csrf(client)
    assert login(client, token=anonymous) == 303
    signed_in = csrf(client)
    assert signed_in != anonymous
    assert client.post("/logout", data={"csrf_token": anonymous}).status_code == 403


def test_tokens_are_per_session(app: FastAPI) -> None:
    with (
        TestClient(app, base_url="https://testserver", follow_redirects=False) as a,
        TestClient(app, base_url="https://testserver", follow_redirects=False) as b,
    ):
        assert login(a) == 303 and login(b) == 303
        assert csrf(a) != csrf(b)
        assert a.post("/logout", data={"csrf_token": csrf(b)}).status_code == 403


def test_forms_carry_the_hidden_token(client: TestClient) -> None:
    page = client.get("/login").text
    assert re.search(r'<input type="hidden" name="csrf_token" value="[^"]{30,}">', page)
    assert login(client) == 303
    home = client.get("/").text
    assert f'name="csrf_token" value="{csrf(client)}"' in home


class TestSec11:
    def test_headers_on_pages_and_api(self, app: FastAPI, client: TestClient) -> None:
        _assert_security_headers(client.get("/login"), "login page")
        _assert_security_headers(client.get("/healthz"), "healthz")
        _assert_security_headers(client.get("/api/me"), "401")
        _assert_security_headers(client.get("/", headers={"accept": "text/html"}), "redirect")
        _assert_security_headers(client.post("/login", data={}), "403")
        assert login(client) == 303
        _assert_security_headers(client.get("/"), "home page")
        _assert_security_headers(client.get("/api/me"), "API")
        _assert_security_headers(client.get("/api/not-there"), "404")
        _assert_security_headers(client.get("/static/css/app.css"), "static")

    def test_headers_on_unexpected_errors(self, app: FastAPI) -> None:
        @app.get("/api/test-only-crash")
        def crash() -> None:  # pyright: ignore[reportUnusedFunction]
            raise RuntimeError("simulated")

        with TestClient(
            app,
            base_url="https://testserver",
            follow_redirects=False,
            raise_server_exceptions=False,
        ) as c:
            assert login(c) == 303
            response = c.get("/api/test-only-crash")
        assert response.status_code == 500
        _assert_security_headers(response, "500")


# --- static assets: local only, public, safe -------------------------------------------------


def test_static_css_is_served_without_a_session(client: TestClient) -> None:
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def _raw_get(app: FastAPI, path: str) -> tuple[int, bytes]:
    """Send ``path`` to the ASGI app verbatim (the HTTP test client would normalise ``..``)."""
    import anyio

    messages: list[dict[str, object]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("testclient", 1),
        "server": ("testserver", 443),
    }

    async def run() -> None:
        done = anyio.Event()
        sent_request = False

        async def receive() -> dict[str, object]:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await done.wait()  # like a real server: disconnect only after the response
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                done.set()

        await app(scope, receive, send)  # pyright: ignore[reportArgumentType]

    anyio.run(run)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")  # pyright: ignore[reportArgumentType]
    return int(status), body  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "path",
    [
        "/static/",
        "/static/css/",
        "/static/../api/me",
        "/static/../../../pyproject.toml",
        "/static/css/../../templates/base.html",
        "/static/%2e%2e/%2e%2e/pyproject.toml",
        "/static/css/..%2f..%2fmain.py",
    ],
)
def test_static_refuses_listing_and_traversal(app: FastAPI, path: str) -> None:
    status, body = _raw_get(app, path)
    assert status == 404, (path, status)
    assert b"[project]" not in body and b"def create_app" not in body and b"<html" not in body


def test_raw_helper_reaches_the_static_mount(app: FastAPI) -> None:
    status, body = _raw_get(app, "/static/css/app.css")
    assert status == 200 and b"banner" in body


def test_only_the_static_prefix_is_public(client: TestClient) -> None:
    assert client.get("/staticx/anything").status_code == 401
    assert client.get("/static").status_code in {401, 404}


def test_static_files_are_all_local(app: FastAPI) -> None:
    from starlette.routing import Mount

    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert [m.path for m in mounts] == ["/static"]


# --- templates and static files respect the CSP and use no CDN --------------------------------

_FORBIDDEN = [
    (re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.I), "inline <script> without src"),
    (re.compile(r"\son[a-z]+\s*=", re.I), "inline event handler"),
    (re.compile(r"\sstyle\s*=", re.I), "inline style attribute"),
    (re.compile(r"<style", re.I), "<style> block"),
    (re.compile(r"javascript:", re.I), "javascript: URL"),
    (re.compile(r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", re.I), "external URL (no CDN)"),
    (re.compile(r"@import\s+url\(\s*[\"']?(?:https?:)?//", re.I), "external CSS import"),
]


VENDOR = WEB / "static" / "vendor"


def _web_files() -> list[Path]:
    """Our own templates and assets; vendored third-party files are pinned by hash instead."""
    files = sorted(
        p
        for p in WEB.rglob("*")
        if p.is_file() and p.suffix in {".html", ".css", ".js"} and VENDOR not in p.parents
    )
    assert files, "no templates or static files found"
    return files


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: str(p.relative_to(WEB)))
def test_web_files_respect_the_csp(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    problems = [why for pattern, why in _FORBIDDEN if pattern.search(text)]
    assert problems == [], (path, problems)


@pytest.mark.parametrize(
    ("snippet", "why"),
    [
        ("<script>alert(1)</script>", "inline <script> without src"),
        ('<button onclick="x()">', "inline event handler"),
        ('<p style="color:red">', "inline style attribute"),
        ('<script src="https://cdn.example/htmx.js"></script>', "external URL (no CDN)"),
        ('<link href="//cdn.example/x.css">', "external URL (no CDN)"),
    ],
)
def test_the_scanner_catches_violations(snippet: str, why: str) -> None:
    assert why in [w for pattern, w in _FORBIDDEN if pattern.search(snippet)]


def test_visiting_a_protected_page_keeps_the_open_login_form_valid(client: TestClient) -> None:
    token = csrf(client)  # login form opened in one tab
    assert client.get("/", headers={"accept": "text/html"}).status_code == 303  # another tab
    assert login(client, token=token) == 303


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("Application/X-WWW-Form-Urlencoded", 303),
        ("application/x-www-form-urlencoded; charset=UTF-8", 303),
        # python-multipart lowercases the media type only when there are no parameters, so
        # Starlette sees no form at all here; the request fails closed (D-016).
        ("APPLICATION/X-WWW-FORM-URLENCODED; charset=UTF-8", 403),
    ],
)
def test_form_content_type_is_case_insensitive(
    client: TestClient, content_type: str, expected: int
) -> None:
    from urllib.parse import urlencode

    assert login(client) == 303
    body = urlencode({"csrf_token": csrf(client)})
    response = client.post("/logout", content=body, headers={"content-type": content_type})
    assert response.status_code == expected


def test_unusual_methods_are_checked_too(app: FastAPI, client: TestClient) -> None:
    @app.api_route("/api/test-only-propfind", methods=["PROPFIND"])
    def propfind() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        return {"ok": True}

    assert login(client) == 303
    assert client.request("PROPFIND", "/api/test-only-propfind").status_code == 403
    ok = client.request(
        "PROPFIND", "/api/test-only-propfind", headers={"x-csrf-token": csrf(client)}
    )
    assert ok.status_code == 200


# --- vendored third-party assets: pinned by hash, recorded in PROVENANCE.md --------------------


def test_vendored_files_match_provenance() -> None:
    import hashlib

    provenance = (Path(__file__).resolve().parents[2] / "PROVENANCE.md").read_text(encoding="utf-8")
    vendored = sorted(p for p in VENDOR.rglob("*") if p.is_file())
    assert vendored, "expected vendored assets"
    for path in vendored:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest in provenance, (path, digest)


def test_htmx_is_locked_down_by_config(client: TestClient) -> None:
    import json

    page = client.get("/login").text
    assert '<script src="/static/vendor/htmx-2.0.10/htmx.min.js" defer></script>' in page
    match = re.search(r"<meta name=\"htmx-config\" content='([^']+)'>", page)
    assert match
    config = json.loads(match.group(1))
    assert config["allowEval"] is False
    assert config["allowScriptTags"] is False
    assert config["selfRequestsOnly"] is True
    assert config["includeIndicatorStyles"] is False
    assert config["historyCacheSize"] == 0
    js = client.get("/static/vendor/htmx-2.0.10/htmx.min.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
