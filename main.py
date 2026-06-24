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
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        print(f"已发送: {text[:60]}...")
    except:
        pass

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

            if actual_chat_id != CHAT_ID:
                continue

            if TWITTER_REGEX.search(text):
                state["count"] += 1
                current = state["count"]
                print(f"🔗 检测到链接！当前计数: {current}")

                # === 新的回复逻辑 ===
                if current == MAX_LIMIT:
                    send_message("截止此处！今日互推链接已满40条，超出部分不予转推！互推规则请看群置顶！")

                elif current > MAX_LIMIT:
                    excess = current - MAX_LIMIT
                    if excess % 3 == 1:   # 每超过3条提醒一次（43,46,49...）
                        if excess <= 3:
                            send_message(f"{current}已经超过40条了，再发我主人要来打你脑壳啦～")
                        elif excess <= 6:
                            send_message(f"要命了！！都已经{current}条了，早已经超过40条，再发我要咬人啦～")
                        else:
                            send_message(f"最高40条，都已经{current}条了！你还发啊？！你完蛋了，放学别走！")

        save_state(state)
        print(f"✅ 处理完成，最终计数: {state['count']}")

    except Exception as e:
        print(f"异常: {e}")

if __name__ == "__main__":
    main()
