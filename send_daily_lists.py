from main import load_registry, load_state, save_state, send_daily_lists_to_owner


def main():
    state = load_state()
    results = send_daily_lists_to_owner(
        load_registry(),
        state,
        save_callback=lambda: save_state(state),
    )
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
