"""T-MONEY-01: integer fils in storage, Decimal at the edges, never float (SPEC §3.5, §3.6)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.core.money import MAX_FILS_DIGITS, from_fils, to_fils


class TestMoney01:
    def test_decimal_to_fils(self) -> None:
        assert to_fils(Decimal("50000.00")) == 5000000

    def test_from_fils_returns_two_place_decimal(self) -> None:
        result = from_fils(5000000)
        assert result == Decimal("50000.00")
        assert str(result) == "50000.00"

    def test_string_addition_is_exact(self) -> None:
        assert to_fils("0.10") + to_fils("0.20") == to_fils("0.30")

    def test_float_rejected(self) -> None:
        with pytest.raises(TypeError):
            to_fils(0.1)  # pyright: ignore[reportArgumentType]

    def test_more_than_two_places_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_fils(Decimal("10.005"))

    @given(st.integers(min_value=0))
    def test_round_trip(self, n: int) -> None:
        assert to_fils(from_fils(n)) == n


@pytest.mark.parametrize("value", [True, False, None, [1], b"1.00"])
def test_other_types_rejected(value: object) -> None:
    with pytest.raises(TypeError):
        to_fils(value)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("value", [1.0, "1.00", Decimal("1.00"), True])
def test_from_fils_requires_int(value: object) -> None:
    with pytest.raises(TypeError):
        from_fils(value)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "value",
    ["abc", "", "NaN", "Infinity", "-Infinity", Decimal("NaN"), Decimal("sNaN"), Decimal("Inf")],
)
def test_non_numeric_or_non_finite_rejected(value: str | Decimal) -> None:
    with pytest.raises(ValueError):
        to_fils(value)


@pytest.mark.parametrize("value", ["0.001", "-0.005", Decimal("1E-3")])
def test_sub_fils_precision_rejected(value: str | Decimal) -> None:
    with pytest.raises(ValueError):
        to_fils(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 500),
        (0, 0),
        ("10.5", 1050),
        ("-250.75", -25075),
        (Decimal("10.000"), 1000),  # D-005: judged by value; exact in fils
        (Decimal("1E+3"), 100000),
    ],
)
def test_accepted_values(value: int | str | Decimal, expected: int) -> None:
    assert to_fils(value) == expected


@given(st.integers())
def test_round_trip_including_negatives(n: int) -> None:
    assert to_fils(from_fils(n)) == n


@pytest.mark.parametrize("value", ["0E-50000000", "-0.00", Decimal("-0"), "000.00"])
def test_zero_forms(value: str | Decimal) -> None:
    assert to_fils(value) == 0


@pytest.mark.parametrize("value", ["1E+30000000", Decimal("1E+999999999"), "1" + "0" * 4300])
def test_huge_amounts_rejected_quickly(value: str | Decimal) -> None:
    with pytest.raises(ValueError, match="out of range"):
        to_fils(value)


def test_tiny_exponent_rejected_quickly() -> None:
    with pytest.raises(ValueError, match="decimal places"):
        to_fils("1E-30000000")


def test_int_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        to_fils(10**MAX_FILS_DIGITS)


def test_from_fils_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        from_fils(-(10**MAX_FILS_DIGITS))


def test_largest_amount_round_trips() -> None:
    n = 10**MAX_FILS_DIGITS - 1
    assert to_fils(from_fils(n)) == n


def test_trailing_zeros_beyond_two_places_accepted() -> None:
    assert to_fils("10.5000000000000000000000000000000000") == 1050
