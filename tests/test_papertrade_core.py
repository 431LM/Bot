import unittest
import tempfile
from pathlib import Path
from papertrade.config import Config
from papertrade.execution import open_position, process_bar
from papertrade.models import Account, Bar, Side, Signal
from papertrade.risk import size_position, update_kill_switches
from papertrade.store import load_account, save_account
from papertrade.strategy import signal


def bar(i, close, high=None, low=None):
    return Bar("X", f"2024-01-{i:02d}T00:00:00+00:00", close, high or close*1.01, low or close*.99, close, 1000, True)


class CoreTests(unittest.TestCase):
    def test_strategy_requires_history_and_complete_bar(self):
        self.assertIsNone(signal([bar(i+1, 10+i*.1) for i in range(20)]))
        data = [bar(i+1, 10+i*.1) for i in range(60)]
        data[-1] = Bar("X", "2024-03-01T00:00:00+00:00", 17, 18, 16, 17, 1000, False)
        self.assertIsNone(signal(data))

    def test_position_size_risk_and_leverage_caps(self):
        cfg = Config()
        a = Account(100, 100, 100, "2024-01-01")
        s = Signal("X", Side.LONG, "2024-01-01T00:00:00Z", 10, 1)
        qty, err = size_position(a, s, cfg, {"X": 10})
        self.assertIsNone(err)
        self.assertLessEqual(qty * 2, 1.0000001)
        self.assertLessEqual(qty * 10, 500)

    def test_daily_halt_and_new_day_reset(self):
        a = Account(95, 100, 100, "2024-01-01")
        events = update_kill_switches(a, 95, "2024-01-01", Config())
        self.assertIn("DAILY_LOSS_LIMIT", events)
        update_kill_switches(a, 95, "2024-01-02", Config())
        self.assertFalse(a.daily_halt)

    def test_stop_precedes_target_when_ambiguous(self):
        cfg = Config()
        p, _ = open_position("X", Side.LONG, "t", 100, 1, 90, 10, "OTHER", cfg)
        fills = process_bar(p, Bar("X", "t2", 100, 106, 89, 101, 1000, True), cfg)
        self.assertEqual(fills[0]["reason"], "STOP")
        self.assertAlmostEqual(p.quantity, 0)

    def test_targets_once_and_trailing(self):
        cfg = Config()
        p, _ = open_position("X", Side.LONG, "t", 100, 4, 90, 80, "OTHER", cfg)
        fills = process_bar(p, Bar("X", "t2", 100, 111, 99, 110, 1000, True), cfg)
        self.assertEqual(len(fills), 3)
        self.assertAlmostEqual(p.quantity, 1)
        self.assertEqual(p.targets_hit, [0, 1, 2])
        self.assertTrue(p.trailing_active)
        self.assertEqual(process_bar(p, Bar("X", "t3", 110, 112, 109, 111, 1000, True), cfg), [])

    def test_state_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            a = Account(100, 100, 100, "2024-01-01")
            save_account(path, a)
            self.assertEqual(load_account(path).cash, 100)
            path.write_text("{")
            with self.assertRaises(RuntimeError):
                load_account(path)


if __name__ == "__main__":
    unittest.main()
