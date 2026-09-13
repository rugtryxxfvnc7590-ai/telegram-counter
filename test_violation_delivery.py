from copy import deepcopy
from datetime import datetime
from io import StringIO
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", ModuleType("requests"))
import main
from violation_delivery import deliver_violation_reply, process_violation_replies, receipt_run_key


DAY = "2026-09-13"
GROUP2 = main.GROUP_2_CHAT_ID_FALLBACK


def at(hour, minute=0):
    return datetime(2026, 9, 13, hour, minute, tzinfo=main.BEIJING)


def entry(message_id=1, **changes):
    row = {
        "chat_id": GROUP2, "message_id": message_id, "time": DAY + " 10:00:00",
        "promo_handle": "example", "promo_post_id": str(100 + message_id),
        "promo_url": f"https://x.com/example/status/{100 + message_id}",
        "followers_count": 4000, "followers_sources": ["profile_v2"],
        "check_followers_count": 4000, "check_followers_sources": ["profile_v2"],
        "check_handle": "example", "dual_link": False,
        "tweet_text": "@ToBulaer @ToBuerma", "required_mentions_count": 2,
        "tweet_text_sources": ["vx_status_v2"], "content_eligible": True,
    }
    row.update(changes)
    return row


class ViolationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.state = {"date": DAY, "groups": {}, "group_snapshot_sync": {
            "date": DAY, "completed_groups": ["群一", "群二", "群三"],
            "violation_receipt_groups": {group: {"run_key": receipt_run_key(),
                "checked_at": DAY + " 12:00:00", "bot_user_id": 900}
                for group in ("群一", "群二", "群三")},
        }}
        self.registry = {"date": DAY, "post_entries": {GROUP2: {"101": entry()}}}
        self.rules = deepcopy(main.DEFAULT_REPLY_RULES)
        self.rules["low_followers"]["text"] = "测试自定义文案"
        self.sender = patch("main.reply_to_message", return_value=True)
        self.send = self.sender.start()
        self.addCleanup(self.sender.stop)

    def run_replies(self, **kwargs):
        now = kwargs.pop("now", at(12))
        for receipt in self.state["group_snapshot_sync"]["violation_receipt_groups"].values():
            receipt["checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        return process_violation_replies(self.registry, self.state, rules=self.rules,
                                         now=now, **kwargs)

    def test_snapshot_only_message_uses_existing_custom_rule_once(self):
        self.assertEqual(list(self.run_replies().values()), ["sent"])
        for _ in range(10):
            self.run_replies()
        self.send.assert_called_once_with(GROUP2, 1, "测试自定义文案")
        self.assertEqual(self.state["groups"][GROUP2]["reply_keys"], ["1:low_followers"])

    def test_failure_does_not_mark_sent_and_next_run_retries(self):
        self.send.side_effect = [False, True]
        self.assertEqual(list(self.run_replies().values()), ["failed"])
        self.assertFalse(self.state["groups"][GROUP2].get("reply_keys"))
        self.assertEqual(list(self.run_replies(now=at(12, 1)).values()), ["retry_later"])
        self.assertEqual(list(self.run_replies(now=at(12, 15)).values()), ["sent"])
        self.assertEqual(self.send.call_count, 2)
        record = self.state["violation_replies"]["groups"]["群二"]["1:low_followers"]
        self.assertEqual(record["attempts"], 2)

    def test_success_survives_snapshot_count_state_reset(self):
        self.run_replies()
        self.state["groups"] = {}
        self.run_replies()
        self.send.assert_called_once()

    def test_legacy_success_not_replayed_and_imported_once(self):
        self.state["groups"]["-3218974409"] = {"reply_keys": ["1:low_followers"]}
        self.run_replies()
        self.state["groups"] = {}
        self.run_replies()
        self.send.assert_not_called()

    def test_disabled_or_other_group_only_rule_stays_quiet(self):
        for changes in ({"enabled": False}, {"groups": ["群一"]}):
            with self.subTest(changes=changes):
                self.rules["low_followers"] = {**main.DEFAULT_REPLY_RULES["low_followers"], **changes}
                self.run_replies()
        self.send.assert_not_called()

    def test_qualified_return_account_prevents_low_follower_reply(self):
        self.registry["post_entries"][GROUP2]["101"] = entry(
            dual_link=True, check_handle="returner", check_followers_count=20000,
        )
        self.run_replies()
        self.send.assert_not_called()

    def test_unknown_return_count_or_unverified_primary_not_violation(self):
        for changes in (
            {"dual_link": True, "check_handle": "returner", "check_followers_count": None},
            {"followers_sources": ["status_v2"]},
        ):
            self.registry["post_entries"][GROUP2]["101"] = entry(**changes)
            self.run_replies()
        self.send.assert_not_called()

    def test_long_incomplete_text_not_missing_mentions(self):
        self.registry["post_entries"][GROUP2]["101"] = entry(
            followers_count=30000, tweet_text="正文未全部返回…", required_mentions_count=0,
            tweet_text_sources=[], content_eligible=None,
        )
        self.run_replies()
        self.send.assert_not_called()

    def test_missing_mentions_still_uses_primary_only(self):
        self.registry["post_entries"][GROUP2]["101"] = entry(
            followers_count=30000, tweet_text="完整正文没有指定账号", required_mentions_count=0,
        )
        self.run_replies()
        self.send.assert_called_once_with(GROUP2, 1, self.rules["missing_mentions"]["text"])

    def test_after_19_no_retry_or_new_reply(self):
        for when in (at(19), at(23, 59)):
            self.assertEqual(self.run_replies(now=when), {})
            self.assertEqual(deliver_violation_reply(self.state, GROUP2, 1, "low_followers",
                                                     self.rules, now=when), "skipped")
        self.send.assert_not_called()

    def test_send_time_rechecked_for_each_message(self):
        self.registry["post_entries"][GROUP2]["102"] = entry(2)
        self.state["group_snapshot_sync"]["violation_receipt_groups"]["群二"]["checked_at"] = DAY + " 18:59:00"
        with patch("violation_delivery._now", side_effect=[at(18, 59), at(18, 59), at(18, 59), at(19)]):
            process_violation_replies(self.registry, self.state, rules=self.rules)
        self.send.assert_called_once()

    def test_old_day_cutoff_and_future_messages_not_replied(self):
        for stamp in ("2026-09-12 10:00:00", DAY + " 19:00:00", DAY + " 18:00:00", DAY + " bad"):
            self.registry["post_entries"][GROUP2]["101"] = entry(time=stamp)
            self.run_replies()
        self.send.assert_not_called()

    def test_incomplete_snapshot_or_deleted_message_not_replied(self):
        self.state["group_snapshot_sync"]["completed_groups"] = ["群一"]
        self.run_replies()
        self.state["group_snapshot_sync"]["completed_groups"] = ["群二"]
        self.registry["post_entries"][GROUP2] = {}
        self.run_replies()
        self.send.assert_not_called()

    def test_already_admitted_edit_uses_dedicated_receipt(self):
        with patch("main.defer_edited_link_reply", return_value=True):
            self.run_replies()
        self.send.assert_not_called()

    def test_same_id_in_another_group_does_not_dedupe_or_mix(self):
        group1 = main.GROUP_1_CHAT_ID_FALLBACK
        self.registry["post_entries"][group1] = {"101": entry(chat_id=group1)}
        self.run_replies()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual({call.args[0] for call in self.send.call_args_list}, {GROUP2, group1})

    def test_wrong_bucket_or_invalid_message_id_is_not_sent(self):
        self.registry["post_entries"][GROUP2]["101"] = entry(chat_id=main.GROUP_1_CHAT_ID_FALLBACK)
        self.run_replies()
        for message_id in (0, -1, "oops", ""):
            self.assertEqual(deliver_violation_reply(self.state, GROUP2, message_id, "low_followers",
                                                     self.rules, now=at(12)), "skipped")
        self.send.assert_not_called()

    def test_success_saved_only_after_ack(self):
        events = []
        self.send.side_effect = lambda *args: events.append("ack") or True
        self.run_replies(save_callback=lambda: events.append("save"))
        self.assertEqual(events, ["ack", "save"])


