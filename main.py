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
GROUP_REGISTRY_DIR = "registry_groups"

# 群一（10万以上大佬群）→ GitHub Secret TELEGRAM_CHAT_ID_GROUP1
# 群二（5万以下新手营）→ GitHub Secret TELEGRAM_CHAT_ID
# 群三 → GitHub Secret TELEGRAM_CHAT_ID_GROUP3，未配置时使用固定群 ID
GROUP_1_CHAT_ID_FALLBACK = "-1003891628675"
GROUP_2_CHAT_ID_FALLBACK = "-1003218974409"
GROUP_3_CHAT_ID_FALLBACK = "-1003739822194"
LOW_FOLLOWER_REPLY_TEXT = "粉丝数量低于最低互推标准，该链接不予互推！"
TARGET_MENTIONS = ("ToBulaer", "ToBuerma", "KawasawaSen", "BulmaList")
INVALID_MENTIONS_REPLY_TEXT = "推文内容未包含至少2个指定互推账号，该链接不予互推！"

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
_x_author_meta_cache = {}


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


def group_label_for_chat(chat_id):
    cid = str(chat_id).strip()
    if cid in expand_chat_id(GROUP_1_CHAT_ID_FALLBACK):
        return "群一"
    if cid in expand_chat_id(GROUP_2_CHAT_ID_FALLBACK):
        return "群二"
    if cid in expand_chat_id(GROUP_3_CHAT_ID_FALLBACK):
        return "群三"
    return "chat_" + cid.replace("-", "m")


def min_followers_for_chat(chat_id):
    cid = str(chat_id).strip()
    if cid in expand_chat_id(GROUP_1_CHAT_ID_FALLBACK):
        return 100000
    if cid in expand_chat_id(GROUP_3_CHAT_ID_FALLBACK):
        return 0
    return 20000


def compact_number_floor(n, unit):
    value = (int(n) * 10) // int(unit)
    whole, decimal = divmod(value, 10)
    return str(whole) if decimal == 0 else f"{whole}.{decimal}"


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
    save_group_registry_exports(registry)


def save_registry_archive(registry):
    day = (registry or {}).get("date")
    if not day:
        return
    archive_dir = Path(REGISTRY_HISTORY_DIR)
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{day}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def _registry_bucket_for_chat(registry, bucket_name, chat_id):
    bucket = {}
    for cid in expand_chat_id(chat_id):
        items = ((registry or {}).get(bucket_name) or {}).get(cid)
        if items:
            bucket[cid] = items
    return bucket


def _single_group_registry(registry, chat_id):
    return {
        "date": (registry or {}).get("date", ""),
        "entries": _registry_bucket_for_chat(registry, "entries", chat_id),
        "post_entries": _registry_bucket_for_chat(registry, "post_entries", chat_id),
    }


