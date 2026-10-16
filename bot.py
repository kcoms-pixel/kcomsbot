# KCOMS V6.6 - TG ALERTS FIXED - 10 TRADES + RR 1:2
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
MAX_TRADES=10
RR=2.0
COOLDOWN_MINUTES=5

PAPER_BALANCE_FILE="/tmp/kcoms_balance.txt"
daily_trades=0
last_day=datetime.utcnow().day
cooldown_until=None
used_zones=[]
latest={"price":0,"ema":0,"state":"BOOTING","dir":"NONE","last":"Booting V6.6 TG fixed","wins":0,"losses":0,"bal":"100","daily":0}
start_time=time.time()

@app.route('/')
def home():
    up=int(time.time()-start_time)
    tg_status = "✅ TG SET" if TG_TOKEN and TG_CHAT else "❌ TG NOT SET - ADD ENV"
    cd = f"Cooldown {cooldown_until.strftime('%H:%M')}" if cooldown_until and datetime.utcnow() < cooldown_until else "Ready"
    return f"<h1>KCOMS V6.6 TG FIXED ✅</h1><p>{tg_status}</p><p>Uptime {up}s | Price {latest['price']:.2f} | EMA200 {latest['ema']:.2f} | {latest['state']} {latest['dir']} | RR 1:{RR} | {cd}</p><p>Bal ${latest['bal']} | W:{latest['wins']} L:{latest['losses']} D:{latest['daily']}/{MAX_TRADES}</p><p>Last: {latest['last']}</p>"

@app.route('/health')
def health():
    return "ok", 200

def send_tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT:
            print(f"TG SKIP - No token/chat: {msg}", flush=True)
            return
        url=f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r=requests.post(url, json={"chat_id":TG_CHAT,"text":msg}, timeout=15)
        print(f"TG SENT {r.status_code}: {msg[:50]}", flush=True)
    except Exception as e:
        print(f"TG FAIL {e}", flush=True)

BINANCE_KLINE_URLS = ["https://data-api.binance.vision/api/v3/klines","https://api.binance.com/api/v3/klines","https://api1.binance.com/api/v3/klines","https://api2.binance.com/api/v3/klines","https://api3.binance.com/api/v3/klines"]

def load_paper_balance():
    try:
        if os.path.exists(PAPER_BALANCE_FILE):
            with open(PAPER_BALANCE_FILE, "r") as f:
                return float(f.read().strip())
    except: pass
    return 100.0

def save_paper_balance(bal):
    try:
        with open(PAPER_BALANCE_FILE, "w") as f:
            f.write(str(bal))
    except: pass

