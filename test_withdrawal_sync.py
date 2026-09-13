import asyncio
import copy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import main
import sync_deleted_messages as deletion
import sync_group_messages as snapshot
import test_private_list_sync as fixtures
from daily_capacity import admitted_rows
from edited_link_receipts import _published_slots
from withdrawal_sync import note_withdrawals

DAY, NOW, GROUPS = fixtures.DAY, fixtures.NOW, fixtures.GROUPS


class WithdrawalListTest(unittest.TestCase):
    setUp = fixtures.PrivateListSyncTest.setUp
    deliver = fixtures.PrivateListSyncTest.deliver
    record = fixtures.PrivateListSyncTest.record
    change = fixtures.PrivateListSyncTest.change

    def remove(self, group="群一", post="101", now=NOW):
        row = self.registry["post_entries"][GROUPS[group]].pop(post)
        note_withdrawals(self.registry, GROUPS[group], {str(row["message_id"]): "message_deleted_by_id"}, now)

    def test_confirmed_deletion_refills_first_waiter_edits_existing_message(self):
        self.remove()
        self.deliver()
        r = self.record()
        self.assertEqual([s["handle"] for s in r["slots"]], ["waiting", "bob"])
        self.assertEqual(r["count"], 2)
        self.assertIn("1 https://x.com/waiting/status/303\n\n2 ", r["text"])
        self.assertEqual(self.edit.call_args.args[:2], (fixtures.OWNER, 900))
        self.send.assert_not_called()
        self.assertNotIn("pending_roster", r)
        self.assertEqual(len(_published_slots(self.state, "群一", DAY, fixtures.OWNER)), 2)

    def test_twenty_runs_same_data_no_repeat_edits(self):
        self.remove()
        self.deliver()
        final = copy.deepcopy(self.record())
        for _ in range(20):
            self.deliver()
            self.assertEqual(self.record(), final)
        self.edit.assert_called_once()

    def test_no_evidence_does_not_treat_missing_row_as_deleted(self):
        self.registry["post_entries"][GROUPS["群一"]].pop("101")
        self.deliver()
        self.edit.assert_not_called()

    def test_bad_new_post_is_not_withdrawal(self):
        self.change(mutual_eligible=False, ineligible_reason="missing_required_mentions")
        self.deliver()
        self.assertTrue(self.record()["links"][-1].endswith("/101"))
        self.edit.assert_not_called()

    def test_no_waiter_reduces_count_then_later_waiter_can_fill(self):
        self.registry["post_entries"][GROUPS["群一"]].pop("303")
        self.remove()
        self.deliver()
        self.assertEqual(self.record()["count"], 1)
        self.assertIn("共 1 条", self.record()["text"])
        self.registry["post_entries"][GROUPS["群一"]]["404"] = fixtures.entry("later", 404, 4, 3)
        self.deliver()
        self.assertEqual([s["handle"] for s in self.record()["slots"]], ["later", "bob"])
        self.assertEqual(self.record()["count"], 2)

    def test_confirmed_at_19_does_not_change_roster(self):
        for hour, minute, second in ((19, 0, 0), (23, 59, 59)):
            with self.subTest(hour=hour):
                registry = copy.deepcopy(self.registry)
                late = NOW.replace(hour=hour, minute=minute, second=second)
                self.remove(now=late)
                self.deliver(late)
                self.edit.assert_not_called()
                self.assertNotIn("confirmed_withdrawals", self.registry)
                self.registry = registry

    def test_185959_allowed(self):
        now = NOW.replace(hour=18, minute=59, second=59)
        self.remove(now=now)
        self.deliver(now)
        self.assertEqual(self.record()["slots"][0]["handle"], "waiting")

    def test_no_new_refill_after_cutoff(self):
        self.registry["post_entries"][GROUPS["群一"]].pop("303")
        self.remove()
        self.deliver()
        self.registry["post_entries"][GROUPS["群一"]]["404"] = fixtures.entry("later", 404, 4, 3)
        self.deliver(NOW.replace(hour=19))
        self.assertEqual(self.record()["count"], 1)
        self.edit.assert_called_once()

    def test_failed_edit_keeps_ack_and_pending_plan_retries_after_19(self):
        old = copy.deepcopy(self.record())
        self.remove()
        self.edit.return_value = (False, "network")
        saved = []
        self.deliver(save_callback=lambda: saved.append(copy.deepcopy(self.record())))
        r = self.record()
        self.assertEqual({k: r[k] for k in old}, old)
        self.assertIn("pending_roster", r)
        self.assertIn("pending_roster", saved[0])
        self.edit.return_value = (True, "")
        self.deliver(NOW.replace(hour=19))
        self.assertEqual([s["handle"] for s in r["slots"]], ["waiting", "bob"])
        self.assertNotIn("pending_roster", r)
        self.send.assert_not_called()

    def test_pending_new_member_can_also_withdraw_before_ack(self):
        bucket = self.registry["post_entries"][GROUPS["群一"]]
        bucket["404"] = fixtures.entry("next", 404, 4, 3)
        self.remove()
        self.edit.return_value = (False, "network")
        self.deliver()
        self.remove(post="303")
        self.edit.return_value = (True, "")
        self.deliver()
        self.assertEqual([s["handle"] for s in self.record()["slots"]], ["next", "bob"])

    def test_processing_crossing_19_stops_new_refills_in_remaining_groups(self):
        before = NOW.replace(hour=18, minute=59, second=59)
        self.remove("群一", now=before)
        self.remove("群二", now=before)
        class Clock(datetime):
            ticks = iter([before, before, before.replace(hour=19), before.replace(hour=19)])
            @classmethod
            def now(cls, tz=None):
                return next(cls.ticks)
        with patch("main.datetime", Clock):
            self.deliver(now=None)
        self.assertEqual(self.record("群一")["slots"][0]["handle"], "waiting")
        self.assertEqual(self.record("群二")["slots"][-1]["handle"], "alice")
        self.edit.assert_called_once()

    def test_failed_or_wrong_date_snapshot_no_roster_update(self):
        self.remove()
        self.state["group_snapshot_sync"]["completed_groups"].remove("群一")
        self.deliver()
        self.edit.assert_not_called()
        self.state["group_snapshot_sync"]["completed_groups"].append("群一")
        self.registry["confirmed_withdrawals"]["date"] = "2026-09-11"
        self.deliver()
        self.edit.assert_not_called()

    def test_unqualified_late_profile_and_return_links_not_candidates(self):
        bucket = self.registry["post_entries"][GROUPS["群一"]]
        bucket["303"]["mutual_eligible"] = False
        bucket["304"] = fixtures.entry("late", 304, 4, time=f"{DAY} 19:00:00")
        bucket["305"] = fixtures.entry("unknown", 305, 5, mutual_eligible=None)
        bucket["306"] = fixtures.entry("return", 999, 6)
        bucket["307"] = fixtures.entry("after", 307, 7, after_cutoff=True)
        bucket["308"] = fixtures.entry("profile", "", 8, promo_url="https://x.com/profile")
        bucket["309"] = fixtures.entry("good", 309, 9, 4)
        self.remove()
        self.deliver()
        self.assertEqual([s["handle"] for s in self.record()["slots"]], ["good", "bob"])

    def test_multiple_withdrawals_and_identical_timestamps_follow_message_order(self):
        bucket = self.registry["post_entries"][GROUPS["群一"]]
        bucket["405"] = fixtures.entry("fifth", 405, 5, 2)
        bucket["404"] = fixtures.entry("fourth", 404, 4, 2)
        self.remove()
        self.remove(post="202")
        self.deliver()
        self.assertEqual([s["handle"] for s in self.record()["slots"]], ["fourth", "waiting"])

    def test_each_group_independent_even_with_same_message_id(self):
        for group in GROUPS:
            with self.subTest(group=group):
                self.remove(group)
                self.deliver()
                self.assertEqual(self.edit.call_args.args[2].split("（")[0], group)
                self.assertEqual(self.record(group)["slots"][0]["handle"], "waiting")
        self.assertEqual(self.edit.call_count, 3)

    def test_increased_limit_does_not_expand_original_roster(self):
        self.limits["群一"] = 40
        self.remove()
        self.registry["post_entries"][GROUPS["群一"]]["404"] = fixtures.entry("next", 404, 4, 3)
        self.deliver()
        self.assertEqual(self.record()["count"], 2)

    def test_unbound_member_does_not_guess_withdrawal_by_username(self):
        self.record()["slots"][-1].pop("message_id")
        self.remove()
        self.deliver()
        self.edit.assert_not_called()

    def test_edited_marker_survives_renumbering(self):
        self.change()
        self.deliver()
        self.remove(post="202")
        self.deliver()
        self.assertIn("已编辑 2 https://x.com/alice/status/404", self.record()["text"])

    def test_before_first_private_list_existing_capacity_computation_refills(self):
        self.state["owner_daily_lists"]["groups"].pop("群一")
        self.remove()
        self.deliver()
        self.assertEqual(self.record()["slots"][0]["handle"], "waiting")
        self.send.assert_called_once()


