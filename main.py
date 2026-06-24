import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

# 正确写法：使用 Secrets 的名称
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
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
        print("❌ Token 或 Chat ID 未正确加载")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        print(f"发送消息状态: {r.status_code}")
    except Exception as e:
        print(f"发送失败: {e}")

def main():
    if not BOT_TOKEN or BOT_TOKEN == "None" or not CHAT_ID:
        print("❌ 严重错误：TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未在 Secrets 中正确设置！")
        print(f"当前 BOT_TOKEN: {BOT_TOKEN}")
        print(f"当前 CHAT_ID: {CHAT_ID}")
        return

    state = load_state()
    print(f"启动状态 → count={state.get('count',0)}, date={state.get('date','')}")
    
    today = get_beijing_time().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["count"] = 0
        state["date"] = today
        print("✅ 已重置今日计数")
    
    # 获取新消息
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": state.get("offset", 0) + 1, "timeout": 10}
    
    try:
        response = requests.get(url, params=params, timeout=15)
        print(f"API 状态码: {response.status_code}")
        
        data = response.json()
        if not data.get("ok"):
            print("Telegram API 错误:", data)
            return
        
        updates = data.get("result", [])
        print(f"获取到 {len(updates)} 条新消息")
        
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
            print(f"检测到消息，包含X链接: {has_link}")
            
            if has_link:
                state["count"] += 1
                current = state["count"]
                print(f"🔗 计数增加 → 当前 {current} 条")
                
                if current == MAX_LIMIT:
                    send_message("互推链接已到达40条，今日互推名单已截止，后面新链接将不被转推！")
                elif current > MAX_LIMIT:
                    send_message(f"当前互推链接（{current}）条已超出40条，今日互推仅转前40条，超出部分不予转推！")
        
        save_state(state)
        print(f"✅ 本次运行完成，最终计数: {state['count']}")
        
    except Exception as e:
        print(f"运行异常: {e}")

if __name__ == "__main__":
    main()
