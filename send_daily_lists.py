from datetime import datetime
from main import BEIJING, load_daily_limits, load_registry, load_state, save_registry, save_state, send_daily_lists_to_owner
from capacity_delivery import process_capacity_replies
from daily_capacity import stamp_admission


def main():
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
    )
    process_capacity_replies(registry, state, limits=limits, save_callback=lambda: save_state(state))
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
