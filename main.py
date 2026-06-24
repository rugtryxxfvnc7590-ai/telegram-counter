import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

BOT_TOKEN = os.getenv("8755020155:AAHy6IkSGF-9Tp4h8nsf4Uw2mIe0D9frtPs")
CHAT_ID = os.getenv("-3599537338")
MAX_LIMIT = 40

STATE_FILE = "state.json"

# 加强版正则
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
        print("❌ Token 或 Chat ID 未设置，无法发送消息")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"发送消息状态: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"发送失败: {e}")

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ 错误：TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未在 Secrets 中设置！")
        return

    state = load_state()
    print(f"当前状态: count={state.get('count',0)}, date={state.get('date','')}, offset={state.get('offset',0)}")
    
    # 每日重置
    today = get_beijing_time().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["count"] = 0
        state["date"] = today
        print("✅ 已重置今日计数")
    
    # 获取消息
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "offset": state.get("offset", 0) + 1,
        "timeout": 10,
        "allowed_updates": ["message"]
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        print(f"getUpdates 状态码: {response.status_code}")
        
        data = response.json()
        if not data.get("ok"):
            print("API 错误:", data)
            if data.get('description') == 'Not Found':
                print("❌ BOT_TOKEN 可能错误，请重新检查 Token！")
            return
            
        updates = data.get("result", [])
        print(f"本次获取到 {len(updates)} 条新消息")
        
        for update in updates:
            state["offset"] = update["update_id"]
            message = update.get("message")
            if not message or "text" not in message:
                continue
                
            text = message.get("text", "")
            msg_chat_id = str(message["chat"]["id"])
            
            print(f"收到消息 chat_id={msg_chat_id}, 文本: {text[:100]}...")
            
            if msg_chat_id != str(CHAT_ID):
                continue
            
            has_link = bool(TWITTER_REGEX.search(text))
            print(f"是否包含X/Twitter链接: {has_link}")
            
            if has_link:
                state["count"] += 1
                current = state["count"]
                print(f"🔗 检测到链接！当前计数变为: {current}")
                
                if current == MAX_LIMIT:
                    send_message("互推链接已到达40条，今日互推名单已截止，后面新链接将不被转推！")
                elif current > MAX_LIMIT:
                    send_message(f"当前互推链接（{current}）条已超出40条，今日互推仅转前40条，超出部分不予转推！")
        
        save_state(state)
        print(f"✅ 处理完成，最终计数: {state['count']}")
        
    except Exception as e:
        print(f"运行出错: {e}")

if __name__ == "__main__":
    main()
