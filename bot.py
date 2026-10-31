"""KCOMS V7.2 - multi-timeframe BTCUSDT signal engine with V6.7-compatible Binance price fallback.

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
LOG_INTERVAL_SECONDS = max(1, int(os.getenv("LOG_INTERVAL_SECONDS", "2")))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "15"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "5"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.1"))  # percent of paper equity
RR_TARGET = float(os.getenv("RR_TARGET", "2.0"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "100"))
PAPER_BALANCE_FILE = os.getenv("PAPER_BALANCE_FILE", "/tmp/kcoms_v7_balance.json")
PAPER_START_BALANCE = float(os.getenv("PAPER_BALANCE", "100"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
TRADE_ENABLED = os.getenv("TRADE_ENABLED", "false").lower() == "true"
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0.001"))  # estimated fee per side
PRICE_STALE_SECONDS = max(3, int(os.getenv("PRICE_STALE_SECONDS", "10")))

if TRADE_ENABLED:
    raise RuntimeError(
        "KCOMS V7 release is paper-only. Live trading is blocked until a tested "
        "Binance USD-M Futures execution module is installed. Set TRADE_ENABLED=false."
    )
if not (1 <= MIN_SCORE <= 100):
    raise ValueError("MIN_SCORE must be between 1 and 100")
if not (0 < RISK_PERCENT <= 2):
    raise ValueError("RISK_PERCENT must be >0 and <=2")

# Binance Spot public market-data only. No Futures endpoints are used for V7 paper analysis.
# data-api.binance.vision is the public market-data host; official Spot API hosts are fallbacks.
SPOT_REST_ENDPOINTS = [
    # V6.7 successfully used this public market-data host for /api/v3/klines.
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api-gcp.binance.com",
]
PRICE_WS_URLS = [
    f"wss://data-stream.binance.vision/ws/{SYMBOL.lower()}@trade",
    f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@trade",
]
price_lock = threading.RLock()
price_feed = {"price": None, "received_at": 0.0, "source": "WAITING_FOR_BINANCE_SPOT"}
ws_thread = None
ws_stop = threading.Event()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; KCOMS-V7.2/1.0)"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("KCOMS-V7.2")

# ----------------------------- Runtime state -----------------------------
lock = threading.RLock()
latest = {
    "bot": "KCOMS V7.2", "status": "BOOTING", "symbol": SYMBOL,
    "price": 0.0, "ema": 0.0, "price_source": "NOT_CONNECTED", "price_timestamp": None, "price_age_seconds": None, "bias": "NEUTRAL", "signal": "NONE", "score": 0,
    "wins": 0, "losses": 0, "completed_trades": 0, "trades_today": 0,
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
    "last_status_log": 0.0,
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
def _record_price(value: float, source: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid Binance Spot price")
    with price_lock:
        price_feed.update({"price": value, "received_at": time.time(), "source": source})
    latest["price_source"] = source
    latest["price_timestamp"] = datetime.now(timezone.utc).isoformat()
    latest["price_age_seconds"] = 0.0
    latest["last_error"] = ""


def _spot_get(path: str, params: dict) -> Optional[object]:
    failures = []
    for base in SPOT_REST_ENDPOINTS:
        try:
            response = SESSION.get(base + path, params=params, timeout=(3, 6))
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                raise ValueError(f"Binance API {payload.get('code')}: {payload.get('msg')}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{base}: {exc}")
    latest["last_error"] = " | ".join(failures[-3:]) or "No Binance Spot REST endpoint responded"
    return None


def _websocket_price_worker() -> None:
    """Reconnect to Binance public Spot trade stream; REST remains the fallback."""
    try:
        import websocket
    except ImportError:
        log.warning("websocket-client is not installed; Binance Spot REST price polling will be used")
        return
    attempt = 0
    while not ws_stop.is_set():
        url = PRICE_WS_URLS[attempt % len(PRICE_WS_URLS)]
        try:
            def on_message(_ws, message):
                try:
                    payload = json.loads(message)
                    _record_price(float(payload["p"]), "BINANCE_SPOT_WS")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    log.warning("Ignored malformed Binance Spot WebSocket message")
            def on_error(_ws, error):
                log.warning("Binance Spot WebSocket error: %s", error)
            def on_close(_ws, _code, _message):
                log.warning("Binance Spot WebSocket disconnected")
            app_ws = websocket.WebSocketApp(url, on_message=on_message,
                                            on_error=on_error, on_close=on_close)
            app_ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as exc:
            log.warning("Binance Spot WebSocket connection failed: %s", exc)
        attempt += 1
        ws_stop.wait(min(30, 2 ** min(attempt, 5)))


def start_price_websocket_once() -> None:
    global ws_thread
    if ws_thread is not None and ws_thread.is_alive():
        return
    ws_thread = threading.Thread(target=_websocket_price_worker, daemon=True, name="binance-spot-ws-v72")
    ws_thread.start()


def _price_from_kline(interval: str) -> Optional[float]:
    """Read the live close of Binance's currently-forming Spot candle.

    This deliberately mirrors the V6.7 method (reading kline[4]) that the
    operator confirmed worked on Render. It is a live-updating candle close,
    not the last completed candle close.
    """
    raw = _spot_get("/api/v3/klines", {"symbol": SYMBOL, "interval": interval, "limit": 2})
    if not isinstance(raw, list) or not raw:
        return None
    try:
        value = float(raw[-1][4])
        if math.isfinite(value) and value > 0:
            return value
    except (IndexError, TypeError, ValueError):
        return None
    return None


def get_price() -> Optional[float]:
    """Get fresh Binance Spot BTCUSDT price, preserving V6.7's working route.

    Priority: fresh Binance trade WebSocket, Spot ticker REST, then the
    V6.7-compatible live kline-close fallback (1m, then 15m).
    """
    with price_lock:
        cached_price = price_feed["price"]
        age = time.time() - float(price_feed["received_at"])
        source = str(price_feed["source"])
    if cached_price is not None and age <= PRICE_STALE_SECONDS:
        latest["price_source"] = source
        latest["price_age_seconds"] = round(age, 2)
        return float(cached_price)

    # First try the direct last-traded-price endpoint.
    data = _spot_get("/api/v3/ticker/price", {"symbol": SYMBOL})
    try:
        value = float(data["price"]) if isinstance(data, dict) else None
        if value is not None and math.isfinite(value) and value > 0:
            _record_price(value, "BINANCE_SPOT_REST_TICKER")
            return value
    except (KeyError, TypeError, ValueError):
        pass

    # Important: V6.7 fetched its working price from the latest kline close.
    # Keep this exact data path as the primary fallback instead of depending
    # solely on /ticker/price, which may be blocked while /klines still works.
    for interval, source_name in (("1m", "BINANCE_SPOT_1M_KLINE"),
                                  ("15m", "BINANCE_SPOT_15M_KLINE_FALLBACK")):
        value = _price_from_kline(interval)
        if value is not None:
            _record_price(value, source_name)
            return value

    latest["price_source"] = "UNAVAILABLE"
    with price_lock:
        latest["price_age_seconds"] = (round(time.time() - float(price_feed["received_at"]), 2)
                                        if price_feed["received_at"] else None)
    return None


def get_klines(interval: str, limit: int = 250) -> Optional[List[dict]]:
    now = time.time()
    cached = candle_cache.get(interval)
    if cached and now - float(cached["time"]) < CACHE_TTL[interval]:
        return cached["data"]  # type: ignore[return-value]
    raw = _spot_get("/api/v3/klines", {"symbol": SYMBOL, "interval": interval, "limit": limit})
    if not isinstance(raw, list) or len(raw) < 30:
        return None
    rows = []
    previous_open = -1
    for k in raw:
        try:
            row = {"open_time": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                   "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                   "close_time": int(k[6])}
            if (row["open_time"] <= previous_open or
                not all(math.isfinite(row[x]) for x in ("o", "h", "l", "c", "v")) or
                min(row["o"], row["h"], row["l"], row["c"]) <= 0 or row["v"] < 0 or
                row["h"] < max(row["o"], row["c"], row["l"]) or
                row["l"] > min(row["o"], row["c"], row["h"])):
                continue
            rows.append(row)
            previous_open = row["open_time"]
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
def analyse_market(current_price: Optional[float] = None) -> Optional[dict]:
    data = {tf: get_klines(tf, 250) for tf in ("1m", "5m", "15m", "1h", "4h")}
    if any(v is None for v in data.values()):
        return None
    candles = {tf: closed_candles(v) for tf, v in data.items()}  # type: ignore[arg-type]
    if any(len(v) < 205 for v in (candles["15m"], candles["1h"], candles["4h"])):
        return None
    c1, c5, c15, c1h, c4h = (candles[x] for x in ("1m", "5m", "15m", "1h", "4h"))
    price = current_price if current_price is not None else get_price()
    if price is None:
        return None

    bias1h, bias4h = trend_bias(c1h), trend_bias(c4h)
    bias15 = trend_bias(c15)
    # Regime: EMA separation relative to ATR; low separation is treated as range.
    atr15 = atr(c15)
    closes15 = [x["c"] for x in c15]
    e20_15, e50_15 = ema(closes15, 20), ema(closes15, 50)
    e200_15 = ema(closes15, 200)
    if e200_15 is not None:
        latest["ema"] = round(e200_15, 2)
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
        state["balance"] = max(0.0, float(saved.get("balance", PAPER_START_BALANCE)))
        state["wins"] = max(0, int(saved.get("wins", 0)))
        state["losses"] = max(0, int(saved.get("losses", 0)))
        state["trades_today"] = max(0, int(saved.get("trades_today", 0)))
        state["day"] = str(saved.get("day", state["day"]))
        state["cooldown_until"] = float(saved.get("cooldown_until", 0.0))
        state["position"] = saved.get("position")
        state["last_trade_key"] = saved.get("last_trade_key")
        state["last_alert_key"] = saved.get("last_alert_key")
        if state["position"] is not None:
            required = {"side", "entry", "stop", "target", "qty", "notional"}
            if not required.issubset(state["position"]) or state["position"]["side"] not in ("LONG", "SHORT"):
                log.warning("Discarding invalid saved paper position")
                state["position"] = None
        reset_daily_counter()
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Could not restore paper state; starting from configured balance: %s", exc)
        state.update({"balance": PAPER_START_BALANCE, "wins": 0, "losses": 0,
                      "trades_today": 0, "position": None, "cooldown_until": 0.0})


def save_state() -> None:
    tmp = PAPER_BALANCE_FILE + ".tmp"
    payload = {key: state[key] for key in ("balance", "wins", "losses", "trades_today", "day",
               "cooldown_until", "position", "last_trade_key", "last_alert_key")}
    try:
        os.makedirs(os.path.dirname(PAPER_BALANCE_FILE) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PAPER_BALANCE_FILE)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("Could not persist paper account: %s", exc)


def reset_daily_counter() -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    if today != state["day"]:
        state["day"] = today
        state["trades_today"] = 0
        save_state()


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
    save_state()
    msg = (
        f"🟢 KCOMS V7.2 PAPER {signal['direction']} {SYMBOL}\n"
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
    # Estimated taker fee on both entry and exit notionals; actual rates vary by account/tier.
    exit_notional = float(pos["qty"]) * exit_price
    fees = (float(pos["notional"]) + exit_notional) * PAPER_FEE_RATE
    pnl = gross - fees
    result = "WIN" if pnl > 0 else "LOSS"
    state["balance"] += pnl
    if result == "WIN":
        state["wins"] += 1
    else:
        state["losses"] += 1
        state["cooldown_until"] = time.time() + COOLDOWN_MINUTES * 60
    msg = (
        f"{'✅' if pnl > 0 else '❌'} KCOMS V7.2 PAPER {result} {side}\n"
        f"Exit: {exit_price:.2f}\nNet P/L: ${pnl:.4f} (estimated fees included)\n"
        f"Paper balance: ${state['balance']:.2f}\n"
        f"W:{state['wins']} L:{state['losses']}"
    )
    log.info(msg.replace("\n", " | "))
    send_tg(msg)
    latest["last_signal"] = msg
    state["position"] = None
    save_state()
    latest.update({"wins": state["wins"], "losses": state["losses"],
                   "completed_trades": state["wins"] + state["losses"],
                   "balance": round(state["balance"], 2)})

# ----------------------------- Console monitoring -----------------------------
def log_live_status(price: Optional[float], force: bool = False) -> None:
    """Print the compact V6.7-style status line requested for Render logs."""
    now = time.time()
    if not force and now - float(state.get("last_status_log", 0.0)) < LOG_INTERVAL_SECONDS:
        return
    state["last_status_log"] = now

    # Use the 200-period EMA from completed Binance Spot 15m candles.
    ema_value = float(latest.get("ema") or 0.0)
    if ema_value <= 0:
        candles = get_klines("15m", 250)
        if candles:
            closed = closed_candles(candles)
            calculated = ema([c["c"] for c in closed], 200)
            if calculated is not None:
                ema_value = calculated
                latest["ema"] = round(calculated, 2)

    if price is not None and math.isfinite(float(price)) and float(price) > 0:
        price_text = f"{float(price):.2f}"
    else:
        price_text = "UNAVAILABLE"

    position = state.get("position")
    status = "IN_TRADE" if position else latest.get("status", "SCANNING")
    if status in ("IN_POSITION", "SCANNING", "BOOTING") and price is not None:
        status = "SCANNING"
    balance = float(state.get("balance", PAPER_START_BALANCE))
    wins = int(state.get("wins", 0))
    losses = int(state.get("losses", 0))
    trades = int(state.get("trades_today", 0))
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Price {price_text} | "
        f"EMA {ema_value:.2f} | {status} | Bal ${balance:.2f} | "
        f"W:{wins} L:{losses} D:{trades}/{MAX_TRADES_PER_DAY}",
        flush=True,
    )


# ----------------------------- Main loop -----------------------------
def run_bot() -> None:
    load_state()
    start_price_websocket_once()
    latest.update({"status": "SCANNING", "balance": round(state["balance"], 2)})
    send_tg(
        f"🚀 KCOMS V7.2 ONLINE\n{SYMBOL} | LONG + SHORT\n"
        f"Minimum score: {MIN_SCORE}/100 | Paper trading only\n"
        f"Target RR: 1:{RR_TARGET} | Scan: {SCAN_INTERVAL}s"
    )
    log.info("KCOMS V7.2 started: %s; minimum score %s; risk %.3f%%; daily trade limit %s; cooldown %sm; heartbeat every %ss", SYMBOL, MIN_SCORE, RISK_PERCENT, MAX_TRADES_PER_DAY, COOLDOWN_MINUTES, LOG_INTERVAL_SECONDS)
    log_live_status(None, force=True)

    while True:
        try:
            with lock:
                reset_daily_counter()
                price = get_price()
                if price is None:
                    latest["status"] = "WAITING_FOR_PRICE"
                    latest["last_error"] = latest.get("last_error") or "Binance Spot price unavailable"
                    log.warning("BINANCE SPOT PRICE UNAVAILABLE | REST and WebSocket feed unavailable | %s", latest["last_error"])
                    log_live_status(None)
                    time.sleep(SCAN_INTERVAL)
                    continue
                monitor_paper_position(price)
                analysis = analyse_market(price)
                if analysis is None:
                    latest.update({
                        "status": "IN_TRADE" if state["position"] else "WAITING_FOR_DATA",
                        "price": round(price, 2), "wins": state["wins"],
                        "losses": state["losses"],
                        "completed_trades": state["wins"] + state["losses"],
                        "trades_today": state["trades_today"],
                        "balance": round(state["balance"], 2),
                    })
                    log_live_status(price)
                    time.sleep(SCAN_INTERVAL)
                    continue

                latest.update({
                    "status": "IN_POSITION" if state["position"] else "SCANNING",
                    "price": round(price, 2), "bias": analysis["bias_1h"],
                    "signal": analysis["direction"], "score": analysis["score"],
                    "market_regime": analysis["regime"],
                    "wins": state["wins"], "losses": state["losses"],
                    "completed_trades": state["wins"] + state["losses"],
                    "trades_today": state["trades_today"],
                    "balance": round(state["balance"], 2), "last_error": "",
                })

                # Alert only once per completed 1m candle to avoid Telegram spam.
                alert_key = (analysis["candle_key"], analysis["direction"], analysis["score"])
                if analysis["confirmed"] and state["last_alert_key"] != alert_key:
                    state["last_alert_key"] = alert_key
                    send_tg(
                        f"🔎 KCOMS V7.2 SETUP {analysis['direction']} {SYMBOL}\n"
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
                log_live_status(price)
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
        "<h1>KCOMS V7.2</h1>"
        f"<p>{data['symbol']} | BINANCE SPOT DATA | PAPER ONLY | Minimum score {MIN_SCORE}/100</p>"
        f"<p>Status: {data['status']} | Price: {data['price']} | Source: {data.get('price_source', 'UNKNOWN')} | Bias: {data['bias']}</p>"
        f"<p>Signal: {data['signal']} ({data['score']}/100) | Regime: {data['market_regime']}</p>"
        f"<p>Paper balance: ${data['balance']} | Completed: {data.get('completed_trades', data['wins'] + data['losses'])} "
        f"| Wins: {data['wins']} | Losses: {data['losses']} "
        f"| Today: {data['trades_today']}/{MAX_TRADES_PER_DAY}</p>"
        f"<p>Open position: {'YES' if state['position'] else 'NO'} | Log heartbeat: every {LOG_INTERVAL_SECONDS}s</p>"
        f"<p>Last: {data['last_signal']}</p>"
    )


@app.route("/health")
def health():
    return jsonify({"ok": True, "bot": "KCOMS V7.2", "status": latest["status"]}), 200


@app.route("/status")
def status():
    with lock:
        completed = state["wins"] + state["losses"]
        return jsonify({**latest, "completed_trades": completed,
                        "win_rate_percent": round(100.0 * state["wins"] / completed, 2) if completed else 0.0,
                        "open_position": state["position"] is not None,
                        "position": state["position"],
                        "trades_today_limit": MAX_TRADES_PER_DAY,
                        "paper_only": True, "minimum_score": MIN_SCORE})


# Start the analysis engine when the module is imported by Gunicorn as well as
# when run directly. Render's Procfile uses exactly one Gunicorn worker so this
# process-local guard prevents duplicate engine threads in the supported setup.
_engine_thread = None
_engine_start_lock = threading.Lock()


def start_engine_once() -> None:
    global _engine_thread
    with _engine_start_lock:
        if _engine_thread is not None and _engine_thread.is_alive():
            return
        _engine_thread = threading.Thread(
            target=run_bot, daemon=True, name="kcoms-v7.2-engine"
        )
        _engine_thread.start()


# Gunicorn imports this module; starting here ensures the paper engine runs.
if os.getenv("KCOMS_DISABLE_ENGINE", "false").lower() != "true":
    start_engine_once()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
