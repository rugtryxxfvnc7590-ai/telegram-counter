import copy
import json
import random
import unittest
import sys
from datetime import datetime
from types import ModuleType
from unittest.mock import patch

sys.modules.setdefault("requests", ModuleType("requests"))
import main
import capacity_delivery
from daily_capacity import (
    admitted_rows, capacity_rule_for_rank, capacity_template, chat_ids_for_group,
    eligible_rows, normalize_daily_limits, render_capacity_text, stamp_admission,
)


DAY = "2026-09-07"


def registry_for(counts):
    groups = {}
    for group, count in counts.items():
        cid = dict(main.canonical_group_chat_ids())[group]
        groups[cid] = {
            str(i): {"promo_handle": f"user{i}", "promo_post_id": str(i), "message_id": i,
                     "time": f"{DAY} 08:{i // 60:02d}:{i % 60:02d}", "mutual_eligible": True,
                     "chat_id": cid, "after_cutoff": False}
            for i in range(1, count + 1)
        }
    return {"date": DAY, "post_entries": groups}


def ready_state():
    return {"group_snapshot_sync": {"date": DAY, "completed_groups": ["群一", "群二", "群三"]}}


def test_rules():
    return {key: {"enabled": True, "groups": ["群一", "群二", "群三"],
                  "text": key + " {group} 名额{limit} 当前{count} 候选{overflow_count}"}
            for key in ("limit_full", "limit_overflow", "limit_excess_1", "limit_excess_2", "limit_excess_3")}


class CapacityPolicyTest(unittest.TestCase):
    def test_default_delivery_reads_dashboard_limits_without_real_config(self):
        registry = registry_for({"群一": 43, "群二": 43, "群三": 43})
        now = datetime(2026, 9, 7, 18, tzinfo=main.BEIJING)
        for cap in (0, 1, 30, 40, 500):
            limits = {group: cap for group, _ in main.canonical_group_chat_ids()}
            with self.subTest(cap=cap), patch("main.Path.read_text", return_value=json.dumps({"daily_list_limits": limits})), patch(
                "capacity_delivery.send_capacity_reply", return_value=True
            ) as send:
                self.assertEqual(main.load_daily_limits(), limits)
                for group, chat_id in main.canonical_group_chat_ids():
                    links = main.daily_eligible_links(registry, chat_id)
                    self.assertEqual(len(links), min(cap, 43) if cap else 43)
                capacity_delivery.process_capacity_replies(registry, ready_state(), now, rules=test_rules())
                self.assertEqual(send.call_count, 3 * max(44 - cap, 0) if cap else 0)

    def test_limits_validate_integer_and_support_unlimited(self):
        self.assertEqual(normalize_daily_limits({"群一": 30, "群二": "40", "群三": 0}),
                         {"群一": 30, "群二": 40, "群三": 0})
        for invalid in (-1, "abc", 4.5, True, 501, None):
            self.assertEqual(normalize_daily_limits({"群一": invalid})["群一"], 40)

    def test_thresholds_follow_limit_every_three(self):
        for limit in (30, 40):
            self.assertEqual(capacity_rule_for_rank(limit - 1, limit), "")
            self.assertEqual(capacity_rule_for_rank(limit, limit), "limit_full")
            for extra in (1, 2, 4, 5, 7, 8, 10):
                self.assertEqual(capacity_rule_for_rank(limit + extra, limit), "limit_overflow")
            for extra, stage in ((3, 1), (6, 2), (9, 3), (12, 3)):
                self.assertEqual(capacity_rule_for_rank(limit + extra, limit), f"limit_excess_{stage}")
        self.assertEqual(capacity_rule_for_rank(99, 0), "")

    def test_only_old_quota_phrases_are_migrated(self):
        text = capacity_template("今日40条，40个名额，前40名，联系 @a40，19:00")
        self.assertEqual(render_capacity_text(text, "群一", 30, 33),
                         "今日30条，30个名额，前30名，联系 @a40，19:00")
        self.assertEqual(render_capacity_text("{group}:{limit}/{count}/{overflow_count}", "群二", 40, 43), "群二:40/43/3")

    def test_first_slots_selected_before_reverse_display(self):
        registry = registry_for({"群一": 45, "群二": 45, "群三": 5})
        limits = {"群一": 30, "群二": 40, "群三": 0}
        for group, cid in main.canonical_group_chat_ids():
            links = main.daily_eligible_links(registry, cid, limits)
            count = {"群一": 30, "群二": 40, "群三": 5}[group]
            self.assertEqual(len(links), count)
            self.assertEqual(links[0], f"https://x.com/user{count}/status/{count}")
            self.assertEqual(links[-1], "https://x.com/user1/status/1")

    def test_excludes_invalid_late_return_links_and_prior_day(self):
        registry = registry_for({"群二": 7})
        bucket = registry["post_entries"][main.GROUP_2_CHAT_ID_FALLBACK]
        bucket["2"]["mutual_eligible"] = False
        bucket["3"]["mutual_eligible"] = None
        bucket["4"]["after_cutoff"] = True
        bucket["5"]["time"] = "2026-09-06 18:00:00"
        bucket["6"]["time"] = DAY + " 19:00:00"
        bucket["999"] = dict(bucket["1"], promo_post_id="1", check_handle="return_account")
        accepted, _ = admitted_rows(registry, chat_ids_for_group("群二"), 40)
        self.assertEqual([row["post_id"] for row in accepted], ["1", "7"])

    def test_ten_shuffles_duplicates_and_alias_ids_are_deterministic(self):
        registry = registry_for({"群一": 50})
        cid = main.GROUP_1_CHAT_ID_FALLBACK
        original = list(registry["post_entries"][cid].items())
        expected = None
        for seed in range(10):
            rows = original[:]
            random.Random(seed).shuffle(rows)
            registry["post_entries"][cid] = dict(rows)
            registry["post_entries"]["-3891628675"] = dict(rows)
            result = main.daily_eligible_links(registry, cid, {"群一": 40})
            expected = expected or result
            self.assertEqual(result, expected)
            self.assertEqual(len(result), 40)

    def test_stamp_keeps_eligibility_and_deleted_slot_can_be_replaced(self):
        registry = registry_for({"群三": 4})
        limits = normalize_daily_limits({"群三": 3})
        stamp_admission(registry, limits)
        bucket = registry["post_entries"][main.GROUP_3_CHAT_ID_FALLBACK]
        self.assertEqual(bucket["4"]["daily_list_status"], "waitlist")
        self.assertIs(bucket["4"]["mutual_eligible"], True)
        del bucket["2"]
        stamp_admission(registry, limits)
        self.assertEqual(bucket["4"]["daily_list_status"], "accepted")
        self.assertEqual(bucket["4"]["daily_list_rank"], 3)