class SnapshotWithdrawalTest(unittest.TestCase):
    setUp = fixtures.PrivateListSyncTest.setUp
    deliver = fixtures.PrivateListSyncTest.deliver
    record = fixtures.PrivateListSyncTest.record
    def messages(self, group="群一"):
        messages = []
        for row in self.registry["post_entries"][GROUPS[group]].values():
            row["message_text"] = row["promo_url"]
            date = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=main.BEIJING)
            messages.append({"message_id": row["message_id"], "date": int(date.timestamp()),
                             "text": row["promo_url"], "from": {"id": row["tg_user_id"]}})
        return messages

    def test_complete_snapshot_deletion_changes_backend_and_private_list(self):
        messages = [m for m in self.messages() if m["message_id"] != 1]
        snapshot.replace_group_snapshot(self.registry, self.state, GROUPS["群一"], messages, DAY, now=NOW)
        self.assertNotIn("101", self.registry["post_entries"][GROUPS["群一"]])
        accepted, _ = admitted_rows(self.registry, main.expand_chat_id(GROUPS["群一"]), 2, DAY)
        self.assertEqual([r["post_id"] for r in accepted], ["202", "303"])
        self.deliver()
        self.assertEqual(self.record()["slots"][0]["handle"], "waiting")

    def test_remove_all_post_links_but_keep_message_is_withdrawal(self):
        messages = self.messages()
        messages[0]["text"] = "不参加了 https://x.com/Alice"
        snapshot.replace_group_snapshot(self.registry, self.state, GROUPS["群一"], messages, DAY, now=NOW)
        self.deliver()
        self.assertEqual(self.record()["slots"][0]["handle"], "waiting")

    def test_bot_already_pruned_entry_snapshot_uses_private_original_message_id(self):
        messages = [m for m in self.messages() if m["message_id"] != 1]
        self.registry["post_entries"][GROUPS["群一"]].pop("101")
        snapshot.replace_group_snapshot(self.registry, self.state, GROUPS["群一"], messages, DAY, now=NOW)
        self.deliver()
        self.assertEqual(self.record()["slots"][0]["handle"], "waiting")

    def test_parser_failure_keeps_whole_snapshot_untouched(self):
        messages = self.messages()
        messages[-1]["text"] = "https://x.com/new/status/999"
        before = copy.deepcopy((self.registry, self.state))
        with patch("sync_group_messages.extract_x_links_ordered", side_effect=RuntimeError("parse failed")):
            with self.assertRaises(RuntimeError):
                snapshot.replace_group_snapshot(self.registry, self.state, GROUPS["群一"], messages, DAY, now=NOW)
        self.assertEqual((self.registry, self.state), before)

    def test_snapshot_after_cutoff_no_withdrawal_evidence(self):
        messages = [m for m in self.messages() if m["message_id"] != 1]
        snapshot.replace_group_snapshot(self.registry, self.state, GROUPS["群一"], messages, DAY, now=NOW.replace(hour=19))
        self.deliver(NOW.replace(hour=19))
        self.edit.assert_not_called()


