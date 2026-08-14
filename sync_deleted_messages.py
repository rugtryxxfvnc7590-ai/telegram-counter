import asyncio
import os
from collections import defaultdict

from main import expand_chat_id, load_registry, remove_registry_rows_for_message, save_registry


SESSION_ENV_KEYS = ("TELEGRAM_STRING_SESSION", "TELEGRAM_USER_SESSION", "TELETHON_SESSION")
DELETE_SYNC_BATCH_SIZE = 100


def _env_value(name, env=None):
    env = env or os.environ
    return str(env.get(name) or "").strip()


def _string_session(env=None):
    env = env or os.environ
    for key in SESSION_ENV_KEYS:
        value = _env_value(key, env)
        if value:
            return value
    return ""


def deletion_sync_enabled(env=None):
    env = env or os.environ
    return bool(_env_value("TELEGRAM_API_ID", env) and _env_value("TELEGRAM_API_HASH", env) and _string_session(env))


def _message_id(value):
    try:
        number = int(str(value or "").strip())
    except Exception:
        return None
    return number if number > 0 else None


def collect_registry_message_refs(registry):
    refs = defaultdict(set)
    for bucket_name in ("entries", "post_entries"):
        for cid, bucket in ((registry or {}).get(bucket_name) or {}).items():
            for entry in (bucket or {}).values():
                message_id = _message_id((entry or {}).get("message_id"))
                if not message_id:
                    continue
                chat_id = str((entry or {}).get("chat_id") or cid)
                refs[chat_id].add(message_id)
    return {chat_id: sorted(ids) for chat_id, ids in refs.items()}


def prune_deleted_message_ids(registry, deleted_by_chat):
    removed = []
    for chat_id, message_ids in (deleted_by_chat or {}).items():
        for cid in expand_chat_id(chat_id):
            for message_id in sorted(set(message_ids or [])):
                for entry in remove_registry_rows_for_message(registry, cid, message_id):
                    removed.append({
                        "chat_id": cid,
                        "message_id": message_id,
                        "promo_url": entry.get("promo_url") or entry.get("link") or "",
                        "promo_handle": entry.get("promo_handle") or entry.get("x_handle") or "",
                        "tg_username": entry.get("tg_username") or "",
                    })
    return removed


def _chunks(values, size=DELETE_SYNC_BATCH_SIZE):
    values = list(values or [])
    for idx in range(0, len(values), size):
        yield values[idx:idx + size]


async def _resolve_entity(client, chat_id):
    last_error = None
    candidates = sorted(expand_chat_id(chat_id), key=lambda value: 0 if str(value).startswith("-100") else 1)
    for cid in candidates:
        try:
            return await client.get_entity(int(cid))
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError(f"cannot resolve chat {chat_id}")


async def _existing_message_ids(client, entity, message_ids):
    existing = set()
    for batch in _chunks(message_ids):
        messages = await client.get_messages(entity, ids=batch)
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        for message_id, message in zip(batch, messages):
            if message is not None:
                existing.add(message_id)
    return existing


async def sync_deleted_messages_with_client(client, registry=None, logger=print):
    registry = registry if registry is not None else load_registry()
    refs = collect_registry_message_refs(registry)
    deleted_by_chat = {}
    errors = {}

    for chat_id, message_ids in refs.items():
        try:
            entity = await _resolve_entity(client, chat_id)
            existing = await _existing_message_ids(client, entity, message_ids)
        except Exception as exc:
            errors[chat_id] = str(exc)
            logger(f"删除同步：群 {chat_id} 无法核查，跳过：{exc}")
            continue
        deleted = sorted(set(message_ids) - existing)
        if deleted:
            deleted_by_chat[chat_id] = deleted

    removed = prune_deleted_message_ids(registry, deleted_by_chat)
    if removed:
        save_registry(registry)
    return {
        "checked_chats": len(refs),
        "checked_messages": sum(len(v) for v in refs.values()),
        "deleted_messages": sum(len(v) for v in deleted_by_chat.values()),
        "removed_rows": len(removed),
        "removed": removed,
        "errors": errors,
    }


async def async_main():
    if not deletion_sync_enabled():
        print("删除同步：未配置 TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_STRING_SESSION，跳过。")
        return 0
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception as exc:
        print(f"删除同步：缺少 telethon 依赖，跳过：{exc}")
        return 0

    api_id = int(_env_value("TELEGRAM_API_ID"))
    api_hash = _env_value("TELEGRAM_API_HASH")
    session = _string_session()
    client = TelegramClient(StringSession(session), api_id, api_hash)
    async with client:
        result = await sync_deleted_messages_with_client(client)
    print(
        "删除同步完成："
        f"核查群 {result['checked_chats']} 个，"
        f"核查消息 {result['checked_messages']} 条，"
        f"发现已删除消息 {result['deleted_messages']} 条，"
        f"移除后台登记 {result['removed_rows']} 条。"
    )
    return 0


def main():
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
