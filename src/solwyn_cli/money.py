"""Dollar amounts as tables show them."""

import math


def money(value: float) -> str:
    """At least two decimals, and up to six when an amount has sub-cent
    digits: a $0.002 cap reads 0.002, not 0.00. Float noise past the sixth
    decimal (0.0014810000000000001) is dropped."""
    if not math.isfinite(value):
        return str(value)
    text = f"{value:.6f}".rstrip("0")
    whole, _, decimals = text.partition(".")
    if value != 0 and float(text) == 0:
        return "<0.000001" if value > 0 else "-<0.000001"
    if whole == "-0" and not decimals.strip("0"):
        whole = "0"
    return f"{whole}.{decimals.ljust(2, '0')}"
