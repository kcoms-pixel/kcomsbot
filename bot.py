"""KCOMS V7.6 - multi-timeframe BTCUSDT signal engine with V6.7-compatible Binance price fallback.

Paper-trading by default. Live execution is intentionally not implemented in this
release; do not enable real orders until a separately tested Binance USD-M Futures
execution/risk module has been added.

Dependencies: pip install flask requests
Environment:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional)
  SYMBOL=BTCUSDT
  MIN_SCORE=60
  PAPER_BALANCE=100
  PAPER_BALANCE_FILE=/tmp/kcoms_v7_balance.json
  SCAN_INTERVAL=1
  COOLDOWN_MINUTES=2
  PRICE_STALE_SECONDS=5
  PRICE_RELAY_INTERVAL=15
  TRADE_ENABLED=false (true is rejected intentionally in this build)
"""

import json
import logging
import math
import os
import threading
import time
import queue
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify

# ----------------------------- Configuration -----------------------------
app = Flask(__name__)
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
MIN_SCORE = int(os.getenv("MIN_SCORE", "60"))
SCAN_INTERVAL = max(1, int(os.getenv("SCAN_INTERVAL", "1")))
LOG_INTERVAL_SECONDS = max(1, int(os.getenv("LOG_INTERVAL_SECONDS", "2")))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "15"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "2"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.1"))  # percent of paper equity
RR_TARGET = float(os.getenv("RR_TARGET", "2.0"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "100"))
PAPER_BALANCE_FILE = os.getenv("PAPER_BALANCE_FILE", "/tmp/kcoms_v7_balance.json")
PAPER_START_BALANCE = float(os.getenv("PAPER_BALANCE", "100"))
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
TRADE_ENABLED = os.getenv("TRADE_ENABLED", "false").lower() == "true"
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0.001"))  # estimated fee per side
PRICE_STALE_SECONDS = max(2, int(os.getenv("PRICE_STALE_SECONDS", "5")))
PRICE_RELAY_INTERVAL = max(5, int(os.getenv("PRICE_RELAY_INTERVAL", "15")))
REST_CONNECT_TIMEOUT = max(0.5, float(os.getenv("REST_CONNECT_TIMEOUT", "1.5")))
REST_READ_TIMEOUT = max(1.0, float(os.getenv("REST_READ_TIMEOUT", "2.5")))
# Signal freshness windows: lower-timeframe confirmation may arrive after structure forms.
CHOCH_VALID_30S_CANDLES = max(1, int(os.getenv("CHOCH_VALID_30S_CANDLES", "5")))
STRUCTURE_VALID_5M_CANDLES = max(1, int(os.getenv("STRUCTURE_VALID_5M_CANDLES", "2")))

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
EAT = timezone(timedelta(hours=3), "EAT")  # East Africa Time (UTC+3)

def current_clock() -> dict:
    now = datetime.now(EAT)
    return {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
            "timezone": "EAT", "timestamp": now.isoformat()}

price_lock = threading.RLock()
price_feed = {
    "price": None, "received_at": 0.0, "source": "WAITING_FOR_BINANCE_SPOT",
    "exchange_event_ms": None, "exchange_trade_ms": None,
    "latency_ms": None, "ws_connected": False, "last_ws_message_at": 0.0,
}
telegram_queue = queue.Queue(maxsize=500)
telegram_worker_started = False
telegram_last_relay = 0.0
rest_endpoint_retry_after = {}
ws_thread = None
ws_stop = threading.Event()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; KCOMS-V7.6/1.0)"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("KCOMS-V7.6")

# ----------------------------- Runtime state -----------------------------
lock = threading.RLock()
latest = {
    "bot": "KCOMS V7.6", "status": "BOOTING", "symbol": SYMBOL,
    **current_clock(),
    "price": 0.0, "ema": 0.0, "price_source": "NOT_CONNECTED", "price_timestamp": None,
    "price_age_seconds": None, "price_latency_ms": None, "price_feed_status": "CONNECTING",
    "bias": "NEUTRAL", "signal": "NONE", "score": 0,
    "wins": 0, "losses": 0, "completed_trades": 0, "trades_today": 0,
    "balance": PAPER_START_BALANCE, "last_signal": "Starting",
    "market_regime": "UNKNOWN", "entry_status": "WAITING", "rejection_reasons": [],
    "side_diagnostics": {}, "long_score": 0, "short_score": 0,
    "entry_trigger": "NONE", "last_error": "",
}
state = {
    "balance": PAPER_START_BALANCE,
    "wins": 0,
    "losses": 0,
    "trades_today": 0,
    "day": datetime.now(EAT).date().isoformat(),
    "cooldown_until": 0.0,
    "position": None,
    "last_processed_1m": None,
    "last_alert_key": None,
    "last_trade_key": None,
    "last_status_log": 0.0,
    "last_diagnostic_key": None,
    "rejection_counts": {},
    "recent_rejections": [],
}

