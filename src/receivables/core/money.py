"""Money conversion: integer fils in storage, Decimal at the edges, never float (SPEC §3.5).

1 AED = 100 fils. Conversions use exact integer arithmetic on the Decimal's digits and
exponent, so no decimal context precision or rounding mode can alter a value.
"""

from decimal import Decimal, InvalidOperation
from typing import cast

__all__ = ["from_fils", "to_fils"]

_FILS_EXPONENT = -2


def to_fils(value: Decimal | str | int) -> int:
    """Convert an AED amount to integer fils.

    Accepts ``Decimal``, ``str`` or ``int`` only. Raises ``TypeError`` for floats and every
    other type (including ``bool``), and ``ValueError`` for non-numeric or non-finite input
    and for values that cannot be represented exactly in fils (more than two decimal places
    by value, see docs/DECISIONS.md D-005).
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise TypeError(f"to_fils accepts Decimal, str or int, not {type(value).__name__}")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"not a decimal amount: {value!r}") from None
    if not value.is_finite():
        raise ValueError(f"amount must be finite: {value!r}")

    sign, digits, raw_exponent = value.as_tuple()
    exponent = cast(int, raw_exponent)  # finite, so never 'n', 'N' or 'F'
    coefficient = int("".join(map(str, digits)))
    shift = exponent - _FILS_EXPONENT
    if shift >= 0:
        fils = coefficient * 10**shift
    else:
        fils, remainder = divmod(coefficient, 10**-shift)
        if remainder:
            raise ValueError(f"more than two decimal places: {value!r}")
    return -fils if sign else fils


def from_fils(n: int) -> Decimal:
    """Convert integer fils to an AED ``Decimal`` with exactly two decimal places."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"from_fils accepts int, not {type(n).__name__}")
    digits = tuple(int(d) for d in str(abs(n)))
    return Decimal((1 if n < 0 else 0, digits, _FILS_EXPONENT))
