"""One stable daily roster shared by the website and owner delivery."""
from copy import deepcopy

from daily_capacity import GROUP_CHAT_IDS, admitted_rows, chat_ids_for_group, eligible_rows
from private_list_sync import edited_slots, freeze_links, order_roster_slots
from withdrawal_sync import beijing_now, plan_roster
from website_deletions import capture_website_deletions


def refresh_daily_rosters(registry, state, limits, now=None, save_callback=None):
    now = beijing_now(now)
    day, now_text = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
    ledger = state.get("daily_rosters") or {}
    snapshot = state.get("group_snapshot_sync") or {}
    if registry.get("date") != day or snapshot.get("date") != day:
        return {}
    capture_website_deletions(registry, state, now)
    if now.hour >= 19:
        from batch_rosters import batch_policy, refresh_batch_roster
        for group in snapshot.get("completed_groups", []):
            previous = (ledger.get("groups") or {}).get(group) if ledger.get("date") == day else None
            policy = (previous or {}).get("batch_policy") or batch_policy(group, day)
            if not policy:
                continue
            if ledger.get("date") != day:
                ledger = {"date": day, "groups": {}}
                state["daily_rosters"] = ledger
            if not (previous or {}).get("cutoff_finalized"):
                previous = refresh_batch_roster(previous, registry, group, day, day + " 18:59:59", policy)
            current = refresh_batch_roster(previous, registry, group, day, now_text, policy)
            current["cutoff_finalized"] = True
            ledger["groups"][group] = current
            if save_callback:
                save_callback()
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
        from batch_rosters import batch_policy, refresh_batch_roster
        policy = (previous or {}).get("batch_policy") or batch_policy(group, day)
        if policy:
            current = refresh_batch_roster(previous, registry, group, day, now_text, policy)
            if current != previous:
                ledger["groups"][group] = current
                if save_callback:
                    save_callback()
            continue
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
        # Before the first owner delivery, admit arrivals without changing membership.
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
        plan["slots"] = order_roster_slots(slots, plan["vacant_positions"])
        current = dict(plan, roster_capacity=plan["capacity"], count=len(slots), admission_limit=limit)
        if current != previous:
            ledger["groups"][group] = current
            if save_callback:
                save_callback()
    return ledger["groups"]


def roster_items(record):
    items = [{"position": slot["position"], "url": slot["url"],
              "note": "已编辑" if slot.get("edited") else "",
              "isReplacement": bool(slot.get("is_replacement")),
              **({"listId": slot["list_id"], "listPosition": slot["list_position"],
                  "displayName": slot.get("display_name", "")} if slot.get("list_id") else {})}
             for slot in record.get("slots", [])]
    for position in (record.get("vacant_positions") or {}):
        item = {"position": int(position), "url": None, "note": "等待候补", "isReplacement": False}
        if record.get("batches"):
            size = record["batch_policy"]["size"]
            item.update(listId=chr(65 + (int(position) - 1) // size), listPosition=(int(position) - 1) % size + 1)
        items.append(item)
    return sorted(items, key=lambda item: item["position"])


def final_owner_rosters(registry, state, limits, now=None):
    """Include arrivals before cutoff in a first private delivery after cutoff.

    The website keeps its last successful pre-19:00 snapshot. Build the final
    private roster from a copy so late polling neither alters that snapshot nor
    discards eligible arrivals since the previous poll. Existing slots retain
    their positions; normal eligibility still excludes arrivals at/after 19:00.
    """
    now = beijing_now(now)
    snapshot = dict(state, daily_rosters=deepcopy(state.get("daily_rosters") or {}),
                    website_deletions=deepcopy(state.get("website_deletions") or {}))
    cutoff = now.replace(hour=18, minute=59, second=59, microsecond=0)
    return refresh_daily_rosters(registry, snapshot, limits, now=cutoff)


def admission_slots(state, group, day):
    ledger = state.get("daily_rosters") or {}
    if ledger.get("date") != day:
        return []
    return ((ledger.get("groups") or {}).get(group) or {}).get("slots") or []
