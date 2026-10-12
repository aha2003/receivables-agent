"""Golden synthetic fixtures (SPEC §6.8 to §6.10): counts, verbatim content, consistency.

The consistency checks use only date and integer arithmetic to catch transcription errors in
fixtures/expected/golden_results.json; the ledger, scoring and KPI logic is tested in later
steps against these same values.
"""

import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from receivables.core.money import to_fils

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures"
SPEC = (ROOT / "docs" / "SPEC.md").read_text(encoding="utf-8")
CSV_FILES = ["customers.csv", "invoices.csv", "payments.csv", "events.csv", "bad_invoices.csv"]

Loader = Callable[[str], list[dict[str, str]]]


def _section(start: str, end: str) -> str:
    return SPEC[SPEC.index(start) : SPEC.index(end)]


def test_fixture_loader_counts(load_fixture_csv: Loader) -> None:
    assert len(load_fixture_csv("customers.csv")) == 3
    assert len(load_fixture_csv("invoices.csv")) == 14
    assert len(load_fixture_csv("payments.csv")) == 4
    assert len(load_fixture_csv("events.csv")) == 2
    assert len(load_fixture_csv("correspondence_labeled.csv")) == 15
    assert len(load_fixture_csv("bad_invoices.csv")) == 9


@pytest.mark.parametrize("name", CSV_FILES)
def test_csv_is_verbatim_spec_block(name: str) -> None:
    section = _section("### 6.8 ", "### 6.9 ")
    match = re.search(rf"\*\*{re.escape(name)}\*\*[^\n]*\n```\n(.*?)```", section, re.S)
    assert match is not None, name
    assert (FIXTURES / name).read_bytes() == match.group(1).encode("utf-8")


def test_labelled_messages_match_spec_table(load_fixture_csv: Loader) -> None:
    section = _section("### 6.10 ", "## 7. ")
    expected = []
    for line in section.splitlines():
        m = re.match(r"^\| (M-\d{2}) \| (.*?) \| (en|ar) \| (.*) \| ([A-Z_]+) \|$", line)
        if m:
            mid, inv, lang, body, label = m.groups()
            expected.append(
                {
                    "message_id": mid,
                    "invoice_number": "" if inv == "none" else inv,
                    "language": lang,
                    "body": body,
                    "expected_label": label,
                }
            )
    assert len(expected) == 15
    assert load_fixture_csv("correspondence_labeled.csv") == expected


def test_fixture_files_are_utf8_without_bom() -> None:
    for path in sorted(FIXTURES.rglob("*")):
        if path.is_file() and path.suffix in {".csv", ".json"}:
            raw = path.read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), path
            assert b"\r" not in raw, path
            raw.decode("utf-8")


# --- golden results: internal consistency -------------------------------------------------


def _bucket_for(days: int) -> str:
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def test_days_overdue_and_buckets_follow_due_dates(
    golden_results: dict[str, Any], load_fixture_csv: Loader
) -> None:
    as_of = date.fromisoformat(golden_results["as_of"])
    due = {
        r["invoice_number"]: date.fromisoformat(r["due_date"])
        for r in load_fixture_csv("invoices.csv")
    }
    for inv, item in golden_results["open_invoices"].items():
        assert item["days_overdue"] == (as_of - due[inv]).days, inv
        assert item["bucket"] == _bucket_for(item["days_overdue"]), inv


def test_open_and_closed_cover_every_invoice(
    golden_results: dict[str, Any], load_fixture_csv: Loader
) -> None:
    all_invoices = {r["invoice_number"] for r in load_fixture_csv("invoices.csv")}
    open_ = set(golden_results["open_invoices"])
    closed = set(golden_results["closed_invoices"])
    assert open_ | closed == all_invoices
    assert not open_ & closed


def test_open_amounts_are_invoice_minus_allocated_payments(
    golden_results: dict[str, Any], load_fixture_csv: Loader
) -> None:
    paid: dict[str, int] = {}
    for p in load_fixture_csv("payments.csv"):
        paid[p["invoice_number"]] = paid.get(p["invoice_number"], 0) + to_fils(p["amount"])
    for r in load_fixture_csv("invoices.csv"):
        inv = r["invoice_number"]
        remaining = to_fils(r["amount"]) - paid.get(inv, 0)
        if inv in golden_results["open_invoices"]:
            assert to_fils(golden_results["open_invoices"][inv]["open_amount"]) == remaining, inv
        else:
            assert remaining == 0, inv


def test_totals_and_buckets_add_up(golden_results: dict[str, Any]) -> None:
    items = golden_results["open_invoices"].values()
    total = sum(to_fils(i["open_amount"]) for i in items)
    assert total == to_fils(golden_results["total_open"])
    buckets: dict[str, int] = {}
    for i in items:
        buckets[i["bucket"]] = buckets.get(i["bucket"], 0) + to_fils(i["open_amount"])
    assert buckets == {k: to_fils(v) for k, v in golden_results["bucket_totals"].items()}
    overdue = total - buckets["current"]
    assert overdue == to_fils(golden_results["total_overdue"])


