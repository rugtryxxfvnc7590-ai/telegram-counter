import asyncio
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", ModuleType("requests"))
import main
import sync_group_messages
from test_violation_delivery import DAY, GROUP2, at, entry
from violation_delivery import (
    deliver_violation_reply, history_is_current, process_violation_replies,
    receipt_run_key, recover_violation_replies, verified_reply_bot_id,
)


BOT_ID = 900


def receipt(message_id=20, reply_to=1, **changes):
    message = {"message_id": message_id, "reply_to_message_id": reply_to,
               "date": int(at(11).timestamp()), "text": main.LOW_FOLLOWER_REPLY_TEXT,
               "from": {"id": BOT_ID, "is_bot": True}}
    message.update(changes)
    return message


class ViolationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.state = {"date": DAY, "groups": {GROUP2: {"count": 1, "message_ids": ["1"]}},
                      "group_snapshot_sync": {"date": DAY, "completed_groups": ["群二"]}}
        self.registry = {"date": DAY, "post_entries": {GROUP2: {"101": entry()}}}
        self.rules = deepcopy(main.DEFAULT_REPLY_RULES)
        self.patcher = patch("main.reply_to_message", return_value=True)
        self.sender = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def recover(self, messages=None, state=None, **kwargs):
        return recover_violation_replies(state if state is not None else self.state, GROUP2,
                                         messages if messages is not None else [receipt()],
                                         kwargs.pop("bot_id", BOT_ID), self.rules,
                                         now=kwargs.pop("now", at(12)), **kwargs)

    def send(self, state=None):
        return process_violation_replies(self.registry, state if state is not None else self.state,
                                         rules=self.rules, now=at(12))

    def test_send_success_then_git_upload_loss_is_recovered_from_actual_reply(self):
        old_remote = deepcopy(self.state)
        self.recover([])
        self.send()
        self.sender.assert_called_once()
        self.sender.reset_mock()
        # Next run starts with the old remote JSON, just like the failed 17:15 push.
        for _ in range(10):
            fresh_run = deepcopy(old_remote)
            self.assertEqual(self.recover(state=fresh_run), 1)
            self.assertEqual(list(self.send(fresh_run).values()), ["already_sent"])
        self.sender.assert_not_called()

    def test_unknown_http_outcome_but_visible_reply_is_not_retried(self):
        self.state["violation_replies"] = {"date": DAY, "groups": {"群二": {
            "1:low_followers": {"status": "failed", "retry_after": DAY + " 11:10:00"}}}}
        self.recover()
        self.send()
        record = self.state["violation_replies"]["groups"]["群二"]["1:low_followers"]
        self.assertEqual(record["status"], "sent")
        self.assertEqual(record["telegram_reply_message_id"], 20)
        self.assertNotIn("retry_after", record)
        self.sender.assert_not_called()

    def test_multiple_old_replies_recover_one_key_using_first_ack(self):
        self.assertEqual(self.recover([receipt(21), receipt(20)]), 1)
        record = self.state["violation_replies"]["groups"]["群二"]["1:low_followers"]
        self.assertEqual(record["telegram_reply_message_id"], 20)

    def test_other_bot_or_user_with_same_name_and_text_cannot_forge_receipt(self):
        for sender in ({"id": 901, "is_bot": True}, {"id": BOT_ID, "is_bot": False}):
            self.assertEqual(self.recover([receipt(**{"from": sender})]), 0)
        self.assertEqual(self.state["violation_replies"]["groups"]["群二"], {})

    def test_only_original_message_in_same_group_can_be_confirmed(self):
        self.assertEqual(self.recover([receipt(reply_to=2)]), 0)
        other = main.GROUP_3_CHAT_ID_FALLBACK
        self.state["groups"][other] = {"message_ids": ["1"]}
        recover_violation_replies(self.state, other, [receipt()], BOT_ID, self.rules, now=at(12))
        self.recover([])
        self.send()
        self.sender.assert_called_once_with(GROUP2, 1, main.LOW_FOLLOWER_REPLY_TEXT)

    def test_unthreaded_old_and_future_messages_are_not_receipts(self):
        variants = [receipt(reply_to=None), receipt(date=int((at(11) - timedelta(days=1)).timestamp())),
                    receipt(date=int(at(13).timestamp()))]
        self.assertEqual(self.recover(variants), 0)

    def test_customized_reply_and_previous_attempt_text_are_recognized(self):
        self.rules["low_followers"]["text"] = "我的新文案"
        self.state["violation_replies"] = {"date": DAY, "groups": {"群二": {
            "1:low_followers": {"status": "failed", "text": "我的旧文案"}}}}
        self.assertEqual(self.recover([receipt(text="我的旧文案")]), 1)
        self.send()
        self.sender.assert_not_called()
        self.state.pop("violation_replies")
        self.assertEqual(self.recover([receipt(text="我的新文案")]), 1)

    def test_capacity_and_edit_receipts_are_not_mistaken_for_violations(self):
        self.assertEqual(self.recover([receipt(text="更换的新链接已收录"),
                                       receipt(text="今日互推已满30条")]), 0)

    def test_missing_mentions_receipt_stays_its_own_reason(self):
        self.assertEqual(self.recover([receipt(text=main.INVALID_MENTIONS_REPLY_TEXT)]), 1)
        records = self.state["violation_replies"]["groups"]["群二"]
        self.assertIn("1:missing_mentions", records)
        self.assertNotIn("1:low_followers", records)

    def test_getupdates_cannot_send_before_current_history_is_checked(self):
        self.assertEqual(deliver_violation_reply(self.state, GROUP2, 1, "low_followers",
                                                 self.rules, now=at(12)), "awaiting_history")
        self.send()
        self.sender.assert_not_called()

    def test_history_without_verified_bot_identity_cannot_enable_sending(self):
        self.assertEqual(self.recover(bot_id=None), 0)
        self.assertFalse(history_is_current(self.state, "群二", at(12)))
        self.send()
        self.sender.assert_not_called()

    def test_previous_run_or_rerun_attempt_proof_is_not_reused(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "run1", "GITHUB_RUN_ATTEMPT": "1"}):
            self.recover([])
            self.assertTrue(history_is_current(self.state, "群二", at(12)))
        for env in ({"GITHUB_RUN_ID": "run2", "GITHUB_RUN_ATTEMPT": "1"},
                    {"GITHUB_RUN_ID": "run1", "GITHUB_RUN_ATTEMPT": "2"}):
            with patch.dict(os.environ, env):
                self.assertFalse(history_is_current(self.state, "群二", at(12)))
                self.send()
        self.sender.assert_not_called()

    def test_stale_or_incomplete_history_proof_blocks_new_sends(self):
        self.recover([], now=at(11))
        self.send()
        self.recover([])
        self.state["group_snapshot_sync"]["completed_groups"] = []
        self.send()
        self.sender.assert_not_called()

    def test_confirmed_not_sent_still_retries_normally(self):
        self.state["violation_replies"] = {"date": DAY, "groups": {"群二": {
            "1:low_followers": {"status": "failed", "retry_after": DAY + " 11:10:00"}}}}
        self.recover([])
        self.send()
        self.sender.assert_called_once()

    def test_disabled_rule_and_after_19_stay_quiet(self):
        self.rules["low_followers"]["enabled"] = False
        self.recover([])
        self.send()
        self.rules["low_followers"]["enabled"] = True
        self.recover([], now=at(19))
        process_violation_replies(self.registry, self.state, rules=self.rules, now=at(19))
        self.sender.assert_not_called()


