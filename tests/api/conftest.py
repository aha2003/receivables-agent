"""App fixtures: a migrated temp database, one synthetic user and a controllable clock."""

import re
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

from receivables.adapters.clock import DUBAI, SimulatedClock
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.migrate import upgrade_to_head
from receivables.adapters.db.repo import insert_user
from receivables.api.main import create_app
from receivables.api.security import hash_password
from receivables.settings import Settings

SECRET = "test-only-session-secret-0123456789abcdef"  # synthetic, tests only
USERNAME = "demo.user"
PASSWORD = secrets.token_urlsafe(18)  # generated per test run; never a real password


@pytest.fixture
def clock() -> SimulatedClock:
    return SimulatedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))


@pytest.fixture
def engine(tmp_path: Path, clock: SimulatedClock) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'app.db'}"
    upgrade_to_head(url)
    eng = make_engine(url)
    with eng.begin() as conn:
        insert_user(
            conn,
            clock,
            username=USERNAME,
            password_hash=hash_password(PASSWORD),
            role="user",
            actor="test",
        )
    yield eng
    eng.dispose()


@pytest.fixture
def app(engine: Engine, clock: SimulatedClock) -> FastAPI:
    settings = Settings(session_secret=SecretStr(SECRET), database_url=str(engine.url))
    return create_app(settings, clock=clock, engine=engine)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def csrf(client: TestClient) -> str:
    """The session's CSRF token: from /api/me when signed in, else from the /login form."""
    me = client.get("/api/me")
    if me.status_code == 200:
        return me.json()["csrf_token"]
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)
    assert match, "login form has no csrf_token field"
    return match.group(1)


def login(
    client: TestClient,
    username: str = USERNAME,
    password: str = PASSWORD,
    token: str | None = None,
) -> int:
    data = {"username": username, "password": password, "csrf_token": token or csrf(client)}
    return client.post("/login", data=data).status_code
