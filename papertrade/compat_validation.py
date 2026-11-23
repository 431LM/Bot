"""Compatibility validation utilities for the paper-trading package."""

from .models import Bar


def validate_ohlc_sequence(bars: list[Bar]) -> list[str]:
    if not bars:
        return ["NO_DATA"]
    problems: list[str] = []
    for i, bar in enumerate(bars):
        if not bar.complete:
            problems.append(f"INCOMPLETE_BAR:{bar.timestamp}")
        if bar.low <= 0 or bar.high < bar.low or bar.open <= 0 or bar.close <= 0:
            problems.append(f"INVALID_OHLC:{bar.timestamp}")
        if i and bar.timestamp <= bars[i - 1].timestamp:
            problems.append(f"NON_MONOTONIC_TIME:{bar.timestamp}")
    return problems
