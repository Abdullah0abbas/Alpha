import unittest
from datetime import datetime, timezone, timedelta
from bot.utils.formatters import to_local, format_local_datetime, format_local_time


class TestTimeFormatters(unittest.TestCase):
    def test_to_local_default_offset(self):
        # 12:00 UTC -> 15:00 (+3 Damascus)
        dt_utc = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
        dt_local = to_local(dt_utc)
        self.assertEqual(dt_local.hour, 15)
        self.assertEqual(dt_local.day, 24)
        self.assertEqual(dt_local.utcoffset(), timedelta(hours=3))

    def test_to_local_naive_assumed_utc(self):
        # Naive datetime treated as UTC
        dt_naive = datetime(2026, 10, 24, 10, 30, 0)
        dt_local = to_local(dt_naive)
        self.assertEqual(dt_local.hour, 13)
        self.assertEqual(dt_local.minute, 30)

    def test_to_local_midnight_crossover(self):
        # 22:45 UTC on the 24th -> 01:45 on the 25th in Damascus (+3)
        dt_crossover = datetime(2026, 10, 24, 22, 45, 0, tzinfo=timezone.utc)
        dt_local = to_local(dt_crossover)
        self.assertEqual(dt_local.day, 25)
        self.assertEqual(dt_local.hour, 1)
        self.assertEqual(dt_local.minute, 45)

    def test_to_local_custom_offset(self):
        # Custom offset +5
        dt_utc = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
        dt_local = to_local(dt_utc, offset_hours=5)
        self.assertEqual(dt_local.hour, 17)

    def test_format_local_datetime_and_suffix(self):
        dt_utc = datetime(2026, 10, 24, 14, 5, 0, tzinfo=timezone.utc)
        formatted_with_suffix = format_local_datetime(dt_utc, include_suffix=True)
        self.assertIn("17:05", formatted_with_suffix)
        self.assertIn("2026-10-24", formatted_with_suffix)
        self.assertIn("(بتوقيت دمشق)", formatted_with_suffix)

        formatted_no_suffix = format_local_datetime(dt_utc, include_suffix=False)
        self.assertIn("17:05", formatted_no_suffix)
        self.assertNotIn("بتوقيت دمشق", formatted_no_suffix)

    def test_format_local_time(self):
        dt_utc = datetime(2026, 10, 24, 8, 9, 0, tzinfo=timezone.utc)
        formatted_time = format_local_time(dt_utc)
        self.assertEqual(formatted_time, "11:09")

    def test_none_handling(self):
        self.assertIsNone(to_local(None))
        self.assertEqual(format_local_datetime(None), "غير محدد")
        self.assertEqual(format_local_time(None), "غير محدد")


if __name__ == "__main__":
    unittest.main()
