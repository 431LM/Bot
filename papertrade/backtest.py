from __future__ import annotations

from dataclasses import asdict
from math import sqrt
from statistics import mean, stdev
from .config import Config
from .data import validate_bars
from .engine import PaperEngine
from .models import Bar, Signal
from .strategy import signal


def backtest(bars_by_ticker: dict[str, list[Bar]], config: Config | None = None) -> dict:
    """Simple chronological paper simulation; signal at completed close, entry next bar open."""
    cfg = config or Config()
    validated: dict[str, list[Bar]] = {}
    issues = {}
    for ticker, bars in bars_by_ticker.items():
        valid, problems = validate_bars(bars, config=cfg)
        validated[ticker] = valid
        if problems:
            issues[ticker] = problems
    if not validated or any(not v for v in validated.values()):
        return {"status": "ERROR", "reason": "missing/invalid data", "data_issues": issues, "trades": 0}
    lengths = [len(v) for v in validated.values()]
    # Align test period by ordinal bar index; assets with different calendars remain independent.
    engine = PaperEngine(cfg, state_path="/tmp/papertrade-backtest-state.json")
    marks: dict[str, float] = {}
    fills: list[dict] = []
    signals = 0
    steps = max(lengths)
    for i in range(steps):
        for ticker, bars in validated.items():
            if i >= len(bars):
                continue
            b = bars[i]
            marks[ticker] = b.close
            if ticker in engine.account.positions:
                fills.extend(engine.on_bar(b, marks))
            # Compute using only bars ending before this bar; fill at current bar open.
            if i >= cfg.min_history and ticker not in engine.account.positions:
                s = signal(bars[:i], cfg.min_history)
                if s:
                    signals += 1
                    entry = Signal(s.ticker, s.side, b.timestamp, b.open, s.atr, s.strategy_version)
                    engine.accept_signal(entry, marks)
                    # No same-bar exits on entry bar: avoid uncertain intrabar ordering at entry.
    equity = engine.account.equity(marks)
    all_fills = engine.account.fills
    exits = [f for f in all_fills if f.get("reason") != "ENTRY"]
    trade_returns = [float(f["net_pnl"]) for f in exits]
    peak = cfg.starting_cash
    max_dd = 0.0
    curve = [cfg.starting_cash]
    # Return series is based on realized exit fills (conservative, excludes interim mark-to-market path).
    for pnl in trade_returns:
        value = curve[-1] + pnl
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak if peak else 0.0)
        curve.append(value)
    wins = [x for x in trade_returns if x > 0]
    losses = [x for x in trade_returns if x < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    returns = [x / cfg.starting_cash for x in trade_returns]
    sharpe = (mean(returns) / stdev(returns) * sqrt(len(returns))) if len(returns) > 1 and stdev(returns) else None
    report = {"status": "NOT VALIDATED", "strategy_version": "ema-rsi-atr-v1", "initial_cash": cfg.starting_cash,
              "ending_equity": equity, "net_pnl": equity-cfg.starting_cash, "signals": signals,
              "trades": len(exits), "open_positions": len(engine.account.positions),
              "win_rate": len(wins)/len(exits) if exits else None,
              "profit_factor": gross_profit/gross_loss if gross_loss else None,
              "max_drawdown_realized_only": max_dd, "sharpe_trade_returns": sharpe,
              "fees": sum(float(f.get("fee", 0)) for f in all_fills), "equity_curve_realized_only": curve,
              "data_issues": issues,
              "caveat": "Ordinal-bar multi-asset alignment; realized-fill-only drawdown; no benchmark or OOS claim. Not production validation."}
    return report


def time_split(bars: list[Bar], train: float = 0.60, validation: float = 0.20) -> tuple[list[Bar], list[Bar], list[Bar]]:
    if not 0 < train < 1 or not 0 < validation < 1 or train + validation >= 1:
        raise ValueError("split proportions must be positive and sum to less than one")
    n = len(bars)
    a, b = int(n*train), int(n*(train+validation))
    return bars[:a], bars[a:b], bars[b:]


def walk_forward_splits(bars: list[Bar], min_train: int, test_size: int, step: int | None = None):
    """Yield expanding-window (train, test) slices with no temporal overlap."""
    if min_train < 1 or test_size < 1:
        raise ValueError("min_train and test_size must be positive")
    step = step or test_size
    if step < 1:
        raise ValueError("step must be positive")
    start = min_train
    while start + test_size <= len(bars):
        yield bars[:start], bars[start:start+test_size]
        start += step
