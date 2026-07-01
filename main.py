import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN").strip() if os.getenv("TELEGRAM_BOT_TOKEN") else None
MAX_LIMIT = 40
STATE_FILE = "state.json"
REGISTRY_FILE = "link_registry.json"

# 群一（10万以上大佬群）电报群 ID
GROUP_1_CHAT_ID = "-1003891628675"
# 群二（5万以下新手营）由 GitHub Secret TELEGRAM_CHAT_ID 配置

BEIJING = timezone(timedelta(hours=8))
TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/', re.IGNORECASE)
X_HANDLE_RE = re.compile(
    r'https?://(?:www\.)?(?:twitter\.com|x\.com)/(@?)([A-Za-z0-9_]{1,15})(?:/|$|\?)',
    re.IGNORECASE,
)
SKIP_HANDLES = frozenset({"i", "intent", "search", "home", "share", "hashtag"})


def load_chat_ids():
    ids = []
    env = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if env:
        for part in env.split(","):
            part = part.strip()
            if part:
                ids.append(part)
    for cid in (GROUP_1_CHAT_ID,):
        if cid not in ids:
            ids.append(cid)
    return ids


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}
    if "groups" not in state:
        legacy = state.get("count", 0)
        legacy_cid = os.getenv("TELEGRAM_CHAT_ID", "").strip().split(",")[0].strip()
        state = {
            "date": state.get("date", ""),
            "offset": state.get("offset", 0),
            "groups": {legacy_cid: {"count": legacy}} if legacy_cid else {},
        }
    state.setdefault("groups", {})
    state.setdefault("offset", 0)
    state.setdefault("date", "")
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_registry():
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"date": "", "entries": {}}
    if "entries" not in data and data.get("by_x_handle"):
        legacy_cid = os.getenv("TELEGRAM_CHAT_ID", "").strip().split(",")[0].strip()
        data = {"date": data.get("date", ""), "entries": {legacy_cid: data["by_x_handle"]}}
    data.setdefault("entries", {})
    return data


def save_registry(registry):
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def beijing_day_of(unix_ts):
    return datetime.fromtimestamp(unix_ts, BEIJING).strftime("%Y-%m-%d")


def beijing_full_time(unix_ts):
    return datetime.fromtimestamp(unix_ts, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def extract_x_handles(text):
    handles = set()
    for m in X_HANDLE_RE.finditer(text or ""):
        handle = m.group(2).lower()
        if handle not in SKIP_HANDLES:
            handles.add(handle)
    return handles


def record_link(registry, x_handle, msg, text, chat_id, msg_time):
    user = msg.get("from", {})
    name = user.get("first_name", "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    bucket = registry.setdefault("entries", {}).setdefault(chat_id, {})
    bucket[x_handle] = {
        "x_handle": x_handle,
        "tg_username": user.get("username") or "",
        "tg_user_id": user.get("id"),
        "tg_name": name.strip() or "未知用户",
        "link": text,
        "chat_id": chat_id,
        "time": msg_time,
    }


def reply_to_message(chat_id, message_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "reply_to_message_id": message_id}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"群内回复状态: {r.status_code} | 消息ID: {message_id}")
    except Exception as e:
        print(f"回复异常: {e}")


def main():
    chat_ids = load_chat_ids()
    if not chat_ids:
        print("❌ 没读到任何群 ID，请检查 TELEGRAM_CHAT_ID Secret")
        return

    print(f"监听群: {chat_ids}（群二=Secret，群一={GROUP_1_CHAT_ID}；仅收录，不私信）")
    state = load_state()
    registry = load_registry()

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": state.get("offset", 0) + 1, "timeout": 10, "allowed_updates": ["message"]}

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            print(f"❌ Telegram API 返回错误(getUpdates): {data}")
            return

        updates = data.get("result", [])
        print(f"本次获取到 {len(updates)} 条更新")
        matched = 0
        for update in updates:
            state["offset"] = update["update_id"]
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue

            text = msg["text"]
            actual_chat_id = str(msg["chat"]["id"])
            message_id = msg["message_id"]

            if actual_chat_id not in chat_ids or not TWITTER_REGEX.search(text):
                continue

            msg_day = beijing_day_of(msg["date"])
            msg_time = beijing_full_time(msg["date"])

            if state.get("date") != msg_day:
                state["date"] = msg_day
                state["groups"] = {}
                print(f"✅ 计数已重置，进入新的一天: {msg_day}")

            if registry.get("date") != msg_day:
                registry = {"date": msg_day, "entries": {}}
                print(f"✅ 链接收录已重置: {msg_day}")

            grp = state["groups"].setdefault(actual_chat_id, {"count": 0})
            grp["count"] += 1
            current = grp["count"]
            matched += 1

            user = msg.get("from", {})
            tg_tag = f"@{user['username']}" if user.get("username") else user.get("first_name", "?")
            print(f"🔗 [{msg_time}] 群 {actual_chat_id} 第 {current} 条 | 发送人: {tg_tag}")

            for handle in extract_x_handles(text):
                record_link(registry, handle, msg, text, actual_chat_id, msg_time)
                print(f"   📝 收录 @{handle} ← {tg_tag}")

            if current == MAX_LIMIT:
                reply_to_message(
                    actual_chat_id,
                    message_id,
                    "🐾 叮当~ 今日互推已满40条，前40名已锁定上车！后面发的会被机器猫记进候选名单，如有空位会优先安排哦，辛苦各位啦~记得看群置顶规则呀！",
                )
            elif current > MAX_LIMIT:
                excess = current - MAX_LIMIT
                if excess % 3 == 1:
                    if excess <= 3:
                        reply_to_message(
                            actual_chat_id,
                            message_id,
                            "🐾 机器猫收到啦~ 不过今日40个名额已满，你这条先帮你放进候选名单排队啦，有机会就给你顶上去！",
                        )
                    elif excess <= 6:
                        reply_to_message(
                            actual_chat_id,
                            message_id,
                            "🐾 又有新链接~ 机器猫已经悄悄记下，放进候选备用区啦。今日正选已满，这些会作为优先候选，辛苦再等等~",
                        )
                    else:
                        reply_to_message(
                            actual_chat_id,
                            message_id,
                            "🐾 机器猫的小本本快记满啦！今日40条正选早已满员，后面这些都帮你存进候选池，有空位时优先考虑，感谢理解和支持呀~",
                        )

        save_state(state)
        save_registry(registry)
        total = sum(g.get("count", 0) for g in state.get("groups", {}).values())
        reg_n = sum(len(v) for v in registry.get("entries", {}).values())
        print(
            f"✅ 处理完成 | 本次匹配: {matched} 条 | 日期: {state.get('date')} "
            f"| 各群计数: {state.get('groups')} | 今日收录 X 账号: {reg_n} 个"
        )
    except Exception as e:
        print(f"运行异常: {e}")


if __name__ == "__main__":
    main()
