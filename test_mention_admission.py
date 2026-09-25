"""Regression for an internal ellipsis incorrectly bypassing required mentions."""
import unittest
from unittest.mock import patch

import main
from daily_capacity import GROUP_CHAT_IDS, eligible_rows, stamp_admission
from daily_roster import refresh_daily_rosters
from private_list_sync import edited_slots, freeze_links
from test_website_roster import NOW, DAY


def entry_for(text, chat_id):
    return {
        "promo_handle": "member", "promo_post_id": "123", "message_id": 123,
        "tg_user_id": 456, "time": DAY + " 08:00:00", "chat_id": chat_id,
        "tweet_text": text, "tweet_text_sources": ["status_v2", "vx_status_v2"],
        "content_eligible": main.promo_link_content_eligible([{"role": "promo", "tweet_text": text}]),
        "followers_count": 200000, "followers_sources": ["profile_v2"],
    }


class MentionAdmissionTest(unittest.TestCase):
    def test_internal_ellipsis_with_other_handles_is_confirmed_invalid_in_all_groups(self):
        text = "今天的记录……\n\n@other_one @other_two"
        for group, cid in GROUP_CHAT_IDS.items():
            with self.subTest(group=group):
                entry = entry_for(text, cid)
                main.update_eligibility_fields(entry, cid)
                registry = {"date": DAY, "post_entries": {cid: {"123": entry}}}
                self.assertFalse(main.tweet_text_may_be_truncated(text))
                self.assertEqual(main.count_required_mentions(text), 0)
                self.assertFalse(entry["content_eligible"])
                self.assertFalse(entry["mutual_eligible"])
                self.assertEqual(entry["eligibility_text"], "❌缺少指定@")
                self.assertEqual(eligible_rows(registry, {cid}, DAY), [])
                self.assertTrue(main.promo_link_missing_required_mentions([dict(entry, role="promo")]))

    def test_unknown_text_waits_without_admission_or_false_violation(self):
        for text in ("", "正文未返回完整…", "长正文" * 100):
            for followers in (None, 100, 200000):
                entry = entry_for(text, GROUP_CHAT_IDS["群二"])
                entry.update(followers_count=followers, followers_sources=["status_v2"], mutual_eligible=True)
                main.update_eligibility_fields(entry, entry["chat_id"])
                self.assertIsNone(entry["mutual_eligible"])
                self.assertEqual(entry["eligibility_text"], "待确认")
                self.assertFalse(main.promo_link_missing_required_mentions([dict(entry, role="promo")]))
                registry = {"date": DAY, "post_entries": {entry["chat_id"]: {"123": entry}}}
                stamp_admission(registry, {g: 30 for g in GROUP_CHAT_IDS})
                self.assertEqual(entry["daily_list_rank"], 0)
                self.assertEqual(eligible_rows(registry, {entry["chat_id"]}, DAY), [])

    def test_long_or_ellipsis_text_with_two_real_mentions_still_passes(self):
        for text in ("正文…… @ToBulaer @ToBuerma", "很长的正文" * 100 + "\n@KawasawaSen @BulmaList"):
            entry = entry_for(text, GROUP_CHAT_IDS["群三"])
            main.update_eligibility_fields(entry, entry["chat_id"])
            self.assertTrue(entry["content_eligible"])
            self.assertTrue(entry["mutual_eligible"])

    def test_unknown_does_not_freeze_into_roster_and_can_enter_after_verified_retry(self):
        cid = GROUP_CHAT_IDS["群三"]
        entry = entry_for("正文暂不完整…", cid)
        registry = {"date": DAY, "post_entries": {cid: {"123": entry}}}
        state = {"group_snapshot_sync": {"date": DAY, "completed_groups": ["群三"]}}
        limits = {g: 30 for g in GROUP_CHAT_IDS}
        main.update_eligibility_fields(entry, cid)
        refresh_daily_rosters(registry, state, limits, NOW)
        self.assertEqual(state["daily_rosters"]["groups"]["群三"]["slots"], [])
        entry["tweet_text"] = "正文已补全 @ToBulaer @BulmaList"
        with patch("main.fetch_x_author_meta", return_value={}):
            main.enrich_entry_metadata(entry)
        refresh_daily_rosters(registry, state, limits, NOW)
        self.assertEqual([s["post_id"] for s in state["daily_rosters"]["groups"]["群三"]["slots"]], ["123"])

    def test_unconfirmed_replacement_keeps_original_good_link(self):
        cid = GROUP_CHAT_IDS["群三"]
        old = entry_for("@ToBulaer @BulmaList", cid)
        registry = {"date": DAY, "post_entries": {cid: {"123": old}}}
        slots = freeze_links(["https://x.com/member/status/123"], registry, {cid}, DAY)
        changed = dict(old, promo_post_id="456", edited=True, edit_time=DAY+" 09:00:00",
                       tweet_text="新文未读完…", content_eligible=None, mutual_eligible=True)
        main.update_eligibility_fields(changed, cid)
        registry["post_entries"][cid] = {"456": changed}
        self.assertEqual(edited_slots(slots, registry, {cid}, DAY, DAY+" 12:00:00"), slots)

    def test_cached_pending_row_is_corrected_from_existing_complete_text_without_fetch(self):
        entry = entry_for("正文……\n@other_one @other_two", GROUP_CHAT_IDS["群三"])
        entry.update(content_eligible=None, mutual_eligible=True, x_name="Member")
        with patch("main.fetch_x_author_meta") as fetch:
            main.enrich_entry_metadata(entry)
        fetch.assert_not_called()
        self.assertFalse(entry["mutual_eligible"])
        self.assertFalse(entry["content_eligible"])


if __name__ == "__main__":
    unittest.main()
