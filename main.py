import os
import json
import requests
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN").strip() if os.getenv("TELEGRAM_BOT_TOKEN") else None
MAX_LIMIT = 40
STATE_FILE = "state.json"
REGISTRY_FILE = "link_registry.json"
REGISTRY_HISTORY_DIR = "registry_history"

# 群一（10万以上大佬群）→ GitHub Secret TELEGRAM_CHAT_ID_GROUP1
# 群二（5万以下新手营）→ GitHub Secret TELEGRAM_CHAT_ID
# 群三 → GitHub Secret TELEGRAM_CHAT_ID_GROUP3，未配置时使用固定群 ID
GROUP_1_CHAT_ID_FALLBACK = "-1003891628675"
GROUP_3_CHAT_ID_FALLBACK = "-1003739822194"

# Telegram 消息的 date 是 Unix 时间戳，统一换算到中国标准时间。
BEIJING = ZoneInfo("Asia/Shanghai")
TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/', re.IGNORECASE)
X_HANDLE_RE = re.compile(
    r'https?://(?:www\.)?(?:twitter\.com|x\.com)/(@?)([A-Za-z0-9_]{1,15})(?:/|$|\?)',
    re.IGNORECASE,
)
SKIP_HANDLES = frozenset({"i", "intent", "search", "home", "share", "hashtag"})
X_URL_RE = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s\]\)<>\"]+", re.IGNORECASE)
STATUS_RE = re.compile(r"/(?:i/)?status/(\d+)", re.IGNORECASE)
I_STATUS_RE = re.compile(r"/i/status/(\d+)", re.IGNORECASE)
_tweet_author_cache = {}


def expand_chat_id(cid):
    """兼容 Telegram 群 ID 短格式（-3599…）与完整格式（-1003599…）。"""
    cid = str(cid).strip()
    out = {cid}
    if cid.startswith("-100") and len(cid) > 4:
        out.add("-" + cid[4:])
    elif cid.startswith("-") and not cid.startswith("-100"):
        out.add("-100" + cid[1:])
    return out


def load_chat_ids():
    ids = set()
    for env_key in ("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_GROUP1", "TELEGRAM_CHAT_ID_GROUP3"):
        env = os.getenv(env_key, "").strip()
        if env:
            for part in env.split(","):
                part = part.strip()
                if part:
                    ids.update(expand_chat_id(part))
    ids.update(expand_chat_id(GROUP_1_CHAT_ID_FALLBACK))
    if not os.getenv("TELEGRAM_CHAT_ID_GROUP3", "").strip():
        ids.update(expand_chat_id(GROUP_3_CHAT_ID_FALLBACK))
    return sorted(ids)


def limit_reply_enabled(chat_id):
    """群一只收录链接，不发满 40 条/候选名单提示。"""
    return str(chat_id).strip() not in expand_chat_id(GROUP_1_CHAT_ID_FALLBACK)


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
    data.setdefault("post_entries", {})
    return data


def save_registry(registry):
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    save_registry_archive(registry)


def save_registry_archive(registry):
    day = (registry or {}).get("date")
    if not day:
        return
    archive_dir = Path(REGISTRY_HISTORY_DIR)
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{day}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def beijing_day_of(unix_ts):
    return datetime.fromtimestamp(unix_ts, BEIJING).strftime("%Y-%m-%d")


