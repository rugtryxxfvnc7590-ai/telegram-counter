"""Website-only tombstones; private delivery and admission rules stay unchanged."""
from copy import deepcopy
import re

from withdrawal_sync import beijing_now


def capture_website_deletions(registry, state, now=None):
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    evidence = registry.get("confirmed_withdrawals") or {}
    if registry.get("date") != day or evidence.get("date") != day:
        return
    ledger = state.setdefault("website_deletions", {})
    if ledger.get("date") != day:
        ledger.clear()
        ledger.update(date=day, groups={})
    # Capture before admission removes the old slot. Never infer deletion from
    # a failed API call, an incomplete snapshot, or an unqualified new link.
    for name in ("daily_rosters", "owner_daily_lists"):
        source = state.get(name) or {}
        if source.get("date") != day:
            continue
        for group, record in (source.get("groups") or {}).items():
            confirmations = (evidence.get("groups") or {}).get(group) or {}
            for slot in record.get("slots") or []:
                mid = str(slot.get("message_id") or "")
                proof = confirmations.get(mid) or {}
                stamp = str(proof.get("confirmed_at") or "")
                if stamp[:10] != day or stamp > now.strftime("%Y-%m-%d %H:%M:%S"):
                    continue
                account = str(slot.get("handle") or "").lstrip("@").lower()
                post_id = str(slot.get("post_id") or "")
                position = slot.get("position")
                if (not re.fullmatch(r"[a-z0-9_]{1,15}", account) or not post_id.isdigit()
                        or not isinstance(position, int) or not 1 <= position <= 300):
                    continue
                ledger["groups"].setdefault(group, {}).setdefault(mid, {
                    "position": position, "account": account, "postId": post_id,
                    "confirmed_at": stamp,
                })


def deleted_entries(state, group, day):
    ledger = state.get("website_deletions") or {}
    if ledger.get("date") != day:
        return {}
    return (ledger.get("groups") or {}).get(group) or {}


def website_items(record, deletions):
    from daily_roster import roster_items

    items = {item["position"]: item for item in roster_items(record)}
    slots = {slot["position"]: slot for slot in record.get("slots") or []}
    for mid, removed in sorted(deletions.items(), key=lambda pair: (pair[1]["confirmed_at"], pair[0])):
        position, account = removed["position"], removed["account"]
        item = items.setdefault(position, {"position": position, "url": None,
                                          "note": "", "isReplacement": False})
        accounts = item.setdefault("deletedAccounts", [])
        if account not in accounts:
            accounts.append(account)
        if str(slots.get(position, {}).get("message_id") or "") == mid or item["url"] is None:
            item.update(url=None, account=account, note="链接已删除", isReplacement=False)
    return deepcopy(sorted(items.values(), key=lambda item: item["position"]))
