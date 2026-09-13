from copy import deepcopy
from datetime import datetime
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

sys.modules.setdefault("requests", ModuleType("requests"))
import main
from sync_group_messages import replace_group_snapshot


DAY = "2026-09-13"
NOW = datetime(2026, 9, 13, 14, tzinfo=main.BEIJING)
GROUP2 = main.GROUP_2_CHAT_ID_FALLBACK
PROMO = "https://x.com/primary/status/111?s=46"
RETURN = "https://x.com/returner"


class ReturnProfileFollowersTest(unittest.TestCase):
    def setUp(self):
        self.registry = {"date": DAY, "entries": {}, "post_entries": {}}
        self.state = {"date": DAY, "groups": {}}
        self.counts = {"primary": 1000, "returner": 20000}
        self.texts = {"primary": "@ToBulaer @ToBuerma", "returner": "没有社区@"}
        mocker = patch("main.fetch_x_author_meta", side_effect=self.meta)
        self.fetch = mocker.start()
        self.addCleanup(mocker.stop)

    def meta(self, url, handle="", post_id=""):
        count = self.counts.get(handle, 0)
        return {"name": handle, "screen_name": handle, "followers_count": count,
                "followers_text": main.format_followers(count), "followers_sources": ["profile_v2"],
                "tweet_text": self.texts.get(handle, ""), "tweet_text_sources": ["vx_status_v2"]}

    def sync(self, text=None, group=GROUP2):
        text = text or PROMO + "\n回推号：" + RETURN
        msg = {"message_id": 10, "date": int(NOW.replace(hour=10).timestamp()),
               "text": text, "from": {"id": 1010, "username": "owner"}}
        replace_group_snapshot(self.registry, self.state, group, [msg], DAY, now=NOW)
        return self.registry["post_entries"][group].get("111", {})

    def test_second_profile_at_threshold_admits_only_first_post(self):
        row = self.sync()
        self.assertEqual(row["check_handle"], "returner")
        self.assertEqual(row["check_followers_count"], 20000)
        self.assertTrue(row["mutual_eligible"])
        self.assertEqual(main.daily_eligible_links(self.registry, GROUP2), ["https://x.com/primary/status/111"])
        self.assertEqual(set(self.registry["post_entries"][GROUP2]), {"111"})

    def test_second_post_at_threshold_works_without_required_mentions(self):
        row = self.sync(PROMO + "\nhttps://x.com/returner/status/222?s=46")
        self.assertTrue(row["mutual_eligible"])
        self.assertEqual(row["check_followers_count"], 20000)
        self.assertEqual(main.daily_eligible_links(self.registry, GROUP2), ["https://x.com/primary/status/111"])

    def test_two_below_minimum_are_not_added_together(self):
        self.counts.update(primary=15000, returner=19999)
        row = self.sync()
        self.assertFalse(row["mutual_eligible"])
        self.assertIn("followers_below_minimum", row["ineligible_reason"])

    def test_primary_sufficient_return_below_is_still_eligible(self):
        self.counts.update(primary=20000, returner=1000)
        self.assertTrue(self.sync()["mutual_eligible"])

    def test_return_mentions_cannot_replace_missing_primary_mentions(self):
        self.texts.update(primary="普通正文", returner="@ToBulaer @ToBuerma")
        row = self.sync()
        self.assertFalse(row["mutual_eligible"])
        self.assertIn("missing_required_mentions", row["ineligible_reason"])

    def test_old_snapshot_without_return_profile_is_repaired_without_text_edit(self):
        with patch("main.reply_account_check_enabled", return_value=False), patch(
                "sync_group_messages.reply_account_check_enabled", return_value=False, create=True):
            row = self.sync()
        self.assertFalse(row["mutual_eligible"])
        self.fetch.reset_mock()
        row = self.sync()
        self.assertTrue(row["mutual_eligible"])
        self.assertEqual(row["check_url"], RETURN)
        self.assertEqual(row["check_followers_count"], 20000)

    def test_correct_snapshot_stays_identical_without_repeated_metadata_requests(self):
        self.sync()
        before = deepcopy(self.registry)
        self.fetch.reset_mock()
        for _ in range(10):
            self.sync()
            self.assertEqual(self.registry, before)
        self.fetch.assert_not_called()

    def test_group1_profile_return_rule_remains_disabled(self):
        self.counts["returner"] = 200000
        row = self.sync(group=main.GROUP_1_CHAT_ID_FALLBACK)
        self.assertFalse(row["mutual_eligible"])
        self.assertEqual(row["check_handle"], "primary")

    def test_group3_retains_return_profile_separate_from_group2(self):
        self.sync()
        group2 = deepcopy(self.registry["post_entries"][GROUP2])
        row = self.sync(group=main.GROUP_3_CHAT_ID_FALLBACK)
        self.assertEqual(row["check_handle"], "returner")
        self.assertEqual(self.registry["post_entries"][GROUP2], group2)

    def test_profile_alone_does_not_create_admission(self):
        self.sync(RETURN)
        self.assertEqual(main.daily_eligible_links(self.registry, GROUP2), [])

    def test_cache_parser_never_requests_x_even_for_i_status_links(self):
        with patch("main.resolve_tweet_author", side_effect=AssertionError("must not request X")):
            links = main.extract_x_links_ordered("https://x.com/i/status/111\n" + RETURN,
                                                allow_profile_check=True, resolve_metadata=False)
        self.assertEqual(links[1]["handle"], "returner")
        self.fetch.assert_not_called()

    def test_profile_query_and_twitter_domain_are_supported(self):
        row = self.sync(PROMO + "\nhttps://twitter.com/returner?s=21")
        self.assertTrue(row["mutual_eligible"])
        self.assertEqual(row["check_handle"], "returner")

    def test_missing_return_followers_is_not_assumed_to_meet_minimum(self):
        self.counts["returner"] = None
        row = self.sync()
        self.assertNotEqual(row["eligibility_text"], "✅合格")
        self.assertIn(row["eligibility_text"], ("待确认", "❌粉丝不足"))

    def test_same_account_second_profile_does_not_reparse_forever(self):
        text = PROMO + "\nhttps://x.com/primary"
        self.sync(text)
        self.fetch.reset_mock()
        self.sync(text)
        self.fetch.assert_not_called()

    def test_third_account_cannot_satisfy_first_two_accounts_threshold(self):
        self.counts.update(returner=1000, third=50000)
        row = self.sync(PROMO + "\nhttps://x.com/returner/status/222\nhttps://x.com/third/status/333")
        self.assertFalse(row["mutual_eligible"])
        self.assertEqual(row["qualified_followers_count"], 1000)


if __name__ == "__main__":
    unittest.main()
