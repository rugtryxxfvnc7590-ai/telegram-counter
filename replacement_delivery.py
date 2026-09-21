"""Retryable replies to original candidate messages admitted into fixed slots."""
from datetime import datetime

import main
from daily_capacity import GROUP_CHAT_IDS, group_for_chat
from violation_delivery import history_is_current
from withdrawal_sync import beijing_now

RULE = "replacement_admitted"


def _records(state, day, group):
    ledger = state.setdefault("replacement_replies", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    return ledger.setdefault("groups", {}).setdefault(group, {})


def recover_replacement_replies(state, chat_id, messages, bot_id, rules, now=None):
    """A completed Telegram snapshot repairs a lost state commit or lost HTTP reply."""
    now = beijing_now(now)
    day, group = now.strftime("%Y-%m-%d"), group_for_chat(chat_id)
    if not bot_id or not group:
        return 0
    records = _records(state, day, group)
    found = 0
    for message in messages:
        sender = message.get("from") or {}
        mid = str(message.get("reply_to_message_id") or "")
        if (str(sender.get("id")) != str(bot_id) or sender.get("is_bot") is not True
                or not mid.isdigit() or int(mid) <= 0):
            continue
        stamp = datetime.fromtimestamp(int(message.get("date") or 0), main.BEIJING)
        if stamp.strftime("%Y-%m-%d") != day or stamp > now:
            continue
        previous = records.get(mid) or {}
        text = str(message.get("text") or "").strip()
        known = {main.reply_rule_text(rules, RULE), main.DEFAULT_REPLY_RULES[RULE]["text"], previous.get("text")}
        if text and text in known:
            records[mid] = dict(previous, status="sent", text=text, confirmation="telegram_history",
                                telegram_reply_message_id=message["message_id"], sent_at=stamp.isoformat())
            found += 1
    return found


def process_replacement_replies(state, now=None, rules=None, save_callback=None, send_reply=None):
    from capacity_delivery import send_capacity_reply
    fixed_now = now
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    source = state.get("daily_rosters") or {}
    if source.get("date") != day:
        return {}
    rules = rules if rules is not None else main.load_reply_rules()
    send_reply = send_reply or send_capacity_reply
    results = {}
    for group, roster in (source.get("groups") or {}).items():
        chat_id = GROUP_CHAT_IDS.get(group)
        if not chat_id or not main.reply_rule_enabled(rules, RULE, chat_id):
            continue
        limit = roster.get("admission_limit", main.load_daily_limits().get(group, 0))
        if not limit or int(roster.get("roster_capacity") or roster.get("capacity") or 0) < limit:
            continue
        if not history_is_current(state, group, now):
            results[group] = "awaiting_history"
            continue
        records = _records(state, day, group)
        for slot in roster.get("slots") or []:
            mid = str(slot.get("message_id") or "")
            if not slot.get("is_replacement") or not mid.isdigit() or int(mid) <= 0:
                continue
            previous = records.get(mid) or {}
            if previous.get("status") == "sent":
                continue
            send_time = beijing_now(fixed_now)
            if send_time.strftime("%Y-%m-%d") != day:
                return results
            text = main.reply_rule_text(rules, RULE)
            record = dict(previous, status="pending", position=slot["position"], text=text,
                          attempts=int(previous.get("attempts") or 0) + 1, attempted_at=send_time.isoformat())
            records[mid] = record
            if save_callback:
                save_callback()
            try:
                ok = send_reply(chat_id, int(mid), text)
            except Exception:
                ok = False
            record["status"] = "sent" if ok else "failed"
            if ok:
                record["sent_at"] = send_time.isoformat()
            if save_callback:
                save_callback()
            results[f"{group}:{mid}"] = record["status"]
    return results
