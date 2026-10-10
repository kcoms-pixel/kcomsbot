import os
import threading
import time
import requests
from flask import Flask
from datetime import datetime

app = Flask(__name__)

@app.route('/')
def home():
    return "KCOMS BOT V6 - KRAKEN - LIVE! - No Cache"

def send_telegram(msg):
    try:
        token = os.getenv("8834684274:AAE2vGC7TruiUV_YNNuaky5AMqOtoSMm4OU")
        chat = os.getenv("498798769")
        if not token or not chat:
            print("Telegram not set")
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=10)
        print(f"Telegram sent! {msg[:60]}", flush=True)
    except Exception as e:
        print(f"TG error: {e}", flush=True)

def get_kraken_price():
    try:
        ts = int(time.time() * 1000)
        # Cache-bust Kraken
        url = f"https://api.kraken.com/0/public/Ticker?pair=BTCUSD&_={ts}"
        r = requests.get(url, headers={'Cache-Control': 'no-cache'}, timeout=10).json()
        pair_key = list(r['result'].keys())[0]
        price = float(r['result'][pair_key]['c'][0])
        return price
    except Exception as e:
        print(f"Price error: {e}", flush=True)
        return None

def run_bot():
    print("KCOMS BOT V6 - KRAKEN - NO RATE LIMIT - WITH FLASK - CACHE FIX", flush=True)
    send_telegram("🚀 Kcoms_bot V6 KRAKEN LIVE! Cache fix applied")

    wins = 0
    losses = 0
    pnl = 0.0
    last_price = None
    in_trade = False
    buy_price = 0

    while True:
        price = get_kraken_price()
        if not price:
            time.sleep(5)
            continue

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking BTCUSD... Price: {price} | Wins: {wins} Losses: {losses} PnL: ${pnl:.2f}", flush=True)

        # --- MORE ACTIVE V6 LOGIC ---
        if not in_trade:
            if last_price and price < last_price * 0.9995: # 0.05% dip
                in_trade = True
                buy_price = price
                print(f"*** BUY signal! @ {price}", flush=True)
                send_telegram(f"🟢 BUY BTCUSD @ {price} (Paper)")
        else:
            if price > buy_price * 1.005: # +0.5% TP
                profit = price - buy_price
                pnl += profit
                wins += 1
                in_trade = False
                print(f"*** SELL PROFIT! @ {price} Profit: {profit:.2f}", flush=True)
                send_telegram(f"💰 SELL PROFIT @ {price} +${profit:.2f} | Wins: {wins} PnL: ${pnl:.2f}")
            elif price < buy_price * 0.995: # -0.5% SL
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
