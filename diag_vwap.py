# diag_vwap.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Diagnostic: trace VWAP calculation through vol_engine

"""
Feeds 200 candles through the full vol_engine pipeline and checks
whether VWAP is computing correctly with DatetimeIndex.

Usage:
    python diag_vwap.py
"""

import sys
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "crypto_trader"))

import bt_config as cfg
from backtest.data_fetcher import DataFetcher
from backtest.replay import CandleBuffer
from analysis.volatility_engine import get_volatility_engine

print("\n=== VWAP Diagnostic ===\n")

fetcher = DataFetcher(cfg.CACHE_DIR)
df      = fetcher.load_quarter("2025-Q1")
buf     = CandleBuffer()
vol     = get_volatility_engine()

print(f"Cache columns: {df.columns.tolist()}")
print(f"Volume sample: {df['volume'].head(5).tolist()}")
print(f"Volume zeros in first 500: {(df['volume'].head(500) == 0).sum()}")
print()

# Feed 300 candles
for i, (_, row) in enumerate(df.iterrows()):
    buf.push(row.to_dict())
    if i >= 299:
        break

df_5m = buf.windows.get("5m", pd.DataFrame())
df_1h = buf.windows.get("1h", pd.DataFrame())

print(f"df_5m rows: {len(df_5m)}  columns: {df_5m.columns.tolist()}")
print(f"df_1h rows: {len(df_1h)}")
print(f"df_5m index type: {type(df_5m.index)}")
print()

# Set DatetimeIndex as strategy_bundle does
df_5m_idx = df_5m.copy()
if "timestamp" in df_5m_idx.columns:
    df_5m_idx["timestamp"] = pd.to_datetime(df_5m_idx["timestamp"], utc=True)
    df_5m_idx = df_5m_idx.set_index("timestamp")

df_1h_idx = df_1h.copy()
if "timestamp" in df_1h_idx.columns:
    df_1h_idx["timestamp"] = pd.to_datetime(df_1h_idx["timestamp"], utc=True)
    df_1h_idx = df_1h_idx.set_index("timestamp")

print(f"After set_index — df_5m index type: {type(df_5m_idx.index)}")
print(f"df_5m index tz: {df_5m_idx.index.tz}")
print(f"df_5m first ts: {df_5m_idx.index[0]}")
print(f"df_5m volume sum: {df_5m_idx['volume'].sum():.4f}")
print()

# Run vol_engine
current_price = float(df_5m_idx["close"].iloc[-1])
state = vol.analyze(df_5m=df_5m_idx, df_1h=df_1h_idx, current_price=current_price)

print(f"current_price:   {current_price:.2f}")
print(f"vol_state.vwap:  {state.vwap:.2f}")
print(f"price_vs_vwap:   {state.price_vs_vwap}")
print(f"atr_current:     {state.atr_current:.2f}")
print(f"bb_state:        {state.bb_state}")
print()

# Manually compute VWAP to verify

today_start = pd.Timestamp.now(tz=df_5m_idx.index.tz).normalize()
df_today = df_5m_idx[df_5m_idx.index >= today_start]
print(f"today_start:     {today_start}")
print(f"df_today rows:   {len(df_today)}  (should be 0 for 2025 data)")
df_used = df_today if len(df_today) >= 3 else df_5m_idx
print(f"df_used rows:    {len(df_used)}  (fallback to full df)")
typical = (df_used["high"] + df_used["low"] + df_used["close"]) / 3
vwap_manual = float((typical * df_used["volume"]).sum() / df_used["volume"].sum()) if df_used["volume"].sum() > 0 else 0
print(f"manual VWAP:     {vwap_manual:.2f}")
print(f"volume sum:      {df_used['volume'].sum():.6f}")

print("\n=== Done ===\n")
