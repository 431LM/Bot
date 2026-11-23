import unittest
from datetime import datetime, timezone
from papertrade.config import Config
from papertrade.data import validate_bars, healthy_for_trading
from papertrade.models import Bar
from papertrade.backtest import time_split, walk_forward_splits


def b(i, **kw):
    base = dict(ticker="X", timestamp=f"2024-01-{i:02d}T00:00:00+00:00", open=10, high=11, low=9, close=10, volume=100, complete=True)
    base.update(kw)
    return Bar(**base)


class DataValidationTests(unittest.TestCase):
    def test_bad_ohlc_rejected(self):
        valid, issues = validate_bars([b(1, high=8)])
        self.assertEqual(valid, [])
        self.assertTrue(any(x.startswith("INVALID_OHLC") for x in issues))

    def test_duplicates_and_naive_time_rejected(self):
        valid, issues = validate_bars([b(1), b(1), b(2, timestamp="2024-01-02T00:00:00")])
        self.assertEqual(len(valid), 1)
        self.assertTrue(any(x.startswith("DUPLICATE") for x in issues))
        self.assertTrue(any(x.startswith("NAIVE") for x in issues))

    def test_incomplete_bar_does_not_enter_output(self):
        valid, issues = validate_bars([b(1), b(2, complete=False)])
        self.assertEqual(len(valid), 1)
        self.assertEqual(issues, [])

    def test_stale_data_fails_closed(self):
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        _, issues = validate_bars([b(1)], now=now)
        self.assertTrue(any(x.startswith("STALE_DATA") for x in issues))
        self.assertFalse(healthy_for_trading(issues))

    def test_price_jump_rejected(self):
        _, issues = validate_bars([b(1), b(2, open=20, high=21, low=19, close=20)])
        self.assertTrue(any(x.startswith("PRICE_JUMP") for x in issues))

    def test_time_splits_do_not_overlap(self):
        data = [b(i+1) for i in range(20)]
        train, val, test = time_split(data)
        self.assertEqual(len(train), 12)
        self.assertEqual(len(val), 4)
        self.assertEqual(len(test), 4)
        self.assertLess(train[-1].timestamp, val[0].timestamp)
        folds = list(walk_forward_splits(data, 10, 4))
        self.assertTrue(folds)
        for tr, te in folds:
            self.assertLess(tr[-1].timestamp, te[0].timestamp)


if __name__ == "__main__":
    unittest.main()
