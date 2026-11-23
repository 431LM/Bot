import unittest

from papertrade.compat_validation import validate_ohlc_sequence
from papertrade.models import Bar


class CompatibilityTests(unittest.TestCase):
    def test_complete_valid_bars(self):
        bars = [Bar("TEST", "2024-01-01T00:00:00Z", 10, 11, 9, 10, 100)]
        self.assertEqual(validate_ohlc_sequence(bars), [])

    def test_incomplete_and_invalid_bars(self):
        bars = [Bar("TEST", "2024-01-01T00:00:00Z", 10, 11, 9, 10, 100, False),
                Bar("TEST", "2024-01-01T00:00:00Z", 0, 1, -1, 0, 100)]
        p = validate_ohlc_sequence(bars)
        self.assertIn("INCOMPLETE_BAR:2024-01-01T00:00:00Z", p)
        self.assertTrue(any("INVALID_OHLC" in x for x in p))


if __name__ == "__main__":
    unittest.main()
