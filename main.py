import os
import json
import requests
import re
from datetime import datetime, timedelta, timezone

# 配置
BOT_TOKEN = os.getenv("8755020155:AAHy6IkSGF-9Tp4h8nsf4Uw2mIe0D9frtPs")
CHAT_ID = os.getenv("-3599537338")
MAX_LIMIT = 40

# 文件路径
STATE_FILE = "state.json"

# Twitter/X 链接正则（一条消息只要有一个就算1条）
TWITTER_REGEX = re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com|t\.co)/[^\s]+')

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"count": 0, "date": "", "offset": 0}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def get_beijing_time():
    """获取北京时间"""
    utc_now = datetime.now(timezone.utc)
    beijing = utc_now + timedelta(hours=8)
    return beijing

def should_reset(state):
    """判断是否需要重置计数（北京时间00:00）"""
    today = get_beijing_time().strftime("%Y-%m-%d")
    if state["date"] != today:
        return True
    return False

def send_message(text):
    """发送消息到群里"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass  # 静默失败

def main():
    state = load_state()
    
    # 检查是否需要每日重置
    if should_reset(state):
        state["count"] = 0
        state["date"] = get_beijing_time().strftime("%Y-%m-%d")
        print("✅ 已重置今日计数")
    
    # 获取新消息
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "offset": state["offset"] + 1,
        "timeout": 10,
        "allowed_updates": ["message"]
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        
        if not data.get("ok"):
            print("API错误:", data)
            return
            
        updates = data.get("result", [])
        
        for update in updates:
            message = update.get("message")
            if not message or "text" not in message:
                state["offset"] = update["update_id"]
                continue
                
            text = message["text"]
            chat_id = str(message["chat"]["id"])
            
            # 只处理目标群的消息
            if chat_id != str(CHAT_ID):
                state["offset"] = update["update_id"]
                continue
            
            # 检查是否包含 Twitter/X 链接
            if TWITTER_REGEX.search(text):
                state["count"] += 1
                current_count = state["count"]
                
                print(f"🔗 检测到链接，当前计数: {current_count}")
                
                # 达到或超过限制时发消息
                if current_count == MAX_LIMIT:
                    send_message("互推链接已到达40条，今日互推名单已截止，后面新链接将不被转推！")
                elif current_count > MAX_LIMIT:
                    send_message(f"当前互推链接（{current_count}）条已超出40条，今日互推仅转前40条，超出部分不予转推/不参与今日互推！")
            
            # 更新offset
            state["offset"] = update["update_id"]
        
        save_state(state)
        print(f"✅ 处理完成，当前计数: {state['count']}")
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    main()
