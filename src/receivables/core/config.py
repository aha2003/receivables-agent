"""Engine settings with the SPEC §5 ``engine:`` defaults. Pure: callers pass a config object.

Weights and rates are exact ``Fraction`` values, never floats (docs/DECISIONS.md D-011).
"""

from dataclasses import dataclass
from fractions import Fraction

__all__ = ["EngineConfig"]


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineConfig:
    min_history_invoices: int = 5
    history_window_days: int = 365
    grace_days: int = 0
    weight_overdue: Fraction = Fraction(40, 100)
    weight_history: Fraction = Fraction(25, 100)
    weight_size: Fraction = Fraction(15, 100)
    weight_dispute: Fraction = Fraction(10, 100)
    weight_promise: Fraction = Fraction(10, 100)
    overdue_cap_days: int = 90
    band_high: Fraction = Fraction(60)
    band_medium: Fraction = Fraction(30)
    queue_size: int = 20
    dso_window_days: int = 90
    no_history_fallback_rate: Fraction = Fraction(1, 2)
