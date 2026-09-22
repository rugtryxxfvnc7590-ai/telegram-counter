"""Cloud-only group cutoff announcements, independent of the owner's computer."""
from datetime import datetime, timedelta
import json
import re
from pathlib import Path
from string import Formatter

import main
from daily_capacity import chat_ids_for_group, group_for_chat
from edited_link_receipts import _published_slots
from violation_delivery import receipt_run_key


def beijing_now(value=None):
    value = value or datetime.now(main.BEIJING)
    return value.replace(tzinfo=main.BEIJING) if value.tzinfo is None else value.astimezone(main.BEIJING)


def load_cutoff_rules(path=None):
    try:
        data = json.loads(Path(path or main.REPLY_RULES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {group: dict(rule) for group, rule in (data.get("cutoff_announcements") or {}).items()
            if group in dict(main.canonical_group_chat_ids()) and isinstance(rule, dict)}


def render_cutoff_text(template, day, count):
    for _, field, _, _ in Formatter().parse(str(template)):
        if field and field not in {"date_label", "success_count"}:
            raise ValueError("unsupported cutoff template field")
    date = datetime.strptime(day, "%Y-%m-%d")
    return str(template).format(date_label=f"{date.month}月{date.day}日", success_count=count)


def reply_target(slots, day):
    candidates = []
    for slot in slots:
        stamp = str(slot.get("admission_time") or slot.get("time") or "")
        mid = str(slot.get("message_id") or "")
        try:
            parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if parsed.strftime("%Y-%m-%d") == day and parsed.hour < 19 and mid.isdigit() and int(mid) > 0:
            candidates.append((stamp, int(mid)))
    return max(candidates)[1] if candidates else None


def _ledger(state, day):
    ledger = state.setdefault("cutoff_announcements", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    return ledger.setdefault("groups", {})


def _plain_text(text):
    # Group history contains rendered Telegram text, not Markdown link syntax.
    from telethon.extensions import markdown
    plain, _ = markdown.parse(str(text or ""))
    return plain.replace("\r\n", "\n").strip()


def recover_cutoff_announcements(state, chat_id, messages, bot_id, rules=None, now=None):
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    group = group_for_chat(chat_id)
    snapshot = state.get("group_snapshot_sync") or {}
    if not group or not bot_id or state.get("date") != day or snapshot.get("date") != day:
        return 0
    rules = rules if rules is not None else load_cutoff_rules()
    record = _ledger(state, day).get(group) or {}
    known = set()
    if record.get("text") and record.get("reply_to_message_id"):
        known.add((int(record["reply_to_message_id"]), _plain_text(record["text"])))
    slots = _published_slots(state, group, day)
    target = reply_target(slots, day)
    template = (rules.get(group) or {}).get("text")
    if template and target:
        try:
            known.add((target, _plain_text(render_cutoff_text(template, day, len(slots)))))
        except ValueError:
            pass
    history_pattern = None
    try:
        fields = {field for _, field, _, _ in Formatter().parse(template or "")}
    except ValueError:
        fields = set()
    if template and {"date_label", "success_count"}.issubset(fields):
        try:
            rendered = _plain_text(render_cutoff_text(template, day, 987654321))
            parts = rendered.split("987654321")
            history_pattern = re.compile(re.escape(parts[0]) + r"(?P<count>\d{1,3})"
                                         + r"(?P=count)".join(re.escape(part) for part in parts[1:]))
        except ValueError:
            pass
    originals = {int(mid) for cid in chat_ids_for_group(group)
                 for mid in ((state.get("groups") or {}).get(cid) or {}).get("message_ids", [])
                 if str(mid).isdigit()}
    found = 0
    for message in sorted(messages, key=lambda item: int(item.get("message_id") or 0)):
        sender = message.get("from") or {}
        if str(sender.get("id")) != str(bot_id) or sender.get("is_bot") is not True:
            continue
        stamp = datetime.fromtimestamp(int(message.get("date") or 0), main.BEIJING)
        if stamp.strftime("%Y-%m-%d") != day or stamp.hour < 19 or stamp > now:
            continue
        pair = (message.get("reply_to_message_id"), str(message.get("text") or "").replace("\r\n", "\n").strip())
        match = history_pattern.fullmatch(pair[1]) if history_pattern else None
        recovered_count = int(match["count"]) if match else None
        if pair not in known and not (pair[0] in originals and recovered_count and recovered_count <= 500):
            continue
        _ledger(state, day)[group] = {
            **record, "status": "sent", "confirmation": "telegram_history",
            "message_id": int(message["message_id"]), "reply_to_message_id": pair[0],
            "sent_at": stamp.strftime("%Y-%m-%d %H:%M:%S"),
            "count": recovered_count if recovered_count is not None else len(slots),
        }
        found = 1
        break
    snapshot.setdefault("cutoff_receipt_groups", {})[group] = {
        "run_key": receipt_run_key(), "checked_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "bot_user_id": int(bot_id), "confirmed": found,
    }
    return found


def _history_checked(state, group, now):
    snapshot = state.get("group_snapshot_sync") or {}
    receipt = (snapshot.get("cutoff_receipt_groups") or {}).get(group) or {}
    try:
        checked_at = datetime.strptime(receipt["checked_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=main.BEIJING)
    except (KeyError, TypeError, ValueError):
        return False
    return (snapshot.get("date") == now.strftime("%Y-%m-%d")
            and group in (snapshot.get("completed_groups") or [])
            and receipt.get("run_key") == receipt_run_key() and bool(receipt.get("bot_user_id"))
            and timedelta(0) <= now - checked_at <= timedelta(minutes=5))


def send_cutoff_reply(chat_id, reply_to, text):
    if not main.BOT_TOKEN:
        return False, {"error": "missing_bot_token"}
    try:
        response = main.requests.post(
            f"https://api.telegram.org/bot{main.BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True, "reply_to_message_id": reply_to,
                  "allow_sending_without_reply": False}, timeout=15,
        )
        data = response.json()
        mid = (data.get("result") or {}).get("message_id")
        if response.status_code == 200 and data.get("ok") is True and isinstance(mid, int) and mid > 0:
            return True, {"message_id": mid}
        return False, {"error": f"telegram_{data.get('error_code', response.status_code)}"}
    except Exception as exc:
        return False, {"error": type(exc).__name__}


def process_cutoff_announcements(registry, state, now=None, rules=None, send_reply=None, save_callback=None):
    fixed_now = now
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    if now.hour < 19 or registry.get("date") != day or state.get("date") != day:
        print("群内截止公告：未到北京时间19:00或今日登记未就绪，不发送。")
        return {}
    rules = rules if rules is not None else load_cutoff_rules()
    send_reply = send_reply or send_cutoff_reply
    records = _ledger(state, day)
    results = {}
    for group, chat_id in main.canonical_group_chat_ids():
        rule = rules.get(group) or {}
        previous = records.get(group) or {}
        if not rule.get("enabled", False) or not rule.get("text"):
            results[group] = "disabled"
            continue
        if previous.get("status") == "sent":
            results[group] = "already_sent"
            continue
        send_time = beijing_now(fixed_now)
        if send_time.strftime("%Y-%m-%d") != day or send_time.hour < 19:
            break
        if not _history_checked(state, group, send_time):
            results[group] = "awaiting_history"
            continue
        slots = _published_slots(state, group, day)
        target = reply_target(slots, day)
        owner_record = ((state.get("owner_daily_lists") or {}).get("groups") or {}).get(group) or {}
        if not slots or not target or owner_record.get("pending_roster"):
            results[group] = "awaiting_published_roster"
            continue
        try:
            text = render_cutoff_text(rule["text"], day, len(slots))
        except ValueError:
            results[group] = "invalid_template"
            continue
        pending = {"status": "pending", "text": text, "count": len(slots),
                   "count_basis": "admitted_roster", "chat_id": chat_id,
                   "reply_to_message_id": target, "attempts": int(previous.get("attempts") or 0) + 1,
                   "attempted_at": send_time.strftime("%Y-%m-%d %H:%M:%S")}
        records[group] = pending
        if save_callback:
            save_callback()
        try:
            ok, detail = send_reply(chat_id, target, text)
        except Exception as exc:
            ok, detail = False, {"error": type(exc).__name__}
        if ok:
            pending.update(status="sent", sent_at=send_time.strftime("%Y-%m-%d %H:%M:%S"), **detail)
        else:
            pending.update(status="failed", **detail)
        if save_callback:
            save_callback()
        results[group] = pending["status"]
    print(f"群内截止公告：{results}")
    return results
