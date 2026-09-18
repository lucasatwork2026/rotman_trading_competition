import unittest

from strategy import (
    Settings,
    black_scholes,
    news_signature,
    remaining_years,
    risk_ok,
    volatility_from_news,
)


class StrategyTests(unittest.TestCase):
    def test_put_call_parity_at_zero_rate(self):
        call, _ = black_scholes(50, 49, 1 / 12, 0.0, 0.2, "C")
        put, _ = black_scholes(50, 49, 1 / 12, 0.0, 0.2, "P")
        self.assertAlmostEqual(call - put, 1.0, places=8)

    def test_option_deltas_differ_by_one(self):
        _, call_delta = black_scholes(50, 50, 1 / 12, 0.0, 0.2, "C")
        _, put_delta = black_scholes(50, 50, 1 / 12, 0.0, 0.2, "P")
        self.assertAlmostEqual(call_delta - put_delta, 1.0, places=8)

    def test_news_exact_volatility(self):
        news = [{"news_id": 1, "body": "Realized volatility for this week will be 29%."}]
        self.assertEqual(volatility_from_news(news), 0.29)

    def test_news_range_midpoint(self):
        news = [{"news_id": 1, "body": "Volatility for next week will be between 27-30%."}]
        self.assertAlmostEqual(volatility_from_news(news), 0.285)

    def test_newer_range_replaces_older_exact_forecast(self):
        news = [
            {"news_id": 1, "body": "Realized volatility for this week will be 20%."},
            {"news_id": 2, "body": "Volatility for next week will be between 27-30%."},
        ]
        self.assertAlmostEqual(volatility_from_news(news), 0.285)

    def test_news_signature_changes_only_with_news(self):
        first = [{"news_id": 1, "headline": "Weekly volatility", "body": "20%"}]
        same = list(reversed(first))
        second = first + [{"news_id": 2, "headline": "Update", "body": "27-30%"}]
        self.assertEqual(news_signature(first), news_signature(same))
        self.assertNotEqual(news_signature(first), news_signature(second))

    def test_remaining_case_month(self):
        self.assertAlmostEqual(remaining_years(0, 600), 1 / 12)
        self.assertEqual(remaining_years(600, 600), 0)

    def test_option_limit_buffer(self):
        settings = Settings()
        positions = {"RTM48C": 900}
        self.assertFalse(risk_ok(positions, "RTM48C", 1, settings))


if __name__ == "__main__":
    unittest.main()
