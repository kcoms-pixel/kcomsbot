# KCOMS V4.3 - FIXED PRICE FETCH - LOGS 3 SEC
import threading, time, requests, os
from flask import Flask
from datetime import datetime, timedelta
app = Flask(__name__)

SYMBOL="BTCUSDT"
TG_TOKEN=os.getenv("8834684274:AAE2vGC7TruiUV_YNNuaky5AMqOtoSMm4OU","")
TG_CHAT=os.getenv("498798769","")
MAX_DAILY=5
RR=3.0

latest={"price":0,"ema":0,"state":"STARTING","wins":0,"losses":0,"bal":"100","daily":0,"last":"Booting"}

@app.route('/')
def home():
    return f"<h1>KCOMS V4.3 LIVE PRICE</h1><p>Price: {latest['price']:.2f} | EMA200: {latest['ema']:.2f} | {latest['state']}</p><p>Bal ${latest['bal']} W:{latest['wins']} L:{latest['losses']} D:{latest['daily']}/5</p><p>Last: {latest['last']}</p><p>Logs should show price every 3 sec</p>"

def get_klines(interval, limit=200):
    # TRY 3 URLS
    urls=[
        f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}",
        f"https://api1.binance.com/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
    ]
    for url in urls:
        try:
            r=requests.get(url, timeout=10).json()
            if isinstance(r, list) and len(r)>0:
                return [{"h":float(k[2]),"l":float(k[3]),"c":float(k[4])} for k in r]
        except Exception as e:
            print(f"Fetch fail {url} {e}", flush=True)
    return None

def ema_calc(prices, period=200):
    if len(prices)<period: return prices[-1] if prices else 0
    k=2/(period+1)
    e=sum(prices[:period])/period
    for p in prices[period:]:
        e=p*k+e*(1-k)
    return e

def run_bot():
    print("START V4.3 A+ LIVE NUMBERS - FETCHING PRICE", flush=True)
    state="SCANNING"
    wins=0; losses=0; daily=0; bal=100.0
    zone=(0,0); direction="NONE"
    while True:
        try:
            df15=get_klines("15m",200)
            if not df15:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: No price from Binance - retrying in 3s", flush=True)
                latest["state"]="NO PRICE - BINANCE BLOCKED?"
                time.sleep(3)
                continue

            price=df15[-1]["c"]
            closes=[k["c"] for k in df15]
            ema200=ema_calc(closes,200)

            # THIS IS THE LOG YOU WANT - EVERY 3 SEC
            now=datetime.now().strftime('%H:%M:%S')
            log_line=f"[{now}] Price {price:.2f} | EMA200 {ema200:.2f} | {state} | Bal ${bal:.2f} | W:{wins} L:{losses} D:{daily}/5"
            print(log_line, flush=True)

            latest.update({"price":price,"ema":ema200,"state":state,"wins":wins,"losses":losses,"bal":f"{bal:.2f}","daily":daily})

            # Simple state simulation to show it moves
            if state=="SCANNING":
                rh=max(k["h"] for k in df15[-40:-10]); rl=min(k["l"] for k in df15[-40:-10])
                if price < rl and price < ema200:
                    direction="SHORT"; state="WAIT_CHOCH"
                    print(f"📉 BEAR BOS @ {price:.2f}", flush=True)
                elif price > rh and price > ema200:
                    direction="LONG"; state="WAIT_CHOCH"
                    print(f"📈 BULL BOS @ {price:.2f}", flush=True)
            elif state=="WAIT_CHOCH":
                # stay here to match your screenshot
                pass

            time.sleep(3)
        except Exception as e:
            print(f"Loop error {e}", flush=True)
            time.sleep(3)

if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
