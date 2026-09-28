from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from cutoff_delivery import (
    _plain_text, process_cutoff_announcements, recover_cutoff_announcements,
    render_cutoff_text, reply_target, send_cutoff_message,
)
from violation_delivery import receipt_run_key

DAY = "2026-09-22"
NOW = datetime(2026, 9, 22, 19, tzinfo=main.BEIJING)
GROUPS = dict(main.canonical_group_chat_ids())
TEMPLATE = "{date_label}已截止，共{success_count}条\n\n[点击主页](https://x.com/example)"


def fixture():
    state = {"date": DAY, "groups": {},
             "owner_daily_lists": {"date": DAY, "groups": {}},
             "group_snapshot_sync": {"date": DAY, "completed_groups": list(GROUPS),
                                     "cutoff_receipt_groups": {}}}
    for group, cid in GROUPS.items():
        slots = [{"message_id": "20", "position": 1, "time": f"{DAY} 18:00:00",
                  "url": "https://x.com/two/status/2"},
                 {"message_id": "10", "position": 2, "time": f"{DAY} 17:00:00",
                  "url": "https://x.com/one/status/1"}]
        links = [s["url"] for s in slots]
        state["groups"][cid] = {"message_ids": [10, 20]}
        state["owner_daily_lists"]["groups"][group] = {
            "sent": True, "count": 2, "message_id": 999, "owner_chat_id": "123456",
            "links": links, "slots": slots,
            "text": main.format_daily_list_message(group, DAY, links, slots=slots),
        }
        state["group_snapshot_sync"]["cutoff_receipt_groups"][group] = {
            "run_key": receipt_run_key(), "checked_at": f"{DAY} 19:00:00", "bot_user_id": 123,
        }
    rules = {g: {"enabled": True, "text": TEMPLATE} for g in GROUPS}
    return {"date": DAY}, state, rules


class CloudCutoffTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.state, self.rules = fixture()
        self.sender = Mock(return_value=(True, {"message_id": 1001}))

    def run_cutoff(self, now=NOW, **kwargs):
        return process_cutoff_announcements(self.registry, self.state, now=now, rules=self.rules,
                                            send_message=self.sender, **kwargs)

    def test_19_sends_one_per_group_without_local_publish_state(self):
        self.assertEqual(set(self.run_cutoff().values()), {"sent"})
        self.assertEqual(self.sender.call_count, 3)
        for call in self.sender.call_args_list:
            self.assertIn(call.args[0], GROUPS.values())
            self.assertEqual(len(call.args), 2)
            self.assertIn("共2条", call.args[1])
            self.assertIn("https://x.com/example", call.args[1])
        for record in self.state["cutoff_announcements"]["groups"].values():
            self.assertEqual(record["delivery_mode"], "standalone")
            self.assertNotIn("reply_to_message_id", record)

    def test_before_19_and_next_day_do_not_send_previous_day(self):
        for now in (NOW.replace(hour=18, minute=59, second=59), NOW.replace(day=23, hour=0)):
            self.assertEqual(self.run_cutoff(now), {})
        self.sender.assert_not_called()

    def test_utc_equivalent_time_sends(self):
        self.assertEqual(set(self.run_cutoff(NOW.astimezone(timezone.utc)).values()), {"sent"})

    def test_success_survives_state_roundtrip_and_does_not_resend(self):
        self.run_cutoff()
        self.state = json.loads(json.dumps(self.state))
        self.assertEqual(set(self.run_cutoff().values()), {"already_sent"})
        self.assertEqual(self.sender.call_count, 3)

    def test_disabled_rule_and_custom_text(self):
        self.rules["群二"]["enabled"] = False
        self.rules["群三"]["text"] = "自定义：{date_label} / {success_count}"
        result = self.run_cutoff()
        self.assertEqual(result["群二"], "disabled")
        self.assertEqual(self.sender.call_args.args[1], "自定义：9月22日 / 2")

    def test_missing_original_message_target_does_not_block_announcement(self):
        slots = self.state["owner_daily_lists"]["groups"]["群一"]["slots"]
        for slot in slots:
            slot.pop("message_id")
            slot.pop("time")
        self.assertIsNone(reply_target(slots, DAY))
        self.assertEqual(self.run_cutoff()["群一"], "sent")

    def test_sent_legacy_reply_is_not_sent_again_after_upgrade(self):
        self.state["cutoff_announcements"] = {"date": DAY, "groups": {
            "群一": {"status": "sent", "reply_to_message_id": 20, "message_id": 880}}}
        self.assertEqual(self.run_cutoff()["群一"], "already_sent")
        self.assertEqual(self.sender.call_count, 2)

    def test_failed_legacy_reply_retries_as_standalone(self):
        self.state["cutoff_announcements"] = {"date": DAY, "groups": {
            "群一": {"status": "failed", "reply_to_message_id": 20, "attempts": 1}}}
        self.assertEqual(self.run_cutoff()["群一"], "sent")
        record = self.state["cutoff_announcements"]["groups"]["群一"]
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["delivery_mode"], "standalone")
        self.assertNotIn("reply_to_message_id", record)

    def test_failed_group_retries_without_resending_successful_groups(self):
        self.sender.side_effect = [(False, {"error": "offline"}), (True, {"message_id": 2}),
                                   (True, {"message_id": 3}), (True, {"message_id": 4})]
        self.assertEqual(self.run_cutoff()["群一"], "failed")
        self.assertEqual(self.run_cutoff()["群一"], "sent")
        self.assertEqual(self.sender.call_count, 4)

    def test_failed_or_old_snapshot_waits_without_guessing(self):
        self.state["group_snapshot_sync"]["cutoff_receipt_groups"]["群一"]["checked_at"] = f"{DAY} 18:00:00"
        self.state["group_snapshot_sync"]["completed_groups"].remove("群二")
        self.state["group_snapshot_sync"]["cutoff_receipt_groups"]["群三"]["run_key"] = "old-run"
        self.assertEqual(set(self.run_cutoff().values()), {"awaiting_history"})
        self.sender.assert_not_called()

    def test_unpublished_or_inconsistent_private_roster_waits(self):
        self.state["owner_daily_lists"]["groups"]["群一"]["sent"] = False
        self.state["owner_daily_lists"]["groups"]["群二"]["count"] = 99
        self.state["owner_daily_lists"]["groups"]["群三"]["slots"][0]["url"] = "https://x.com/wrong/status/55"
        self.assertEqual(set(self.run_cutoff().values()), {"awaiting_published_roster"})
        self.sender.assert_not_called()

    def test_latest_admission_not_list_end_or_largest_message_id(self):
        slots = [{"message_id": "80", "time": f"{DAY} 01:00:00", "admission_time": f"{DAY} 18:59:00"},
                 {"message_id": "90", "time": f"{DAY} 18:00:00"},
                 {"message_id": "100", "time": f"{DAY} 19:00:00"}]
        self.assertEqual(reply_target(slots, DAY), 80)

    def test_pending_private_update_waits_for_confirmed_final_roster(self):
        self.state["owner_daily_lists"]["groups"]["群一"]["pending_roster"] = {"slots": []}
        self.assertEqual(self.run_cutoff()["群一"], "awaiting_published_roster")
        self.assertEqual(self.sender.call_count, 2)

    def test_invalid_template_does_not_block_other_groups(self):
        self.rules["群一"]["text"] = "{unknown}"
        self.assertEqual(self.run_cutoff()["群一"], "invalid_template")
        self.assertEqual(self.sender.call_count, 2)

    def history_message(self):
        return {"message_id": 880, "date": int(NOW.timestamp()), "reply_to_message_id": 20,
                "from": {"id": 123, "is_bot": True},
                "text": _plain_text(render_cutoff_text(TEMPLATE, DAY, 2))}

    def test_recover_lost_ack_only_from_our_bot_actual_reply(self):
        message = self.history_message()
        for changes in ({"from": {"id": 999, "is_bot": True}}, {"reply_to_message_id": 999},
                        {"date": int(NOW.replace(day=21).timestamp())}):
            self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
                [{**message, **changes}], 123, self.rules, NOW), 0)
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [message], 123, self.rules, NOW), 1)
        self.assertEqual(self.run_cutoff()["群一"], "already_sent")
        self.assertEqual(self.sender.call_count, 2)

    def test_history_recovers_even_if_whole_previous_state_commit_was_lost(self):
        self.state.pop("owner_daily_lists")
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [self.history_message()], 123, self.rules, NOW), 1)
        self.assertEqual(self.state["cutoff_announcements"]["groups"]["群一"]["count"], 2)
        self.assertEqual(self.run_cutoff()["群一"], "already_sent")

    def test_standalone_history_recovers_lost_ack_and_does_not_repeat(self):
        message = self.history_message()
        message.pop("reply_to_message_id")
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [message], 123, self.rules, NOW), 1)
        record = self.state["cutoff_announcements"]["groups"]["群一"]
        self.assertEqual(record["delivery_mode"], "standalone")
        self.assertIsNone(record["reply_to_message_id"])
        self.assertEqual(self.run_cutoff()["群一"], "already_sent")

    def test_standalone_history_without_saved_roster_is_still_deduplicated(self):
        self.state.pop("owner_daily_lists")
        message = self.history_message()
        message.pop("reply_to_message_id")
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [message], 123, self.rules, NOW), 1)
        self.assertEqual(self.run_cutoff()["群一"], "already_sent")

    def test_standalone_history_rejects_other_sender_day_or_text(self):
        message = self.history_message()
        message.pop("reply_to_message_id")
        for changes in ({"from": {"id": 999, "is_bot": True}},
                        {"date": int(NOW.replace(day=21).timestamp())},
                        {"date": int(NOW.replace(hour=18).timestamp())},
                        {"text": "其他公告"}):
            self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
                [{**message, **changes}], 123, self.rules, NOW), 0)

    def test_history_count_placeholders_must_agree(self):
        template = "{date_label}截止{success_count}条，再次核对{success_count}条"
        self.rules["群一"]["text"] = template
        self.state.pop("owner_daily_lists")
        message = self.history_message()
        message["text"] = render_cutoff_text(template, DAY, 2)
        bad = dict(message, text=message["text"].replace("核对2条", "核对3条"))
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [bad], 123, self.rules, NOW), 0)
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [message], 123, self.rules, NOW), 1)

    def test_malformed_template_does_not_break_group_snapshot(self):
        self.rules["群一"]["text"] = "invalid {"
        self.assertEqual(recover_cutoff_announcements(self.state, GROUPS["群一"],
            [self.history_message()], 123, self.rules, NOW), 0)

    def test_api_payload_keeps_markdown_links_without_any_reply_fields(self):
        post = Mock(return_value=SimpleNamespace(status_code=200,
                    json=lambda: {"ok": True, "result": {"message_id": 99}}))
        with patch.object(main, "BOT_TOKEN", "test"), patch.object(main.requests, "post", post, create=True):
            self.assertTrue(send_cutoff_message(GROUPS["群一"], TEMPLATE)[0])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload, {"chat_id": GROUPS["群一"], "text": TEMPLATE,
                                  "parse_mode": "Markdown", "disable_web_page_preview": True})

    def test_api_failure_does_not_leak_token_or_claim_success(self):
        with patch.object(main, "BOT_TOKEN", "test-private-token"), \
                patch.object(main.requests, "post", side_effect=RuntimeError("test-private-token"), create=True):
            ok, detail = send_cutoff_message(GROUPS["群一"], TEMPLATE)
        self.assertFalse(ok)
        self.assertNotIn("test-private-token", str(detail))


if __name__ == "__main__":
    unittest.main()
