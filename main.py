import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

# 使用你当前 Secrets 的名称
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
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
    utc_now = datetime.now(timezone.utc)
    beijing = utc_now + timedelta(hours=8)
    return beijing

def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Token 或 Chat ID 未加载")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        print(f"发送状态: {r.status_code}")
    except Exception as e:
        print(f"发送失败: {e}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Secrets 未正确设置！")
        print(f"BOT_TOKEN 是否为空: {BOT_TOKEN is None}")
        print(f"CHAT_ID 是否为空: {CHAT_ID is None}")
        return

    print(f"✅ Token 和 Chat ID 加载成功！")
    # ... 其余代码保持不变（使用上面的调试版 main.py 其他部分）

    state = load_state()
    print(f"当前状态: count={state.get('count',0)}")

    today = get_beijing_time().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["count"] = 0
        state["date"] = today
        print("✅ 已重置今日计数")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": state.get("offset", 0) + 1, "timeout": 10}

    try:
        response = requests.get(url, params=params, timeout=15)
        print(f"API 状态码: {response.status_code}")

        data = response.json()
        if not data.get("ok"):
            print("API 错误:", data)
            return

        updates = data.get("result", [])
        print(f"获取到 {len(updates)} 条消息")

        for update in updates:
            state["offset"] = update["update_id"]
            message = update.get("message")
            if not message or "text" not in message:
                continue

            text = message["text"]
            chat_id = str(message["chat"]["id"])

            if chat_id != str(CHAT_ID):
                continue

            has_link = bool(TWITTER_REGEX.search(text))
            print(f"包含链接: {has_link} | 文本: {text[:80]}...")

            if has_link:
                state["count"] += 1
                current = state["count"]
                print(f"🔗 计数 +1 → 当前 {current} 条")

                if current == MAX_LIMIT:
                    send_message("互推链接已到达40条，今日互推名单已截止，后面新链接将不被转推！")
                elif current > MAX_LIMIT:
                    send_message(f"当前互推链接（{current}）条已超出40条，今日互推仅转前40条，超出部分不予转推！")

        save_state(state)
        print(f"✅ 处理完成，最终计数: {state['count']}")

    except Exception as e:
        print(f"异常: {e}")

if __name__ == "__main__":
    main()
