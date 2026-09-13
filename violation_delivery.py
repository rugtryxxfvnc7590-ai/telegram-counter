"""Delivery and snapshot catch-up for the existing group violation rules."""
from datetime import datetime, timedelta

import main
from daily_capacity import chat_ids_for_group, group_for_chat
from private_list_sync import current_rows


def _now(value=None):
    value = value or datetime.now(main.BEIJING)
    return value.replace(tzinfo=main.BEIJING) if value.tzinfo is None else value.astimezone(main.BEIJING)


def deliver_violation_reply(state, chat_id, message_id, reason, rules, now=None, save_callback=None):
    now = _now(now)
    day = now.strftime("%Y-%m-%d")
    group = group_for_chat(chat_id)
    if (not group or reason not in {"low_followers", "missing_mentions"}
            or now.hour >= 19 or state.get("date") != day
            or not str(message_id).isdigit() or int(message_id) <= 0
            or not main.reply_rule_enabled(rules, reason, chat_id)):
        return "skipped"
    ledger = state.setdefault("violation_replies", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    records = ledger.setdefault("groups", {}).setdefault(group, {})
    key = f"{int(message_id)}:{reason}"
    previous = records.get(key) or {}
    if previous.get("status") in {"sent", "legacy_sent"}:
        return "already_sent"
    legacy_keys = set()
    for cid in chat_ids_for_group(group):
        legacy_keys.update(((state.get("groups") or {}).get(cid) or {}).get("reply_keys") or [])
    if key in legacy_keys:
        # Keep existing successful replies quiet when upgrading from older versions.
        records[key] = {"status": "legacy_sent"}
        if save_callback:
            save_callback()
        return "already_sent"
    if previous.get("retry_after", "") > now.strftime("%Y-%m-%d %H:%M:%S"):
        return "retry_later"
    canonical_id = next(cid for cid in chat_ids_for_group(group) if cid.startswith("-100"))
    group_state = state.setdefault("groups", {}).setdefault(canonical_id, {"count": 0})
    ok = main.reply_to_message_once(
        group_state, canonical_id, int(message_id), reason, main.reply_rule_text(rules, reason),
    )
    records[key] = {
        "status": "sent" if ok else "failed", "message_id": int(message_id), "rule": reason,
        "attempts": int(previous.get("attempts") or 0) + 1,
        "attempted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not ok:
        records[key]["retry_after"] = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    if save_callback:
        save_callback()
    return records[key]["status"]


def process_violation_replies(registry, state, now=None, rules=None, save_callback=None):
    fixed_now = now
    now = _now(now)
    day = now.strftime("%Y-%m-%d")
    snapshot = state.get("group_snapshot_sync") or {}
    if (now.hour >= 19 or registry.get("date") != day or state.get("date") != day
            or snapshot.get("date") != day):
        return {}
    rules = rules if rules is not None else main.load_reply_rules()
    results = {}
    for group, chat_id in main.canonical_group_chat_ids():
        if group not in (snapshot.get("completed_groups") or []):
            continue
        rows = current_rows(registry, chat_ids_for_group(group), day)
        seen = set()
        for row in sorted(rows, key=lambda item: (item["time"], item["message_id"])):
            entry, message_id = row["entry"], row["message_id"]
            if message_id in seen or entry.get("after_cutoff"):
                continue
            seen.add(message_id)
            if str(entry.get("chat_id")) not in chat_ids_for_group(group):
                continue
            try:
                sent_at = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=main.BEIJING)
            except ValueError:
                continue
            send_time = _now(fixed_now)
            if send_time.hour >= 19 or send_time.strftime("%Y-%m-%d") != day:
                return results
            if sent_at > send_time or main.defer_edited_link_reply(registry, state, chat_id, message_id, now=send_time):
                continue
            followers = main.qualified_followers_count_from_entry(entry)
            if (followers is not None and followers < main.min_followers_for_chat(chat_id)
                    and main.followers_low_is_confirmed(entry)
                    and main.reply_rule_enabled(rules, "low_followers", chat_id)):
                reason = "low_followers"
            elif (main.promo_link_missing_required_mentions([dict(entry, role="promo")])
                    and main.reply_rule_enabled(rules, "missing_mentions", chat_id)):
                reason = "missing_mentions"
            else:
                continue
            result = deliver_violation_reply(state, chat_id, message_id, reason, rules,
                                             now=send_time, save_callback=save_callback)
            results[f"{group}:{message_id}:{reason}"] = result
            if result in {"sent", "failed"}:
                print(f"群内违规回复：{group} 原消息 {message_id} {reason} {result}")
    return results
