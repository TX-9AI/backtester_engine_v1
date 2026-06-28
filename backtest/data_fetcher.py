# backtest/data_fetcher.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Pull and cache Kraken BTC/USD OHLCV in 90-day quarters via CCXT
# v1.1 — 2026-06-28 — Fix: _quarter_bounds() swapped month/day variables causing wrong date range
# v1.2 — 2026-06-28 — Fix: Kraken uses XBT/USD not BTC/USD in CCXT; auto-detect symbol on init
# v1.3 — 2026-06-28 — Fix: probe all three symbol variants with live fetch to confirm OHLCV available
# v1.4 — 2026-06-28 — Fix: Kraken OHLC ignores since for history; use publicGetOHLC with last cursor
#                      Kraken returns max 720 candles per call, paginate via 'last' field in response

"""
Fetches historical BTC/USD 1-minute OHLCV from Kraken via CCXT.
Data is cached locally as CSV per quarter to avoid re-fetching.

Usage:
    from backtest.data_fetcher import DataFetcher
    fetcher = DataFetcher()
    quarters = fetcher.list_available_quarters()
    df = fetcher.load_quarter("2025-Q4")
"""

import os
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import ccxt

logger = logging.getLogger(__name__)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

SYMBOL          = "XBT/USD:BTNL"   # Kraken margin perpetual — what the bot actually trades
SYMBOL_SPOT     = "BTC/USD"        # Kraken spot — fallback for OHLCV history
SYMBOL_XBT_SPOT = "XBT/USD"        # Kraken XBT spot — second fallback
TIMEFRAME       = "1m"
CANDLES_PER_REQ = 720          # Kraken max per request
RATE_LIMIT_SEC  = 1.2          # Stay under Kraken rate limits
CACHE_DIR       = Path("data/cache")
EARLIEST_DATE   = datetime(2020, 1, 1, tzinfo=timezone.utc)   # How far back to go


# ─── QUARTER HELPERS ──────────────────────────────────────────────────────────

def _quarter_bounds(year: int, q: int) -> tuple[datetime, datetime]:
    """Return (start, end) UTC datetimes for a given year/quarter."""
    # (month, day) tuples for quarter start and next-quarter start
    starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    ends   = {1: (4, 1), 2: (7, 1), 3: (10, 1), 4: (1,  1)}
    start_month, start_day = starts[q]
    end_month,   end_day   = ends[q]
    end_year = year if q < 4 else year + 1
    start = datetime(year,     start_month, start_day, tzinfo=timezone.utc)
    end   = datetime(end_year, end_month,   end_day,   tzinfo=timezone.utc) - timedelta(seconds=1)
    return start, end


def _all_quarters() -> list[tuple[int, int]]:
    """Return list of (year, quarter) tuples from EARLIEST_DATE through last completed quarter."""
    now = datetime.now(timezone.utc)
    # Current quarter is incomplete — stop at previous completed quarter
    current_q = (now.month - 1) // 3 + 1
    results = []
    year = EARLIEST_DATE.year
    q    = (EARLIEST_DATE.month - 1) // 3 + 1
    while True:
        if year > now.year:
            break
        if year == now.year and q >= current_q:
            break
        results.append((year, q))
        q += 1
        if q > 4:
            q = 1
            year += 1
    return results


def quarter_label(year: int, q: int) -> str:
    return f"{year}-Q{q}"


# ─── DATA FETCHER ─────────────────────────────────────────────────────────────

