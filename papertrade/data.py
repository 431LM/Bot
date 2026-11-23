from __future__ import annotations

from datetime import datetime, timezone
import math
from .config import Config
from .models import Bar


WATCHLIST = ("NVDA", "AMD", "TSLA", "PLTR", "AAPL", "MSFT", "META", "AMZN", "SAP.DE", "SIE.DE", "RHM.DE", "COIN", "MSTR", "SMCI", "ARM", "UBER")


def validate_bars(bars: list[Bar], now: datetime | None = None, config: Config | None = None) -> tuple[list[Bar], list[str]]:
    """Validate time-ordered OHLCV bars. Invalid rows are rejected; never imputed."""
    cfg = config or Config()
    issues: list[str] = []
    if not bars:
        return [], ["NO_DATA"]
    out: list[Bar] = []
    seen: set[tuple[str, str]] = set()
    previous: datetime | None = None
    ticker = bars[0].ticker
    for b in bars:
        try:
            ts = datetime.fromisoformat(b.timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            issues.append(f"INVALID_TIMESTAMP:{b.ticker}:{b.timestamp}")
            continue
        if ts.tzinfo is None:
            issues.append(f"NAIVE_TIMESTAMP:{b.ticker}:{b.timestamp}")
            continue
        ts = ts.astimezone(timezone.utc)
        key = (b.ticker, ts.isoformat())
        values = (b.open, b.high, b.low, b.close, b.volume)
        if b.ticker != ticker:
            issues.append("MIXED_TICKERS")
            continue
        if key in seen:
            issues.append(f"DUPLICATE_BAR:{b.ticker}:{b.timestamp}")
            continue
        seen.add(key)
        if not all(math.isfinite(x) for x in values) or min(values[:4]) <= 0 or b.volume < 0:
            issues.append(f"NONFINITE_OR_NONPOSITIVE:{b.ticker}:{b.timestamp}")
            continue
        if b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close, b.high):
            issues.append(f"INVALID_OHLC:{b.ticker}:{b.timestamp}")
            continue
        if b.volume < cfg.min_volume:
            issues.append(f"LOW_LIQUIDITY:{b.ticker}:{b.timestamp}")
            continue
        if previous is not None and ts <= previous:
            issues.append(f"OUT_OF_ORDER:{b.ticker}:{b.timestamp}")
            continue
        if out and abs(b.close / out[-1].close - 1) > cfg.max_price_jump:
            issues.append(f"PRICE_JUMP:{b.ticker}:{b.timestamp}")
            continue
        if b.complete:
            out.append(b)
            previous = ts
    if now is not None and out:
        if now.tzinfo is None:
            issues.append("NAIVE_NOW")
        else:
            age = (now.astimezone(timezone.utc) - datetime.fromisoformat(out[-1].timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)).total_seconds()
            if age < 0:
                issues.append("FUTURE_BAR")
            elif age > cfg.max_bar_age_seconds:
                issues.append(f"STALE_DATA:{int(age)}s")
    return out, issues


def healthy_for_trading(issues: list[str]) -> bool:
    return not issues
