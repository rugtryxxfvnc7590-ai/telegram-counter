"""Read the existing group's durable webhook inbox without changing its polling schedule."""
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import requests

from website_sync import DEFAULT_URL, encode_payload, signed_headers

BRIDGE_PATH = "/api/sync/telegram-inbox"
KEY = re.compile(r"\d{16}:\d{10}")


def polled_group_key(update, chat_ids):
    message = update.get("edited_message") or update.get("message") or {}
    chat = message.get("chat") or {}
    if str(chat.get("id")) not in chat_ids or not isinstance(message.get("text"), str):
        return None
    stamp = message.get("edit_date") or message.get("date")
    update_id = update.get("update_id")
    if type(stamp) is not int or type(update_id) is not int:
        return None
    key = f"{update_id:016d}:{stamp}"
    return key if KEY.fullmatch(key) else None


def pull_group_inbox(chat_ids, ack, token, *, secret=None, origin=None, post=None):
    secret = os.getenv("WEBSITE_SYNC_SECRET", "") if secret is None else secret
    origin = (origin or os.getenv("WEBSITE_SYNC_URL") or DEFAULT_URL).rstrip("/")
    try:
        parsed = urlsplit(origin)
        if (len(secret) < 32 or parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.path or parsed.query or parsed.fragment):
            return None
        body = encode_payload({"chatIds": chat_ids, "ack": ack, "botId": token.split(":")[0]})
        headers = signed_headers(secret, BRIDGE_PATH, body, int(time.time()), "telegram-inbox-" + uuid.uuid4().hex)
        response = (post or requests.post)(origin + BRIDGE_PATH, data=body, headers=headers,
                                           timeout=20, allow_redirects=False)
        if response.status_code != 200:
            return None
        events = response.json().get("events")
        if not isinstance(events, list) or len(events) > 100:
            return None
        rows = []
        for item in events:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not KEY.fullmatch(item["key"]):
                return None
            if not isinstance(item.get("update"), dict) or type(item["update"].get("update_id")) is not int:
                return None
            rows.append((item["key"], item["update"]))
        return rows
    except (ValueError, AttributeError, requests.RequestException):
        return None
