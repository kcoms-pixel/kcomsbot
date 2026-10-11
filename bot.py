# KCOMS SMC MASTER BOT - ALL 18 VIDEOS COMBINED
# Concepts: BOS, CHOCH, REAL vs FAKE, Breaker, Order Block, FVG, Liquidity Sweep, Valid Liquidity, IRL, Dow Theory
import threading, time, requests, pandas as pd
from flask import Flask
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home():
    return "KCOMS SMC MASTER - 18 STRATEGIES LIVE - M15 BOS + M1 CHOCH"

SYMBOL = "BTCUSDT"

def get_klines(interval="15m", limit=100):
    try:
        url = f"https://data-api.binance.vision/api/v3/klines?symbol={SYMBOL}&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=15).json()
        if not isinstance(r, list): return None
        df = pd.DataFrame(r, columns=['t','o','h','l','c','v','ct','qv','n','tb','qb','i'])
        for col in ['o','h','l','c']: df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"Kline error {interval}: {e}", flush=True)
        return None

# === SMC LOGIC FROM VIDEOS ===

def get_swings(df, lookback=5):
    highs, lows = [], []
    for i in range(lookback, len(df)-lookback):
        if df['h'].iloc[i] == df['h'].iloc[i-lookback:i+lookback+1].max():
            highs.append((i, df['h'].iloc[i]))
        if df['l'].iloc[i] == df['l'].iloc[i-lookback:i+lookback+1].min():
            lows.append((i, df['l'].iloc[i]))
    return highs, lows

def is_liquidity_sweep(df, level, direction="high"):
    # Did price sweep liquidity? Wick above/below
    last = df.iloc[-1]
    if direction=="high" and last['h'] > level and last['c'] < level:
        return True
    if direction=="low" and last['l'] < level and last['c'] > level:
        return True
    return False

def is_valid_liquidity(df, swing_idx, direction="low"):
    # Video 11: Valid liquidity must break the high it came from
    # In uptrend, low is valid only if it broke previous high
    if direction=="low":
        swing_low = df['l'].iloc[swing_idx]
        # Find high before this low
        prior_high = df['h'].iloc[:swing_idx].max()
        # The move to low must have broken a high? Simplified check
        return swing_low < prior_high * 0.995
    return True

def find_fvg(df):
    # Fair Value Gap: 3 candle pattern with gap
    fvgs = []
    for i in range(1, len(df)-1):
        if df['l'].iloc[i+1] > df['h'].iloc[i-1]: # Bullish FVG
            fvgs.append((df['h'].iloc[i-1], df['l'].iloc[i+1], "bull", i))
        if df['h'].iloc[i+1] < df['l'].iloc[i-1]: # Bearish FVG
            fvgs.append((df['l'].iloc[i-1], df['h'].iloc[i+1], "bear", i))
    return fvgs

def is_corrective_move(df, zone_low, zone_high):
    # Video Scenario 1 vs 2: If move back to IRL has FVGs = impulsive = bad
    # If clean no FVGs = corrective = good
    fvgs = find_fvg(df.tail(20))
    overlapping = [f for f in fvgs if not (f[1] < zone_low or f[0] > zone_high)]
    if len(overlapping) > 0:
        return False, "IMPULSIVE - Has FVGs - Skip (Scenario 1)"
    return True, "CORRECTIVE - Clean - Good (Scenario 2)"

def find_breaker_blocks(df):
    # Bearish breaker: last bullish OB before bearish BOS
    breakers = []
    for i in range(10, len(df)-2):
        if df['c'].iloc[i] < df['o'].iloc[i] and df['c'].iloc[i+1] > df['o'].iloc[i+1]: # shift
            # Breaker low/high
            breaker_low = df['l'].iloc[i]
            breaker_high = df['h'].iloc[i]
            # Check if fresh (not mitigated 50%)
            mitigated = any(df['l'].iloc[j] < (breaker_low+breaker_high)/2 for j in range(i+1, len(df)))
            if not mitigated:
                breakers.append((breaker_low, breaker_high, i, "fresh"))
    return breakers

def is_real_bos(df, bos_level, direction="bearish"):
    # REAL BOS must have swept liquidity before breaking
    # Check 20 candles before BOS for equal highs/lows sweep
    recent = df.iloc[-30:-5]
    if direction=="bearish":
        eq_high = recent['h'].max()
        # Was there a sweep of that high before breaking low?
        swept = any(df['h'].iloc[i] > eq_high and df['c'].iloc[i] < eq_high for i in range(len(df)-25, len(df)-5))
        return swept, eq_high
    return True, None

def find_long_wick_liquidity_grab(df):
    # Video: long lower wick after BOS = liquidity grab + bullish
    last = df.iloc[-1]
    body = abs(last['c'] - last['o'])
    lower_wick = min(last['o'], last['c']) - last['l']
    if lower_wick > body * 1.5:
        return True, "Long Wick - Liquidity Grab"
    return False, ""

# === MAIN BOT LOOP ===
def run_bot():
    print("KCOMS MASTER BOT - ALL 18 VIDEOS - STARTING", flush=True)
    state = "SCANNING_M15"
    supply_zone = (0,0)
    m15_low_target = 0

    while True:
        try:
            df_m15 = get_klines("15m", 120)
            df_m1 = get_klines("1m", 100)
            if df_m15 is None or df_m1 is None:
                time.sleep(10); continue

            price = df_m15['c'].iloc[-1]
            highs, lows = get_swings(df_m15)

            if state == "SCANNING_M15":
                # 1. M15 FIND A BOS
                recent_low = df_m15['l'].iloc[-30:-10].min()
                if price < recent_low:
                    real, sweep_level = is_real_bos(df_m15, recent_low, "bearish")
                    if not real:
                        print(f"[M15] FAKE BOS @ {price} - No liquidity sweep at {sweep_level} - SKIP (Video 1)", flush=True)
                    else:
                        print(f"[M15] REAL BOS CONFIRMED @ {price} broke {recent_low} after sweep {sweep_level}", flush=True)
                        # 2. HIGHLIGHT THAT AREA
                        supply_high = df_m15['h'].iloc[-15:].max()
                        supply_low = df_m15['h'].iloc[-15:].min() # last bearish OB
                        supply_zone = (supply_low, supply_high)
                        m15_low_target = df_m15['l'].iloc[-30:].min()
                        print(f"-> Highlight area Supply {supply_zone[0]:.2f} - {supply_zone[1]:.2f} | Target M15 low {m15_low_target:.2f}", flush=True)
                        # Check Breaker fresh?
                        breakers = find_breaker_blocks(df_m15)
                        fresh = [b for b in breakers if b[3]=="fresh"]
                        print(f"-> Breaker check: {len(fresh)} fresh breakers found", flush=True)
                        state = "WAITING_LIQUIDITY"

            elif state == "WAITING_LIQUIDITY":
                # 3. WAIT FOR LIQUIDITY TO FORM + 4. SWEEP
                if price >= supply_zone[0]:
                    # Liquidity formed? Equal highs
                    eq_high = df_m15['h'].iloc[-10:-2].max()
                    swept = is_liquidity_sweep(df_m15, eq_high, "high")
                    if swept:
                        print(f"*** LIQUIDITY SWEPT @ {price} above {eq_high} - Now drop to M1 for CHOCH", flush=True)
                        # Check IRL corrective vs impulsive
                        is_corr, reason = is_corrective_move(df_m15, supply_zone[0], supply_zone[1])
                        print(f"IRL Check: {reason}", flush=True)
                        if is_corr:
                            state = "WAITING_M1_CHOCH"
                        else:
                            print("-> Impulsive return, waiting for better corrective move", flush=True)
                # Timeout
                if price < supply_zone[0] - 200:
                    state = "SCANNING_M15"

            elif state == "WAITING_M1_CHOCH":
                # 5. M1 CHOCH
                m1_low = df_m1['l'].iloc[-20:-5].min()
                m1_high = df_m1['h'].iloc[-1]
                # Bearish CHOCH: break of M1 low after sweep
                if df_m1['c'].iloc[-1] < m1_low:
                    # Check REAL vs FAKE CHOCH - is zone above mitigated?
                    zone_above_fresh = df_m1['h'].iloc[-50:-20].max() > m1_high
                    if zone_above_fresh:
                        print(f"[M1] FAKE CHOCH @ {price} - Fresh zone above exists - This is liquidity!", flush=True)
                        state = "SCANNING_M15"
                    else:
                        print(f"*** REAL CHOCH @ {price} - M1 broke {m1_low}", flush=True)
                        # Long wick check
                        has_wick, msg = find_long_wick_liquidity_grab(df_m1)
                        print(f"-> Wick check: {msg}" if has_wick else "-> No long wick")
                        # 6. ENTER
                        sl = df_m1['h'].iloc[-10:].max() + 20
                        tp = m15_low_target
                        rr = (price - tp) / (sl - price) if sl>price else 0
                        print(f"=== ENTER SHORTS === Price:{price:.2f} SL:{sl:.2f} above M1 high TP:M15 low {tp:.2f} RR:{rr:.2f} | Target M15 low", flush=True)
                        state = "IN_TRADE"
                        # Here place real order if you connect API

            elif state == "IN_TRADE":
                if price <= m15_low_target:
                    print(f"*** TP HIT ✅ @ {price} - M15 low target reached - Win", flush=True)
                    state = "SCANNING_M15"
                # SL logic
                if price > supply_zone[1] + 50:
                    print(f"*** SL HIT @ {price}", flush=True)
                    state = "SCANNING_M15"

            print(f"[{datetime.now().strftime('%H:%M:%S')}] State:{state} Price:{price:.2f} | Supply:{supply_zone} | M15 Low:{m15_low_target:.2f}", flush=True)

        except Exception as e:
            print(f"Loop error: {e}", flush=True)

        time.sleep(15)

if __name__=="__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
