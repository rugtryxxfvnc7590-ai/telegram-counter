import copy
from contextlib import redirect_stdout
from datetime import datetime
import io
import unittest
from unittest.mock import Mock, patch

import main
import send_daily_lists
from edited_link_receipts import (
    REJECTED_TEXT, SUCCESS_TEXT, defer_edited_link_reply, process_edited_link_receipts,
)
from test_private_list_sync import DAY, GROUPS, OWNER, entry


def at(hour, minute=0, second=0):
    return datetime(2026, 9, 12, hour, minute, second, tzinfo=main.BEIJING)


class EditReceiptTest(unittest.TestCase):
    def setUp(self):
        self.registry = {"date": DAY, "post_entries": {cid: {"101": entry("Alice", 101, 1)}
                                                        for cid in GROUPS.values()}}
        self.state = {"group_snapshot_sync": {"date": DAY, "completed_groups": list(GROUPS)}}
        self.limits = {group: 1 for group in GROUPS}
        for mocker in [patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: OWNER}),
                       patch("main._send_private_message", return_value=(True, {"message_id": 901})),
                       patch("main._edit_private_message", return_value=(True, ""))]:
            mocker.start()
            self.addCleanup(mocker.stop)
        out = redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        main.send_daily_lists_to_owner(self.registry, self.state, now=at(10), limits=self.limits)
        self.reply = Mock(return_value=True)

    def record(self, group="群一"):
        return self.state["owner_daily_lists"]["groups"][group]

    def change(self, group="群一", old="101", post="202", **extra):
        bucket = self.registry["post_entries"][GROUPS[group]]
        row = dict(bucket.pop(old), promo_post_id=post, promo_url=f"https://x.com/Alice/status/{post}",
                   edited=True, edit_time=f"{DAY} 11:00:00")
        row.update(extra)
        bucket[post] = row
        return row

    def sync(self, now=None):
        return main.send_daily_lists_to_owner(self.registry, self.state, now=now or at(12), limits=self.limits)

    def receipts(self, now=None, **kwargs):
        return process_edited_link_receipts(self.registry, self.state, self.reply, OWNER,
                                           now=now or at(12), **kwargs)

    def reject(self, **extra):
        return self.change(mutual_eligible=False, ineligible_reason="missing_required_mentions", **extra)

    def test_pending_or_failed_private_edit_never_announces_success(self):
        self.change()
        self.receipts()
        self.reply.assert_not_called()
        with patch("main._edit_private_message", return_value=(False, "temporary")):
            self.sync()
        self.receipts()
        self.reply.assert_not_called()

    def test_success_only_after_private_ack_with_exact_text_and_original_message(self):
        self.change()
        self.sync()
        saved = Mock()
        self.receipts(save_callback=saved)
        self.reply.assert_called_once_with(GROUPS["群一"], 1, "更换的新链接已收录")
        saved.assert_called_once()
        for _ in range(10):
            self.receipts()
        self.reply.assert_called_once()

    def test_rejected_edit_retains_old_private_link_and_exact_reply(self):
        self.reject()
        self.sync()
        self.assertTrue(self.record()["links"][0].endswith("/101"))
        self.receipts()
        self.reply.assert_called_once_with(GROUPS["群一"], 1, REJECTED_TEXT)

    def test_both_receipts_silent_at_19_and_2359(self):
        for invalid in [False, True]:
            for when in [at(19), at(23, 59, 59)]:
                with self.subTest(invalid=invalid, now=when):
                    test = EditReceiptTest()
                    test.setUp()
                    try:
                        test.reject() if invalid else test.change()
                        test.sync(when)
                        test.receipts(when)
                        test.reply.assert_not_called()
                        expected = "101" if invalid else "202"
                        self.assertTrue(test.record()["links"][0].endswith("/" + expected))
                    finally:
                        test.doCleanups()

    def test_185959_is_allowed(self):
        self.reject()
        self.receipts(at(18, 59, 59))
        self.reply.assert_called_once()

    def test_running_loop_stops_before_next_reply_when_clock_reaches_19(self):
        self.change("群一")
        self.change("群二")
        self.sync()
        with patch("edited_link_receipts._now", side_effect=[at(18, 59, 59), at(18, 59, 59), at(19)]):
            process_edited_link_receipts(self.registry, self.state, self.reply, OWNER)
        self.reply.assert_called_once()

    def test_no_reply_when_private_delivery_id_or_text_not_confirmed(self):
        self.change()
        self.sync()
        original = copy.deepcopy(self.record())
        for field, value in [("message_id", None), ("sent", False), ("text", "old"),
                             ("links", []), ("owner_chat_id", "99999")]:
            self.state["owner_daily_lists"]["groups"]["群一"] = dict(original, **{field: value})
            self.receipts()
            self.reply.assert_not_called()

    def test_rejection_also_requires_private_old_link_confirmation(self):
        self.reject()
        self.record()["message_id"] = None
        self.receipts()
        self.reply.assert_not_called()

    def test_unknown_quality_is_not_called_violation(self):
        row = self.change(mutual_eligible=None)
        for value, reason in [(None, ""), (False, ""), (False, "followers_low_unconfirmed")]:
            row.update(mutual_eligible=value, ineligible_reason=reason)
            self.receipts()
            self.reply.assert_not_called()

    def test_confirmed_followers_violation_uses_rejection(self):
        self.change(mutual_eligible=False, ineligible_reason="followers_below_minimum")
        self.receipts()
        self.reply.assert_called_once_with(GROUPS["群一"], 1, REJECTED_TEXT)

    def test_reply_failure_retries_but_does_not_reedit_private_message(self):
        self.change()
        self.sync()
        self.reply.return_value = False
        self.receipts()
        self.assertEqual(self.state["edited_link_receipts"]["groups"]["群一"], {})
        self.reply.return_value = True
        with patch("main._edit_private_message", side_effect=AssertionError("重复编辑私信")):
            self.sync()
            self.receipts()
            self.receipts()
        self.assertEqual(self.reply.call_count, 2)

    def test_failed_receipt_is_not_retried_after_19(self):
        self.reject()
        self.reply.return_value = False
        self.receipts(at(18, 59))
        self.receipts(at(19))
        self.reply.assert_called_once()

    def test_caption_only_change_does_not_repeat_success_or_rejection(self):
        row = self.reject()
        self.receipts()
        row["edit_time"] = f"{DAY} 11:10:00"
        self.receipts()
        self.reply.assert_called_once()
        row.update(mutual_eligible=True, ineligible_reason="")
        self.sync()
        self.receipts()
        self.assertEqual(self.reply.call_count, 2)
        row["edit_time"] = f"{DAY} 11:20:00"
        self.sync()
        self.receipts()
        self.assertEqual(self.reply.call_count, 2)

    def test_rejected_then_corrected_new_post_can_get_success_receipt(self):
        row = self.reject()
        self.receipts()
        row.update(mutual_eligible=True, ineligible_reason="")
        self.sync()
        self.receipts()
        self.assertEqual([c.args[2] for c in self.reply.call_args_list], [REJECTED_TEXT, SUCCESS_TEXT])

    def test_next_invalid_link_retains_last_accepted_not_very_first_link(self):
        self.change()
        self.sync()
        self.receipts()
        self.change(old="202", post="303", mutual_eligible=False,
                    ineligible_reason="missing_required_mentions", edit_time=f"{DAY} 11:10:00")
        self.sync()
        self.receipts()
        self.assertTrue(self.record()["links"][0].endswith("/202"))
        self.assertEqual(self.reply.call_args.args[2], REJECTED_TEXT)

    def test_three_groups_use_own_message_and_ledger(self):
        for group in GROUPS:
            self.change(group)
        self.sync()
        self.receipts()
        self.assertEqual({c.args[0] for c in self.reply.call_args_list}, set(GROUPS.values()))
        self.assertEqual(self.reply.call_count, 3)

    def test_different_account_message_sender_or_return_link_does_not_reply(self):
        row = self.change()
        original = dict(row)
        for extra in [{"message_id": 99}, {"promo_handle": "other"}, {"tg_user_id": 99999},
                      {"promo_post_id": "888"}]:
            row.clear()
            row.update(original, **extra)
            self.sync()
            self.receipts()
            self.reply.assert_not_called()

    def test_deleted_or_incomplete_snapshot_does_not_reply(self):
        self.change()
        self.sync()
        self.state["group_snapshot_sync"]["completed_groups"] = []
        self.receipts()
        self.reply.assert_not_called()
        self.state["group_snapshot_sync"]["completed_groups"] = list(GROUPS)
        self.registry["post_entries"][GROUPS["群一"]] = {}
        self.receipts()
        self.reply.assert_not_called()

    def test_prior_day_not_replied_next_morning(self):
        self.reject()
        self.receipts(datetime(2026, 9, 13, 8, tzinfo=main.BEIJING))
        self.reply.assert_not_called()

    def test_admitted_edit_defers_generic_violation_even_after_19(self):
        self.reject()
        for when in [at(12), at(19), at(23, 59)]:
            self.assertTrue(defer_edited_link_reply(self.registry, self.state, GROUPS["群一"], 1, now=when))
        self.assertFalse(defer_edited_link_reply(self.registry, self.state, GROUPS["群一"], 999, now=at(12)))
        self.assertFalse(defer_edited_link_reply(self.registry, self.state, GROUPS["群二"], 1, now=at(12)))

    def test_save_ack_happens_before_group_reply_in_delivery_entrypoint(self):
        self.change()
        events = []
        def ack(*args, **kwargs):
            events.append("private_ack")
            return True, ""
        def reply(*args):
            self.assertIn("saved_ack", events)
            events.append("group_reply")
            return True
        def save(state):
            if state["owner_daily_lists"]["groups"]["群一"]["slots"][0].get("edited"):
                events.append("saved_ack")
        with patch("send_daily_lists.load_state", return_value=self.state), patch("send_daily_lists.load_registry", return_value=self.registry), patch("send_daily_lists.load_daily_limits", return_value=self.limits), patch("send_daily_lists.save_state", side_effect=save), patch("send_daily_lists.save_registry"), patch("send_daily_lists.process_capacity_replies"), patch("send_daily_lists.send_capacity_reply", side_effect=reply), patch("main._edit_private_message", side_effect=ack), patch("send_daily_lists.datetime") as clock, patch("main.datetime") as main_clock, patch("edited_link_receipts._now", return_value=at(12)):
            clock.now.return_value = at(12)
            main_clock.now.return_value = at(12)
            main_clock.strptime.side_effect = datetime.strptime
            send_daily_lists.main()
        self.assertLess(events.index("private_ack"), events.index("saved_ack"))
        self.assertLess(events.index("saved_ack"), events.index("group_reply"))


if __name__ == "__main__":
    unittest.main()
