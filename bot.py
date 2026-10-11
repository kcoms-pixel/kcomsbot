import os
import threading
import time
import requests
from flask import Flask
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home():
    return "KCOMS BOT V6 - BINANCE - LIVE - FIXED"

def send_telegram(msg):
    try:
        token = os.getenv("8834684274:AAE2vGC7TruiUV_YNNuaky5AMqOtoSMm4OU")
        chat = os.getenv("498798769")
        if not token or not chat:
            print("Telegram not set", flush=True)
            return
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=10)
    except Exception as e:
        print(f"TG error {e}", flush=True)

def get_binance_price():
    # 1. Try Binance first
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT&_={int(time.time()*1000)}"
        r = requests.get(url, timeout=10).json()
        if 'price' in r:
            return float(r['price'])
        print(f"Binance blocked, response: {r}", flush=True)
    except Exception as e:
        print(f"Binance fail: {e}", flush=True)

    # 2. Try Coinbase (never blocked on Render)
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10).json()
        price = float(r['data']['amount'])
        print(f"Using Coinbase fallback: {price}", flush=True)
        return price
    except Exception as e:
        print(f"Coinbase fail: {e}", flush=True)

    # 3. Try CoinGecko (last resort)
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10).json()
        price = float(r['bitcoin']['usd'])
        print(f"Using CoinGecko fallback: {price}", flush=True)
        return price
    except Exception as e:
        print(f"CoinGecko fail: {e}", flush=True)

    return None

def run_bot():
    print("KCOMS BOT V6 - BINANCE - LIVE - FIXED", flush=True)
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

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking BTCUSDT... Price: {price} | Wins: {wins} Losses: {losses} PnL: ${pnl:.2f}", flush=True)

        if not in_trade and last_price and price < last_price * 0.9995:
            in_trade = True
            buy_price = price
            print(f"*** BUY @ {price}", flush=True)

        elif in_trade:
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