class CapacityDeliveryTest(unittest.TestCase):
    def setUp(self):
        # Delivery fixtures use 40 slots, independently of live dashboard settings.
        limits = patch("main.load_daily_limits", return_value={"群一": 40, "群二": 40, "群三": 40})
        limits.start()
        self.addCleanup(limits.stop)

    def test_full_group_sent_before_19_other_groups_wait_and_no_repeat(self):
        registry = registry_for({"群一": 42, "群二": 3, "群三": 5})
        limits = {"群一": 40, "群二": 30, "群三": 0}
        state = ready_state()
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}), patch("main._send_private_message", return_value=(True, "")) as send:
            first = main.send_daily_lists_to_owner(registry, state, datetime(2026, 9, 7, 10, tzinfo=main.BEIJING), limits=limits)
            second = main.send_daily_lists_to_owner(registry, state, datetime(2026, 9, 7, 19, tzinfo=main.BEIJING), limits=limits)
        self.assertEqual(first, {"群一": "sent", "群二": "waiting_for_limit_or_cutoff", "群三": "waiting_for_limit_or_cutoff"})
        self.assertEqual(second, {"群一": "already_sent", "群二": "sent", "群三": "sent"})
        self.assertEqual(len(send.call_args_list), 3)
        self.assertTrue(all(call.args[0] == "8614747348" for call in send.call_args_list))
        self.assertEqual(state["owner_daily_lists"]["groups"]["群一"]["trigger"], "capacity_full")
        self.assertNotIn("status/41", send.call_args_list[0].args[1])

    def test_full_group_waits_for_snapshot_and_retries_failed_private_send(self):
        registry = registry_for({"群一": 2})
        limits = {"群一": 2, "群二": 40, "群三": 40}
        now = datetime(2026, 9, 7, 9, tzinfo=main.BEIJING)
        state = {}
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "8614747348"}), patch("main._send_private_message", return_value=(False, "temporary")) as send:
            self.assertEqual(main.send_daily_lists_to_owner(registry, state, now, limits=limits)["群一"], "waiting_for_snapshot")
            send.assert_not_called()
            state.update(ready_state())
            self.assertEqual(main.send_daily_lists_to_owner(registry, state, now, limits=limits)["群一"], "failed")
            send.return_value = (True, "")
            self.assertEqual(main.send_daily_lists_to_owner(registry, state, now, limits=limits)["群一"], "sent")

    def test_capacity_replies_each_overflow_one_message_stage_every_three(self):
        registry = registry_for({"群一": 52, "群二": 34, "群三": 2})
        state = ready_state()
        limits = {"群一": 40, "群二": 30, "群三": 0}
        now = datetime(2026, 9, 7, 18, tzinfo=main.BEIJING)
        with patch("capacity_delivery.send_capacity_reply", return_value=True) as send:
            first = capacity_delivery.process_capacity_replies(registry, state, now, limits, test_rules())
            second = capacity_delivery.process_capacity_replies(registry, state, now, limits, test_rules())
        self.assertEqual(len(first), 18)
        self.assertEqual(second, {})
        for group, cap, last in (("群一", 40, 52), ("群二", 30, 34)):
            cid = dict(main.canonical_group_chat_ids())[group]
            calls = {call.args[1]: call.args[2] for call in send.call_args_list if call.args[0] == cid}
            self.assertEqual(sorted(calls), list(range(cap, last + 1)))
            for rank, text in calls.items():
                self.assertTrue(text.startswith(capacity_rule_for_rank(rank, cap)))
                self.assertIn(f"名额{cap}", text)

    def test_no_group_replies_after_cutoff_or_with_incomplete_snapshot(self):
        registry = registry_for({"群三": 45})
        with patch("capacity_delivery.send_capacity_reply") as send:
            for state, now in ((ready_state(), datetime(2026, 9, 7, 19, tzinfo=main.BEIJING)),
                               ({}, datetime(2026, 9, 7, 18, tzinfo=main.BEIJING))):
                self.assertEqual(capacity_delivery.process_capacity_replies(registry, state, now, rules=test_rules()), {})
            send.assert_not_called()

    def test_reply_switches_failed_retry_and_snapshot_reuse(self):
        registry = registry_for({"群三": 43})
        state = ready_state()
        now = datetime(2026, 9, 7, 18, tzinfo=main.BEIJING)
        rules = test_rules()
        rules["limit_full"]["enabled"] = False
        rules["limit_excess_1"]["groups"] = ["群二"]
        with patch("capacity_delivery.send_capacity_reply", return_value=False) as send:
            failed = capacity_delivery.process_capacity_replies(registry, state, now, rules=rules)
            self.assertEqual(set(failed.values()), {"failed"})
            self.assertEqual(len(failed), 3)
            self.assertTrue(all(call.args[2].startswith("limit_overflow") for call in send.call_args_list))
            send.return_value = True
            succeeded = capacity_delivery.process_capacity_replies(registry, state, now, rules=rules)
            self.assertEqual(set(succeeded.values()), {"sent"})
            state["groups"] = {}  # Telethon's snapshot replaces counters, not capacity delivery state.
            self.assertEqual(capacity_delivery.process_capacity_replies(registry, state, now, rules=rules), {})

    def test_legacy_reply_keys_prevent_repeating_a_previously_notified_message(self):
        registry = registry_for({"群三": 43})
        state = ready_state()
        state["groups"] = {main.GROUP_3_CHAT_ID_FALLBACK: {"reply_keys": ["40:limit_full", "41:limit_excess_1"]}}
        with patch("capacity_delivery.send_capacity_reply", return_value=True) as send:
            capacity_delivery.process_capacity_replies(registry, state, datetime(2026, 9, 7, 18), rules=test_rules())
        self.assertEqual([call.args[1] for call in send.call_args_list], [42, 43])

    def test_quota_does_not_change_return_account_and_source_links(self):
        registry = registry_for({"群二": 2})
        row = registry["post_entries"][main.GROUP_2_CHAT_ID_FALLBACK]["1"]
        row.update(check_handle="return_account", check_url="https://x.com/return_account", dual_link=True)
        previous = copy.deepcopy(row)
        stamp_admission(registry, normalize_daily_limits({"群二": 1}))
        self.assertEqual({key: row[key] for key in previous}, previous)


if __name__ == "__main__":
    unittest.main()
