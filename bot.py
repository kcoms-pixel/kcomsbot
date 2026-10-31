"""KCOMS V7 - multi-timeframe BTCUSDT signal engine.

Paper-trading by default. Live execution is intentionally not implemented in this
release; do not enable real orders until a separately tested Binance USD-M Futures
execution/risk module has been added.

Dependencies: pip install flask requests
Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional)
  SYMBOL=BTCUSDT
  MIN_SCORE=80
  PAPER_BALANCE=100
  PAPER_BALANCE_FILE=/tmp/kcoms_v7_balance.json
  SCAN_INTERVAL=1
  TRADE_ENABLED=false (true is rejected intentionally in this build)
"""

import json
import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify

# ----------------------------- Configuration -----------------------------
app = Flask(__name__)
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))
SCAN_INTERVAL = max(1, int(os.getenv("SCAN_INTERVAL", "1")))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "4"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "15"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))  # percent of paper equity
RR_TARGET = float(os.getenv("RR_TARGET", "2.0"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "100"))
PAPER_BALANCE_FILE = os.getenv("PAPER_BALANCE_FILE", "/tmp/kcoms_v7_balance.json")
PAPER_START_BALANCE = float(os.getenv("PAPER_BALANCE", "100"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
TRADE_ENABLED = os.getenv("TRADE_ENABLED", "false").lower() == "true"

if TRADE_ENABLED:
    raise RuntimeError(
        "KCOMS V7 release is paper-only. Live trading is blocked until a tested "
        "Binance USD-M Futures execution module is installed. Set TRADE_ENABLED=false."
    )
if not (1 <= MIN_SCORE <= 100):
    raise ValueError("MIN_SCORE must be between 1 and 100")
if not (0 < RISK_PERCENT <= 2):
    raise ValueError("RISK_PERCENT must be >0 and <=2")

BASE_URLS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
]
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "KCOMS-V7/1.0"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("KCOMS-V7")

# ----------------------------- Runtime state -----------------------------
lock = threading.RLock()
latest = {
    "bot": "KCOMS V7", "status": "BOOTING", "symbol": SYMBOL,
    "price": 0.0, "bias": "NEUTRAL", "signal": "NONE", "score": 0,
    "wins": 0, "losses": 0, "trades_today": 0,
    "balance": PAPER_START_BALANCE, "last_signal": "Starting",
    "market_regime": "UNKNOWN", "last_error": "",
}
state = {
    "balance": PAPER_START_BALANCE,
    "wins": 0,
    "losses": 0,
    "trades_today": 0,
    "day": datetime.now(timezone.utc).date().isoformat(),
    "cooldown_until": 0.0,
    "position": None,
    "last_processed_1m": None,
    "last_alert_key": None,
    "last_trade_key": None,
}

# Cached candles avoid requesting five full histories every second.
candle_cache: Dict[str, Dict[str, object]] = {}
CACHE_TTL = {"1m": 8, "5m": 20, "15m": 30, "1h": 60, "4h": 120}

# ----------------------------- Notifications -----------------------------
def send_tg(message: str) -> None:
    """Send Telegram alert; failures are logged rather than silently swallowed."""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": message}, timeout=8,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Telegram delivery failed: %s", exc)

# ----------------------------- Market data -----------------------------
def request_binance(path: str, params: dict) -> Optional[object]:
    for base in BASE_URLS:
        try:
            response = SESSION.get(base + path, params=params, timeout=8)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            log.debug("Market-data endpoint %s failed: %s", base, exc)
    return None


