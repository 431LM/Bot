from datetime import datetime, timezone
from .config import Config
from .execution import open_position, process_bar
from .models import Account, Bar, Signal
from .risk import size_position, update_kill_switches
from .store import load_account, save_account


class PaperEngine:
    """Orchestration for deterministic paper-only state transitions."""
    def __init__(self, config: Config | None = None, state_path: str = "paper_state.json"):
        self.config = config or Config()
        self.state_path = state_path
        self.account = load_account(state_path, self.config.starting_cash)
        self.seen: set[str] = set()

    def accept_signal(self, signal: Signal, marks: dict[str, float], sector: str = "OTHER") -> str:
        key = f"{signal.ticker}|{signal.timestamp}|{signal.side.value}|{signal.strategy_version}"
        if key in self.seen:
            return "DUPLICATE"
        qty, reason = size_position(self.account, signal, self.config, marks, sector)
        if reason:
            return "REJECTED: " + reason
        self.seen.add(key)
        stop = signal.close - self.config.stop_atr_multiple * signal.atr if signal.side.value == "LONG" else signal.close + self.config.stop_atr_multiple * signal.atr
        p, fee = open_position(signal.ticker, signal.side, signal.timestamp, signal.close, qty, stop, qty * self.config.stop_atr_multiple * signal.atr, sector, self.config)
        self.account.cash -= fee
        self.account.positions[signal.ticker] = p
        self.account.fills.append({"ticker": signal.ticker, "time": signal.timestamp, "side": signal.side.value,
                                   "qty": qty, "price": p.entry_price, "fee": fee, "reason": "ENTRY", "signal_id": key})
        save_account(self.state_path, self.account)
        return "ACCEPTED"

    def on_bar(self, bar: Bar, marks: dict[str, float] | None = None) -> list[dict]:
        if not bar.complete:
            return []
        marks = marks or {bar.ticker: bar.close}
        p = self.account.positions.get(bar.ticker)
        if p is None:
            return []
        fills = process_bar(p, bar, self.config)
        for fill in fills:
            self.account.cash += fill["gross_pnl"] - fill["fee"]
            self.account.fills.append(fill)
        if p.quantity <= 1e-12:
            del self.account.positions[bar.ticker]
        equity = self.account.equity({**marks, bar.ticker: bar.close})
        day = bar.timestamp[:10]
        update_kill_switches(self.account, equity, day, self.config)
        save_account(self.state_path, self.account)
        return fills
