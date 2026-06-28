# status.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Session dashboard: current config, cached quarters, DB summary
# v1.1 — 2026-06-28 — Import bt_config instead of config to avoid shadowing crypto_trader/config.py

"""
Displays a clean pre-flight dashboard of the current backtester state.
Run automatically at the end of install.sh, or any time manually.

Usage:
    python status.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bt_config as cfg
from backtest.data_fetcher import DataFetcher

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
CYAN   = '\033[0;36m'
RED    = '\033[0;31m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

def col(text, c):   return f"{c}{text}{RESET}"
def bold(text):     return f"{BOLD}{text}{RESET}"
def ok(text):       return f"  {GREEN}✓{RESET}  {text}"
def warn(text):     return f"  {YELLOW}⚠{RESET}  {text}"
def item(k, v):     return f"  {DIM}{k:<28}{RESET}{v}"

def sep(char="─", width=58):
    return col(char * width, CYAN)

# ── Data helpers ──────────────────────────────────────────────────────────────

def get_db_summary():
    db = Path(cfg.RESULTS_DB)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(db)
        runs   = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        last   = conn.execute(
            "SELECT quarter, net_pnl, win_rate, total_trades, created_at "
            "FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return {"runs": runs, "trades": trades, "last": last}
    except Exception:
        return None

def get_strategy_files():
    ct_dir = Path(__file__).parent / "crypto_trader"
    if not ct_dir.exists():
        return [], False
    files = [f.name for f in ct_dir.glob("*.py") if f.name != "__init__.py"]
    return sorted(files), len(files) > 0

def get_cached_quarters(fetcher, available):
    cached   = [q for q in available if fetcher.is_cached(q)]
    uncached = [q for q in available if not fetcher.is_cached(q)]
    return cached, uncached

# ── Main display ──────────────────────────────────────────────────────────────

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fetcher   = DataFetcher(cfg.CACHE_DIR)
    available = fetcher.list_available_quarters()
    cached, uncached = get_cached_quarters(fetcher, available)
    db_summary = get_db_summary()
    strategy_files, strategy_ready = get_strategy_files()

    print()
    print(col("╔══════════════════════════════════════════════════════════╗", CYAN))
    print(col("║", CYAN) + bold(f"  backtester_engine_v1  |  Vertigo Capital") + "               " + col("║", CYAN))
    print(col("║", CYAN) + f"  BTC/USD  |  Kraken Historical  |  {now}" + "  " + col("║", CYAN))
    print(col("╚══════════════════════════════════════════════════════════╝", CYAN))

    # ── Session parameters ────────────────────────────────────────────────────
    print()
    print(bold("  SESSION PARAMETERS"))
    print(sep())

    buying_power = cfg.DEFAULT_STARTING_BALANCE * cfg.DEFAULT_LEVERAGE
    print(item("Default balance:",
               col(f"${cfg.DEFAULT_STARTING_BALANCE:,.0f}", GREEN) +
               f"  →  {col('$'+f'{buying_power:,.0f}', CYAN)} buying power ({cfg.DEFAULT_LEVERAGE}x)"))
    print(item("Grade A size:",     f"{cfg.DEFAULT_GRADE_A_NOTIONAL_PCT*100:.0f}% of buying power"))
    print(item("Grade B size:",     f"{cfg.DEFAULT_GRADE_B_NOTIONAL_PCT*100:.0f}% of buying power"))
    print(item("Min R:R:",          str(cfg.DEFAULT_MIN_RRR)))
    print(item("ATR stop mult:",    str(cfg.DEFAULT_ATR_STOP_MULTIPLIER)))
    print(item("ADX trend thresh:", str(cfg.DEFAULT_ADX_TREND_THRESHOLD)))
    print(item("ADX range thresh:", str(cfg.DEFAULT_ADX_RANGE_THRESHOLD)))
    print(item("BB compression:",   f"{cfg.DEFAULT_BB_WIDTH_COMPRESSION*100:.0f}%"))
    print(item("Trail activation:", f"{cfg.DEFAULT_TRAIL_ACTIVATION_R}R"))
    print(item("Partial exit:",     f"{cfg.DEFAULT_PARTIAL_EXIT_PCT*100:.0f}% at {cfg.DEFAULT_PARTIAL_MINIMUM_R}R"))
    print(item("Stagnant timeout:", f"{cfg.DEFAULT_STAGNANT_TRADE_MIN} min"))
    print(item("Entry cooldown:",   f"{cfg.DEFAULT_ENTRY_COOLDOWN_MIN} min"))
    print(item("VWAP gate:",        col("ACTIVE", GREEN) if cfg.DEFAULT_VWAP_FILTER_ACTIVE else col("OFF", YELLOW)))
    print(item("Min daily range:",  f"{cfg.DEFAULT_MIN_DAILY_RANGE_PCT*100:.1f}%"))
    print(item("ADX stop mults:",
               f"<25={cfg.DEFAULT_ADX_MULT_LOW}x  "
               f"25-40={cfg.DEFAULT_ADX_MULT_MID}x  "
               f"40-60={cfg.DEFAULT_ADX_MULT_HIGH}x  "
               f">60={cfg.DEFAULT_ADX_MULT_EXTREME}x"))

    # ── Data cache ────────────────────────────────────────────────────────────
    print()
    print(bold("  DATA CACHE"))
    print(sep())
    print(item("Total quarters:",   f"{len(available)}  (2020-Q1 → {available[-1]})"))
    print(item("Cached:",
               col(f"{len(cached)} quarters", GREEN) if cached
               else col("0 — will fetch on first run", YELLOW)))
    print(item("Not cached:",       f"{len(uncached)} quarters"))

    if cached:
        # Show last 3 cached
        recent = cached[-3:]
        print(item("Recently cached:", "  ".join(col(q, GREEN) for q in recent)))

    print(item("Cache dir:",        str(cfg.CACHE_DIR)))

    # ── Strategy files ────────────────────────────────────────────────────────
    print()
    print(bold("  STRATEGY FILES  (crypto_trader/)"))
    print(sep())
    if strategy_ready:
        print(ok(f"{len(strategy_files)} files loaded"))
        for f in strategy_files:
            print(f"       {DIM}{f}{RESET}")
    else:
        print(warn("No strategy files found in crypto_trader/"))
        print(f"  {DIM}Re-run install.sh and answer Y at Step 3{RESET}")

    # ── Results DB ────────────────────────────────────────────────────────────
    print()
    print(bold("  RESULTS DATABASE"))
    print(sep())
    if db_summary:
        print(ok(f"{db_summary['runs']} runs  |  {db_summary['trades']} trades logged"))
        if db_summary["last"]:
            q, pnl, wr, n, ts = db_summary["last"]
            pnl_col = col(f"${pnl:+,.2f}", GREEN if pnl >= 0 else RED) if pnl else "—"
            wr_str  = f"{wr*100:.1f}%" if wr else "—"
            print(item("Last run:", f"{q}  |  {n} trades  |  {pnl_col}  |  WR {wr_str}"))
        print(item("DB path:", str(cfg.RESULTS_DB)))
    else:
        print(f"  {DIM}No runs yet — database will be created on first backtest{RESET}")
        print(item("DB path:", str(cfg.RESULTS_DB)))

    # ── Quick commands ────────────────────────────────────────────────────────
    print()
    print(bold("  QUICK START"))
    print(sep())
    print(f"  {CYAN}python main.py{RESET}                          interactive session")
    print(f"  {CYAN}python main.py --quarter 2025-Q3{RESET}        single quarter")
    print(f"  {CYAN}python main.py --all-quarters{RESET}           full history")
    print(f"  {CYAN}python main.py --balance 5000{RESET}           override balance")
    print(f"  {CYAN}python reports/html_report.py --latest{RESET}  last report")
    print(f"  {CYAN}python status.py{RESET}                        this screen")
    print()

if __name__ == "__main__":
    main()
