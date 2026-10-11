import os
import threading
import time
import requests
from flask import Flask
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home():
    return "KCOMS BOT V6 - BINANCE PRICE - LIVE!"

def send_telegram(msg):
    try:
        token = os.getenv("TELEGRAM_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat:
            print("Telegram not set - add vars in Render", flush=True)
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=10)
        print(f"Telegram sent! {msg[:60]}", flush=True)
    except Exception as e:
        print(f"TG error: {e}", flush=True)

def get_price():
    # Binance is NOT blocked on Render + matches your screenshot $83,046
    try:
        ts = int(time.time()*1000)
        url = f"https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT&_={ts}"
        r = requests.get(url, headers={'Cache-Control':'no-cache'}, timeout=10)
        data = r.json()
        print(f"API raw: {data}", flush=True)
        return float(data['price'])
    except Exception as e:
        print(f"PRICE ERROR: {e}", flush=True)
        return None

def run_bot():
    print("KCOMS BOT V6 - BINANCE PRICE - STARTING", flush=True)
    send_telegram("🚀 Bot starting - Binance price feed")
    wins = 0
    losses = 0
    pnl = 0.0
    last_price = None
    in_trade = False
    buy_price = 0

    while True:
        try:
            price = get_price()
            if not price:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] No price, retrying...", flush=True)
                time.sleep(5)
                continue

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking BTCUSDT... Price: {price} | Wins: {wins} Losses: {losses} PnL: ${pnl:.2f}", flush=True)

            if not in_trade:
                if last_price and price < last_price * 0.9995:
                    in_trade = True
                    buy_price = price
                    print(f"*** BUY @ {price}", flush=True)
                    send_telegram(f"🟢 BUY @ {price}")
            else:
                if price > buy_price * 1.005:
                    wins += 1
                    pnl += price - buy_price
                    in_trade = False
                    print(f"*** SELL PROFIT @ {price}", flush=True)
                    send_telegram(f"💰 SELL PROFIT @ {price}")
                elif price < buy_price * 0.995:
                    losses += 1
                    pnl += price - buy_price
                    in_trade = False
                    print(f"*** SELL LOSS @ {price}", flush=True)
                    send_telegram(f"🔴 SELL LOSS @ {price}")

            last_price = price
        except Exception as e:
            print(f"BOT LOOP ERROR: {e}", flush=True)
        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
