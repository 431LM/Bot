# Paper-trading baseline (initial implementation)

This package is an isolated, deterministic simulator. It does not contain broker integrations or submit orders. It does not change the existing radar, pattern, scanner, or watcher systems.

## First implementation slice

- `papertrade/strategy.py`: EMA20/EMA50, RSI14, ATR14 baseline; incomplete final bars and insufficient history produce no signal.
- `papertrade/risk.py`: risk-based sizing, a 1% risk-per-entry ceiling, 5x notional ceiling, portfolio risk limit, position/group caps, daily 5% halt and drawdown halt.
- `papertrade/execution.py`: paper fills, adverse spread/slippage and fees, stop-first ambiguous OHLC execution, gap-aware stops, three 25%-of-original target exits and trailing stop on the final quarter.
- `papertrade/engine.py`: paper-only coordination and deterministic state transitions.
- `papertrade/store.py`: versioned atomic JSON persistence; malformed or unknown state fails closed rather than silently resetting.
- `tests/test_papertrade_core.py`: standard-library regression tests.

## Important limitations (not validated)

This is a core implementation slice, not a production-ready or strategy-validated system. No historical performance claim is made. The trailing distance currently derives from the initial per-unit risk amount as an ATR proxy; the engine/data adapter still needs to persist actual ATR for correct trailing semantics. Correlation is handled only through configured asset groups, not estimated from returns. Daily boundaries currently use the date prefix of the supplied timestamp; the live runner must normalize this to a configured exchange timezone. Marks and stale-data validation are not yet integrated. Backtesting, walk-forward/OOS validation, monitoring, Actions scheduling, and broad test coverage remain follow-up work.

Free market data may be delayed, incomplete, adjusted, or subject to rate limits. Paper results do not imply real-world execution or profitability. All prices and performance are simulations only.

## Run core tests

`python -m unittest discover -s tests -v`
