from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from daily_roster import refresh_daily_rosters
from test_website_roster import fixture, DAY, NOW, CID, LIMITS, withdraw
from website_deletions import capture_website_deletions, deleted_entries, website_items
from website_sync import sync_website
from withdrawal_sync import note_withdrawals


class WebsiteDeletionTest(unittest.TestCase):
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
