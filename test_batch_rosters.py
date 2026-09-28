from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from batch_rosters import batch_policy, refresh_batch_roster, export_batch_rosters, process_batch_notices
from daily_roster import refresh_daily_rosters
from website_sync import sync_website
from website_deletions import website_items, deleted_entries
from withdrawal_sync import note_withdrawals
from violation_delivery import receipt_run_key

DAY = "2026-09-28"
NOW = datetime(2026, 9, 28, 12, tzinfo=main.BEIJING)
CID = "-1003891628675"
LIMITS = {group: 30 for group in ("群一", "群二", "群三")}


def fixture(count=61):
    registry = {"date": DAY, "post_entries": {CID: {}}}
    for n in range(1, count + 1):
        registry["post_entries"][CID][str(10000+n)] = {
            "promo_handle": f"user{n}", "promo_post_id": str(10000+n), "message_id": n,
            "tg_user_id": n, "time": f"{DAY} 01:{n//60:02d}:{n%60:02d}", "mutual_eligible": True,
            "x_name": f"Member {n}",
        }
    state = {"date": DAY, "group_snapshot_sync": {"date": DAY, "completed_groups": ["群一"],
        "violation_receipt_groups":{"群一":{"run_key":receipt_run_key(),"bot_user_id":123,
            "checked_at":f"{DAY} 12:00:00"}}}}
    return registry, state


def roster(registry, state, now=NOW):
    return refresh_daily_rosters(registry, state, LIMITS, now)["群一"]


def acknowledge_website(state):
    post=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:{"status":"updated"}))
    sync_website(state,NOW,secret="test-secret",post=post)


