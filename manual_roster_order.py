"""Apply authenticated website administrator swaps, never infer membership."""
from copy import deepcopy
import hashlib
import os
import re
from urllib.parse import urlsplit

import requests

from batch_rosters import flatten_batches, batch_vacancies
from daily_capacity import chat_ids_for_group
from private_list_sync import freeze_links, post_identity
from website_sync import DEFAULT_URL, encode_payload, signed_headers
from withdrawal_sync import beijing_now


def apply_order(record, order, day, registry=None):
    revision = order.get("revision")
    current = record.get("manual_order_revision", 0)
    if order.get("date") != day or type(revision) is not int or revision < 1:
        raise ValueError("invalid_order")
    if current == revision:
        return deepcopy(record)
    if current != order.get("baseRevision", 0) or not record.get("batches"):
        raise ValueError("order_base_changed")
    if order.get("operation") == "add":
        return apply_addition(record, order, day, registry or {})
    moves = order.get("moves")
    if not isinstance(moves, list) or not 2 <= len(moves) <= 300:
        raise ValueError("invalid_moves")
    slots = {slot["position"]: slot for slot in record["slots"]}
    origins, targets, accounts = set(), set(), set()
    for move in moves:
        origin, target = move.get("from"), move.get("to")
        if type(origin) is not int or type(target) is not int or origin in origins or target in targets:
            raise ValueError("duplicate_position")
        slot = slots.get(origin)
        if (not slot or target not in slots or move.get("account") != str(slot["handle"]).lower()
                or move.get("postId") != str(slot["post_id"]) or move["account"] in accounts):
            raise ValueError("roster_changed")
        origins.add(origin)
        targets.add(target)
        accounts.add(move["account"])
    if origins != targets:
        raise ValueError("not_a_permutation")
    result = deepcopy(record)
    destination = {move["from"]: move["to"] for move in moves}
    for batch in result["batches"].values():
        batch["slots"] = []
    for origin, old in slots.items():
        slot = deepcopy(old)
        position = destination.get(origin, origin)
        target_slot = slots[position]
        label, local = target_slot["list_id"], target_slot["list_position"]
        slot.pop("global_position", None)
        if target_slot.get("global_position"):
            slot["global_position"] = position
        slot.update(position=local, list_id=label)
        slot.pop("list_position", None)
        if origin in destination:
            slot["manual_position"] = True
        result["batches"][label]["slots"].append(slot)
        result["assigned_accounts"][slot["handle"].lower()] = label
    for batch in result["batches"].values():
        batch["slots"].sort(key=lambda slot: slot["position"])
        batch["count"] = len(batch["slots"])
    result["slots"] = flatten_batches(result)
    result["manual_order_revision"] = revision
    return result


def apply_addition(record, order, day, registry):
    addition = order.get("addition") or {}
    label, position, local = addition.get("listId"), addition.get("position"), addition.get("listPosition")
    size, count = record["batch_policy"]["size"], record["batch_policy"]["max_batches"]
    handle, post_id = post_identity(addition.get("url"))
    if (label not in record["batches"] or not re.fullmatch(r"[a-z0-9_]{1,15}", handle)
            or addition.get("account") != handle or addition.get("postId") != post_id
            or type(position) is not int or not size * count < position <= 300
            or type(local) is not int or not size < local <= 300):
        raise ValueError("invalid_addition")
    batch = record["batches"][label]
    used_global = {s["position"] for s in record["slots"]} | set(map(int, record.get("vacant_positions", {})))
    used_local = {s["position"] for s in batch["slots"]} | set(map(int, batch["vacant_positions"]))
    if (position in used_global or local in used_local
            or any(s["handle"].lower() == handle or s["post_id"] == post_id for s in record["slots"])):
        raise ValueError("roster_changed")
    result = deepcopy(record)
    url = f"https://x.com/{handle}/status/{post_id}"
    slot = freeze_links([url], registry, chat_ids_for_group("群一"), day)[0]
    slot.update(position=local, global_position=position, list_id=label,
                manual_addition=True, manual_position=True, admitted_at=order["requestedAt"],
                display_name=str(addition.get("displayName") or ""))
    batch = result["batches"][label]
    batch.setdefault("manual_positions", {})[str(local)] = position
    batch["slots"].append(slot)
    batch["slots"].sort(key=lambda s: s["position"])
    batch.update(count=len(batch["slots"]), capacity=max(batch["capacity"], len(batch["slots"])),
                 roster_capacity=max(batch.get("roster_capacity", 0), len(batch["slots"])))
    result["assigned_accounts"][handle] = label
    result.update(slots=flatten_batches(result), vacant_positions=batch_vacancies(result),
                  count=record["count"] + 1, manual_order_revision=order["revision"])
    return result


def sync_manual_roster_order(state, now=None, save_callback=None, secret=None, origin=None, post=None, registry=None):
    now = beijing_now(now)
    day = now.strftime("%Y-%m-%d")
    daily = state.get("daily_rosters") or {}
    record = (daily.get("groups") or {}).get("群一") or {}
    if daily.get("date") != day or not record.get("batches"):
        return "no_batch_roster"
    secret = secret if secret is not None else os.getenv("WEBSITE_SYNC_SECRET", "")
    if not secret:
        return "not_configured"
    origin = (origin or os.getenv("WEBSITE_SYNC_URL") or DEFAULT_URL).rstrip("/")
    url = urlsplit(origin)
    if url.scheme != "https" or not url.netloc or url.path or url.query or url.fragment or url.username:
        raise ValueError("Invalid website origin")
    post = post or requests.post
    path = "/api/sync/orders/group-1"

    def request(payload):
        body = encode_payload(payload)
        request_id = "order-" + hashlib.sha256(body).hexdigest()[:24]
        headers = signed_headers(secret, path, body, int(now.timestamp()), request_id)
        response = post(origin + path, data=body, headers=headers, timeout=20)
        if response.status_code != 200:
            raise ValueError("order_http_" + str(response.status_code))
        return response.json()

    try:
        response = request({"date": day})
        order = response.get("order") or {}
        if order.get("status") not in {"pending", "applied"}:
            return "unchanged"
        if order.get("revision") == record.get("manual_order_revision", 0):
            return "unchanged"
        try:
            result = apply_order(record, order, day, registry=registry)
        except (KeyError, TypeError, ValueError):
            request({"date": day, "revision": order.get("revision"), "status": "rejected"})
            return "roster_changed"
        daily["groups"]["群一"] = result
        if save_callback:
            save_callback()
        return "applied"
    except Exception as exc:
        # Failed pulls leave the website's pending edit and published roster intact.
        print(f"管理员调序同步暂未完成：{type(exc).__name__}，下轮重试。")
        return "retry"
