import copy
from datetime import datetime
import io
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
import send_daily_lists
from private_list_sync import freeze_links

DAY = "2026-09-12"
OWNER = "8614747348"
GROUPS = dict(main.canonical_group_chat_ids())
NOW = datetime(2026, 9, 12, 12, tzinfo=main.BEIJING)


def entry(handle, post_id, message_id, minute=0, **extra):
    row = dict(promo_handle=handle, promo_post_id=str(post_id), message_id=message_id,
               tg_user_id=message_id + 1000, time=f"{DAY} 00:{minute:02d}:00",
               promo_url=f"https://x.com/{handle}/status/{post_id}",
               after_cutoff=False, mutual_eligible=True, edited=False, edit_time="")
    row.update(extra)
    return row


class PrivateListSyncTest(unittest.TestCase):
    def setUp(self):
        self.registry = {"date": DAY, "post_entries": {cid: {
            "101": entry("Alice", 101, 1), "202": entry("Bob", 202, 2, 1),
            "303": entry("waiting", 303, 3, 2),
        } for cid in GROUPS.values()}}
        self.state = {"group_snapshot_sync": {"date": DAY, "completed_groups": list(GROUPS)}}
        self.limits = {g: 2 for g in GROUPS}
        for mocker in [
            patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: OWNER}),
            patch("main._send_private_message", return_value=(True, {"message_id": 900})),
            patch("main._edit_private_message", return_value=(True, "")),
        ]:
            obj = mocker.start()
            self.addCleanup(mocker.stop)
            if hasattr(obj, "mock_calls"):
                if not hasattr(self, "send"):
                    self.send = obj
                else:
                    self.edit = obj
        self.output = io.StringIO()
        ctx = redirect_stdout(self.output)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.deliver()
        self.send.reset_mock()

    def deliver(self, now=NOW, **kwargs):
        return main.send_daily_lists_to_owner(self.registry, self.state, now=now, limits=self.limits, **kwargs)

    def record(self, group="群一"):
        return self.state["owner_daily_lists"]["groups"][group]

    def change(self, group="群一", post_id="404", **extra):
        bucket = self.registry["post_entries"][GROUPS[group]]
        old = bucket.pop("101")
        new = dict(old, promo_post_id=post_id, promo_url=f"https://x.com/Alice/status/{post_id}",
                   edited=True, edit_time=f"{DAY} 11:00:00")
        new.update(extra)
        bucket[post_id] = new
        return new

    def test_first_send_saves_private_message_and_frozen_bindings(self):
        r = self.record()
        self.assertEqual(r["message_id"], 900)
        self.assertEqual(r["owner_chat_id"], OWNER)
        self.assertEqual([s["handle"] for s in r["slots"]], ["bob", "alice"])
        self.assertEqual([s["message_id"] for s in r["slots"]], ["2", "1"])

    def test_edit_after_full_changes_same_message_only(self):
        self.change()
        before = self.record()["links"][0]
        saved = []
        result = self.deliver(save_callback=lambda: saved.append(copy.deepcopy(self.record())))
        self.assertEqual(result["群一"], "edited")
        self.edit.assert_called_once()
        owner, mid, text = self.edit.call_args.args
        self.assertEqual((owner, mid), (OWNER, 900))
        self.assertIn("已编辑 2 https://x.com/alice/status/404", text)
        self.assertIn(f"1 {before}\n\n", text)
        self.assertNotIn("waiting", text)
        self.assertEqual(self.record()["count"], 2)
        self.assertEqual(len(saved), 1)
        self.send.assert_not_called()
        self.assertEqual(self.deliver()["群一"], "already_sent")
        self.edit.assert_called_once()

    def test_edits_after_19_allowed_and_limits_do_not_refill_or_reorder(self):
        self.change(edit_time=f"{DAY} 23:10:00")
        self.limits["群一"] = 40
        self.deliver(datetime(2026, 9, 12, 23, 59, tzinfo=main.BEIJING))
        self.assertEqual([s["handle"] for s in self.record()["slots"]], ["bob", "alice"])
        self.assertEqual(len(self.record()["links"]), 2)
        self.assertTrue(self.record()["slots"][1]["edited"])

    def test_list_sent_at_19_without_full_can_also_edit(self):
        self.state["owner_daily_lists"]["groups"].pop("群一")
        self.limits["群一"] = 40
        late = datetime(2026, 9, 12, 19, tzinfo=main.BEIJING)
        self.deliver(late)
        self.assertEqual(self.record()["trigger"], "19:00")
        self.change()
        self.deliver(late)
        self.assertEqual(self.record()["links"][-1], "https://x.com/alice/status/404")

    def test_all_three_groups_same_rule_but_isolated(self):
        for group in GROUPS:
            with self.subTest(group=group):
                self.change(group)
                result = self.deliver()
                self.assertEqual(result[group], "edited")
                self.assertTrue(self.edit.call_args.args[2].startswith(group))
        self.assertEqual(self.edit.call_count, 3)

    def test_wrong_account_message_sender_date_or_unqualified_ignored(self):
        original = copy.deepcopy(self.registry)
        bad_values = [
            {"promo_handle": "mallory"}, {"message_id": 99}, {"tg_user_id": 999},
            {"time": "2026-09-11 00:00:00"}, {"time": f"{DAY} 19:00:00"},
            {"mutual_eligible": False}, {"mutual_eligible": None}, {"after_cutoff": True},
            {"edited": False}, {"edit_time": ""}, {"edit_time": "2026-09-13 00:00:00"},
            {"edit_time": f"{DAY} 13:00:00"}, {"edit_time": f"{DAY} 99:99:99"},
        ]
        for extra in bad_values:
            with self.subTest(extra=extra):
                self.registry = copy.deepcopy(original)
                self.change(**extra)
                self.deliver()
                self.edit.assert_not_called()

    def test_unqualified_edit_can_become_qualified_next_snapshot(self):
        row = self.change(mutual_eligible=False)
        self.deliver()
        self.assertTrue(self.record()["links"][-1].endswith("/101"))
        row["mutual_eligible"] = True
        self.deliver()
        self.assertTrue(self.record()["links"][-1].endswith("/404"))

    def test_case_and_query_only_change_is_not_a_post_edit(self):
        row = self.registry["post_entries"][GROUPS["群一"]]["101"]
        row.update(promo_handle="aLiCe", promo_url="https://x.com/aLiCe/status/101?s=46",
                   edited=True, edit_time=f"{DAY} 11:00:00")
        self.deliver()
        self.edit.assert_not_called()

    def test_return_link_only_and_waitlist_edit_do_not_change_private_roster(self):
        bucket = self.registry["post_entries"][GROUPS["群一"]]
        bucket["999"] = dict(bucket["101"], check_post_id="999", check_handle="alternate",
                             edited=True, edit_time=f"{DAY} 11:00:00")
        bucket["303"].update(edited=True, edit_time=f"{DAY} 11:00:00")
        self.deliver()
        self.edit.assert_not_called()

    def test_deleted_member_does_not_refill_from_waitlist(self):
        self.registry["post_entries"][GROUPS["群一"]].pop("101")
        self.deliver()
        self.edit.assert_not_called()
        self.assertEqual(len(self.record()["links"]), 2)

    def test_incomplete_or_stale_snapshot_does_not_edit(self):
        self.change()
        self.state["group_snapshot_sync"]["completed_groups"].remove("群一")
        self.assertEqual(self.deliver()["群一"], "waiting_for_snapshot")
        self.edit.assert_not_called()
        self.state["group_snapshot_sync"]["completed_groups"].append("群一")
        self.registry["date"] = "2026-09-11"
        self.deliver()
        self.edit.assert_not_called()

    def test_failed_edit_preserves_state_then_retries_without_resending(self):
        old = copy.deepcopy(self.record())
        self.change()
        self.edit.return_value = (False, "temporary")
        saved = []
        self.assertEqual(self.deliver(save_callback=lambda: saved.append(True))["群一"], "edit_failed")
        self.assertEqual(self.record(), old)
        self.assertEqual(saved, [])
        self.edit.return_value = (True, "")
        self.assertEqual(self.deliver()["群一"], "edited")
        self.send.assert_not_called()

    def test_edit_back_to_original_remains_marked_and_stale_snapshot_cannot_roll_back(self):
        changed = self.change()
        self.deliver()
        bucket = self.registry["post_entries"][GROUPS["群一"]]
        bucket.pop("404")
        bucket["101"] = dict(changed, promo_post_id="101", edit_time=f"{DAY} 11:10:00")
        self.deliver()
        self.assertIn("已编辑 2 https://x.com/alice/status/101", self.record()["text"])
        bucket.pop("101")
        bucket["404"] = changed
        self.deliver()
        self.assertTrue(self.record()["links"][-1].endswith("/101"))

    def test_conflicting_alias_bucket_versions_are_not_guessed(self):
        changed = self.change()
        alias = next(cid for cid in main.expand_chat_id(GROUPS["群一"]) if cid != GROUPS["群一"])
        self.registry["post_entries"][alias] = {"405": dict(changed, promo_post_id="405")}
        self.deliver()
        self.edit.assert_not_called()

    def test_owner_change_is_not_sent_to_new_recipient(self):
        self.change()
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "12345"}):
            self.assertEqual(self.deliver()["群一"], "owner_changed")
        self.edit.assert_not_called()

    def test_unknown_group_recipient_is_also_rejected(self):
        self.change()
        with patch.dict("os.environ", {main.OWNER_CHAT_ID_ENV: "-100999999999"}):
            self.assertEqual(self.deliver(), {})
        self.edit.assert_not_called()
        self.send.assert_not_called()

    def test_legacy_migration_is_opt_in_once_preserving_original_members(self):
        r = self.record()
        for key in ("slots", "message_id", "text", "owner_chat_id"):
            r.pop(key)
        row = self.change()
        row["link_options"] = [{"post_id": "101", "url": "https://x.com/Alice/status/101"}]
        self.deliver()
        self.send.assert_not_called()
        self.limits["群一"] = 50
        self.assertEqual(self.deliver(replace_legacy_groups={"群一"})["群一"], "legacy_reissued")
        self.send.assert_called_once()
        self.assertEqual(len(r["slots"]), 2)
        self.assertIn("已编辑 2 https://x.com/alice/status/404", r["text"])
        self.deliver(replace_legacy_groups={"群一"})
        self.send.assert_called_once()

    def test_legacy_missing_or_ambiguous_origin_stays_unbound(self):
        self.registry["post_entries"][GROUPS["群一"]].pop("101")
        slots = freeze_links(["https://x.com/Alice/status/101"], self.registry,
                             main.expand_chat_id(GROUPS["群一"]), DAY)
        self.assertNotIn("message_id", slots[0])

    def test_overlong_edited_message_is_not_sent_or_committed(self):
        self.change()
        old = copy.deepcopy(self.record())
        with patch("main.format_daily_list_message", return_value="x" * 4097):
            self.assertEqual(self.deliver()["群一"], "message_too_long")
        self.assertEqual(self.record(), old)
        self.edit.assert_not_called()

    def test_next_day_does_not_edit_yesterdays_private_message(self):
        self.change()
        self.deliver(datetime(2026, 9, 13, 0, tzinfo=main.BEIJING))
        self.edit.assert_not_called()


