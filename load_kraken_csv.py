# load_kraken_csv.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Process Kraken OHLCVT ZIP into quarterly cache CSVs

"""
Extracts XBTUSD 1-minute OHLCVT data from the official Kraken historical
data ZIP, splits it into quarterly cache files matching data_fetcher format,
then deletes the ZIP and raw extracted files to reclaim disk space.

Kraken ZIP structure:
    Kraken_OHLCVT/
        XBTUSD_1.csv     ← 1-minute BTC/USD (what we want)
        XBTUSD_5.csv     ← 5-minute
        XBTUSD_60.csv    ← 1-hour
        ... (all pairs, all timeframes)

Kraken CSV columns (no header):
    timestamp, open, high, low, close, volume, trades

Output: data/cache/BTC_USD_1m_YYYY-QN.csv per quarter
        columns: timestamp, open, high, low, close, volume

Usage:
    python load_kraken_csv.py                    # process ~/Kraken_OHLCVT.zip
    python load_kraken_csv.py --zip ~/my.zip     # specify ZIP path
    python load_kraken_csv.py --no-delete        # keep ZIP after processing
"""

import argparse
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import bt_config as cfg

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
CYAN   = '\033[0;36m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

DEFAULT_ZIP   = Path.home() / "Kraken_OHLCVT.zip"
TARGET_FILE   = "XBTUSD_1.csv"   # 1-minute BTC/USD inside the ZIP


def quarter_label(ts: pd.Timestamp) -> str:
    q = (ts.month - 1) // 3 + 1
    return f"{ts.year}-Q{q}"


def find_target_in_zip(zf: zipfile.ZipFile) -> str | None:
    """Find XBTUSD 1m CSV inside the ZIP — handle nested directories."""
    for name in zf.namelist():
        if Path(name).name == TARGET_FILE:
            return name
    # Fallback: any file matching pattern
    for name in zf.namelist():
        n = Path(name).name.upper()
        if "XBTUSD" in n and n.endswith("_1.CSV"):
            return name
    return None


def main():
    parser = argparse.ArgumentParser(description="Load Kraken OHLCVT ZIP into quarterly cache")
    parser.add_argument("--zip",       type=str, default=str(DEFAULT_ZIP), help="Path to ZIP file")
    parser.add_argument("--no-delete", action="store_true", help="Keep ZIP after processing")
    args = parser.parse_args()

    zip_path = Path(args.zip)

    print(f"\n{BOLD}{CYAN}  Kraken OHLCVT Loader — backtester_engine_v1{RESET}")
    print(f"  {'─'*52}")

    if not zip_path.exists():
        print(f"\n  {YELLOW}⚠  ZIP not found: {zip_path}{RESET}")
        print(f"  Download it first:")
        print(f"    {CYAN}gdown 1ptNqWYidLkhb2VAKuLCxmp2OXEfGO-AP{RESET}\n")
        sys.exit(1)

    zip_size = zip_path.stat().st_size / (1024**3)
    print(f"\n  ZIP: {zip_path}  ({zip_size:.2f}GB)")

    # ── Find target file in ZIP ───────────────────────────────────────────────
    print(f"\n  Scanning ZIP contents for {TARGET_FILE}...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        target = find_target_in_zip(zf)
        if not target:
            print(f"  {YELLOW}⚠  Could not find {TARGET_FILE} in ZIP.{RESET}")
            print(f"  Files in ZIP matching XBTUSD:")
            for n in zf.namelist():
                if "XBTUSD" in n.upper():
                    print(f"    {n}")
            sys.exit(1)

        print(f"  Found: {target}")
        file_size = zf.getinfo(target).file_size / (1024**3)
        print(f"  Uncompressed size: {file_size:.2f}GB")
        print(f"\n  Reading CSV... (this may take 1-2 minutes)")

        # ── Read CSV ──────────────────────────────────────────────────────────
        with zf.open(target) as f:
            df = pd.read_csv(
                f,
                header=None,
                names=["timestamp", "open", "high", "low", "close", "volume", "trades"],
                dtype={
                    "timestamp": "int64",
                    "open":  "float64",
                    "high":  "float64",
                    "low":   "float64",
                    "close": "float64",
                    "volume":"float64",
                    "trades":"int64",
                }
            )

    print(f"  Loaded {len(df):,} rows")
    print(f"  Raw range: {df['timestamp'].min()} → {df['timestamp'].max()}")

    # ── Convert timestamps ────────────────────────────────────────────────────
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp")

    start_dt = df["timestamp"].iloc[0]
    end_dt   = df["timestamp"].iloc[-1]
    print(f"  Date range: {start_dt.date()} → {end_dt.date()}")

    # Keep only columns matching our cache format (drop trades)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]]

    # ── Split into quarters ───────────────────────────────────────────────────
    cfg.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    df["_quarter"] = df["timestamp"].apply(quarter_label)
    quarters = df["_quarter"].unique()
    quarters.sort()

    print(f"\n  Splitting into {len(quarters)} quarters...")
    print(f"  {'─'*52}")

    written = 0
    for label in quarters:
        q_df = df[df["_quarter"] == label].drop(columns=["_quarter"])
        out_path = cfg.CACHE_DIR / f"BTC_USD_1m_{label}.csv"
        q_df.to_csv(out_path, index=False)
        size = out_path.stat().st_size / (1024**2)
        print(f"  {GREEN}✓{RESET}  {label}  {len(q_df):>8,} candles  ({size:.1f}MB)")
        written += 1

    print(f"  {'─'*52}")
    print(f"  {GREEN}✓{RESET}  {written} quarters written to {cfg.CACHE_DIR}")

    # ── Delete ZIP ────────────────────────────────────────────────────────────
    if not args.no_delete:
        print(f"\n  Deleting ZIP to reclaim disk space...")
        zip_path.unlink()
        print(f"  {GREEN}✓{RESET}  Deleted {zip_path}")
    else:
        print(f"\n  {DIM}Keeping ZIP (--no-delete){RESET}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Done.{RESET} Cache ready for backtesting.")
    print(f"\n  Next steps:")
    print(f"    {CYAN}python fetch_data.py --status{RESET}      — verify cache")
    print(f"    {CYAN}python main.py --quarter 2025-Q1{RESET}   — run a backtest\n")


if __name__ == "__main__":
    main()