def save_group_registry_exports(registry, base_dir=GROUP_REGISTRY_DIR):
    day = (registry or {}).get("date")
    base = Path(base_dir)
    for chat_id in (GROUP_1_CHAT_ID_FALLBACK, GROUP_2_CHAT_ID_FALLBACK, GROUP_3_CHAT_ID_FALLBACK):
        group_data = _single_group_registry(registry, chat_id)
        group_dir = base / group_label_for_chat(chat_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        with open(group_dir / "link_registry.json", "w", encoding="utf-8") as f:
            json.dump(group_data, f, ensure_ascii=False, indent=2)
        if day:
            history_dir = group_dir / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            with open(history_dir / f"{day}.json", "w", encoding="utf-8") as f:
                json.dump(group_data, f, ensure_ascii=False, indent=2)


def _bounded_list_append(values, value, limit=800):
    values = list(values or [])
    if value not in values:
        values.append(value)
    if len(values) > limit:
        values = values[-limit:]
    return values


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


def format_followers(count):
    try:
        n = int(count)
    except Exception:
        return ""
    if n >= 10000:
        return f"{compact_number_floor(n, 10000)}W"
    if n >= 1000:
        return f"{compact_number_floor(n, 1000)}K"
    return str(n)


def count_required_mentions(text):
    lower = str(text or "").lower()
    return sum(1 for handle in TARGET_MENTIONS if f"@{handle.lower()}" in lower)


def _payload_text(*values):
    for value in values:
        if isinstance(value, dict):
            value = value.get("text") or value.get("full_text")
        value = str(value or "").strip()
        if value:
            return value
    return ""


def _author_meta_from_payload(data):
    tweet = data.get("tweet") or {}
    author = (data.get("tweet") or {}).get("author") or data.get("user") or {}
    if not author:
        return {}
    followers = author.get("followers")
    meta = {
        "screen_name": str(author.get("screen_name") or "").lstrip("@"),
        "name": str(author.get("name") or "").strip(),
        "followers_count": followers,
        "followers_text": format_followers(followers),
        "tweet_text": _payload_text(
            tweet.get("text"),
            tweet.get("full_text"),
            tweet.get("raw_text"),
            data.get("text"),
            data.get("full_text"),
            data.get("raw_text"),
        ),
    }
    return {k: v for k, v in meta.items() if v not in ("", None)}


def fetch_x_author_meta(url="", handle="", post_id=""):
    key = str(post_id or handle or url or "").lower()
    if not key:
        return {}
    if key in _x_author_meta_cache:
        return _x_author_meta_cache[key]
    endpoints = []
    if post_id:
        endpoints.append(f"https://api.fxtwitter.com/i/status/{post_id}")
    if handle:
        endpoints.append(f"https://api.fxtwitter.com/{handle.lstrip('@')}")
    meta = {}
    if not hasattr(requests, "get"):
        _x_author_meta_cache[key] = meta
        return meta
    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, timeout=8)
            if r.status_code != 200:
                continue
            meta = _author_meta_from_payload(r.json())
            if meta:
                break
        except Exception as e:
            print(f"   ⚠️ X 作者信息读取失败: {endpoint} | {e}")
    _x_author_meta_cache[key] = meta
    return meta


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
        meta = fetch_x_author_meta(url, handle=links[-1]["handle"], post_id=links[-1]["post_id"])
        if meta:
            links[-1]["author_name"] = meta.get("name", "")
            links[-1]["followers_count"] = meta.get("followers_count", "")
            links[-1]["followers_text"] = meta.get("followers_text", "")
            links[-1]["tweet_text"] = meta.get("tweet_text", "")
            links[-1]["required_mentions_count"] = count_required_mentions(meta.get("tweet_text", ""))
            if meta.get("screen_name"):
                links[-1]["handle"] = meta["screen_name"].lower()
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
    return parse_check_handle_from_handles(ordered)


def parse_check_handle_from_handles(ordered):
    if not ordered:
        return None, False, None
    if len(ordered) >= 2:
        return ordered[1], True, ordered[0]
    return ordered[0], False, ordered[0]


def handles_from_links(links):
    seen, ordered = set(), []
    for item in links or []:
        handle = item.get("handle")
        if not handle or handle in seen:
            continue
        seen.add(handle)
        ordered.append(handle)
    return ordered


def _link_by_role(links, role):
    for item in links or []:
        if item.get("role") == role:
            return item
    return {}


def follower_count_from_link(link):
    try:
        return int((link or {}).get("followers_count"))
    except Exception:
        return None


def update_eligibility_fields(entry, chat_id):
    if not isinstance(entry, dict):
        return False
    before = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    minimum = min_followers_for_chat(chat_id)
    entry["followers_min"] = minimum
    try:
        followers = int(entry.get("followers_count"))
    except Exception:
        followers = None
    if entry.get("content_eligible") is False:
        entry["mutual_eligible"] = False
        entry["eligibility_text"] = "❌缺少指定@"
        entry["ineligible_reason"] = "missing_required_mentions"
    elif followers is not None and followers < minimum:
        entry["mutual_eligible"] = False
        entry["eligibility_text"] = "❌粉丝不足"
        entry["ineligible_reason"] = "followers_below_minimum"
    elif entry.get("content_eligible") is None:
        entry["mutual_eligible"] = True
        entry["eligibility_text"] = "待确认"
        entry.pop("ineligible_reason", None)
    elif followers is None and minimum > 0:
        entry["mutual_eligible"] = True
        entry["eligibility_text"] = "待确认"
        entry.pop("ineligible_reason", None)
    else:
        entry["mutual_eligible"] = True
        entry["eligibility_text"] = "✅合格"
        entry.pop("ineligible_reason", None)
    after = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    return after != before


