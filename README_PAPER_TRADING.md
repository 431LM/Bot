# Existing bot and paper system

The existing repository contains several independent market-observation components: `radar.py`, `muster.py`, `scanner.py`, `scanner_live.py`, `tick.py`, and `catalyst.py`. Existing GitHub Actions run these components and persist their logs/state. They use Yahoo Finance and ntfy; these systems generate observations and notifications, not broker orders.

The new `papertrade/` package is deliberately isolated from those workflows and has no broker or real-order API. Its first slice separates strategy, risk, paper execution, state, and orchestration. Future work should connect a market-data adapter only after OHLC validation, then build a shared event-driven backtest and paper runner using the same tested transition logic.

Current validation status: **NOT VALIDATED**. Tests were authored but have not been executed in this environment. No profitability claim is made.
