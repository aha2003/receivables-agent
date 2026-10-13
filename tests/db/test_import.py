"""Atomic, idempotent fixed-schema import (SPEC §4.6, §5, §9 response shape): T-IMP-04, T-IMP-05.

T-IMP-04/05 are verified at service level here (``http_status``); the HTTP route and its
tests come in S1.13 (docs/DECISIONS.md D-013). Synthetic data only; pytest tmp databases.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, exc, text

from receivables.adapters.clock import DUBAI, FixedClock
from receivables.adapters.db import importer
from receivables.adapters.db.engine import make_engine
from receivables.adapters.db.importer import MAPPING_HASH, ImportResult, import_csv
from receivables.adapters.db.migrate import upgrade_to_head

CLOCK = FixedClock(datetime(2026, 10, 31, 9, 0, tzinfo=DUBAI))
TABLES = ("customers", "invoices", "payments", "events", "imports", "audit_log")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{tmp_path / 'import.db'}"
    upgrade_to_head(url)
    engine = make_engine(url)
    yield engine
    engine.dispose()


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in TABLES}  # noqa: S608


def _load(engine: Engine, fixture_text: Callable[[str], str], *kinds: str) -> list[ImportResult]:
    return [
        import_csv(
            engine, kind, fixture_text(f"{kind}.csv").encode(), filename=f"{kind}.csv", clock=CLOCK
        )
        for kind in kinds
    ]


class TestImp04:
    def test_bad_invoices_file_rejected_and_nothing_written(
        self, db: Engine, fixture_text: Callable[[str], str]
    ) -> None:
        _load(db, fixture_text, "customers")
        good = fixture_text("invoices.csv")
        bad = good.replace(
            "INV-005,CUST-B,2026-08-15,2026-09-14,30000.00",
            "INV-005,CUST-B,2026-08-15,2026-09-14,30000.001",
        )
        assert bad != good
        before = _counts(db)
        result = import_csv(db, "invoices", bad.encode(), filename="invoices.csv", clock=CLOCK)
        assert result.status == "rejected"
        assert result.http_status == 422
        assert [(e.line, e.code, e.field) for e in result.errors] == [(6, "E_BAD_AMOUNT", "amount")]
        assert result.rows_total == 14
        assert _counts(db) == before
        response = result.to_response()
        assert response == {
            "status": "rejected",
            "rows_total": 14,
            "errors": [{"line": 6, "code": "E_BAD_AMOUNT", "field": "amount"}],
            "warnings": [],
        }

    def test_full_error_list_for_bad_invoices_fixture(
        self, db: Engine, fixture_text: Callable[[str], str]
    ) -> None:
        with db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO customers (customer_id, customer_name) "
                    "VALUES ('C001', 'Fictional C001')"
                )
            )
        before = _counts(db)
        result = import_csv(
            db,
            "invoices",
            fixture_text("bad_invoices.csv").encode(),
            filename="bad.csv",
            clock=CLOCK,
        )
        assert result.http_status == 422
        assert [(e.line, e.code) for e in result.errors] == [
            (3, "E_DUP_KEY"),
            (4, "E_BAD_DATE"),
            (5, "E_DUE_BEFORE_ISSUE"),
            (6, "E_BAD_AMOUNT"),
            (7, "E_FK_MISSING"),
            (8, "E_CURRENCY"),
            (9, "E_REQUIRED_EMPTY"),
            (10, "E_BAD_AMOUNT"),
        ]
        assert _counts(db) == before


class TestImp05:
    def test_same_file_twice_is_duplicate(
        self, db: Engine, fixture_text: Callable[[str], str]
    ) -> None:
        raw = fixture_text("customers.csv").encode()
        first = import_csv(db, "customers", raw, filename="customers.csv", clock=CLOCK)
        after_first = _counts(db)
        second = import_csv(db, "customers", raw, filename="customers.csv", clock=CLOCK)
        assert first.status == "accepted" and first.http_status == 200
        assert second.status == "duplicate" and second.http_status == 200
        assert second.import_id == first.import_id
        assert _counts(db) == after_first
        assert second.to_response() == {
            "import_id": first.import_id,
            "status": "duplicate",
            "rows_total": 3,
            "rows_ok": 3,
            "warnings": [],
        }


def test_golden_load_in_order(db: Engine, fixture_text: Callable[[str], str]) -> None:
    results = _load(db, fixture_text, "customers", "invoices", "payments", "events")
    assert [r.status for r in results] == ["accepted"] * 4
    counts = _counts(db)
    assert {
        t: counts[t]
        for t in ("customers", "invoices", "payments", "events", "imports", "audit_log")
    } == {
        "customers": 3,
        "invoices": 14,
        "payments": 4,
        "events": 2,
        "imports": 4,
        "audit_log": 4,
    }
    with db.connect() as c:
        tin = c.execute(text("SELECT tin FROM customers WHERE customer_id = 'CUST-A'")).scalar_one()
        inv = c.execute(
            text(
                "SELECT amount_fils, currency, po_number FROM invoices "
                "WHERE invoice_number = 'INV-006'"
            )
        ).one()
        event = c.execute(
            text("SELECT source, promise_date FROM events WHERE event_id = 'E-002'")
        ).one()
    assert tin == "1000000000"
    assert tuple(inv) == (4000000, "AED", None)
    assert tuple(event) == ("import", "2026-10-20")


def test_imports_and_audit_rows(db: Engine, fixture_text: Callable[[str], str]) -> None:
    raw = fixture_text("customers.csv").encode()
    result = import_csv(db, "customers", raw, filename="customers.csv", clock=CLOCK, actor="tester")
    sha = hashlib.sha256(raw).hexdigest()
    assert hashlib.sha256(b"fixed-schema-v1").hexdigest() == MAPPING_HASH
    with db.connect() as c:
        imp = c.execute(text("SELECT * FROM imports")).mappings().one()
        audit = c.execute(text("SELECT * FROM audit_log")).mappings().one()
    assert imp["import_id"] == result.import_id
    assert (imp["file_kind"], imp["filename"], imp["sha256"], imp["mapping_hash"]) == (
        "customers",
        "customers.csv",
        sha,
        MAPPING_HASH,
    )
    assert imp["idempotency_key"] == f"customers:{sha}:{MAPPING_HASH}"
    assert (imp["rows_total"], imp["rows_ok"], imp["rows_error"], imp["status"]) == (
        3,
        3,
        0,
        "accepted",
    )
    assert imp["created_at"] == "2026-10-31T05:00:00Z"
    assert json.loads(imp["report_json"]) == {"warnings": []}
    assert (
        audit["ts"],
        audit["actor"],
        audit["action"],
        audit["entity_type"],
        audit["entity_id"],
    ) == (
        "2026-10-31T05:00:00Z",
        "tester",
        "import.accepted",
        "import",
        result.import_id,
    )
    assert json.loads(audit["after_json"])["sha256"] == sha


def test_out_of_order_upload_fails_with_fk_missing(
    db: Engine, fixture_text: Callable[[str], str]
) -> None:
    result = import_csv(
        db, "invoices", fixture_text("invoices.csv").encode(), filename="i.csv", clock=CLOCK
    )
    assert result.status == "rejected"
    assert {e.code for e in result.errors} == {"E_FK_MISSING"}
    assert len(result.errors) == 14


def test_rejected_file_is_not_remembered(db: Engine) -> None:
    raw = b"customer_id,customer_name,trn\nC-1,Fictional,12345\n"
    first = import_csv(db, "customers", raw, filename="c.csv", clock=CLOCK)
    second = import_csv(db, "customers", raw, filename="c.csv", clock=CLOCK)
    assert first.status == second.status == "rejected"
    assert _counts(db)["imports"] == 0


def test_upsert_replaces_rows_and_keeps_references(
    db: Engine, fixture_text: Callable[[str], str]
) -> None:
    _load(db, fixture_text, "customers", "invoices")
    changed = fixture_text("customers.csv").replace(
        "Alpha Trading LLC", "Alpha Trading Renamed LLC"
    )
    result = import_csv(db, "customers", changed.encode(), filename="customers.csv", clock=CLOCK)
    assert result.status == "accepted"
    with db.connect() as c:
        name = c.execute(
            text("SELECT customer_name FROM customers WHERE customer_id = 'CUST-A'")
        ).scalar_one()
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).all()
    assert name == "Alpha Trading Renamed LLC"
    assert _counts(db)["customers"] == 3
    assert _counts(db)["invoices"] == 14
    assert fk_errors == []


def test_write_failure_leaves_nothing(
    db: Engine, fixture_text: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("simulated failure after data rows were written")

    monkeypatch.setattr(importer, "_write_audit", boom)
    before = _counts(db)
    with pytest.raises(RuntimeError, match="simulated failure"):
        _load(db, fixture_text, "customers")
    assert _counts(db) == before


def test_non_utf8_file_rejected(db: Engine) -> None:
    result = import_csv(
        db, "customers", b"customer_id,customer_name\n\xff\xfe,X\n", filename="c.csv", clock=CLOCK
    )
    assert result.status == "rejected"
    assert [(e.line, e.code) for e in result.errors] == [(1, "E_MISSING_COLUMN")]


def test_utf8_bom_is_accepted(db: Engine, fixture_text: Callable[[str], str]) -> None:
    raw = b"\xef\xbb\xbf" + fixture_text("customers.csv").encode()
    assert import_csv(db, "customers", raw, filename="c.csv", clock=CLOCK).status == "accepted"


def test_no_payments_warning(db: Engine, fixture_text: Callable[[str], str]) -> None:
    _, invoices = _load(db, fixture_text, "customers", "invoices")
    assert [(w.code, w.line) for w in invoices.warnings] == [("W_NO_PAYMENTS", 0)]
    payments = _load(db, fixture_text, "payments")[0]
    assert "W_NO_PAYMENTS" not in [w.code for w in payments.warnings]


def test_unallocated_warning_in_response(db: Engine, fixture_text: Callable[[str], str]) -> None:
    _load(db, fixture_text, "customers", "invoices")
    raw = (fixture_text("payments.csv") + "P-005,,CUST-C,2026-10-28,12000.00,payment\n").encode()
    result = import_csv(db, "payments", raw, filename="p.csv", clock=CLOCK)
    assert result.status == "accepted"
    assert {
        "line": 0,
        "code": "W_UNALLOCATED",
        "field": "invoice_number",
        "detail": "count=1 total=12000.00",
    } in result.to_response()["warnings"]


def test_unsupported_kind(db: Engine) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        import_csv(db, "correspondence", b"message_id\n", filename="m.csv", clock=CLOCK)


def test_concurrent_duplicate_is_caught_by_unique_key(
    db: Engine, fixture_text: Callable[[str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = fixture_text("customers.csv").encode()
    first = import_csv(db, "customers", raw, filename="customers.csv", clock=CLOCK)
    real_lookup = importer._existing_import
    calls: list[str] = []

    def stale_lookup(conn: Any, key: str) -> ImportResult | None:
        calls.append(key)
        # Simulate the race: the first check misses the committed import, the retry sees it.
        return None if len(calls) == 1 else real_lookup(conn, key)

    monkeypatch.setattr(importer, "_existing_import", stale_lookup)
    before = _counts(db)
    second = import_csv(db, "customers", raw, filename="customers.csv", clock=CLOCK)
    assert second.status == "duplicate"
    assert second.import_id == first.import_id
    assert _counts(db) == before
    assert len(calls) == 2


def test_header_only_file_is_accepted_with_zero_rows(db: Engine) -> None:
    result = import_csv(
        db, "customers", b"customer_id,customer_name\n", filename="c.csv", clock=CLOCK
    )
    assert (result.status, result.rows_total, result.rows_ok) == ("accepted", 0, 0)
    assert _counts(db)["customers"] == 0
    assert _counts(db)["imports"] == 1


def test_database_check_is_a_backstop_when_validation_misses(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If validation ever let a bad value through, the §5 CHECK rejects it and nothing is kept."""
    from receivables.core.validation import CustomerRow, ValidationResult

    bad = CustomerRow(
        customer_id="C-1",
        customer_name="Fictional",
        trn=None,
        tin=None,
        trade_license_no=None,
        payment_terms_days=999,  # violates CHECK (payment_terms_days BETWEEN 0 AND 365)
        contact_email=None,
    )
    monkeypatch.setattr(
        importer, "validate_csv", lambda kind, *_: ValidationResult(kind, 1, (bad,), (), ())
    )
    before = _counts(db)
    with pytest.raises(exc.IntegrityError, match="CHECK"):
        import_csv(
            db, "customers", b"customer_id,customer_name\nC-1,X\n", filename="c.csv", clock=CLOCK
        )
    assert _counts(db) == before
