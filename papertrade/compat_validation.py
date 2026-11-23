from .models import Bar


def validate_ohlc_sequence(bars: list[Bar]) -> list[str]:
    """Compatibility validator for existing modules."""
    problems = []
    if not bars:
        return ["NO_DATA"]
    for i, b in enumerate(bars):
        if not b.complete:
            problems.append(f"INCOMPLETE_BAR:{b.timestamp}")
        if b.low <= 0 or b.high < b.low or b.open <= 0 or b.close <= 0:
            problems.append(f"INVALID_OHLC:{b.timestamp}")
        if i and b.timestamp <= bars[i-1].timestamp:
            problems.append(f"NON_MONOTONIC_TIME:{b.timestamp}")
    return problems
