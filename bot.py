# KCOMS V4 A+ SNIPER - HIGH WIN RATE 1:3 RR - NO PANDAS
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
USE_TESTNET=os.getenv("USE_TESTNET","false").lower()=="true"
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")
BASE_URL="https://testnet.binance.vision" if USE_TESTNET else "https://data-api.binance.vision"
TRADE_URL="https://testnet.binance.vision" if USE_TESTNET else "https://api.binance.com"
MAX_TRADES_PER_DAY=5
RR_RATIO=3.0
daily_trades=0
last_day=datetime.utcnow().day
cooldown_until=None
used_zones=[]
latest_state={"price":0,"state":"A+ SCANNING","direction":"NONE","last_signal":"Waiting for A+ setup","wins":0,"losses":0,"balance":"?","daily_trades":0}
@app.route('/')
def home():
    cd = f"Cooldown till {cooldown_until.strftime('%H:%M')}" if cooldown_until and datetime.utcnow() < cooldown_until else "Ready"
    return f"<h1>KCOMS V4 A+ SNIPER LIVE</h1><p>A+ Mode | Risk {RISK_PERCENT}% Cap ${TRADE_SIZE_USDT} RR 1:{RR_RATIO} | TG:{bool(TG_TOKEN)} Trade:{TRADE_ENABLED}</p><p>Price: {latest_state['price']} | {latest_state['state']} {latest_state['direction']} | {cd}</p><p>Daily: {latest_state['daily_trades']}/{MAX_TRADES_PER_DAY} | Last: {latest_state['last_signal']}</p><p>W:{latest_state['wins']} L:{latest_state['losses']} Balance:{latest_state['balance']}</p>"
def send_tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT: return
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"Markdown"}, timeout=10)
    except: pass
