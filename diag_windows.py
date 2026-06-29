# diag_windows.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Diagnostic: inspect candle buffer window columns after warmup

"""
Loads 2025-Q1 cache, feeds 500 candles through CandleBuffer,
then prints the columns and sample rows of each timeframe window.
This tells us exactly what columns the strategy stack receives.

Usage:
    python diag_windows.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import bt_config as cfg
from backtest.data_fetcher import DataFetcher
from backtest.replay import CandleBuffer

print("\n=== Window Column Diagnostic ===\n")

# Load data
fetcher = DataFetcher(cfg.CACHE_DIR)
df = fetcher.load_quarter("2025-Q1")
print(f"Loaded {len(df):,} candles")
print(f"Cache columns: {df.columns.tolist()}\n")

# Feed candles into buffer
buf = CandleBuffer()
for i, (_, row) in enumerate(df.iterrows()):
    buf.push(row.to_dict())
    if i >= 499:
        break

print(f"Fed 500 candles into buffer\n")

# Print each window
for tf in ["1m", "5m", "15m", "1h", "1d"]:
    w = buf.windows.get(tf)
    if w is None or w.empty:
        print(f"  {tf}: EMPTY")
    else:
        print(f"  {tf}: {len(w)} rows | columns: {w.columns.tolist()}")
        print(f"       first row: {w.iloc[0].to_dict()}")
    print()

print("=== Done ===\n")
