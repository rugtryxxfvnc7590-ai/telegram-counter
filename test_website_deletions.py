from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from daily_roster import refresh_daily_rosters
from test_website_roster import fixture, DAY, NOW, CID, LIMITS, withdraw
from website_deletions import capture_website_deletions, deleted_entries, deletion_patches, website_items
from website_sync import sync_website
from withdrawal_sync import note_withdrawals


class WebsiteDeletionTest(unittest.TestCase):
    def resubmission(self, group="群三"):
        return {"daily_rosters": {"date": DAY, "groups": {group: {"slots": [
            {"position": 10, "message_id": "6682", "handle": "yishiyu7788",
             "post_id": "2106395884703617166", "url": "https://x.com/yishiyu7788/status/2106395884703617166"}
        ]}}}, "website_deletions": {"date": DAY, "groups": {group: {"6672": {
            "position": 2, "account": "yishiyu7788", "postId": "2106395884703617166",
            "confirmed_at": DAY + " 00:16:15"
        }}}}}

    def test_deleted_old_message_cannot_remove_same_post_readmitted_by_new_message(self):
        for group in ("群一", "群二", "群三"):
            with self.subTest(group=group):
                state = self.resubmission(group)
                record = state["daily_rosters"]["groups"][group]
                deletions = deleted_entries(state, group, DAY)
                self.assertEqual(website_items(record, deletions)[-1]["url"],
                                 "https://x.com/yishiyu7788/status/2106395884703617166")
                self.assertEqual(deletion_patches(record, deletions), [])
                before = deepcopy(state)
                post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "updated"}))
                for hour in (19, 23):
                    sync_website(state, NOW.replace(hour=hour), secret="test-secret", post=post)
                    payload = json.loads(post.call_args.kwargs["data"])
                    self.assertEqual(payload["deletions"], [])
                    self.assertNotIn("items", payload)
                post.assert_called_once()
                self.assertEqual(state["daily_rosters"], before["daily_rosters"])
                self.assertEqual(state["website_deletions"], before["website_deletions"])

    def test_new_message_also_deleted_still_removes_post_and_retries_are_idempotent(self):
        state = self.resubmission()
        post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "updated"}))
        sync_website(state, NOW.replace(hour=19), secret="test-secret", post=post)
        state["website_deletions"]["groups"]["群三"]["6682"] = {
            "position": 10, "account": "yishiyu7788", "postId": "2106395884703617166",
            "confirmed_at": DAY + " 22:30:41"
        }
        sync_website(state, NOW.replace(hour=22, minute=30, second=43), secret="test-secret", post=post)
        payload = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(len(payload["deletions"]), 2)
        self.assertEqual(payload["sourceRevision"], 2)
        self.assertIn({"position": 10, "account": "yishiyu7788", "postId": "2106395884703617166"}, payload["deletions"])
        sync_website(state, NOW.replace(hour=23), secret="test-secret", post=post)
        self.assertEqual(post.call_count, 2)

    def test_protection_requires_same_group_account_post_and_live_slot(self):
        state = self.resubmission()
        record = state["daily_rosters"]["groups"]["群三"]
        deletions = deleted_entries(state, "群三", DAY)
        for changes in ({"handle": "different", "url": "https://x.com/different/status/2106395884703617166"},
                        {"post_id": "999", "url": "https://x.com/yishiyu7788/status/999"}):
            altered = deepcopy(record)
            altered["slots"][0].update(changes)
            self.assertEqual(len(deletion_patches(altered, deletions)), 1)
        self.assertEqual(len(deletion_patches({"slots": []}, deletions)), 1)
        other = {"42": {"position": 20, "account": "another", "postId": "123", "confirmed_at": DAY + " 18:00:00"}}
        self.assertEqual(deletion_patches(record, {**deletions, **other}),
                         [{"position": 20, "account": "another", "postId": "123"}])

    def test_before_cutoff_candidate_inherits_number_and_deleted_username(self):
        registry, state = fixture()
        withdraw(registry, state, 5)
        record = state["daily_rosters"]["groups"]["群一"]
        item = website_items(record, deleted_entries(state, "群一", DAY))[25]
        self.assertEqual(item["position"], 26)
        self.assertEqual(item["url"], "https://x.com/person31/status/1031")
        self.assertTrue(item["isReplacement"])
        self.assertEqual(item["deletedAccounts"], ["person5"])
        self.assertNotIn("1005", json.dumps(item))
        withdraw(registry, state, 31)
        item = website_items(state["daily_rosters"]["groups"]["群一"], deleted_entries(state, "群一", DAY))[25]
        self.assertIsNone(item["url"])
        self.assertEqual(set(item["deletedAccounts"]), {"person5", "person31"})
        self.assertEqual(item["note"], "链接已删除")

    def test_at_and_after_19_only_patch_deletions_and_never_refill_or_reorder(self):
        for stamp in (NOW.replace(hour=19), NOW.replace(hour=23, minute=59, second=59)):
            with self.subTest(stamp=stamp):
                registry, state = fixture()
                before = deepcopy(state["daily_rosters"])
                registry["post_entries"][CID].pop("1005")
                note_withdrawals(registry, CID, {"5": "message_deleted"}, stamp)
                refresh_daily_rosters(registry, state, LIMITS, stamp)
                self.assertEqual(state["daily_rosters"], before)
                post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "updated"}))
                sync_website(state, stamp, secret="test-secret", post=post)
                payload = json.loads(post.call_args.kwargs["data"])
                self.assertEqual(payload["mode"], "deletions")
                self.assertNotIn("items", payload)
                self.assertEqual(payload["deletions"], [{"position": 26, "account": "person5", "postId": "1005"}])
                sync_website(state, stamp, secret="test-secret", post=post)
                post.assert_called_once()

    def test_185959_still_refills(self):
        registry, state = fixture()
        stamp = NOW.replace(hour=18, minute=59, second=59)
        note_withdrawals(registry, CID, {"5": "message_deleted"}, stamp)
        registry["post_entries"][CID].pop("1005")
        refresh_daily_rosters(registry, state, LIMITS, stamp)
        self.assertEqual(state["daily_rosters"]["groups"]["群一"]["slots"][25]["message_id"], "31")

    def test_no_guessing_from_missing_registry_or_other_group_or_day(self):
        registry, state = fixture()
        registry["post_entries"][CID].pop("1005")
        capture_website_deletions(registry, state, NOW)
        self.assertEqual(deleted_entries(state, "群一", DAY), {})
        note_withdrawals(registry, CID, {"5": "message_deleted"}, NOW)
        capture_website_deletions(registry, state, NOW)
        self.assertEqual(deleted_entries(state, "群二", DAY), {})
        self.assertEqual(deleted_entries(state, "群一", "2026-09-17"), {})
        registry["confirmed_withdrawals"]["date"] = "2026-09-15"
        state.pop("website_deletions")
        capture_website_deletions(registry, state, NOW)
        self.assertNotIn("website_deletions", state)

    def test_waitlisted_message_deletion_cannot_remove_admitted_slot(self):
        registry, state = fixture()
        note_withdrawals(registry, CID, {"31": "message_deleted"}, NOW)
        capture_website_deletions(registry, state, NOW)
        self.assertEqual(deleted_entries(state, "群一", DAY), {})

    def test_frozen_payload_never_contains_new_links_even_when_crossing_boundary(self):
        registry, state = fixture()
        withdraw(registry, state, 5)
        post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "updated"}))
        sync_website(state, NOW.replace(hour=19), secret="test-secret", post=post)
        body = post.call_args.kwargs["data"]
        self.assertNotIn(b"https://x.com/", body)


if __name__ == "__main__":
    unittest.main()
