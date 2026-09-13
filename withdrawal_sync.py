"""Confirmed same-day withdrawals and bounded refill of published owner lists."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_capacity import chat_ids_for_group, eligible_rows, group_for_chat
from private_list_sync import freeze_links

BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_now(now=None):
    now = now or datetime.now(BEIJING)
    return now.replace(tzinfo=BEIJING) if now.tzinfo is None else now.astimezone(BEIJING)


def note_withdrawals(registry, chat_id, reasons, now=None):
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    group = group_for_chat(chat_id)
    if now.hour >= 19 or registry.get("date") != day or not group or not reasons:
        return
    ledger = registry.setdefault("confirmed_withdrawals", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    entries = ledger["groups"].setdefault(group, {})
    for mid, reason in sorted(reasons.items()):
        if str(mid).isdigit() and int(mid) > 0:
            entries.setdefault(str(mid), {"reason": reason, "confirmed_at": now.strftime("%Y-%m-%d %H:%M:%S")})


def snapshot_withdrawals(registry, state, chat_id, messages, day, contains_post, now=None):
    """Called only after a complete, successfully parsed Telegram group snapshot."""
    known = set()
    for cid in chat_ids_for_group(group_for_chat(chat_id)):
        for bucket in ("entries", "post_entries"):
            for entry in ((registry.get(bucket) or {}).get(cid) or {}).values():
                if str(entry.get("time") or "")[:10] == day:
                    known.add(str(entry.get("message_id") or ""))
    published = state.get("owner_daily_lists") or {}
    if published.get("date") == day:
        record = (published.get("groups") or {}).get(group_for_chat(chat_id)) or {}
        for slot in list(record.get("slots") or []) + list((record.get("pending_roster") or {}).get("slots") or []):
            if str(slot.get("time") or "")[:10] == day:
                known.add(str(slot.get("message_id") or ""))
    current = {str(msg.get("message_id")): msg for msg in messages}
    reasons = {}
    for mid in known:
        if mid not in current:
            reasons[mid] = "message_deleted_in_complete_snapshot"
        elif not contains_post(str(current[mid].get("text") or "")):
            reasons[mid] = "post_links_removed_from_message"
    note_withdrawals(registry, chat_id, reasons, now)


def plan_roster(record, registry, chat_id, day, now_text, limit):
    pending = record.get("pending_roster") or {}
    slots = deepcopy(pending.get("slots", record.get("slots") or []))
    retired = set(pending.get("withdrawn_message_ids", record.get("withdrawn_message_ids") or []))
    capacity = pending.get("capacity", record.get("roster_capacity", record.get("count", len(slots))))
    original = deepcopy(slots)
    if now_text[:10] == day and now_text[11:16] < "19:00":
        ledger = registry.get("confirmed_withdrawals") or {}
        confirmations = (ledger.get("groups") or {}).get(group_for_chat(chat_id)) or {}
        if ledger.get("date") == day:
            for slot in slots:
                mid = str(slot.get("message_id") or "")
                stamp = str((confirmations.get(mid) or {}).get("confirmed_at") or "")
                try:
                    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if stamp[:10] == day and stamp[11:16] < "19:00" and stamp <= now_text:
                    retired.add(mid)
            slots = [slot for slot in slots if str(slot.get("message_id") or "") not in retired]
        target = min(capacity, limit) if limit else capacity
        if retired and len(slots) < target:
            used_messages = {str(slot.get("message_id") or "") for slot in slots} | retired
            used_posts = {str(slot.get("post_id") or "") for slot in slots}
            rows = eligible_rows(registry, chat_ids_for_group(group_for_chat(chat_id)), day)
            for row in rows:
                mid = str(row["message_id"])
                if (not row["message_id"] or mid in used_messages or row["post_id"] in used_posts
                        or row["time"] > now_text):
                    continue
                bound = freeze_links([row["url"]], registry, chat_ids_for_group(group_for_chat(chat_id)), day)[0]
                if bound.get("message_id") != mid:
                    continue
                slots.append(bound)
                used_messages.add(mid)
                used_posts.add(row["post_id"])
                if len(slots) >= target:
                    break
        if slots != original:
            slots.sort(key=lambda s: (s.get("time", ""), int(s.get("message_id") or 0), s["url"]), reverse=True)
    return {"slots": slots, "capacity": capacity, "withdrawn_message_ids": sorted(retired)}
