"""Customer payment history (SPEC §6.3; "Late" per §2). Pure."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from receivables.core.config import EngineConfig
from receivables.core.ledger import LedgerRow

__all__ = ["CustomerHistory", "HistoryResult", "customer_history"]


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    paid: int
    late: int

    @property
    def late_rate(self) -> Fraction:
        return Fraction(self.late, self.paid)


@dataclass(frozen=True, slots=True)
class HistoryResult:
    customers: dict[str, CustomerHistory]
    portfolio_late_rate: Fraction | None


def customer_history(rows: Iterable[LedgerRow], as_of: date, config: EngineConfig) -> HistoryResult:
    """Paid and late counts over invoices settled in ``[as_of - window, as_of]``.

    Late means ``(settled_date - due_date) > grace_days``. Customers with no settled invoice in
    the window are absent; the portfolio rate is ``None`` when nothing settled.
    """
    window_start = as_of - timedelta(days=config.history_window_days)
    paid: dict[str, int] = {}
    late: dict[str, int] = {}
    for row in rows:
        settled = row.settled_date
        if settled is None or not (window_start <= settled <= as_of):
            continue
        paid[row.customer_id] = paid.get(row.customer_id, 0) + 1
        is_late = (settled - row.due_date).days > config.grace_days
        late[row.customer_id] = late.get(row.customer_id, 0) + int(is_late)
    customers = {c: CustomerHistory(paid=paid[c], late=late[c]) for c in sorted(paid)}
    total_paid = sum(paid.values())
    portfolio = Fraction(sum(late.values()), total_paid) if total_paid else None
    return HistoryResult(customers=customers, portfolio_late_rate=portfolio)
