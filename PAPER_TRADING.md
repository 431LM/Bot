# Validation and data policy (increment 2)

`papertrade.data.validate_bars` rejects invalid OHLCV rows, naive timestamps, duplicates, out-of-order bars, large close-to-close jumps, insufficient liquidity, and stale/future data when a `now` timestamp is supplied. Incomplete bars are omitted. It never fills or fabricates market values. A caller must treat any issue as degraded and refrain from new entries; the present backtest returns ERROR if validated series are empty or data issues are found in reporting.

`papertrade.backtest` adds a chronological baseline simulator scaffold. Signals are calculated from prior complete bars and scheduled at the following bar open. Output is always `NOT VALIDATED`; the current metrics are explicitly limited and must not be used as evidence of edge. The multi-asset loop uses ordinal bar alignment, not exchange-calendar synchronization, and realized-only drawdown is not the true intratrade portfolio drawdown. It is an intermediate harness, not a completed research-grade backtester.

Time splitting and expanding-window split helpers are included. No market history was downloaded and no backtest performance or strategy-validation result was generated in this change.

Run checks locally with `python -m unittest discover -s tests -v`. This environment provides repository file tools only, so execution is not claimed.
