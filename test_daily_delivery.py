import unittest
from datetime import datetime
from types import ModuleType
from unittest.mock import patch
import sys

sys.modules.setdefault("requests", ModuleType("requests"))
import main
import sync_group_messages
from main import BEIJING


def _entry(post_id, message_id, when, *, eligible=True, after_cutoff=False, promo_post_id=None):
    return {
        "promo_handle": f"user{post_id}",
        "promo_post_id": str(promo_post_id or post_id),
        "promo_url": f"https://x.com/user{post_id}/status/{post_id}?s=46",
        "message_id": message_id,
        "time": when,
        "mutual_eligible": eligible,
        "after_cutoff": after_cutoff,
    }


class DailyListTest(unittest.TestCase):
    def test_links_are_eligible_pre_cutoff_promo_posts_in_reverse_message_order(self):
        registry = {
            "date": "2026-08-27",
            "post_entries": {
                main.GROUP_1_CHAT_ID_FALLBACK: {
                    "222": _entry("222", 2, "2026-08-27 00:02:00"),
                    "111": _entry("111", 1, "2026-08-27 00:01:00"),
                    "333": _entry("333", 3, "2026-08-27 00:03:00", eligible=False),
                    "444": _entry("444", 4, "2026-08-27 19:00:00", after_cutoff=True),
                    "555": _entry("555", 5, "2026-08-27 00:05:00", promo_post_id="999"),
                }
            },
        }

        self.assertEqual(
            main.daily_eligible_links(registry, main.GROUP_1_CHAT_ID_FALLBACK),
            [
                "https://x.com/user222/status/222",
                "https://x.com/user111/status/111",
            ],
        )

    def test_message_has_number_space_link_and_one_group_header(self):
        text = main.format_daily_list_message(
            "群二",
            "2026-08-27",
            ["https://x.com/a/status/1", "https://x.com/b/status/2"],
        )
        self.assertEqual(
            text,
            "群二（8月27日）互推名单，共 2 条\n\n"
            "1 https://x.com/a/status/1\n\n"
            "2 https://x.com/b/status/2",
        )

    def test_send_is_private_per_group_and_is_not_duplicated(self):
        state = {
            "group_snapshot_sync": {
                "date": "2026-08-27",
                "completed_groups": ["群一", "群二", "群三"],
            }
        }
        registry = {"date": "2026-08-27", "post_entries": {}}
        calls = []
        now = datetime(2026, 8, 27, 19, 0, tzinfo=BEIJING)

        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}, clear=False), patch(
            "main._send_private_message",
            side_effect=lambda chat_id, text: calls.append((chat_id, text)) or (True, ""),
        ):
            first = main.send_daily_lists_to_owner(registry, state, now=now)
            second = main.send_daily_lists_to_owner(registry, state, now=now)

        self.assertEqual(first, {"群一": "sent", "群二": "sent", "群三": "sent"})
        self.assertEqual(second, {"群一": "already_sent", "群二": "already_sent", "群三": "already_sent"})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(chat_id == "8614747348" for chat_id, _ in calls))

    def test_failed_group_is_the_only_group_retried(self):
        state = {
            "group_snapshot_sync": {
                "date": "2026-08-27",
                "completed_groups": ["群一", "群二", "群三"],
            }
        }
        registry = {"date": "2026-08-27", "post_entries": {}}
        now = datetime(2026, 8, 27, 19, 0, tzinfo=BEIJING)
        first_calls = []

        def first_send(chat_id, text):
            first_calls.append(text)
            return (False, "temporary") if text.startswith("群二") else (True, "")

        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}, clear=False), patch(
            "main._send_private_message", side_effect=first_send
        ):
            first = main.send_daily_lists_to_owner(registry, state, now=now)

        retry_calls = []
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}, clear=False), patch(
            "main._send_private_message",
            side_effect=lambda chat_id, text: retry_calls.append(text) or (True, ""),
        ):
            second = main.send_daily_lists_to_owner(registry, state, now=now)

        self.assertEqual(first["群二"], "failed")
        self.assertEqual(second["群一"], "already_sent")
        self.assertEqual(second["群二"], "sent")
        self.assertEqual(second["群三"], "already_sent")
        self.assertEqual(len(retry_calls), 1)
        self.assertTrue(retry_calls[0].startswith("群二"))

    def test_group_waits_for_current_day_snapshot(self):
        state = {
            "group_snapshot_sync": {
                "date": "2026-08-27",
                "completed_groups": ["群一", "群三"],
            }
        }
        registry = {"date": "2026-08-27", "post_entries": {}}
        now = datetime(2026, 8, 27, 19, 0, tzinfo=BEIJING)
        calls = []
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}, clear=False), patch(
            "main._send_private_message",
            side_effect=lambda chat_id, text: calls.append(text) or (True, ""),
        ):
            result = main.send_daily_lists_to_owner(registry, state, now=now)

        self.assertEqual(result["群二"], "waiting_for_snapshot")
        self.assertEqual(len(calls), 2)


