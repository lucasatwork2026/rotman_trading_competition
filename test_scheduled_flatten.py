import math
import statistics
import unittest

from strategy_scheduled_flatten import (
    flatten_tick_due,
    realized_volatility_from_prices,
)


class ScheduledFlattenTests(unittest.TestCase):
    def test_exact_flatten_ticks(self):
        self.assertEqual(flatten_tick_due(73, set()), (73, 74))
        self.assertEqual(flatten_tick_due(148, {73}), (148, 149))
        self.assertEqual(flatten_tick_due(223, {73, 148}), (223, 224))

    def test_one_tick_catch_up(self):
        self.assertEqual(flatten_tick_due(74, set()), (73, 74))

    def test_completed_or_late_ticks_do_not_repeat(self):
        self.assertIsNone(flatten_tick_due(74, {73}))
        self.assertIsNone(flatten_tick_due(75, set()))

    def test_realized_volatility_uses_market_returns(self):
        prices = [100.0, 101.0, 99.99]
        sigma = realized_volatility_from_prices(prices, total_ticks=600, window=30)
        self.assertGreater(sigma, 0)

    def test_realized_volatility_annualization(self):
        # Log returns are +1% and -1%; annualize from 600 ticks per case month.
        prices = [100.0, 100.0 * math.exp(0.01), 100.0]
        expected = statistics.stdev([0.01, -0.01]) * math.sqrt(600 * 12)
        actual = realized_volatility_from_prices(prices, 600)
        self.assertAlmostEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
