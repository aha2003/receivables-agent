"""Payment allocation (SPEC §6.7, §9): T-API-09, T-API-10 and the 409 rules (D-040).

Golden data plus the synthetic unallocated payment P-005 (CUST-C, AED 12,000.00, 2026-10-28,
as in T-UNA-01). The test clock is 2026-10-31 09:00 Dubai.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from receivables.adapters.clock import SimulatedClock
from receivables.seed import seed

from .conftest import USERNAME, csrf, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
P005 = b"P-005,,CUST-C,2026-10-28,12000.00,payment\n"
HEADER = b"payment_id,invoice_number,customer_id,payment_date,amount,payment_type\n"


def upload_payments(client: TestClient, rows: bytes) -> Any:
    return client.post(
        "/api/imports/payments",
        files={"file": ("p.csv", HEADER + rows, "text/csv")},
        headers={"x-csrf-token": csrf(client)},
    )


@pytest.fixture
def user(client: TestClient, engine: Engine, clock: SimulatedClock) -> TestClient:
    seed(engine, FIXTURES, clock)
    assert login(client) == 303
    assert upload_payments(client, P005).status_code == 200
    return client


def allocate(client: TestClient, payment_id: str, invoice: Any) -> Any:
    return client.post(
        f"/api/payments/{payment_id}/allocate",
        json={"invoice_number": invoice},
        headers={"x-csrf-token": csrf(client)},
    )


def rows(engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), params)]


def payments(engine: Engine) -> list[dict[str, Any]]:
    return rows(engine, "SELECT * FROM payments ORDER BY payment_id")


def audits(engine: Engine, action: str) -> list[dict[str, Any]]:
    found = rows(engine, "SELECT * FROM audit_log WHERE action = :a ORDER BY audit_id", a=action)
    return [
        {
            **r,
            "before": json.loads(r["before_json"] or "null"),
            "after": json.loads(r["after_json"]),
        }
        for r in found
    ]


def open_amount(client: TestClient, invoice: str) -> str:
    return client.get(f"/api/invoices/{invoice}").json()["open_amount"]


# --- T-API-09 ---------------------------------------------------------------------------------


@pytest.mark.spec("T-API-09")
def test_api_09_allocate_p005_to_inv008(user: TestClient, engine: Engine) -> None:
    assert user.get("/api/dashboard").json()["unallocated_total"] == "12000.00"
    response = allocate(user, "P-005", "INV-008")
    assert response.status_code == 200
    assert response.json() == {
        "payment_id": "P-005", "invoice_number": "INV-008", "customer_id": "CUST-C",
        "amount": "12000.00", "invoice_open_amount": "0.00",
    }  # fmt: skip
    assert open_amount(user, "INV-008") == "0.00"
    assert user.get("/api/dashboard").json()["unallocated_total"] == "0.00"
    (audit,) = audits(engine, "payment.allocated")
    assert (audit["actor"], audit["entity_type"], audit["entity_id"]) == (
        USERNAME, "payment", "P-005",
    )  # fmt: skip
    assert audit["before"]["invoice_number"] is None
    assert audit["after"]["invoice_number"] == "INV-008"
    assert audit["after"]["invoice_open_amount"] == "0.00"


@pytest.mark.spec("T-API-09")
def test_api_09_an_invoice_of_another_customer_is_409(user: TestClient, engine: Engine) -> None:
    before = payments(engine)
    response = allocate(user, "P-005", "INV-003")  # CUST-A
    assert response.status_code == 409
    assert response.json()["reason"] == "CUSTOMER_MISMATCH"
    assert payments(engine) == before
    (refused,) = audits(engine, "payment.allocation_refused")
    assert refused["after"] == {"requested_invoice": "INV-003", "reason": "CUSTOMER_MISMATCH"}


# --- T-API-10 ---------------------------------------------------------------------------------


@pytest.mark.spec("T-API-10")
def test_api_10_a_payment_larger_than_the_open_amount_is_409(
    user: TestClient, engine: Engine
) -> None:
    before = payments(engine)
    response = allocate(user, "P-005", "INV-012")  # AED 9,000.00 open; the payment is 12,000.00
    assert response.status_code == 409
    assert response.json() == {
        "error": "conflict",
        "detail": "The payment is larger than the invoice's open amount. Choose another "
        "invoice; splitting a payment is not supported.",
        "reason": "EXCEEDS_OPEN_AMOUNT",
    }
    assert payments(engine) == before  # no ledger or payment mutation
    assert rows(engine, "SELECT invoice_number FROM payments WHERE payment_id = 'P-005'") == [
        {"invoice_number": None}
    ]
    assert open_amount(user, "INV-012") == "9000.00"
    (refused,) = audits(engine, "payment.allocation_refused")  # the rejection is recorded
    assert (refused["actor"], refused["entity_id"]) == (USERNAME, "P-005")
    assert refused["after"]["reason"] == "EXCEEDS_OPEN_AMOUNT"
    assert audits(engine, "payment.allocated") == []


# --- the other rules ----------------------------------------------------------------------------


def test_an_allocated_payment_cannot_be_allocated_again(user: TestClient, engine: Engine) -> None:
    assert allocate(user, "P-005", "INV-008").status_code == 200
    again = allocate(user, "P-005", "INV-009")
    assert (again.status_code, again.json()["reason"]) == (409, "ALREADY_ALLOCATED")
    # An imported allocation is just as final.
    imported = allocate(user, "P-001", "INV-001")
    assert (imported.status_code, imported.json()["reason"]) == (409, "ALREADY_ALLOCATED")


def test_a_payment_dated_after_today_is_409(
    user: TestClient, engine: Engine, clock: SimulatedClock
) -> None:
    upload_payments(user, b"P-009,,CUST-C,2026-11-05,900.00,payment\n")
    early = allocate(user, "P-009", "INV-008")
    assert (early.status_code, early.json()["reason"]) == (409, "NOT_YET_RECEIVED")
    clock.advance(timedelta(days=5))  # 2026-11-05: received today
    assert login(user) == 303  # the session expired meanwhile
    assert allocate(user, "P-009", "INV-008").status_code == 200


def test_the_exact_open_amount_is_allowed_and_less_leaves_a_balance(
    user: TestClient, engine: Engine
) -> None:
    upload_payments(user, b"P-010,,CUST-C,2026-10-29,1000.00,payment\n")
    assert allocate(user, "P-010", "INV-012").json()["invoice_open_amount"] == "8000.00"
    assert open_amount(user, "INV-012") == "8000.00"


def test_one_fils_over_the_open_amount_is_409(user: TestClient, engine: Engine) -> None:
    upload_payments(user, b"P-010,,CUST-C,2026-10-29,9000.01,payment\n")  # INV-012: 9,000.00
    response = allocate(user, "P-010", "INV-012")
    assert (response.status_code, response.json()["reason"]) == (409, "EXCEEDS_OPEN_AMOUNT")


def test_an_invoice_dated_after_today_is_409(user: TestClient, engine: Engine) -> None:
    upload = user.post(
        "/api/imports/invoices",
        files={"file": ("i.csv", b"invoice_number,customer_id,invoice_date,due_date,amount,"
                                 b"currency,po_number\nINV-901,CUST-C,2026-11-15,2026-12-15,"
                                 b"20000.00,AED,PO-C-901\n", "text/csv")},
        headers={"x-csrf-token": csrf(user)},
    )  # fmt: skip
    assert upload.status_code == 200
    response = allocate(user, "P-005", "INV-901")
    assert (response.status_code, response.json()["reason"]) == (409, "NOT_IN_LEDGER")


@pytest.mark.parametrize(
    ("payment", "invoice", "status"),
    [("P-404", "INV-008", 404), ("P-005", "INV-404", 404), ("P-005", "", 422), ("P-005", 7, 422)],
)
def test_unknown_ids_and_bad_bodies(
    user: TestClient, engine: Engine, payment: str, invoice: Any, status: int
) -> None:
    assert allocate(user, payment, invoice).status_code == status
    assert audits(engine, "payment.allocated") == []


def test_a_fully_paid_invoice_resolves_its_case_at_once(user: TestClient, engine: Engine) -> None:
    # Option 3a (§18.2): the open amount reached 0, so the case is RESOLVED, by the person.
    user.post("/api/agent/run", json={"as_of": "2026-10-31"}, headers={"x-csrf-token": csrf(user)})
    state = "SELECT state FROM cases WHERE invoice_number = 'INV-008'"
    assert rows(engine, state) != [{"state": "RESOLVED"}]
    allocate(user, "P-005", "INV-008")
    assert rows(engine, state) == [{"state": "RESOLVED"}]
    moved = audits(engine, "case.transition")[-1]
    assert (moved["actor"], moved["entity_id"]) == (USERNAME, "INV-008")
    expected = {"state": "RESOLVED", "trigger": "PaidInFull", "payment_id": "P-005"}
    assert moved["after"] == expected


def test_allocation_needs_a_session_and_csrf(client: TestClient) -> None:
    body = {"invoice_number": "INV-008"}
    assert client.post("/api/payments/P-005/allocate", json=body).status_code == 401
    assert login(client) == 303
    assert client.post("/api/payments/P-005/allocate", json=body).status_code == 403


# --- a later import (D-040 option 1a) -----------------------------------------------------------


def test_a_later_import_keeps_a_persons_allocation(user: TestClient, engine: Engine) -> None:
    allocate(user, "P-005", "INV-008")
    # The accounting file still lists P-005 as unallocated, with a corrected date.
    response = upload_payments(user, b"P-005,,CUST-C,2026-10-27,12000.00,payment\n")
    assert response.status_code == 200
    assert {"line": 2, "code": "W_ALLOCATION_KEPT", "field": "invoice_number",
            "detail": "P-005"} in response.json()["warnings"]  # fmt: skip
    # Nothing is unallocated any more, so the import does not say so.
    assert not [w for w in response.json()["warnings"] if w["code"] == "W_UNALLOCATED"]
    last_import = audits(engine, "import.accepted")[-1]
    assert last_import["after"]["allocations_kept"] == ["P-005"]
    (row,) = rows(engine, "SELECT * FROM payments WHERE payment_id = 'P-005'")
    assert (row["invoice_number"], row["customer_id"], row["payment_date"]) == (
        "INV-008", "CUST-C", "2026-10-27",
    )  # fmt: skip
    assert open_amount(user, "INV-008") == "0.00"


def test_a_later_import_that_agrees_or_other_payments_import_as_written(
    user: TestClient, engine: Engine
) -> None:
    allocate(user, "P-005", "INV-008")
    agreed = upload_payments(user, b"P-005,INV-008,,2026-10-28,12000.00,payment\n")
    assert not [w for w in agreed.json()["warnings"] if w["code"] == "W_ALLOCATION_KEPT"]
    # An allocation that came from a file (not a person) is the file's to change.
    moved = upload_payments(user, b"P-001,INV-003,,2026-08-10,10000.00,payment\n")
    assert not [w for w in moved.json()["warnings"] if w["code"] == "W_ALLOCATION_KEPT"]
    assert rows(engine, "SELECT invoice_number FROM payments WHERE payment_id = 'P-001'") == [
        {"invoice_number": "INV-003"}
    ]


# --- the agent's proposals (§18.3 propose_allocation; a person confirms) -------------------------


def proposal(user: TestClient, engine: Engine, invoice: str) -> str:
    """An allocation item as the agent's propose_allocation step leaves it."""
    post_run = user.post(
        "/api/agent/run", json={"as_of": "2026-10-31"}, headers={"x-csrf-token": csrf(user)}
    )
    assert post_run.status_code == 200
    (step,) = rows(engine, "SELECT step_id FROM agent_steps ORDER BY seq LIMIT 1")
    payload = json.dumps({"payment_id": "P-005", "invoice_number": invoice})
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO approvals (approval_id, step_id, kind, payload_json)"
                 " VALUES ('A-ALLOC', :s, 'allocation', :p)"),
            {"s": step["step_id"], "p": payload},
        )  # fmt: skip
    return "A-ALLOC"