class GroupSnapshotTest(unittest.TestCase):
    def test_unchanged_message_reuses_snapshot_without_x_lookup(self):
        chat_id = main.GROUP_1_CHAT_ID_FALLBACK
        old_entry = {
            "x_handle": "kept",
            "promo_handle": "kept",
            "promo_post_id": "111",
            "promo_url": "https://x.com/kept/status/111",
            "message_id": 7,
            "message_text": "https://x.com/kept/status/111",
            "time": "2026-08-27 18:30:00",
            "mutual_eligible": True,
            "after_cutoff": False,
        }
        registry = {
            "date": "2026-08-27",
            "entries": {chat_id: {"kept": dict(old_entry)}},
            "post_entries": {chat_id: {"111": dict(old_entry)}},
        }
        state = {"date": "2026-08-27", "groups": {}}
        messages = [{
            "message_id": 7,
            "date": datetime(2026, 8, 27, 18, 30, tzinfo=BEIJING).timestamp(),
            "text": "https://x.com/kept/status/111",
            "from": {"id": 9, "username": "telegram_user", "first_name": "小王"},
        }]

        with patch("sync_group_messages.extract_x_links_ordered", side_effect=AssertionError("不应重新请求 X")):
            count = sync_group_messages.replace_group_snapshot(registry, state, chat_id, messages, "2026-08-27")

        self.assertEqual(count, 1)
        self.assertEqual(registry["entries"][chat_id]["kept"]["promo_post_id"], "111")
        self.assertIn("111", registry["post_entries"][chat_id])

    def test_snapshot_replaces_stale_rows_and_counts_only_x_messages(self):
        chat_id = main.GROUP_2_CHAT_ID_FALLBACK
        registry = {
            "date": "2026-08-27",
            "entries": {chat_id: {"old": {"message_id": 1}}},
            "post_entries": {chat_id: {"1": {"message_id": 1}}},
        }
        state = {"date": "2026-08-27", "groups": {chat_id: {"count": 9, "reply_keys": ["1:x"]}}}
        messages = [
            {
                "message_id": 2,
                "date": datetime(2026, 8, 27, 18, 0, tzinfo=BEIJING).timestamp(),
                "text": "https://x.com/newuser/status/222",
                "from": {"id": 9, "username": "telegram_user", "first_name": "小王"},
            },
            {
                "message_id": 3,
                "date": datetime(2026, 8, 27, 18, 1, tzinfo=BEIJING).timestamp(),
                "text": "普通聊天",
                "from": {"id": 10, "username": "chat_user", "first_name": "小李"},
            },
            {
                "message_id": 4,
                "date": datetime(2026, 8, 27, 18, 2, tzinfo=BEIJING).timestamp(),
                "text": "主页 https://x.com/profileonly",
                "from": {"id": 11, "username": "profile_user", "first_name": "小张"},
            },
        ]
        links = [{
            "order": 1,
            "url": "https://x.com/newuser/status/222",
            "handle": "newuser",
            "post_id": "222",
            "role": "promo",
            "followers_count": 30000,
            "followers_text": "3W",
            "followers_sources": ["profile_v2"],
            "tweet_text": "@ToBulaer @ToBuerma",
            "required_mentions_count": 2,
        }]

        with patch("sync_group_messages.extract_x_links_ordered", return_value=links):
            count = sync_group_messages.replace_group_snapshot(registry, state, chat_id, messages, "2026-08-27")

        self.assertEqual(count, 1)
        self.assertEqual(state["groups"][chat_id]["count"], 1)
        self.assertEqual(state["groups"][chat_id]["message_ids"], ["2"])
        self.assertEqual(state["groups"][chat_id]["reply_keys"], ["1:x"])
        self.assertNotIn("old", registry["entries"][chat_id])
        self.assertIn("newuser", registry["entries"][chat_id])


if __name__ == "__main__":
    unittest.main()
