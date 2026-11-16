import json
import unittest
from unittest.mock import Mock, patch

import main
from community_mentions import validate_policy
from private_intake import SYNC_PATH, private_envelope, relay_private
from website_sync import signed_headers


def update(update_id, text, chat_type="private", uid=9002, chat_id=9002):
    return {"update_id": update_id, "message": {
        "from": {"id": uid, "is_bot": False}, "chat": {"id": chat_id, "type": chat_type},
        "date": 1794780000, "text": text,
    }}


class PrivateIntakeTest(unittest.TestCase):
    def test_relay_only_private_commands_and_x_links(self):
        self.assertIsNone(private_envelope(update(12, "/code", "supergroup"), "9001:example"))
        self.assertIsNone(private_envelope(update(12, "/code", chat_id=9003), "9001:example"))
        self.assertIsNone(private_envelope(update(12, "unrelated private conversation"), "9001:example"))
        self.assertIsNone(private_envelope(update(12, "https://example.com/x.com/post"), "9001:example"))
        self.assertIsNone(private_envelope({"update_id": 12, "edited_message": update(12, "/code")["message"]}, "9001:example"))
        self.assertEqual(private_envelope(update(12, "/code"), "9001:example")["text"], "/code")
        body = private_envelope(update(13, "ignored description https://x.com/main/status/1\nhttps://x.com/return\n10 万"), "9001:example")
        self.assertEqual(body, {"updateId": 13, "botId": "9001", "chatId": "9002",
                                "viewer": "9002", "date": 1794780000, "text": "https://x.com/main/status/1\nhttps://x.com/return\n10万"})

    def test_signed_private_request_does_not_follow_redirects(self):
        body = private_envelope(update(12, "/code"), "9001:example")
        secret = "test-signed-dm-secret-at-least-thirty-two"
        origin = "https://rosters.example"
        post = Mock(return_value=Mock(status_code=200, json=lambda: {"status": "ignored"}))
        self.assertTrue(relay_private(body, post=post, origin=origin, secret=secret, now=1794780000))
        args, kwargs = post.call_args
        self.assertEqual(args, (origin + SYNC_PATH,))
        self.assertEqual(json.loads(kwargs["data"]), body)
        self.assertEqual(kwargs["headers"], signed_headers(secret, SYNC_PATH, kwargs["data"], 1794780000, "intake-dm-12"))
        self.assertFalse(kwargs["allow_redirects"])
        post.return_value.status_code = 302
        self.assertFalse(relay_private(body, post=post, origin=origin, secret=secret))

    def test_dm_poll_failure_preserves_first_unconfirmed_offset(self):
        state = {"offset": 5}
        data = {"ok": True, "result": [update(6, "/code"), update(7, "/code"), update(8, "/code")]}
        with (patch("community_mentions.local_policy", return_value={"privateIntake": True, "websiteOnly": True}),
              patch.object(main, "BOT_TOKEN", "9001:example"),
              patch.object(main, "load_state", return_value=state),
              patch.object(main, "load_registry", return_value={}),
              patch.object(main, "load_reply_rules", return_value={}),
              patch.object(main.requests, "get", return_value=Mock(json=lambda: data), create=True) as get_updates,
              patch("private_intake.relay_private", side_effect=[True, False]) as relay,
              patch.object(main, "save_state") as save_state,
              patch.object(main, "save_registry") as save_registry):
            main.main()
        self.assertEqual(state["offset"], 6)
        save_state.assert_called_once_with(state)
        save_registry.assert_not_called()
        self.assertEqual(relay.call_count, 2)
        self.assertEqual(get_updates.call_args.kwargs["params"]["offset"], 6)

    def test_trial_switch_leaves_legacy_independent(self):
        policy = {"handles": ["ABC"], "revision": 0, "minimum": 1, "privateIntake": True}
        self.assertTrue(validate_policy(policy)["privateIntake"])
        self.assertNotIn("websiteOnly", validate_policy(policy))
        with self.assertRaises(ValueError):
            validate_policy({**policy, "privateIntake": "true"})