def get_price() -> Optional[float]:
    data = request_binance("/fapi/v1/ticker/price", {"symbol": SYMBOL})
    try:
        return float(data["price"]) if isinstance(data, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def get_klines(interval: str, limit: int = 250) -> Optional[List[dict]]:
    now = time.time()
    cached = candle_cache.get(interval)
    if cached and now - float(cached["time"]) < CACHE_TTL[interval]:
        return cached["data"]  # type: ignore[return-value]
    raw = request_binance(
        "/fapi/v1/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit}
    )
    if not isinstance(raw, list) or len(raw) < 30:
        return None
    rows = []
    for k in raw:
        try:
            rows.append({
                "open_time": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                "close_time": int(k[6]),
            })
        except (IndexError, TypeError, ValueError):
            continue
    if len(rows) < 30:
        return None
    candle_cache[interval] = {"time": now, "data": rows}
    return rows


def closed_candles(rows: List[dict]) -> List[dict]:
    """Exclude the still-forming final candle using exchange close timestamp."""
    now_ms = int(time.time() * 1000)
    return [c for c in rows if c["close_time"] < now_ms]

# ----------------------------- Indicators -----------------------------
def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def atr(candles: List[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    tr = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        tr.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    return sum(tr[-period:]) / period


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes[-period:]]
    losses = [max(-x, 0.0) for x in changes[-period:]]
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def trend_bias(candles: List[dict]) -> str:
    closes = [c["c"] for c in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    if None in (e20, e50, e200):
        return "NEUTRAL"
    last = closes[-1]
    if last > e200 and e20 > e50 > e200:
        return "BULLISH"
    if last < e200 and e20 < e50 < e200:
        return "BEARISH"
    return "NEUTRAL"

# ----------------------------- Signal analysis -----------------------------
def analyse_market() -> Optional[dict]:
    data = {tf: get_klines(tf, 250) for tf in ("1m", "5m", "15m", "1h", "4h")}
    if any(v is None for v in data.values()):
        return None
    candles = {tf: closed_candles(v) for tf, v in data.items()}  # type: ignore[arg-type]
    if any(len(v) < 205 for v in (candles["15m"], candles["1h"], candles["4h"])):
        return None
    c1, c5, c15, c1h, c4h = (candles[x] for x in ("1m", "5m", "15m", "1h", "4h"))
    price = get_price()
    if price is None:
        price = c1[-1]["c"]

    bias1h, bias4h = trend_bias(c1h), trend_bias(c4h)
    bias15 = trend_bias(c15)
    # Regime: EMA separation relative to ATR; low separation is treated as range.
    atr15 = atr(c15)
    e20_15, e50_15 = ema([x["c"] for x in c15], 20), ema([x["c"] for x in c15], 50)
    if atr15 is None or e20_15 is None or e50_15 is None:
        return None
    separation = abs(e20_15 - e50_15) / max(atr15, 1e-9)
    regime = "TRENDING" if separation >= 0.45 else "RANGING"

    # 15m structural break: close beyond the prior 20 closed-candle range.
    prior = c15[-21:-1]
    prior_high = max(x["h"] for x in prior)
    prior_low = min(x["l"] for x in prior)
    last15 = c15[-1]
    bos_long = last15["c"] > prior_high
    bos_short = last15["c"] < prior_low

    # Liquidity sweep: pierce a recent 15m swing and close back inside the level.
    swing = c15[-11:-1]
    swing_high = max(x["h"] for x in swing)
    swing_low = min(x["l"] for x in swing)
    sweep_high = last15["h"] > swing_high and last15["c"] < swing_high
    sweep_low = last15["l"] < swing_low and last15["c"] > swing_low

    # 5m CHOCH proxy: close breaks the recent opposite-side micro structure.
    last5 = c5[-1]
    micro5 = c5[-7:-1]
    choch_up = last5["c"] > max(x["h"] for x in micro5) and last5["c"] > last5["o"]
    choch_down = last5["c"] < min(x["l"] for x in micro5) and last5["c"] < last5["o"]

    # 1m trigger: close breaks recent micro range with body-direction confirmation.
    last1 = c1[-1]
    micro1 = c1[-6:-1]
    trigger_up = last1["c"] > max(x["h"] for x in micro1) and last1["c"] > last1["o"]
    trigger_down = last1["c"] < min(x["l"] for x in micro1) and last1["c"] < last1["o"]

    rsi1 = rsi([x["c"] for x in c1], 14)
    avg_vol = sum(x["v"] for x in c1[-21:-1]) / 20
    volume_ok = avg_vol > 0 and last1["v"] >= avg_vol * 1.15
    atr1 = atr(c1)

    # Score each side. Trend alignment and distinct confirmation stages are weighted.
    def score_side(side: str) -> Tuple[int, List[str]]:
        points, reasons = 0, []
        if side == "LONG":
            if bias4h == "BULLISH": points += 8; reasons.append("4H bullish")
            if bias1h == "BULLISH": points += 7; reasons.append("1H bullish")
            if bias15 == "BULLISH" and bos_long: points += 15; reasons.append("15M bullish BOS")
            if sweep_low: points += 20; reasons.append("sell-side sweep/reclaim")
            if choch_up: points += 15; reasons.append("5M bullish CHOCH")
            if trigger_up: points += 15; reasons.append("1M bullish trigger")
            # FVG proxy: three-candle imbalance; use as confluence, not a standalone entry.
            if len(c15) >= 3 and c15[-1]["l"] > c15[-3]["h"]:
                points += 10; reasons.append("15M bullish FVG")
            if volume_ok and rsi1 is not None and 50 <= rsi1 <= 72:
                points += 5; reasons.append("volume/momentum confirmation")
        else:
            if bias4h == "BEARISH": points += 8; reasons.append("4H bearish")
            if bias1h == "BEARISH": points += 7; reasons.append("1H bearish")
            if bias15 == "BEARISH" and bos_short: points += 15; reasons.append("15M bearish BOS")
            if sweep_high: points += 20; reasons.append("buy-side sweep/rejection")
            if choch_down: points += 15; reasons.append("5M bearish CHOCH")
            if trigger_down: points += 15; reasons.append("1M bearish trigger")
            if len(c15) >= 3 and c15[-1]["h"] < c15[-3]["l"]:
                points += 10; reasons.append("15M bearish FVG")
            if volume_ok and rsi1 is not None and 28 <= rsi1 <= 50:
                points += 5; reasons.append("volume/momentum confirmation")
        # Five points for a viable volatility-adjusted stop and at least 2R room.
        if atr1 and atr1 > 0 and regime == "TRENDING":
            points += 5; reasons.append("volatility/risk conditions acceptable")
        return min(points, 100), reasons

    long_score, long_reasons = score_side("LONG")
    short_score, short_reasons = score_side("SHORT")
    if long_score >= short_score:
        direction, score, reasons = "LONG", long_score, long_reasons
    else:
        direction, score, reasons = "SHORT", short_score, short_reasons

    # Require trend agreement, structural/sweep/CHOCH/trigger confluence and minimum score.
    hard_confirm = (
        (direction == "LONG" and bias1h == "BULLISH" and bias4h != "BEARISH"
         and (sweep_low or bos_long) and choch_up and trigger_up)
        or
        (direction == "SHORT" and bias1h == "BEARISH" and bias4h != "BULLISH"
         and (sweep_high or bos_short) and choch_down and trigger_down)
    )
    if regime == "RANGING":
        hard_confirm = False

    # Swing/ATR stop placement; avoid an arbitrarily fixed dollar buffer.
    if direction == "LONG":
        swing_stop = min(x["l"] for x in c1[-8:]) - (0.25 * (atr1 or 0))
        stop = min(swing_stop, price - 1.2 * (atr1 or 0))
        risk_distance = price - stop
    else:
        swing_stop = max(x["h"] for x in c1[-8:]) + (0.25 * (atr1 or 0))
        stop = max(swing_stop, price + 1.2 * (atr1 or 0))
        risk_distance = stop - price
    target = price + RR_TARGET * risk_distance if direction == "LONG" else price - RR_TARGET * risk_distance

    # Ensure risk is neither tiny nor absurdly wide versus current ATR.
    risk_ok = bool(atr1 and risk_distance >= 0.8 * atr1 and risk_distance <= 3.5 * atr1)
    if not risk_ok:
        score = min(score, MIN_SCORE - 1)
        reasons.append("rejected: stop distance outside ATR limits")
    if not hard_confirm:
        score = min(score, MIN_SCORE - 1)
        reasons.append("rejected: required confirmations not aligned")

    return {
        "price": price, "direction": direction, "score": score,
        "long_score": long_score, "short_score": short_score,
        "reasons": reasons, "bias_1h": bias1h, "bias_4h": bias4h,
        "bias_15m": bias15, "regime": regime, "atr_1m": atr1,
        "stop": stop, "target": target, "risk_distance": risk_distance,
        "confirmed": hard_confirm and risk_ok and score >= MIN_SCORE,
        "candle_key": c1[-1]["open_time"],
    }

# ----------------------------- Paper trading -----------------------------
def load_state() -> None:
    try:
        with open(PAPER_BALANCE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        state["balance"] = float(saved.get("balance", PAPER_START_BALANCE))
        state["wins"] = int(saved.get("wins", 0))
        state["losses"] = int(saved.get("losses", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        state["balance"] = PAPER_START_BALANCE


def save_state() -> None:
    tmp = PAPER_BALANCE_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(PAPER_BALANCE_FILE) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"balance": state["balance"], "wins": state["wins"], "losses": state["losses"]}, f)
        os.replace(tmp, PAPER_BALANCE_FILE)
    except OSError as exc:
        log.warning("Could not persist paper account: %s", exc)


def reset_daily_counter() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    if today != state["day"]:
        state["day"] = today
        state["trades_today"] = 0


def open_paper_position(signal: dict) -> None:
    equity = float(state["balance"])
    risk_cash = equity * (RISK_PERCENT / 100.0)
    distance = float(signal["risk_distance"])
    if distance <= 0 or equity <= 0:
        return
    qty = risk_cash / distance
    qty = min(qty, MAX_NOTIONAL_USDT / float(signal["price"]))
    if qty <= 0:
        return
    notional = qty * float(signal["price"])
    state["position"] = {
        "side": signal["direction"], "entry": float(signal["price"]),
        "stop": float(signal["stop"]), "target": float(signal["target"]),
        "qty": qty, "notional": notional, "opened_at": time.time(),
        "score": int(signal["score"]), "reasons": signal["reasons"],
    }
    state["trades_today"] += 1
    msg = (
        f"🟢 KCOMS V7 PAPER {signal['direction']} {SYMBOL}\n"
        f"Entry: {signal['price']:.2f}\nSL: {signal['stop']:.2f}\n"
        f"TP: {signal['target']:.2f}\nScore: {signal['score']}/100\n"
        f"Risk: ${risk_cash:.2f} | Notional: ${notional:.2f}\n"
        f"Evidence: {', '.join(signal['reasons'])}"
    )
    log.info(msg.replace("\n", " | "))
    send_tg(msg)
    latest["last_signal"] = msg


def monitor_paper_position(price: float) -> None:
    pos = state.get("position")
    if not pos:
        return
    side = pos["side"]
    stop_hit = price <= pos["stop"] if side == "LONG" else price >= pos["stop"]
    target_hit = price >= pos["target"] if side == "LONG" else price <= pos["target"]
    if not (stop_hit or target_hit):
        return
    # If one sampled price crosses both levels (gap), assume the adverse stop fills first.
    result = "LOSS" if stop_hit else "WIN"
    exit_price = float(pos["stop"] if stop_hit else pos["target"])
    direction = 1 if side == "LONG" else -1
    gross = (exit_price - pos["entry"]) * float(pos["qty"]) * direction
    # Conservative estimated round-trip taker fee; actual fees vary by account/tier.
    fees = float(pos["notional"]) * 0.001
    pnl = gross - fees
    state["balance"] += pnl
    if result == "WIN":
        state["wins"] += 1
    else:
        state["losses"] += 1
        state["cooldown_until"] = time.time() + COOLDOWN_MINUTES * 60
    save_state()
    msg = (
        f"{'✅' if pnl > 0 else '❌'} KCOMS V7 PAPER {result} {side}\n"
        f"Exit: {exit_price:.2f}\nNet P/L: ${pnl:.4f} (estimated fees included)\n"
        f"Paper balance: ${state['balance']:.2f}\n"
        f"W:{state['wins']} L:{state['losses']}"
    )
    log.info(msg.replace("\n", " | "))
    send_tg(msg)
    latest["last_signal"] = msg
    state["position"] = None

# ----------------------------- Main loop -----------------------------
def run_bot() -> None:
    load_state()
    latest.update({"status": "SCANNING", "balance": round(state["balance"], 2)})
    send_tg(
        f"🚀 KCOMS V7 ONLINE\n{SYMBOL} | LONG + SHORT\n"
        f"Minimum score: {MIN_SCORE}/100 | Paper trading only\n"
        f"Target RR: 1:{RR_TARGET} | Scan: {SCAN_INTERVAL}s"
    )
    log.info("KCOMS V7 started: %s; minimum score %s", SYMBOL, MIN_SCORE)

    while True:
        try:
            with lock:
                reset_daily_counter()
                price = get_price()
                if price is None:
                    latest["status"] = "WAITING_FOR_PRICE"
                    time.sleep(SCAN_INTERVAL)
                    continue
                monitor_paper_position(price)
                analysis = analyse_market()
                if analysis is None:
                    latest.update({"status": "WAITING_FOR_DATA", "price": price})
                    time.sleep(SCAN_INTERVAL)
                    continue

                latest.update({
                    "status": "IN_POSITION" if state["position"] else "SCANNING",
                    "price": round(price, 2), "bias": analysis["bias_1h"],
                    "signal": analysis["direction"], "score": analysis["score"],
                    "market_regime": analysis["regime"],
                    "wins": state["wins"], "losses": state["losses"],
                    "trades_today": state["trades_today"],
                    "balance": round(state["balance"], 2), "last_error": "",
                })

                # Alert only once per completed 1m candle to avoid Telegram spam.
                alert_key = (analysis["candle_key"], analysis["direction"], analysis["score"])
                if analysis["confirmed"] and state["last_alert_key"] != alert_key:
                    state["last_alert_key"] = alert_key
                    send_tg(
                        f"🔎 KCOMS V7 SETUP {analysis['direction']} {SYMBOL}\n"
                        f"Score: {analysis['score']}/100\nPrice: {price:.2f}\n"
                        f"Regime: {analysis['regime']}\n"
                        f"Evidence: {', '.join(analysis['reasons'])}"
                    )

                can_open = (
                    analysis["confirmed"] and state["position"] is None
                    and state["trades_today"] < MAX_TRADES_PER_DAY
                    and time.time() >= float(state["cooldown_until"])
                    and state["last_trade_key"] != analysis["candle_key"]
                )
                if can_open:
                    state["last_trade_key"] = analysis["candle_key"]
                    open_paper_position(analysis)
                latest["status"] = "IN_POSITION" if state["position"] else "SCANNING"
                latest["wins"] = state["wins"]
                latest["losses"] = state["losses"]
                latest["trades_today"] = state["trades_today"]
                latest["balance"] = round(state["balance"], 2)
            time.sleep(SCAN_INTERVAL)
        except Exception as exc:
            log.exception("Main loop error")
            latest["last_error"] = str(exc)
            latest["status"] = "ERROR_RECOVERING"
            time.sleep(max(2, SCAN_INTERVAL))

# ----------------------------- Status endpoints -----------------------------
@app.route("/")
def home():
    with lock:
        data = dict(latest)
    return (
        "<h1>KCOMS V7</h1>"
        f"<p>{data['symbol']} | PAPER ONLY | Minimum score {MIN_SCORE}/100</p>"
        f"<p>Status: {data['status']} | Price: {data['price']} | Bias: {data['bias']}</p>"
        f"<p>Signal: {data['signal']} ({data['score']}/100) | Regime: {data['market_regime']}</p>"
        f"<p>Balance: ${data['balance']} | W:{data['wins']} L:{data['losses']} "
        f"| Today: {data['trades_today']}/{MAX_TRADES_PER_DAY}</p>"
        f"<p>Last: {data['last_signal']}</p>"
    )


@app.route("/health")
def health():
    return jsonify({"ok": True, "bot": "KCOMS V7", "status": latest["status"]}), 200


@app.route("/status")
def status():
    with lock:
        return jsonify({**latest, "open_position": state["position"] is not None,
                        "paper_only": True, "minimum_score": MIN_SCORE})


# Start the engine when the module is imported by Gunicorn (Render).
# Deploy with ONE Gunicorn worker so only one trading engine runs.
_engine_thread = None
_engine_start_lock = threading.Lock()

def start_engine_once() -> None:
    global _engine_thread
    with _engine_start_lock:
        if _engine_thread is None or not _engine_thread.is_alive():
            _engine_thread = threading.Thread(
                target=run_bot, daemon=True, name="kcoms-engine"
            )
            _engine_thread.start()


# Gunicorn imports this module; __main__ is not executed in that deployment mode.
start_engine_once()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