def promo_link_below_minimum(links, chat_id):
    promo_link = _link_by_role(links, "promo")
    followers = follower_count_from_link(promo_link)
    minimum = min_followers_for_chat(chat_id)
    return followers is not None and followers < minimum


def promo_link_missing_required_mentions(links):
    promo_link = _link_by_role(links, "promo")
    if not promo_link.get("tweet_text"):
        return False
    try:
        count = int(promo_link.get("required_mentions_count"))
    except Exception:
        count = count_required_mentions(promo_link.get("tweet_text", ""))
    return count < 2


def promo_link_content_eligible(links):
    promo_link = _link_by_role(links, "promo")
    if not promo_link.get("tweet_text"):
        return None
    return not promo_link_missing_required_mentions(links)


def remove_registry_rows_for_message(registry, chat_id, message_id):
    if not message_id:
        return []
    removed = []
    for bucket_name in ("entries", "post_entries"):
        bucket = registry.setdefault(bucket_name, {}).setdefault(chat_id, {})
        for key, entry in list(bucket.items()):
            if str((entry or {}).get("message_id") or "") == str(message_id):
                removed.append(dict(entry or {}))
                bucket.pop(key, None)
    return removed


def _link_option(label, url="", post_id="", handle="", source_time="", edit_time=""):
    url = str(url or "").strip()
    if not url:
        return None
    return {
        "label": label,
        "url": url,
        "post_id": str(post_id or _status_id_from_x_url(url)),
        "handle": str(handle or _handle_from_x_url(url) or "").lstrip("@").lower(),
        "source_time": source_time,
        "edit_time": edit_time,
    }


def build_link_options(links, previous_entries=None, msg_time=""):
    options = []
    seen = set()

    promo_link = _link_by_role(links, "promo")
    current = _link_option(
        "编辑后" if previous_entries else "当前",
        promo_link.get("url", ""),
        promo_link.get("post_id", ""),
        promo_link.get("handle", ""),
        msg_time,
    )
    if current:
        options.append(current)
        seen.add(current["url"])

    for entry in previous_entries or []:
        option = _link_option(
            "编辑前",
            entry.get("promo_url", ""),
            entry.get("promo_post_id", ""),
            entry.get("promo_handle", ""),
            entry.get("time", ""),
            entry.get("edit_time", ""),
        )
        if option and option["url"] not in seen:
            options.append(option)
            seen.add(option["url"])
    return options


