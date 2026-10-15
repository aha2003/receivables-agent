"""Dashboard, queue and import APIs (SPEC §9, §6, §4.6): T-API-01..03, T-IMP-04/05 over HTTP."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.api.main import create_app
from receivables.settings import Settings

from .conftest import SECRET, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
KINDS = ("customers", "invoices", "payments", "events")
TABLES = ("customers", "invoices", "payments", "events", "imports", "audit_log")


def upload(client: TestClient, kind: str, content: bytes, **params: str) -> Any:
    return client.post(
        f"/api/imports/{kind}",
        files={"file": (f"{kind}.csv", content, "text/csv")},
        headers={"x-csrf-token": csrf(client)},
        params=params,
    )


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in TABLES}  # noqa: S608


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    assert login(client) == 303
    for kind in KINDS:
        response = upload(client, kind, (FIXTURES / f"{kind}.csv").read_bytes())
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"
    return client


class TestApi01:
    def test_dashboard_golden_totals(
        self, seeded: TestClient, golden_results: dict[str, Any]
    ) -> None:
        body = seeded.get("/api/dashboard", params={"as_of": "2026-10-31"}).json()
        assert body["as_of"] == "2026-10-31"
        assert body["total_open"] == "193000.00"
        assert body["overdue_total"] == "147000.00"
        assert body["overdue_pct"] == "76.2"
        assert body["buckets"] == golden_results["bucket_totals"]
        assert body["estimated_dso"] == "149.7"
        assert body["unallocated_total"] == "0.00"
        assert body["unallocated_by_customer"] == {}
        assert body["exposure_by_band"] == golden_results["exposure_by_band"]

    def test_dashboard_details(self, seeded: TestClient, golden_results: dict[str, Any]) -> None:
        body = seeded.get("/api/dashboard", params={"as_of": "2026-10-31"}).json()
        top = body["top_overdue_customers"]
        assert [(c["customer_id"], c["amount"], c["share_pct"]) for c in top] == [
            (r["customer_id"], r["amount"], r["share_pct"])
            for r in golden_results["overdue_by_customer"]
        ]
        assert top[0]["customer_name"] == "Beta Contracting LLC"
        assert top[0]["share"] == "0.6939"
        dso = body["dso_detail"]
        assert dso["label"] == "estimated DSO (90-day sales proxy)"
        assert dso["credit_sales_in_window"] == "116000.00"
        assert (dso["window_start"], dso["window_end"]) == ("2026-08-03", "2026-10-31")
        assert "90" in dso["formula"]
        assert body["data_quality"] == {
            "invoices": 14,
            "payments": 4,
            "has_payments": True,
            "warnings": [],
        }
        assert body["labels"]["synthetic"] == "SYNTHETIC DEMO DATA"
        assert body["labels"]["heuristic"] == "Heuristic score. Not a probability."


class TestApi02:
    def test_queue_matches_the_golden_table(
        self, seeded: TestClient, golden_results: dict[str, Any]
    ) -> None:
        body = seeded.get("/api/queue", params={"as_of": "2026-10-31"}).json()
        assert body["as_of"] == "2026-10-31"
        assert body["note"] == "Heuristic score. Not a probability."
        expected = golden_results["queue_default"]["rows"]
        items = body["items"]
        assert [i["invoice_number"] for i in items] == [r["invoice_number"] for r in expected]
        for item, row in zip(items, expected, strict=True):
            assert item["rank"] == row["rank"]
            assert item["priority_value"] == row["priority_value"]
            assert item["risk_score"] == row["risk"]
            assert item["band"] == row["band"]
            assert item["action"] == row["action"]
            assert (
                item["open_amount"]
                == golden_results["open_invoices"][row["invoice_number"]]["open_amount"]
            )
            assert item["flags"] == []
        first = items[0]
        assert first["customer_name"] == "Beta Contracting LLC"
        assert (first["dispute_label"], first["broken_promise"], first["days_overdue"]) == (
            "PO_AMOUNT_MISMATCH",
            False,
            122,
        )

    def test_queue_limit(self, seeded: TestClient) -> None:
        assert (
            len(
                seeded.get("/api/queue", params={"as_of": "2026-10-31", "limit": "3"}).json()[
                    "items"
                ]
            )
            == 3
        )
        for bad in ("0", "21", "abc"):
            response = seeded.get("/api/queue", params={"as_of": "2026-10-31", "limit": bad})
            assert response.status_code == 422, bad


class TestApi03:
    def test_time_travel(self, seeded: TestClient) -> None:
        body = seeded.get("/api/dashboard", params={"as_of": "2026-09-30"}).json()
        assert body["as_of"] == "2026-09-30"
        assert body["data_quality"]["invoices"] == 13
        assert body["data_quality"]["payments"] == 3
        assert body["total_open"] == "163000.00"
        queue = seeded.get("/api/queue", params={"as_of": "2026-09-30"}).json()["items"]
        inv005 = next(i for i in queue if i["invoice_number"] == "INV-005")
        assert inv005["open_amount"] == "30000.00"
        assert "INV-006" not in {i["invoice_number"] for i in queue}


def test_as_of_defaults_to_the_clock_date(seeded: TestClient) -> None:
    assert (
        seeded.get("/api/dashboard").json()
        == seeded.get("/api/dashboard", params={"as_of": "2026-10-31"}).json()
    )


@pytest.mark.parametrize("bad", ["31/10/2026", "2026-13-01", "today", ""])
def test_bad_as_of_is_422(seeded: TestClient, bad: str) -> None:
    response = seeded.get("/api/dashboard", params={"as_of": bad})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_unallocated_cash_flows_to_dashboard_and_queue(seeded: TestClient) -> None:
    extra = (
        FIXTURES / "payments.csv"
    ).read_bytes() + b"P-005,,CUST-C,2026-10-28,12000.00,payment\n"
    assert upload(seeded, "payments", extra).json()["status"] == "accepted"
    body = seeded.get("/api/dashboard", params={"as_of": "2026-10-31"}).json()
    assert body["total_open"] == "193000.00"
    assert body["unallocated_total"] == "12000.00"
    assert body["unallocated_by_customer"] == {"CUST-C": "12000.00"}
    assert body["data_quality"]["warnings"] == [
        {"code": "W_UNALLOCATED", "detail": "count=1 total=12000.00"}
    ]
    items = seeded.get("/api/queue", params={"as_of": "2026-10-31"}).json()["items"]
    flagged = {i["invoice_number"]: i["flags"] for i in items if i["flags"]}
    assert set(flagged) == {"INV-008", "INV-009", "INV-012"}
    assert flagged["INV-008"] == [
        {
            "code": "UNALLOCATED_CASH_ON_ACCOUNT",
            "amount": "12000.00",
            "message": "This customer has AED 12,000.00 of unallocated cash. "
            "Check whether it belongs to this invoice before chasing.",
        }
    ]


def test_empty_database_dashboard(client: TestClient) -> None:
    assert login(client) == 303
    body = client.get("/api/dashboard", params={"as_of": "2026-10-31"}).json()
    assert body["total_open"] == "0.00"
    assert body["overdue_pct"] is None
    assert body["estimated_dso"] is None
    assert body["data_quality"] == {
        "invoices": 0,
        "payments": 0,
        "has_payments": False,
        "warnings": [{"code": "W_NO_PAYMENTS", "detail": None}],
    }
    assert client.get("/api/queue").json()["items"] == []


def test_config_file_drives_the_engine(
    engine: Engine, clock: SimulatedClock, tmp_path: Path
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("engine:\n  min_history_invoices: 2\n", encoding="utf-8")
    settings = Settings(
        session_secret=SecretStr(SECRET), database_url=str(engine.url), config_path=str(cfg)
    )
    app = create_app(settings, clock=clock, engine=engine)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        assert login(c) == 303
        for kind in KINDS:
            assert upload(c, kind, (FIXTURES / f"{kind}.csv").read_bytes()).status_code == 200
        items = c.get("/api/queue", params={"as_of": "2026-10-31"}).json()["items"]
    assert [i["invoice_number"] for i in items][:4] == ["INV-004", "INV-011", "INV-005", "INV-003"]
    assert next(i for i in items if i["invoice_number"] == "INV-003")["priority_value"] == "7100.0"


def test_invalid_config_stops_the_app(
    engine: Engine, clock: SimulatedClock, tmp_path: Path
) -> None:
    from receivables.adapters.config import ConfigError

    cfg = tmp_path / "config.yaml"
    cfg.write_text("engine:\n  queue_size: 0\n", encoding="utf-8")
    settings = Settings(
        session_secret=SecretStr(SECRET), database_url=str(engine.url), config_path=str(cfg)
    )
    with pytest.raises(ConfigError):
        create_app(settings, clock=clock, engine=engine)


# --- imports over HTTP (T-IMP-04, T-IMP-05) ----------------------------------------------------


class TestImp04OverHttp:
    def test_bad_upload_is_422_and_changes_nothing(
        self, client: TestClient, engine: Engine
    ) -> None:
        assert login(client) == 303
        assert (
            upload(client, "customers", (FIXTURES / "customers.csv").read_bytes()).status_code
            == 200
        )
        before = _counts(engine)
        bad = (FIXTURES / "invoices.csv").read_text().replace("30000.00", "30000.001").encode()
        response = upload(client, "invoices", bad)
        assert response.status_code == 422
        assert response.json() == {
            "status": "rejected",
            "rows_total": 14,
            "errors": [{"line": 6, "code": "E_BAD_AMOUNT", "field": "amount"}],
            "warnings": [],
        }
        assert _counts(engine) == before


class TestImp05OverHttp:
    def test_repeat_upload_is_duplicate(self, client: TestClient, engine: Engine) -> None:
        assert login(client) == 303
        raw = (FIXTURES / "customers.csv").read_bytes()
        first = upload(client, "customers", raw).json()
        after_first = _counts(engine)
        second = upload(client, "customers", raw)
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"
        assert second.json()["import_id"] == first["import_id"]
        assert _counts(engine) == after_first


def test_import_audit_actor_is_the_signed_in_user(client: TestClient, engine: Engine) -> None:
    assert login(client) == 303
    upload(client, "customers", (FIXTURES / "customers.csv").read_bytes())
    with engine.connect() as c:
        actor = c.execute(
            text("SELECT actor FROM audit_log WHERE action = 'import.accepted'")
        ).scalar_one()
    assert actor == "demo.user"


@pytest.mark.parametrize(
    ("kind", "params", "status", "error"),
    [
        ("ledgers", {}, 404, "not_found"),
        ("customers", {"dry_run": "true"}, 400, "not_supported"),
        ("customers", {"mode": "replace"}, 422, "invalid_request"),
    ],
)
def test_import_refusals(
    client: TestClient, kind: str, params: dict[str, str], status: int, error: str
) -> None:
    assert login(client) == 303
    response = upload(client, kind, (FIXTURES / "customers.csv").read_bytes(), **params)
    assert response.status_code == status
    assert response.json()["error"] == error


def test_import_without_file_is_422(client: TestClient) -> None:
    assert login(client) == 303
    response = client.post("/api/imports/customers", headers={"x-csrf-token": csrf(client)})
    assert response.status_code == 422


def test_import_size_cap(client: TestClient, engine: Engine) -> None:
    assert login(client) == 303
    before = _counts(engine)
    response = upload(
        client, "customers", b"customer_id,customer_name\n" + b"x" * (5 * 1024 * 1024)
    )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"
    assert _counts(engine) == before


def test_import_needs_csrf(client: TestClient) -> None:
    assert login(client) == 303
    response = client.post(
        "/api/imports/customers",
        files={"file": ("c.csv", (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
    )
    assert response.status_code == 403


# --- spec review regressions ---------------------------------------------------------------


@pytest.mark.parametrize("field", [{"dry_run": "true"}, {"mode": "replace"}])
def test_import_options_sent_as_form_fields_are_honoured(
    client: TestClient, engine: Engine, field: dict[str, str]
) -> None:
    assert login(client) == 303
    before = _counts(engine)
    response = client.post(
        "/api/imports/customers",
        files={"file": ("customers.csv", (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
        data=field,
        headers={"x-csrf-token": csrf(client)},
    )
    assert response.status_code in {400, 422}
    assert _counts(engine) == before


def test_upload_page_honours_dry_run_form_field(client: TestClient, engine: Engine) -> None:
    assert login(client) == 303
    before = _counts(engine)
    response = client.post(
        "/upload/customers",
        files={"file": ("customers.csv", (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
        data={"csrf_token": csrf(client), "dry_run": "true"},
    )
    assert response.status_code == 400
    assert _counts(engine) == before


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\someone\\customers.csv", "customers.csv"),
        ("reports/2026/customers.csv", "customers.csv"),
    ],
)
def test_upload_filename_is_reduced_to_a_safe_base_name(
    client: TestClient, engine: Engine, sent: str, stored: str
) -> None:
    assert login(client) == 303
    response = client.post(
        "/api/imports/customers",
        files={"file": (sent, (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
        headers={"x-csrf-token": csrf(client)},
    )
    assert response.status_code == 200
    with engine.connect() as c:
        name = c.execute(text("SELECT filename FROM imports")).scalar_one()
        audit = c.execute(
            text("SELECT after_json FROM audit_log WHERE action = 'import.accepted'")
        ).scalar_one()
    assert name == stored
    assert "../" not in audit and "\\\\" not in audit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd\r\nX: y", "passwdX: y"),
        ("\x00\x1b[31mreport.csv\x7f", "[31mreport.csv"),
        ("", "customers.csv"),
        (None, "customers.csv"),
        ("\r\n\t", "customers.csv"),
        ("a" * 500, "a" * 200),
    ],
)
def test_safe_filename_strips_paths_and_control_characters(raw: str | None, expected: str) -> None:
    """Unit check: the HTTP client percent-encodes control characters, so test them directly."""
    from receivables.api.imports import safe_filename

    assert safe_filename(raw, "customers") == expected


def test_upload_with_empty_filename_is_not_treated_as_a_file(client: TestClient) -> None:
    assert login(client) == 303
    response = client.post(
        "/api/imports/customers",
        files={"file": ("", (FIXTURES / "customers.csv").read_bytes(), "text/csv")},
        headers={"x-csrf-token": csrf(client)},
    )
    assert response.status_code == 422


def test_oversized_anonymous_request_is_401_not_413(client: TestClient) -> None:
    response = client.post(
        "/api/imports/customers", content=b"x", headers={"content-length": "100000000"}
    )
    assert response.status_code == 401
