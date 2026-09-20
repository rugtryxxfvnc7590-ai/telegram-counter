from copy import deepcopy
from datetime import datetime
import hashlib
import hmac
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from daily_capacity import GROUP_CHAT_IDS
from daily_roster import refresh_daily_rosters, roster_items
from replacement_delivery import process_replacement_replies, recover_replacement_replies, RULE
from violation_delivery import receipt_run_key
from website_sync import sync_website
from withdrawal_sync import note_withdrawals

DAY = "2026-09-16"
NOW = datetime(2026, 9, 16, 12, tzinfo=main.BEIJING)
SYNC_NOW = NOW.replace(hour=14)
CID = GROUP_CHAT_IDS["群一"]
LIMITS = {group: 30 for group in GROUP_CHAT_IDS}


def entry(n):
    return {"promo_handle": f"person{n}", "promo_post_id": str(1000+n), "message_id": n,
            "tg_user_id": n+100, "time": f"{DAY} 01:{n:02d}:00", "mutual_eligible": True}


def fixture(count=31):
    registry = {"date": DAY, "post_entries": {CID: {str(1000+n): entry(n) for n in range(1, count+1)}}}
    state = {"date": DAY, "group_snapshot_sync": {"date": DAY, "completed_groups": ["群一"],
             "violation_receipt_groups": {"群一": {"run_key": receipt_run_key(), "bot_user_id": 123,
                                                    "checked_at": f"{DAY} 12:00:00"}}}}
    refresh_daily_rosters(registry, state, LIMITS, NOW)
    return registry, state


def withdraw(registry, state, n):
    registry["post_entries"][CID].pop(str(1000+n))
    note_withdrawals(registry, CID, {str(n): "message_deleted"}, NOW)
    refresh_daily_rosters(registry, state, LIMITS, NOW)


def edit_link(registry, n, new_post_id):
    updated = registry["post_entries"][CID].pop(str(1000+n))
    updated.update(promo_post_id=str(new_post_id), edited=True, edit_time=f"{DAY} 12:00:00")
    registry["post_entries"][CID][str(new_post_id)] = updated


class DailyRosterTest(unittest.TestCase):
    def test_edit_keeps_member_position_and_is_not_a_replacement(self):
        registry, state = fixture()
        edit_link(registry, 5, 9005)
        refresh_daily_rosters(registry, state, LIMITS, NOW)
        item = roster_items(state["daily_rosters"]["groups"]["群一"])[25]
        self.assertEqual(item, {"position": 26, "url": "https://x.com/person5/status/9005",
                                "note": "已编辑", "isReplacement": False})
        self.assertEqual(len(state["daily_rosters"]["groups"]["群一"]["slots"]), 30)

    def test_edit_of_true_candidate_retains_its_candidate_identity(self):
        registry, state = fixture()
        withdraw(registry, state, 5)
        edit_link(registry, 31, 9031)
        refresh_daily_rosters(registry, state, LIMITS, NOW)
        item = roster_items(state["daily_rosters"]["groups"]["群一"])[25]
        self.assertEqual(item, {"position": 26, "url": "https://x.com/person31/status/9031",
                                "note": "已编辑", "isReplacement": True})

    def test_slot_26_kept_with_first_candidate_and_other_slots_unchanged(self):
        registry, state = fixture()
        before = deepcopy(state["daily_rosters"]["groups"]["群一"])
        self.assertNotIn("owner_daily_lists", state)
        self.assertEqual(len(before["slots"]), 30)
        withdraw(registry, state, 5)
        after = state["daily_rosters"]["groups"]["群一"]
        for old, new in zip(before["slots"], after["slots"]):
            if old["position"] != 26:
                self.assertEqual(old, new)
            else:
                self.assertEqual(new["message_id"], "31")
                self.assertEqual(new["position"], 26)
                self.assertTrue(new["is_replacement"])
        text = main.format_daily_list_message("群一", DAY, [s["url"] for s in after["slots"]], slots=after["slots"])
        self.assertIn("候补26 https://x.com/person31/status/1031", text)
        self.assertTrue(roster_items(after)[25]["isReplacement"])

    def test_early_arrivals_append_without_reordering_or_making_false_vacancies(self):
        registry, state = fixture(0)
        for n in (1, 2, 3):
            registry["post_entries"][CID][str(1000+n)] = entry(n)
            refresh_daily_rosters(registry, state, LIMITS, NOW)
        roster = state["daily_rosters"]["groups"]["群一"]
        self.assertEqual([s["message_id"] for s in roster["slots"]], ["1", "2", "3"])
        self.assertEqual(roster["vacant_positions"], {})
        withdraw(registry, state, 2)
        self.assertEqual(roster_items(state["daily_rosters"]["groups"]["群一"])[1]["url"], None)
        registry["post_entries"][CID]["1004"] = entry(4)
        refresh_daily_rosters(registry, state, LIMITS, NOW)
        roster = state["daily_rosters"]["groups"]["群一"]
        self.assertEqual([s["message_id"] for s in roster["slots"]], ["1", "4", "3"])
        self.assertTrue(roster["slots"][1]["is_replacement"])

    def test_freeze_and_stale_snapshot_preserve_roster(self):
        registry, state = fixture()
        before = deepcopy(state["daily_rosters"])
        registry["post_entries"][CID].pop("1005")
        note_withdrawals(registry, CID, {"5": "deleted"}, NOW)
        refresh_daily_rosters(registry, state, LIMITS, NOW.replace(hour=19))
        self.assertEqual(state["daily_rosters"], before)
        state["group_snapshot_sync"]["completed_groups"] = []
        refresh_daily_rosters(registry, state, LIMITS, NOW)
        self.assertEqual(state["daily_rosters"], before)

    def test_migration_keeps_existing_owner_order(self):
        registry, state = fixture(3)
        slots = deepcopy(state["daily_rosters"]["groups"]["群一"]["slots"])
        state.pop("daily_rosters")
        state["owner_daily_lists"] = {"date": DAY, "groups": {"群一": {"sent": True, "slots": slots,
                                                                                "count": 3}}}
        refresh_daily_rosters(registry, state, LIMITS, NOW)
        self.assertEqual(state["daily_rosters"]["groups"]["群一"]["slots"], slots)

    def test_first_private_cutoff_delivery_includes_late_arrival_without_website_mutation(self):
        registry, state = fixture(3)
        before = deepcopy(state["daily_rosters"])
        for n, stamp in ((4, "18:50:00"), (5, "19:00:00")):
            row = entry(n)
            row["time"] = f"{DAY} {stamp}"
            registry["post_entries"][CID][str(1000+n)] = row
        sender = Mock(return_value=(True, {"message_id": 999}))
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}), patch(
                "main._send_private_message", sender):
            result = main.send_daily_lists_to_owner(registry, state, now=NOW.replace(hour=19), limits=LIMITS)
        self.assertEqual(result["群一"], "sent")
        slots = state["owner_daily_lists"]["groups"]["群一"]["slots"]
        self.assertEqual([s["message_id"] for s in slots], ["3", "2", "1", "4"])
        self.assertEqual([s["position"] for s in slots], [1, 2, 3, 4])
        self.assertEqual(slots[:3], before["groups"]["群一"]["slots"])
        self.assertEqual(state["daily_rosters"], before)
        text = sender.call_args.args[1]
        self.assertIn("4 https://x.com/person4/status/1004", text)
        self.assertNotIn("status/1005", text)
        website = Mock()
        self.assertEqual(sync_website(state, NOW.replace(hour=19), secret="test", post=website), {})
        website.assert_not_called()


class WebsiteSyncTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.state = fixture()
        self.post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"status": "updated"}))

    def send(self, **kwargs):
        return sync_website(self.state, now=kwargs.pop("now", SYNC_NOW), secret="unit-test-secret",
                            post=self.post, **kwargs)

    def test_signature_matches_raw_body_and_no_redundant_upload(self):
        self.assertEqual(self.send()["群一"], "updated")
        url = self.post.call_args.args[0]
        body, headers = (self.post.call_args.kwargs[key] for key in ("data", "headers"))
        payload = json.loads(body)
        self.assertEqual(payload["sourceRevision"], 1)
        self.assertEqual(payload["date"], DAY)
        self.assertEqual(len(payload["items"]), 30)
        canonical = "\n".join(["POST", "/api/sync/groups/group-1", str(int(SYNC_NOW.timestamp())),
                                 headers["X-Sync-Request-Id"], hashlib.sha256(body).hexdigest()])
        self.assertEqual(headers["X-Sync-Signature"], hmac.new(b"unit-test-secret", canonical.encode(), hashlib.sha256).hexdigest())
        self.assertTrue(url.endswith("/api/sync/groups/group-1"))
        self.assertEqual(self.send()["群一"], "unchanged")
        self.post.assert_called_once()
        withdraw(self.registry, self.state, 5)
        self.send()
        self.assertEqual(json.loads(self.post.call_args.kwargs["data"])["sourceRevision"], 2)

    def test_timeout_retry_retains_revision_and_request_id_across_json_roundtrip(self):
        self.post.side_effect = TimeoutError
        self.send()
        first = self.post.call_args.kwargs
        self.state = json.loads(json.dumps(self.state))
        self.post.side_effect = None
        self.send(now=SYNC_NOW.replace(minute=15))
        retry = self.post.call_args.kwargs
        self.assertEqual(first["data"], retry["data"])
        self.assertEqual(first["headers"]["X-Sync-Request-Id"], retry["headers"]["X-Sync-Request-Id"])
        self.assertNotEqual(first["headers"]["X-Sync-Timestamp"], retry["headers"]["X-Sync-Timestamp"])
        self.assertTrue(self.state["website_sync"]["groups"]["群一"]["acknowledged"])

    def test_frozen_http_and_stale_response_never_acknowledged(self):
        for code in (423, 409, 500):
            self.post.return_value = SimpleNamespace(status_code=code, json=lambda: {"reason": "frozen"})
            self.send()
            self.assertNotIn("acknowledged", self.state["website_sync"]["groups"]["群一"])
        self.assertEqual(self.state["website_sync"]["groups"]["群一"]["sourceRevision"], 1)

    def test_boundaries_and_previous_day_do_not_send(self):
        before = deepcopy(self.state)
        for stamp in (NOW.replace(hour=0), NOW.replace(hour=13, minute=59, second=59),
                      NOW.replace(hour=19), NOW.replace(hour=23, minute=59, second=59),
                      NOW.replace(day=17, hour=0)):
            self.assertEqual(self.send(now=stamp), {})
        self.post.assert_not_called()
        self.assertEqual(self.state, before)
        self.send(now=NOW.replace(hour=18, minute=59, second=59))
        self.post.assert_called_once()

    def test_exact_14_opens_window_and_new_day_waits_until_14(self):
        self.assertEqual(self.send(now=SYNC_NOW)["群一"], "updated")
        self.post.reset_mock()
        self.state["daily_rosters"]["date"] = "2026-09-17"
        ledger = deepcopy(self.state["website_sync"])
        self.assertEqual(self.send(now=SYNC_NOW.replace(day=17, hour=13, minute=59, second=59)), {})
        self.assertEqual(self.state["website_sync"], ledger)
        self.post.assert_not_called()
        self.assertEqual(self.send(now=SYNC_NOW.replace(day=17))["群一"], "updated")
        payload = json.loads(self.post.call_args.kwargs["data"])
        self.assertEqual(payload["date"], "2026-09-17")
        self.assertEqual(payload["sourceRevision"], 1)

    def test_crossing_cutoff_stops_before_next_group(self):
        self.state["daily_rosters"]["groups"]["群二"] = deepcopy(self.state["daily_rosters"]["groups"]["群一"])
        before, after = NOW.replace(hour=18, minute=59, second=59), NOW.replace(hour=19)
        with patch("website_sync.beijing_now", side_effect=[before, before, after]):
            sync_website(self.state, secret="unit-test-secret", post=self.post)
        self.post.assert_called_once()


class ReplacementReplyTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.state = fixture()
        withdraw(self.registry, self.state, 5)
        self.sender = Mock(return_value=True)
        self.rules = main.load_reply_rules()

    def process(self, **kwargs):
        return process_replacement_replies(self.state, now=NOW, rules=self.rules,
                                           send_reply=self.sender, **kwargs)

    def test_custom_reply_targets_candidate_original_message_once(self):
        self.rules[RULE]["text"] = "自定义补位成功"
        saved = []
        self.process(save_callback=lambda: saved.append(deepcopy(self.state)))
        self.sender.assert_called_once_with(CID, 31, "自定义补位成功")
        self.assertEqual(saved[0]["replacement_replies"]["groups"]["群一"]["31"]["status"], "pending")
        self.state = json.loads(json.dumps(self.state))
        self.process()
        self.sender.assert_called_once()

    def test_failed_send_retries_after_history_then_suppresses_duplicates(self):
        self.sender.return_value = False
        self.process()
        self.assertEqual(self.state["replacement_replies"]["groups"]["群一"]["31"]["status"], "failed")
        self.sender.return_value = True
        self.process()
        self.process()
        self.assertEqual(self.sender.call_count, 2)

    def test_lost_ack_recovered_only_from_our_bot_reply(self):
        message = {"message_id": 900, "date": int(NOW.timestamp()), "reply_to_message_id": 31,
                   "from": {"id": 123, "is_bot": True}, "text": main.DEFAULT_REPLY_RULES[RULE]["text"]}
        self.assertEqual(recover_replacement_replies(self.state, CID, [message], 999, self.rules, NOW), 0)
        self.assertEqual(recover_replacement_replies(self.state, CID, [message], 123, self.rules, NOW), 1)
        self.process()
        self.sender.assert_not_called()

    def test_pre_cutoff_admission_notification_can_retry_after_19(self):
        self.sender.return_value = False
        self.process()
        self.sender.return_value = True
        self.state["group_snapshot_sync"]["violation_receipt_groups"]["群一"]["checked_at"] = f"{DAY} 19:15:00"
        process_replacement_replies(self.state, NOW.replace(hour=19, minute=15), send_reply=self.sender)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.state["replacement_replies"]["groups"]["群一"]["31"]["status"], "sent")

    def test_disabled_rule_unverified_history_and_next_day_do_not_reply(self):
        self.rules[RULE]["enabled"] = False
        self.process()
        self.rules[RULE]["enabled"] = True
        process_replacement_replies(self.state, NOW.replace(day=17, hour=0), send_reply=self.sender)
        self.state["group_snapshot_sync"]["violation_receipt_groups"] = {}
        self.process()
        self.sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