def decide(user: TestClient, approval_id: str, decision: str) -> Any:
    return user.post(
        f"/api/agent/approvals/{approval_id}/{decision}", json={},
        headers={"x-csrf-token": csrf(user)},
    )  # fmt: skip


def test_approving_a_proposal_allocates_by_the_same_rules(user: TestClient, engine: Engine) -> None:
    approval_id = proposal(user, engine, "INV-008")
    response = decide(user, approval_id, "approve")
    assert response.status_code == 200
    assert response.json()["allocation"]["invoice_open_amount"] == "0.00"
    assert open_amount(user, "INV-008") == "0.00"
    (audit,) = audits(engine, "payment.allocated")
    assert (audit["actor"], audit["after"]["approval_id"]) == (USERNAME, approval_id)
    status = "SELECT status, decided_by FROM approvals WHERE approval_id = 'A-ALLOC'"
    assert rows(engine, status) == [{"status": "approved", "decided_by": USERNAME}]


def test_a_proposal_for_a_gone_payment_is_audited(user: TestClient, engine: Engine) -> None:
    approval_id = proposal(user, engine, "INV-404")
    assert decide(user, approval_id, "approve").status_code == 409
    (refused,) = audits(engine, "approval.allocation_refused")
    assert (refused["actor"], refused["after"]["reason"]) == (USERNAME, "not_found")


