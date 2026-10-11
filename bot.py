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
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=10)
    except:
        pass

# === 1-LINE FIX FROM THAT MESSAGE ===
def get_kraken_price():
    # Use Coinbase - same price as Binance $83,046, never blocked
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10).json()
        return float(r['data']['amount'])
    except Exception as e:
        print(f"Coinbase fail {e}, trying CoinGecko", flush=True)
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10).json()
            return float(r['bitcoin']['usd'])
        except:
            return None

def run_bot():
    print("KCOMS BOT V6 - BINANCE PRICE - LIVE!", flush=True)
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

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking BTCUSDT... Price: {price} | Wins: {wins} Losses: {losses} PnL: ${pnl:.2f}", flush=True)

        if not in_trade:
            if last_price and price < last_price * 0.9995:
                in_trade = True
                buy_price = price
                print(f"*** BUY @ {price}", flush=True)
        else:
            if price > buy_price * 1.005:
                wins += 1
                pnl += price - buy_price
                in_trade = False
                print(f"*** SELL PROFIT @ {price}", flush=True)
            elif price < buy_price * 0.995:
                losses += 1
                pnl += price - buy_price
                in_trade = False
                print(f"*** SELL LOSS @ {price}", flush=True)

        last_price = price
        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
