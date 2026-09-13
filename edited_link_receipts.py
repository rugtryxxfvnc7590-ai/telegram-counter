"""Group edit receipts derived only from acknowledged private-list contents."""
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_capacity import chat_ids_for_group, group_for_chat
from private_list_sync import current_rows, matching_edit_rows

BEIJING = ZoneInfo("Asia/Shanghai")
SUCCESS_TEXT = "更换的新链接已收录"
REJECTED_TEXT = "此编辑后的新链接违规，不予收录，已收录原本旧链接"
VIOLATION_REASONS = {"missing_required_mentions", "followers_below_minimum"}


def _now(value=None):
    value = value or datetime.now(BEIJING)
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def _published_slots(state, group, day, owner=None):
    from main import format_daily_list_message

    delivery = state.get("owner_daily_lists") or {}
    record = (delivery.get("groups") or {}).get(group) or {}
    if (delivery.get("date") != day or not record.get("sent") or not record.get("message_id")
            or not str(record.get("owner_chat_id") or "").isdigit()
            or (owner is not None and record.get("owner_chat_id") != owner)):
        return []
    slots = record.get("slots") or []
    links = [slot["url"] for slot in slots]
    indices = [i for i, slot in enumerate(slots, 1) if slot.get("edited")]
    if (len(slots) != record.get("count") or links != record.get("links")
            or record.get("text") != format_daily_list_message(group, day, links, indices)):
        return []
    return slots


def _latest_edit(slot, rows, day, now_text):
    matches = list(matching_edit_rows(slot, rows, day, now_text))
    if not matches:
        return None
    latest_time = max(t for _, t in matches)
    latest = [row for row, t in matches if t == latest_time]
    identities = {(row["post_id"], row["url"], row["entry"].get("mutual_eligible"),
                   row["entry"].get("ineligible_reason"), bool(row["entry"].get("after_cutoff")))
                  for row in latest}
    return (latest[0], latest_time) if len(identities) == 1 else None


def defer_edited_link_reply(registry, state, chat_id, message_id, now=None):
    """Admitted post edits use one dedicated reply, not an extra generic violation."""
    now = _now(now)
    day = now.strftime("%Y-%m-%d")
    group = group_for_chat(chat_id)
    if not group or registry.get("date") != day:
        return False
    rows = current_rows(registry, chat_ids_for_group(group), day)
    for slot in _published_slots(state, group, day):
        if slot.get("message_id") != str(message_id):
            continue
        candidate = _latest_edit(slot, rows, day, now.strftime("%Y-%m-%d %H:%M:%S"))
        if candidate and (candidate[0]["post_id"] != slot["post_id"] or slot.get("edited")):
            return True
    return False


def process_edited_link_receipts(registry, state, send_reply, owner_chat_id, now=None, save_callback=None):
    fixed_now = now
    now = _now(now)
    day, now_text = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
    snapshot = state.get("group_snapshot_sync") or {}
    owner = str(owner_chat_id or "").strip()
    if (now.hour >= 19 or registry.get("date") != day or snapshot.get("date") != day
            or not owner.isdigit() or int(owner) <= 0):
        return {}
    ledger = state.setdefault("edited_link_receipts", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    results = {}
    for group in snapshot.get("completed_groups") or []:
        if group not in {"群一", "群二", "群三"}:
            continue
        chat_ids = chat_ids_for_group(group)
        chat_id = next(cid for cid in chat_ids if cid.startswith("-100"))
        rows = current_rows(registry, chat_ids, day)
        sent = ledger["groups"].setdefault(group, {})
        for slot in _published_slots(state, group, day, owner):
            candidate = _latest_edit(slot, rows, day, now_text)
            if not candidate:
                continue
            row, edit_time = candidate
            entry = row["entry"]
            if entry.get("after_cutoff"):
                continue
            if (slot.get("edited") and slot["post_id"] == row["post_id"]
                    and entry.get("mutual_eligible") is True):
                outcome, text = "synced", SUCCESS_TEXT
                revision = slot.get("edit_time")
            elif (row["post_id"] != slot["post_id"] and entry.get("mutual_eligible") is False
                  and VIOLATION_REASONS.intersection(str(entry.get("ineligible_reason") or "").split(","))):
                outcome, text, revision = "rejected", REJECTED_TEXT, edit_time
            else:
                continue
            if not revision:
                continue
            message_id = row["message_id"]
            key = f"{message_id}:{row['post_id']}:{revision}:{outcome}"
            previous = sent.get(message_id) or {}
            # Caption-only edits to the same rejected post need no second warning.
            if previous.get("key") == key or (outcome == "rejected"
                    and previous.get("outcome") == outcome and previous.get("post_id") == row["post_id"]
                    and previous.get("retained_post_id") == slot["post_id"]):
                continue
            send_time = _now(fixed_now)
            if send_time.hour >= 19 or send_time.strftime("%Y-%m-%d") != day:
                return results
            try:
                ok = send_reply(chat_id, int(message_id), text)
            except Exception:
                ok = False
            if not ok:
                results[f"{group}:{message_id}"] = "failed"
                continue
            sent[message_id] = {"key": key, "post_id": row["post_id"], "outcome": outcome,
                                "retained_post_id": slot["post_id"],
                                "sent_at": send_time.strftime("%Y-%m-%d %H:%M:%S")}
            if save_callback:
                save_callback()
            results[f"{group}:{message_id}"] = outcome
            print(f"换帖回执：{group} 原消息 {message_id} {outcome} 已回复。")
    return results
