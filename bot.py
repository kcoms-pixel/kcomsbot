# KCOMS V6 FINAL COMBINED - V4.3 LOGIC + V5.1 HEALTH + MULTI-BINANCE FIX
import threading, time, requests, os, hmac, hashlib
from flask import Flask
from datetime import datetime, timedelta
from urllib.parse import urlencode

app = Flask(__name__)
SYMBOL="BTCUSDT"
API_KEY=os.getenv("BINANCE_API_KEY","")
API_SECRET=os.getenv("BINANCE_API_SECRET","")
TRADE_ENABLED=os.getenv("TRADE_ENABLED","false").lower()=="true"
TRADE_SIZE_USDT=float(os.getenv("TRADE_SIZE_USDT","5"))
RISK_PERCENT=float(os.getenv("RISK_PERCENT","0.1"))
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")
MAX_TRADES=5
RR=3.0

daily_trades=0
last_day=datetime.utcnow().day
cooldown_until=None
used_zones=[]
latest={"price":0,"ema":0,"state":"BOOTING","dir":"NONE","last":"Booting V6","wins":0,"losses":0,"bal":"100","daily":0}
start_time=time.time()

@app.route('/')
def home():
    up=int(time.time()-start_time)
    cd = f"Cooldown {cooldown_until.strftime('%H:%M')}" if cooldown_until and datetime.utcnow() < cooldown_until else "Ready"
    return f"<h1>KCOMS V6 FINAL COMBINED ✅</h1><p>Uptime {up}s | Price {latest['price']:.2f} | EMA200 {latest['ema']:.2f} | {latest['state']} {latest['dir']} | {cd}</p><p>Bal ${latest['bal']} | W:{latest['wins']} L:{latest['losses']} D:{latest['daily']}/5</p><p>Last: {latest['last']}</p><script>setTimeout(()=>location.reload(),3000)</script>"

@app.route('/health')
def health():
    return "ok", 200

def send_tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT: return
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id":TG_CHAT,"text":msg}, timeout=10)
    except: pass

# === FIXED FETCH - TRIES 5 ENDPOINTS ===
BINANCE_KLINE_URLS = [
    "https://data-api.binance.vision/api/v3/klines",
    "https://api.binance.com/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://api2.binance.com/api/v3/klines",
    "https://api3.binance.com/api/v3/klines"
]

