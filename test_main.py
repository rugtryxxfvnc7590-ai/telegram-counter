import unittest
from datetime import datetime, timezone
from types import ModuleType
import sys

# 时区单元测试不会访问 Telegram/X，无需安装网络依赖。
sys.modules.setdefault("requests", ModuleType("requests"))
from main import beijing_day_of, beijing_full_time


class BeijingTimeTest(unittest.TestCase):
    def test_second_before_beijing_midnight_belongs_to_previous_day(self):
        timestamp = datetime(2026, 8, 5, 15, 59, 59, tzinfo=timezone.utc).timestamp()

        self.assertEqual(beijing_day_of(timestamp), "2026-08-05")
        self.assertEqual(beijing_full_time(timestamp), "2026-08-05 23:59:59")

    def test_beijing_midnight_starts_new_day(self):
        timestamp = datetime(2026, 8, 5, 16, 0, 0, tzinfo=timezone.utc).timestamp()

        self.assertEqual(beijing_day_of(timestamp), "2026-08-06")
        self.assertEqual(beijing_full_time(timestamp), "2026-08-06 00:00:00")


if __name__ == "__main__":
    unittest.main()
