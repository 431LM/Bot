from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class Bar:
    ticker: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool = True


@dataclass(frozen=True)
class Signal:
    ticker: str
    side: Side
    timestamp: str
    close: float
    atr: float
    strategy_version: str = "ema-rsi-atr-v1"


@dataclass
class Position:
    ticker: str
    side: Side
    entry_time: str
    entry_price: float
    quantity: float
    original_quantity: float
    stop_price: float
    initial_risk_cash: float
    sector: str = "OTHER"
    targets_hit: list[int] = field(default_factory=list)
    trailing_active: bool = False
    trailing_stop: Optional[float] = None
    high_water: Optional[float] = None
    low_water: Optional[float] = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0


@dataclass
class Account:
    cash: float
    peak_equity: float
    day_start_equity: float
    day: str
    daily_halt: bool = False
    risk_halt: bool = False
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    last_error: Optional[str] = None

    def equity(self, marks: dict[str, float]) -> float:
        value = self.cash
        for ticker, p in self.positions.items():
            mark = marks.get(ticker, p.entry_price)
            direction = 1 if p.side is Side.LONG else -1
            value += direction * (mark - p.entry_price) * p.quantity
        return value
