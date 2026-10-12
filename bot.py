# KCOMS V5.1 FINAL - UPTIMEROBOT FIX - NEVER DOWN
import threading, time, requests, os
from flask import Flask
from datetime import datetime
app = Flask(__name__)
SYMBOL="BTCUSDT"
latest={"price":0,"ema":0,"state":"BOOTING","wins":0,"losses":0,"bal":"100","daily":0,"last":"Booting..."}
start_time=time.time()

@app.route('/')
def home():
    up=int(time.time()-start_time)
    return f"<h1>KCOMS V5.1 LIVE ✅</h1><p>Price: {latest['price']:.2f} | EMA200: {latest['ema']:.2f} | {latest['state']}</p><p>Uptime: {up}s | D:{latest['daily']}/5</p><script>setTimeout(()=>location.reload(),3000)</script>"

@app.route('/health')
def health():
    return "ok", 200

def get_price():
    try:
        r=requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}", timeout=5).json()
        return float(r['price'])
    except: return None

def get_klines():
    try:
        r=requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval=15m&limit=200", timeout=10).json()
        if isinstance(r,list): return [float(k[4]) for k in r]
    except: pass
    return None

def run_forever():
    print("START V5.1 FINAL - /health ACTIVE", flush=True)
    state="SCANNING"; wins=0; losses=0; daily=0
    while True:
        try:
            price=get_price(); klines=get_klines()
            if price is None or klines is None:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] RETRY Binance", flush=True)
                time.sleep(3); continue
            k=2/(200+1); e=klines[0]
            for p in klines[1:]: e=p*k+e*(1-k)
            ema=e
            now=datetime.now().strftime('%H:%M:%S')
            print(f"[{now}] Price {price:.2f} | EMA200 {ema:.2f} | {state} | W:{wins} L:{losses} D:{daily}/5", flush=True)
            latest.update({"price":price,"ema":ema,"state":state})
            time.sleep(3)
        except Exception as e:
            print(f"ERROR {e} restart", flush=True); time.sleep(3)

if __name__=="__main__":
    threading.Thread(target=run_forever, daemon=True).start()
    app.run(host="0.0.0.0", port=10000, threaded=True)
