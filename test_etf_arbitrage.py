import unittest

from strategy_etf_arbitrage import (
    RiskCaps,
    Settings,
    arbitrage_edges,
    book_capacity,
    book_vwap,
    maximum_safe_quantity,
    project_positions,
    tender_edges,
    weighted_position_risk,
)


class ETFArbitrageTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            max_arb_clip=1_000,
            max_inventory_units=10_000,
            position_limit_buffer=0.90,
        )

    def test_book_vwap_uses_remaining_quantity(self):
        book = {
            "asks": [
                {"price": 10.00, "quantity": 100, "quantity_filled": 40},
                {"price": 10.10, "quantity": 100, "quantity_filled": 0},
            ],
            "bids": [],
        }
        self.assertEqual(book_capacity(book, "BUY"), 160)
        self.assertAlmostEqual(book_vwap(book, "BUY", 100), (60 * 10 + 40 * 10.1) / 100)
        self.assertIsNone(book_vwap(book, "BUY", 161))

    def test_weighted_etf_limit(self):
        gross, net = weighted_position_risk({"BULL": 100, "BEAR": 100, "RITC": -100})
        self.assertEqual(gross, 400)
        self.assertEqual(net, 0)

    def test_projected_arbitrage_is_net_neutral(self):
        projected = project_positions({}, "RICH", 500)
        self.assertEqual(projected, {"RITC": -500, "BULL": 500, "BEAR": 500})
        self.assertEqual(weighted_position_risk(projected), (2_000, 0))

    def test_market_fees_reduce_edge(self):
        gross, net = arbitrage_edges(25.20, 10.00, 15.00, 1.0, 0.02)
        self.assertAlmostEqual(gross, 0.20)
        self.assertAlmostEqual(net, 0.14)

    def test_quantity_respects_buffered_gross_limit(self):
        caps = RiskCaps(gross_limit=10_000, net_limit=5_000, gross_now=0, net_now=0)
        quantity = maximum_safe_quantity({}, "CHEAP", 10_000, caps, self.settings)
        # One complete arbitrage unit consumes four weighted gross units.
        self.assertEqual(quantity, 2_250)

    def test_tender_buy_action_means_long_etf(self):
        securities = {
            "USD": {"ticker": "USD", "bid": 0.999, "ask": 1.001},
            "BULL": {"ticker": "BULL", "bid": 10.00, "ask": 10.02},
            "BEAR": {"ticker": "BEAR", "bid": 15.00, "ask": 15.02},
        }
        book = {
            "bids": [{"price": 25.00, "quantity": 1_000, "quantity_filled": 0}],
            "asks": [{"price": 25.02, "quantity": 1_000, "quantity_filled": 0}],
        }
        immediate, hedged = tender_edges(
            {"action": "BUY", "quantity": 100, "price": 24.80},
            securities,
            book,
            self.settings,
        )
        self.assertIsNotNone(immediate)
        self.assertGreater(immediate, 0.10)
        self.assertGreater(hedged, 0.10)


if __name__ == "__main__":
    unittest.main()
