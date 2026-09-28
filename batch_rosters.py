"""Persistent per-account batches. Never partition a fresh ranked list on retry."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
from string import Formatter

from daily_capacity import chat_ids_for_group, eligible_rows
from private_list_sync import edited_slots, freeze_links, order_roster_slots


def load_batch_config(path=None):
    try:
        value = json.loads(Path(path or Path(__file__).with_name("batch_roster_config.json")).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def batch_policy(group, day, config=None):
    value = (load_batch_config() if config is None else config).get(group) or {}
    if group != "群一" or value.get("enabled") is not True:
        return None
    try:
        start = str(value["effective_from"])
        datetime.strptime(start, "%Y-%m-%d")
        size, count = value["size"], value["max_batches"]
        if (type(size) is not int or type(count) is not int or not 1 <= size <= 30
                or not 1 <= count <= 10 or day < start):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {"size": size, "max_batches": count, "effective_from": start}


def flatten_batches(record):
    result = []
    size = record["batch_policy"]["size"]
    for index, (label, batch) in enumerate(sorted(record["batches"].items())):
        for slot in batch["slots"]:
            result.append(dict(slot, list_id=label, list_position=slot["position"],
                               position=index * size + slot["position"]))
    return result


def refresh_batch_roster(previous, registry, group, day, now_text, policy):
    """Each day's policy and assignment stay fixed, including when settings change."""
    result = deepcopy(previous) if previous and previous.get("batches") else {
        "batch_policy": deepcopy(policy), "batches": {}, "assigned_accounts": {},
        "withdrawn_message_ids": [],
    }
    policy = result["batch_policy"]
    size, count = policy["size"], policy["max_batches"]
    ids = chat_ids_for_group(group)
    for i in range(count):
        result["batches"].setdefault(chr(65 + i), {"slots": [], "vacant_positions": {}, "capacity": 0})
    # A pre-feature published roster must not be silently repartitioned.
    if previous and not previous.get("batches") and previous.get("slots"):
        raise ValueError("Cannot split an already published single roster in place")
    evidence = registry.get("confirmed_withdrawals") or {}
    removed = (evidence.get("groups") or {}).get(group, {}) if evidence.get("date") == day else {}
    retired = set(result.get("withdrawn_message_ids") or [])
    before_cutoff = now_text[11:16] < "19:00" and not result.get("cutoff_finalized")
    for label, batch in result["batches"].items():
        retained = []
        for slot in batch["slots"]:
            stamp = str(removed.get(str(slot.get("message_id")), {}).get("confirmed_at") or "")
            if stamp[:10] == day and stamp <= now_text:
                mid = str(slot["message_id"])
                batch["vacant_positions"][str(slot["position"])] = mid
                batch["withdrawn_message_ids"] = sorted(set(batch.get("withdrawn_message_ids") or []) | {mid})
                retired.add(mid)
            else:
                retained.append(slot)
        batch["slots"] = retained
        batch["slots"] = edited_slots(batch["slots"], registry, ids, day, now_text)
    if before_cutoff:
        used_posts = {slot["post_id"] for batch in result["batches"].values() for slot in batch["slots"]}
        used_messages = {str(slot.get("message_id")) for batch in result["batches"].values() for slot in batch["slots"]}
        for row in eligible_rows(registry, ids, day):
            handle = str(row["entry"].get("promo_handle") or "").lower().lstrip("@")
            mid = str(row["message_id"])
            if (not handle or handle in result["assigned_accounts"] or mid in retired or mid in used_messages
                    or row["post_id"] in used_posts or row["time"] > now_text):
                continue
            available = next(((label, batch) for label, batch in sorted(result["batches"].items())
                              if len(batch["slots"]) < size), None)
            if available is None:
                break
            label, batch = available
            bound = freeze_links([row["url"]], registry, ids, day)[0]
            if bound.get("message_id") != mid:
                continue
            if batch["vacant_positions"]:
                position = min(map(int, batch["vacant_positions"]))
                bound.update(is_replacement=True, replaced_message_id=batch["vacant_positions"].pop(str(position)))
            else:
                position = max([slot["position"] for slot in batch["slots"]], default=0) + 1
            bound.update(position=position, list_id=label, display_name=str(row["entry"].get("x_name") or ""),
                         admitted_at=now_text)
            batch["slots"].append(bound)
            result["assigned_accounts"][handle] = label
            used_messages.add(mid)
            used_posts.add(row["post_id"])
    for label, batch in result["batches"].items():
        batch["capacity"] = max(batch["capacity"], len(batch["slots"]) + len(batch["vacant_positions"]))
        batch["slots"] = order_roster_slots(batch["slots"], batch["vacant_positions"])
        batch.update(count=len(batch["slots"]), roster_capacity=batch["capacity"],
                     withdrawn_message_ids=batch.get("withdrawn_message_ids", []))
    result["withdrawn_message_ids"] = sorted(retired)
    result["slots"] = flatten_batches(result)
    result["vacant_positions"] = {str(index * size + int(pos)): mid
        for index, batch in enumerate(result["batches"].values()) for pos, mid in batch["vacant_positions"].items()}
    result.update(count=len(result["slots"]), capacity=size * count, roster_capacity=size * count,
                  admission_limit=size * count)
    return result


