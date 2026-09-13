from datetime import datetime
import os
from pathlib import Path
from main import BEIJING, load_daily_limits, load_registry, load_state, save_registry, save_state, send_daily_lists_to_owner
from capacity_delivery import process_capacity_replies, send_capacity_reply
from edited_link_receipts import process_edited_link_receipts
from daily_capacity import stamp_admission


def main():
    version = Path(__file__).with_name("VERSION_COUNTER_BOT").read_text().strip()
    print(f"计数监听机器人 v{version}：检查当日私信名单发送与编辑同步。")
    state = load_state()
    registry = load_registry()
    limits = load_daily_limits()
    if registry.get("date") == datetime.now(BEIJING).strftime("%Y-%m-%d"):
        stamp_admission(registry, limits)
        save_registry(registry)
    results = send_daily_lists_to_owner(
        registry,
        state,
        limits=limits,
        save_callback=lambda: save_state(state),
        replace_legacy_groups=legacy_reissue_groups(),
    )
    process_edited_link_receipts(
        registry, state, send_reply=send_capacity_reply,
        owner_chat_id=os.getenv("TELEGRAM_OWNER_CHAT_ID", ""),
        save_callback=lambda: save_state(state),
    )
    process_capacity_replies(registry, state, limits=limits, save_callback=lambda: save_state(state))
    save_state(state)
    return 0


def legacy_reissue_groups(now=None):
    now = now or datetime.now(BEIJING)
    group = os.getenv("OWNER_LIST_REISSUE_GROUP", "")
    day = os.getenv("OWNER_LIST_REISSUE_DATE", "")
    if group in {"群一", "群二", "群三"} and day == now.strftime("%Y-%m-%d"):
        return {group}
    return set()


if __name__ == "__main__":
    raise SystemExit(main())
