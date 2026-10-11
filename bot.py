# KCOMS SMC ULTIMATE AUTO-TRADER V2 - TELEGRAM + 0.1% RISK + $5 CAP
import threading, time, requests, pandas as pd, os, hmac, hashlib
from flask import Flask
from datetime import datetime
from urllib.parse import urlencode

app = Flask(__name__)

SYMBOL="BTCUSDT"
API_KEY=os.getenv("BINANCE_API_KEY","")
API_SECRET=os.getenv("BINANCE_API_SECRET","")
TRADE_ENABLED=os.getenv("TRADE_ENABLED","false").lower()=="true"
TRADE_SIZE_USDT=float(os.getenv("TRADE_SIZE_USDT","5")) # $5 cap you asked
RISK_PERCENT=float(os.getenv("RISK_PERCENT","0.1")) # 0.1% risk you asked
USE_TESTNET=os.getenv("USE_TESTNET","false").lower()=="true"
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")

BASE_URL="https://testnet.binance.vision" if USE_TESTNET else "https://data-api.binance.vision"
TRADE_URL="https://testnet.binance.vision" if USE_TESTNET else "https://api.binance.com"

latest_state={"price":0,"state":"INIT","direction":"NONE","last_signal":"None","wins":0,"losses":0,"balance":"?"}

@app.route('/')
def home():
    return f"<h1>KCOMS AUTO V2 LIVE</h1><p>Risk: {RISK_PERCENT}% | Cap: ${TRADE_SIZE_USDT} | TG: {bool(TG_TOKEN)} | Trade:{TRADE_ENABLED} Testnet:{USE_TESTNET}</p><p>Price: {latest_state['price']} | {latest_state['state']} {latest_state['direction']}</p><p>Last: {latest_state['last_signal']}</p><p>W:{latest_state['wins']} L:{latest_state['losses']} Bal:{latest_state['balance']}</p>"

def send_tg(msg):
    try:
        if not TG_TOKEN or not TG_CHAT:
            print(f"[TG DISABLED] {msg}", flush=True)
            return
        url=f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"Markdown"}, timeout=10)
        print(f"[TG SENT] {msg}", flush=True)
    except Exception as e:
        print(f"TG error {e}", flush=True)

def get_klines(interval="15m", limit=150):
    try:
        url=f"{BASE_URL}/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
        r=requests.get(url, timeout=15).json()
        if not isinstance(r, list): return None
        df=pd.DataFrame(r, columns=['t','o','h','l','c','v','ct','qv','n','tb','qb','i'])
        for c in ['o','h','l','c']: df[c]=df[c].astype(float)
        return df
    except: return None

