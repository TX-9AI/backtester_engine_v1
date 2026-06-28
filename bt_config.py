# bt_config.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Backtester session configuration
# v1.1 — 2026-06-28 — Renamed from config.py → bt_config.py to avoid shadowing crypto_trader/config.py

"""
All tunable backtester parameters live here.
Loaded at session start. User-facing values are prompted interactively
via main.py — this file provides defaults and constraints.

Do NOT store credentials here. Bot strategy params (ADX thresholds, etc.)
are defined in backtest/replay.py → ReplayConfig and can be overridden
by the optimizer via parameter_grid.py.
"""

from pathlib import Path

# ─── PATHS ────────────────────────────────────────────────────────────────────

DATA_DIR            = Path("data")
CACHE_DIR           = DATA_DIR / "cache"
RESULTS_DB          = DATA_DIR / "backtest_results.db"
REPORTS_DIR         = Path("reports") / "output"

# ─── DATA SOURCE ──────────────────────────────────────────────────────────────

SYMBOL              = "BTC/USD"
TIMEFRAME           = "1m"
EARLIEST_QUARTER    = "2020-Q1"    # How far back to allow

# ─── SESSION DEFAULTS (overridden at runtime via main.py prompts) ─────────────

DEFAULT_STARTING_BALANCE  = 1000.0      # USD — user changes this at session start
DEFAULT_LEVERAGE          = 10

# ─── STRATEGY DEFAULTS (mirrors live bot config.py) ──────────────────────────

DEFAULT_GRADE_A_NOTIONAL_PCT    = 0.90
DEFAULT_GRADE_B_NOTIONAL_PCT    = 0.75
DEFAULT_MIN_RRR                 = 1.5
DEFAULT_MIN_FEE_ADJUSTED_R      = 1.0
DEFAULT_ATR_STOP_MULTIPLIER     = 1.5
DEFAULT_ADX_TREND_THRESHOLD     = 20
DEFAULT_ADX_RANGE_THRESHOLD     = 15
DEFAULT_BB_WIDTH_COMPRESSION    = 0.20
DEFAULT_TRAIL_ACTIVATION_R      = 1.0
DEFAULT_PARTIAL_EXIT_PCT        = 0.50
DEFAULT_PARTIAL_MINIMUM_R       = 0.75
DEFAULT_STAGNANT_TRADE_MIN      = 120
DEFAULT_ENTRY_COOLDOWN_MIN      = 5
DEFAULT_VWAP_FILTER_ACTIVE      = True
DEFAULT_SWEEP_REJECTION_CANDLES = 2
DEFAULT_MIN_DAILY_RANGE_PCT     = 0.008

# ADX stop multipliers
DEFAULT_ADX_MULT_LOW     = 1.0    # ADX < 25
DEFAULT_ADX_MULT_MID     = 1.5    # ADX 25–40
DEFAULT_ADX_MULT_HIGH    = 2.0    # ADX 40–60
DEFAULT_ADX_MULT_EXTREME = 2.5    # ADX > 60

# ─── BACKTEST ENGINE ─────────────────────────────────────────────────────────

EQUITY_LOG_STRIDE   = 5       # Log every Nth candle to equity table (reduces DB size)
WARMUP_CANDLES      = 200     # Candles required before first entry allowed

# ─── REPORT SETTINGS ─────────────────────────────────────────────────────────

HTML_REPORT_ENABLED = True
PDF_REPORT_ENABLED  = True
REPORT_TITLE        = "Vertigo Capital — BTC Backtester"

# ─── OPTIMIZER SETTINGS ──────────────────────────────────────────────────────

OPTIMIZER_METRIC    = "profit_factor"   # Rank by: profit_factor | sharpe | net_pnl | win_rate
OPTIMIZER_MIN_TRADES = 10               # Reject param sets with fewer trades than this
WALK_FORWARD_IN_PCT  = 0.70            # 70% in-sample, 30% out-of-sample per fold
WALK_FORWARD_FOLDS   = 4

# ─── MONTE CARLO ─────────────────────────────────────────────────────────────

MONTE_CARLO_RUNS     = 1000
MONTE_CARLO_SEED     = 42
