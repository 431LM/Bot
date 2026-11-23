from .config import Config
from .models import Bar, Position, Side


def _execution_price(raw: float, side: Side, closing: bool, config: Config) -> float:
    # Adverse half-spread plus slippage on every simulated execution.
    direction = -1 if (side is Side.LONG) == closing else 1
    return raw * (1 + direction * (config.spread_bps / 2 + config.slippage_bps) / 10000)


def _close(position: Position, qty: float, raw_price: float, timestamp: str, reason: str, config: Config) -> dict:
    qty = min(qty, position.quantity)
    price = _execution_price(raw_price, position.side, True, config)
    direction = 1 if position.side is Side.LONG else -1
    gross = direction * (price - position.entry_price) * qty
    fee = abs(price * qty) * config.fee_rate
    position.quantity -= qty
    position.realized_pnl += gross - fee
    position.fees_paid += fee
    return {"ticker": position.ticker, "time": timestamp, "side": position.side.value, "qty": qty,
            "price": price, "gross_pnl": gross, "fee": fee, "net_pnl": gross-fee, "reason": reason}


def open_position(ticker: str, side: Side, timestamp: str, raw_price: float, quantity: float, stop: float,
                  risk_cash: float, sector: str, config: Config) -> tuple[Position, float]:
    price = _execution_price(raw_price, side, False, config)
    fee = price * quantity * config.fee_rate
    p = Position(ticker, side, timestamp, price, quantity, quantity, stop, risk_cash, sector,
                 high_water=price, low_water=price, fees_paid=fee)
    return p, fee


def process_bar(position: Position, bar: Bar, config: Config) -> list[dict]:
    """Conservative OHLC simulation. Stop wins ambiguous stop/target bars; gaps fill at open."""
    if not bar.complete or bar.ticker != position.ticker or min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return []
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
        return []
    fills = []
    long = position.side is Side.LONG
    stop = position.trailing_stop if position.trailing_active and position.trailing_stop is not None else position.stop_price
    stopped = bar.open <= stop if long else bar.open >= stop
    touched = bar.low <= stop if long else bar.high >= stop
    # A stop triggered in an ambiguous bar is applied before any profit target.
    if stopped or touched:
        fills.append(_close(position, position.quantity, bar.open if stopped else stop, bar.timestamp, "STOP", config))
        return fills
    for idx, target in enumerate(config.targets):
        if idx in position.targets_hit:
            continue
        level = position.entry_price * (1 + target if long else 1 - target)
        hit = bar.high >= level if long else bar.low <= level
        if hit:
            gap = (bar.open >= level if long else bar.open <= level)
            px = bar.open if gap else level
            qty = min(position.original_quantity * config.partial_fraction, position.quantity)
            fills.append(_close(position, qty, px, bar.timestamp, f"TP_{target:.3f}", config))
            position.targets_hit.append(idx)
    if len(position.targets_hit) == 3 and position.quantity > 1e-12:
        position.trailing_active = True
        if long:
            position.high_water = max(position.high_water or bar.close, bar.high)
            candidate = position.high_water - config.trailing_atr_multiple * (position.initial_risk_cash / max(position.original_quantity, 1e-12))
            position.trailing_stop = max(position.trailing_stop or position.stop_price, candidate, position.stop_price)
        else:
            position.low_water = min(position.low_water or bar.close, bar.low)
            candidate = position.low_water + config.trailing_atr_multiple * (position.initial_risk_cash / max(position.original_quantity, 1e-12))
            position.trailing_stop = min(position.trailing_stop or position.stop_price, candidate, position.stop_price)
    return fills
