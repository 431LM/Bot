from .config import Config
from .models import Account, Position, Side, Signal


def size_position(account: Account, signal: Signal, config: Config, marks: dict[str, float], sector: str = "OTHER") -> tuple[float, str | None]:
    """Risk-based quantity, capped by leverage, portfolio risk and group exposure."""
    if account.daily_halt or account.risk_halt:
        return 0.0, "risk halt active"
    if signal.ticker in account.positions:
        return 0.0, "position already open"
    if len(account.positions) >= config.max_positions:
        return 0.0, "maximum open positions reached"
    if signal.close <= 0 or signal.atr <= 0:
        return 0.0, "invalid price or ATR"
    group = config.asset_groups.get(signal.ticker, sector)
    grouped = sum(config.asset_groups.get(t, p.sector) == group for t, p in account.positions.items())
    if grouped >= config.max_correlated_positions:
        return 0.0, "correlated-asset position cap reached"
    equity = account.equity(marks)
    stop_distance = config.stop_atr_multiple * signal.atr
    per_unit_risk = stop_distance
    risk_budget = min(equity * config.risk_per_trade, equity * config.max_position_loss)
    used_risk = sum(p.initial_risk_cash * p.quantity / p.original_quantity for p in account.positions.values())
    risk_budget = min(risk_budget, max(0.0, equity * config.max_portfolio_risk - used_risk))
    qty = risk_budget / per_unit_risk if per_unit_risk else 0.0
    max_notional = max(0.0, min(equity * config.max_leverage, account.cash * config.max_leverage))
    qty = min(qty, max_notional / signal.close)
    return (qty, None) if qty > 0 else (0.0, "risk or leverage budget exhausted")


def update_kill_switches(account: Account, equity: float, day: str, config: Config) -> list[str]:
    events = []
    if day != account.day:
        account.day = day
        account.day_start_equity = equity
        account.daily_halt = False
    account.peak_equity = max(account.peak_equity, equity)
    if equity <= account.day_start_equity * (1 - config.daily_loss_limit) and not account.daily_halt:
        account.daily_halt = True
        events.append("DAILY_LOSS_LIMIT")
    if equity <= account.peak_equity * (1 - config.max_drawdown) and not account.risk_halt:
        account.risk_halt = True
        events.append("MAX_DRAWDOWN")
    return events
