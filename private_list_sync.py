"""Frozen owner-list slots: only the original message/account can replace a URL."""
from copy import deepcopy
from datetime import datetime
import re

POST_URL = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)(?:[?#].*)?$", re.I)


def post_identity(url):
    match = POST_URL.fullmatch(str(url or "").strip())
    return (match[1].lower(), match[2]) if match else ("", "")


def current_rows(registry, chat_ids, day):
    rows = []
    for cid in sorted(str(cid) for cid in chat_ids):
        for post_id, entry in (((registry or {}).get("post_entries") or {}).get(cid) or {}).items():
            if str(entry.get("promo_post_id") or "") != str(post_id):
                continue
            when = str(entry.get("time") or "")
            if when[:10] != day or when[11:16] >= "19:00":
                continue
            handle = str(entry.get("promo_handle") or "").strip().lstrip("@").lower()
            url = f"https://x.com/{handle}/status/{post_id}"
            if not all(post_identity(url)):
                continue
            rows.append({"handle": handle, "post_id": str(post_id), "url": url,
                         "message_id": str(entry.get("message_id") or ""),
                         "tg_user_id": str(entry.get("tg_user_id") or ""),
                         "time": when, "entry": entry})
    return rows


def freeze_links(links, registry, chat_ids, day):
    """Bind an already selected list, including legacy URLs retained in edit history."""
    rows = current_rows(registry, chat_ids, day)
    slots = []
    for url in links:
        handle, post_id = post_identity(url)
        matches = []
        for row in rows:
            old_ids = {str(option.get("post_id") or "") for option in row["entry"].get("link_options") or []
                       if post_identity(option.get("url"))[0] == handle}
            if row["handle"] == handle and (row["post_id"] == post_id or post_id in old_ids):
                matches.append(row)
        bindings = {(r["message_id"], r["tg_user_id"], r["time"]) for r in matches}
        slot = {"handle": handle, "post_id": post_id, "original_post_id": post_id,
                "url": url, "edited": False}
        if len(bindings) == 1:
            slot.update(zip(("message_id", "tg_user_id", "time"), next(iter(bindings))))
        slots.append(slot)
    return slots


def matching_edit_rows(slot, rows, day, now_text):
    if not slot.get("message_id"):
        return
    for row in rows:
        entry = row["entry"]
        if (row["handle"] != slot["handle"] or row["message_id"] != slot["message_id"]
                or row["time"] != slot.get("time")
                or (slot.get("tg_user_id") and row["tg_user_id"] != slot["tg_user_id"])):
            continue
        edit_time = str(entry.get("edit_time") or "")
        try:
            datetime.strptime(edit_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (not entry.get("edited") or edit_time[:10] != day or edit_time > now_text
                or edit_time < slot.get("edit_time", "")):
            continue
        yield row, edit_time


def edited_slots(slots, registry, chat_ids, day, now_text):
    rows = current_rows(registry, chat_ids, day)
    updated = deepcopy(slots)
    for slot in updated:
        matches = [(edit_time, row["post_id"], row["url"])
                   for row, edit_time in matching_edit_rows(slot, rows, day, now_text)
                   if row["entry"].get("mutual_eligible") is True and not row["entry"].get("after_cutoff")]
        if not matches:
            continue
        latest_time = max(item[0] for item in matches)
        latest = {item for item in matches if item[0] == latest_time}
        if len(latest) != 1:
            continue
        edit_time, post_id, url = next(iter(latest))
        if post_id != slot["post_id"]:
            slot.update(post_id=post_id, url=url, edited=True, edit_time=edit_time)
    return updated
