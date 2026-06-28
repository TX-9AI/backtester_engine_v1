# fetch_data.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Standalone data fetcher: pull and cache all Kraken 1m OHLCV quarters
# v1.1 — 2026-06-28 — Add inter-quarter delay to avoid Kraken rate limiting

"""
Run once to populate the local data cache with historical BTC/USD 1m candles.
Subsequent backtests load instantly from CSV — no network calls.

Usage:
    python fetch_data.py                    # Fetch all missing quarters
    python fetch_data.py --quarter 2025-Q1  # Fetch one specific quarter
    python fetch_data.py --force            # Re-fetch all (overwrite cache)
    python fetch_data.py --from 2024-Q1     # Fetch from a quarter onwards
    python fetch_data.py --status           # Show cache status only, no fetch
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bt_config as cfg
from backtest.data_fetcher import DataFetcher

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = '\033[0;32m'
YELLOW = '\033[1;33m'
CYAN   = '\033[0;36m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def fmt_size(path: Path) -> str:
    """Human-readable file size."""
    try:
        b = path.stat().st_size
        for unit in ["B", "KB", "MB", "GB"]:
            if b < 1024:
                return f"{b:.1f}{unit}"
            b /= 1024
        return f"{b:.1f}GB"
    except Exception:
        return "?"


def print_status(fetcher: DataFetcher, available: list[str]) -> None:
    cached   = [q for q in available if fetcher.is_cached(q)]
    uncached = [q for q in available if not fetcher.is_cached(q)]
    total_size = sum(
        fetcher.cache_path(q).stat().st_size
        for q in cached
        if fetcher.cache_path(q).exists()
    )

    print(f"\n{BOLD}  Cache Status{RESET}")
    print(f"  {'─'*50}")
    print(f"  Total quarters available:  {len(available)}")
    print(f"  Cached:                    {GREEN}{len(cached)}{RESET}")
    print(f"  Not cached:                {YELLOW}{len(uncached)}{RESET}")
    if total_size > 0:
        sz = total_size
        for unit in ["B", "KB", "MB", "GB"]:
            if sz < 1024:
                print(f"  Cache size on disk:        {sz:.1f}{unit}")
                break
            sz /= 1024

    print(f"\n  {'Quarter':<12} {'Status':<12} {'Size':<10} {'Candles'}")
    print(f"  {'─'*50}")
    for q in available:
        p = fetcher.cache_path(q)
        if fetcher.is_cached(q):
            # Quick row count
            try:
                with open(p) as f:
                    rows = sum(1 for _ in f) - 1  # subtract header
            except Exception:
                rows = 0
            print(f"  {q:<12} {GREEN}✓ cached{RESET:<12} {fmt_size(p):<10} {rows:,}")
        else:
            print(f"  {q:<12} {DIM}○ missing{RESET}")
    print()


def main():
    parser = argparse.ArgumentParser(description="BTC Backtester — Data Fetcher")
    parser.add_argument("--quarter",  type=str,   help="Fetch single quarter e.g. 2025-Q1")
    parser.add_argument("--from",     type=str,   dest="from_quarter",
                        help="Fetch from this quarter onwards e.g. 2024-Q1")
    parser.add_argument("--force",    action="store_true", help="Re-fetch even if cached")
    parser.add_argument("--status",   action="store_true", help="Show cache status only")
    args = parser.parse_args()

    fetcher   = DataFetcher(cfg.CACHE_DIR)
    available = fetcher.list_available_quarters()

    print(f"\n{BOLD}{CYAN}  BTC Backtester — Data Cache{RESET}")
    print(f"  Symbol: {fetcher.symbol}  |  Timeframe: 1m  |  Source: Kraken")
    print(f"  Cache: {cfg.CACHE_DIR}")

    if args.status:
        print_status(fetcher, available)
        return

    # Determine which quarters to fetch
    if args.quarter:
        q = args.quarter.upper()
        if q not in available:
            print(f"\n  ⚠  Quarter '{q}' not in available range.")
            print(f"  Available: {available[0]} → {available[-1]}")
            sys.exit(1)
        to_fetch = [q]

    elif args.from_quarter:
        q = args.from_quarter.upper()
        if q not in available:
            print(f"\n  ⚠  Quarter '{q}' not found.")
            sys.exit(1)
        idx = available.index(q)
        to_fetch = available[idx:]

    else:
        # Default: fetch all missing quarters
        to_fetch = available if args.force else [
            q for q in available if not fetcher.is_cached(q)
        ]

    if not to_fetch:
        print(f"\n  {GREEN}✓ All quarters already cached.{RESET}")
        print(f"  Use --force to re-fetch, or --status to inspect cache.\n")
        return

    already = len(available) - len(to_fetch) if not args.force else 0
    print(f"\n  Fetching {len(to_fetch)} quarter(s)"
          f"{f'  ({already} already cached)' if already else ''}")
    print(f"  {'─'*50}")

    success = 0
    failed  = 0
    t_start = time.time()

    for i, label in enumerate(to_fetch, 1):
        print(f"\n  [{i}/{len(to_fetch)}] {label} ", end="", flush=True)
        try:
            df = fetcher.load_quarter(label, force_refresh=args.force)
            if df.empty:
                print(f"{YELLOW}⚠  0 candles returned{RESET}")
                failed += 1
            else:
                sz = fmt_size(fetcher.cache_path(label))
                print(f"{GREEN}✓  {len(df):,} candles  ({sz}){RESET}")
                print(f"     {DIM}{df['timestamp'].iloc[0].date()} → "
                      f"{df['timestamp'].iloc[-1].date()}{RESET}")
                success += 1
        except Exception as e:
            print(f"{YELLOW}✗  {e}{RESET}")
            failed += 1
            # Longer pause after failure before next quarter
            time.sleep(10)

    elapsed = time.time() - t_start
    print(f"\n  {'─'*50}")
    print(f"  Done in {elapsed:.0f}s  |  "
          f"{GREEN}{success} succeeded{RESET}  |  "
          f"{YELLOW if failed else ''}{failed} failed{RESET if failed else ''}")
    print()

    if success > 0:
        print(f"  Data ready for backtesting:")
        print(f"    python main.py --all-quarters --balance 10000")
        print(f"    python main.py --quarter 2025-Q1 --balance 10000\n")


if __name__ == "__main__":
    main()
