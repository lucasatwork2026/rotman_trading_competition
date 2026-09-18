import csv
import tempfile
import unittest
from pathlib import Path

from round_recorder import RoundRecorder


class RoundRecorderTests(unittest.TestCase):
    def test_records_metrics_trades_and_summary(self):
        securities = [
            {
                "ticker": "RTM",
                "position": 100,
                "bid": 49.99,
                "ask": 50.01,
                "last": 50.0,
                "vwap": 50.0,
                "realized": 10,
                "unrealized": -2,
            },
            {
                "ticker": "RTM50C",
                "position": 5,
                "bid": 1.0,
                "ask": 1.02,
                "last": 1.01,
                "vwap": 1.0,
                "realized": 4,
                "unrealized": 3,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            recorder = RoundRecorder(directory)
            recorder.record_snapshot(
                1, 1, "active", 50.0, 0.2, 100.0, securities, {"RTM50C"}
            )
            recorder.record_trade(1, 1, "RTM50C", 5, "option_target")
            recorder.finish_round("test")

            with (Path(directory) / "round_summaries.csv").open(
                encoding="utf-8"
            ) as handle:
                summaries = list(csv.DictReader(handle))
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["ending_pnl"], "15.0000")
            self.assertEqual(summaries[0]["option_contract_volume"], "5")
            self.assertEqual(summaries[0]["orders"], "1")


if __name__ == "__main__":
    unittest.main()