class DataFetcher:
    """
    Fetches and caches Kraken BTC/USD 1m OHLCV data by quarter.
    Cache lives at data/cache/BTC_USD_1m_<YYYY>-Q<N>.csv
    """

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.exchange = ccxt.kraken({
            "enableRateLimit": True,
            "timeout": 30000,
        })
        # Auto-detect correct symbol — Kraken uses XBT/USD in CCXT
        self.symbol = self._resolve_symbol()

    # ── Public API ────────────────────────────────────────────────────────────

    def list_available_quarters(self) -> list[str]:
        """Return labels for all quarters from 2020 through last completed quarter."""
        return [quarter_label(y, q) for y, q in _all_quarters()]

    def cache_path(self, label: str) -> Path:
        return self.cache_dir / f"BTC_USD_1m_{label}.csv"

    def is_cached(self, label: str) -> bool:
        p = self.cache_path(label)
        return p.exists() and p.stat().st_size > 1024

    def load_quarter(self, label: str, force_refresh: bool = False) -> pd.DataFrame:
        """
        Load a quarter of 1m OHLCV data. Fetches from Kraken if not cached.
        Returns DataFrame with columns: [timestamp, open, high, low, close, volume]
        timestamp is UTC-aware datetime.
        """
        if self.is_cached(label) and not force_refresh:
            logger.info(f"[DataFetcher] Loading from cache: {label}")
            return self._load_csv(label)

        logger.info(f"[DataFetcher] Fetching from Kraken: {label}")
        year, q = self._parse_label(label)
        start, end = _quarter_bounds(year, q)
        df = self._fetch_range(start, end)
        self._save_csv(df, label)
        logger.info(f"[DataFetcher] Cached {len(df)} candles → {self.cache_path(label)}")
        return df

    def load_date_range(self, start: datetime, end: datetime) -> pd.DataFrame:
        """
        Load arbitrary date range, stitching from cached quarters.
        Any missing quarters are fetched automatically.
        """
        all_q = _all_quarters()
        frames = []
        for year, q in all_q:
            qs, qe = _quarter_bounds(year, q)
            if qe < start or qs > end:
                continue
            label = quarter_label(year, q)
            df = self.load_quarter(label)
            frames.append(df)

        if not frames:
            raise ValueError(f"No data found for range {start} → {end}")

        combined = pd.concat(frames).drop_duplicates(subset="timestamp").sort_values("timestamp")
        mask = (combined["timestamp"] >= start) & (combined["timestamp"] <= end)
        return combined[mask].reset_index(drop=True)

    def fetch_all(self, verbose: bool = True) -> dict[str, pd.DataFrame]:
        """
        Fetch and cache every available quarter. Returns dict of label → DataFrame.
        Useful for a one-time full historical pull.
        """
        quarters = self.list_available_quarters()
        result = {}
        for label in quarters:
            if verbose:
                cached = "✓ cached" if self.is_cached(label) else "fetching..."
                print(f"  {label}  {cached}")
            result[label] = self.load_quarter(label)
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _resolve_symbol(self) -> str:
        """
        Probe Kraken for the correct OHLCV symbol.
        Tries margin pair first, then spot fallbacks.
        Uses a live candle fetch to confirm data is actually available.
        """
        candidates = [SYMBOL, SYMBOL_XBT_SPOT, SYMBOL_SPOT]
        try:
            markets = self.exchange.load_markets()
            # Filter to only symbols that exist in the market
            candidates = [s for s in candidates if s in markets] + \
                         [s for s in candidates if s not in markets]
        except Exception as e:
            logger.warning(f"[DataFetcher] Market load failed: {e}")

        # Probe each symbol with a tiny fetch to confirm OHLCV works
        test_since_sec = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp())
        for sym in candidates:
            try:
                test = self.exchange.fetch_ohlcv(
                    sym, "1m",
                    since=test_since_sec * 1000,
                    limit=2,
                    params={"since": test_since_sec},
                )
                if test:
                    logger.info(f"[DataFetcher] Confirmed symbol: {sym} ({len(test)} candles returned)")
                    return sym
                else:
                    logger.debug(f"[DataFetcher] {sym} returned no data")
            except Exception as e:
                logger.debug(f"[DataFetcher] {sym} probe failed: {e}")

        logger.warning(f"[DataFetcher] All symbol probes failed — using {SYMBOL_SPOT}")
        return SYMBOL_SPOT

    def _fetch_range(self, start: datetime, end: datetime) -> pd.DataFrame:
        """
        Fetch historical OHLCV from Kraken using publicGetOHLC with cursor pagination.

        Key findings from API diagnostic:
        - Symbol: XBTUSD (Kraken internal) mapped from BTC/USD (CCXT)
        - Kraken returns max 720 candles per call
        - Pagination uses the 'last' field in the response as the next 'since'
        - 'since' is in UNIX seconds
        - Candle format: [time, open, high, low, close, vwap, volume, count]
        """
        since_sec = int(start.timestamp())
        end_sec   = int(end.timestamp())
        all_rows  = []
        retries   = 0
        max_retries = 5

        # Kraken internal pair name for BTC/USD
        kraken_pair = "XBTUSD"

        while since_sec < end_sec:
            try:
                response = self.exchange.publicGetOHLC({
                    "pair":     kraken_pair,
                    "interval": 1,           # 1 minute
                    "since":    since_sec,
                })
                retries = 0
            except ccxt.DDoSProtection as e:
                wait = min(30 * (retries + 1), 120)
                logger.warning(f"[DataFetcher] Rate limited — waiting {wait}s")
                time.sleep(wait)
                retries += 1
                if retries >= max_retries:
                    logger.error("[DataFetcher] Max retries hit — stopping")
                    break
                continue
            except ccxt.NetworkError as e:
                logger.warning(f"[DataFetcher] Network error, retrying in 10s: {e}")
                time.sleep(10)
                retries += 1
                if retries >= max_retries:
                    break
                continue
            except ccxt.ExchangeError as e:
                logger.error(f"[DataFetcher] Exchange error: {e}")
                raise

            result = response.get("result", {})
            # Get candle data — key is the pair name (e.g. 'XXBTZUSD')
            candle_key = next((k for k in result if k != "last"), None)
            if not candle_key:
                break

            candles = result[candle_key]
            last_cursor = result.get("last")

            if not candles:
                break

            # Filter to requested date range and convert
            for c in candles:
                ts_sec = int(c[0])
                if ts_sec > end_sec:
                    break
                if ts_sec >= since_sec:
                    all_rows.append({
                        "timestamp_sec": ts_sec,
                        "open":   float(c[1]),
                        "high":   float(c[2]),
                        "low":    float(c[3]),
                        "close":  float(c[4]),
                        "volume": float(c[6]),
                    })

            # Check if we've reached the end of the requested range
            last_candle_sec = int(candles[-1][0])
            if last_candle_sec >= end_sec:
                break

            # Advance using Kraken's last cursor
            if last_cursor and int(last_cursor) > since_sec:
                since_sec = int(last_cursor)
            else:
                # Fallback: advance past last candle
                since_sec = last_candle_sec + 60

            time.sleep(RATE_LIMIT_SEC)

        if not all_rows:
            raise ValueError(f"No data returned from Kraken for range {start} → {end}")

        df = pd.DataFrame(all_rows)
        df["timestamp"] = pd.to_datetime(df["timestamp_sec"], unit="s", utc=True)
        df = df.drop(columns=["timestamp_sec"])
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        df = df[df["timestamp"] <= end]
        df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
        return df

    def _save_csv(self, df: pd.DataFrame, label: str) -> None:
        path = self.cache_path(label)
        df.to_csv(path, index=False)

    def _load_csv(self, label: str) -> pd.DataFrame:
        path = self.cache_path(label)
        df = pd.read_csv(path, parse_dates=["timestamp"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    @staticmethod
    def _parse_label(label: str) -> tuple[int, int]:
        """Parse '2025-Q4' → (2025, 4)"""
        try:
            year_s, q_s = label.split("-Q")
            return int(year_s), int(q_s)
        except Exception:
            raise ValueError(f"Invalid quarter label: '{label}'. Expected format: YYYY-QN")


# ─── CLI ENTRY POINT ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    fetcher = DataFetcher()
    available = fetcher.list_available_quarters()
    print(f"\nAvailable quarters ({len(available)} total):")
    for i, label in enumerate(available, 1):
        cached = "✓" if fetcher.is_cached(label) else "○"
        print(f"  {cached} {label}")

    if len(sys.argv) > 1:
        label = sys.argv[1].upper()
        print(f"\nFetching {label}...")
        df = fetcher.load_quarter(label)
        print(f"Loaded {len(df):,} candles")
        print(f"Range: {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")
        print(df.head())
    else:
        print("\nTo fetch a quarter: python -m backtest.data_fetcher 2025-Q3")
        print("To fetch all:       python -m backtest.data_fetcher all")
