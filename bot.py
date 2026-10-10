import os
import threading
import time
import requests
from flask import Flask
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home():
    return "KCOMS BOT V6 - BINANCE - LIVE! - No Cache"

BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")
PAIR = os.getenv("BINANCE_PAIR", "BTCUSDT")

def send_telegram(msg):
    try:
        token = os.getenv("TELEGRAM_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat:
            print("Telegram not configured")
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=10)
        print(f"Telegram sent! {msg[:60]}", flush=True)
    except Exception as e:
        print(f"TG error {e}", flush=True)

def get_binance_price():
    try:
        # Cache-bust with timestamp + no-cache header
        ts = int(time.time() * 1000)
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={PAIR}&_={ts}"
        r = requests.get(url, headers={'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}, timeout=10).json()
        return float(r['price'])
    except Exception as e:
        print(f"Price error: {e} | raw: {r if 'r' in locals() else 'no response'}", flush=True)
        return None

def run_bot():
    print("KCOMS BOT V6 - BINANCE - LIVE - WITH CACHE FIX", flush=True)
    send_telegram(f"🚀 Kcoms_bot V6 BINANCE LIVE! Pair: {PAIR} | Logs fixed")

    wins = 0
    losses = 0
    pnl = 0.0
    last_price = None
    in_trade = False
    buy_price = 0

    while True:
        price = get_binance_price()
        if not price:
            time.sleep(5)
            continue

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking {PAIR}... Price: {price} | Wins: {wins} Losses: {losses} PnL: ${pnl:.2f}", flush=True)

        # --- FIXED V6 STRATEGY - More Active ---
        if not in_trade:
            if last_price and price < last_price * 0.9995: # 0.05% dip = triggers 2x faster
                in_trade = True
                buy_price = price
                print(f"*** BUY signal! @ {price}", flush=True)
                send_telegram(f"🟢 BUY {PAIR} @ {price} (Paper)")
        else:
            if price > buy_price * 1.005: # Take profit 0.5% (faster than 1.5%)
                profit = price - buy_price
                pnl += profit
                wins += 1
                in_trade = False
                print(f"*** SELL PROFIT! @ {price} Profit: {profit:.2f}", flush=True)
                send_telegram(f"💰 SELL PROFIT @ {price} +${profit:.2f} | Wins: {wins} PnL: ${pnl:.2f}")
            elif price < buy_price * 0.995: # Stop loss 0.5%
                loss = price - buy_price
                pnl += loss
                losses += 1
                in_trade = False
                print(f"*** SELL LOSS! @ {price} Loss: {loss:.2f}", flush=True)
                send_telegram(f"🔴 SELL LOSS @ {price} {loss:.2f} | Losses: {losses}")

        last_price = price
        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
