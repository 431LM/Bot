from .models import Bar, Side, Signal

VERSION = "ema-rsi-atr-v1"


def ema(values: list[float], period: int) -> list[float]:
    if period < 1:
        raise ValueError("period must be positive")
    out = []
    alpha = 2 / (period + 1)
    for value in values:
        out.append(value if not out else alpha * value + (1 - alpha) * out[-1])
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return []
    gains, losses = [], []
    for a, b in zip(values, values[1:]):
        gains.append(max(b - a, 0.0)); losses.append(max(a - b, 0.0))
    ag, al = gains[0], losses[0]
    result = [50.0]
    for i, (g, loss) in enumerate(zip(gains, losses)):
        if i:
            ag = ((period - 1) * ag + g) / period
            al = ((period - 1) * al + loss) / period
        result.append(100.0 if al == 0 and ag > 0 else 50.0 if al == ag == 0 else 100 - 100 / (1 + ag / al))
    return result


def atr(bars: list[Bar], period: int = 14) -> list[float]:
    tr = []
    for i, b in enumerate(bars):
        prev = bars[i - 1].close if i else b.close
        tr.append(max(b.high - b.low, abs(b.high - prev), abs(b.low - prev)))
    out = []
    for x in tr:
        out.append(x if not out else ((period - 1) * out[-1] + x) / period)
    return out


def signal(bars: list[Bar], min_history: int = 60) -> Signal | None:
    """Evaluate only the last complete bar; caller must supply chronological history."""
    if len(bars) < min_history or not bars[-1].complete:
        return None
    if any(not b.complete for b in bars):
        # Never silently mix partial bars into indicators.
        bars = [b for b in bars if b.complete]
    if len(bars) < min_history:
        return None
    closes = [b.close for b in bars]
    e20, e50, rs, ats = ema(closes, 20), ema(closes, 50), rsi(closes), atr(bars)
    i = len(bars) - 1
    side = None
    if e20[i] > e50[i] and closes[i] > e20[i] and 50 <= rs[i] <= 75:
        side = Side.LONG
    elif e20[i] < e50[i] and closes[i] < e20[i] and 25 <= rs[i] <= 50:
        side = Side.SHORT
    if side is None or ats[i] <= 0:
        return None
    return Signal(bars[i].ticker, side, bars[i].timestamp, closes[i], ats[i])
