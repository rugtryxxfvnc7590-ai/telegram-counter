"""One stable daily roster shared by the website and owner delivery."""
from copy import deepcopy

from daily_capacity import GROUP_CHAT_IDS, admitted_rows, chat_ids_for_group, eligible_rows
from private_list_sync import edited_slots, freeze_links
from withdrawal_sync import beijing_now, plan_roster


def refresh_daily_rosters(registry, state, limits, now=None, save_callback=None):
    now = beijing_now(now)
    day, now_text = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
    ledger = state.get("daily_rosters") or {}
    snapshot = state.get("group_snapshot_sync") or {}
    if registry.get("date") != day or snapshot.get("date") != day:
        return {}
    if now.hour >= 19:
        return ledger.get("groups", {}) if ledger.get("date") == day else {}
    if ledger.get("date") != day:
        ledger = {"date": day, "groups": {}}
        state["daily_rosters"] = ledger
    owners = state.get("owner_daily_lists") or {}
    for group, chat_id in GROUP_CHAT_IDS.items():
        if group not in snapshot.get("completed_groups", []):
            continue
        owner = ((owners.get("groups") or {}).get(group) or {}) if owners.get("date") == day else {}
        previous = ledger["groups"].get(group)
        seed = deepcopy(previous if previous is not None else owner if owner.get("sent") else None)
        ids = chat_ids_for_group(group)
        limit = limits[group]
        if seed is not None and seed.get("slots") is None:
            seed["slots"] = freeze_links(seed.get("links") or [], registry, ids, day)
        if seed is None:
            admitted, _ = admitted_rows(registry, ids, limit, day)
            slots = freeze_links([row["url"] for row in reversed(admitted) if row["time"] <= now_text], registry, ids, day)
            seed = {"slots": slots, "count": len(slots), "roster_capacity": len(slots)}
        plan = plan_roster(seed, registry, chat_id, day, now_text, limit)
        slots = edited_slots(plan["slots"], registry, ids, day, now_text)
        # Before the first owner delivery, admit new arrivals up to the configured
        # capacity. Already published positions never move as more people join.
        if not owner.get("sent"):
            occupied = {slot["position"] for slot in slots}
            used_messages = {slot.get("message_id") for slot in slots} | set(plan["withdrawn_message_ids"])
            used_posts = {slot["post_id"] for slot in slots}
            rows = eligible_rows(registry, ids, day)
            target = limit or len(rows)
            for row in rows:
                if len(slots) >= target:
                    break
                if (str(row["message_id"]) in used_messages or row["post_id"] in used_posts
                        or row["time"] > now_text):
                    continue
                bound = freeze_links([row["url"]], registry, ids, day)[0]
                if bound.get("message_id") != str(row["message_id"]):
                    continue
                position = max(occupied | {int(p) for p in plan["vacant_positions"]}, default=0) + 1
                bound["position"] = position
                slots.append(bound)
                occupied.add(position)
                used_messages.add(bound["message_id"])
                used_posts.add(bound["post_id"])
        plan["capacity"] = max(plan["capacity"], len(slots) + len(plan["vacant_positions"]))
        plan["slots"] = sorted(slots, key=lambda slot: slot["position"])
        current = dict(plan, roster_capacity=plan["capacity"], count=len(slots))
        if current != previous:
            ledger["groups"][group] = current
            if save_callback:
                save_callback()
    return ledger["groups"]


def roster_items(record):
    items = [{"position": slot["position"], "url": slot["url"],
              "note": "已编辑" if slot.get("edited") else "",
              "isReplacement": bool(slot.get("is_replacement"))} for slot in record.get("slots", [])]
    for position in (record.get("vacant_positions") or {}):
        items.append({"position": int(position), "url": None, "note": "等待候补", "isReplacement": False})
    return sorted(items, key=lambda item: item["position"])


def final_owner_rosters(registry, state, limits, now=None):
    """Include arrivals before cutoff in a first private delivery after cutoff.

    The website keeps its last successful pre-19:00 snapshot. Build the final
    private roster from a copy so late polling neither alters that snapshot nor
    discards eligible arrivals since the previous poll. Existing slots retain
    their positions; normal eligibility still excludes arrivals at/after 19:00.
    """
    now = beijing_now(now)
    snapshot = dict(state, daily_rosters=deepcopy(state.get("daily_rosters") or {}))
    cutoff = now.replace(hour=18, minute=59, second=59, microsecond=0)
    return refresh_daily_rosters(registry, snapshot, limits, now=cutoff)


def admission_slots(state, group, day):
    ledger = state.get("daily_rosters") or {}
    if ledger.get("date") != day:
        return []
    return ((ledger.get("groups") or {}).get(group) or {}).get("slots") or []
