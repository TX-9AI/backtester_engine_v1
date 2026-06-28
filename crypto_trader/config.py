"""
config.py — crypto_trader v6.0
v6.0 — original release
v6.1 — 2026-06-27 — remove credentials.py import, read from environment variables

BTC/USD only. Fee-aware. Auto-sizing based on grade and live balance.
No user-configured position sizes. No circuit breaker.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

# ─── CREDENTIALS (from systemd environment) ───────────────────────────────────
KRAKEN_API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
BOT_NAME          = os.environ.get("BOT_NAME", "crypto_trader")

# ─── INSTRUMENT — BTC ONLY ────────────────────────────────────────────────────
TRADING_SYMBOL  = "BTC/USD"
KRAKEN_SYMBOL   = "XBT/USD:BTNL"
INSTRUMENT      = "BTC"

# ─── ACCOUNT ──────────────────────────────────────────────────────────────────
ACCOUNT_BALANCE_USD       = float(os.environ.get("BOT_CASH_BALANCE", "2000"))
LEVERAGE                  = 10

# ─── MODE ─────────────────────────────────────────────────────────────────────
PAPER_TRADING             = os.environ.get("PAPER_TRADING", "True") != "False"

# ─── AUTO POSITION SIZING ─────────────────────────────────────────────────────
GRADE_A_NOTIONAL_PCT      = 0.90
GRADE_B_NOTIONAL_PCT      = 0.75
GRADE_C_NOTIONAL_PCT      = 0.50
TRADE_GRADE_C             = False

# ─── KRAKEN FEE STRUCTURE ─────────────────────────────────────────────────────
KRAKEN_TAKER_FEE          = 0.0080
KRAKEN_MAKER_FEE          = 0.0040
KRAKEN_MARGIN_OPEN_FEE    = 0.0002
KRAKEN_ROLLOVER_FEE       = 0.0002
KRAKEN_ROLLOVER_HOURS     = 4
MIN_FEE_ADJUSTED_R        = 1.0

# ─── DAILY RANGE FILTER ───────────────────────────────────────────────────────
MIN_DAILY_RANGE_PCT       = 0.008

# ─── ENTRY GATES ──────────────────────────────────────────────────────────────
MIN_RRR                   = 1.5
VWAP_FILTER_ACTIVE        = True
MIN_TF_CONFLUENCE         = 1
ENTRY_COOLDOWN_MINUTES    = 5

# ─── SETUP SCORER ─────────────────────────────────────────────────────────────
GRADE_A_MIN_SCORE         = 0.78
GRADE_B_MIN_SCORE         = 0.55
GRADE_SIZE_MULTIPLIER     = {"A": 1.0, "B": 1.0, "C": 0.5}

# ─── STOPS ────────────────────────────────────────────────────────────────────
ATR_STOP_MULTIPLIER       = 1.5

# ─── REGIME ───────────────────────────────────────────────────────────────────
ADX_TREND_THRESHOLD       = 20
ADX_RANGE_THRESHOLD       = 15
ATR_EXPANSION_MULTIPLIER  = 1.5
BB_WIDTH_COMPRESSION_PCT  = 0.20
SWEEP_REJECTION_CANDLES   = 2
EQUAL_LEVEL_PCT           = 0.001
REGIME_REASSESS_MINUTES   = 5

# ─── VIX ──────────────────────────────────────────────────────────────────────
VIX_LOW_THRESHOLD         = 15
VIX_ELEVATED_THRESHOLD    = 25
VIX_CRISIS_THRESHOLD      = 35
VIX_CRISIS_NO_LONG        = True
VIX_NO_ENTRY_THRESHOLD    = 30

# ─── STRATEGY PARAMETERS ──────────────────────────────────────────────────────
BB_PERIOD                 = 20
BB_STD                    = 2.0
EMA_FAST                  = 9
EMA_MID                   = 21
EMA_SLOW                  = 50
EMA_ANCHOR                = 200
ATR_PERIOD                = 14

# ─── LIQUIDITY ────────────────────────────────────────────────────────────────
EQUAL_HIGH_LOW_LOOKBACK   = 50
ORDER_BLOCK_LOOKBACK      = 20
IMBALANCE_MIN_SIZE_PCT    = 0.002
LIQUIDITY_BUFFER_PCT      = 0.003

# ─── STRUCTURE ────────────────────────────────────────────────────────────────
SWING_LOOKBACK            = 10
MIN_SWING_SIZE_ATR        = 0.5
FVG_MIN_SIZE_PCT          = 0.001
SR_TOUCH_MIN              = 2
SR_ZONE_PCT               = 0.002

# ─── EXIT / TRAIL ─────────────────────────────────────────────────────────────
TRAIL_ACTIVATION_R        = 1.0
TRAIL_STEP_1_R            = 1.0
TRAIL_STEP_2_R            = 2.0
TRAIL_STEP_3_R            = 3.0
TRAIL_TIGHTEN_ON_REGIME   = 0.25
PARTIAL_EXIT_PCT          = 0.50
PARTIAL_MINIMUM_R         = 0.75
STAGNANT_TRADE_MINUTES    = 120

# ─── ORDER EXECUTION ──────────────────────────────────────────────────────────
ORDER_TYPE_DEFAULT        = "market"
LIMIT_ORDER_SLIPPAGE_PCT  = 0.0005
PAPER_FILL_SLIPPAGE_PCT   = 0.001
POLL_INTERVAL_SECONDS     = 10

# ─── TIMEFRAMES ───────────────────────────────────────────────────────────────
TIMEFRAMES = {
    "1d":  {"candles": 10,  "role": "bias"},
    "1h":  {"candles": 50,  "role": "structure"},
    "15m": {"candles": 50,  "role": "trend"},
    "5m":  {"candles": 100, "role": "entry_context"},
    "1m":  {"candles": 60,  "role": "trigger"},
}
CACHE_STALENESS_SECONDS = {
    "1d": 3600, "1h": 300, "15m": 120, "5m": 30, "1m": 10,
}

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────
NOTIFY_ON_ENTRY           = True
NOTIFY_ON_EXIT            = True
NOTIFY_CIRCUIT_BREAKER    = False
NOTIFY_REGIME_CHANGE      = False
NOTIFY_ON_REGIME_CHANGE   = True
NOTIFY_PNL_UPDATE_MINUTES = 30
NOTIFY_CONSECUTIVE_LOSS   = True

# ─── MAX OPEN RISK ────────────────────────────────────────────────────────────
MAX_OPEN_RISK_PCT         = 1.0

# ─── PATHS ────────────────────────────────────────────────────────────────────
DB_PATH                   = os.path.expanduser("~/crypto-trader/trades.db")
LOG_FILE                  = os.path.expanduser("~/crypto-trader/bot.log")
LOG_ROTATION_MB           = 50
LOG_LEVEL                 = "INFO"

# ─── MACRO ────────────────────────────────────────────────────────────────────
FOREX_FACTORY_URL         = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MACRO_FETCH_INTERVAL_MIN  = 60

# ─── MISC ─────────────────────────────────────────────────────────────────────
TRADING_24_7              = True
BLACKOUT_WINDOWS          = []
MAX_CONSECUTIVE_LOSSES    = 3
MIN_ORDER_SIZE_BTC        = 0.0001
DIAGNOSTIC_MODE           = False

SCORE_WEIGHTS = {
    "regime_conviction":     0.25,
    "structure_alignment":   0.20,
    "tf_confluence":         0.20,
    "liquidity_clear":       0.15,
    "vwap_context":          0.10,
    "macro_alignment":       0.10,
}

# ─── SESSION CONFIG ───────────────────────────────────────────────────────────
@dataclass
class SessionConfig:
    paper_trading:     bool  = PAPER_TRADING
    instrument:        str   = TRADING_SYMBOL
    notes:             str   = ""
    confirmed_at:      str   = ""
