"""
data/market_data.py — Fetches OHLCV data from Kraken via ccxt.
Handles all timeframes, rate limiting, and DataFrame construction.
"""

import logging
import time
from typing import Dict, Optional
import pandas as pd
import ccxt

from config import (
    KRAKEN_API_KEY, KRAKEN_API_SECRET,
    TRADING_SYMBOL, TIMEFRAMES
)

logger = logging.getLogger(__name__)


def _build_exchange() -> ccxt.kraken:
    """Instantiate and configure the Kraken exchange object."""
    exchange = ccxt.kraken({
        "apiKey":               KRAKEN_API_KEY,
        "secret":               KRAKEN_API_SECRET,
        "enableRateLimit":      True,
        "options": {
            "defaultType": "spot",   # Use margin via order params
        }
    })
    return exchange


# Module-level exchange singleton
_exchange: Optional[ccxt.kraken] = None


def get_exchange() -> ccxt.kraken:
    """Return the shared Kraken exchange instance."""
    global _exchange
    if _exchange is None:
        _exchange = _build_exchange()
        logger.info("Kraken exchange initialized.")
    return _exchange


def fetch_ohlcv(timeframe: str, limit: int = 100,
                retries: int = 3) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles from Kraken for the given timeframe.

    Args:
        timeframe: ccxt timeframe string e.g. "5m", "1h", "1d"
        limit:     Number of candles to fetch
        retries:   Retry attempts on network error

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
        Returns None on failure.
    """
    exchange = get_exchange()

    for attempt in range(retries):
        try:
            raw = exchange.fetch_ohlcv(
                TRADING_SYMBOL,
                timeframe=timeframe,
                limit=limit
            )
            if not raw:
                logger.warning(f"Empty OHLCV response for {timeframe}")
                return None

            df = pd.DataFrame(raw, columns=[
                "timestamp", "open", "high", "low", "close", "volume"
            ])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)

            logger.debug(f"Fetched {len(df)} candles [{timeframe}] "
                         f"last={df.index[-1].strftime('%H:%M')} "
                         f"close={df['close'].iloc[-1]:.2f}")
            return df

        except ccxt.NetworkError as e:
            logger.warning(f"Network error [{timeframe}] attempt {attempt+1}: {e}")
            time.sleep(2 ** attempt)

        except ccxt.ExchangeError as e:
            logger.error(f"Exchange error [{timeframe}]: {e}")
            return None

        except Exception as e:
            logger.error(f"Unexpected error fetching {timeframe}: {e}")
            return None

    logger.error(f"Failed to fetch {timeframe} after {retries} attempts.")
    return None


def fetch_all_timeframes() -> Dict[str, Optional[pd.DataFrame]]:
    """
    Fetch all configured timeframes.
    Returns dict keyed by timeframe string.
    """
    results = {}
    for tf, cfg in TIMEFRAMES.items():
        df = fetch_ohlcv(tf, limit=cfg["candles"])
        results[tf] = df
        if df is not None:
            logger.debug(f"[{tf}] ✓ {len(df)} candles")
        else:
            logger.warning(f"[{tf}] ✗ fetch failed")
    return results


def get_current_price() -> Optional[float]:
    """
    Fetch the current best bid/ask midpoint from the order book.
    Faster and more accurate than last trade price.
    """
    try:
        exchange = get_exchange()
        ticker = exchange.fetch_ticker(TRADING_SYMBOL)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask:
            return (bid + ask) / 2
        return ticker.get("last")
    except Exception as e:
        logger.error(f"Error fetching current price: {e}")
        return None


def get_ticker() -> Optional[dict]:
    """Full ticker data including 24h volume, high, low."""
    try:
        exchange = get_exchange()
        return exchange.fetch_ticker(TRADING_SYMBOL)
    except Exception as e:
        logger.error(f"Error fetching ticker: {e}")
        return None


def get_order_book(depth: int = 10) -> Optional[dict]:
    """
    Fetch order book. Used for entry precision and
    liquidity assessment near key levels.
    """
    try:
        exchange = get_exchange()
        return exchange.fetch_order_book(TRADING_SYMBOL, limit=depth)
    except Exception as e:
        logger.error(f"Error fetching order book: {e}")
        return None


def get_account_balance() -> Optional[dict]:
    """
    Fetch current account balance from Kraken.
    Returns dict with 'total', 'free', 'used' for USD and BTC.
    """
    try:
        exchange = get_exchange()
        balance = exchange.fetch_balance()
        return {
            "USD": {
                "total": balance.get("USD", {}).get("total", 0),
                "free":  balance.get("USD", {}).get("free",  0),
                "used":  balance.get("USD", {}).get("used",  0),
            },
            "BTC": {
                "total": balance.get("BTC", {}).get("total", 0),
                "free":  balance.get("BTC", {}).get("free",  0),
                "used":  balance.get("BTC", {}).get("used",  0),
            }
        }
    except Exception as e:
        logger.error(f"Error fetching balance: {e}")
        return None
