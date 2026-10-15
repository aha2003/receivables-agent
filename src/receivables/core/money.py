"""Money conversion: integer fils in storage, Decimal at the edges, never float (SPEC §3.5).

1 AED = 100 fils. Conversions use exact integer arithmetic on the Decimal's digits and
exponent, so no decimal context precision or rounding mode can alter a value.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import cast

__all__ = ["MAX_FILS_DIGITS", "from_fils", "parse_written_amount", "to_fils"]

_FILS_EXPONENT = -2

# Python's default int/str conversion limit; anything longer is rejected up front with a
# clear error instead of an accidental one (and exponent forms cannot force huge integers).
MAX_FILS_DIGITS = 4300
_FILS_LIMIT = 10**MAX_FILS_DIGITS


def to_fils(value: Decimal | str | int) -> int:
    """Convert an AED amount to integer fils.

    Accepts ``Decimal``, ``str`` or ``int`` only. Raises ``TypeError`` for floats and every
    other type (including ``bool``), and ``ValueError`` for non-numeric or non-finite input,
    for values that cannot be represented exactly in fils (more than two decimal places by
    value, see docs/DECISIONS.md D-005) and for amounts longer than ``MAX_FILS_DIGITS``.

    String parsing follows ``Decimal`` and is permissive; strict input formats are enforced
    by the import and API validation layers (D-005).
    """
    if isinstance(value, bool) or not isinstance(value, Decimal | str | int):
        raise TypeError(f"to_fils accepts Decimal, str or int, not {type(value).__name__}")
    if isinstance(value, int):
        if abs(value) * 100 >= _FILS_LIMIT:
            raise ValueError("amount out of range")
        return value * 100
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"not a decimal amount: {value[:40]!r}") from None
    if not value.is_finite():
        raise ValueError(f"amount must be finite: {value!r}")

    sign, digit_tuple, raw_exponent = value.as_tuple()
    exponent = cast(int, raw_exponent)  # finite, so never 'n', 'N' or 'F'
    digits = list(digit_tuple)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return 0

    shift = exponent - _FILS_EXPONENT
    if shift < 0:  # the last digit is non-zero and lies below one fils
        raise ValueError("more than two decimal places")
    if len(digits) + shift > MAX_FILS_DIGITS:
        raise ValueError("amount out of range")
    fils = int("".join(map(str, digits))) * 10**shift
    return -fils if sign else fils


def from_fils(n: int) -> Decimal:
    """Convert integer fils to an AED ``Decimal`` with exactly two decimal places."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError(f"from_fils accepts int, not {type(n).__name__}")
    if abs(n) >= _FILS_LIMIT:
        raise ValueError("amount out of range")
    digits = tuple(int(d) for d in str(abs(n)))
    return Decimal((1 if n < 0 else 0, digits, _FILS_EXPONENT))


# An amount as a customer writes it (and the model copies it, D-035): ASCII or Arabic-Indic
# digits, optional comma thousands groups of three, at most two decimals.
_WRITTEN_AMOUNT = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(\.[0-9]{1,2})?")
_ARABIC_SEPARATORS = str.maketrans({"\u066c": ",", "\u066b": "."})
MAX_WRITTEN_AMOUNT_CHARS = 32


def parse_written_amount(value: str) -> Decimal | None:
    """``"47,000"``, ``"47000.00"`` or the same in Arabic-Indic digits as an exact ``Decimal``;
    ``None`` for anything else (other separators, signs, spaces, exponents, more than two
    decimals)."""
    if len(value) > MAX_WRITTEN_AMOUNT_CHARS:
        return None
    text = "".join(
        str(unicodedata.digit(ch)) if ch.isdecimal() else ch
        for ch in value.translate(_ARABIC_SEPARATORS)
    )
    match = _WRITTEN_AMOUNT.fullmatch(text)
    if match is None:
        return None
    return Decimal(match.group(1).replace(",", "") + (match.group(2) or ""))
