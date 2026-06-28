"""
data/macro_data.py — Macro context: VIX, DXY, 10Y yield.
Uses Yahoo Finance (yfinance) as a free data source.
Fetched every 60 minutes — not real-time, but sufficient for regime context.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from utils.time_utils import now_utc

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    logger.warning("yfinance not installed — macro data disabled. "
                   "Run: pip install yfinance")


@dataclass
class MacroSnapshot:
    """Point-in-time macro market context."""
    vix:            Optional[float] = None   # CBOE Volatility Index
    dxy:            Optional[float] = None   # US Dollar Index
    yield_10y:      Optional[float] = None   # 10-Year Treasury yield %
    btc_dominance:  Optional[float] = None   # BTC market cap dominance %

    vix_regime:     str = "UNKNOWN"          # LOW / NORMAL / ELEVATED / CRISIS
    macro_context:  str = "NEUTRAL"          # RISK_ON / RISK_OFF / NEUTRAL

    fetched_at: Optional[datetime] = None

    @property
    def age_minutes(self) -> float:
        if self.fetched_at is None:
            return float("inf")
        return (now_utc() - self.fetched_at).total_seconds() / 60


def _classify_vix(vix: Optional[float]) -> str:
    """Bin VIX into regime label."""
    if vix is None:
        return "UNKNOWN"
    if vix < 15:
        return "LOW"
    if vix < 25:
        return "NORMAL"
    if vix < 35:
        return "ELEVATED"
    return "CRISIS"


def _classify_macro(vix: Optional[float], dxy: Optional[float]) -> str:
    """
    Simple macro context classifier.
    Risk-on = low VIX + weak/stable dollar.
    Risk-off = high VIX + strong dollar (flight to safety).
    """
    if vix is None:
        return "NEUTRAL"

    if vix < 18:
        if dxy is not None and dxy < 104:
            return "RISK_ON"
        return "RISK_ON"  # low vol = risk on even with moderate dollar

    if vix > 28:
        return "RISK_OFF"

    return "NEUTRAL"


def fetch_macro_snapshot() -> MacroSnapshot:
    """
    Fetch current macro data from Yahoo Finance.
    Returns a MacroSnapshot with all available fields populated.
    Returns empty snapshot if yfinance unavailable or API fails.
    """
    snapshot = MacroSnapshot(fetched_at=now_utc())

    if not YFINANCE_AVAILABLE:
        logger.warning("Macro data unavailable — yfinance not installed.")
        return snapshot

    # Symbols: ^VIX = CBOE VIX, DX-Y.NYB = DXY, ^TNX = 10Y yield
    symbols = {
        "vix":       "^VIX",
        "dxy":       "DX-Y.NYB",
        "yield_10y": "^TNX",
    }

    for field, symbol in symbols.items():
        try:
            ticker = yf.Ticker(symbol)
            hist   = ticker.history(period="1d", interval="1m")
            if hist.empty:
                logger.warning(f"No data for {symbol}")
                continue
            value = float(hist["Close"].iloc[-1])
            setattr(snapshot, field, value)
            logger.debug(f"Macro [{symbol}] = {value:.4f}")
        except Exception as e:
            logger.warning(f"Failed to fetch {symbol}: {e}")

    snapshot.vix_regime    = _classify_vix(snapshot.vix)
    snapshot.macro_context = _classify_macro(snapshot.vix, snapshot.dxy)

    logger.info(f"Macro: VIX={snapshot.vix} ({snapshot.vix_regime}) "
                f"DXY={snapshot.dxy} ctx={snapshot.macro_context}")
    return snapshot


class MacroDataManager:
    """
    Manages macro snapshot with TTL-based refresh.
    Macro data is only fetched every 60 minutes.
    """

    def __init__(self, refresh_minutes: int = 60):
        self.refresh_minutes = refresh_minutes
        self._snapshot: Optional[MacroSnapshot] = None

    def get(self, force: bool = False) -> MacroSnapshot:
        """
        Return current macro snapshot, refreshing if stale.
        """
        if (force or self._snapshot is None or
                self._snapshot.age_minutes >= self.refresh_minutes):
            logger.info("Refreshing macro data...")
            self._snapshot = fetch_macro_snapshot()
        return self._snapshot

    @property
    def vix(self) -> Optional[float]:
        return self._snapshot.vix if self._snapshot else None

    @property
    def vix_regime(self) -> str:
        return self._snapshot.vix_regime if self._snapshot else "UNKNOWN"

    @property
    def macro_context(self) -> str:
        return self._snapshot.macro_context if self._snapshot else "NEUTRAL"

    def is_crisis(self) -> bool:
        """True if VIX is in crisis mode — tighten long bias."""
        return self.vix_regime == "CRISIS"

    def is_risk_off(self) -> bool:
        """True if macro context is risk-off."""
        return self.macro_context == "RISK_OFF"


# Module-level singleton
_macro_manager: Optional[MacroDataManager] = None


def get_macro_manager() -> MacroDataManager:
    """Return the shared MacroDataManager singleton."""
    global _macro_manager
    if _macro_manager is None:
        _macro_manager = MacroDataManager()
    return _macro_manager
