"""Keep source-message identity separate from pending-link admission order."""


def _same_member(entry, other):
    return (
        str(entry.get("message_id") or "") == str(other.get("message_id") or "")
        and entry.get("time") == other.get("time")
        and str(entry.get("tg_user_id") or "") == str(other.get("tg_user_id") or "")
        and str(entry.get("promo_handle") or "").lower()
        == str(other.get("promo_handle", other.get("handle")) or "").lower()
    )


def set_admission_time(entry, previous_entries=(), published_slots=None):
    # A confirmed roster takes precedence over provisional ranks in the registry.
    if published_slots is not None:
        admitted = [slot for slot in published_slots if _same_member(entry, slot)]
    else:
        admitted = [old for old in previous_entries or () if _same_member(entry, old)
                    and (old.get("admission_locked") or old.get("daily_list_status") == "accepted")]
    if admitted:
        entry["admission_time"] = min(str(old.get("admission_time") or old["time"]) for old in admitted)
        entry["admission_locked"] = True
    else:
        entry["admission_time"] = str(entry.get("edit_time") or entry.get("time") or "")
        entry["admission_locked"] = False