def get_klines(interval="15m", limit=200):
    try:
        url=f"{BASE_URL}/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
        r=requests.get(url, timeout=15).json()
        if not isinstance(r, list): return None
        return [{"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"v":float(k[5])} for k in r]
    except: return None
def calc_ema(prices, period=200):
    if len(prices) < period: return None
    k = 2/(period+1)
    ema = sum(prices[:period])/period
    for p in prices[period:]:
        ema = p*k + ema*(1-k)
    return ema
def binance_sign(params): return hmac.new(API_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
def get_balance_usdt():
    try:
        if not API_KEY: return 100.0
        ts=int(time.time()*1000); params={"timestamp":ts}; params["signature"]=binance_sign(params)
        r=requests.get(f"{TRADE_URL}/api/v3/account", params=params, headers={"X-MBX-APIKEY":API_KEY}, timeout=10).json()
        for b in r.get("balances",[]):
            if b["asset"]=="USDT": return float(b["free"])
        return 0.0
    except: return 100.0
def calc_qty_risk(entry, sl, balance):
    risk_amount = balance * (RISK_PERCENT/100.0)
    sl_dist = abs(entry - sl)
    if sl_dist < 20: sl_dist=50
    qty_risk = risk_amount / sl_dist
    qty_cap = TRADE_SIZE_USDT / entry
    return round(max(min(qty_risk, qty_cap),0.00001),6)
def place_order(side, qty):
    try:
        if not TRADE_ENABLED: return {"paper":True}
        if not API_KEY: return {"paper":True}
        ts=int(time.time()*1000); params={"symbol":SYMBOL,"side":side,"type":"MARKET","quantity":qty,"timestamp":ts}
        params["signature"]=binance_sign(params)
        r=requests.post(f"{TRADE_URL}/api/v3/order", params=params, headers={"X-MBX-APIKEY":API_KEY}, timeout=10).json()
        return r
    except: return None
def run_bot():
    global latest_state, daily_trades, last_day, cooldown_until, used_zones
    print(f"START V4 A+ SNIPER", flush=True)
    send_tg(f"🎯 KCOMS V4 A+ SNIPER Started\nRisk {RISK_PERCENT}% | Cap ${TRADE_SIZE_USDT}\nRR 1:{RR_RATIO} | Max {MAX_TRADES_PER_DAY}/day\nEMA200 + Fresh Zone + Strong CHoCH")
    state="SCANNING"; zone=(0,0); direction="NONE"; wins=0; losses=0
    in_pos=False; pos_side="NONE"; entry_price=0; sl_price=0; tp_price=0
    while True:
        try:
            today=datetime.utcnow().day
            if today!= last_day: daily_trades=0; last_day=today; used_zones=[]; send_tg(f"📅 New Day Reset - 0/{MAX_TRADES_PER_DAY}")
            if cooldown_until and datetime.utcnow() < cooldown_until:
                latest_state.update({"state":f"COOLDOWN till {cooldown_until.strftime('%H:%M')}", "daily_trades":daily_trades})
                time.sleep(30); continue
            if daily_trades >= MAX_TRADES_PER_DAY:
                latest_state.update({"state":"DAILY LIMIT A+","daily_trades":daily_trades})
                time.sleep(60); continue
            df15=get_klines("15m",200); df1=get_klines("1m",120)
            if not df15 or not df1: time.sleep(10); continue
            price=df15[-1]["c"]
            closes15=[k["c"] for k in df15]
            ema200=calc_ema(closes15,200)
            latest_state["price"]=price
            if state=="SCANNING":
                if not ema200: time.sleep(10); continue
                rh=max(k["h"] for k in df15[-40:-10]); rl=min(k["l"] for k in df15[-40:-10])
                if price < rl and price < ema200:
                    potential_zone=(min(k["h"] for k in df15[-15:]), max(k["h"] for k in df15[-15:]))
                    if any(abs(potential_zone[0]-z[0])<50 for z in used_zones): time.sleep(15); continue
                    eq_high=max(k["h"] for k in df15[-35:-5])
                    real=False
                    for i in range(len(df15)-30, len(df15)-5):
                        if df15[i]["h"] > eq_high and df15[i]["c"] < eq_high: real=True; break
                    if real:
                        direction="SHORT"; zone=potential_zone
                        send_tg(f"📉 A+ BEAR BOS @ {price:.2f} Below EMA{ema200:.0f}"); state="WAIT_LIQ"
                elif price > rh and price > ema200:
                    potential_zone=(min(k["l"] for k in df15[-15:]), max(k["l"] for k in df15[-15:]))
                    if any(abs(potential_zone[0]-z[0])<50 for z in used_zones): time.sleep(15); continue
                    eq_low=min(k["l"] for k in df15[-35:-5])
                    real=False
                    for i in range(len(df15)-30, len(df15)-5):
                        if df15[i]["l"] < eq_low and df15[i]["c"] > eq_low: real=True; break
                    if real:
                        direction="LONG"; zone=potential_zone
                        send_tg(f"📈 A+ BULL BOS @ {price:.2f} Above EMA{ema200:.0f}"); state="WAIT_LIQ"
            elif state=="WAIT_LIQ":
                corrective=True
                for i in range(1,len(df15)-1):
                    if df15[i+1]["l"] > df15[i-1]["h"] or df15[i+1]["h"] < df15[i-1]["l"]:
                        if df15[i]["c"] >= zone[0] and df15[i]["c"] <= zone[1]: corrective=False
                if not corrective:
                    send_tg(f"⚠️ Weak zone - SKIPPING"); state="SCANNING"; continue
                if direction=="SHORT" and price>=zone[0]*0.999:
                    eq=max(k["h"] for k in df15[-12:-2])
                    if price>eq*1.0005: send_tg(f"💧 A+ Swept SHORT @ {price:.2f} -> M1"); state="WAIT_CHOCH"
                elif direction=="LONG" and price<=zone[1]*1.001:
                    eq=min(k["l"] for k in df15[-12:-2])
                    if price<eq*0.9995: send_tg(f"💧 A+ Swept LONG @ {price:.2f} -> M1"); state="WAIT_CHOCH"
            elif state=="WAIT_CHOCH":
                if direction=="SHORT":
                    m1_low=min(k["l"] for k in df1[-20:-5])
                    if df1[-1]["c"] < m1_low * 0.9985 and df1[-1]["c"] < df1[-1]["o"]:
                        if abs(df1[-1]["o"]-df1[-1]["c"]) > 3:
                            sl_price=max(k["h"] for k in df1[-15:])+40; entry_price=price
                            tp_price=entry_price - (abs(entry_price-sl_price)*RR_RATIO)
                            balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                            msg=f"🔴 A+ SHORT {entry_price:.2f} SL {sl_price:.2f} TP {tp_price:.2f} (1:{RR_RATIO}) Qty {qty}"
                            send_tg(msg)
                            if place_order("SELL", qty): in_pos=True; pos_side="SHORT"; state="IN_TRADE"; daily_trades+=1; used_zones.append(zone); latest_state["last_signal"]=msg
                else:
                    m1_high=max(k["h"] for k in df1[-20:-5])
                    if df1[-1]["c"] > m1_high * 1.0015 and df1[-1]["c"] > df1[-1]["o"]:
                        if abs(df1[-1]["o"]-df1[-1]["c"]) > 3:
                            sl_price=min(k["l"] for k in df1[-15:])-40; entry_price=price
                            tp_price=entry_price + (abs(entry_price-sl_price)*RR_RATIO)
                            balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                            msg=f"🟢 A+ LONG {entry_price:.2f} SL {sl_price:.2f} TP {tp_price:.2f} (1:{RR_RATIO}) Qty {qty}"
                            send_tg(msg)
                            if place_order("BUY", qty): in_pos=True; pos_side="LONG"; state="IN_TRADE"; daily_trades+=1; used_zones.append(zone); latest_state["last_signal"]=msg
            elif state=="IN_TRADE":
                if pos_side=="SHORT" and price <= tp_price:
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("BUY", qty); wins+=1; send_tg(f"✅ A+ TP 1:{RR_RATIO} SHORT @ {price:.2f} W:{wins} L:{losses}"); state="SCANNING"; in_pos=False
                elif pos_side=="LONG" and price >= tp_price:
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("SELL", qty); wins+=1; send_tg(f"✅ A+ TP 1:{RR_RATIO} LONG @ {price:.2f} W:{wins} L:{losses}"); state="SCANNING"; in_pos=False
                elif (pos_side=="SHORT" and price > sl_price) or (pos_side=="LONG" and price < sl_price):
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("BUY" if pos_side=="SHORT" else "SELL", qty); losses+=1
                    cooldown_until = datetime.utcnow() + timedelta(hours=2)
                    send_tg(f"❌ A+ SL {pos_side} @ {price:.2f} L:{losses} Cooldown 2h"); state="SCANNING"; in_pos=False
            latest_state.update({"state":state,"direction":direction,"wins":wins,"losses":losses,"daily_trades":daily_trades})
            time.sleep(15)
        except Exception as e:
            print(f"Err {e}", flush=True); time.sleep(10)
if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