def get_klines(interval="15m", limit=200):
    for base in BINANCE_KLINE_URLS:
        try:
            url=f"{base}?symbol={SYMBOL}&interval={interval}&limit={limit}"
            r=requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
            data=r.json()
            if isinstance(data, list) and len(data)>0:
                return [{"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"o":float(k[1])} for k in data]
        except: pass
    return None

def calc_ema(prices, period=200):
    if len(prices) < period: return prices[-1] if prices else 0
    k=2/(period+1); ema=sum(prices[:period])/period
    for p in prices[period:]: ema=p*k+ema*(1-k)
    return ema

def binance_sign(params): return hmac.new(API_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()

def calc_qty(entry, sl, bal):
    dist=abs(entry-sl)
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
    print(f"START V6.6 TG={bool(TG_TOKEN)} CHAT={bool(TG_CHAT)}", flush=True)
    send_tg(f"🚀 KCOMS V6.6 Online\nPrice scanning...\nBal ${load_paper_balance():.2f}\n10 trades/day RR 1:2")
    state="SCANNING"; zone=(0,0); direction="NONE"; wins=0; losses=0
    balance=load_paper_balance()
    in_pos=False; pos_side="NONE"; entry_price=0; sl_price=0; tp_price=0

    while True:
        try:
            today=datetime.utcnow().day
            if today!=last_day: daily_trades=0; last_day=today; used_zones=[]
            if cooldown_until and datetime.utcnow() < cooldown_until:
                remaining = int((cooldown_until - datetime.utcnow()).total_seconds())
                print(f"[{datetime.now().strftime('%H:%M:%S')}] COOLDOWN {remaining}s | Bal ${balance:.2f}", flush=True)
                time.sleep(10); continue
            if daily_trades>=MAX_TRADES:
                time.sleep(30); continue

            df15=get_klines("15m",200)
            df1=get_klines("1m",120)
            if not df15 or not df1:
                time.sleep(5); continue

            price=df15[-1]["c"]
            closes15=[k["c"] for k in df15]
            ema200=calc_ema(closes15,200)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Price {price:.2f} | EMA200 {ema200:.2f} | {state} | Bal ${balance:.2f} | W:{wins} L:{losses} D:{daily_trades}/{MAX_TRADES}", flush=True)
            latest.update({"price":price,"ema":ema200,"state":state,"dir":direction,"wins":wins,"losses":losses,"bal":f"{balance:.2f}","daily":daily_trades})

            if state=="SCANNING":
                rh=max(k["h"] for k in df15[-40:-10]); rl=min(k["l"] for k in df15[-40:-10])
                if price < rl and price < ema200:
                    pz=(min(k["h"] for k in df15[-15:]), max(k["h"] for k in df15[-15:]))
                    if not any(abs(pz[0]-z[0])<50 for z in used_zones):
                        direction="SHORT"; zone=pz
                        msg=f"📉 BEAR BOS SHORT setup @ {price:.2f}\nEMA {ema200:.2f}\nWaiting sweep"
                        print(msg, flush=True); send_tg(msg); state="WAIT_LIQ"
                elif price > rh and price > ema200:
                    pz=(min(k["l"] for k in df15[-15:]), max(k["l"] for k in df15[-15:]))
                    if not any(abs(pz[0]-z[0])<50 for z in used_zones):
                        direction="LONG"; zone=pz
                        msg=f"📈 BULL BOS LONG setup @ {price:.2f}\nEMA {ema200:.2f}\nWaiting sweep"
                        print(msg, flush=True); send_tg(msg); state="WAIT_LIQ"

            elif state=="WAIT_LIQ":
                if direction=="SHORT" and price>=zone[0]*0.999:
                    eq=max(k["h"] for k in df15[-12:-2])
                    if price>eq*1.0005:
                        msg=f"💧 Liquidity swept SHORT @ {price:.2f}\nWaiting CHOCH 1m"
                        print(msg, flush=True); send_tg(msg); state="WAIT_CHOCH"
                elif direction=="LONG" and price<=zone[1]*1.001:
                    eq=min(k["l"] for k in df15[-12:-2])
                    if price<eq*0.9995:
                        msg=f"💧 Liquidity swept LONG @ {price:.2f}\nWaiting CHOCH 1m"
                        print(msg, flush=True); send_tg(msg); state="WAIT_CHOCH"

            elif state=="WAIT_CHOCH":
                if direction=="SHORT":
                    m1_low=min(k["l"] for k in df1[-20:-5])
                    if df1[-1]["c"] < m1_low * 0.9985 and df1[-1]["c"] < df1[-1]["o"]:
                        sl_price=max(k["h"] for k in df1[-15:])+40; entry_price=price
                        tp_price=entry_price-(abs(entry_price-sl_price)*RR)
                        qty=calc_qty(entry_price, sl_price, balance)
                        msg=f"🔴 A+ SHORT ENTRY\nEntry {entry_price:.2f}\nSL {sl_price:.2f}\nTP {tp_price:.2f}\nRR 1:{RR} Bal ${balance:.2f}"
                        print(msg, flush=True); send_tg(msg)
                        if place_order("SELL", qty): in_pos=True; pos_side="SHORT"; state="IN_TRADE"; daily_trades+=1; used_zones.append(zone); latest["last"]=msg
                else:
                    m1_high=max(k["h"] for k in df1[-20:-5])
                    if df1[-1]["c"] > m1_high * 1.0015 and df1[-1]["c"] > df1[-1]["o"]:
                        sl_price=min(k["l"] for k in df1[-15:])-40; entry_price=price
                        tp_price=entry_price+(abs(entry_price-sl_price)*RR)
                        qty=calc_qty(entry_price, sl_price, balance)
                        msg=f"🟢 A+ LONG ENTRY\nEntry {entry_price:.2f}\nSL {sl_price:.2f}\nTP {tp_price:.2f}\nRR 1:{RR} Bal ${balance:.2f}"
                        print(msg, flush=True); send_tg(msg)
                        if place_order("BUY", qty): in_pos=True; pos_side="LONG"; state="IN_TRADE"; daily_trades+=1; used_zones.append(zone); latest["last"]=msg

            elif state=="IN_TRADE":
                risk_dist=abs(entry_price-sl_price)
                qty=calc_qty(entry_price, sl_price, balance)
                profit_usd=risk_dist*RR*qty
                loss_usd=risk_dist*qty
                if pos_side=="SHORT" and price <= tp_price:
                    place_order("BUY", qty); balance+=profit_usd; save_paper_balance(balance); wins+=1
                    msg=f"✅ TP SHORT @ {price:.2f}\n+${profit_usd:.2f} → Bal ${balance:.2f}\nW:{wins} L:{losses}"
                    print(msg, flush=True); send_tg(msg); state="SCANNING"; in_pos=False
                elif pos_side=="LONG" and price >= tp_price:
                    place_order("SELL", qty); balance+=profit_usd; save_paper_balance(balance); wins+=1
                    msg=f"✅ TP LONG @ {price:.2f}\n+${profit_usd:.2f} → Bal ${balance:.2f}\nW:{wins} L:{losses}"
                    print(msg, flush=True); send_tg(msg); state="SCANNING"; in_pos=False
                elif (pos_side=="SHORT" and price > sl_price) or (pos_side=="LONG" and price < sl_price):
                    place_order("BUY" if pos_side=="SHORT" else "SELL", qty); balance-=loss_usd; save_paper_balance(balance); losses+=1
                    cooldown_until=datetime.utcnow()+timedelta(minutes=COOLDOWN_MINUTES)
                    msg=f"❌ SL {pos_side} @ {price:.2f}\n-${loss_usd:.2f} → Bal ${balance:.2f}\nCooldown {COOLDOWN_MINUTES}m"
                    print(msg, flush=True); send_tg(msg); state="SCANNING"; in_pos=False
            time.sleep(3)
        except Exception as e:
            print(f"LOOP ERR {e}", flush=True); time.sleep(3)

if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, threaded=True)
