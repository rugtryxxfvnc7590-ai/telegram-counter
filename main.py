import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN").strip() if os.getenv("TELEGRAM_BOT_TOKEN") else None
CHAT_ID = "-5525900243"      # 要监听的新群
MY_CHAT_ID = "8614747348"    # 私信推送给你（你的私人 chat_id）
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

def format_sender(msg):
    """整理出发送人的显示名字 + @用户名"""
    user = msg.get("from", {})
    name = user.get("first_name", "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    username = user.get("username")
    if username:
        return f"{name}（@{username}）"
    return name or "未知用户"

def send_dm(text):
    """私信推送给你本人"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": MY_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.json().get("ok"):
            print("📩 已私信推送")
        else:
            print(f"❌ 私信失败（你可能还没私聊过机器人）: {r.text}")
    except Exception as e:
        print(f"私信异常: {e}")

def reply_to_message(chat_id, message_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "reply_to_message_id": message_id}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"群内回复状态: {r.status_code} | 消息ID: {message_id}")
    except Exception as e:
        print(f"回复异常: {e}")

def main():
    print(f"监听群: {CHAT_ID} | 私信对象: {MY_CHAT_ID}")
    state = load_state()
    today = get_beijing_time().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["count"] = 0
        state["date"] = today
        print("✅ 今日计数已重置（北京时间新的一天）")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": state.get("offset", 0) + 1, "timeout": 10, "allowed_updates": ["message"]}

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            print(f"❌ Telegram API 返回错误(getUpdates): {data}")
            return

        updates = data.get("result", [])
        print(f"本次获取到 {len(updates)} 条更新")
        for update in updates:
            state["offset"] = update["update_id"]
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue
            text = msg["text"]
            actual_chat_id = str(msg["chat"]["id"])
            message_id = msg["message_id"]

            if actual_chat_id == CHAT_ID and TWITTER_REGEX.search(text):
                state["count"] += 1
                current = state["count"]
                sender = format_sender(msg)
                print(f"🔗 检测到链接！当前计数: {current} | 发送人: {sender}")

                # 前40条（含第40条）：私信推送给你，并注明发送人
                if current <= MAX_LIMIT:
                    send_dm(f"📌 第 {current}/{MAX_LIMIT} 条互推链接\n发送人：{sender}\n链接：{text}")

                # 群内提醒逻辑
                if current == MAX_LIMIT:
                    reply_to_message(actual_chat_id, message_id, "截止此处！今日互推链接已满40条，超出部分不予转推！互推规则请看群置顶！")
                elif current > MAX_LIMIT:
                    excess = current - MAX_LIMIT
                    if excess % 3 == 1:
                        if excess <= 3:
                            reply_to_message(actual_chat_id, message_id, f"{current}已经超过40条了，再发我主人要来打你脑壳啦～")
                        elif excess <= 6:
                            reply_to_message(actual_chat_id, message_id, f"要命了！！都已经{current}条了，早已经超过40条，再发我要咬人啦～")
                        else:
                            reply_to_message(actual_chat_id, message_id, f"最高40条，都已经{current}条了！你还发啊？！你完蛋了，放学别走！")

        save_state(state)
        print(f"✅ 处理完成，最终计数: {state['count']}")
    except Exception as e:
        print(f"运行异常: {e}")

if __name__ == "__main__":
    main()
