"""Offline policy changes: never contact Telegram or use live group records."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from community_mentions import DEFAULT_HANDLES, POLICY_PATH_ENV, SYNC_PATH, fetch_policy, load_target_mentions, save_policy, validate_policy
from daily_capacity import GROUP_CHAT_IDS


def policy(handles=None, revision=1):
    return {"handles": handles or ["newone", "newtwo"], "minimum": 2, "revision": revision, "updatedAt": None}


class CommunityMentionsTest(unittest.TestCase):
    def test_defaults_preserve_offline_and_legacy_callers(self):
        with patch.dict(os.environ, {POLICY_PATH_ENV: ""}):
            self.assertEqual(load_target_mentions(), DEFAULT_HANDLES)

    def test_validation_rejects_invalid_or_duplicate_policy(self):
        self.assertEqual(validate_policy(policy(["NewOne", "NewTwo"]))["handles"], ["newone", "newtwo"])
        for value in [None, [], {}, policy(["one"]), policy(["one", "ONE"]), policy(["@one", "two"]),
                      policy(["bad-name", "two"]), policy(revision=True), policy(revision=-1),
                      dict(policy(), minimum=1), dict(policy(), minimum=True),
                      policy(["account"+str(i) for i in range(21)])]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_policy(value)

    def test_signed_fetch_and_atomic_snapshot_used_by_separate_steps(self):
        secret = "test-community-sync-secret-more-than-32"
        def post(url, *, data, headers, timeout, allow_redirects):
            self.assertEqual(url, "https://example.com"+SYNC_PATH)
            self.assertEqual(timeout, 20); self.assertFalse(allow_redirects)
            canonical = "\n".join(("POST", SYNC_PATH, "1234567890", "test", hashlib.sha256(data).hexdigest()))
            self.assertEqual(headers["X-Sync-Signature"], hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest())
            return SimpleNamespace(status_code=200, json=lambda: policy())
        result = fetch_policy(secret, "https://example.com", post=post, timestamp=1234567890, request_id="test")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"snapshot.json"
            save_policy(path, result)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            env = dict(os.environ, **{POLICY_PATH_ENV: str(path)})
            for _ in range(3):
                run = subprocess.run([sys.executable, "-c", "import main,json; print(json.dumps(main.TARGET_MENTIONS))"],
                                     cwd=Path(__file__).parent, env=env, capture_output=True, text=True, check=True)
                self.assertEqual(json.loads(run.stdout), ["newone", "newtwo"])

    def test_failures_never_silently_fall_back_to_old_accounts(self):
        secret = "test-community-sync-secret-more-than-32"
        for status in (302, 401, 404, 503):
            with self.subTest(status=status), self.assertRaises(ValueError):
                fetch_policy(secret, post=lambda *args, **kwargs: SimpleNamespace(status_code=status))
        with self.assertRaises(ValueError):
            fetch_policy("", post=lambda *args, **kwargs: self.fail("Network must not be called"))
        for origin in ["http://example.com", "https://example.com/path", "https://user@example.com"]:
            with self.assertRaises(ValueError):
                fetch_policy(secret, origin, post=lambda *args, **kwargs: self.fail("Invalid origin"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {POLICY_PATH_ENV: str(Path(directory)/"missing.json")}):
            with self.assertRaises(FileNotFoundError):
                load_target_mentions()

    def test_all_groups_use_new_accounts_recount_stale_cache_and_ignore_return_mentions(self):
        with patch.object(main, "TARGET_MENTIONS", ("newone", "newtwo")):
            for chat_id in GROUP_CHAT_IDS.values():
                for text, stale, expected in [("@NEWONE @newtwo", 0, True), ("@ToBulaer @ToBuerma", 4, False),
                                              ("@newone @newone", 4, False), ("@newone_more @newtwo", 4, False)]:
                    with self.subTest(chat_id=chat_id, text=text):
                        links = [{"role": "promo", "tweet_text": text, "required_mentions_count": stale},
                                 {"role": "check", "tweet_text": "@newone @newtwo", "required_mentions_count": 2}]
                        self.assertEqual(main.promo_link_content_eligible(links), expected)
                        self.assertEqual(main.promo_link_missing_required_mentions(links), not expected)
                        entry = {"chat_id": chat_id, "tweet_text": text, "x_name": "Member", "followers_count": 200000,
                                 "followers_sources": ["profile_v2"], "required_mentions_count": stale}
                        with patch("main.fetch_x_author_meta", return_value={}):
                            main.enrich_entry_metadata(entry)
                        self.assertEqual(entry["content_eligible"], expected)
                        self.assertEqual(entry["mutual_eligible"], expected)
                        self.assertEqual(entry["required_mentions_count"], main.count_required_mentions(text))

    def test_long_text_unknown_protection_still_applies_under_new_policy(self):
        with patch.object(main, "TARGET_MENTIONS", ("newone", "newtwo")):
            for text in ("", "正文未全部返回…", "正文"*100):
                links = [{"role": "promo", "tweet_text": text, "required_mentions_count": 2}]
                self.assertFalse(main.promo_link_missing_required_mentions(links))
                self.assertIsNone(main.promo_link_content_eligible(links))
            self.assertTrue(main.promo_link_content_eligible([{"role": "promo", "tweet_text": "正文"*100+" @newone @newtwo"}]))

    def test_workflow_fetches_policy_before_any_telegram_steps_without_changing_schedule(self):
        workflow = Path(__file__).with_name(".github").joinpath("workflows/run.yml").read_text()
        self.assertIn("- cron: '*/15 * * * *'", workflow)
        self.assertIn("- cron: '5,10 11 * * *'", workflow)
        fetch_at = workflow.index('python community_mentions.py --output')
        for command in ('run: python main.py', 'run: python sync_group_messages.py', 'run: python send_daily_lists.py'):
            self.assertLess(fetch_at, workflow.index(command))
        self.assertIn('COMMUNITY_MENTIONS_FILE=$RUNNER_TEMP/community-mentions.json', workflow)
        self.assertIn('>> "$GITHUB_ENV"', workflow)


if __name__ == "__main__":
    unittest.main()
