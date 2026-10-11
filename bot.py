import os, threading, time, requests
from flask import Flask
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home():
    return "KCOMS BOT V7 - FIXED - BINANCE PRICE WORKS!"

def get_price():
    # 1. Try UNBLOCKED Binance endpoint (works on Render!)
    try:
        url = "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"
        r = requests.get(url, timeout=10).json()
        if 'price' in r:
            return float(r['price']), "Binance.Vision"
    except Exception as e:
        print(f"Binance.vision fail: {e}", flush=True)

    # 2. Try Coinbase - same price, never blocked
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10).json()
        return float(r['data']['amount']), "Coinbase"
    except:
        pass

    # 3. Last resort CoinGecko
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=10).json()
        return float(r['bitcoin']['usd']), "CoinGecko"
    except:
        return None, "None"

def run_bot():
    print("KCOMS BOT V7 - FIXED - STARTING", flush=True)
    wins=0; losses=0; pnl=0.0
    last_price=None; in_trade=False; buy_price=0
    
    while True:
        try:
            price, source = get_price()
            if not price:
                print("All price sources failed, retry in 5s", flush=True)
                time.sleep(5)
                continue

            print(f"[{datetime.now().strftime('%H:%M:%S')}] {source} Price: {price} | Wins:{wins} Losses:{losses} PnL:${pnl:.2f}", flush=True)

            if not in_trade:
                if last_price and price < last_price * 0.9995:
                    in_trade=True
                    buy_price=price
                    print(f"*** BUY @ {price}", flush=True)
            else:
                if price > buy_price * 1.005:
                    wins+=1; pnl+=price-buy_price; in_trade=False
                    print(f"*** SELL WIN @ {price} +{price-buy_price:.2f}", flush=True)
                elif price < buy_price * 0.995:
                    losses+=1; pnl+=price-buy_price; in_trade=False
                    print(f"*** SELL LOSS @ {price}", flush=True)

            last_price=price
        except Exception as e:
            print(f"Loop error: {e}", flush=True)
        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