class PrivateDeliveryApiTest(unittest.TestCase):
    def test_send_preserves_message_id(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 777}})
        with patch.object(main, "BOT_TOKEN", "test-token"), patch.object(main.requests, "post", return_value=response, create=True):
            self.assertEqual(main._send_private_message(OWNER, "text"), (True, {"message_id": 777}))

    def test_edit_uses_exact_private_message_and_no_change_response_is_idempotent(self):
        for code, payload, expected in [(200, {"ok": True}, True),
                (400, {"error_code": 400, "description": "Bad Request: message is not modified"}, True),
                (400, {"error_code": 400, "description": "message to edit not found"}, False),
                (429, {"error_code": 429, "description": "Too Many Requests"}, False)]:
            response = SimpleNamespace(status_code=code, json=lambda: payload)
            with patch.object(main, "BOT_TOKEN", "test-token"), patch.object(main.requests, "post", return_value=response, create=True) as post:
                self.assertEqual(main._edit_private_message(OWNER, 777, "text")[0], expected)
                self.assertTrue(post.call_args.args[0].endswith("/editMessageText"))
                self.assertEqual(post.call_args.kwargs["json"]["message_id"], 777)
                self.assertEqual(post.call_args.kwargs["json"]["chat_id"], OWNER)

    def test_reissue_authorization_requires_today_and_valid_group(self):
        with patch.dict("os.environ", {"OWNER_LIST_REISSUE_GROUP": "群一", "OWNER_LIST_REISSUE_DATE": DAY}):
            self.assertEqual(send_daily_lists.legacy_reissue_groups(NOW), {"群一"})
            self.assertEqual(send_daily_lists.legacy_reissue_groups(datetime(2026, 9, 13)), set())

    def test_release_version_matches_changelog(self):
        root = Path(__file__).parent
        version = (root / "VERSION_COUNTER_BOT").read_text().strip()
        self.assertIn(f"## v{version} - ", (root / "CHANGELOG_COUNTER_BOT.md").read_text())


if __name__ == "__main__":
    unittest.main()