# Cached candles avoid requesting five full histories every second.
candle_cache: Dict[str, Dict[str, object]] = {}
CACHE_TTL = {"1m": 2, "5m": 15, "15m": 30, "30m": 45, "1h": 60}

# ----------------------------- Notifications -----------------------------
def _telegram_sender_worker() -> None:
    """Deliver queued Telegram messages off the market-scanning thread."""
    while True:
        message = telegram_queue.get()
        try:
            if TG_TOKEN and TG_CHAT:
                response = SESSION.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id": TG_CHAT, "text": message},
                    timeout=(2, 4),
                )
                response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("Telegram delivery failed: %s", exc)
        finally:
            telegram_queue.task_done()


def start_telegram_worker_once() -> None:
    global telegram_worker_started
    if telegram_worker_started:
        return
    telegram_worker_started = True
    threading.Thread(
        target=_telegram_sender_worker, daemon=True, name="telegram-outbox"
    ).start()


def send_tg(message: str) -> None:
    """Queue Telegram alerts so network delays never block price scanning."""
    if not TG_TOKEN or not TG_CHAT:
        return
    start_telegram_worker_once()
    try:
        telegram_queue.put_nowait(message)
    except queue.Full:
        log.error("Telegram outbox full; dropping oldest pending message")
        try:
            telegram_queue.get_nowait()
            telegram_queue.task_done()
            telegram_queue.put_nowait(message)
        except queue.Empty:
            pass

