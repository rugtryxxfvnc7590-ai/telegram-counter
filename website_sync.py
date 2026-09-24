"""Authenticated, idempotent snapshots of the fixed daily public rosters."""
import hashlib
import hmac
import json
import os
from urllib.parse import urlsplit

import requests

from website_deletions import deleted_entries, website_items
from withdrawal_sync import beijing_now

DEFAULT_URL = "https://daily-links.guerridodominique142615.workers.dev"
GROUPS = {"群一": ("group-1", "群一·10万以上大佬群"),
          "群二": ("group-2", "群二·2-5w粉"), "群三": ("group-3", "群三·0-2w粉")}


def encode_payload(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def signed_headers(secret, path, body, timestamp, request_id):
    stamp = str(timestamp)
    canonical = "\n".join(("POST", path, stamp, request_id, hashlib.sha256(body).hexdigest()))
    return {"Content-Type": "application/json", "X-Sync-Timestamp": stamp,
            "X-Sync-Request-Id": request_id,
            "X-Sync-Signature": hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()}


def sync_website(state, now=None, save_callback=None, secret=None, origin=None, post=None):
    fixed_now = now
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    source = state.get("daily_rosters") or {}
    if source.get("date") != day:
        return {}
    if not 14 <= now.hour < 19 and not any(deleted_entries(state, group, day) for group in GROUPS):
        return {}
    secret = secret if secret is not None else os.getenv("WEBSITE_SYNC_SECRET", "")
    if not secret:
        print("网站同步：未配置 WEBSITE_SYNC_SECRET，跳过本轮。")
        return {}
    origin = (origin or os.getenv("WEBSITE_SYNC_URL") or DEFAULT_URL).rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("WEBSITE_SYNC_URL 必须是 HTTPS 网站根地址")
    post = post or requests.post
    ledger = state.setdefault("website_sync", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    results = {}
    for group, (group_id, name) in GROUPS.items():
        record = (source.get("groups") or {}).get(group)
        if record is None:
            continue
        send_time = beijing_now(fixed_now)
        if send_time.strftime("%Y-%m-%d") != day:
            break
        deletions = deleted_entries(state, group, day)
        payload = {"date": day, "groupName": name}
        if 14 <= send_time.hour < 19:
            payload["items"] = website_items(record, deletions)
        else:
            if not deletions:
                continue
            # Frozen hours cannot introduce or replace a link, even if an
            # admission snapshot changed while a request was in flight.
            payload.update(mode="deletions", deletions=[
                {key: entry[key] for key in ("position", "account", "postId")}
                for _, entry in sorted(deletions.items())])
        digest = hashlib.sha256(encode_payload(payload)).hexdigest()
        previous = ledger["groups"].get(group) or {}
        if previous.get("digest") != digest:
            previous = {"digest": digest, "sourceRevision": int(previous.get("sourceRevision") or 0) + 1}
            ledger["groups"][group] = previous
            if save_callback:
                save_callback()
        if previous.get("acknowledged"):
            results[group] = "unchanged"
            continue
        revision = previous["sourceRevision"]
        payload["sourceRevision"] = revision
        body = encode_payload(payload)
        path = f"/api/sync/groups/{group_id}"
        request_id = f"{day}-{group_id}-{revision}-{digest[:16]}"
        headers = signed_headers(secret, path, body, int(send_time.timestamp()), request_id)
        try:
            response = post(origin + path, data=body, headers=headers, timeout=20)
            data = response.json()
            if response.status_code == 200 and data.get("status") in {"updated", "unchanged"}:
                previous.update(acknowledged=True, acknowledged_at=send_time.isoformat())
                previous.pop("last_error", None)
                outcome = data["status"]
            elif response.status_code == 423:
                outcome = "frozen"
            else:
                outcome = f"http_{response.status_code}"
        except Exception as exc:
            # Never log a request object, authentication headers, or response bodies.
            outcome = type(exc).__name__
        if not previous.get("acknowledged"):
            previous["last_error"] = outcome
        if save_callback:
            save_callback()
        results[group] = outcome
        print(f"网站同步：{group} {day} 第 {revision} 版 {outcome}。")
    return results
