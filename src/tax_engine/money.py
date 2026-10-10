"""Money is Decimal, quantised to cents with half-up rounding. Floats are rejected."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse(value: object) -> Decimal:
    """Parse an amount from a string or int. Floats are refused: they cannot hold cents exactly."""
    if isinstance(value, bool | float):
        raise TypeError(f"refusing to parse {type(value).__name__} as money: {value!r}")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc


def fmt(value: Decimal) -> str:
    """Format for people: $1,234.56 (negative as -$1,234.56)."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(q(value)):,.2f}"
