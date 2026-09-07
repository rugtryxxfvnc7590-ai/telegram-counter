"""Pure daily-list quota policy, mirrored in the dashboard and counter projects."""
import re

GROUP_CHAT_IDS = {
    "群一": "-1003891628675",
    "群二": "-1003218974409",
    "群三": "-1003739822194",
}
DEFAULT_DAILY_LIMITS = {group: 40 for group in GROUP_CHAT_IDS}
CAPACITY_RULE_LABELS = {
    "limit_full": "达到当日上限",
    "limit_overflow": "超过上限的链接",
    "limit_excess_1": "候选第1阶段（上限 +3）",
    "limit_excess_2": "候选第2阶段（上限 +6）",
    "limit_excess_3": "候选第3阶段（上限 +9 起，每3条）",
}


def normalize_daily_limits(values=None):
    values = values or {}
    result = {}
    for group, default in DEFAULT_DAILY_LIMITS.items():
        value = values.get(group, default)
        try:
            if isinstance(value, bool) or str(value).strip() != str(int(value)):
                raise ValueError("not an integer")
            number = int(value)
            if not 0 <= number <= 500:
                raise ValueError("out of range")
        except (TypeError, ValueError, OverflowError):
            number = default
        result[group] = number
    return result


def capacity_template(text):
    # Migrate only the former quota phrases; preserve other custom wording.
    text = str(text or "")
    text = re.sub(r"(?<!\d)40(?=条|个)", "{limit}", text)
    text = text.replace("前40名", "前{limit}名")
    return text


def render_capacity_text(text, group, limit, count):
    values = {"group": group, "limit": limit, "count": count,
              "overflow_count": max(0, count - limit)}
    for key, value in values.items():
        text = str(text).replace("{" + key + "}", str(value))
    return text


def capacity_rule_for_rank(rank, limit):
    if not limit or rank < limit:
        return ""
    if rank == limit:
        return "limit_full"
    excess = rank - limit
    if excess % 3 == 0:
        return "limit_excess_" + str(min(3, excess // 3))
    return "limit_overflow"


def _message_id(entry):
    try:
        return int(entry.get("message_id") or 0)
    except (TypeError, ValueError):
        return 0


def eligible_rows(registry, chat_ids, day=""):
    day = str(day or (registry or {}).get("date") or "")
    rows = []
    for cid in sorted(str(cid) for cid in chat_ids):
        for post_id, raw in (((registry or {}).get("post_entries") or {}).get(cid) or {}).items():
            entry = raw or {}
            if str(entry.get("promo_post_id") or "") != str(post_id):
                continue
            when = str(entry.get("time") or "")
            if day and when[:10] != day:
                continue
            if entry.get("after_cutoff") or entry.get("mutual_eligible") is not True:
                continue
            if len(when) >= 16 and when[11:16] >= "19:00":
                continue
            handle = str(entry.get("promo_handle") or "").strip().lstrip("@")
            url = (f"https://x.com/{handle}/status/{post_id}" if handle and str(post_id).isdigit()
                   else str(entry.get("promo_url") or entry.get("link") or "").split("?", 1)[0])
            if not re.fullmatch(r"https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d+", url):
                continue
            rows.append({"post_id": str(post_id), "url": url, "time": when,
                         "message_id": _message_id(entry), "entry": entry})
    rows.sort(key=lambda row: (row["time"], row["message_id"], row["url"]))
    unique = []
    seen = set()
    for row in rows:
        if row["post_id"] not in seen:
            unique.append(row)
            seen.add(row["post_id"])
    return unique


def admitted_rows(registry, chat_ids, limit=0, day=""):
    rows = eligible_rows(registry, chat_ids, day)
    return (rows[:limit], rows[limit:]) if limit else (rows, [])


def chat_ids_for_group(group):
    cid = GROUP_CHAT_IDS[group]
    return {cid, "-" + cid[4:]}


def group_for_chat(chat_id):
    return next((group for group in GROUP_CHAT_IDS if str(chat_id) in chat_ids_for_group(group)), "")


def stamp_admission(registry, limits):
    """Persist the policy with this day's registry; never change content eligibility."""
    registry["daily_list_limits"] = dict(limits)
    for group in GROUP_CHAT_IDS:
        ids = chat_ids_for_group(group)
        accepted, waiting = admitted_rows(registry, ids, limits[group])
        ranks = {row["post_id"]: (i + 1, "accepted" if i < len(accepted) else "waitlist")
                 for i, row in enumerate(accepted + waiting)}
        for bucket in ("entries", "post_entries"):
            for cid in ids:
                for entry in ((registry.get(bucket) or {}).get(cid) or {}).values():
                    rank, status = ranks.get(str(entry.get("promo_post_id") or ""), (0, "ineligible"))
                    entry["daily_list_rank"] = rank
                    entry["daily_list_status"] = status
