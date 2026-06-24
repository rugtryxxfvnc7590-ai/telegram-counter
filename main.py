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

def reply_to_message(chat_id, message_id, text):
    """直接回复某条消息，并打印详细错误"""
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Token 或 Chat ID 为空，无法发送")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_to_message_id": message_id
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"发送回复状态: {r.status_code} | 内容: {r.text[:200]}")
        if r.status_code != 200:
            print(f"❌ 发送失败，详细错误: {r.text}")
    except Exception as e:
        print(f"❌ 发送异常: {e}")

def main():
    print(f"Token 长度: {len(BOT_TOKEN) if BOT_TOKEN else 0}")
    print(f"配置的 CHAT_ID: {CHAT_ID}")

    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Secrets 加载失败")
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
        data = resp.json()
        updates = data.get("result", [])

        for update in updates:
            state["offset"] = update["update_id"]
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue

            text = msg["text"]
            actual_chat_id = str(msg["chat"]["id"])
            message_id = msg["message_id"]

            if actual_chat_id != CHAT_ID:
                continue

            if TWITTER_REGEX.search(text):
                state["count"] += 1
                current = state["count"]
                print(f"🔗 检测到链接！当前计数: {current} | 消息ID: {message_id}")

                if current == MAX_LIMIT:
                    reply_to_message(actual_chat_id, message_id, 
                        "截止此处！今日互推链接已满40条，超出部分不予转推！互推规则请看群置顶！")

                elif current > MAX_LIMIT:
                    excess = current - MAX_LIMIT
                    if excess % 3 == 1:
                        if excess <= 3:
                            reply_to_message(actual_chat_id, message_id, 
                                f"{current}已经超过40条了，再发我主人要来打你脑壳啦～")
                        elif excess <= 6:
                            reply_to_message(actual_chat_id, message_id, 
                                f"要命了！！都已经{current}条了，早已经超过40条，再发我要咬人啦～")
                        else:
                            reply_to_message(actual_chat_id, message_id, 
                                f"最高40条，都已经{current}条了！你还发啊？！你完蛋了，放学别走！")

        save_state(state)
        print(f"✅ 处理完成，最终计数: {state['count']}")

    except Exception as e:
        print(f"运行异常: {e}")

if __name__ == "__main__":
    main()