def export_batch_rosters(registry, state):
    """The checker consumes this authoritative roster, never a new top-N ranking."""
    daily = state.get("daily_rosters") or {}
    day = registry.get("date")
    if daily.get("date") != day:
        return
    groups = {group: deepcopy(record) for group, record in daily.get("groups", {}).items() if record.get("batches")}
    if groups:
        registry["batch_rosters"] = {"date": day, "groups": groups}
        for group, record in groups.items():
            accepted = {slot["post_id"]: slot for slot in record["slots"]}
            for bucket_name in ("entries", "post_entries"):
                for cid in chat_ids_for_group(group):
                    for entry in (registry.get(bucket_name, {}).get(cid) or {}).values():
                        slot = accepted.get(str(entry.get("promo_post_id") or ""))
                        if slot:
                            entry.update(daily_list_status="accepted", daily_list_rank=slot["position"],
                                         daily_list_batch=slot["list_id"], daily_list_batch_position=slot["list_position"])
                        elif entry.get("mutual_eligible") is True and not entry.get("after_cutoff"):
                            entry.update(daily_list_status="waitlist", daily_list_batch="")


def batch_url(group, day, label=""):
    from website_sync import DEFAULT_URL, GROUPS
    group_id = GROUPS[group][0]
    return f"{DEFAULT_URL}/g/{group_id}?date={day}" + (f"&list={label}" if label else "")


def admission_notice(group, day, label):
    return (f"你已加入「{day}·{group}·{label}名单」。\n"
            f"仅需转发{label}名单内其他成员的帖子，不需要转其他名单。\n"
            f"查看自己的名单：{batch_url(group, day, label)}")


def full_notice(group, day, label, count):
    return f"{group}（{day}）{label}名单已满，共{count}人。\n仅需互推本名单，不要跨名单转发。\n{batch_url(group, day, label)}"


def first_batch_message(slots, day):
    candidates = []
    for slot in slots:
        stamp = str(slot.get("admission_time") or slot.get("time") or "")
        mid = str(slot.get("message_id") or "")
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when.strftime("%Y-%m-%d") == day and when.hour < 19 and mid.isdigit() and int(mid) > 0:
            candidates.append((stamp, int(mid)))
    return min(candidates)[1] if candidates else None


def batch_start_notice(group, day):
    return (f"{group}（{day}）B名单从这条消息开始。\n"
            "已入选B名单的成员只需互推B名单，不需要转A名单。\n"
            f"网站每日北京时间14:00开始公示：{batch_url(group, day, 'B')}")


DEFAULT_BATCH_CUTOFF = ("群一（{date_label}）今日互推已截止\n\n{batch_lines}\n\n"
    "请先打开名单，输入自己的X用户名或昵称，确认属于哪一份名单。\n\n"
    "1. 只需转发自己所属名单内其他成员的帖子，不同名单之间不要求互推。请勿按群一主页全部转发。\n"
    "2. 旧帖、野推按群规则撤销。转发完成后，请按自己的名单逐条核对，不要把自己那条也算作需要转发的帖子。\n"
    "3. 可根据时间安排提前或分段完成，最迟于次日北京时间01:00转发完毕。届时按各自名单检查互推情况。\n\n"
    "当天换帖及删除情况以这份名单页面的最新内容为准。请勿混推或漏推，欢迎相互监督。")