def record_link(registry, x_handle, msg, text, chat_id, msg_time,
                check_handle=None, dual_link=False, promo_handle=None, links=None,
                previous_entries=None):
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
        "link_options": build_link_options(links or [], previous_entries, msg_time),
        "x_name": promo_link.get("author_name", ""),
        "promo_name": promo_link.get("author_name", ""),
        "followers_count": promo_link.get("followers_count", ""),
        "followers_text": promo_link.get("followers_text", ""),
        "tweet_text": promo_link.get("tweet_text", ""),
        "required_mentions_count": promo_link.get("required_mentions_count", ""),
        "content_eligible": promo_link_content_eligible(links),
        "promo_url": promo_link.get("url", ""),
        "check_url": check_link.get("url", ""),
        "promo_post_id": promo_link.get("post_id", ""),
        "check_post_id": check_link.get("post_id", ""),
        "chat_id": chat_id,
        "message_id": msg.get("message_id", ""),
        "time": msg_time,
        "edited": bool(msg.get("edit_date")),
        "edit_time": beijing_full_time(msg["edit_date"]) if msg.get("edit_date") else "",
    }
    update_eligibility_fields(entry, chat_id)
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
            "link_options": build_link_options(links or [], [], msg_time),
            "x_name": promo_link.get("author_name", ""),
            "promo_name": promo_link.get("author_name", ""),
            "followers_count": promo_link.get("followers_count", ""),
            "followers_text": promo_link.get("followers_text", ""),
            "tweet_text": promo_link.get("tweet_text", ""),
            "required_mentions_count": promo_link.get("required_mentions_count", ""),
            "content_eligible": promo_link_content_eligible(links),
            "promo_url": promo_link.get("url", ""),
            "check_url": check_link.get("url", ""),
            "promo_post_id": promo_link.get("post_id", ""),
            "check_post_id": check_link.get("post_id", ""),
            "chat_id": chat_id,
            "message_id": msg.get("message_id", ""),
            "time": msg_time,
            "edited": bool(msg.get("edit_date")),
            "edit_time": beijing_full_time(msg["edit_date"]) if msg.get("edit_date") else "",
        }
        update_eligibility_fields(post_bucket[post_id], chat_id)


def enrich_entry_metadata(entry):
    if not isinstance(entry, dict):
        return False
    changed = False
    url = entry.get("promo_url") or entry.get("link") or ""
    handle = entry.get("promo_handle") or entry.get("x_handle") or ""
    post_id = entry.get("promo_post_id") or _status_id_from_x_url(url)
    if not (entry.get("x_name") and entry.get("followers_count") and entry.get("tweet_text")):
        meta = fetch_x_author_meta(url, handle=handle, post_id=post_id)
        if meta.get("name") and not entry.get("x_name"):
            entry["x_name"] = meta["name"]
            entry["promo_name"] = meta["name"]
            changed = True
        if meta.get("followers_count") not in ("", None) and not entry.get("followers_count"):
            entry["followers_count"] = meta["followers_count"]
            changed = True
        if meta.get("tweet_text") and not entry.get("tweet_text"):
            entry["tweet_text"] = meta["tweet_text"]
            changed = True
    if entry.get("tweet_text"):
        mention_count = count_required_mentions(entry.get("tweet_text", ""))
        if entry.get("required_mentions_count") != mention_count:
            entry["required_mentions_count"] = mention_count
            changed = True
        content_eligible = mention_count >= 2
        if entry.get("content_eligible") is not content_eligible:
            entry["content_eligible"] = content_eligible
            changed = True
    formatted = format_followers(entry.get("followers_count"))
    if formatted and entry.get("followers_text") != formatted:
        entry["followers_text"] = formatted
        changed = True
    if update_eligibility_fields(entry, entry.get("chat_id", "")):
        changed = True
    return changed


def backfill_registry_metadata(registry):
    changed = 0
    for bucket_group in ("entries", "post_entries"):
        for bucket in (registry.get(bucket_group) or {}).values():
            for entry in (bucket or {}).values():
                if enrich_entry_metadata(entry):
                    changed += 1
    return changed


def reply_to_message(chat_id, message_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "reply_to_message_id": message_id}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"群内回复状态: {r.status_code} | 消息ID: {message_id}")
    except Exception as e:
        print(f"回复异常: {e}")


