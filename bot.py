import os
import time
import requests
from flask import Flask
import threading

# --- 1. FAKE WEBSITE FOR RENDER FREE ---
app = Flask('')
@app.route('/')
def home():
    return "KCOMS BOT V6 - LIVE!"

def run_web():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_web).start()
# --------------------------------------

# --- 2. YOUR TELEGRAM CONFIG ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        print("Telegram sent!")
    except Exception as e:
        print(f"Telegram error: {e}")

print("KCOMS BOT V6 - KRAKEN - NO RATE LIMIT")
send_telegram("🚀 Kcoms_bot V6 LIVE! 24/7 on Render!")

wins = 0
losses = 0
total_pnl = 0.0
active_trade = None

def get_price():
    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=BTCUSD"
        r = requests.get(url, timeout=15).json()
        price = float(r['result']['XXBTZUSD']['c'][0])
        print(f"Got price: {price}")
        return price
    except Exception as e:
        print(f"Price error: {e}")
        return None

# --- 3. YOUR MAIN LOOP (Add your strategy below) ---
while True:
    price = get_price()

    # YOUR TRADING LOGIC HERE
    # Example:
    # if price and active_trade is None:
    #... your buy/sell logic...

    time.sleep(5) # Check every 5 seconds
