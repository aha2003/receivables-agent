"""The seeded demo company (SPEC §19.2; D-047). SYNTHETIC DEMO DATA, fictional throughout.

Pure: no I/O or clock; every random value comes from ``random.Random(scenario.seed)``.
§19.2 names the generator and the ranges but not the order of the draws, so this order is part
of what "same seed, same data" means here (pinned by a hash in tests/sim/test_sim_company.py):
for each customer in turn, for each of its invoices, the due-date offset and then the amount.

- Customers ``SIM-C01``… take the archetypes in blocks (C01 to C03 the first archetype, and so on);
  ``archetype_index`` (0, 1, 2) is the "customer index" §19.2 cycles by.
- An invoice falls due ``due_offset`` days after the start (day 0 to day 30) and is dated
  ``payment_terms_days`` before that, so every invoice exists on day 0; amounts are
  ``amount_step_aed * rng.randint(min, max)`` AED. A customer's invoices are numbered in due
  order: ``SIM-001``… across the company.
- A claims-paid customer holds one unallocated payment (``SIM-P01``…) equal to its oldest
  invoice (earliest due date), received on day 0 (D-047 option 1a).
"""

import csv
import hashlib
import io
import json
import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from receivables.core.money import from_fils
from receivables.sim.scenario import Archetype, Scenario

__all__ = [
    "SimCompany",
    "SimCustomer",
    "SimInvoice",
    "SimPayment",
    "company_csv",
    "generate_company",
]

_FILS_PER_AED = 100


@dataclass(frozen=True, slots=True)
class SimCustomer:
    customer_id: str
    customer_name: str
    trn: str
    trade_license_no: str
    payment_terms_days: int
    contact_email: str
    archetype: Archetype
    archetype_index: int


@dataclass(frozen=True, slots=True)
class SimInvoice:
    invoice_number: str
    customer_id: str
    invoice_date: date
    due_date: date
    amount_fils: int
    po_number: str


@dataclass(frozen=True, slots=True)
class SimPayment:
    payment_id: str
    customer_id: str
    payment_date: date
    amount_fils: int
    invoice_number: None = None  # unallocated cash


@dataclass(frozen=True, slots=True)
class SimCompany:
    seed: int
    currency: str
    customers: tuple[SimCustomer, ...]
    invoices: tuple[SimInvoice, ...]
    payments: tuple[SimPayment, ...]

    def canonical_json(self) -> str:
        """Sorted keys, no spaces, dates as ISO strings: the bytes the hash is taken over."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()


def _customer(scenario: Scenario, n: int) -> SimCustomer:
    """Customer ``n`` (1-based); identifiers derive from ``n`` alone, never from the RNG."""
    per = scenario.customers_per_archetype
    return SimCustomer(
        customer_id=f"SIM-C{n:02d}",
        customer_name=f"Sim Customer {n:02d} LLC",
        trn=f"300{n:09d}003",
        trade_license_no=f"TL-SIM-{n:02d}",
        payment_terms_days=scenario.payment_terms_days,
        contact_email=f"ap@sim-c{n:02d}.example",
        archetype=scenario.archetypes[(n - 1) // per],
        archetype_index=(n - 1) % per,
    )


def generate_company(scenario: Scenario) -> SimCompany:
    rng = random.Random(scenario.seed)  # noqa: S311 - reproducible simulation, not security
    terms = timedelta(days=scenario.payment_terms_days)
    customers: list[SimCustomer] = []
    invoices: list[SimInvoice] = []
    payments: list[SimPayment] = []
    for n in range(1, scenario.customers + 1):
        customer = _customer(scenario, n)
        customers.append(customer)
        drawn: list[tuple[date, int]] = []
        for _ in range(scenario.invoices_per_customer):
            offset = rng.randint(scenario.due_offset_min_days, scenario.due_offset_max_days)
            units = rng.randint(scenario.amount_units_min, scenario.amount_units_max)
            due = scenario.start_date + timedelta(days=offset)
            drawn.append((due, units * scenario.amount_step_aed * _FILS_PER_AED))
        mine: list[SimInvoice] = []
        for due, amount in sorted(drawn, key=lambda d: d[0]):  # stable: draw order on ties
            seq = len(invoices) + 1
            mine.append(
                SimInvoice(
                    invoice_number=f"SIM-{seq:03d}",
                    customer_id=customer.customer_id,
                    invoice_date=due - terms,
                    due_date=due,
                    amount_fils=amount,
                    po_number=f"PO-S-{seq:03d}",
                )
            )
            invoices.append(mine[-1])
        if customer.archetype is Archetype.CLAIMS_PAID:
            payments.append(
                SimPayment(
                    payment_id=f"SIM-P{len(payments) + 1:02d}",
                    customer_id=customer.customer_id,
                    payment_date=scenario.start_date,
                    amount_fils=mine[0].amount_fils,  # the oldest: first in due order
                )
            )
    return SimCompany(
        seed=scenario.seed,
        currency=scenario.currency,
        customers=tuple(customers),
        invoices=tuple(invoices),
        payments=tuple(payments),
    )


def _csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue().encode("ascii")


def company_csv(company: SimCompany) -> dict[str, bytes]:
    """The company as the canonical CSV files (SPEC §4.6), in import order."""
    return {
        "customers": _csv(
            ("customer_id", "customer_name", "trn", "trade_license_no", "payment_terms_days",
             "contact_email"),
            ((c.customer_id, c.customer_name, c.trn, c.trade_license_no, c.payment_terms_days,
              c.contact_email) for c in company.customers),
        ),
        "invoices": _csv(
            ("invoice_number", "customer_id", "invoice_date", "due_date", "amount", "currency",
             "po_number"),
            ((i.invoice_number, i.customer_id, i.invoice_date.isoformat(), i.due_date.isoformat(),
              from_fils(i.amount_fils), company.currency, i.po_number) for i in company.invoices),
        ),
        "payments": _csv(
            ("payment_id", "invoice_number", "customer_id", "payment_date", "amount",
             "payment_type"),
            ((p.payment_id, "", p.customer_id, p.payment_date.isoformat(),
              from_fils(p.amount_fils), "payment") for p in company.payments),
        ),
    }  # fmt: skip
