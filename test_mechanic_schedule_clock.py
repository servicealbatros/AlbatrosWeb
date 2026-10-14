import unittest
from datetime import datetime, timezone
from mechanic_schedule_clock import due_day, next_send

class ClockTests(unittest.TestCase):
    def test_weekdays_only(self):
        for day in range(12, 17):
            self.assertIsNotNone(due_day(datetime(2026, 10, day, 8)))
        for day in (17, 18):
            self.assertIsNone(due_day(datetime(2026, 10, day, 8)))

    def test_summer_and_winter_use_romanian_eight_am(self):
        self.assertIsNotNone(due_day(datetime(2026, 10, 14, 5, tzinfo=timezone.utc)))
        self.assertIsNotNone(due_day(datetime(2026, 12, 14, 6, tzinfo=timezone.utc)))
        self.assertIsNone(due_day(datetime(2026, 10, 14, 4, 59, tzinfo=timezone.utc)))
        self.assertIsNone(due_day(datetime(2026, 10, 14, 5, 20, tzinfo=timezone.utc)))

    def test_friday_after_send_skips_weekend(self):
        self.assertEqual(next_send(datetime(2026, 10, 16, 9)).isoformat(), '2026-10-19T08:00:00+03:00')
