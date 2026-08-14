import unittest
from datetime import datetime, timezone
from types import ModuleType
import sys
from unittest.mock import patch

# 时区单元测试不会访问 Telegram/X，无需安装网络依赖。
sys.modules.setdefault("requests", ModuleType("requests"))
import main
from main import (
    INVALID_MENTIONS_REPLY_TEXT,
    LOW_FOLLOWER_REPLY_TEXT,
    beijing_day_of,
    beijing_full_time,
    backfill_registry_metadata,
    count_required_mentions,
    extract_x_handles_ordered,
    extract_x_links_ordered,
    fetch_x_author_meta,
    format_followers,
    limit_reply_enabled,
    load_chat_ids,
    min_followers_for_chat,
    parse_check_handle,
    promo_link_below_minimum,
    promo_link_missing_required_mentions,
    record_link,
    remove_registry_rows_for_message,
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
    def setUp(self):
        main._x_author_meta_cache.clear()
        main._tweet_author_cache.clear()

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
        self.assertEqual(min_followers_for_chat("-1003891628675"), 100000)
        self.assertEqual(min_followers_for_chat("-1003218974409"), 20000)
        self.assertEqual(min_followers_for_chat("-1003739822194"), 0)

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

    def test_author_name_and_followers_are_recorded_from_x_link(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "tweet": {
                        "author": {
                            "screen_name": "promoA",
                            "name": "小王",
                            "followers": 40125,
                        }
                    },
                    "text": "hello @ToBulaer @ToBuerma",
                }

        with patch.object(main.requests, "get", return_value=FakeResp(), create=True):
            text = "https://x.com/promoA/status/111?s=46"
            links = extract_x_links_ordered(text)
            msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
            registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}
            record_link(registry, "promoa", msg, text, "-1001", "2026-08-09 00:01:02", links=links)

        entry = registry["entries"]["-1001"]["promoa"]
        self.assertEqual(entry["x_name"], "小王")
        self.assertEqual(entry["followers_count"], 40125)
        self.assertEqual(entry["followers_text"], "4W")
        self.assertEqual(entry["eligibility_text"], "✅合格")
        self.assertEqual(entry["required_mentions_count"], 2)
        self.assertEqual(format_followers(9999), "9.9K")
        self.assertEqual(format_followers(3299), "3.2K")
        self.assertEqual(format_followers(12000), "1.2W")
        self.assertEqual(format_followers(40125), "4W")
        self.assertEqual(format_followers(45999), "4.5W")
        self.assertEqual(format_followers(100999), "10W")
        self.assertEqual(fetch_x_author_meta("https://x.com/promoA/status/111", "promoa", "111")["name"], "小王")

    def test_backfill_registry_metadata_updates_old_entries(self):
        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "tweet": {
                        "author": {
                            "screen_name": "oldA",
                            "name": "老王",
                            "followers": 12000,
                        }
                    },
                    "text": "hello @ToBulaer @ToBuerma",
                }

        registry = {
            "date": "2026-08-09",
            "entries": {
                "-1001": {
                    "olda": {
                        "x_handle": "olda",
                        "promo_handle": "olda",
                        "promo_url": "https://x.com/oldA/status/111?s=46",
                        "promo_post_id": "111",
                    }
                }
            },
            "post_entries": {},
        }
        with patch.object(main.requests, "get", return_value=FakeResp(), create=True):
            changed = backfill_registry_metadata(registry)

        entry = registry["entries"]["-1001"]["olda"]
        self.assertEqual(changed, 1)
        self.assertEqual(entry["x_name"], "老王")
        self.assertEqual(entry["followers_text"], "1.2W")
        self.assertEqual(entry["eligibility_text"], "❌粉丝不足")

    def test_low_followers_are_marked_ineligible_by_group_minimum(self):
        text = "https://x.com/low/status/111?s=46"
        links = [{"role": "promo", "url": text, "handle": "low", "post_id": "111", "followers_count": 19999, "followers_text": "1.9W"}]
        msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}

        record_link(registry, "low", msg, text, "-1003218974409", "2026-08-09 00:01:02", links=links)

        entry = registry["entries"]["-1003218974409"]["low"]
        self.assertEqual(LOW_FOLLOWER_REPLY_TEXT, "粉丝数量低于最低互推标准，该链接不予互推！")
        self.assertFalse(entry["mutual_eligible"])
        self.assertEqual(entry["followers_min"], 20000)
        self.assertEqual(entry["eligibility_text"], "❌粉丝不足")
        self.assertTrue(promo_link_below_minimum(links, "-1003218974409"))
        self.assertFalse(promo_link_below_minimum(links, "-1003739822194"))

    def test_missing_required_mentions_are_marked_ineligible(self):
        text = "https://x.com/bad/status/111?s=46"
        links = [{
            "role": "promo",
            "url": text,
            "handle": "bad",
            "post_id": "111",
            "followers_count": 30000,
            "followers_text": "3W",
            "tweet_text": "hello @ToBulaer",
            "required_mentions_count": 1,
        }]
        msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}

        record_link(registry, "bad", msg, text, "-1003218974409", "2026-08-09 00:01:02", links=links)

        entry = registry["entries"]["-1003218974409"]["bad"]
        self.assertEqual(INVALID_MENTIONS_REPLY_TEXT, "推文内容未包含至少2个指定互推账号，该链接不予互推！")
        self.assertEqual(count_required_mentions("x @ToBulaer @BulmaList"), 2)
        self.assertTrue(promo_link_missing_required_mentions(links))
        self.assertFalse(entry["mutual_eligible"])
        self.assertEqual(entry["eligibility_text"], "❌缺少指定@")

    def test_edited_message_replaces_old_registry_rows(self):
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}
        msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        old_text = "https://x.com/old/status/111?s=46"
        old_links = extract_x_links_ordered(old_text)
        record_link(registry, "old", msg, old_text, "-1003218974409", "2026-08-09 00:00:00", links=old_links)

        msg["edit_date"] = 1786204801
        removed = remove_registry_rows_for_message(registry, "-1003218974409", 456)
        new_text = "https://x.com/new/status/222?s=46"
        new_links = extract_x_links_ordered(new_text)
        record_link(registry, "new", msg, new_text, "-1003218974409", "2026-08-09 00:00:00", links=new_links)

        self.assertEqual(removed, 2)
        self.assertNotIn("old", registry["entries"]["-1003218974409"])
        self.assertNotIn("111", registry["post_entries"]["-1003218974409"])
        self.assertIn("new", registry["entries"]["-1003218974409"])
        self.assertIn("222", registry["post_entries"]["-1003218974409"])
        self.assertTrue(registry["entries"]["-1003218974409"]["new"]["edited"])
        self.assertEqual(registry["entries"]["-1003218974409"]["new"]["message_id"], 456)


if __name__ == "__main__":
    unittest.main()