def get_klines(interval="15m", limit=200):
    for base in BINANCE_KLINE_URLS:
        try:
            url=f"{base}?symbol={SYMBOL}&interval={interval}&limit={limit}"
            r=requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
            data=r.json()
            if isinstance(data, list) and len(data)>0:
                return [{"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"o":float(k[1])} for k in data]
            else:
                print(f"Binance reply error from {base}: {str(data)[:100]}", flush=True)
        except Exception as e:
            print(f"Fail {base} -> {e}", flush=True)
    return None

def calc_ema(prices, period=200):
    if len(prices) < period: return prices[-1] if prices else 0
    k=2/(period+1); ema=sum(prices[:period])/period
    for p in prices[period:]: ema=p*k+ema*(1-k)
    return ema

def binance_sign(params): return hmac.new(API_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()

def get_balance():
    try:
        if not API_KEY: return 100.0
        ts=int(time.time()*1000); p={"timestamp":ts}; p["signature"]=binance_sign(p)
        r=requests.get("https://api.binance.com/api/v3/account", params=p, headers={"X-MBX-APIKEY":API_KEY}, timeout=10).json()
        for b in r.get("balances",[]):
            if b["asset"]=="USDT": return float(b["free"])
        return 100.0
    except: return 100.0

def calc_qty(entry, sl, bal):
    dist=abs(entry-sl);
    if dist<20: dist=50
    risk=bal*(RISK_PERCENT/100.0)
    return round(max(min(risk/dist, TRADE_SIZE_USDT/entry),0.00001),6)

def place_order(side, qty):
    try:
        if not TRADE_ENABLED or not API_KEY: return {"paper":True}
        ts=int(time.time()*1000); p={"symbol":SYMBOL,"side":side,"type":"MARKET","quantity":qty,"timestamp":ts}
        p["signature"]=binance_sign(p)
        r=requests.post("https://api.binance.com/api/v3/order", params=p, headers={"X-MBX-APIKEY":API_KEY}, timeout=10).json()
        return r
    except: return None

def run_bot():
    global daily_trades, last_day, cooldown_until, used_zones, latest
    print("START V6 FINAL COMBINED - V4.3 LOGIC + HEALTH + MULTI-BINANCE", flush=True)
    send_tg("🎯 V6 FINAL Started - Full A+ Logic")
    state="SCANNING"; zone=(0,0); direction="NONE"; wins=0; losses=0; balance=100.0
    in_pos=False; pos_side="NONE"; entry_price=0; sl_price=0; tp_price=0

    while True:
        try:
            today=datetime.utcnow().day
            if today!=last_day: daily_trades=0; last_day=today; used_zones=[]

            if cooldown_until and datetime.utcnow() < cooldown_until:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] COOLDOWN till {cooldown_until.strftime('%H:%M')}", flush=True)
                time.sleep(10); continue
            if daily_trades>=MAX_TRADES:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] DAILY LIMIT {daily_trades}/{MAX_TRADES}", flush=True)
                time.sleep(30); continue

            df15=get_klines("15m",200)
            df1=get_klines("1m",120)

            if not df15 or not df1:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] All Binance endpoints blocked, retry 5s", flush=True)
                latest["state"]="BINANCE BLOCKED - RETRY"
                time.sleep(5); continue

            price=df15[-1]["c"]
            closes15=[k["c"] for k in df15]
            ema200=calc_ema(closes15,200)
            balance=get_balance()

            now_str=datetime.now().strftime('%H:%M:%S')
            print(f"[{now_str}] Price {price:.2f} | EMA200 {ema200:.2f} | {state} | Bal ${balance:.2f} | W:{wins} L:{losses} D:{daily_trades}/{MAX_TRADES}", flush=True)
            latest.update({"price":price,"ema":ema200,"state":state,"dir":direction,"wins":wins,"losses":losses,"bal":f"{balance:.2f}","daily":daily_trades})

            # === V4.3 A+ LOGIC RESTORED ===
            if state=="SCANNING":
                rh=max(k["h"] for k in df15[-40:-10]); rl=min(k["l"] for k in df15[-40:-10])
                if price < rl and price < ema200:
                    pz=(min(k["h"] for k in df15[-15:]), max(k["h"] for k in df15[-15:]))
                    if not any(abs(pz[0]-z[0])<50 for z in used_zones):
                        direction="SHORT"; zone=pz
                        print(f"📉 BEAR BOS @ {price:.2f} Below EMA", flush=True); send_tg(f"📉 BEAR BOS @ {price:.2f}"); state="WAIT_LIQ"
                elif price > rh and price > ema200:
                    pz=(min(k["l"] for k in df15[-15:]), max(k["l"] for k in df15[-15:]))
                    if not any(abs(pz[0]-z[0])<50 for z in used_zones):
                        direction="LONG"; zone=pz
                        print(f"📈 BULL BOS @ {price:.2f} Above EMA", flush=True); send_tg(f"📈 BULL BOS @ {price:.2f}"); state="WAIT_LIQ"

            elif state=="WAIT_LIQ":
                if direction=="SHORT" and price>=zone[0]*0.999:
                    eq=max(k["h"] for k in df15[-12:-2])
                    if price>eq*1.0005:
                        print(f"💧 Swept SHORT @ {price:.2f} -> M1", flush=True); send_tg(f"💧 Swept SHORT"); state="WAIT_CHOCH"
                elif direction=="LONG" and price<=zone[1]*1.001:
                    eq=min(k["l"] for k in df15[-12:-2])
                    if price<eq*0.9995:
                        print(f"💧 Swept LONG @ {price:.2f} -> M1", flush=True); send_tg(f"💧 Swept LONG"); state="WAIT_CHOCH"

            elif state=="WAIT_CHOCH":
                if direction=="SHORT":
                    m1_low=min(k["l"] for k in df1[-20:-5])
                    if df1[-1]["c"] < m1_low * 0.9985 and df1[-1]["c"] < df1[-1]["o"]:
                        sl_price=max(k["h"] for k in df1[-15:])+40; entry_price=price
                        tp_price=entry_price-(abs(entry_price-sl_price)*RR)
                        qty=calc_qty(entry_price, sl_price, balance)
                        msg=f"🔴 A+ SHORT {entry_price:.2f} SL {sl_price:.2f} TP {tp_price:.2f} (1:{RR})"
                        print(msg, flush=True); send_tg(msg)
                        if place_order("SELL", qty): in_pos=True; pos_side="SHORT"; state="IN_TRADE"; daily_trades+=1; used_zones.append(zone); latest["last"]=msg
                else:
                    m1_high=max(k["h"] for k in df1[-20:-5])
                    if df1[-1]["c"] > m1_high * 1.0015 and df1[-1]["c"] > df1[-1]["o"]:
                        sl_price=min(k["l"] for k in df1[-15:])-40; entry_price=price
                        tp_price=entry_price+(abs(entry_price-sl_price)*RR)
                        qty=calc_qty(entry_price, sl_price, balance)
                        msg=f"🟢 A+ LONG {entry_price:.2f} SL {sl_price:.2f} TP {tp_price:.2f} (1:{RR})"
                        print(msg, flush=True); send_tg(msg)
                        if place_order("BUY", qty): in_pos=True; pos_side="LONG"; state="IN_TRADE"; daily_trades+=1; used_zones.append(zone); latest["last"]=msg

            elif state=="IN_TRADE":
                if pos_side=="SHORT" and price <= tp_price:
                    place_order("BUY", calc_qty(entry_price, sl_price, balance)); wins+=1
                    print(f"✅ TP SHORT @ {price:.2f} +{RR}R", flush=True); send_tg(f"✅ TP SHORT 1:{RR}"); state="SCANNING"; in_pos=False
                elif pos_side=="LONG" and price >= tp_price:
                    place_order("SELL", calc_qty(entry_price, sl_price, balance)); wins+=1
                    print(f"✅ TP LONG @ {price:.2f} +{RR}R", flush=True); send_tg(f"✅ TP LONG 1:{RR}"); state="SCANNING"; in_pos=False
                elif (pos_side=="SHORT" and price > sl_price) or (pos_side=="LONG" and price < sl_price):
                    place_order("BUY" if pos_side=="SHORT" else "SELL", calc_qty(entry_price, sl_price, balance)); losses+=1
                    cooldown_until=datetime.utcnow()+timedelta(hours=2)
                    print(f"❌ SL {pos_side} @ {price:.2f}", flush=True); send_tg(f"❌ SL {pos_side}"); state="SCANNING"; in_pos=False

            time.sleep(3)
        except Exception as e:
            print(f"LOOP ERR {e}", flush=True); time.sleep(3)

if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000, threaded=True)
