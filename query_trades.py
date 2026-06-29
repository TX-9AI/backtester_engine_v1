# query_trades.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Query and display trade results from backtest DB

"""
Displays trade results for the most recent (or specified) backtest run.

Usage:
    python query_trades.py              # most recent run
    python query_trades.py --run 7      # specific run ID
    python query_trades.py --all        # all runs summary
"""

import sys
import argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import bt_config as cfg
from backtest.backtest_logger import BacktestLogger

GREEN  = '\033[0;32m'
RED    = '\033[0;31m'
YELLOW = '\033[1;33m'
CYAN   = '\033[0;36m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

parser = argparse.ArgumentParser()
parser.add_argument("--run",  type=int, default=None, help="Run ID (default: most recent)")
parser.add_argument("--all",  action="store_true",    help="Show all runs summary")
args = parser.parse_args()

bl = BacktestLogger(cfg.RESULTS_DB)

# ── All runs summary ──────────────────────────────────────────────────────────
if args.all:
    runs = bl.get_runs()
    if runs is None or runs.empty:
        print("\n  No runs in database.\n")
        sys.exit(0)
    print(f"\n  {BOLD}All Backtest Runs{RESET}")
    print(f"  {'─'*70}")
    print(f"  {'ID':<5} {'Quarter':<10} {'Balance':<12} {'Net P&L':<12} {'Trades':<8} {'Win%':<8} {'Started'}")
    print(f"  {'─'*70}")
    for _, r in runs.iterrows():
        pnl      = float(r.get('net_pnl') or 0)
        color    = GREEN if pnl >= 0 else RED
        trades_n = int(r['total_trades']) if r.get('total_trades') == r.get('total_trades') else 0
        win_r    = float(r['win_rate']) if r.get('win_rate') == r.get('win_rate') else 0.0
        s_bal    = float(r['starting_balance']) if r.get('starting_balance') == r.get('starting_balance') else 0.0
        print(f"  {int(r['run_id']):<5} {str(r.get('quarter','?')):<10} "
              f"${s_bal:>9,.0f}  "
              f"{color}${pnl:>+9,.0f}{RESET}  "
              f"{trades_n:<8} "
              f"{win_r*100:>5.1f}%  "
              f"{str(r.get('started_at',''))[:16]}")
    print()
    sys.exit(0)

# ── Single run ────────────────────────────────────────────────────────────────
runs = bl.get_runs()
if runs is None or runs.empty:
    print("\n  No runs in database.\n")
    sys.exit(0)

run_id = args.run if args.run else int(runs["run_id"].iloc[-1])
run    = runs[runs["run_id"] == run_id]
if run.empty:
    print(f"\n  Run {run_id} not found.\n")
    sys.exit(0)

r = run.iloc[0]
trades = bl.get_trades(run_id=run_id)

print(f"\n  {BOLD}Backtest Run #{run_id} — {r.get('quarter','?')}{RESET}")
print(f"  {'─'*70}")
print(f"  Starting balance:  ${r.get('starting_balance',0):>10,.2f}")
print(f"  Final balance:     ${r.get('final_balance',0):>10,.2f}")

net = r.get('net_pnl', 0) or 0
color = GREEN if net >= 0 else RED
print(f"  Net P&L:           {color}${net:>+10,.2f}{RESET}")
print(f"  Trades:            {int(r.get('total_trades',0)):>10}")
print(f"  Win rate:          {r.get('win_rate',0)*100:>9.1f}%")
print(f"  Max drawdown:      {r.get('max_drawdown_pct',0)*100:>9.1f}%")

if trades is None or trades.empty:
    print(f"\n  No trades found for run {run_id}.\n")
    sys.exit(0)

# ── Trade log ─────────────────────────────────────────────────────────────────
print(f"\n  {BOLD}Trade Log ({len(trades)} trades){RESET}")
print(f"  {'─'*90}")
print(f"  {'#':<4} {'Dir':<6} {'Strategy':<16} {'Entry':>10} {'Exit':>10} "
      f"{'Stop':>10} {'R':>6} {'Net P&L':>10} {'Exit Reason'}")
print(f"  {'─'*90}")

for i, t in trades.iterrows():
    r_val  = t.get('r_achieved') or 0
    pnl    = t.get('net_pnl') or 0
    color  = GREEN if pnl >= 0 else RED
    r_col  = GREEN if r_val >= 0 else RED
    dirn   = t.get('direction','?').upper()[:5]
    strat  = str(t.get('strategy','?'))[:14]
    reason = str(t.get('exit_reason','?'))[:20]

    print(f"  {i+1:<4} {dirn:<6} {strat:<16} "
          f"{t.get('entry_price',0):>10.2f} "
          f"{t.get('exit_price',0) or 0:>10.2f} "
          f"{t.get('stop_price',0):>10.2f} "
          f"{r_col}{r_val:>+6.2f}{RESET} "
          f"{color}${pnl:>+9.2f}{RESET}  "
          f"{reason}")

# ── Strategy breakdown ────────────────────────────────────────────────────────
print(f"\n  {BOLD}By Strategy{RESET}")
print(f"  {'─'*50}")
for strat, grp in trades.groupby("strategy"):
    wins   = (grp["net_pnl"] > 0).sum()
    total  = len(grp)
    pnl    = grp["net_pnl"].sum()
    color  = GREEN if pnl >= 0 else RED
    print(f"  {strat:<20} {total:>3} trades  "
          f"win={wins/total*100:>4.0f}%  "
          f"{color}${pnl:>+8,.2f}{RESET}")

# ── Exit reason breakdown ─────────────────────────────────────────────────────
print(f"\n  {BOLD}By Exit Reason{RESET}")
print(f"  {'─'*50}")
for reason, grp in trades.groupby("exit_reason"):
    pnl   = grp["net_pnl"].sum()
    color = GREEN if pnl >= 0 else RED
    print(f"  {reason:<25} {len(grp):>3} trades  "
          f"{color}${pnl:>+8,.2f}{RESET}")

print()