# ----------------------------- Market data -----------------------------
def _record_price(value: float, source: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid Binance Spot price")
    received = time.time()
    with price_lock:
        price_feed.update({"price": value, "received_at": received, "source": source})
        if source != "BINANCE_SPOT_WS":
            # A REST snapshot has no exchange event timestamp; don't reuse old WS latency.
            price_feed["exchange_event_ms"] = None
            price_feed["exchange_trade_ms"] = None
            price_feed["latency_ms"] = None
        age_ms = None
        if price_feed.get("exchange_event_ms") is not None:
            age_ms = max(0.0, received * 1000.0 - float(price_feed["exchange_event_ms"]))
        price_feed["latency_ms"] = round(age_ms, 1) if age_ms is not None else None
    latest["price_source"] = source
    latest["price_timestamp"] = datetime.now(timezone.utc).isoformat()
    latest["price_age_seconds"] = 0.0
    latest["price_latency_ms"] = price_feed.get("latency_ms")
    latest["last_error"] = ""


def _spot_get(path: str, params: dict) -> Optional[object]:
    failures = []
    now = time.time()
    for base in SPOT_REST_ENDPOINTS:
        if rest_endpoint_retry_after.get(base, 0.0) > now:
            continue
        try:
            response = SESSION.get(
                base + path, params=params,
                timeout=(REST_CONNECT_TIMEOUT, REST_READ_TIMEOUT),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                raise ValueError(f"Binance API {payload.get('code')}: {payload.get('msg')}")
            rest_endpoint_retry_after.pop(base, None)
            return payload
        except (requests.RequestException, ValueError) as exc:
            failures.append(f"{base}: {exc}")
            # Temporarily skip a failing host rather than paying its timeout on every scan.
            rest_endpoint_retry_after[base] = time.time() + 30
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
            def on_open(_ws):
                with price_lock:
                    price_feed["ws_connected"] = True
                latest["price_feed_status"] = "WEBSOCKET_CONNECTED"
                log.info("Binance Spot WebSocket connected: %s", url)

            def on_message(_ws, message):
                try:
                    payload = json.loads(message)
                    price = float(payload["p"])
                    event_ms = payload.get("E")
                    trade_ms = payload.get("T")
                    with price_lock:
                        price_feed["exchange_event_ms"] = int(event_ms) if event_ms else None
                        price_feed["exchange_trade_ms"] = int(trade_ms) if trade_ms else None
                        price_feed["last_ws_message_at"] = time.time()
                    _record_price(price, "BINANCE_SPOT_WS")
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    log.warning("Ignored malformed Binance Spot WebSocket message")

            def on_error(_ws, error):
                latest["price_feed_status"] = "WEBSOCKET_ERROR"
                log.warning("Binance Spot WebSocket error: %s", error)

            def on_close(_ws, _code, _message):
                with price_lock:
                    price_feed["ws_connected"] = False
                latest["price_feed_status"] = "WEBSOCKET_RECONNECTING"
                log.warning("Binance Spot WebSocket disconnected")

            app_ws = websocket.WebSocketApp(
                url, on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close,
            )
            app_ws.run_forever(ping_interval=15, ping_timeout=8)
        except Exception as exc:
            log.warning("Binance Spot WebSocket connection failed: %s", exc)
        attempt += 1
        ws_stop.wait(min(30, 2 ** min(attempt, 5)))


def start_price_websocket_once() -> None:
    global ws_thread
    if ws_thread is not None and ws_thread.is_alive():
        return
    ws_thread = threading.Thread(target=_websocket_price_worker, daemon=True, name="binance-spot-ws-v76")
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
        with price_lock:
            latest["price_latency_ms"] = price_feed.get("latency_ms")
            latest["price_feed_status"] = (
                "WEBSOCKET_CONNECTED" if price_feed.get("ws_connected")
                else "REST_OR_CACHED"
            )
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
    latest["price_feed_status"] = "STALE_OR_DISCONNECTED"
    with price_lock:
        latest["price_age_seconds"] = (round(time.time() - float(price_feed["received_at"]), 2)
                                        if price_feed["received_at"] else None)
        latest["price_latency_ms"] = price_feed.get("latency_ms")
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
def entry_confirmation(direction: str, bias30: str, regime: str,
                       side_sweep: bool, side_bos: bool, side_choch: bool,
                       side_trigger: bool) -> Tuple[bool, List[str]]:
    """Return hard-gate result and transparent reasons for the selected side."""
    is_long = direction == "LONG"
    expected = "BULLISH" if is_long else "BEARISH"
    opposite = "BEARISH" if is_long else "BULLISH"
    rejected = []
    if bias30 == opposite:
        rejected.append("30M trend opposes setup")
    if not side_choch:
        rejected.append("no fresh 1M CHOCH")
    if not side_trigger:
        rejected.append("no current 1M breakout or retest/reclaim trigger")
    if regime == "TRENDING" and not (side_sweep or side_bos):
        rejected.append("no recent 5M BOS or liquidity sweep")
    if regime == "RANGING" and not side_sweep:
        rejected.append("range entry requires a 5M liquidity sweep")
    trend_context_ok = bias30 == expected or (bias30 == "NEUTRAL" and regime == "RANGING" and side_sweep)
    structure_ok = (side_sweep or side_bos) if regime == "TRENDING" else side_sweep
    confirmed = trend_context_ok and structure_ok and side_choch and side_trigger
    return confirmed, rejected


def analyse_market(current_price: Optional[float] = None) -> Optional[dict]:
    # Binance Spot provides native 1m/5m/15m/30m/1h klines. CHOCH is detected
    # on a completed 1m candle; its validity expires after the configured number
    # of completed 30-second clock intervals. No separate 30s signal is required.
    data = {tf: get_klines(tf, 250) for tf in ("1m", "5m", "15m", "30m", "1h")}
    if any(v is None for v in data.values()):
        return None
    candles = {tf: closed_candles(v) for tf, v in data.items()}  # type: ignore[arg-type]
    if any(len(v) < 205 for v in (candles["15m"], candles["30m"], candles["1h"])):
        return None
    c1, c5, c15, c30, c1h = (candles[x] for x in ("1m", "5m", "15m", "30m", "1h"))
    price = current_price if current_price is not None else get_price()
    if price is None:
        return None

    bias30, bias1h = trend_bias(c30), trend_bias(c1h)
    bias15 = trend_bias(c15)
    # Keep the 15m volatility classification as context, not as a blanket veto.
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

    # Recent 5m BOS and liquidity sweeps remain eligible for two completed 5m candles.
    bos_long = bos_short = sweep_high = sweep_low = False
    bos_long_age = bos_short_age = sweep_high_age = sweep_low_age = None
    first_recent_5 = max(20, len(c5) - STRUCTURE_VALID_5M_CANDLES - 1)
    for idx in range(first_recent_5, len(c5)):
        candle = c5[idx]
        prior20 = c5[idx - 20:idx]
        prior10 = c5[idx - 10:idx]
        if len(prior20) < 20 or len(prior10) < 10:
            continue
        age = len(c5) - 1 - idx
        if candle["c"] > max(x["h"] for x in prior20):
            if bos_long_age is None or age < bos_long_age:
                bos_long, bos_long_age = True, age
        if candle["c"] < min(x["l"] for x in prior20):
            if bos_short_age is None or age < bos_short_age:
                bos_short, bos_short_age = True, age
        swing_high = max(x["h"] for x in prior10)
        swing_low = min(x["l"] for x in prior10)
        if candle["h"] > swing_high and candle["c"] < swing_high:
            if sweep_high_age is None or age < sweep_high_age:
                sweep_high, sweep_high_age = True, age
        if candle["l"] < swing_low and candle["c"] > swing_low:
            if sweep_low_age is None or age < sweep_low_age:
                sweep_low, sweep_low_age = True, age

    # 1m CHOCH remains active for the configured number of completed 30s intervals.
    choch_up = choch_down = False
    choch_up_age = choch_down_age = None
    now_ms = int(time.time() * 1000)
    latest_30s_close_ms = (now_ms // 30000) * 30000
    first_recent_1 = max(6, len(c1) - 7)
    for idx in range(first_recent_1, len(c1)):
        candle = c1[idx]
        micro = c1[idx - 6:idx]
        if len(micro) < 6:
            continue
        age_30s = max(0, (latest_30s_close_ms - candle["close_time"]) // 30000)
        if age_30s > CHOCH_VALID_30S_CANDLES:
            continue
        if candle["c"] > max(x["h"] for x in micro) and candle["c"] > candle["o"]:
            if choch_up_age is None or age_30s < choch_up_age:
                choch_up, choch_up_age = True, age_30s
        if candle["c"] < min(x["l"] for x in micro) and candle["c"] < candle["o"]:
            if choch_down_age is None or age_30s < choch_down_age:
                choch_down, choch_down_age = True, age_30s

    # Entry trigger: accept a fresh breakout OR a breakout retest/reclaim.
    last1 = c1[-1]
    micro1 = c1[-6:-1]
    trigger_up_breakout = last1["c"] > max(x["h"] for x in micro1) and last1["c"] > last1["o"]
    trigger_down_breakout = last1["c"] < min(x["l"] for x in micro1) and last1["c"] < last1["o"]
    atr1 = atr(c1)
    retest_tolerance = 0.15 * (atr1 or 0.0)
    trigger_up_retest = trigger_down_retest = False
    retest_up_level = retest_down_level = None
    # Look for a breakout in the preceding three completed 1m candles, then a retest.
    for idx in range(max(5, len(c1) - 4), len(c1) - 1):
        prior = c1[idx - 5:idx]
        if len(prior) < 5:
            continue
        level_up = max(x["h"] for x in prior)
        level_down = min(x["l"] for x in prior)
        breakout = c1[idx]
        if breakout["c"] > level_up and breakout["c"] > breakout["o"]:
            if last1["l"] <= level_up + retest_tolerance and last1["c"] > level_up and last1["c"] > last1["o"]:
                trigger_up_retest, retest_up_level = True, level_up
        if breakout["c"] < level_down and breakout["c"] < breakout["o"]:
            if last1["h"] >= level_down - retest_tolerance and last1["c"] < level_down and last1["c"] < last1["o"]:
                trigger_down_retest, retest_down_level = True, level_down
    trigger_up = trigger_up_breakout or trigger_up_retest
    trigger_down = trigger_down_breakout or trigger_down_retest

    rsi1 = rsi([x["c"] for x in c1], 14)
    avg_vol = sum(x["v"] for x in c1[-21:-1]) / 20
    volume_ok = avg_vol > 0 and last1["v"] >= avg_vol * 1.15

    def score_side(side: str) -> Tuple[int, List[str]]:
        points, reasons = 0, []
        if side == "LONG":
            if bias30 == "BULLISH": points += 12; reasons.append("30M bullish primary trend")
            elif bias30 == "NEUTRAL": points += 5; reasons.append("30M neutral; range reversal permitted with sweep")
            else: points -= 10; reasons.append("30M bearish against LONG")
            if bias1h == "BULLISH": points += 6; reasons.append("1H bullish alignment")
            elif bias1h == "BEARISH": points -= 4; reasons.append("1H bearish headwind (not an automatic veto)")
            if bos_long: points += 15; reasons.append(f"recent 5M bullish BOS (age {bos_long_age})")
            if sweep_low: points += 20; reasons.append(f"recent 5M sell-side sweep/reclaim (age {sweep_low_age})")
            if choch_up: points += 15; reasons.append(f"1M bullish CHOCH active ({choch_up_age}/{CHOCH_VALID_30S_CANDLES} completed 30s candles)")
            if trigger_up_breakout: points += 15; reasons.append("1M bullish breakout trigger")
            elif trigger_up_retest: points += 15; reasons.append("1M bullish retest/reclaim trigger")
            if len(c15) >= 3 and c15[-1]["l"] > c15[-3]["h"]:
                points += 8; reasons.append("15M bullish FVG")
            if volume_ok and rsi1 is not None and 48 <= rsi1 <= 74:
                points += 5; reasons.append("volume/momentum confirmation")
        else:
            if bias30 == "BEARISH": points += 12; reasons.append("30M bearish primary trend")
            elif bias30 == "NEUTRAL": points += 5; reasons.append("30M neutral; range reversal permitted with sweep")
            else: points -= 10; reasons.append("30M bullish against SHORT")
            if bias1h == "BEARISH": points += 6; reasons.append("1H bearish alignment")
            elif bias1h == "BULLISH": points -= 4; reasons.append("1H bullish headwind (not an automatic veto)")
            if bos_short: points += 15; reasons.append(f"recent 5M bearish BOS (age {bos_short_age})")
            if sweep_high: points += 20; reasons.append(f"recent 5M buy-side sweep/rejection (age {sweep_high_age})")
            if choch_down: points += 15; reasons.append(f"1M bearish CHOCH active ({choch_down_age}/{CHOCH_VALID_30S_CANDLES} completed 30s candles)")
            if trigger_down_breakout: points += 15; reasons.append("1M bearish breakout trigger")
            elif trigger_down_retest: points += 15; reasons.append("1M bearish retest/reclaim trigger")
            if len(c15) >= 3 and c15[-1]["h"] < c15[-3]["l"]:
                points += 8; reasons.append("15M bearish FVG")
            if volume_ok and rsi1 is not None and 26 <= rsi1 <= 52:
                points += 5; reasons.append("volume/momentum confirmation")
        if atr1 and atr1 > 0 and regime == "TRENDING":
            points += 5; reasons.append("trending volatility conditions")
        elif regime == "RANGING" and (sweep_low if side == "LONG" else sweep_high):
            points += 5; reasons.append("range reversal supported by liquidity sweep")
        return max(0, min(points, 100)), reasons

    long_score, long_reasons = score_side("LONG")
    short_score, short_reasons = score_side("SHORT")

    def evaluate_side(side: str, side_score: int, side_reasons: List[str]) -> dict:
        is_long_side = side == "LONG"
        side_sweep = sweep_low if is_long_side else sweep_high
        side_bos = bos_long if is_long_side else bos_short
        side_choch = choch_up if is_long_side else choch_down
        side_trigger = trigger_up if is_long_side else trigger_down
        hard_confirm, rejected = entry_confirmation(
            side, bias30, regime, side_sweep, side_bos, side_choch, side_trigger
        )
        if is_long_side:
            swing_stop = min(x["l"] for x in c1[-8:]) - (0.25 * (atr1 or 0))
            stop = min(swing_stop, price - 1.2 * (atr1 or 0))
            risk_distance = price - stop
        else:
            swing_stop = max(x["h"] for x in c1[-8:]) + (0.25 * (atr1 or 0))
            stop = max(swing_stop, price + 1.2 * (atr1 or 0))
            risk_distance = stop - price
        target = price + RR_TARGET * risk_distance if is_long_side else price - RR_TARGET * risk_distance
        risk_ok = bool(atr1 and risk_distance >= 0.8 * atr1 and risk_distance <= 3.5 * atr1)
        if not risk_ok:
            rejected.append("stop distance outside ATR risk limits")
        if side_score < MIN_SCORE:
            rejected.append(f"score {side_score} below minimum {MIN_SCORE}")
        confirmed = hard_confirm and risk_ok and side_score >= MIN_SCORE
        if not confirmed and not rejected:
            rejected.append("setup did not pass all entry gates")
        trigger_name = (
            "BREAKOUT" if (trigger_up_breakout if is_long_side else trigger_down_breakout)
            else "RETEST_RECLAIM" if (trigger_up_retest if is_long_side else trigger_down_retest)
            else "NONE"
        )
        return {
            "direction": side, "score": side_score, "reasons": side_reasons,
            "rejection_reasons": rejected, "confirmed": confirmed,
            "stop": stop, "target": target, "risk_distance": risk_distance,
            "entry_trigger": trigger_name,
        }

    long_candidate = evaluate_side("LONG", long_score, long_reasons)
    short_candidate = evaluate_side("SHORT", short_score, short_reasons)
    # Prefer a fully qualified setup over a higher-scoring side that failed a hard gate.
    qualified = [x for x in (long_candidate, short_candidate) if x["confirmed"]]
    selected = max(qualified or [long_candidate, short_candidate], key=lambda x: x["score"])
    direction = selected["direction"]
    score = selected["score"]
    reasons = selected["reasons"]
    rejection_reasons = selected["rejection_reasons"]
    stop, target = selected["stop"], selected["target"]
    risk_distance = selected["risk_distance"]
    confirmed = selected["confirmed"]

    return {
        "price": price, "direction": direction, "score": score,
        "long_score": long_score, "short_score": short_score,
        "reasons": reasons, "rejection_reasons": rejection_reasons,
        "side_diagnostics": {
            "LONG": {"score": long_candidate["score"], "confirmed": long_candidate["confirmed"],
                     "rejection_reasons": long_candidate["rejection_reasons"]},
            "SHORT": {"score": short_candidate["score"], "confirmed": short_candidate["confirmed"],
                      "rejection_reasons": short_candidate["rejection_reasons"]},
        },
        "entry_trigger": selected["entry_trigger"],
        "bias_30m": bias30, "bias_1h": bias1h,
        "bias_15m": bias15, "regime": regime, "atr_1m": atr1,
        "confirmation_windows": {
            "choch_valid_30s_candles": CHOCH_VALID_30S_CANDLES,
            "structure_valid_5m_candles": STRUCTURE_VALID_5M_CANDLES,
            "choch_up_age_30s": choch_up_age, "choch_down_age_30s": choch_down_age,
            "bos_long_age_5m": bos_long_age, "bos_short_age_5m": bos_short_age,
            "sweep_low_age_5m": sweep_low_age, "sweep_high_age_5m": sweep_high_age,
        },
        "stop": stop, "target": target, "risk_distance": risk_distance,
        "confirmed": confirmed,
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
    today = datetime.now(EAT).date().isoformat()
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
        f"🟢 KCOMS V7.6 PAPER {signal['direction']} {SYMBOL}\nDate: {current_clock()['date']} EAT\n"
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
        f"{'✅' if pnl > 0 else '❌'} KCOMS V7.6 PAPER {result} {side}\nDate: {current_clock()['date']} EAT\n"
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

# ----------------------------- Telegram price relay -----------------------------
def relay_price_to_telegram(price: float) -> None:
    """Send periodic live-price snapshots without blocking the scan loop."""
    global telegram_last_relay
    now = time.time()
    if not TG_TOKEN or not TG_CHAT or now - telegram_last_relay < PRICE_RELAY_INTERVAL:
        return
    telegram_last_relay = now
    with price_lock:
        source = price_feed.get("source", "UNKNOWN")
        age = max(0.0, now - float(price_feed.get("received_at") or now))
        latency = price_feed.get("latency_ms")
        connected = bool(price_feed.get("ws_connected"))
    latency_text = f"{latency:.0f}ms" if isinstance(latency, (int, float)) else "n/a"
    send_tg(
        f"📡 {SYMBOL} PRICE UPDATE\n"
        f"Price: {price:.2f}\n"
        f"Feed: {source} | Age: {age:.1f}s\n"
        f"Exchange-to-server latency: {latency_text}\n"
        f"WebSocket: {'CONNECTED' if connected else 'RECONNECTING / REST FALLBACK'}"
    )


# ----------------------------- Console monitoring -----------------------------
def log_live_status(price: Optional[float], force: bool = False) -> None:
    """Print the compact V6.7-style status line requested for Render logs."""
    now = time.time()
    if not force and now - float(state.get("last_status_log", 0.0)) < LOG_INTERVAL_SECONDS:
        return
    state["last_status_log"] = now
    latest.update(current_clock())

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
    cooldown_remaining = max(0, int(float(state.get("cooldown_until", 0.0)) - now))
    if position:
        status = f"IN_TRADE_{position['side']}"
        trade_detail = (f"ENTRY {float(position['entry']):.2f} | "
                        f"SL {float(position['stop']):.2f} | TP {float(position['target']):.2f}")
    elif cooldown_remaining > 0:
        status = "COOLING"
        trade_detail = f"COOLDOWN {cooldown_remaining}s remaining"
    else:
        status = latest.get("status", "SCANNING")
        if status in ("IN_POSITION", "SCANNING", "BOOTING") and price is not None:
            status = "SCANNING"
        trade_detail = ""
    balance = float(state.get("balance", PAPER_START_BALANCE))
    wins = int(state.get("wins", 0))
    losses = int(state.get("losses", 0))
    trades = int(state.get("trades_today", 0))
    print(
        f"[{latest['date']} {latest['time']} EAT] Price {price_text} | "
        f"EMA {ema_value:.2f} | {status} | Bal ${balance:.2f} | "
        f"W:{wins} L:{losses} D:{trades}/{MAX_TRADES_PER_DAY}"
        f"{' | ' + trade_detail if trade_detail else ''}",
        flush=True,
    )


# ----------------------------- Main loop -----------------------------
def run_bot() -> None:
    load_state()
    start_telegram_worker_once()
    start_price_websocket_once()
    latest.update({"status": "SCANNING", "balance": round(state["balance"], 2)})
    send_tg(
        f"🚀 KCOMS V7.6 ONLINE\nDate: {current_clock()['date']} EAT\n{SYMBOL} | LONG + SHORT\n"
        f"Trend: 30M primary + 1H preference | BOS/Sweep: 5M | CHOCH: 1M\n"
        f"Entry: 1M breakout or retest/reclaim | Range reversals require 5M sweep\n"
        f"CHOCH validity: {CHOCH_VALID_30S_CANDLES} completed 30s candles\n"
        f"Minimum score: {MIN_SCORE}/100 | Adaptive entries | Paper trading only\n"
        f"Target RR: 1:{RR_TARGET} | Scan: {SCAN_INTERVAL}s\n"
        f"Live price relay: every {PRICE_RELAY_INTERVAL}s | Stale threshold: {PRICE_STALE_SECONDS}s"
    )
    log.info("KCOMS V7.6 started: %s; minimum score %s; risk %.3f%%; daily trade limit %s; cooldown %sm; heartbeat every %ss", SYMBOL, MIN_SCORE, RISK_PERCENT, MAX_TRADES_PER_DAY, COOLDOWN_MINUTES, LOG_INTERVAL_SECONDS)
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
                relay_price_to_telegram(price)
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

                # Record one diagnostic sample per completed 1m candle (not every scan).
                if state.get("last_diagnostic_key") != analysis["candle_key"]:
                    state["last_diagnostic_key"] = analysis["candle_key"]
                    if not analysis["confirmed"]:
                        for reason in analysis["rejection_reasons"]:
                            counts = state.setdefault("rejection_counts", {})
                            counts[reason] = int(counts.get(reason, 0)) + 1
                    recent = state.setdefault("recent_rejections", [])
                    recent.append({
                        "time": current_clock()["timestamp"],
                        "direction": analysis["direction"],
                        "score": analysis["score"],
                        "confirmed": analysis["confirmed"],
                        "reasons": analysis["rejection_reasons"],
                        "side_diagnostics": analysis.get("side_diagnostics", {}),
                    })
                    state["recent_rejections"] = recent[-20:]

                latest.update({
                    "status": "IN_POSITION" if state["position"] else "SCANNING",
                    "price": round(price, 2), "bias": analysis["bias_30m"],
                    "signal": analysis["direction"], "score": analysis["score"],
                    "market_regime": analysis["regime"],
                    "entry_status": "CONFIRMED" if analysis["confirmed"] else "REJECTED",
                    "rejection_reasons": analysis["rejection_reasons"],
                    "side_diagnostics": analysis.get("side_diagnostics", {}),
                    "long_score": analysis["long_score"], "short_score": analysis["short_score"],
                    "entry_trigger": analysis["entry_trigger"],
                    "rejection_counts": dict(state.get("rejection_counts", {})),
                    "recent_rejections": list(state.get("recent_rejections", [])),
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
                        f"🔎 KCOMS V7.6 SETUP {analysis['direction']} {SYMBOL}\nDate: {current_clock()['date']} EAT\n"
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
        latest.update(current_clock())
        data = dict(latest)
    return (
        "<h1>KCOMS V7.6</h1>"
        f"<p>Date: {data['date']} | Time: {data['time']} {data['timezone']}</p>"
        f"<p>{data['symbol']} | BINANCE SPOT DATA | PAPER ONLY | Minimum score {MIN_SCORE}/100</p>"
        f"<p>Status: {data['status']} | Price: {data['price']} | Source: {data.get('price_source', 'UNKNOWN')} | Bias: {data['bias']}</p>"
        f"<p>Feed: {data.get('price_feed_status', 'UNKNOWN')} | Price age: {data.get('price_age_seconds')}s | Exchange latency: {data.get('price_latency_ms')}ms</p>"
        f"<p>Signal: {data['signal']} ({data['score']}/100) | Regime: {data['market_regime']} | Entry: {data.get('entry_status', 'WAITING')} | Trigger: {data.get('entry_trigger', 'NONE')}</p>"
        f"<p>LONG score: {data.get('long_score', 0)} | SHORT score: {data.get('short_score', 0)}</p>"
        f"<p>Rejection reasons: {', '.join(data.get('rejection_reasons', [])) or 'None'}</p>"
        f"<p>Side diagnostics: {data.get('side_diagnostics', {})}</p>"
        f"<p>Rejection counts: {data.get('rejection_counts', {})}</p>"
        f"<p>Paper balance: ${data['balance']} | Completed: {data.get('completed_trades', data['wins'] + data['losses'])} "
        f"| Wins: {data['wins']} | Losses: {data['losses']} "
        f"| Today: {data['trades_today']}/{MAX_TRADES_PER_DAY}</p>"
        f"<p>Open position: {'YES' if state['position'] else 'NO'} | Log heartbeat: every {LOG_INTERVAL_SECONDS}s</p>"
        f"<p>Trade details: {('SIDE ' + str(state['position']['side']) + ' | ENTRY ' + format(float(state['position']['entry']), '.2f') + ' | SL ' + format(float(state['position']['stop']), '.2f') + ' | TP ' + format(float(state['position']['target']), '.2f')) if state['position'] else ('COOLING: ' + str(max(0, int(float(state['cooldown_until']) - time.time()))) + ' seconds remaining' if float(state['cooldown_until']) > time.time() else 'No open trade / not cooling')}</p>"
        f"<p>Last: {data['last_signal']}</p>"
    )


@app.route("/health")
def health():
    latest.update(current_clock())
    return jsonify({"ok": True, "bot": "KCOMS V7.6", "status": latest["status"], **current_clock()}), 200


@app.route("/status")
def status():
    with lock:
        latest.update(current_clock())
        completed = state["wins"] + state["losses"]
        return jsonify({**latest, "completed_trades": completed,
                        "win_rate_percent": round(100.0 * state["wins"] / completed, 2) if completed else 0.0,
                        "open_position": state["position"] is not None,
                        "position": state["position"],
                        "trades_today_limit": MAX_TRADES_PER_DAY,
                        "cooldown_active": time.time() < float(state["cooldown_until"]),
                        "cooldown_remaining_seconds": max(0, int(float(state["cooldown_until"]) - time.time())),
                        "trade_levels": ({"side": state["position"]["side"],
                                         "entry": state["position"]["entry"],
                                         "stop_loss": state["position"]["stop"],
                                         "take_profit": state["position"]["target"]}
                                        if state["position"] else None),
                        "paper_only": True, "minimum_score": MIN_SCORE,
                        "rejection_counts": dict(state.get("rejection_counts", {})),
                        "recent_rejections": list(state.get("recent_rejections", [])),
                        "side_diagnostics": latest.get("side_diagnostics", {})})


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
            target=run_bot, daemon=True, name="kcoms-v7.6-engine"
        )
        _engine_thread.start()


# Gunicorn imports this module; starting here ensures the paper engine runs.
if os.getenv("KCOMS_DISABLE_ENGINE", "false").lower() != "true":
    start_engine_once()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