def binance_sign(params):
    return hmac.new(API_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()

def get_balance_usdt():
    try:
        if not API_KEY: return 100.0 # paper default for risk calc
        ts=int(time.time()*1000)
        params={"timestamp":ts}
        params["signature"]=binance_sign(params)
        headers={"X-MBX-APIKEY":API_KEY}
        r=requests.get(f"{TRADE_URL}/api/v3/account", params=params, headers=headers, timeout=10).json()
        for b in r.get("balances",[]):
            if b["asset"]=="USDT": return float(b["free"])
        return 0.0
    except:
        return 100.0

def calc_qty_risk(entry, sl, balance):
    # Risk 0.1% = balance * 0.001 = max loss
    risk_amount = balance * (RISK_PERCENT/100.0) # 0.1% = 0.001
    sl_dist = abs(entry - sl)
    if sl_dist < 10: sl_dist=50 # avoid div 0
    qty_risk = risk_amount / sl_dist # qty that loses exactly 0.1%
    qty_cap = TRADE_SIZE_USDT / entry # $5 cap
    qty = min(qty_risk, qty_cap)
    qty = max(qty, 0.00001) # min BTC
    qty = round(qty, 6)
    print(f"Risk Calc: Bal ${balance:.2f} Risk ${risk_amount:.4f} SLdist {sl_dist:.2f} qty_risk {qty_risk:.6f} qty_cap {qty_cap:.6f} -> final {qty}", flush=True)
    return qty

def place_order(side, qty):
    try:
        if not TRADE_ENABLED:
            print(f"[PAPER] {side} {qty} {SYMBOL}", flush=True)
            return {"paper":True}
        if not API_KEY:
            print("No API keys paper", flush=True)
            return {"paper":True}
        ts=int(time.time()*1000)
        params={"symbol":SYMBOL,"side":side,"type":"MARKET","quantity":qty,"timestamp":ts}
        params["signature"]=binance_sign(params)
        headers={"X-MBX-APIKEY":API_KEY}
        r=requests.post(f"{TRADE_URL}/api/v3/order", params=params, headers=headers, timeout=10).json()
        print(f"ORDER RESULT {r}", flush=True)
        return r
    except Exception as e:
        print(f"Order err {e}", flush=True)
        return None

def is_real_bos(df, direction):
    recent=df.iloc[-35:-5]
    if direction=="BEAR":
        eq_high=recent['h'].max()
        return any(df['h'].iloc[i] > eq_high and df['c'].iloc[i] < eq_high for i in range(len(df)-30, len(df)-5))
    else:
        eq_low=recent['l'].min()
        return any(df['l'].iloc[i] < eq_low and df['c'].iloc[i] > eq_low for i in range(len(df)-30, len(df)-5))

def is_corrective(df, z_low, z_high):
    fvgs=[]
    for i in range(1,len(df)-1):
        if df['l'].iloc[i+1] > df['h'].iloc[i-1]: fvgs.append((df['h'].iloc[i-1], df['l'].iloc[i+1]))
        if df['h'].iloc[i+1] < df['l'].iloc[i-1]: fvgs.append((df['h'].iloc[i+1], df['l'].iloc[i-1]))
    bad=[f for f in fvgs if f[0]>=z_low and f[1]<=z_high]
    return len(bad)==0

def run_bot():
    global latest_state
    send_tg(f"🚀 *KCOMS Bot Started*\nMode: {'TESTNET' if USE_TESTNET else 'LIVE'}\nRisk: {RISK_PERCENT}% per trade\nCap: ${TRADE_SIZE_USDT}\nSymbol: {SYMBOL}")
    state="SCANNING"; zone=(0,0); direction="NONE"; m15_target=0; wins=0; losses=0; in_pos=False; pos_side="NONE"; entry_price=0; sl_price=0
    balance=get_balance_usdt()
    latest_state["balance"]=f"${balance:.2f}"

    while True:
        try:
            df15=get_klines("15m",150); df1=get_klines("1m",120)
            if df15 is None or df1 is None: time.sleep(10); continue
            price=df15['c'].iloc[-1]; latest_state["price"]=price

            if state=="SCANNING":
                rh=df15['h'].iloc[-40:-10].max(); rl=df15['l'].iloc[-40:-10].min()
                if price < rl and is_real_bos(df15,"BEAR"):
                    direction="SHORT"; zone=(df15['h'].iloc[-15:].min(), df15['h'].iloc[-15:].max()); m15_target=df15['l'].iloc[-40:].min()
                    msg=f"📉 *REAL BEAR BOS* @ {price:.2f}\nZone {zone[0]:.1f}-{zone[1]:.1f}\nWaiting for sweep"
                    print(msg, flush=True); send_tg(msg)
                    state="WAIT_LIQ"
                elif price > rh and is_real_bos(df15,"BULL"):
                    direction="LONG"; zone=(df15['l'].iloc[-15:].min(), df15['l'].iloc[-15:].max()); m15_target=df15['h'].iloc[-40:].max()
                    msg=f"📈 *REAL BULL BOS* @ {price:.2f}\nZone {zone[0]:.1f}-{zone[1]:.1f}\nWaiting for sweep"
                    print(msg, flush=True); send_tg(msg)
                    state="WAIT_LIQ"

            elif state=="WAIT_LIQ":
                if direction=="SHORT" and price>=zone[0]*0.999:
                    eq=df15['h'].iloc[-12:-2].max()
                    if price>eq and is_corrective(df15,zone[0],zone[1]):
                        send_tg(f"💧 *Liquidity Swept SHORT* @ {price:.2f}\nDropping to M1 for CHOCH")
                        state="WAIT_CHOCH"
                elif direction=="LONG" and price<=zone[1]*1.001:
                    eq=df15['l'].iloc[-12:-2].min()
                    if price<eq and is_corrective(df15,zone[0],zone[1]):
                        send_tg(f"💧 *Liquidity Swept LONG* @ {price:.2f}\nDropping to M1 for CHOCH")
                        state="WAIT_CHOCH"

            elif state=="WAIT_CHOCH":
                if direction=="SHORT":
                    m1_low=df1['l'].iloc[-20:-5].min()
                    if df1['c'].iloc[-1] < m1_low:
                        fresh=df1['h'].iloc[-60:-20].max() > df1['h'].iloc[-5:].max()
                        if not fresh:
                            sl_price=df1['h'].iloc[-15:].max()+30; entry_price=price
                            balance=get_balance_usdt()
                            qty=calc_qty_risk(entry_price, sl_price, balance)
                            rr=(entry_price-m15_target)/(sl_price-entry_price) if sl_price>entry_price else 0
                            msg=f"🔴 *ENTER SHORT*\nPrice {entry_price:.2f}\nSL {sl_price:.2f} (Risk {RISK_PERCENT}%)\nTP M15 Low {m15_target:.2f}\nRR {rr:.2f}\nQty {qty} (~${qty*entry_price:.2f})\nCap ${TRADE_SIZE_USDT}"
                            print(msg, flush=True); send_tg(msg)
                            res=place_order("SELL", qty)
                            if res: in_pos=True; pos_side="SHORT"; state="IN_TRADE"; latest_state["last_signal"]=msg
                else:
                    m1_high=df1['h'].iloc[-20:-5].max()
                    if df1['c'].iloc[-1] > m1_high:
                        fresh=df1['l'].iloc[-60:-20].min() < df1['l'].iloc[-5:].min()
                        if not fresh:
                            sl_price=df1['l'].iloc[-15:].min()-30; entry_price=price
                            balance=get_balance_usdt()
                            qty=calc_qty_risk(entry_price, sl_price, balance)
                            rr=(m15_target-entry_price)/(entry_price-sl_price) if entry_price>sl_price else 0
                            msg=f"🟢 *ENTER LONG*\nPrice {entry_price:.2f}\nSL {sl_price:.2f} (Risk {RISK_PERCENT}%)\nTP M15 High {m15_target:.2f}\nRR {rr:.2f}\nQty {qty} (~${qty*entry_price:.2f})\nCap ${TRADE_SIZE_USDT}"
                            print(msg, flush=True); send_tg(msg)
                            res=place_order("BUY", qty)
                            if res: in_pos=True; pos_side="LONG"; state="IN_TRADE"; latest_state["last_signal"]=msg

            elif state=="IN_TRADE":
                if pos_side=="SHORT" and price <= m15_target:
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("BUY", qty); wins+=1
                    send_tg(f"✅ *TP HIT SHORT WIN* @ {price:.2f}\nProfit ~${entry_price-price:.2f}\nW:{wins} L:{losses}\nBal ${balance:.2f}")
                    state="SCANNING"; in_pos=False
                elif pos_side=="LONG" and price >= m15_target:
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("SELL", qty); wins+=1
                    send_tg(f"✅ *TP HIT LONG WIN* @ {price:.2f}\nProfit ~${price-entry_price:.2f}\nW:{wins} L:{losses}\nBal ${balance:.2f}")
                    state="SCANNING"; in_pos=False
                elif (pos_side=="SHORT" and price > zone[1]+80) or (pos_side=="LONG" and price < zone[0]-80):
                    balance=get_balance_usdt(); qty=calc_qty_risk(entry_price, sl_price, balance)
                    place_order("BUY" if pos_side=="SHORT" else "SELL", qty); losses+=1
                    send_tg(f"❌ *SL HIT {pos_side}* @ {price:.2f}\nLoss ~${abs(entry_price-price):.2f} = {RISK_PERCENT}% risk\nW:{wins} L:{losses}")
                    state="SCANNING"; in_pos=False
                latest_state.update({"wins":wins,"losses":losses,"balance":f"${get_balance_usdt():.2f}"})

            latest_state.update({"state":state,"direction":direction})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {state} {direction} {price:.1f} W:{wins} L:{losses}", flush=True)
            time.sleep(12)
        except Exception as e:
            print(f"Err {e}", flush=True); time.sleep(10)

if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