def beijing_full_time(unix_ts):
    return datetime.fromtimestamp(unix_ts, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def resolve_tweet_author(tweet_id):
    """x.com/i/status/推文ID → 反查作者 @（无官方 API）。"""
    tid = str(tweet_id).strip()
    if not tid:
        return None
    if tid in _tweet_author_cache:
        return _tweet_author_cache[tid]
    handle = None
    try:
        r = requests.get(f"https://api.fxtwitter.com/i/status/{tid}", timeout=12)
        if r.status_code == 200:
            data = r.json()
            sn = (data.get("tweet") or {}).get("author", {}).get("screen_name")
            if sn:
                handle = sn.lower()
    except Exception as e:
        print(f"   ⚠️ 推文 {tid} 反查作者失败: {e}")
    _tweet_author_cache[tid] = handle
    return handle


def _handle_from_x_url(url):
    """从单条 X 链接解析用户名；/i/status/ 走推文反查。"""
    i_m = I_STATUS_RE.search(url or "")
    if i_m:
        return resolve_tweet_author(i_m.group(1))
    h_m = X_HANDLE_RE.search(url)
    if not h_m:
        return None
    handle = h_m.group(2).lower()
    if handle in SKIP_HANDLES:
        return None
    return handle


def _status_id_from_x_url(url):
    m = STATUS_RE.search(url or "")
    return m.group(1) if m else ""


def extract_x_links_ordered(text):
    links = []
    seen_urls = set()
    for idx, m in enumerate(X_URL_RE.finditer(text or ""), start=1):
        url = m.group(0).rstrip(".,，。；;：:")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        handle = _handle_from_x_url(url)
        links.append({
            "order": len(links) + 1,
            "url": url,
            "handle": handle or "",
            "post_id": _status_id_from_x_url(url),
            "role": "",
        })
    if links:
        links[0]["role"] = "promo"
    if len(links) >= 2:
        links[1]["role"] = "check"
    for item in links[2:]:
        item["role"] = "extra"
    return links


def extract_handles_ordered(text):
    """按消息里链接出现顺序提取 X 账号（含 /i/status/ 反查）。"""
    seen, ordered = set(), []
    for item in extract_x_links_ordered(text):
        handle = item.get("handle")
        if not handle or handle in seen:
            continue
        seen.add(handle)
        ordered.append(handle)
    return ordered


def extract_x_handles(text):
    return set(extract_handles_ordered(text))


def extract_x_handles_ordered(text):
    return extract_handles_ordered(text)


def parse_check_handle(text):
    """第一条链接为互推原帖号；第二条链接为回推检查号；单链接用该号本身。"""
    ordered = extract_x_handles_ordered(text)
    if not ordered:
        return None, False, None
    if len(ordered) >= 2:
        return ordered[1], True, ordered[0]
    return ordered[0], False, ordered[0]


def _link_by_role(links, role):
    for item in links or []:
        if item.get("role") == role:
            return item
    return {}


def record_link(registry, x_handle, msg, text, chat_id, msg_time,
                check_handle=None, dual_link=False, promo_handle=None, links=None):
    user = msg.get("from", {})
    name = user.get("first_name", "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    promo_link = _link_by_role(links, "promo")
    check_link = _link_by_role(links, "check") or promo_link
    bucket = registry.setdefault("entries", {}).setdefault(chat_id, {})
    entry = {
        "x_handle": x_handle,
        "check_handle": check_handle or x_handle,
        "dual_link": bool(dual_link),
        "promo_handle": promo_handle or x_handle,
        "tg_username": user.get("username") or "",
        "tg_user_id": user.get("id"),
        "tg_name": name.strip() or "未知用户",
        "link": text,
        "message_text": text,
        "links": links or [],
        "promo_url": promo_link.get("url", ""),
        "check_url": check_link.get("url", ""),
        "promo_post_id": promo_link.get("post_id", ""),
        "check_post_id": check_link.get("post_id", ""),
        "chat_id": chat_id,
        "time": msg_time,
    }
    bucket[x_handle] = entry
    post_bucket = registry.setdefault("post_entries", {}).setdefault(chat_id, {})
    for item in links or []:
        post_id = item.get("post_id")
        if post_id:
            post_bucket[post_id] = dict(entry, x_handle=item.get("handle") or x_handle)


def record_post_only(registry, msg, text, chat_id, msg_time, links):
    user = msg.get("from", {})
    name = user.get("first_name", "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    post_bucket = registry.setdefault("post_entries", {}).setdefault(chat_id, {})
    promo_link = _link_by_role(links, "promo")
    check_link = _link_by_role(links, "check") or promo_link
    for item in links or []:
        post_id = item.get("post_id")
        if not post_id:
            continue
        post_bucket[post_id] = {
            "x_handle": item.get("handle") or "",
            "check_handle": check_link.get("handle") or "",
            "dual_link": len(links) >= 2,
            "promo_handle": promo_link.get("handle") or "",
            "tg_username": user.get("username") or "",
            "tg_user_id": user.get("id"),
            "tg_name": name.strip() or "未知用户",
            "link": text,
            "message_text": text,
            "links": links or [],
            "promo_url": promo_link.get("url", ""),
            "check_url": check_link.get("url", ""),
            "promo_post_id": promo_link.get("post_id", ""),
            "check_post_id": check_link.get("post_id", ""),
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

    print(
        f"监听群: {chat_ids}（群一=TELEGRAM_CHAT_ID_GROUP1，"
        "群二=TELEGRAM_CHAT_ID，群三=TELEGRAM_CHAT_ID_GROUP3；仅收录，不私信）"
    )
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
                save_registry_archive(registry)
                registry = {"date": msg_day, "entries": {}}
                print(f"✅ 链接收录已重置: {msg_day}")

            grp = state["groups"].setdefault(actual_chat_id, {"count": 0})
            grp["count"] += 1
            current = grp["count"]
            matched += 1

            user = msg.get("from", {})
            tg_tag = f"@{user['username']}" if user.get("username") else user.get("first_name", "?")
            print(f"🔗 [{msg_time}] 群 {actual_chat_id} 第 {current} 条 | 发送人: {tg_tag}")

            check_handle, dual_link, promo_handle = parse_check_handle(text)
            links = extract_x_links_ordered(text)
            handles = extract_handles_ordered(text)
            if not handles:
                print(f"   ⚠️ 未能从链接解析 X 账号 ← {tg_tag}")
                record_post_only(registry, msg, text, actual_chat_id, msg_time, links)
            for handle in handles:
                record_link(
                    registry, handle, msg, text, actual_chat_id, msg_time,
                    check_handle=check_handle or handle, dual_link=dual_link,
                    promo_handle=promo_handle or handle, links=links,
                )
                extra = ""
                if dual_link and check_handle and handle != check_handle:
                    extra = f" → 查 @{check_handle}"
                elif I_STATUS_RE.search(text):
                    extra = " (i/status反查)"
                print(f"   📝 收录 @{handle} ← {tg_tag}{extra}")

            if limit_reply_enabled(actual_chat_id) and current == MAX_LIMIT:
                reply_to_message(
                    actual_chat_id,
                    message_id,
                    "🐾 叮当~ 今日互推已满40条，前40名已锁定上车！后面发的会被机器猫记进候选名单，如有空位会优先安排哦，辛苦各位啦~记得看群置顶规则呀！",
                )
            elif limit_reply_enabled(actual_chat_id) and current > MAX_LIMIT:
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
