import unittest
import json
import tempfile
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
    reply_to_message_once,
    remove_registry_rows_for_message,
    remove_registry_rows_if_edited_message_lost_links,
    save_group_registry_exports,
    tweet_text_may_be_truncated,
)
from sync_deleted_messages import collect_registry_message_refs, deletion_sync_enabled, prune_deleted_message_ids


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

    def test_group_registry_exports_are_separated_and_keep_multiple_posts(self):
        registry = {
            "date": "2026-08-14",
            "entries": {
                "-1003891628675": {"same": {"promo_handle": "same", "chat_id": "-1003891628675"}},
                "-1003218974409": {"same": {"promo_handle": "same", "chat_id": "-1003218974409"}},
            },
            "post_entries": {
                "-1003891628675": {
                    "111": {
                        "promo_handle": "same",
                        "promo_post_id": "111",
                        "chat_id": "-1003891628675",
                        "tg_username": "tg_same",
                    },
                    "222": {
                        "promo_handle": "same",
                        "promo_post_id": "222",
                        "chat_id": "-1003891628675",
                        "tg_username": "tg_same",
                    },
                },
                "-1003218974409": {
                    "333": {
                        "promo_handle": "same",
                        "promo_post_id": "333",
                        "chat_id": "-1003218974409",
                        "tg_username": "tg_same",
                    },
                },
            },
        }
        with tempfile.TemporaryDirectory() as td:
            save_group_registry_exports(registry, base_dir=td)
            with open(f"{td}/群一/link_registry.json", encoding="utf-8") as f:
                group1 = json.load(f)
            with open(f"{td}/群二/link_registry.json", encoding="utf-8") as f:
                group2 = json.load(f)
            with open(f"{td}/群三/link_registry.json", encoding="utf-8") as f:
                group3 = json.load(f)

        self.assertEqual(set(group1["post_entries"]["-1003891628675"]), {"111", "222"})
        self.assertNotIn("-1003218974409", group1["post_entries"])
        self.assertEqual(set(group2["post_entries"]["-1003218974409"]), {"333"})
        self.assertNotIn("-1003891628675", group2["post_entries"])
        self.assertEqual(group3["entries"], {})
        self.assertEqual(group3["post_entries"], {})

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

    def test_reply_account_followers_can_satisfy_group_minimum(self):
        text = "https://x.com/low/status/111?s=46\nhttps://x.com/back/status/222?s=46"
        links = [
            {
                "role": "promo",
                "url": "https://x.com/low/status/111?s=46",
                "handle": "low",
                "post_id": "111",
                "followers_count": 19999,
                "followers_text": "1.9W",
                "tweet_text": "正文 @ToBulaer @ToBuerma",
                "required_mentions_count": 2,
            },
            {"role": "check", "url": "https://x.com/back/status/222?s=46", "handle": "back", "post_id": "222", "followers_count": 30000, "followers_text": "3W"},
        ]
        msg = {"message_id": 457, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}

        record_link(
            registry,
            "low",
            msg,
            text,
            "-1003218974409",
            "2026-08-09 00:01:02",
            check_handle="back",
            dual_link=True,
            promo_handle="low",
            links=links,
        )

        entry = registry["entries"]["-1003218974409"]["low"]
        self.assertFalse(promo_link_below_minimum(links, "-1003218974409"))
        self.assertTrue(entry["mutual_eligible"])
        self.assertEqual(entry["eligibility_text"], "✅合格")
        self.assertEqual(entry["check_followers_text"], "3W")
        self.assertEqual(entry["qualified_followers_count"], 30000)

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
        self.assertEqual(INVALID_MENTIONS_REPLY_TEXT, "该链接违规，未@社区账号，不予互推")
        self.assertEqual(count_required_mentions("x @ToBulaer @BulmaList"), 2)
        self.assertTrue(promo_link_missing_required_mentions(links))
        self.assertFalse(entry["mutual_eligible"])
        self.assertEqual(entry["eligibility_text"], "❌缺少指定@")

    def test_required_mentions_support_fullwidth_at_and_zero_width(self):
        self.assertEqual(count_required_mentions("＠ToBulaer @To\u200bBuerma"), 2)

    def test_truncated_tweet_text_does_not_trigger_missing_mentions_reply(self):
        links = [{
            "role": "promo",
            "tweet_text": "正文很长但是接口只返回前半段…",
            "required_mentions_count": 0,
        }]
        self.assertTrue(tweet_text_may_be_truncated(links[0]["tweet_text"]))
        self.assertFalse(promo_link_missing_required_mentions(links))
        self.assertIsNone(main.promo_link_content_eligible(links))

    def test_reply_to_message_once_marks_before_send_and_saves(self):
        group_state = {}
        calls = []
        saves = []
        with patch("main.reply_to_message", lambda *args: calls.append(args)):
            self.assertTrue(reply_to_message_once(group_state, "-1001", 456, "missing_mentions", "bad", save_callback=lambda: saves.append(True)))
            self.assertFalse(reply_to_message_once(group_state, "-1001", 456, "missing_mentions", "bad", save_callback=lambda: saves.append(True)))
        self.assertEqual(len(calls), 1)
        self.assertIn("456:missing_mentions", group_state.get("reply_keys"))
        self.assertGreaterEqual(len(saves), 1)

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
        record_link(
            registry,
            "new",
            msg,
            new_text,
            "-1003218974409",
            "2026-08-09 00:00:00",
            links=new_links,
            previous_entries=removed,
        )

        self.assertEqual(len(removed), 2)
        self.assertNotIn("old", registry["entries"]["-1003218974409"])
        self.assertNotIn("111", registry["post_entries"]["-1003218974409"])
        self.assertIn("new", registry["entries"]["-1003218974409"])
        self.assertIn("222", registry["post_entries"]["-1003218974409"])
        entry = registry["entries"]["-1003218974409"]["new"]
        self.assertTrue(entry["edited"])
        self.assertEqual(entry["message_id"], 456)
        self.assertEqual([item["label"] for item in entry["link_options"]], ["编辑后", "编辑前"])
        self.assertEqual([item["url"] for item in entry["link_options"]], [
            "https://x.com/new/status/222?s=46",
            "https://x.com/old/status/111?s=46",
        ])

    def test_edited_message_without_x_link_removes_old_registry_rows(self):
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}
        msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        old_text = "https://x.com/old/status/111?s=46"
        old_links = extract_x_links_ordered(old_text)
        record_link(registry, "old", msg, old_text, "-1003218974409", "2026-08-09 00:00:00", links=old_links)

        kept = remove_registry_rows_if_edited_message_lost_links(
            registry, "-1003218974409", 456, "https://x.com/new/status/222?s=46", True
        )
        self.assertEqual(kept, [])
        self.assertIn("old", registry["entries"]["-1003218974409"])

        removed = remove_registry_rows_if_edited_message_lost_links(
            registry, "-1003218974409", 456, "这条消息已经删掉链接", True
        )

        self.assertEqual(len(removed), 2)
        self.assertNotIn("old", registry["entries"]["-1003218974409"])
        self.assertNotIn("111", registry["post_entries"]["-1003218974409"])

    def test_non_edited_message_without_x_link_does_not_remove_registry_rows(self):
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}
        msg = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        old_text = "https://x.com/old/status/111?s=46"
        old_links = extract_x_links_ordered(old_text)
        record_link(registry, "old", msg, old_text, "-1003218974409", "2026-08-09 00:00:00", links=old_links)

        removed = remove_registry_rows_if_edited_message_lost_links(
            registry, "-1003218974409", 456, "普通无链接消息", False
        )

        self.assertEqual(removed, [])
        self.assertIn("old", registry["entries"]["-1003218974409"])
        self.assertIn("111", registry["post_entries"]["-1003218974409"])

    def test_deleted_message_sync_collects_and_prunes_by_chat_message_id(self):
        registry = {"date": "2026-08-09", "entries": {}, "post_entries": {}}
        msg1 = {"message_id": 456, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        msg2 = {"message_id": 789, "from": {"id": 123, "username": "tg_user", "first_name": "小王"}}
        links1 = extract_x_links_ordered("https://x.com/old/status/111?s=46")
        links2 = extract_x_links_ordered("https://x.com/old/status/222?s=46")
        record_link(registry, "old", msg1, links1[0]["url"], "-1003218974409", "2026-08-09 00:00:00", links=links1)
        record_link(registry, "old", msg2, links2[0]["url"], "-1003891628675", "2026-08-09 00:01:00", links=links2)

        refs = collect_registry_message_refs(registry)
        self.assertEqual(refs["-1003218974409"], [456])
        self.assertEqual(refs["-1003891628675"], [789])

        removed = prune_deleted_message_ids(registry, {"-1003218974409": [456]})

        self.assertEqual(len(removed), 2)
        self.assertNotIn("old", registry["entries"]["-1003218974409"])
        self.assertNotIn("111", registry["post_entries"]["-1003218974409"])
        self.assertIn("old", registry["entries"]["-1003891628675"])
        self.assertIn("222", registry["post_entries"]["-1003891628675"])

    def test_deleted_message_sync_requires_user_session_secrets(self):
        self.assertFalse(deletion_sync_enabled({
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "hash",
        }))
        self.assertTrue(deletion_sync_enabled({
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "hash",
            "TELEGRAM_STRING_SESSION": "session",
        }))


if __name__ == "__main__":
    unittest.main()