class ReceiptInputTests(unittest.TestCase):
    def test_user_history_retains_exact_sender_and_reply_anchor(self):
        class Client:
            async def iter_messages(self, entity, limit):
                async def sender():
                    return SimpleNamespace(id=BOT_ID, bot=True)
                yield SimpleNamespace(id=20, date=at(11), edit_date=None, raw_text=main.LOW_FOLLOWER_REPLY_TEXT,
                                      get_sender=sender, sender_id=BOT_ID, reply_to_msg_id=1)
        messages = asyncio.run(sync_group_messages._today_messages(Client(), None, at(0)))
        self.assertEqual(messages[0]["from"]["id"], BOT_ID)
        self.assertTrue(messages[0]["from"]["is_bot"])
        self.assertEqual(messages[0]["reply_to_message_id"], 1)

    def test_identity_must_be_confirmed_by_this_tokens_getme(self):
        for status, body, expected in (
            (200, {"ok": True, "result": {"id": BOT_ID, "is_bot": True}}, BOT_ID),
            (400, {"ok": False}, None),
            (200, {"ok": True, "result": {"id": BOT_ID, "is_bot": False}}, None),
        ):
            response = SimpleNamespace(status_code=status, json=lambda: body)
            with patch("main.BOT_TOKEN", "fake-token"), patch("main.requests.get", create=True, return_value=response):
                self.assertEqual(verified_reply_bot_id(), expected)

    def test_identity_failure_does_not_throw_or_expose_credentials(self):
        with patch("main.BOT_TOKEN", "fake-token"), patch("main.requests.get", create=True, side_effect=TimeoutError):
            self.assertIsNone(verified_reply_bot_id())

    def test_workflow_keeps_schedule_and_adds_bounded_non_force_push_retry(self):
        workflow = Path(__file__).with_name(".github").joinpath("workflows/run.yml").read_text()
        self.assertEqual([line.strip() for line in workflow.splitlines() if "- cron:" in line],
                         ["- cron: '*/15 * * * *'", "- cron: '5,10 11 * * *'"])
        self.assertIn("for attempt in 1 2 3; do", workflow)
        self.assertNotIn("--force", workflow)


if __name__ == "__main__":
    unittest.main()
