import os
import requests
from flask import Flask
import threading
import time

# --- FLASK WEBSITE TO KEEP RENDER FREE ALIVE (from yesterday) ---
app = Flask('')
@app.route('/')
def home():
    return "KCOMS BOT V6 - LIVE!"

def run_web():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_web).start()
# ----------------------------------------------------------------

# --- 2. YOUR TELEGRAM CONFIG - FIXED WITH YOUR NEW ID ---
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
            print(f"Telegram FAILED: {data}")
    except Exception as e:
        print(f"Telegram error: {e}")

print("KCOMS BOT V6 - KRAKEN - NO RATE LIMIT - WITH FLASK")
send_telegram("🚀 Kcoms_bot V6 LIVE! 24/7 on Render! Flask OK!")

wins = 0
losses = 0
total_pnl = 0.0
active_trade = None

# --- YOUR KRAKEN TRADING LOGIC STARTS HERE ---
# (Keep your buy/sell logic below this line - I left it as is)

# Example loop:
# while True:
#     ... your kraken code ...
#     send_telegram(f"BUY signal! Wins: {wins}")
#     time.sleep(60)