class DeletionResponseTest(unittest.TestCase):
    def test_missing_or_mismatched_response_never_confirms_deletion(self):
        for response in ([SimpleNamespace(id=1)], [None, SimpleNamespace(id=3)]):
            client = SimpleNamespace(get_messages=AsyncMock(return_value=response))
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                asyncio.run(deletion._existing_message_ids(client, None, [1, 2]))

    def test_exact_id_query_confirms_only_explicit_absence(self):
        client = SimpleNamespace(get_messages=AsyncMock(return_value=[SimpleNamespace(id=1), None]))
        self.assertEqual(asyncio.run(deletion._existing_message_ids(client, None, [1, 2])), {1})

    def test_incomplete_history_does_not_replace_registry(self):
        class Client:
            async def iter_messages(self, entity, limit):
                for mid in range(limit):
                    async def sender():
                        return SimpleNamespace(id=1)
                    yield SimpleNamespace(id=mid + 1, date=NOW, edit_date=None, raw_text="hello",
                                          get_sender=sender, sender_id=1)
        with patch("sync_group_messages.HISTORY_LIMIT", 2), self.assertRaises(RuntimeError):
            asyncio.run(snapshot._today_messages(Client(), None, NOW.replace(hour=0)))

    def test_undated_message_does_not_prove_complete_history(self):
        class Client:
            async def iter_messages(self, entity, limit):
                yield SimpleNamespace(date=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(snapshot._today_messages(Client(), None, NOW.replace(hour=0)))


if __name__ == "__main__":
    unittest.main()
