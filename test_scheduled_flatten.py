import math
import unittest

from strategy_scheduled_flatten import (
    flatten_tick_due,
    required_volatility_from_news,
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

    def test_generic_tick_zero_volatility_news(self):
        news = [
            {
                "news_id": 1,
                "headline": "Analyst update",
                "body": "The annualized volatility of RTM is 24%.",
            }
        ]
        self.assertEqual(required_volatility_from_news(news), 0.24)

    def test_no_news_never_uses_default(self):
        self.assertTrue(math.isnan(required_volatility_from_news([])))


if __name__ == "__main__":
    unittest.main()
