from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    starting_cash: float = 100.0
    risk_per_trade: float = 0.01
    max_positions: int = 3
    max_leverage: float = 5.0
    max_portfolio_risk: float = 0.03
    daily_loss_limit: float = 0.05
    max_drawdown: float = 0.20
    max_position_loss: float = 0.03
    max_sector_positions: int = 2
    stop_atr_multiple: float = 2.0
    targets: tuple[float, ...] = (0.05, 0.075, 0.10)
    partial_fraction: float = 0.25
    trailing_atr_multiple: float = 2.0
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    spread_bps: float = 5.0
    min_history: int = 60
    max_bar_age_seconds: int = 86400
    max_price_jump: float = 0.50
    min_volume: float = 0.0
    # Optional conservative grouping; assets in one group share a position cap.
    asset_groups: dict[str, str] = field(default_factory=dict)
    max_correlated_positions: int = 2

    def __post_init__(self):
        if self.starting_cash <= 0 or not 0 < self.risk_per_trade <= 0.01:
            raise ValueError("starting_cash must be positive and risk_per_trade in (0, 1%]")
        if self.max_positions < 1 or not 1 <= self.max_leverage <= 5:
            raise ValueError("max_positions >= 1 and max_leverage in [1, 5] required")
        if not 0 < self.daily_loss_limit <= 0.05 or not 0 < self.max_drawdown <= 1:
            raise ValueError("loss limits must be positive; daily limit cannot exceed 5%")
        if len(self.targets) != 3 or tuple(sorted(self.targets)) != self.targets:
            raise ValueError("exactly three ascending profit targets are required")
        if abs(sum([self.partial_fraction] * 3) - 0.75) > 1e-9:
            raise ValueError("three partial exits must each close 25% of original size")
        if min(self.fee_rate, self.slippage_bps, self.spread_bps) < 0:
            raise ValueError("execution costs cannot be negative")