def test_a_refused_proposal_stays_pending_and_is_audited(user: TestClient, engine: Engine) -> None:
    approval_id = proposal(user, engine, "INV-012")  # more than INV-012's open amount
    before = payments(engine)
    response = decide(user, approval_id, "approve")
    assert response.status_code == 409
    assert payments(engine) == before
    (refused,) = audits(engine, "payment.allocation_refused")
    assert refused["after"]["reason"] == "EXCEEDS_OPEN_AMOUNT"
    assert refused["after"]["approval_id"] == approval_id
    status = "SELECT status FROM approvals WHERE approval_id = 'A-ALLOC'"
    assert rows(engine, status) == [{"status": "pending"}]
    assert decide(user, approval_id, "reject").status_code == 200


def test_a_later_import_that_raises_a_kept_payment_warns_it_overpays(
    user: TestClient, engine: Engine
) -> None:
    # Validation sees the kept allocation, so a larger amount is an overpayment of INV-008.
    allocate(user, "P-005", "INV-008")
    response = upload_payments(user, b"P-005,,CUST-C,2026-10-28,20000.00,payment\n")
    codes = {(w["code"], w.get("detail")) for w in response.json()["warnings"]}
    assert ("W_OVERPAID", "INV-008") in codes and ("W_ALLOCATION_KEPT", "P-005") in codes
    assert not [c for c, _ in codes if c == "W_UNALLOCATED"]


