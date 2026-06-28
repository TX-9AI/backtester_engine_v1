"""
data/data_cache.py — In-memory OHLCV cache with staleness management.
Prevents hammering the Kraken API on every 10-second poll.
Each timeframe has its own TTL based on candle duration.
"""

import logging
from datetime import datetime
from typing import Dict, Optional
import pandas as pd

from config import CACHE_STALENESS_SECONDS, TIMEFRAMES
from data.market_data import fetch_ohlcv, get_current_price
from utils.time_utils import now_utc

logger = logging.getLogger(__name__)


class DataCache:
    """
    Manages cached OHLCV DataFrames per timeframe.
    Fetches only when data is stale. Thread-safe for single process.
    """

    def __init__(self):
        self._cache: Dict[str, pd.DataFrame]  = {}
        self._last_fetch: Dict[str, datetime] = {}
        self._current_price: Optional[float]  = None
        self._price_fetched: Optional[datetime] = None

    def is_stale(self, timeframe: str) -> bool:
        """True if the cached data for this TF needs a refresh."""
        if timeframe not in self._last_fetch:
            return True
        ttl = CACHE_STALENESS_SECONDS.get(timeframe, 60)
        elapsed = (now_utc() - self._last_fetch[timeframe]).total_seconds()
        return elapsed >= ttl

    def get(self, timeframe: str, force: bool = False) -> Optional[pd.DataFrame]:
        """
        Return cached DataFrame for timeframe, fetching fresh if stale.

        Args:
            timeframe: e.g. "5m", "1h"
            force:     bypass cache and fetch fresh

        Returns:
            DataFrame or None if fetch failed
        """
        if force or self.is_stale(timeframe):
            cfg = TIMEFRAMES.get(timeframe, {})
            limit = cfg.get("candles", 100)
            df = fetch_ohlcv(timeframe, limit=limit)
            if df is not None:
                self._cache[timeframe] = df
                self._last_fetch[timeframe] = now_utc()
                logger.debug(f"Cache refreshed [{timeframe}]")
            else:
                logger.warning(f"Cache refresh failed [{timeframe}], using stale data")
                return self._cache.get(timeframe)

        return self._cache.get(timeframe)

    def get_all(self, force: bool = False) -> Dict[str, Optional[pd.DataFrame]]:
        """
        Return all timeframes, refreshing stale ones.
        Only fetches TFs that have exceeded their TTL.
        """
        result = {}
        for tf in TIMEFRAMES:
            result[tf] = self.get(tf, force=force)
        return result

    def get_price(self) -> Optional[float]:
        """
        Return current price, refreshing if older than 10 seconds.
        This is the hot path — called every poll tick.
        """
        if (self._price_fetched is None or
                (now_utc() - self._price_fetched).total_seconds() >= 10):
            price = get_current_price()
            if price is not None:
                self._current_price = price
                self._price_fetched = now_utc()
        return self._current_price

    def invalidate(self, timeframe: Optional[str] = None):
        """
        Force invalidate cache for one TF or all.
        Call after a trade entry to ensure fresh data on next poll.
        """
        if timeframe:
            self._last_fetch.pop(timeframe, None)
            logger.debug(f"Cache invalidated [{timeframe}]")
        else:
            self._last_fetch.clear()
            logger.debug("All cache invalidated")

    def age_seconds(self, timeframe: str) -> float:
        """How old is the cached data for this timeframe?"""
        if timeframe not in self._last_fetch:
            return float("inf")
        return (now_utc() - self._last_fetch[timeframe]).total_seconds()

    def status_report(self) -> str:
        """Human-readable cache status for logging."""
        lines = ["Cache status:"]
        for tf in TIMEFRAMES:
            age = self.age_seconds(tf)
            ttl = CACHE_STALENESS_SECONDS.get(tf, 60)
            stale = "STALE" if age >= ttl else "fresh"
            candles = len(self._cache[tf]) if tf in self._cache else 0
            lines.append(f"  [{tf}] {stale} | age={age:.0f}s | candles={candles}")
        return "\n".join(lines)


# Module-level singleton
_cache: Optional[DataCache] = None


def get_cache() -> DataCache:
    """Return the shared DataCache singleton."""
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache
