"""Check the configured new group's original messages, without touching legacy registries."""
import asyncio
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import requests

from sync_deleted_messages import _string_session, deletion_sync_enabled
from website_sync import DEFAULT_URL, encode_payload, signed_headers

PATH = "/api/sync/intake-group"
MAX_REFS = 1800  # Two days, three tiers, up to 300 active entries per tier.
PATCH_SIZE = 20
X_URL = re.compile(r"https?://[^\s<>\"「」]+", re.IGNORECASE)


def has_visible_x_link(message):
    text = getattr(message, "message", None)
    if not isinstance(text, str):
        return None
    urls = X_URL.findall(text)
    urls.extend(url for entity in (getattr(message, "entities", None) or [])
                if isinstance(url := getattr(entity, "url", None), str))
    for url in urls:
        try:
            parsed = urlsplit(url.rstrip("]),.，。；！、"))
        except ValueError:
            continue
        if re.fullmatch(r"(?:(?:www|mobile)\.)?(?:x|twitter)\.com", parsed.netloc, re.IGNORECASE):
            return True
    return False


def website_call(payload, post=None):
    origin = (os.getenv("WEBSITE_SYNC_URL") or DEFAULT_URL).rstrip("/")
    secret = os.getenv("WEBSITE_SYNC_SECRET", "")
    parsed = urlsplit(origin)
    if (len(secret) < 32 or parsed.scheme != "https" or not parsed.netloc or parsed.username
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Invalid website sync configuration")
    body = encode_payload(payload)
    headers = signed_headers(secret, PATH, body, int(time.time()), "intake-delete-" + uuid.uuid4().hex)
    response = (post or requests.post)(origin + PATH, data=body, headers=headers, timeout=20, allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError("Website sync not available")
    return response.json()


async def sync_intake_with_client(client, snapshot, call=website_call):
    if snapshot.get("enabled") is not True:
        return {"checked": 0, "removed": 0}
    chat_id, refs = snapshot.get("chatId"), snapshot.get("refs")
    if (not isinstance(chat_id, str) or not re.fullmatch(r"-[1-9][0-9]{4,18}", chat_id)
            or not isinstance(refs, list) or len(refs) > MAX_REFS):
        raise ValueError("Invalid group reference snapshot")
    if any(not isinstance(ref, dict) or type(ref.get("messageId")) is not int or ref["messageId"] <= 0 for ref in refs):
        raise ValueError("Invalid message reference")
    try:
        entity = await client.get_entity(int(chat_id))
    except ValueError:
        # A fresh StringSession has no cached access hashes; populate them from our chats.
        await client.get_dialogs()
        entity = await client.get_entity(int(chat_id))
    permission = await client.get_permissions(entity, "me")
    if (permission is None or getattr(permission, "has_left", True)
            or getattr(getattr(permission, "participant", None), "left", False)):
        raise RuntimeError("User session is not a group member")
    if not refs:
        return {"checked": 0, "removed": 0}
    ids = sorted({ref["messageId"] for ref in refs})
    removed_messages = set()
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        messages = await client.get_messages(entity, ids=batch)
        if not isinstance(messages, (list, tuple)) or len(messages) != len(batch):
            raise RuntimeError("Incomplete message lookup; no deletion sent")
        for mid, message in zip(batch, messages):
            if message is None:
                removed_messages.add(mid)
            elif getattr(message, "id", None) != mid:
                raise RuntimeError("Message lookup mismatch; no deletion sent")
            elif getattr(message, "edit_date", None) is not None:
                # An X link may be a replacement awaiting review, even if it is not a valid post.
                if has_visible_x_link(message) is False:
                    removed_messages.add(mid)
    deleted = [ref for ref in refs if ref["messageId"] in removed_messages]
    removed = 0
    for start in range(0, len(deleted), PATCH_SIZE):
        patch = deleted[start:start + PATCH_SIZE]
        response = call({"action": "deleted", "chatId": chat_id, "refs": patch})
        count = response.get("removed")
        if (response.get("enabled") is not True or response.get("chatId") != chat_id
                or type(count) is not int or not 0 <= count <= len(patch)):
            raise RuntimeError("Deletion acknowledgement not confirmed")
        removed += count
    return {"checked": len(ids), "removed": removed}


async def async_main():
    snapshot = website_call({"action": "refs"})
    if snapshot.get("enabled") is not True:
        print("新群删除核查：未启用群收录，跳过。")
        return 0
    if not deletion_sync_enabled():
        print("新群删除核查：未配置已有 Telegram 用户会话，本轮保留所有链接。")
        return 1
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    async with TelegramClient(StringSession(_string_session()), int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"]) as client:
        result = await sync_intake_with_client(client, snapshot)
    print(f"新群删除核查完成：核查 {result['checked']} 条，删除 {result['removed']} 条。")
    return 0


def main():
    try:
        return asyncio.run(async_main())
    except Exception:
        print("新群删除核查未完成：不会用查询失败或不完整的结果判定删除。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
