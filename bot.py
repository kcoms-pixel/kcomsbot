# KCOMS ULTIMATE BOT - BOTH OPTIONS TOGETHER
# 1. Binance.Vision price = $83,046 exact (fixes restricted location)
# 2. Active scalper = trades every few minutes
import threading, time, requests
from flask import Flask
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home():
    return "KCOMS ULTIMATE - BINANCE.VISION + ACTIVE TRADING - LIVE!"

def get_price():
    # OPTION 1: Binance.Vision - UNBLOCKED Binance, exact $83,046
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", timeout=10).json()
        if 'price' in r:
            return float(r['price']), "BINANCE"
    except Exception as e:
        print(f"Binance.Vision fail: {e}", flush=True)

    # Fallback - Coinbase same price as Binance
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=10).json()
        return float(r['data']['amount']), "COINBASE"
    except:
        pass

    # Last resort
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=BTCUSD", timeout=10).json()
        return float(r['result']['XXBTZUSD']['c'][0]), "KRAKEN"
    except:
        return None, "NONE"

def run_bot():
    print("ULTIMATE BOT - BOTH OPTIONS - STARTING", flush=True)
    wins=0; losses=0; pnl=0.0
    last_price=None
    in_trade=False
    buy_price=0

    while True:
        try:
            price, source = get_price()
            if not price:
                print("All sources failed, retry 5s", flush=True)
                time.sleep(5)
                continue

            action = "HOLD"

            # OPTION 2: ACTIVE TRADING LOGIC
            if not in_trade:
                # ANY small dip = BUY (instead of 0.05%)
                if last_price and price < last_price:
                    in_trade=True
                    buy_price=price
                    action=f"BUY @ {price}"
                    print(f"*** {action}", flush=True)
            else:
                change = (price - buy_price) / buy_price * 100
                # Take profit 0.10% (instead of 0.5%) - trades fast
                if change >= 0.10:
                    wins+=1
                    pnl+= (price - buy_price)
                    in_trade=False
                    action=f"WIN +{change:.3f}% SELL @ {price}"
                    print(f"*** {action}", flush=True)
                # Stop loss 0.08%
                elif change <= -0.08:
                    losses+=1
                    pnl+= (price - buy_price)
                    in_trade=False
                    action=f"LOSS {change:.3f}% SELL @ {price}"
                    print(f"*** {action}", flush=True)

            print(f"[{datetime.now().strftime('%H:%M:%S')}] {source} {action} | Price: {price} | W:{wins} L:{losses} PnL:${pnl:.2f} InTrade:{in_trade}", flush=True)
            last_price=price

        except Exception as e:
            print(f"Loop error: {e}", flush=True)

        time.sleep(8)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
