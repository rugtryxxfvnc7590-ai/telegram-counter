import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN").strip() if os.getenv("TELEGRAM_BOT_TOKEN") else None
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID").strip() if os.getenv("TELEGRAM_CHAT_ID") else None

MAX_LIMIT = 40
STATE_FILE = "state.json"

TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/', re.IGNORECASE)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"count": 0, "date": "", "offset": 0}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_beijing_time():
    return datetime.now(timezone.utc) + timedelta(hours=8)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        print(f"已发送提醒: {text[:50]}...")
    except:
        pass

def main():
    print(f"Token 长度: {len(BOT_TOKEN) if BOT_TOKEN else 0}")
    print(f"配置的 CHAT_ID: {CHAT_ID}")

    if not BOT_TOKEN or not CHAT_ID:
        print("Secrets 加载失败")
        return

    print("🎉 Secrets 加载成功！")

    state = load_state()
    today = get_beijing_time().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["count"] = 0
        state["date"] = today
        print("✅ 今日计数已重置")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": state.get("offset", 0) + 1, "timeout": 10, "allowed_updates": ["message"]}

    try:
        resp = requests.get(url, params=params, timeout=15)
        print(f"getUpdates 状态码: {resp.status_code}")

        data = resp.json()
        updates = data.get("result", [])
        print(f"本次获取到 {len(updates)} 条消息")

        for update in updates:
            state["offset"] = update["update_id"]
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue

            text = msg["text"]
            actual_chat_id = str(msg["chat"]["id"])
            print(f"收到消息 | 实际chat_id={actual_chat_id} | 文本长度={len(text)} | 含链接: {bool(TWITTER_REGEX.search(text))}")

            if actual_chat_id != CHAT_ID:
                continue

            if TWITTER_REGEX.search(text):
                state["count"] += 1
                current = state["count"]
                print(f"🔗 成功计数！当前: {current}")

                if current == MAX_LIMIT:
                    send_message("互推链接已到达40条，今日互推名单已截止，后面新链接
