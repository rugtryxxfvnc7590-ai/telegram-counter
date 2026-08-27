import asyncio
from collections import defaultdict
from datetime import datetime, time, timezone

from main import (
    BEIJING,
    GROUP_1_CHAT_ID_FALLBACK,
    GROUP_2_CHAT_ID_FALLBACK,
    GROUP_3_CHAT_ID_FALLBACK,
    TWITTER_REGEX,
    backfill_registry_metadata,
    beijing_full_time,
    expand_chat_id,
    extract_x_links_ordered,
    handles_from_links,
    load_registry,
    load_state,
    message_after_cutoff,
    parse_check_handle_from_handles,
    record_link,
    record_post_only,
    save_registry,
    save_state,
)
from sync_deleted_messages import _env_value, _resolve_entity, _string_session, deletion_sync_enabled


HISTORY_LIMIT = 3000
PRODUCTION_GROUPS = (
    GROUP_1_CHAT_ID_FALLBACK,
    GROUP_2_CHAT_ID_FALLBACK,
    GROUP_3_CHAT_ID_FALLBACK,
)


def _previous_rows_by_message(registry, chat_id):
    rows = defaultdict(list)
    seen = set()
    for cid in expand_chat_id(chat_id):
        for bucket_name in ("entries", "post_entries"):
            for entry in (((registry or {}).get(bucket_name) or {}).get(cid) or {}).values():
                message_id = str((entry or {}).get("message_id") or "")
                key = (message_id, str((entry or {}).get("promo_post_id") or ""))
                if message_id and key not in seen:
                    rows[message_id].append(dict(entry or {}))
                    seen.add(key)
    return rows


def replace_group_snapshot(registry, state, chat_id, messages, day):
    if registry.get("date") != day:
        registry.clear()
        registry.update({"date": day, "entries": {}, "post_entries": {}})
    if state.get("date") != day:
        state["date"] = day
        state["groups"] = {}

    previous = _previous_rows_by_message(registry, chat_id)
    for cid in expand_chat_id(chat_id):
        registry.setdefault("entries", {}).pop(cid, None)
        registry.setdefault("post_entries", {}).pop(cid, None)
    registry["entries"][chat_id] = {}
    registry["post_entries"][chat_id] = {}

    groups = state.setdefault("groups", {})
    old_group = {}
    for cid in expand_chat_id(chat_id):
        if groups.get(cid):
            old_group = groups[cid]
        groups.pop(cid, None)
    group_state = {"count": 0, "message_ids": []}
    if old_group.get("reply_keys"):
        group_state["reply_keys"] = list(old_group["reply_keys"])
    groups[chat_id] = group_state

    matched = 0
    for msg in sorted(messages, key=lambda item: (int(item.get("date") or 0), int(item.get("message_id") or 0))):
        text = str(msg.get("text") or "")
        if not TWITTER_REGEX.search(text):
            continue
        message_id = int(msg.get("message_id") or 0)
        msg_time = beijing_full_time(msg["date"])
        links = extract_x_links_ordered(text)
        handles = handles_from_links(links)
        check_handle, dual_link, promo_handle = parse_check_handle_from_handles(handles)
        previous_entries = previous.get(str(message_id), [])
        if not handles:
            record_post_only(
                registry,
                msg,
                text,
                chat_id,
                msg_time,
                links,
                after_cutoff=message_after_cutoff(msg["date"]),
            )
        for handle in handles:
            record_link(
                registry,
                handle,
                msg,
                text,
                chat_id,
                msg_time,
                check_handle=check_handle or handle,
                dual_link=dual_link,
                promo_handle=promo_handle or handle,
                links=links,
                previous_entries=previous_entries,
                after_cutoff=message_after_cutoff(msg["date"]),
            )
        group_state["count"] += 1
        group_state["message_ids"].append(str(message_id))
        matched += 1
    return matched


async def _today_messages(client, entity, day_start_utc):
    messages = []
    reached_previous_day = False
    async for message in client.iter_messages(entity, limit=HISTORY_LIMIT):
        if not message.date:
            continue
        message_date = message.date
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        if message_date < day_start_utc:
            reached_previous_day = True
            break
        sender = await message.get_sender()
        messages.append({
            "message_id": message.id,
            "date": int(message_date.timestamp()),
            "edit_date": int(message.edit_date.timestamp()) if message.edit_date else None,
            "text": message.raw_text or "",
            "from": {
                "id": getattr(sender, "id", None) or message.sender_id,
                "username": getattr(sender, "username", "") or "",
                "first_name": getattr(sender, "first_name", "") or "",
                "last_name": getattr(sender, "last_name", "") or "",
            },
        })
    if len(messages) >= HISTORY_LIMIT and not reached_previous_day:
        raise RuntimeError(f"今日消息达到安全上限 {HISTORY_LIMIT}，拒绝用不完整快照覆盖")
    return messages


async def async_main():
    if not deletion_sync_enabled():
        print("今日群消息补齐：未配置 Telegram 用户会话，跳过。")
        return 0
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception as exc:
        print(f"今日群消息补齐：缺少 telethon 依赖，跳过：{exc}")
        return 0

    now = datetime.now(BEIJING)
    day = now.strftime("%Y-%m-%d")
    day_start = datetime.combine(now.date(), time.min, tzinfo=BEIJING).astimezone(timezone.utc)
    registry = load_registry()
    state = load_state()
    state["group_snapshot_sync"] = {
        "date": day,
        "run_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "completed_groups": [],
    }
    api_id = int(_env_value("TELEGRAM_API_ID"))
    client = TelegramClient(StringSession(_string_session()), api_id, _env_value("TELEGRAM_API_HASH"))

    completed = 0
    async with client:
        for chat_id in PRODUCTION_GROUPS:
            try:
                entity = await _resolve_entity(client, chat_id)
                messages = await _today_messages(client, entity, day_start)
                matched = replace_group_snapshot(registry, state, chat_id, messages, day)
            except Exception as exc:
                print(f"今日群消息补齐：群 {chat_id} 核查失败，保留原数据：{exc}")
                continue
            completed += 1
            state["group_snapshot_sync"]["completed_groups"].append(
                {GROUP_1_CHAT_ID_FALLBACK: "群一", GROUP_2_CHAT_ID_FALLBACK: "群二", GROUP_3_CHAT_ID_FALLBACK: "群三"}[chat_id]
            )
            print(f"今日群消息补齐：群 {chat_id} 读取 {len(messages)} 条消息，收录 {matched} 条 X 链接消息。")

    if completed:
        enriched = backfill_registry_metadata(registry)
        if enriched:
            print(f"今日群消息补齐：补齐 X 昵称/粉丝字段 {enriched} 条。")
        save_registry(registry)
    save_state(state)
    print(f"今日群消息补齐完成：成功核查 {completed}/3 个群。")
    return 0


def main():
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
