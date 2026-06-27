import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN").strip() if os.getenv("TELEGRAM_BOT_TOKEN") else None
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID").strip() if os.getenv("TELEGRAM_CHAT_ID") else None
MY_CHAT_ID = "8614747348"
MAX_LIMIT = 40
STATE_FILE = "state.json"

BEIJING = timezone(timedelta(hours=8))
TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/', re.IGNORECASE)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"count": 0, "date": "", "offset": 0}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def beijing_day_of(unix_ts):
    return datetime.fromtimestamp(unix_ts, BEIJING).strftime("%Y-%m-%d")

def beijing_full_time(unix_ts):
    return datetime.fromtimestamp(unix_ts, BEIJING).strftime("%Y-%m-%d %H:%M:%S")

def format_sender(msg):
    user = msg.get("from", {})
    name = user.get("first_name", "")
    if user.get("last_name"):
        name += " " + user["last_name"]
    username = user.get("username")
    if username:
        return f"{name}（@{username}）"
    return name or "未知用户"

def send_dm(text):
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
    if not CHAT_ID:
        print("❌ 没读到 TELEGRAM_CHAT_ID，请检查 Secret")
        return

    print(f"监听群已加载（私信对象: {MY_CHAT_ID}）")
    state = load_state()

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
        matched = 0
        for update in updates:
            state["offset"] = update["update_id"]
            msg = update.get("message")
            if not msg or "text" not in msg:
                continue
            text = msg["text"]
            actual_chat_id = str(msg["chat"]["id"])
            message_id = msg["message_id"]

            if actual_chat_id != CHAT_ID or not TWITTER_REGEX.search(text):
                continue

            msg_day = beijing_day_of(msg["date"])
            msg_time = beijing_full_time(msg["date"])

            if state.get("date") != msg_day:
                state["count"] = 0
                state["date"] = msg_day
                print(f"✅ 计数已重置，进入新的一天: {msg_day}")

            matched += 1
            state["count"] += 1
            current = state["count"]
            sender = format_sender(msg)
            print(f"🔗 [{msg_time}] 检测到链接！当前计数: {current} | 发送人: {sender}")

            if current <= MAX_LIMIT:
                send_dm(
                    f"📌 第 {current}/{MAX_LIMIT} 条互推链接\n"
                    f"发送人：{sender}\n"
                    f"发送时间：{msg_time}\n"
                    f"链接：{text}"
                )

            # 群内回复（机器猫人设·高情商·候选备用）
            if current == MAX_LIMIT:
                reply_to_message(actual_chat_id, message_id,
                    "🐾 叮当~ 今日互推已满40条，前40名已锁定上车！后面发的会被机器猫记进候选名单，如有空位会优先安排哦，辛苦各位啦~记得看群置顶规则呀！")
            elif current > MAX_LIMIT:
                excess = current - MAX_LIMIT
                if excess % 3 == 1:
                    if excess <= 3:
                        reply_to_message(actual_chat_id, message_id,
                            "🐾 机器猫收到啦~ 不过今日40个名额已满，你这条先帮你放进候选名单排队啦，有机会就给你顶上去！")
                    elif excess <= 6:
                        reply_to_message(actual_chat_id, message_id,
                            "🐾 又有新链接~ 机器猫已经悄悄记下，放进候选备用区啦。今日正选已满，这些会作为优先候选，辛苦再等等~")
                    else:
                        reply_to_message(actual_chat_id, message_id,
                            "🐾 机器猫的小本本快记满啦！今日40条正选早已满员，后面这些都帮你存进候选池，有空位时优先考虑，感谢理解和支持呀~")

        save_state(state)
        print(f"✅ 处理完成 | 本次匹配: {matched} 条 | 当前日期: {state.get('date')} | 今日累计: {state['count']}")
    except Exception as e:
        print(f"运行异常: {e}")

if __name__ == "__main__":
    main()
