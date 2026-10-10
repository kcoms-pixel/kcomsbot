import os
import threading
import time
import requests
from flask import Flask
from datetime import datetime

# --- CONFIG ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8834684274:AAE2vGC7TruiUV_YNNuaky5AMqOtoSMm4OU")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "498798769")
KRAKEN_PAIR = "BTCUSD" # or XXBTZUSD

app = Flask(__name__)

@app.route('/')
def home():
    return "KCOMS BOT V6 - LIVE! - Kraken Running"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        print(f"Telegram sent! {msg[:50]}")
    except Exception as e:
        print(f"Telegram error: {e}")

def get_kraken_price():
    try:
        # No API key needed for ticker
        url = f"https://api.kraken.com/0/public/Ticker?pair={KRAKEN_PAIR}"
        r = requests.get(url, timeout=10).json()
        pair_key = list(r['result'].keys())[0]
        price = float(r['result'][pair_key]['c'][0])
        return price
    except Exception as e:
        print(f"Price error: {e}")
        return None

def run_bot():
    print("KCOMS BOT V6 - KRAKEN - NO RATE LIMIT - WITH FLASK", flush=True)
    send_telegram("🚀 Kcoms_bot V6 LIVE on Render! Logs active")

    wins = 0
    losses = 0
    pnl = 0.0
    last_price = None
    in_trade = False
    buy_price = 0

    while True:
        try:
            price = get_kraken_price()
            if price is None:
                time.sleep(5)
                continue

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking {KRAKEN_PAIR}... Price: {price} | Wins: {wins} Losses: {losses} PnL: ${pnl:.2f}", flush=True)

            # --- SIMPLE V6 LOGIC (replace with your real logic) ---
            # Example: buy if price drops 1% from last check, sell if up 1.5%
            if not in_trade:
                if last_price and price < last_price * 0.999: # dip
                    in_trade = True
                    buy_price = price
                    print(f"*** BUY signal! @ {price}", flush=True)
                    send_telegram(f"🟢 BUY {KRAKEN_PAIR} @ {price}")
            else:
                # in trade, check for profit
                if price > buy_price * 1.015: # +1.5% take profit
                    profit = price - buy_price
                    pnl += profit
                    wins += 1
                    in_trade = False
                    print(f"*** SELL PROFIT! @ {price} Profit: {profit:.2f}", flush=True)
                    send_telegram(f"💰 SELL PROFIT @ {price} +${profit:.2f} | Wins: {wins}")
                elif price < buy_price * 0.99: # -1% stop loss
                    loss = price - buy_price
                    pnl += loss
                    losses += 1
                    in_trade = False
                    print(f"*** SELL LOSS! @ {price} Loss: {loss:.2f}", flush=True)
                    send_telegram(f"🔴 SELL LOSS @ {price} {loss:.2f} | Losses: {losses}")

            last_price = price
            time.sleep(8) # No rate limit, but don't spam Kraken

        except Exception as e:
            print(f"Bot loop error: {e}", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    # Threading fix - Bot in background, Flask in main
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
