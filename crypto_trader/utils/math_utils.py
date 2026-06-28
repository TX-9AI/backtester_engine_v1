"""
utils/math_utils.py — Mathematical helpers used across the bot.
Pure functions, no side effects.
"""

import math
import statistics
from typing import List, Optional, Tuple
import pandas as pd
import numpy as np


def round_price(price: float, tick_size: float = 0.5) -> float:
    return round(round(price / tick_size) * tick_size, 2)


def round_size(size: float, lot_size: float = 0.0001) -> float:
    factor = 1 / lot_size
    return math.floor(size * factor) / factor


def pct_diff(a: float, b: float) -> float:
    if a == 0:
        return 0.0
    return (b - a) / a


def within_pct(a: float, b: float, pct: float) -> bool:
    return abs(pct_diff(a, b)) <= pct


def calculate_atr(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        ranges = [h - l for h, l in zip(highs, lows)]
        return statistics.mean(ranges[-period:]) if ranges else 0.0
    true_ranges = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)
    atr = statistics.mean(true_ranges[:period])
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def ema_series(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def ema_value(closes, period):
    s = pd.Series(closes)
    return float(ema_series(s, period).iloc[-1])


def bollinger_bands(closes, period=20, std_dev=2.0):
    middle = closes.rolling(period).mean()
    std    = closes.rolling(period).std()
    upper  = middle + std_dev * std
    lower  = middle - std_dev * std
    return upper, middle, lower


def bb_width(upper, lower, middle):
    if middle == 0:
        return 0.0
    return (upper - lower) / middle


def bb_width_percentile(widths, current, lookback=20):
    recent = widths[-lookback:] if len(widths) >= lookback else widths
    if not recent:
        return 0.5
    below = sum(1 for w in recent if w <= current)
    return below / len(recent)


def adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0]   = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm > 0) & (plus_dm <= minus_dm)]   = 0
    minus_dm[(minus_dm > 0) & (minus_dm <= plus_dm)] = 0
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr_s    = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr_s
    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return adx


def current_adx(df: pd.DataFrame, period: int = 14) -> float:
    series = adx_series(df, period)
    return float(series.iloc[-1]) if not series.empty else 0.0


def vwap_series(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).cumsum() / df["volume"].cumsum()


def current_vwap(df: pd.DataFrame) -> float:
    series = vwap_series(df)
    return float(series.iloc[-1]) if not series.empty else 0.0


def classic_pivots(high, low, close):
    pp = (high + low + close) / 3
    return {
        "PP": pp,
        "R1": 2 * pp - low,
        "R2": pp + (high - low),
        "R3": high + 2 * (pp - low),
        "S1": 2 * pp - high,
        "S2": pp - (high - low),
        "S3": low - 2 * (high - pp),
    }


def find_swing_highs(highs, lookback=10):
    swings = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        window = highs[i - lookback: i + lookback + 1]
        if highs[i] == max(window):
            swings.append((i, highs[i]))
    return swings


def find_swing_lows(lows, lookback=10):
    swings = []
    n = len(lows)
    for i in range(lookback, n - lookback):
        window = lows[i - lookback: i + lookback + 1]
        if lows[i] == min(window):
            swings.append((i, lows[i]))
    return swings


def r_multiple(entry, current, stop, direction):
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    if direction == "long":
        return (current - entry) / risk
    else:
        return (entry - current) / risk


def dollar_risk(entry, stop, size, direction):
    if direction == "long":
        return max(0, (entry - stop) * size)
    else:
        return max(0, (stop - entry) * size)


def unrealized_pnl(entry, current, size, direction):
    if direction == "long":
        return (current - entry) * size
    else:
        return (entry - current) * size
