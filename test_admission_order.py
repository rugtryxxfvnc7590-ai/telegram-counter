from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", ModuleType("requests"))
import main
from daily_capacity import admitted_rows, chat_ids_for_group, stamp_admission
from private_list_sync import edited_slots, freeze_links
from sync_group_messages import replace_group_snapshot
from withdrawal_sync import plan_roster

DAY = "2026-09-14"
NOW = datetime(2026, 9, 14, 18, tzinfo=main.BEIJING)
GROUP = "群一"
CID = main.GROUP_1_CHAT_ID_FALLBACK
IDS = chat_ids_for_group(GROUP)
LIMITS = {"群一": 1, "群二": 1, "群三": 1}


def message(mid, hour, handle="alice", post="111", edit=None):
    return {"message_id": mid, "date": int(NOW.replace(hour=hour).timestamp()),
            "edit_date": int(NOW.replace(hour=edit).timestamp()) if edit is not None else None,
            "text": f"https://x.com/{handle}/status/{post}" if post else "先打个招呼",
            "from": {"id": mid + 1000, "username": f"sender{mid}"}}


class AdmissionOrderTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"date": DAY, "entries": {}, "post_entries": {}}
        self.state = {"date": DAY, "groups": {}}
        self.bad_posts = set()
        mocker = patch("main.fetch_x_author_meta", side_effect=self.meta)
        self.fetch = mocker.start()
        self.addCleanup(mocker.stop)

    def meta(self, url, handle="", post_id=""):
        return {"name": handle, "screen_name": handle, "followers_count": 200000,
                "tweet_text": "没有社区账号" if post_id in self.bad_posts else "@ToBulaer @ToBuerma",
                "tweet_text_sources": ["vx_status_v2"]}

    def sync(self, messages, cid=CID):
        replace_group_snapshot(self.registry, self.state, cid, messages, DAY, now=NOW)

    def entry(self, post="111", cid=CID):
        return self.registry["post_entries"][cid][post]

    def accepted(self, limit=1):
        accepted, _ = admitted_rows(self.registry, IDS, limit, DAY)
        return [row["post_id"] for row in accepted]

    def publish(self):
        links = main.daily_eligible_links(self.registry, CID, LIMITS)
        slots = freeze_links(links, self.registry, IDS, DAY)
        record = {"sent": True, "count": len(links), "message_id": 1234,
                  "owner_chat_id": "5678", "links": links, "slots": slots,
                  "text": main.format_daily_list_message(GROUP, DAY, links)}
        self.state["owner_daily_lists"] = {"date": DAY, "groups": {GROUP: record}}
        return record

    def test_empty_at_one_edited_at_three_does_not_overtake_two(self):
        b = message(20, 2, "bob", "222")
        self.sync([message(10, 1, post=""), b])
        self.sync([message(10, 1, edit=3), b])
        self.assertEqual(self.accepted(), ["222"])
        self.assertEqual(self.entry()["admission_time"], DAY + " 03:00:00")
        self.assertEqual(self.entry()["time"], DAY + " 01:00:00")

    def test_first_snapshot_uses_edit_timestamp_without_historical_observation(self):
        self.sync([message(10, 1, edit=3), message(20, 2, "bob", "222")])
        self.assertEqual(self.accepted(), ["222"])

    def test_bot_recording_and_snapshot_have_identical_order(self):
        msgs = [message(10, 1, edit=3), message(20, 2, "bob", "222")]
        self.sync(msgs)
        registry = {"date": DAY, "entries": {}, "post_entries": {}}
        for msg in msgs:
            links = main.extract_x_links_ordered(msg["text"])
            main.record_link(registry, links[0]["handle"], msg, msg["text"], CID,
                             main.beijing_full_time(msg["date"]), links=links)
        self.assertEqual(registry["post_entries"], self.registry["post_entries"])

    def test_unresolved_i_status_also_uses_edit_time(self):
        msg = message(10, 1, edit=3)
        links = [{"url": "https://x.com/i/status/111", "post_id": "111", "role": "promo"}]
        main.record_post_only(self.registry, msg, msg["text"], CID,
                              main.beijing_full_time(msg["date"]), links)
        self.assertEqual(self.entry()["admission_time"], DAY + " 03:00:00")

    def test_provisionally_accepted_member_keeps_position_when_replacing_post(self):
        self.sync([message(10, 1)])
        stamp_admission(self.registry, LIMITS)
        self.sync([message(10, 1, post="112", edit=4), message(20, 2, "bob", "222")])
        self.assertEqual(self.accepted(), ["112"])
        self.assertEqual(self.entry("112")["admission_time"], DAY + " 01:00:00")

    def test_waiting_replacement_requeues_at_edit_time(self):
        first = message(10, 0, "first", "100")
        self.sync([first, message(20, 1)])
        stamp_admission(self.registry, LIMITS)
        self.sync([first, message(20, 1, post="112", edit=4), message(30, 2, "bob", "222")])
        self.assertEqual(self.accepted(limit=0), ["100", "222", "112"])

    def test_published_member_keeps_slot_despite_stale_waitlist_rank(self):
        self.sync([message(10, 1, edit=3)])
        record = self.publish()
        self.entry()["daily_list_status"] = "waitlist"
        self.sync([message(10, 1, post="112", edit=5)])
        updated = edited_slots(record["slots"], self.registry, IDS, DAY, DAY + " 18:00:00")
        self.assertEqual(updated[0]["post_id"], "112")
        self.assertEqual(updated[0]["admission_time"], DAY + " 03:00:00")
        self.assertEqual(self.entry("112")["admission_time"], DAY + " 03:00:00")
        self.assertEqual(self.state["owner_daily_lists"]["groups"][GROUP], record)

    def test_rejected_admitted_edit_retains_old_link_then_accepts_qualified_replacement(self):
        self.sync([message(10, 1)])
        record = self.publish()
        self.bad_posts.add("112")
        self.sync([message(10, 1, post="112", edit=4)])
        self.assertEqual(edited_slots(record["slots"], self.registry, IDS, DAY, DAY + " 18:00:00"), record["slots"])
        self.sync([message(10, 1, post="113", edit=5)])
        updated = edited_slots(record["slots"], self.registry, IDS, DAY, DAY + " 18:00:00")
        self.assertEqual(updated[0]["post_id"], "113")
        self.assertEqual(self.entry("113")["admission_time"], DAY + " 01:00:00")

    def test_confirmed_roster_does_not_admit_other_message_from_provisional_rank(self):
        self.sync([message(10, 0, "first", "100"), message(20, 1)])
        self.publish()
        self.entry()["daily_list_status"] = "accepted"
        self.sync([message(10, 0, "first", "100"), message(20, 1, post="112", edit=4)])
        self.assertEqual(self.entry("112")["admission_time"], DAY + " 04:00:00")

    def test_same_sender_new_message_does_not_inherit_admission(self):
        a = message(10, 1)
        self.sync([a])
        self.publish()
        new = message(20, 1, post="112", edit=4)
        new["from"] = a["from"]
        self.sync([a, new])
        self.assertEqual(self.entry("112")["admission_time"], DAY + " 04:00:00")

    def test_same_message_id_in_other_group_does_not_inherit_slot(self):
        self.sync([message(10, 1)])
        self.publish()
        group2 = main.GROUP_2_CHAT_ID_FALLBACK
        self.sync([message(10, 1, post="112", edit=4)], cid=group2)
        self.assertEqual(self.entry("112", group2)["admission_time"], DAY + " 04:00:00")

    def test_pending_new_link_after_cutoff_does_not_take_pre_cutoff_slot(self):
        self.sync([message(10, 1, edit=20), message(20, 2, "bob", "222")])
        self.assertEqual(self.accepted(limit=0), ["222"])

    def test_already_admitted_edit_after_cutoff_keeps_original_replacement_rule(self):
        self.sync([message(10, 1)])
        record = self.publish()
        self.sync([message(10, 1, post="112", edit=20)])
        updated = edited_slots(record["slots"], self.registry, IDS, DAY, DAY + " 21:00:00")
        self.assertEqual(updated[0]["post_id"], "112")
        self.assertEqual(self.entry("112")["admission_time"], DAY + " 01:00:00")

    def test_legacy_pending_cache_migrates_without_metadata_requests(self):
        msgs = [message(10, 1, edit=3), message(20, 2, "bob", "222")]
        self.sync(msgs)
        for bucket in ("entries", "post_entries"):
            for entry in self.registry[bucket][CID].values():
                entry.pop("admission_time", None)
                entry.pop("admission_locked", None)
        self.fetch.reset_mock()
        self.sync(msgs)
        self.assertEqual(self.accepted(), ["222"])
        before = deepcopy(self.registry)
        for _ in range(10):
            self.sync(list(reversed(msgs)))
            self.assertEqual(self.registry, before)
        self.fetch.assert_not_called()

    def test_legacy_published_slot_preserves_original_time(self):
        self.sync([message(10, 1)])
        record = self.publish()
        record["slots"][0].pop("admission_time")
        self.sync([message(10, 1, post="112", edit=4)])
        self.assertEqual(self.entry("112")["admission_time"], DAY + " 01:00:00")

    def test_refill_chooses_two_before_edited_three(self):
        first = message(5, 0, "first", "100")
        self.sync([first])
        record = self.publish()
        self.sync([message(10, 1, edit=3), message(20, 2, "bob", "222")])
        plan = plan_roster(record, self.registry, CID, DAY, DAY + " 18:00:00", 1)
        self.assertEqual([s["post_id"] for s in plan["slots"]], ["222"])

    def test_private_list_reverse_numbering_uses_effective_time(self):
        self.sync([message(10, 1, edit=3), message(20, 2, "bob", "222")])
        links = main.daily_eligible_links(self.registry, CID, {GROUP: 0})
        self.assertEqual(links, ["https://x.com/alice/status/111", "https://x.com/bob/status/222"])

    def test_version_and_changelog_match(self):
        root = Path(__file__).parent
        version = (root / "VERSION_COUNTER_BOT").read_text().strip()
        self.assertIn(f"## v{version} - ", (root / "CHANGELOG_COUNTER_BOT.md").read_text())


if __name__ == "__main__":
    unittest.main()