def batch_cutoff_text(group, day, record):
    lines = []
    for label, batch in record["batches"].items():
        if batch["slots"]:
            lines.append(f"{label}名单：{len(batch['slots'])}人\n[点击查看{label}名单]({batch_url(group, day, label)})")
    template = (load_batch_config().get(group) or {}).get("cutoff_text") or DEFAULT_BATCH_CUTOFF
    for _, field, spec, conversion in Formatter().parse(template):
        if field and (field not in {"date_label", "batch_lines"} or spec or conversion):
            raise ValueError("分名单公告变量无效")
    date = datetime.strptime(day, "%Y-%m-%d")
    return template.format(date_label=f"{date.month}月{date.day}日", batch_lines="\n\n".join(lines))


def recover_batch_notices(state, chat_id, messages, bot_id, now=None):
    from daily_capacity import group_for_chat
    from withdrawal_sync import beijing_now
    now = beijing_now(now)
    day, group = now.strftime("%Y-%m-%d"), group_for_chat(chat_id)
    if not group or not bot_id or not batch_policy(group, day):
        return
    ledger = state.setdefault("batch_notices", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    sent = ledger["groups"].setdefault(group, {})
    for message in messages:
        if (str((message.get("from") or {}).get("id")) != str(bot_id)
                or (message.get("from") or {}).get("is_bot") is not True
                or datetime.fromtimestamp(int(message.get("date") or 0), now.tzinfo).strftime("%Y-%m-%d") != day):
            continue
        text, mid = message.get("text", ""), message.get("reply_to_message_id")
        if mid and text == batch_start_notice(group, day):
            sent["start_B"] = {"confirmation": "telegram_history", "message_id": int(mid)}
        for i in range(10):
            label = chr(65 + i)
            if mid and text == admission_notice(group, day, label):
                sent[str(mid)] = {"list_id": label, "confirmation": "telegram_history"}
            match = re.fullmatch(re.escape(f"{group}（{day}）{label}名单已满，共") + r"(\d+)" +
                                 re.escape(f"人。\n仅需互推本名单，不要跨名单转发。\n{batch_url(group, day, label)}"), text)
            if mid and match and 1 <= int(match[1]) <= 30:
                sent["full_" + label] = {"confirmation": "telegram_history"}


def format_batch_message(group, day, label, slots):
    from main import format_daily_list_message
    return (format_daily_list_message(f"{group}·{label}名单", day,
                                     [slot["url"] for slot in slots], slots=slots)
            + f"\n\n仅需互推{label}名单内其他成员，不需要转其他名单。\n{batch_url(group, day, label)}")


def send_batch_lists(registry, state, group, record, owner, now, save_callback=None):
    from main import _send_private_message, _edit_private_message
    day = now.strftime("%Y-%m-%d")
    delivery = state["owner_daily_lists"]["groups"].setdefault(group, {"batches": {}})
    outcomes = {}
    for label, batch in record["batches"].items():
        slots = batch["slots"]
        sent = delivery["batches"].get(label) or {}
        if not sent.get("sent") and (not slots or (len(slots) < record["batch_policy"]["size"] and now.hour < 19)):
            continue
        text = format_batch_message(group, day, label, slots)
        if sent.get("text") == text and sent.get("message_id"):
            outcomes[label] = "unchanged"
            continue
        if len(text) > 4096 or sent.get("owner_chat_id", owner) != owner:
            outcomes[label] = "invalid_delivery"
            continue
        if sent.get("message_id"):
            ok, detail = _edit_private_message(owner, sent["message_id"], text)
        else:
            ok, detail = _send_private_message(owner, text)
        if not ok:
            outcomes[label] = "failed"
            continue
        mid = sent.get("message_id") or (detail.get("message_id") if isinstance(detail, dict) else None)
        delivery["batches"][label] = dict(batch, sent=True, message_id=mid, owner_chat_id=owner,
            text=text, links=[s["url"] for s in slots], updated_at=now.strftime("%Y-%m-%d %H:%M:%S"))
        if save_callback:
            save_callback()
        outcomes[label] = "edited" if sent.get("message_id") else "sent"
    return outcomes


def process_batch_notices(registry, state, now=None, save_callback=None, send_reply=None):
    # A-full/B-start replies were cancelled. Keep the existing caller harmless,
    # including old unsent notices, without altering rosters or delivery history.
    return {}
