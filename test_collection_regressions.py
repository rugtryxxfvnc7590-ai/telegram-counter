"""Deleted admissions, new-message requeue, and full-text provider routing."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from test_batch_rosters import CID, DAY, NOW, fixture, roster
from withdrawal_sync import note_withdrawals


class RepostedMessageTests(unittest.TestCase):
    def setUp(self):
        policy = patch("batch_rosters.load_batch_config", return_value={"群一": {
            "enabled": True, "effective_from": DAY, "size": 30, "max_batches": 2}})
        policy.start()
        self.addCleanup(policy.stop)

    def add_new_message(self, registry, n=5, mid=90, post_id="99005", hour=10, eligible=True):
        row = deepcopy(registry["post_entries"][CID][str(10000 + n)])
        row.update(message_id=mid, promo_post_id=post_id, time=f"{DAY} {hour:02d}:00:00",
                   mutual_eligible=eligible)
        registry["post_entries"][CID][post_id] = row
        return row

    def test_deleted_member_can_return_with_new_message_and_not_old_number(self):
        reg, state = fixture(31)
        old = deepcopy(roster(reg, state))
        old_slot = next(s for s in old["slots"] if s["handle"] == "user5")
        new_entry = self.add_new_message(reg)
        reg["post_entries"][CID].pop("10005")
        note_withdrawals(reg, CID, {"5": "deleted"}, NOW)
        current = roster(reg, state)
        new_slot = next(s for s in current["slots"] if s["handle"] == "user5")
        self.assertEqual(new_slot["post_id"], new_entry["promo_post_id"])
        self.assertEqual(new_slot["list_id"], "B")
        self.assertNotEqual(new_slot["position"], old_slot["position"])
        self.assertIn(str(old_slot["position"]), current["vacant_positions"])
        self.assertIn("5", current["withdrawn_message_ids"])
        for _ in range(10):
            state = json.loads(json.dumps(state))
            self.assertEqual(roster(reg, state), current)

    def test_legacy_stale_assignment_does_not_block_return(self):
        reg, state = fixture(31)
        old = roster(reg, state)
        old["batches"]["A"]["slots"] = [s for s in old["batches"]["A"]["slots"] if s["handle"] != "user5"]
        old["withdrawn_message_ids"] = ["5"]
        self.add_new_message(reg)
        current = roster(reg, state)
        self.assertEqual([s["post_id"] for s in current["slots"] if s["handle"] == "user5"], ["99005"])

    def test_latest_qualified_new_message_requeues_and_next_member_fills_old_slot(self):
        reg, state = fixture(60)
        old = deepcopy(roster(reg, state))
        old_slot = next(s for s in old["slots"] if s["handle"] == "user5")
        extra, _ = fixture(61)
        reg["post_entries"][CID]["10061"] = extra["post_entries"][CID]["10061"]
        self.add_new_message(reg)
        current = roster(reg, state)
        replacement = next(s for s in current["slots"] if s["handle"] == "user61")
        self.assertEqual((replacement["position"], replacement["list_id"]), (old_slot["position"], "A"))
        self.assertNotIn("user5", current["assigned_accounts"])
        self.assertEqual(current["requeued_message_ids"], ["5"])
        self.assertEqual(current["batches"]["B"], old["batches"]["B"])

    def test_latest_new_message_uses_new_time_and_does_not_change_other_members(self):
        reg, state = fixture(32)
        old = deepcopy(roster(reg, state))
        extra, _ = fixture(33)
        reg["post_entries"][CID]["10033"] = extra["post_entries"][CID]["10033"]
        self.add_new_message(reg, post_id="99004", hour=9)
        latest = self.add_new_message(reg, mid=91, hour=10)
        current = roster(reg, state)
        slots = [s for s in current["slots"] if s["handle"] == "user5"]
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0]["post_id"], slots[0]["time"], slots[0]["list_id"]),
                         ("99005", latest["time"], "B"))
        self.assertEqual(current["batches"]["B"]["slots"][0]["handle"], "user5")
        self.assertEqual(current["assigned_accounts"]["user31"], old["assigned_accounts"]["user31"])
        self.assertEqual(current["assigned_accounts"]["user32"], old["assigned_accounts"]["user32"])
        for _ in range(10):
            self.assertEqual(roster(reg, state), current)

    def test_bad_or_after_cutoff_new_message_does_not_remove_original(self):
        for hour, eligible in ((10, False), (19, True)):
            reg, state = fixture(31)
            old = deepcopy(roster(reg, state))
            self.add_new_message(reg, hour=hour, eligible=eligible)
            self.assertEqual(roster(reg, state), old)

    def test_new_message_from_different_sender_cannot_replace_member(self):
        reg, state = fixture(31)
        old = deepcopy(roster(reg, state))
        self.add_new_message(reg)["tg_user_id"] = 9999
        self.assertEqual(roster(reg, state), old)

    def test_finalized_roster_does_not_requeue_new_message(self):
        reg, state = fixture(31)
        roster(reg, state, NOW.replace(hour=19))
        old = deepcopy(state["daily_rosters"]["groups"]["群一"])
        self.add_new_message(reg)
        self.assertEqual(roster(reg, state, NOW.replace(hour=20)), old)


class FullTextRoutingTests(unittest.TestCase):
    def setUp(self):
        main._x_author_meta_cache.clear()
        self.addCleanup(main._x_author_meta_cache.clear)

    def response(self, payload):
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def fetch(self, handle="member", vx_id="123", vx_handle="member"):
        partial = "Long post preview " * 12
        complete = partial + "\n@ToBulaer\n@ToBuerma\n@KawasawaSen"
        calls = []

        def get(url, timeout=8):
            calls.append(url)
            if "api.vxtwitter.com" in url:
                self.assertEqual(url.lower(), "https://api.vxtwitter.com/member/status/123")
                return self.response({"tweetID": vx_id, "user_screen_name": vx_handle,
                                      "user_name": "Member", "text": complete})
            return self.response({"status": {"id": "123", "is_note_tweet": False,
                "author": {"screen_name": "member", "followers": 200000}, "text": partial}})

        with patch.object(main.requests, "get", side_effect=get, create=True):
            meta = main.fetch_x_author_meta(handle=handle, post_id="123")
        return meta, complete, calls

    def test_real_provider_shape_reads_hidden_mentions_from_correct_route(self):
        meta, complete, calls = self.fetch()
        self.assertEqual(meta["tweet_text"], complete)
        self.assertEqual(main.count_required_mentions(meta["tweet_text"]), 3)
        self.assertIn("vx_status_author", meta["tweet_text_sources"])
        self.assertEqual(len([url for url in calls if "api.vxtwitter.com" in url]), 1)
        self.assertTrue(main.promo_link_content_eligible([dict(meta, role="promo")]))

    def test_i_status_resolves_author_before_full_text_request(self):
        meta, complete, _ = self.fetch(handle="")
        self.assertEqual(meta["tweet_text"], complete)

    def test_wrong_post_or_author_does_not_supply_mentions(self):
        for post_id, handle in (("456", "member"), ("123", "other")):
            main._x_author_meta_cache.clear()
            meta, _, _ = self.fetch(vx_id=post_id, vx_handle=handle)
            self.assertEqual(main.count_required_mentions(meta["tweet_text"]), 0)
            self.assertIsNone(main.promo_link_content_eligible([dict(meta, role="promo")]))


if __name__ == "__main__":
    unittest.main()