def reply_to_message_once(group_state, chat_id, message_id, reason, text):
    key = f"{message_id}:{reason}"
    sent = set(group_state.get("reply_keys") or [])
    if key in sent:
        return
    reply_to_message(chat_id, message_id, text)
    group_state["reply_keys"] = _bounded_list_append(group_state.get("reply_keys"), key, limit=800)


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
    params = {"offset": state.get("offset", 0) + 1, "timeout": 10, "allowed_updates": ["message", "edited_message"]}

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
            msg = update.get("message") or update.get("edited_message")
            is_edited_message = "edited_message" in update
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
                registry = {"date": msg_day, "entries": {}, "post_entries": {}}
                print(f"✅ 链接收录已重置: {msg_day}")

            grp = state["groups"].setdefault(actual_chat_id, {"count": 0})
            message_key = str(message_id)
            seen_message_ids = set(str(x) for x in (grp.get("message_ids") or []))
            is_new_message = message_key not in seen_message_ids
            if is_new_message:
                grp["count"] += 1
                grp["message_ids"] = _bounded_list_append(grp.get("message_ids"), message_key, limit=1200)
            current = grp["count"]
            matched += 1

            user = msg.get("from", {})
            tg_tag = f"@{user['username']}" if user.get("username") else user.get("first_name", "?")
            edit_note = " | 编辑消息" if is_edited_message else ""
            print(f"🔗 [{msg_time}] 群 {actual_chat_id} 第 {current} 条 | 发送人: {tg_tag}{edit_note}")

            links = extract_x_links_ordered(text)
            handles = handles_from_links(links)
            check_handle, dual_link, promo_handle = parse_check_handle_from_handles(handles)
            previous_entries = remove_registry_rows_for_message(registry, actual_chat_id, message_id)
            removed = len(previous_entries)
            if removed:
                print(f"   ♻️ 同一 Telegram 消息已更新，移除旧登记 {removed} 条")
            if not handles:
                print(f"   ⚠️ 未能从链接解析 X 账号 ← {tg_tag}")
                record_post_only(registry, msg, text, actual_chat_id, msg_time, links)
            for handle in handles:
                record_link(
                    registry, handle, msg, text, actual_chat_id, msg_time,
                    check_handle=check_handle or handle, dual_link=dual_link,
                    promo_handle=promo_handle or handle, links=links,
                    previous_entries=previous_entries,
                )
                extra = ""
                if dual_link and check_handle and handle != check_handle:
                    extra = f" → 查 @{check_handle}"
                elif I_STATUS_RE.search(text):
                    extra = " (i/status反查)"
                print(f"   📝 收录 @{handle} ← {tg_tag}{extra}")

            if promo_link_below_minimum(links, actual_chat_id):
                reply_to_message_once(grp, actual_chat_id, message_id, "low_followers", LOW_FOLLOWER_REPLY_TEXT)
            elif promo_link_missing_required_mentions(links):
                reply_to_message_once(grp, actual_chat_id, message_id, "missing_mentions", INVALID_MENTIONS_REPLY_TEXT)
            elif limit_reply_enabled(actual_chat_id) and is_new_message and current == MAX_LIMIT:
                reply_to_message_once(
                    grp,
                    actual_chat_id,
                    message_id,
                    "limit_full",
                    "🐾 叮当~ 今日互推已满40条，前40名已锁定上车！后面发的会被机器猫记进候选名单，如有空位会优先安排哦，辛苦各位啦~记得看群置顶规则呀！",
                )
            elif limit_reply_enabled(actual_chat_id) and is_new_message and current > MAX_LIMIT:
                excess = current - MAX_LIMIT
                if excess % 3 == 1:
                    if excess <= 3:
                        reply_to_message_once(
                            grp,
                            actual_chat_id,
                            message_id,
                            "limit_excess_1",
                            "🐾 机器猫收到啦~ 不过今日40个名额已满，你这条先帮你放进候选名单排队啦，有机会就给你顶上去！",
                        )
                    elif excess <= 6:
                        reply_to_message_once(
                            grp,
                            actual_chat_id,
                            message_id,
                            "limit_excess_2",
                            "🐾 又有新链接~ 机器猫已经悄悄记下，放进候选备用区啦。今日正选已满，这些会作为优先候选，辛苦再等等~",
                        )
                    else:
                        reply_to_message_once(
                            grp,
                            actual_chat_id,
                            message_id,
                            "limit_excess_3",
                            "🐾 机器猫的小本本快记满啦！今日40条正选早已满员，后面这些都帮你存进候选池，有空位时优先考虑，感谢理解和支持呀~",
                        )

        enriched = backfill_registry_metadata(registry)
        if enriched:
            print(f"✅ 已补齐 X 昵称/粉丝字段: {enriched} 条")
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