class TelegramReplyTests(unittest.TestCase):
    def response(self, status, body):
        return SimpleNamespace(status_code=status, json=lambda: body)

    def test_http_and_api_ok_are_both_required(self):
        for status, body, expected in (
            (400, {"ok": False, "description": "Bad Request: failure"}, False),
            (200, {"ok": False}, False),
            (500, {"ok": True}, False),
            (200, {"ok": True, "result": {"message_id": 2}}, True),
        ):
            with patch("main.BOT_TOKEN", "fake-token"), patch("main.requests.post", create=True,
                    return_value=self.response(status, body)) as post:
                self.assertIs(main.reply_to_message(GROUP2, 1, "text"), expected)
                payload = post.call_args.kwargs["json"]
                self.assertFalse(payload["allow_sending_without_reply"])
                self.assertEqual(payload["reply_to_message_id"], 1)

    def test_exception_is_false_and_does_not_log_token(self):
        with patch("main.BOT_TOKEN", "secret-token"), patch("main.requests.post", create=True,
                side_effect=RuntimeError("https://api.telegram.org/botsecret-token/sendMessage")), patch("sys.stdout", new_callable=StringIO) as output:
            self.assertFalse(main.reply_to_message(GROUP2, 1, "text"))
            self.assertNotIn("secret-token", output.getvalue())

    def test_failed_request_does_not_mark_reply_key(self):
        state, saved = {}, []
        with patch("main.reply_to_message", return_value=False):
            self.assertFalse(main.reply_to_message_once(state, GROUP2, 1, "low_followers", "text",
                                                        save_callback=lambda: saved.append(True)))
        self.assertEqual(state, {})
        self.assertEqual(saved, [])


if __name__ == "__main__":
    unittest.main()
