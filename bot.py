# KCOMS SMC ULTIMATE AUTO-TRADER V2 - NO PANDAS - FIXED
import threading, time, requests, os, hmac, hashlib
from flask import Flask
from datetime import datetime
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
latest_state={"price":0,"state":"INIT","direction":"NONE","last_signal":"None","wins":0,"losses":0,"balance":"?"}
@app.route('/')
def home():
    return f"<h1>KCOMS AUTO V2 LIVE</h1><p>Risk {RISK_PERCENT}% Cap ${TRADE_SIZE_USDT} TG:{bool(TG_TOKEN)} Trade:{TRADE_ENABLED}</p><p>Price: {latest_state['price']} | {latest_state['state']} {latest_state['direction']}</p><p>Last: {latest_state['last_signal']}</p><p>W:{latest_state['wins']} L:{latest_state['losses']}</p>"
def send_tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT: return
        url=f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"Markdown"}, timeout=10)
    except: pass
def get_klines(interval="15m", limit=150):
    try:
        url=f"{BASE_URL}/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
        r=requests.get(url, timeout=15).json()
        if not isinstance(r, list): return None
        return [{"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4])} for k in r]
    except: return None
def binance_sign(params):
    return hmac.new(API_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
def get_balance_usdt():
    try:
        if not API_KEY: return 100.0
        ts=int(time.time()*1000)
        params={"timestamp":ts}
        params["signature"]=binance_sign(params)
        headers={"X-MBX-APIKEY":API_KEY}
        r=requests.get(f"{TRADE_URL}/api/v3/account", params=params, headers=headers, timeout=10).json()
        for b in r.get("balances",[]):
            if b["asset"]=="USDT": return float(b["free"])
        return 0.0
    except: return 100.0
def calc_qty_risk(entry, sl, balance):
    risk_amount = balance * (RISK_PERCENT/100.0)
    sl_dist = abs(entry - sl)
    if sl_dist < 10: sl_dist=50
    qty_risk = risk_amount / sl_dist
    qty_cap = TRADE_SIZE_USDT / entry
    qty = min(qty_risk, qty_cap)
    qty = max(qty, 0.00001)
    return round(qty, 6)
def place_order(side, qty):
    try:
        if not TRADE_ENABLED:
            print(f"[PAPER] {side} {qty}", flush=True)
            return {"paper":True}
        if not API_KEY: return {"paper":True}
        ts=int(time.time()*1000)
        params={"symbol":SYMBOL,"side":side,"type":"MARKET","quantity":qty,"timestamp":ts}
        params["signature"]=binance_sign(params)
        headers={"X-MBX-APIKEY":API_KEY}
        r=requests.post(f"{TRADE_URL}/api/v3/order", params=params, headers=headers, timeout=10).json()
        print(f"ORDER {r}", flush=True)
        return r
    except Exception as e:
        print(f"Order err {e}", flush=True)
        return None
def is_real_bos(klines, direction):
    recent=klines[-35:-5]
    if direction=="BEAR":
        eq_high=max(k["h"] for k in recent)
        for i in range(len(klines)-30, len(klines)-5):
            if klines[i]["h"] > eq_high and klines[i]["c"] < eq_high: return True
        return False
    else:
        eq_low=min(k["l"] for k in recent)
        for i in range(len(klines)-30, len(klines)-5):
            if klines[i]["l"] < eq_low and klines[i]["c"] > eq_low: return True
        return False
def is_corrective(klines, z_low, z_high):
    for i in range(1, len(klines)-1):
        bull_fvg = klines[i+1]["l"] > klines[i-1]["h"]
        bear_fvg = klines[i+1]["h"] < klines[i-1]["l"]
        if bull_fvg or bear_fvg:
            if klines[i]["c"] >= z_low and klines[i]["c"] <= z_high:
                return False
    return True
def run_bot():
    global latest_state
    print(f"START FIXED NO-PANDAS", flush=True)
    send_tg(f"🚀 KCOMS Fixed Bot Started Risk {RISK_PERCENT}% Cap ${TRADE_SIZE_USDT}")
    state="SCANNING"; zone=(0,0); direction="NONE"; m15_target=0; wins=0; losses=0; in_pos=False; pos_side="NONE"; entry_price=0; sl_price=0
    balance=get_balance_usdt(); latest_state["balance"]=f"${balance:.2f}"
    while True:
        try:
            df15=get_klines("15m",150); df1=get_klines("1m",120)
            if not df15 or not df1: time.sleep(10); continue
            price=df15[-1]["c"]; latest_state["price"]=price
            if state=="SCANNING":
                rh=max(k["h"] for k in df15[-40:-10]); rl=min(k["l"] for k in df15[-40:-10])
                if price < rl and is_real_bos(df15,"BEAR"):
                    direction="SHORT"; zone=(min(k["h"] for k in df15[-15:]), max(k["h"] for k in df15[-15:])); m15_target=min(k["l"] for k in df15[-40:])
                    msg=f"📉 REAL BEAR BOS @ {price:.2f}"; send_tg(msg); state="WAIT_LIQ"
                elif price > rh and is_real_bos(df15,"BULL"):
                    direction="LONG"; zone=(min(k["l"] for k in df15[-15:]), max(k["l"] for k in df15[-15:])); m15_target=max(k["h"] for k in df15[-40:])
                    msg=f"📈 REAL BULL BOS @ {price:.2f}"; send_tg(msg); state="WAIT_LIQ"
            elif state=="WAIT_LIQ":
                if direction=="SHORT" and price>=zone[0]*0.999:
                    eq=max(k["h"] for k in df15[-12:-2])
                    if price>eq and is_corrective(df15,zone[0],zone[1]): send_tg(f"💧 Swept SHORT @ {price:.2f} -> M1"); state="WAIT_CHOCH"
                elif direction=="LONG" and price<=zone[1]*1.001:
                    eq=min(k["l"] for k in df15[-12:-2])
                    if price<eq and is_corrective(df15,zone[0],zone[1]): send_tg(f"💧 Swept LONG @ {price:.2f} -> M1"); state="WAIT_CHOCH"
            elif state=="WAIT_CHOCH":
                if direction=="SHORT":
                    m1_low=min(k["l"] for k in df1[-20:-5])
                    if df1[-1]["c"] < m1_low:
                        fresh=max(k["h"] for k in df1[-60:-20]) > max(k["h"] for k in df1[-5:])
                        if not fresh:
                            sl_price=max(k["h"] for k in df1[-15:])+30; entry_price=price; balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                            msg=f"🔴 ENTER SHORT {entry_price:.2f} SL {sl_price:.2f} TP {m15_target:.2f} Qty {qty}"; send_tg(msg)
                            if place_order("SELL", qty): in_pos=True; pos_side="SHORT"; state="IN_TRADE"; latest_state["last_signal"]=msg
                else:
                    m1_high=max(k["h"] for k in df1[-20:-5])
                    if df1[-1]["c"] > m1_high:
                        fresh=min(k["l"] for k in df1[-60:-20]) < min(k["l"] for k in df1[-5:])
                        if not fresh:
                            sl_price=min(k["l"] for k in df1[-15:])-30; entry_price=price; balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                            msg=f"🟢 ENTER LONG {entry_price:.2f} SL {sl_price:.2f} TP {m15_target:.2f} Qty {qty}"; send_tg(msg)
                            if place_order("BUY", qty): in_pos=True; pos_side="LONG"; state="IN_TRADE"; latest_state["last_signal"]=msg
            elif state=="IN_TRADE":
                if pos_side=="SHORT" and price <= m15_target:
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("BUY", qty); wins+=1; send_tg(f"✅ TP SHORT @ {price:.2f} W:{wins}"); state="SCANNING"; in_pos=False
                elif pos_side=="LONG" and price >= m15_target:
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("SELL", qty); wins+=1; send_tg(f"✅ TP LONG @ {price:.2f} W:{wins}"); state="SCANNING"; in_pos=False
                elif (pos_side=="SHORT" and price > zone[1]+80) or (pos_side=="LONG" and price < zone[0]-80):
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("BUY" if pos_side=="SHORT" else "SELL", qty); losses+=1; send_tg(f"❌ SL {pos_side} @ {price:.2f}"); state="SCANNING"; in_pos=False
            latest_state.update({"state":state,"direction":direction,"wins":wins,"losses":losses})
            time.sleep(12)
        except Exception as e:
            print(f"Err {e}", flush=True); time.sleep(10)
if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
