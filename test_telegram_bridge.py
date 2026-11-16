import json
import unittest
from unittest.mock import Mock, patch

import main
from telegram_bridge import polled_group_key, pull_group_inbox

CHAT = "-1003891628675"
TOKEN = "9001:example"
STAMP = 1794780000


def edited(update_id):
    return {"update_id": update_id, "edited_message": {"message_id": 21, "chat": {"id": int(CHAT)},
                                                       "date": STAMP-60, "edit_date": STAMP, "text": ""}}


class TelegramBridgeTest(unittest.TestCase):
    def test_exact_receipt_includes_edit_time_and_preserves_signed_shape(self):
        update = edited(12)
        key = polled_group_key(update, [CHAT])
        self.assertEqual(key, "0000000000000012:1794780000")
        self.assertIsNone(polled_group_key(update, ["-1000012345678"]))
        post = Mock(return_value=Mock(status_code=200, json=lambda: {"events": [{"key": key, "update": update}]}))
        args = {"post": post, "secret": "fixture-key-at-least-thirty-two-long", "origin": "https://rosters.example"}
        self.assertEqual(pull_group_inbox([CHAT], [key], TOKEN, **args), [(key, update)])
        request = post.call_args.kwargs
        self.assertEqual(json.loads(request["data"]), {"chatIds": [CHAT], "ack": [key], "botId": "9001"})
        self.assertFalse(request["allow_redirects"])
        post.return_value.status_code = 302
        self.assertIsNone(pull_group_inbox([CHAT], [key], TOKEN, **args))

    def test_inbox_uses_original_edit_path_and_never_sets_telegram_offset(self):
        seen, new = edited(12), edited(13)
        old_key, new_key = [polled_group_key(item, [CHAT]) for item in (seen, new)]
        state = {"offset": 12, "telegram_polled_group_keys": [old_key], "telegram_bridge_ack": [old_key]}
        with (patch("community_mentions.local_policy", return_value={"telegramInbox": True, "privateIntake": True}),
              patch.object(main, "BOT_TOKEN", TOKEN), patch.object(main, "load_chat_ids", return_value=[CHAT]),
              patch.object(main, "load_state", return_value=state), patch.object(main, "load_registry", return_value={}),
              patch.object(main, "load_reply_rules", return_value={}), patch.object(main, "backfill_registry_metadata", return_value=0),
              patch("telegram_bridge.pull_group_inbox", return_value=[(old_key, seen), (new_key, new)]) as pull,
              patch.object(main.requests, "get", create=True) as get_updates,
              patch.object(main, "remove_registry_rows_if_edited_message_lost_links", return_value=[]) as remove,
              patch.object(main, "save_state") as save, patch.object(main, "save_registry")):
            main.main()
        pull.assert_called_once_with([CHAT], [old_key], TOKEN)
        get_updates.assert_not_called()
        remove.assert_called_once_with({}, CHAT, 21, "", True)
        save.assert_called_once_with(state)
        self.assertEqual(state["offset"], 12)
        self.assertEqual(state["telegram_bridge_ack"], [old_key, new_key])

    def test_preactivation_polls_and_records_receipts_for_handoff(self):
        state = {"offset": 11}
        data = {"ok": True, "result": [edited(12)]}
        with (patch("community_mentions.local_policy", return_value={"telegramInbox": True}),
              patch.object(main, "BOT_TOKEN", TOKEN), patch.object(main, "load_chat_ids", return_value=[CHAT]),
              patch.object(main, "load_state", return_value=state), patch.object(main, "load_registry", return_value={}),
              patch.object(main, "load_reply_rules", return_value={}), patch.object(main, "backfill_registry_metadata", return_value=0),
              patch("telegram_bridge.pull_group_inbox", return_value=[]),
              patch.object(main.requests, "get", return_value=Mock(json=lambda: data), create=True) as get_updates,
              patch.object(main, "remove_registry_rows_if_edited_message_lost_links", return_value=[]),
              patch.object(main, "save_state"), patch.object(main, "save_registry")):
            main.main()
        self.assertEqual(get_updates.call_args.kwargs["params"]["offset"], 12)
        self.assertEqual(state["offset"], 12)
        self.assertEqual(state["telegram_polled_group_keys"], [polled_group_key(data["result"][0], [CHAT])])
