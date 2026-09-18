import unittest

from strategy_scheduled_flatten import flatten_tick_due


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


if __name__ == "__main__":
    unittest.main()
