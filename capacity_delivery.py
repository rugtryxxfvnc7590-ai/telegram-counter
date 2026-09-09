from datetime import datetime

import main
from daily_capacity import (
    capacity_rule_for_rank, chat_ids_for_group, eligible_rows,
    normalize_daily_limits, render_capacity_text,
)


def send_capacity_reply(chat_id, message_id, text):
    if not main.BOT_TOKEN:
        return False
    try:
        response = main.requests.post(
            f"https://api.telegram.org/bot{main.BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "reply_to_message_id": message_id,
                  "allow_sending_without_reply": False},
            timeout=15,
        )
        return response.status_code == 200 and response.json().get("ok") is True
    except Exception:
        return False


def process_capacity_replies(registry, state, now=None, limits=None, rules=None, save_callback=None):
    now = now or datetime.now(main.BEIJING)
    now = now.replace(tzinfo=main.BEIJING) if now.tzinfo is None else now.astimezone(main.BEIJING)
    day = now.strftime("%Y-%m-%d")
    # eligible_rows filters by message time; a delayed run may reply until midnight.
    if registry.get("date") != day:
        return {}
    snapshot = state.get("group_snapshot_sync") or {}
    if snapshot.get("date") != day:
        return {}
    completed = set(snapshot.get("completed_groups") or [])
    limits = normalize_daily_limits(limits if limits is not None else main.load_daily_limits())
    rules = rules if rules is not None else main.load_reply_rules()
    delivery = state.setdefault("capacity_replies", {})
    if delivery.get("date") != day:
        delivery.clear()
        delivery.update({"date": day, "groups": {}})
    results = {}
    for group, chat_id in main.canonical_group_chat_ids():
        if group not in completed or not limits[group]:
            continue
        sent = delivery.setdefault("groups", {}).setdefault(group, {})
        legacy_keys = set()
        for cid in chat_ids_for_group(group):
            legacy_keys.update(((state.get("groups") or {}).get(cid) or {}).get("reply_keys") or [])
        for rank, row in enumerate(eligible_rows(registry, chat_ids_for_group(group), day), 1):
            rule = capacity_rule_for_rank(rank, limits[group])
            message_id = row["message_id"]
            if not rule or not message_id:
                continue
            # A stage reply replaces the generic overflow reply for that message.
            if rule.startswith("limit_excess_") and not main.reply_rule_enabled(rules, rule, chat_id):
                rule = "limit_overflow"
            if not main.reply_rule_enabled(rules, rule, chat_id):
                continue
            key = "full" if rule == "limit_full" else str(message_id)
            if key in sent or any(item.get("message_id") == message_id for item in sent.values()) or any(f"{message_id}:{reason}" in legacy_keys for reason in (
                "limit_full", "limit_excess_1", "limit_excess_2", "limit_excess_3", "limit_overflow",
            )):
                continue
            text = render_capacity_text(main.reply_rule_text(rules, rule), group, limits[group], rank)
            if send_capacity_reply(chat_id, message_id, text):
                sent[key] = {"message_id": message_id, "rule": rule, "count": rank,
                             "limit": limits[group], "sent_at": now.strftime("%Y-%m-%d %H:%M:%S")}
                if save_callback:
                    save_callback()
                results[f"{group}:{message_id}"] = "sent"
            else:
                results[f"{group}:{message_id}"] = "failed"
    return results
