import os
import requests

# --- 2. YOUR TELEGRAM CONFIG ---
BOT_TOKEN = "8834684274:AAE2vGC7TruiUV_YNNuaky5AMqOtoSMm4OU"
CHAT_ID = "498798769"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        data = resp.json()
        if data.get("ok"):
            print("Telegram sent!")
        else:
            print(f"Telegram FAILED: {data} - Check CHAT_ID / BOT_TOKEN")
            print(f"Using CHAT_ID={CHAT_ID}")
    except Exception as e:
        print(f"Telegram error: {e}")

print("KCOMS BOT V6 - KRAKEN - NO RATE LIMIT")
send_telegram("🚀 Kcoms_bot V6 LIVE! 24/7 on Render!")

wins = 0
losses = 0
total_pnl = 0.0
active_trade = None

# --- PASTE THE REST OF YOUR BOT CODE BELOW THIS LINE ---
# Your trading logic, loops, etc. keep everything as it was
