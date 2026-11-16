"""Relay new private bot messages through the existing getUpdates consumer."""
import os
import re
import time
from urllib.parse import urlsplit

import requests

from website_sync import DEFAULT_URL, encode_payload, signed_headers

SYNC_PATH = "/api/sync/intake-dm"
COMMAND = re.compile(r"/(?:start(?:@[a-z0-9_]+)?(?: [a-z0-9_-]{1,64})?|"
                     r"code(?:@[a-z0-9_]+)?|join_(?:base|20k|100k)_[a-f0-9]{16}(?:@[a-z0-9_]+)?)",
                     re.IGNORECASE)
LINK = re.compile(r"""https?://[^\s<>"「」]+""", re.IGNORECASE)
X_HOST = re.compile(r"(?:(?:www|mobile)\.)?(?:x|twitter)\.com", re.IGNORECASE)


def private_envelope(update, token):
    message = update.get("message") or {}
    chat, sender = message.get("chat") or {}, message.get("from") or {}
    uid, stamp, update_id = sender.get("id"), message.get("date"), update.get("update_id")
    if (chat.get("type") != "private" or type(uid) is not int or uid <= 0 or chat.get("id") != uid
            or sender.get("is_bot") is True or type(stamp) is not int or type(update_id) is not int):
        return None
    text = message.get("text") or message.get("caption")
    if not isinstance(text, str) or len(text) > 4096:
        return None
    text = text.strip()
    if text not in ("通行码", "领码") and not COMMAND.fullmatch(text):
        # No unrelated private text is forwarded to the website.
        links = []
        for link in LINK.findall(text):
            try:
                if X_HOST.fullmatch(urlsplit(link).hostname or ""):
                    links.append(link)
            except ValueError:
                continue
        if not links:
            return None
        tier = "".join(text.splitlines()[-1].split())
        text = "\n".join(links + ([tier] if tier in ("基础", "2万", "10万") else []))
    return {"updateId": update_id, "botId": token.split(":")[0], "chatId": str(uid),
            "viewer": str(uid), "date": stamp, "text": text}


def relay_private(payload, *, post=None, secret=None, origin=None, now=None):
    secret = os.getenv("WEBSITE_SYNC_SECRET", "") if secret is None else secret
    origin = (origin or os.getenv("WEBSITE_SYNC_URL") or DEFAULT_URL).rstrip("/")
    try:
        parsed = urlsplit(origin)
        if (len(secret) < 32 or parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.path or parsed.query or parsed.fragment):
            return False
        body = encode_payload(payload)
        headers = signed_headers(secret, SYNC_PATH, body, int(time.time()) if now is None else now,
                                 "intake-dm-" + str(payload["updateId"]))
        response = (post or requests.post)(origin + SYNC_PATH, data=body, headers=headers,
                                           timeout=45, allow_redirects=False)
        return response.status_code == 200 and response.json().get("status") in ("handled", "ignored", "unchanged")
    except (ValueError, AttributeError, requests.RequestException):
        return False
