"""The seeded demo-company generator (SPEC §19.2; S4.01; D-047): same seed, same company."""

import re
from collections import Counter
from datetime import timedelta

import pytest

from receivables.sim.company import company_csv, generate_company
from receivables.sim.scenario import Archetype, load_scenario

# SHA-256 of seed 42's company as canonical JSON. It pins this implementation's draw order
# (§19.2 names the generator and ranges, not the order of the draws); a change to the draw
# order or the data changes it and needs a decision in docs/DECISIONS.md.
SEED_42_SHA256 = "4474221a2f935fa7b7df78bd6337bf5af0037e751c7cb8c03c17bbb4c32c0903"


@pytest.mark.spec("T-SIM-01")
def test_the_same_seed_gives_an_identical_company() -> None:
    first = generate_company(load_scenario())
    second = generate_company(load_scenario())
    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert first.sha256() == second.sha256() == SEED_42_SHA256


@pytest.mark.spec("T-SIM-01")
def test_another_seed_gives_another_company() -> None:
    assert generate_company(load_scenario(seed=43)).sha256() != SEED_42_SHA256


def test_twelve_customers_sixty_invoices_five_each() -> None:
    company = generate_company(load_scenario())
    assert len(company.customers) == 12 and len(company.invoices) == 60
    per_customer = Counter(i.customer_id for i in company.invoices)
    assert set(per_customer) == {c.customer_id for c in company.customers}
    assert set(per_customer.values()) == {5}
    assert len({i.invoice_number for i in company.invoices}) == 60


def test_three_customers_of_each_archetype_in_blocks() -> None:
    company = generate_company(load_scenario())
    order = [Archetype.RESPONSIVE, Archetype.DISPUTER, Archetype.SILENT, Archetype.CLAIMS_PAID]
    assert [c.archetype for c in company.customers] == [a for a in order for _ in range(3)]
    assert [c.archetype_index for c in company.customers] == [0, 1, 2] * 4
    assert [c.customer_id for c in company.customers] == [f"SIM-C{n:02d}" for n in range(1, 13)]


def test_amounts_terms_and_dates_follow_19_2() -> None:
    s = load_scenario()
    company = generate_company(s)
    start = s.start_date
    assert {c.payment_terms_days for c in company.customers} == {30}
    for inv in company.invoices:
        aed, rest = divmod(inv.amount_fils, 100)
        assert rest == 0 and aed % 100 == 0 and 5_000 <= aed <= 60_000
        assert inv.due_date - inv.invoice_date == timedelta(days=30)
        assert 0 <= (inv.due_date - start).days <= 30  # falls due between day 0 and day 30
        assert inv.invoice_date <= start  # every invoice exists when the run starts
    assert len({i.amount_fils for i in company.invoices}) > 30  # amounts really are drawn
    assert len({i.due_date for i in company.invoices}) > 15


def test_a_customers_invoices_are_numbered_by_due_date() -> None:
    company = generate_company(load_scenario())
    for customer in company.customers:
        mine = [i for i in company.invoices if i.customer_id == customer.customer_id]
        assert [i.due_date for i in mine] == sorted(i.due_date for i in mine)
        assert [i.invoice_number for i in mine] == sorted(i.invoice_number for i in mine)


def test_claims_paid_customers_hold_cash_equal_to_their_oldest_invoice() -> None:
    s = load_scenario()
    company = generate_company(s)
    claims = [c for c in company.customers if c.archetype is Archetype.CLAIMS_PAID]
    assert [p.customer_id for p in company.payments] == [c.customer_id for c in claims]
    for payment in company.payments:
        mine = [i for i in company.invoices if i.customer_id == payment.customer_id]
        oldest = min(mine, key=lambda i: (i.due_date, i.invoice_number))
        assert payment.amount_fils == oldest.amount_fils
        assert payment.payment_date == s.start_date  # received before the first tick
        assert payment.invoice_number is None  # unallocated cash


def test_identifiers_are_synthetic_and_do_not_look_like_each_other() -> None:
    company = generate_company(load_scenario())
    invoice_regex = re.compile(r"\b[A-Z]{2,6}-\d{2,}\b")  # config.yaml drafts.invoice_number_regex
    for inv in company.invoices:
        assert invoice_regex.fullmatch(inv.invoice_number)
        assert not invoice_regex.search(inv.po_number)  # a PO is never read as an invoice
    for customer in company.customers:
        assert not invoice_regex.search(customer.customer_id)
        assert customer.contact_email.endswith(".example")
        assert re.fullmatch(r"[0-9]{15}", customer.trn)
    for payment in company.payments:
        assert not invoice_regex.search(payment.payment_id)
    golden_ids = {"CUST-A", "CUST-B", "CUST-C", "CUST-D"}
    assert not golden_ids & {c.customer_id for c in company.customers}


def test_the_files_are_the_canonical_csv_schema() -> None:
    files = company_csv(generate_company(load_scenario()))
    assert list(files) == ["customers", "invoices", "payments"]
    headers = {kind: raw.decode("ascii").split("\n", 1)[0] for kind, raw in files.items()}
    assert headers == {
        "customers": "customer_id,customer_name,trn,trade_license_no,payment_terms_days,"
        "contact_email",
        "invoices": "invoice_number,customer_id,invoice_date,due_date,amount,currency,po_number",
        "payments": "payment_id,invoice_number,customer_id,payment_date,amount,payment_type",
    }
    # The strict importer accepting every row is checked in test_sim_db.py.
