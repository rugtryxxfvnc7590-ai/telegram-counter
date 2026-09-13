"""Delivery and snapshot catch-up for the existing group violation rules."""
from datetime import datetime, timedelta
import os

import main
from daily_capacity import chat_ids_for_group, group_for_chat
from private_list_sync import current_rows


def _now(value=None):
    value = value or datetime.now(main.BEIJING)
    return value.replace(tzinfo=main.BEIJING) if value.tzinfo is None else value.astimezone(main.BEIJING)


def receipt_run_key():
    run_id = os.getenv("GITHUB_RUN_ID", "")
    return f"{run_id}:{os.getenv('GITHUB_RUN_ATTEMPT', '1')}" if run_id else "local"


def verified_reply_bot_id():
    if not main.BOT_TOKEN:
        return None
    try:
        response = main.requests.get(f"https://api.telegram.org/bot{main.BOT_TOKEN}/getMe", timeout=10)
        data = response.json()
        user = data.get("result") or {}
        if response.status_code == 200 and data.get("ok") is True and user.get("is_bot") is True:
            bot_id = int(user.get("id") or 0)
            return bot_id if bot_id > 0 else None
    except Exception as exc:
        print(f"群内回执核验：机器人身份读取失败 {type(exc).__name__}，本轮不盲目补回复。")
    return None


def recover_violation_replies(state, chat_id, messages, bot_id, rules, now=None):
    """Rebuild lost acknowledgments from this bot's replies in a complete group snapshot."""
    now = _now(now)
    day = now.strftime("%Y-%m-%d")
    group = group_for_chat(chat_id)
    snapshot = state.get("group_snapshot_sync") or {}
    if not bot_id or not group or state.get("date") != day or snapshot.get("date") != day:
        return 0
    original_ids = set()
    for cid in chat_ids_for_group(group):
        original_ids.update(str(mid) for mid in ((state.get("groups") or {}).get(cid) or {}).get("message_ids") or [])
    ledger = state.setdefault("violation_replies", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    records = ledger.setdefault("groups", {}).setdefault(group, {})
    found = set()
    for message in sorted(messages, key=lambda item: (int(item.get("date") or 0), int(item.get("message_id") or 0))):
        sender = message.get("from") or {}
        reply_to = str(message.get("reply_to_message_id") or "")
        if str(sender.get("id")) != str(bot_id) or sender.get("is_bot") is not True or reply_to not in original_ids:
            continue
        stamp = datetime.fromtimestamp(int(message.get("date") or 0), main.BEIJING)
        if stamp.strftime("%Y-%m-%d") != day or stamp > now:
            continue
        text = str(message.get("text") or "").strip()
        for reason in ("low_followers", "missing_mentions"):
            key = f"{reply_to}:{reason}"
            previous = records.get(key) or {}
            known_texts = {main.reply_rule_text(rules, reason), main.DEFAULT_REPLY_RULES[reason]["text"],
                           str(previous.get("text") or "")}
            if not text or text not in known_texts or key in found:
                continue
            found.add(key)
            records[key] = {**previous, "status": "sent", "message_id": int(reply_to), "rule": reason,
                            "text": text, "confirmation": "telegram_history", "bot_user_id": int(bot_id),
                            "telegram_reply_message_id": int(message["message_id"]),
                            "sent_at": stamp.strftime("%Y-%m-%d %H:%M:%S")}
            records[key].pop("retry_after", None)
    snapshot.setdefault("violation_receipt_groups", {})[group] = {
        "run_key": receipt_run_key(), "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "bot_user_id": int(bot_id), "confirmed_replies": len(found),
    }
    print(f"群内回执核验：{group} 已确认 {len(found)} 条原消息的违规回复，恢复去重记录。")
    return len(found)


def history_is_current(state, group, now):
    snapshot = state.get("group_snapshot_sync") or {}
    receipt = (snapshot.get("violation_receipt_groups") or {}).get(group) or {}
    try:
        checked_at = datetime.strptime(receipt["checked_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=main.BEIJING)
    except (KeyError, TypeError, ValueError):
        return False
    return (snapshot.get("date") == now.strftime("%Y-%m-%d")
            and group in (snapshot.get("completed_groups") or [])
            and receipt.get("run_key") == receipt_run_key() and bool(receipt.get("bot_user_id"))
            and timedelta(0) <= now - checked_at <= timedelta(minutes=5))


def deliver_violation_reply(state, chat_id, message_id, reason, rules, now=None, save_callback=None,
                            history_checked=False):
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
    # The getUpdates phase must wait for this run's user-session history check.
    if not history_checked:
        return "awaiting_history"
    if previous.get("retry_after", "") > now.strftime("%Y-%m-%d %H:%M:%S"):
        return "retry_later"
    canonical_id = next(cid for cid in chat_ids_for_group(group) if cid.startswith("-100"))
    group_state = state.setdefault("groups", {}).setdefault(canonical_id, {"count": 0})
    text = main.reply_rule_text(rules, reason)
    ok = main.reply_to_message_once(
        group_state, canonical_id, int(message_id), reason, text,
    )
    records[key] = {
        "status": "sent" if ok else "failed", "message_id": int(message_id), "rule": reason,
        "attempts": int(previous.get("attempts") or 0) + 1,
        "attempted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "text": text,
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
        if not history_is_current(state, group, now):
            print(f"群内回执核验：{group} 本轮未完成核验，暂不发送违规回复，避免重复。")
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
                                             now=send_time, save_callback=save_callback, history_checked=True)
            results[f"{group}:{message_id}:{reason}"] = result
            if result in {"sent", "failed"}:
                print(f"群内违规回复：{group} 原消息 {message_id} {reason} {result}")
    return results