def test_overdue_by_customer_and_exposure_add_up(
    golden_results: dict[str, Any], load_fixture_csv: Loader
) -> None:
    customer = {r["invoice_number"]: r["customer_id"] for r in load_fixture_csv("invoices.csv")}
    overdue_total = to_fils(golden_results["total_overdue"])
    by_customer: dict[str, int] = {}
    for inv, item in golden_results["open_invoices"].items():
        if item["days_overdue"] > 0:
            cust = customer[inv]
            by_customer[cust] = by_customer.get(cust, 0) + to_fils(item["open_amount"])
    rows = golden_results["overdue_by_customer"]
    assert {r["customer_id"]: to_fils(r["amount"]) for r in rows} == by_customer
    amounts = [to_fils(r["amount"]) for r in rows]
    assert amounts == sorted(amounts, reverse=True)
    for r in rows:
        share = (Decimal(to_fils(r["amount"])) * 100 / overdue_total).quantize(Decimal("0.01"))
        assert share == Decimal(r["share_pct"]), r["customer_id"]
    exposure = golden_results["exposure_by_band"]
    assert sum(to_fils(v) for v in exposure.values()) == overdue_total


def test_queue_rows_are_overdue_invoices_sorted_by_priority(golden_results: dict[str, Any]) -> None:
    rows = golden_results["queue_default"]["rows"]
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    overdue = {i for i, v in golden_results["open_invoices"].items() if v["days_overdue"] > 0}
    assert {r["invoice_number"] for r in rows} == overdue
    values = [Decimal(r["priority_value"]) for r in rows]
    assert values == sorted(values, reverse=True)
    band_totals: dict[str, int] = {}
    for r in rows:
        risk = Decimal(r["risk"])
        expected_band = "High" if risk >= 60 else "Medium" if risk >= 30 else "Low"
        assert r["band"] == expected_band, r["invoice_number"]
        amount = to_fils(golden_results["open_invoices"][r["invoice_number"]]["open_amount"])
        band_totals[r["band"]] = band_totals.get(r["band"], 0) + amount
    assert band_totals == {k: to_fils(v) for k, v in golden_results["exposure_by_band"].items()}


def test_min_history_2_order_follows_changed_priorities(golden_results: dict[str, Any]) -> None:
    alt = golden_results["queue_min_history_2"]
    values = {
        r["invoice_number"]: Decimal(r["priority_value"])
        for r in golden_results["queue_default"]["rows"]
    }
    for inv, change in alt["changed_rows"].items():
        values[inv] = Decimal(change["priority_value"])
        risk = Decimal(change["risk"])
        assert 30 <= risk < 60, inv  # bands unchanged, so exposure is unchanged
    assert alt["order"] == sorted(values, key=lambda i: values[i], reverse=True)


def test_time_travel_matches_fixture_dates(
    golden_results: dict[str, Any], load_fixture_csv: Loader
) -> None:
    tt = golden_results["time_travel"]
    as_of = tt["as_of"]
    invoices = load_fixture_csv("invoices.csv")
    in_ledger = [r["invoice_number"] for r in invoices if r["invoice_date"] <= as_of]
    assert len(in_ledger) == tt["ledger_invoice_count"]
    assert {r["invoice_number"] for r in invoices} - set(in_ledger) == set(tt["absent_invoices"])
    ignored_payments = {
        p["payment_id"] for p in load_fixture_csv("payments.csv") if p["payment_date"] > as_of
    }
    assert ignored_payments == set(tt["ignored_payments"])
    events = load_fixture_csv("events.csv")
    assert {e["event_id"] for e in events if e["event_date"] > as_of} == set(tt["ignored_events"])
    assert {e["event_id"] for e in events if e["event_date"] <= as_of} == set(tt["active_events"])


def test_amounts_are_strings_never_floats(golden_results: dict[str, Any]) -> None:
    def walk(node: object) -> None:
        assert not isinstance(node, float), node
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(golden_results)


# --- synthetic data only ------------------------------------------------------------------


def test_customers_are_synthetic(load_fixture_csv: Loader) -> None:
    for c in load_fixture_csv("customers.csv"):
        assert re.fullmatch(r"CUST-[A-Z]", c["customer_id"])
        assert c["contact_email"].endswith(".example"), c["contact_email"]
        assert re.fullmatch(r"1000000[0-9]{8}", c["trn"]), c["trn"]
        assert c["trade_license_no"].startswith("TL-")


def test_identifiers_follow_synthetic_patterns(load_fixture_csv: Loader) -> None:
    for r in load_fixture_csv("invoices.csv"):
        assert re.fullmatch(r"INV-\d{3}", r["invoice_number"])
        assert re.fullmatch(r"CUST-[A-C]", r["customer_id"])
    for r in load_fixture_csv("payments.csv"):
        assert re.fullmatch(r"P-\d{3}", r["payment_id"])
    for r in load_fixture_csv("correspondence_labeled.csv"):
        assert re.fullmatch(r"M-\d{2}", r["message_id"])
        assert r["invoice_number"] == "" or re.fullmatch(r"INV-\d{3}", r["invoice_number"])
