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
LOG_INTERVAL_SECONDS = max(1, int(os.getenv("LOG_INTERVAL_SECONDS", "5")))
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
    "https://data-api.binance.vision",
    "https://api.binance.com",
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
SESSION.headers.update({"User-Agent": "KCOMS-V7.1/1.0"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("KCOMS-V7.1")

# ----------------------------- Runtime state -----------------------------
lock = threading.RLock()
latest = {
    "bot": "KCOMS V7.1", "status": "BOOTING", "symbol": SYMBOL,
    "price": 0.0, "price_source": "NOT_CONNECTED", "price_timestamp": None, "price_age_seconds": None, "bias": "NEUTRAL", "signal": "NONE", "score": 0,
    "wins": 0, "losses": 0, "completed_trades": 0, "trades_today": 0,
    "balance": PAPER_START_BALANCE, "last_signal": "Starting",
    "market_regime": "UNKNOWN", "last_error": "",
}
state = {
    "balance": PAPER_START_BALANCE,
    "wins": 0,
    "losses": 0,