class BatchRosterTests(unittest.TestCase):
    def setUp(self):
        policy=patch("batch_rosters.load_batch_config",return_value={"群一":{
            "enabled":True,"effective_from":DAY,"size":30,"max_batches":2}})
        policy.start()
        self.addCleanup(policy.stop)

    def test_tomorrow_only_group1_and_strict_config(self):
        self.assertIsNone(batch_policy("群一", "2026-09-27"))
        self.assertIsNone(batch_policy("群二", DAY))
        self.assertIsNone(batch_policy("群三", DAY))
        self.assertEqual(batch_policy("群一", DAY)["size"], 30)
        for values in ({"size":31,"max_batches":2},{"size":True,"max_batches":2},{"size":30,"max_batches":0}):
            self.assertIsNone(batch_policy("群一", DAY, {"群一":dict(values,enabled=True,effective_from=DAY)}))

    def test_first30_A_next30_B_61_waits_and_10_replays(self):
        reg, state = fixture()
        first = deepcopy(roster(reg, state))
        self.assertEqual({s["handle"] for s in first["batches"]["A"]["slots"]}, {f"user{i}" for i in range(1,31)})
        self.assertEqual({s["handle"] for s in first["batches"]["B"]["slots"]}, {f"user{i}" for i in range(31,61)})
        self.assertNotIn("user61", first["assigned_accounts"])
        self.assertEqual(first["batches"]["B"]["slots"][0]["handle"], "user60")
        for _ in range(10):
            state = json.loads(json.dumps(state))
            self.assertEqual(roster(reg, state), first)

    def test_duplicate_username_cannot_take_two_slots(self):
        reg, state = fixture(32)
        reg["post_entries"][CID]["10031"]["promo_handle"] = "USER1"
        result = roster(reg, state)
        self.assertEqual(len(result["slots"]), 31)
        self.assertEqual([s["handle"] for s in result["batches"]["B"]["slots"]], ["user32"])

    def test_underfilled_B_sends_at19_not_earlier_and_no_duplicate_private_send(self):
        reg, state = fixture(31)
        with patch.dict("os.environ", {"TELEGRAM_OWNER_CHAT_ID":"12345"}), patch.object(main,"_send_private_message",return_value=(True,{"message_id":999})) as send, patch.object(main,"_edit_private_message",return_value=(True,"")):
            main.send_daily_lists_to_owner(reg,state,NOW,limits=LIMITS)
            self.assertEqual(send.call_count,1)
            self.assertIn("群一·A名单",send.call_args.args[1])
            main.send_daily_lists_to_owner(reg,state,NOW.replace(hour=19),limits=LIMITS)
            self.assertEqual(send.call_count,2)
            self.assertIn("群一·B名单",send.call_args.args[1])
            main.send_daily_lists_to_owner(reg,state,NOW.replace(hour=20),limits=LIMITS)
            self.assertEqual(send.call_count,2)

    def test_deletion_fills_A_from_unassigned_not_B(self):
        reg, state = fixture()
        old = deepcopy(roster(reg,state))
        old_slot = next(s for s in old["slots"] if s["handle"]=="user5")
        reg["post_entries"][CID].pop("10005")
        note_withdrawals(reg,CID,{"5":"deleted"},NOW)
        new = roster(reg,state)
        self.assertEqual(new["batches"]["B"],old["batches"]["B"])
        replacement = next(s for s in new["slots"] if s["handle"]=="user61")
        self.assertEqual(replacement["position"],old_slot["position"])
        self.assertEqual(replacement["list_id"],"A")
        item = next(x for x in website_items(new,deleted_entries(state,"群一",DAY)) if x["position"]==replacement["position"])
        self.assertEqual(item["deletedAccounts"],["user5"])

    def test_deletion_without_waiting_does_not_move_B_or_fill_after19(self):
        reg, state = fixture(31)
        old = deepcopy(roster(reg,state))
        reg["post_entries"][CID].pop("10005")
        note_withdrawals(reg,CID,{"5":"deleted"},NOW)
        new = roster(reg,state)
        self.assertEqual(len(new["batches"]["A"]["slots"]),29)
        self.assertEqual(new["batches"]["B"],old["batches"]["B"])
        extra,_=fixture(32)
        reg["post_entries"][CID]["10032"]=extra["post_entries"][CID]["10032"]
        reg["post_entries"][CID]["10032"]["time"]=f"{DAY} 19:00:00"
        after=roster(reg,state,NOW.replace(hour=19))
        self.assertNotIn("user32",after["assigned_accounts"])

    def test_after19_tombstone_keeps_B_id_local_position(self):
        reg,state=fixture(31)
        before=deepcopy(roster(reg,state))
        note_withdrawals(reg,CID,{"31":"deleted"},NOW.replace(hour=19))
        after=roster(reg,state,NOW.replace(hour=19))
        self.assertEqual(after["batches"]["A"],before["batches"]["A"])
        self.assertEqual(after["batches"]["B"]["slots"],[])
        removed=next(x for x in website_items(after,deleted_entries(state,"群一",DAY)) if x["position"]==31)
        self.assertEqual((removed["url"],removed["listId"],removed["listPosition"]),(None,"B",1))

    def test_qualified_edit_retains_membership_position_bad_edit_retains_url(self):
        reg,state=fixture(31)
        old=deepcopy(roster(reg,state))
        entry=reg["post_entries"][CID].pop("10031")
        entry.update(promo_post_id="99001",edited=True,edit_time=f"{DAY} 11:00:00",mutual_eligible=False)
        reg["post_entries"][CID]["99001"]=entry
        self.assertEqual(roster(reg,state)["batches"]["B"]["slots"][0]["post_id"],"10031")
        entry["mutual_eligible"]=True
        current=roster(reg,state)["batches"]["B"]["slots"][0]
        self.assertEqual((current["post_id"],current["position"]),("99001",1))
        self.assertEqual(roster(reg,state)["batches"]["A"],old["batches"]["A"])

    def test_policy_change_cannot_resize_existing_day(self):
        reg,state=fixture(31)
        old=roster(reg,state)
        new=refresh_batch_roster(old,reg,"群一",DAY,f"{DAY} 12:00:00",{"size":10,"max_batches":3})
        self.assertEqual(new,old)

    def test_export_includes_authoritative_A_B_old_retained_links(self):
        reg,state=fixture(31)
        expected=roster(reg,state)
        export_batch_rosters(reg,state)
        self.assertEqual(reg["batch_rosters"]["groups"]["群一"],expected)
        self.assertEqual(reg["post_entries"][CID]["10031"]["daily_list_status"], "accepted")
        self.assertEqual(reg["post_entries"][CID]["10031"]["daily_list_batch"], "B")

    def test_cutoff_finalizes_once_then_no_new_admissions_or_replacement(self):
        reg,state=fixture(31)
        roster(reg,state)
        extra,_=fixture(32)
        reg["post_entries"][CID]["10032"]=extra["post_entries"][CID]["10032"]
        first=roster(reg,state,NOW.replace(hour=19))
        self.assertIn("user32",first["assigned_accounts"])
        self.assertTrue(first["cutoff_finalized"])
        extra,_=fixture(33)
        reg["post_entries"][CID]["10033"]=extra["post_entries"][CID]["10033"]
        note_withdrawals(reg,CID,{"5":"deleted"},NOW.replace(hour=20))
        after=roster(reg,state,NOW.replace(hour=20))
        self.assertNotIn("user33",after["assigned_accounts"])
        self.assertEqual(len(after["batches"]["A"]["slots"]),29)

    def test_old_snapshot_cannot_send_batch_private_lists(self):
        reg,state=fixture(31)
        roster(reg,state)
        state["group_snapshot_sync"]["completed_groups"]=[]
        with patch.dict("os.environ", {"TELEGRAM_OWNER_CHAT_ID":"12345"}), patch.object(main,"_send_private_message") as send:
            result=main.send_daily_lists_to_owner(reg,state,NOW,limits=LIMITS)
        send.assert_not_called()
        self.assertEqual(result["群一"],"waiting_for_snapshot")

    def test_capacity_only_replies_to_unassigned_accounts(self):
        from capacity_delivery import process_capacity_replies
        reg,state=fixture(61)
        roster(reg,state)
        with patch("capacity_delivery.send_capacity_reply",return_value=True) as send, patch.object(main,"reply_rule_enabled",return_value=True), patch.object(main,"reply_rule_text",return_value="候补"):
            process_capacity_replies(reg,state,NOW,limits=LIMITS,rules={})
            self.assertEqual(send.call_count,1)
            self.assertEqual(send.call_args.args[1],61)
            process_capacity_replies(reg,state,NOW,limits=LIMITS,rules={})
            self.assertEqual(send.call_count,1)

    def test_notice_recovery_deduplicates_only_our_bot(self):
        from batch_rosters import admission_notice, recover_batch_notices
        reg,state=fixture(1)
        roster(reg,state)
        acknowledge_website(state)
        message={"from":{"id":123,"is_bot":True},"date":int(NOW.timestamp()),
                 "reply_to_message_id":1,"text":admission_notice("群一",DAY,"A")}
        recover_batch_notices(state,CID,[message],999,NOW)
        self.assertNotIn("1",state["batch_notices"]["groups"]["群一"])
        recover_batch_notices(state,CID,[message],123,NOW)
        sender=Mock()
        process_batch_notices(reg,state,NOW,send_reply=sender)
        sender.assert_not_called()

    def test_cutoff_navigation_standalone_no_duplicate(self):
        from cutoff_delivery import process_cutoff_announcements
        reg,state=fixture(31)
        now=NOW.replace(hour=19)
        with patch.dict("os.environ", {"TELEGRAM_OWNER_CHAT_ID":"12345"}), patch.object(main,"_send_private_message",return_value=(True,{"message_id":999})):
            main.send_daily_lists_to_owner(reg,state,now,limits=LIMITS)
        state["group_snapshot_sync"]["cutoff_receipt_groups"]={"群一":{
            "run_key":receipt_run_key(),"checked_at":f"{DAY} 19:00:00","bot_user_id":123}}
        sender=Mock(return_value=(True,{"message_id":1000}))
        rules={"群一":{"enabled":True,"text":"旧模板"}}
        result=process_cutoff_announcements(reg,state,now,rules,send_message=sender)
        self.assertEqual(result["群一"],"sent")
        self.assertEqual(sender.call_args.args[0],CID)
        self.assertEqual(len(sender.call_args.args),2)
        self.assertIsNone(sender.call_args.kwargs["reply_to"])
        text=sender.call_args.args[1]
        self.assertIn("A名单：30人",text)
        self.assertIn("B名单：1人",text)
        self.assertIn("&list=B",text)
        process_cutoff_announcements(reg,state,now,rules,send_message=sender)
        self.assertEqual(sender.call_count,1)

    def test_standalone_batch_announcement_recovered_from_history(self):
        from cutoff_delivery import recover_cutoff_announcements, _plain_text
        from batch_rosters import batch_cutoff_text
        reg,state=fixture(31)
        roster(reg,state)
        now=NOW.replace(hour=19)
        message={"message_id":1000,"date":int(now.timestamp()),
                 "from":{"id":123,"is_bot":True},
                 "text":_plain_text(batch_cutoff_text("群一",DAY,state["daily_rosters"]["groups"]["群一"]))}
        rules={"群一":{"enabled":True,"text":"旧模板"}}
        self.assertEqual(recover_cutoff_announcements(state,CID,[message],123,rules,now),1)
        record=state["cutoff_announcements"]["groups"]["群一"]
        self.assertEqual(record["status"],"sent")
        self.assertEqual(record["delivery_mode"],"standalone")
        self.assertEqual(record["message_id"],1000)

    def test_only_A_last_and_B_first_notices_once_before19(self):
        reg,state=fixture(61)
        roster(reg,state)
        sender=Mock(return_value=True)
        process_batch_notices(reg,state,NOW,send_reply=sender)
        sender.assert_not_called()
        acknowledge_website(state)
        process_batch_notices(reg,state,NOW,send_reply=sender)
        self.assertEqual(sender.call_count,2)
        self.assertEqual([call.args[1] for call in sender.call_args_list],[30,31])
        self.assertIn("A名单已满",sender.call_args_list[0].args[2])
        self.assertIn("B名单从这条消息开始",sender.call_args_list[1].args[2])
        process_batch_notices(reg,state,NOW,send_reply=sender)
        process_batch_notices(reg,state,NOW.replace(hour=19),send_reply=sender)
        self.assertEqual(sender.call_count,2)

    def test_no_individual_notices_and_no_member_private_messages(self):
        reg,state=fixture(29)
        roster(reg,state)
        acknowledge_website(state)
        sender=Mock()
        with patch.object(main,"_send_private_message") as private:
            process_batch_notices(reg,state,NOW,send_reply=sender)
        sender.assert_not_called()
        private.assert_not_called()

    def test_boundaries_use_admission_time_not_display_number_or_message_order(self):
        from batch_rosters import first_batch_message
        from cutoff_delivery import reply_target
        slots=[{"message_id":"2","position":1,"time":f"{DAY} 01:00:00","admission_time":f"{DAY} 03:00:00"},
               {"message_id":"90","position":2,"time":f"{DAY} 02:00:00"}]
        self.assertEqual(first_batch_message(slots,DAY),90)
        self.assertEqual(reply_target(slots,DAY),2)

    def test_upgrade_does_not_repeat_already_notified_B_starter(self):
        reg,state=fixture(32)
        roster(reg,state)
        acknowledge_website(state)
        state["batch_notices"]={"date":DAY,"groups":{"群一":{"full_A":{},"31":{"list_id":"B"}}}}
        sender=Mock()
        process_batch_notices(reg,state,NOW,send_reply=sender)
        sender.assert_not_called()
        self.assertEqual(state["batch_notices"]["groups"]["群一"]["start_B"]["message_id"],31)

    def test_recover_new_boundary_notice_only_from_our_bot(self):
        from batch_rosters import batch_start_notice, recover_batch_notices
        reg,state=fixture(31)
        roster(reg,state)
        message={"from":{"id":123,"is_bot":True},"date":int(NOW.timestamp()),
                 "reply_to_message_id":31,"text":batch_start_notice("群一",DAY)}
        recover_batch_notices(state,CID,[message],123,NOW)
        self.assertEqual(state["batch_notices"]["groups"]["群一"]["start_B"]["message_id"],31)

    def test_stale_website_ack_does_not_send_new_member_notice(self):
        reg,state=fixture(30)
        roster(reg,state)
        acknowledge_website(state)
        extra,_=fixture(31)
        reg["post_entries"][CID]["10031"]=extra["post_entries"][CID]["10031"]
        roster(reg,state)
        sender=Mock()
        process_batch_notices(reg,state,NOW,send_reply=sender)
        sender.assert_not_called()

    def test_batch_website_publishes_early_with_no_legacy_group_change(self):
        reg,state=fixture(31)
        roster(reg,state)
        post=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:{"status":"updated"}))
        sync_website(state,NOW,secret="test-secret",post=post)
        payload=json.loads(post.call_args.kwargs["data"])
        self.assertEqual(payload["batchPolicy"]["size"],30)
        self.assertEqual(payload["items"][-1]["listId"],"B")
        self.assertEqual(payload["items"][-1]["listPosition"],1)


if __name__ == "__main__":
    unittest.main()
