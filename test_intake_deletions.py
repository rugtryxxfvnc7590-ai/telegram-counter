import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sync_intake_deletions as module

CHAT = "-1004454689998"


def snapshot():
    return {"enabled": True, "chatId": CHAT, "refs": [
        {"date": "2026-11-23", "messageId": 101, "postId": "1234", "tier": "base"},
        {"date": "2026-11-23", "messageId": 102, "postId": "1235", "tier": "20k"},
        {"date": "2026-11-23", "messageId": 103, "postId": "1236", "tier": "100k"},
    ]}


class IntakeDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_original_message_or_remove_links_without_touching_remaining_posts(self):
        client = SimpleNamespace(get_entity=AsyncMock(return_value="entity"),
            get_permissions=AsyncMock(return_value=SimpleNamespace(has_left=False)),
            get_messages=AsyncMock(return_value=[
                SimpleNamespace(id=101, message="https://x.com/person/status/1234"),
                None, SimpleNamespace(id=103, message="https://x.com/return/status/5678", edit_date=1),
            ]))
        call = Mock(return_value={"enabled": True, "chatId": CHAT, "removed": 2})
        result = await module.sync_intake_with_client(client, snapshot(), call)
        self.assertEqual(result, {"checked": 3, "removed": 2})
        call.assert_called_once_with({"action": "deleted", "chatId": CHAT, "refs": snapshot()["refs"][1:]})
        client.get_entity.assert_awaited_once_with(int(CHAT))
        client.get_messages.assert_awaited_once_with("entity", ids=[101, 102, 103])

    async def test_preserve_url_variants_and_unverified_edits(self):
        client = SimpleNamespace(get_entity=AsyncMock(side_effect=[ValueError("not cached"), "entity"]),
            get_dialogs=AsyncMock(), get_permissions=AsyncMock(return_value=SimpleNamespace(has_left=False)),
            get_messages=AsyncMock(return_value=[
                SimpleNamespace(id=101, message="see: https://mobile.x.com/i/status/1234?s=46.", edit_date=1),
                SimpleNamespace(id=102, message="embedded link", edit_date=1,
                                entities=[SimpleNamespace(url="https://twitter.com/i/web/status/1235")]),
                SimpleNamespace(id=103, message=None, edit_date=1),
            ]))
        call = Mock()
        self.assertEqual(await module.sync_intake_with_client(client, snapshot(), call), {"checked": 3, "removed": 0})
        call.assert_not_called()
        client.get_dialogs.assert_awaited_once()

    async def test_network_error_incomplete_data_or_lost_membership_never_deletes(self):
        for messages, has_left in [(None, False), ([], False), ([None, None], False),
                                    ([SimpleNamespace(id=999), None, None], False), ([None]*3, True)]:
            with self.subTest(messages=messages, has_left=has_left):
                client = SimpleNamespace(get_entity=AsyncMock(return_value="entity"),
                    get_permissions=AsyncMock(return_value=SimpleNamespace(has_left=has_left)),
                    get_messages=AsyncMock(return_value=messages))
                call = Mock()
                with self.assertRaises(RuntimeError):
                    await module.sync_intake_with_client(client, snapshot(), call)
                call.assert_not_called()

    async def test_disabled_or_empty_avoids_telegram_and_keeps_other_groups_separate(self):
        client = SimpleNamespace(get_entity=AsyncMock(return_value="entity"),
            get_permissions=AsyncMock(return_value=SimpleNamespace(has_left=False)), get_messages=AsyncMock())
        self.assertEqual(await module.sync_intake_with_client(client, {"enabled": False}), {"checked": 0, "removed": 0})
        self.assertEqual(await module.sync_intake_with_client(client, {**snapshot(), "refs": []}), {"checked": 0, "removed": 0})
        client.get_messages.assert_not_awaited()
        client.get_permissions.assert_awaited_once_with("entity", "me")
