"""
risk/session_guard.py — Session guard and pre-trade safety checks.
Enforces trading hours (24/7 with optional blackout windows).
"""

import logging
from dataclasses import dataclass
from typing import List, Tuple, Optional

from config import TRADING_24_7, BLACKOUT_WINDOWS
from utils.time_utils import is_blackout, current_session, fmt_et_short

logger = logging.getLogger(__name__)


@dataclass
class SessionCheckResult:
    """Result of session guard check."""
    can_trade:    bool  = True
    reason:       str   = ""
    session_name: str   = ""
    quality:      float = 1.0


class SessionGuard:
    """
    Enforces trading session rules.
    In 24/7 mode (default): always allows trading unless in a blackout window.
    """

    def __init__(self,
                 trading_24_7:       bool             = TRADING_24_7,
                 blackout_windows:   List[Tuple]      = None):
        self.trading_24_7     = trading_24_7
        self.blackout_windows = blackout_windows or BLACKOUT_WINDOWS

    def check(self) -> SessionCheckResult:
        """
        Returns whether trading is permitted right now.
        """
        session_name, quality = current_session()
        result = SessionCheckResult(
            session_name=session_name,
            quality=quality
        )

        # Check blackout windows first (regardless of 24/7 mode)
        if self.blackout_windows and is_blackout(self.blackout_windows):
            result.can_trade = False
            result.reason    = f"In blackout window @ {fmt_et_short()}"
            logger.info(f"SessionGuard: BLOCKED — {result.reason}")
            return result

        # 24/7 mode: always open
        if self.trading_24_7:
            result.can_trade = True
            result.reason    = f"24/7 mode — {session_name} session (quality={quality:.0%})"
            return result

        # Should never reach here with 24/7 enabled
        result.can_trade = True
        return result

    def can_trade(self) -> bool:
        """Quick boolean check."""
        return self.check().can_trade

    def session_quality(self) -> float:
        """Current session quality score (used for position sizing)."""
        return self.check().quality


# Singleton
_guard: Optional[SessionGuard] = None


def get_session_guard() -> SessionGuard:
    global _guard
    if _guard is None:
        _guard = SessionGuard()
    return _guard
