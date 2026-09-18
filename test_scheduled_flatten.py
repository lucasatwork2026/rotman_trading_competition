import unittest

from strategy_scheduled_flatten import flatten_tick_due


class ScheduledFlattenTests(unittest.TestCase):
    def test_exact_flatten_ticks(self):
        self.assertEqual(flatten_tick_due(74, set()), (74, 75))
        self.assertEqual(flatten_tick_due(149, {74}), (149, 150))
        self.assertEqual(flatten_tick_due(224, {74, 149}), (224, 225))

    def test_one_tick_catch_up(self):
        self.assertEqual(flatten_tick_due(75, set()), (74, 75))

    def test_completed_or_late_ticks_do_not_repeat(self):
        self.assertIsNone(flatten_tick_due(75, {74}))
        self.assertIsNone(flatten_tick_due(76, set()))


if __name__ == "__main__":
    unittest.main()
