import unittest
from datetime import datetime, timezone
from types import ModuleType
import sys
from unittest.mock import patch

# 时区单元测试不会访问 Telegram/X，无需安装网络依赖。
sys.modules.setdefault("requests", ModuleType("requests"))
from main import (
    beijing_day_of,
    beijing_full_time,
    extract_x_handles_ordered,
    extract_x_links_ordered,
    limit_reply_enabled,
    load_chat_ids,
    parse_check_handle,
    record_link,
)


class BeijingTimeTest(unittest.TestCase):
    def test_second_before_beijing_midnight_belongs_to_previous_day(self):
        timestamp = datetime(2026, 8, 5, 15, 59, 59, tzinfo=timezone.utc).timestamp()

        self.assertEqual(beijing_day_of(timestamp), "2026-08-05")
        self.assertEqual(beijing_full_time(timestamp), "2026-08-05 23:59:59")

    def test_beijing_midnight_starts_new_day(self):
        timestamp = datetime(2026, 8, 5, 16, 0, 0, tzinfo=timezone.utc).timestamp()

        self.assertEqual(beijing_day_of(timestamp), "2026-08-06")
        self.assertEqual(beijing_full_time(timestamp), "2026-08-06 00:00:00")


class LinkParsingTest(unittest.TestCase):
    def test_group3_chat_id_is_listened_by_default(self):
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_CHAT_ID": "-1003218974409",
                "TELEGRAM_CHAT_ID_GROUP1": "",
                "TELEGRAM_CHAT_ID_GROUP3": "",
            },
            clear=True,
        ):
            ids = set(load_chat_ids())
        self.assertIn("-1003739822194", ids)
        self.assertIn("-3739822194", ids)
        self.assertIn("-1003891628675", ids)
        self.assertIn("-1003218974409", ids)

    def test_group1_chat_id_is_always_listened_even_when_env_is_set(self):
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_CHAT_ID": "-1003218974409",
                "TELEGRAM_CHAT_ID_GROUP1": "-1001111111111",
                "TELEGRAM_CHAT_ID_GROUP3": "",
            },
            clear=True,
        ):
            ids = set(load_chat_ids())
        self.assertIn("-1003891628675", ids)
        self.assertIn("-3891628675", ids)
        self.assertIn("-1001111111111", ids)

    def test_group1_limit_reply_is_disabled(self):
        self.assertFalse(limit_reply_enabled("-1003891628675"))
        self.assertFalse(limit_reply_enabled("-3891628675"))
        self.assertTrue(limit_reply_enabled("-1003218974409"))

    def test_three_x_link_formats_are_parsed(self):
        text = "\n".join([
            "[https://x.com/tianqipaidui/status/2034586318882414816?s=46&t=X8zwROumk3_na_EtR83SQw](https://x.com/tianqipaidui/status/2034586318882414816?s=46&t=X8zwROumk3_na_EtR83SQw)",
            "[https://x.com/yzlzp77/status/2085158793932071149?s=46](https://x.com/yzlzp77/status/2085158793932071149?s=46)",
            "[https://x.com/i/status/2085928788068831535](https://x.com/i/status/2085928788068831535)",
        ])
        links = extract_x_links_ordered(text)
        self.assertEqual([item["post_id"] for item in links], [
            "2034586318882414816",
            "2085158793932071149",
            "2085928788068831535",
        ])
        self.assertEqual([item["handle"] for item in links[:2]], ["tianqipaidui", "yzlzp77"])

    def test_second_link_is_check_handle(self):
        text = "https://x.com/promoA/status/111?s=46\nhttps://x.com/checkB/status/222?s=46"
        self.assertEqual(extract_x_handles_ordered(text), ["promoa", "checkb"])
        self.assertEqual(parse_check_handle(text), ("checkb", True, "promoa"))

    def test_record_link_keeps_message_text_and_post_ids(self):
        text = "https://x.com/promoA/status/111?s=46\nhttps://x.com/checkB/status/222?s=46"
        links = extract_x_links_ordered(text)
        msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}
        record_link(
            registry, "promoa", msg, text, "-1001", "2026-08-09 00:01:02",
            check_handle="checkb", dual_link=True, promo_handle="promoa", links=links,
        )
        entry = registry["entries"]["-1001"]["promoa"]
        self.assertEqual(entry["tg_username"], "tg_user")
        self.assertEqual(entry["message_id"], 456)
        self.assertEqual(entry["message_text"], text)
        self.assertEqual(entry["promo_post_id"], "111")
        self.assertEqual(entry["check_post_id"], "222")
        self.assertIn("111", registry["post_entries"]["-1001"])


if __name__ == "__main__":
    unittest.main()
