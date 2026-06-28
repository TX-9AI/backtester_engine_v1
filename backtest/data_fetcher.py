# data_fetcher.py — btc_backtester
# v1.0 — 2026-06-28 — Pull and cache Kraken BTC/USD OHLCV in 90-day quarters via CCXT

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

SYMBOL          = "BTC/USD"
TIMEFRAME       = "1m"
CANDLES_PER_REQ = 720          # Kraken max per request
RATE_LIMIT_SEC  = 1.2          # Stay under Kraken rate limits
CACHE_DIR       = Path("data/cache")
EARLIEST_DATE   = datetime(2020, 1, 1, tzinfo=timezone.utc)   # How far back to go


# ─── QUARTER HELPERS ──────────────────────────────────────────────────────────

def _quarter_bounds(year: int, q: int) -> tuple[datetime, datetime]:
    """Return (start, end) UTC datetimes for a given year/quarter."""
    starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    ends   = {1: (4, 1), 2: (7, 1), 3: (10, 1), 4: (1, 1)}
    sy, sm = starts[q]
    ey, em = ends[q]
    ey_actual = year if q < 4 else year + 1
    start = datetime(year, sm, sy, tzinfo=timezone.utc)
    end   = datetime(ey_actual, em, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
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

    def _fetch_range(self, start: datetime, end: datetime) -> pd.DataFrame:
        """Paginate through Kraken OHLCV requests to cover a full date range."""
        since_ms  = int(start.timestamp() * 1000)
        end_ms    = int(end.timestamp() * 1000)
        all_rows  = []

        while since_ms < end_ms:
            try:
                raw = self.exchange.fetch_ohlcv(
                    SYMBOL,
                    timeframe=TIMEFRAME,
                    since=since_ms,
                    limit=CANDLES_PER_REQ,
                )
            except ccxt.NetworkError as e:
                logger.warning(f"[DataFetcher] Network error, retrying in 5s: {e}")
                time.sleep(5)
                continue
            except ccxt.ExchangeError as e:
                logger.error(f"[DataFetcher] Exchange error: {e}")
                raise

            if not raw:
                break

            all_rows.extend(raw)
            last_ts = raw[-1][0]

            if last_ts >= end_ms or len(raw) < CANDLES_PER_REQ:
                break

            since_ms = last_ts + 60_000   # advance by one candle
            time.sleep(RATE_LIMIT_SEC)

        if not all_rows:
            raise ValueError(f"No data returned from Kraken for range {start} → {end}")

        df = pd.DataFrame(all_rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
        df = df.drop(columns=["timestamp_ms"])
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
