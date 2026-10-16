"""Slice 1 exit condition (SPEC §16) and D2 in part: the seeded demo starts and shows the
golden numbers.

Unlike T-API-01 (which loads data through the upload API) this follows the setup in
docs/DEMO.md: an empty database, ``receivables reset`` and ``receivables create-user`` from the
real CLI, then the web app on that same database with the clock on the golden date.
"""

import io
import re
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from receivables.adapters.clock import DUBAI, SimulatedClock
from receivables.api.main import create_app
from receivables.cli import main
from receivables.settings import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SECRET = "test-only-session-secret-0123456789abcdef"  # synthetic, tests only
USERNAME = "demo.user"
PASSWORD = secrets.token_urlsafe(18)  # generated per run; never a real password
# The visible banner element, not the <title> (which also says SYNTHETIC DEMO DATA).
BANNER = re.compile(r'<div role="status" class="banner">SYNTHETIC DEMO DATA')
HEURISTIC = "Heuristic score. Not a probability."


@pytest.fixture
def demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db = tmp_path / "demo.db"
    assert not db.exists()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES))
    assert main(["reset", "--as-of", "2026-10-31"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["create-user", USERNAME, "--password-stdin"]) == 0

    settings = Settings(session_secret=SecretStr(SECRET))  # DATABASE_URL from the environment
    clock = SimulatedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))
    app = create_app(settings, clock=clock)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        form = client.get("/login").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', form)
        assert token, "login form has no csrf_token field"
        response = client.post(
            "/login",
            data={"username": USERNAME, "password": PASSWORD, "csrf_token": token.group(1)},
        )
        assert response.status_code == 303
        yield client
    app.state.engine.dispose()


@pytest.mark.spec("T-API-01")
def test_seeded_dashboard_matches_the_golden_numbers(
    demo: TestClient, golden_results: dict[str, Any]
) -> None:
    body = demo.get("/api/dashboard").json()  # as_of defaults to the clock's date
    assert body["as_of"] == "2026-10-31"
    assert body["total_open"] == "193000.00"
    assert body["overdue_total"] == "147000.00"
    assert body["buckets"] == golden_results["bucket_totals"]
    assert body["estimated_dso"] == "149.7"
    assert body["unallocated_total"] == "0.00"
    assert body["exposure_by_band"] == golden_results["exposure_by_band"]

    page = demo.get("/").text
    for section, shown in (
        ("total-outstanding", "AED 193,000.00"),
        ("overdue", "AED 147,000.00"),
        ("dso", "149.7 days"),
        ("unallocated", "AED 0.00"),
    ):
        match = re.search(rf'<section id="{section}">(.*?)</section>', page, re.S)
        assert match and shown in match.group(1) and "2026-10-31" in match.group(1)


def test_seeded_queue_matches_the_golden_order(
    demo: TestClient, golden_results: dict[str, Any]
) -> None:
    body = demo.get("/api/queue").json()
    expected = [r["invoice_number"] for r in golden_results["queue_default"]["rows"]]
    assert [i["invoice_number"] for i in body["items"]] == expected
    for item, row in zip(body["items"], golden_results["queue_default"]["rows"], strict=True):
        assert (item["rank"], item["risk_score"], item["band"]) == (
            row["rank"],
            row["risk"],
            row["band"],
        )
        assert (item["priority_value"], item["action"]) == (row["priority_value"], row["action"])
    assert body["note"] == HEURISTIC

    page = demo.get("/queue").text
    shown = re.findall(r"INV-\d{3}", page)
    assert list(dict.fromkeys(shown)) == expected


@pytest.mark.parametrize("path", ["/", "/queue", "/invoices/INV-001", "/upload"])
def test_every_page_says_synthetic(demo: TestClient, path: str) -> None:
    response = demo.get(path)
    assert response.status_code == 200
    assert BANNER.search(response.text)


def test_login_page_says_synthetic(demo: TestClient) -> None:
    token = demo.get("/api/me").json()["csrf_token"]
    assert demo.post("/logout", data={"csrf_token": token}).status_code == 303
    assert demo.get("/api/me").status_code == 401
    assert BANNER.search(demo.get("/login").text)


@pytest.mark.spec("T-UI-05")
@pytest.mark.parametrize("path", ["/", "/queue", "/invoices/INV-004"])  # INV-004 heads the queue
def test_scores_are_labelled_heuristic(demo: TestClient, path: str) -> None:
    assert HEURISTIC in demo.get(path).text
