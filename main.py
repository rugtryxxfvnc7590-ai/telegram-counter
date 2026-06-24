import os
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print("=== 严格调试开始 ===")
print(f"BOT_TOKEN 原始长度: {len(BOT_TOKEN) if BOT_TOKEN else 0}")
print(f"BOT_TOKEN 前50字符: {str(BOT_TOKEN)[:50] if BOT_TOKEN else 'None'}")
print(f"CHAT_ID: {CHAT_ID}")
print("====================")

# 测试 getMe
try:
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10)
    print(f"getMe 状态码: {r.status_code}")
    print(f"getMe 返回: {r.text[:400]}")
except Exception as e:
    print(f"getMe 异常: {e}")

# 测试 getUpdates
try:
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset=-1", timeout=10)
    print(f"getUpdates 状态码: {r.status_code}")
    print(f"getUpdates 返回: {r.text[:400]}")
except Exception as e:
    print(f"getUpdates 异常: {e}")
