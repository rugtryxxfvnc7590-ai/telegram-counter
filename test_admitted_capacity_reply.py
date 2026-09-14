from copy import deepcopy
from datetime import datetime
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", ModuleType("requests"))
import main
from capacity_delivery import process_capacity_replies
from daily_capacity import chat_ids_for_group, eligible_rows
from private_list_sync import freeze_links
from test_daily_capacity import DAY, ready_state, registry_for, test_rules


class AdmittedCapacityReplyTests(unittest.TestCase):
    def setUp(self):
        self.registry = registry_for({"群一": 30})
        self.state = ready_state()
        self.limits = {"群一": 30, "群二": 30, "群三": 30}
        self.group = main.GROUP_1_CHAT_ID_FALLBACK
        self.now = datetime(2026, 9, 7, 18, tzinfo=main.BEIJING)
        links = main.daily_eligible_links(self.registry, self.group, limits=self.limits)
        self.record = {"sent": True, "count": 30, "message_id": 500, "owner_chat_id": "123",
                       "links": links, "text": main.format_daily_list_message("群一", DAY, links),
                       "slots": freeze_links(links, self.registry, chat_ids_for_group("群一"), DAY)}
        self.state["owner_daily_lists"] = {"date": DAY, "groups": {"群一": self.record}}
        self.state["capacity_replies"] = {"date": DAY, "groups": {"群一": {
            "full": {"message_id": 30, "rule": "limit_full"}}}}
        self.patch = patch("capacity_delivery.send_capacity_reply", return_value=True)
        self.sender = self.patch.start()
        self.addCleanup(self.patch.stop)

    def earlier_links(self, count=2):
        bucket = self.registry["post_entries"][self.group]
        for i in range(count):
            mid = 9000 + i
            bucket[str(mid)] = {"promo_handle": f"early{mid}", "promo_post_id": str(mid),
                                "message_id": mid, "time": DAY + f" 07:00:{i:02d}",
                                "mutual_eligible": True, "chat_id": self.group}

    def run_replies(self):
        return process_capacity_replies(self.registry, self.state, self.now, self.limits, test_rules())

    def test_rank_29_becomes_31_but_published_member_not_warned(self):
        self.earlier_links()
        rows = eligible_rows(self.registry, {self.group}, DAY)
        self.assertEqual(next(i for i, row in enumerate(rows, 1) if row["message_id"] == 29), 31)
        before_record, before_registry = deepcopy(self.record), deepcopy(self.registry)
        for _ in range(10):
            self.assertEqual(self.run_replies(), {})
        self.sender.assert_not_called()
        self.assertEqual(self.record, before_record)
        self.assertEqual(self.registry, before_registry)

    def test_all_candidate_stages_are_suppressed_for_published_member(self):
        for count in (4, 7, 10, 11):
            with self.subTest(earlier_count=count):
                self.earlier_links(count)
                self.run_replies()
        self.sender.assert_not_called()

    def test_same_original_message_changed_post_is_still_protected(self):
        self.earlier_links()
        old = self.registry["post_entries"][self.group].pop("29")
        self.registry["post_entries"][self.group]["2900"] = dict(old, promo_post_id="2900")
        self.run_replies()
        self.sender.assert_not_called()

    def test_same_account_new_message_is_not_mistaken_for_admitted_message(self):
        new = dict(self.registry["post_entries"][self.group]["29"],
                   promo_post_id="3100", message_id=31, time=DAY + " 09:00:00")
        self.registry["post_entries"][self.group]["3100"] = new
        self.run_replies()
        self.assertEqual([call.args[1] for call in self.sender.call_args_list], [31])
        self.assertTrue(self.sender.call_args.args[2].startswith("limit_overflow"))

    def test_first_full_announcement_is_not_removed(self):
        self.state.pop("capacity_replies")
        self.run_replies()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], 30)
        self.assertTrue(self.sender.call_args.args[2].startswith("limit_full"))

    def test_unsent_wrong_day_or_inconsistent_list_is_not_an_admission(self):
        self.earlier_links()
        before = deepcopy(self.state)
        for change in ("unsent", "wrong_day", "wrong_group", "unacknowledged", "mismatched_text"):
            with self.subTest(change=change):
                self.state = deepcopy(before)
                record = self.state["owner_daily_lists"]["groups"]["群一"]
                if change == "unsent":
                    record["sent"] = False
                elif change == "wrong_day":
                    self.state["owner_daily_lists"]["date"] = "2026-09-06"
                elif change == "wrong_group":
                    self.state["owner_daily_lists"]["groups"] = {"群二": record}
                elif change == "unacknowledged":
                    record.pop("message_id")
                else:
                    record["text"] = "unsynchronized contents"
                self.sender.reset_mock()
                self.run_replies()
                # Message 30 already received the original full-capacity notice.
                self.assertEqual([call.args[1] for call in self.sender.call_args_list], [29])

    def test_pending_refill_not_yet_published_is_still_candidate(self):
        new = dict(self.registry["post_entries"][self.group]["29"],
                   promo_post_id="3100", message_id=31, time=DAY + " 09:00:00")
        self.registry["post_entries"][self.group]["3100"] = new
        pending = freeze_links(["https://x.com/user29/status/3100"], self.registry, {self.group}, DAY)
        self.record["pending_roster"] = {"slots": pending}
        self.run_replies()
        self.assertEqual([call.args[1] for call in self.sender.call_args_list], [31])

    def test_shared_guard_applies_to_each_group_without_cross_group_matching(self):
        for group, chat_id in main.canonical_group_chat_ids():
            with self.subTest(group=group):
                self.earlier_links()
                registry = deepcopy(self.registry)
                registry["post_entries"] = {chat_id: registry["post_entries"][self.group]}
                state = deepcopy(self.state)
                record = state["owner_daily_lists"]["groups"]["群一"]
                record["text"] = main.format_daily_list_message(group, DAY, record["links"])
                state["owner_daily_lists"]["groups"] = {group: record}
                state["capacity_replies"]["groups"] = {group: {"full": {"message_id": 30, "rule": "limit_full"}}}
                process_capacity_replies(registry, state, self.now, self.limits, test_rules())
        self.sender.assert_not_called()

    def test_real_candidates_keep_same_stage_rules_and_success_dedup(self):
        rows = registry_for({"群一": 39})["post_entries"][self.group]
        self.registry["post_entries"][self.group].update(rows)
        self.run_replies()
        first = list(self.sender.call_args_list)
        self.assertEqual([call.args[1] for call in first], list(range(31, 40)))
        self.assertTrue(first[2].args[2].startswith("limit_excess_1"))
        self.assertTrue(first[5].args[2].startswith("limit_excess_2"))
        self.assertTrue(first[8].args[2].startswith("limit_excess_3"))
        self.run_replies()
        self.assertEqual(self.sender.call_args_list, first)


if __name__ == "__main__":
    unittest.main()