def test_a_later_import_naming_another_invoice_keeps_the_allocation(
    user: TestClient, engine: Engine
) -> None:
    allocate(user, "P-005", "INV-008")
    response = upload_payments(user, b"P-005,INV-009,,2026-10-28,12000.00,payment\n")
    codes = {(w["code"], w.get("detail")) for w in response.json()["warnings"]}
    assert ("W_ALLOCATION_KEPT", "P-005") in codes
    assert ("W_OVERPAID", "INV-009") not in codes  # INV-009 never receives it
    assert rows(engine, "SELECT invoice_number FROM payments WHERE payment_id = 'P-005'") == [
        {"invoice_number": "INV-008"}
    ]


def test_a_later_dated_allocation_from_a_file_leaves_no_room(
    user: TestClient, engine: Engine
) -> None:
    # A file allocates AED 9,000.00 dated after today to INV-012 (9,000.00): today it is still
    # open, but allocating more cash to it would over-allocate it once that payment is dated.
    upload_payments(
        user,
        b"P-020,INV-012,,2026-11-05,9000.00,payment\nP-010,,CUST-C,2026-10-29,9000.00,payment\n",
    )
    assert open_amount(user, "INV-012") == "9000.00"
    response = allocate(user, "P-010", "INV-012")
    assert (response.status_code, response.json()["reason"]) == (409, "EXCEEDS_OPEN_AMOUNT")


@pytest.mark.parametrize("payment_id", ["P/2026/1", "X/../P-005", "..", ".", "P 1", "P?1"])
def test_a_payment_id_that_a_url_would_change_is_refused_at_import(
    user: TestClient, payment_id: str
) -> None:
    # Ids appear in allocation URLs: "/" or a dot segment could make a click allocate another
    # payment (D-040), so the import refuses them as it does for message ids.
    row = f"{payment_id},,CUST-C,2026-10-29,100.00,payment\n".encode()
    response = upload_payments(user, row)
    assert response.status_code == 422
    assert {"line": 2, "code": "E_BAD_ENUM", "field": "payment_id"} in response.json()["errors"]
