"""
risk/risk_manager.py — v6.0 Fee-aware auto-sizing risk manager.

Key changes from v5.0:
  - No circuit breaker — fee floor gate prevents unprofitable trades instead
  - Position size auto-calculated from grade × buying power (no user input)
  - Fee estimation baked into every entry decision
  - validate_fee_floor() rejects trades where 1R profit won't clear fees
  - Paper mode uses configured cash balance × leverage for sizing
  - Live mode fetches real Kraken balance before sizing

Fee model (Kraken base tier, market orders):
  Open:     taker fee (0.80%) × notional  ← spot trade fee
  Margin:   margin open fee (0.02%) × notional
  Close:    taker fee (0.80%) × notional
  Rollover: 0.02% × notional per 4-hour window held
"""

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta

from config import (
    ACCOUNT_BALANCE_USD, LEVERAGE, PAPER_TRADING,
    GRADE_A_NOTIONAL_PCT, GRADE_B_NOTIONAL_PCT, GRADE_C_NOTIONAL_PCT,
    KRAKEN_TAKER_FEE, KRAKEN_MARGIN_OPEN_FEE, KRAKEN_ROLLOVER_FEE,
    KRAKEN_ROLLOVER_HOURS, MIN_FEE_ADJUSTED_R,
    MIN_ORDER_SIZE_BTC, TRADE_GRADE_C, MAX_OPEN_RISK_PCT,
    INSTRUMENT, SessionConfig
)
from database.trade_logger import get_trade_logger
from utils.time_utils import now_utc, fmt_et_short
from utils.math_utils import round_size

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    size_btc:        float = 0.0
    notional_usd:    float = 0.0
    risk_usd:        float = 0.0
    margin_used:     float = 0.0
    risk_pct:        float = 0.0
    estimated_fees:  float = 0.0   # Round-trip fee estimate at entry
    fee_adjusted_r:  float = 0.0   # 1R profit / estimated fees ratio
    allowed:         bool  = True
    reject_reason:   str   = ""


class RiskManager:
    """
    v6.0 fee-aware risk manager.

    Sizing logic:
      buying_power = cash_balance × leverage
      notional     = buying_power × grade_notional_pct
      size_btc     = notional / entry_price

    Fee gate:
      estimated_fees = open_fee + close_fee + margin_fee + rollover_estimate
      If (stop_distance × size_btc) <= estimated_fees → reject trade
      "You can't make money if 1R doesn't even cover fees"

    No circuit breaker — the fee floor gate naturally prevents
    high-frequency unprofitable scalping in compressed markets.
    """

    def __init__(self, session_config: Optional[SessionConfig] = None,
                 cash_balance: float = ACCOUNT_BALANCE_USD):
        self._session         = session_config
        self._cash_balance    = cash_balance
        self._trade_logger    = get_trade_logger()
        self._open_risk_usd:  float = 0.0
        self._consecutive_losses: int = 0
        self._trades_today:   int = 0

    def update_cash_balance(self, cash: float):
        self._cash_balance = cash

    @property
    def cash_balance(self) -> float:
        return self._cash_balance

    @property
    def buying_power(self) -> float:
        return self._cash_balance * LEVERAGE

    def fetch_live_balance(self) -> float:
        """
        Fetch real cash balance from Kraken for live mode.
        Paper mode returns configured ACCOUNT_BALANCE_USD.
        Falls back to last known balance on error.
        """
        if PAPER_TRADING:
            return self._cash_balance

        try:
            import hashlib, hmac, base64, time, urllib.parse, requests
            from config import KRAKEN_API_KEY, KRAKEN_API_SECRET

            nonce     = str(int(time.time() * 1000))
            data      = {"nonce": nonce}
            post_data = urllib.parse.urlencode(data)
            encoded   = (nonce + post_data).encode()
            message   = "/0/private/Balance".encode() + hashlib.sha256(encoded).digest()
            signature = base64.b64encode(
                hmac.new(base64.b64decode(KRAKEN_API_SECRET), message, hashlib.sha512).digest()
            ).decode()

            resp = requests.post(
                "https://api.kraken.com/0/private/Balance",
                data=data,
                headers={"API-Key": KRAKEN_API_KEY, "API-Sign": signature},
                timeout=10
            )
            result = resp.json()
            if not result.get("error"):
                balances = result.get("result", {})
                # ZUSD is USD balance on Kraken
                usd = float(balances.get("ZUSD", 0))
                if usd > 0:
                    self._cash_balance = usd
                    logger.debug(f"Live balance fetched: ${usd:,.2f}")
                    return usd
        except Exception as e:
            logger.warning(f"Balance fetch failed: {e} — using cached ${self._cash_balance:,.2f}")

        return self._cash_balance

    # ─── Fee Estimation ───────────────────────────────────────────────────────

    def estimate_fees(self, notional: float, hold_hours: float = 2.0) -> float:
        """
        Estimate total round-trip fees for a trade.

        Components:
          - Open taker fee:    notional × KRAKEN_TAKER_FEE
          - Close taker fee:   notional × KRAKEN_TAKER_FEE
          - Margin open fee:   notional × KRAKEN_MARGIN_OPEN_FEE
          - Rollover fee:      notional × KRAKEN_ROLLOVER_FEE × ceil(hold_hours / 4)

        Args:
            notional:   Position notional value in USD
            hold_hours: Expected hold time in hours (default 2h = likely 0-1 rollovers)

        Returns:
            Total estimated fees in USD
        """
        import math
        open_fee     = notional * KRAKEN_TAKER_FEE
        close_fee    = notional * KRAKEN_TAKER_FEE
        margin_fee   = notional * KRAKEN_MARGIN_OPEN_FEE
        rollover_fee = notional * KRAKEN_ROLLOVER_FEE * math.ceil(hold_hours / KRAKEN_ROLLOVER_HOURS)
        total        = open_fee + close_fee + margin_fee + rollover_fee
        return total

    def validate_fee_floor(self, entry_price: float, stop_price: float,
                            notional: float) -> tuple:
        """
        Check if the trade's 1R profit potential clears estimated fees.

        A trade is viable if:
          stop_distance × size × MIN_FEE_ADJUSTED_R > estimated_fees

        Args:
            entry_price: Trade entry price
            stop_price:  Stop loss price
            notional:    Position notional value

        Returns:
            (passes: bool, reason: str, fees: float, ratio: float)
        """
        stop_distance = abs(entry_price - stop_price)
        if stop_distance == 0:
            return False, "stop_distance_zero", 0.0, 0.0

        size_btc    = notional / entry_price
        one_r_usd   = stop_distance * size_btc
        est_fees    = self.estimate_fees(notional)
        ratio       = one_r_usd / est_fees if est_fees > 0 else 0.0

        passes = ratio >= MIN_FEE_ADJUSTED_R

        if not passes:
            reason = (
                f"fee_floor_not_cleared: 1R=${one_r_usd:.2f} "
                f"fees=${est_fees:.2f} ratio={ratio:.2f}x "
                f"(need {MIN_FEE_ADJUSTED_R}x)"
            )
        else:
            reason = f"fee_floor_ok: 1R=${one_r_usd:.2f} fees=${est_fees:.2f} ratio={ratio:.2f}x"

        return passes, reason, est_fees, ratio

    # ─── Position Sizing ──────────────────────────────────────────────────────

    def compute_size(self, entry_price: float, stop_price: float,
                     grade: str = "B",
                     current_balance: Optional[float] = None,
                     direction: Optional[str] = None,
                     **kwargs) -> SizingResult:
        """
        Auto-size position based on grade and buying power.

        Formula:
          buying_power = cash_balance × leverage
          notional     = buying_power × grade_notional_pct
          size_btc     = notional / entry_price

        Then validates fee floor before approving.
        """
        result = SizingResult()

        if grade == "C" and not TRADE_GRADE_C:
            result.allowed = False
            result.reject_reason = "grade_c_disabled"
            return result

        # Fetch or use cached balance
        if current_balance:
            self.update_cash_balance(current_balance)
        elif not PAPER_TRADING:
            self.fetch_live_balance()

        # Grade → notional pct
        grade_pct = {
            "A": GRADE_A_NOTIONAL_PCT,
            "B": GRADE_B_NOTIONAL_PCT,
            "C": GRADE_C_NOTIONAL_PCT,
        }.get(grade, GRADE_B_NOTIONAL_PCT)

        notional    = self.buying_power * grade_pct
        size_btc    = notional / entry_price
        size_btc    = round_size(size_btc, MIN_ORDER_SIZE_BTC)

        if size_btc < MIN_ORDER_SIZE_BTC:
            result.allowed = False
            result.reject_reason = f"size_too_small({size_btc:.6f})"
            return result

        notional     = size_btc * entry_price
        stop_distance = abs(entry_price - stop_price)
        risk_usd     = size_btc * stop_distance if stop_distance > 0 else 0.0
        margin_used  = notional / LEVERAGE

        # ── Fee floor gate ────────────────────────────────────────────────────
        if stop_distance > 0:
            passes, fee_reason, est_fees, fee_ratio = self.validate_fee_floor(
                entry_price, stop_price, notional
            )
            if not passes:
                result.allowed       = False
                result.reject_reason = fee_reason
                result.estimated_fees = est_fees
                logger.info(f"Trade rejected: {fee_reason}")
                return result
        else:
            est_fees  = self.estimate_fees(notional)
            fee_ratio = 0.0
            fee_reason = "no_stop_price"

        result.size_btc       = size_btc
        result.notional_usd   = notional
        result.risk_usd       = risk_usd
        result.margin_used    = margin_used
        result.risk_pct       = risk_usd / self._cash_balance if self._cash_balance > 0 else 0
        result.estimated_fees = est_fees
        result.fee_adjusted_r = fee_ratio
        result.allowed        = True

        logger.info(
            f"Position sized: {size_btc:.4f} BTC "
            f"notional=${notional:,.0f} "
            f"risk=${risk_usd:.2f} ({result.risk_pct:.1%} of cash) "
            f"grade={grade} ({grade_pct:.0%} of buying power) "
            f"est_fees=${est_fees:.2f} fee_ratio={fee_ratio:.2f}x"
        )
        return result

    # ─── Trade Tracking ───────────────────────────────────────────────────────

    def record_trade_result(self, pnl_usd: float):
        if pnl_usd >= 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
        self._trades_today += 1

    def add_open_risk(self, risk_usd: float):
        self._open_risk_usd += risk_usd

    def remove_open_risk(self, risk_usd: float):
        self._open_risk_usd = max(0, self._open_risk_usd - risk_usd)

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    @property
    def open_risk_usd(self) -> float:
        return self._open_risk_usd

    def status_report(self) -> str:
        return (
            f"cash=${self._cash_balance:,.0f} "
            f"buying_power=${self.buying_power:,.0f} "
            f"consecutive_losses={self._consecutive_losses} "
            f"open_risk=${self._open_risk_usd:.2f}"
        )


_risk_manager: Optional[RiskManager] = None


def init_risk_manager(session_config: Optional[SessionConfig] = None,
                      cash_balance: float = ACCOUNT_BALANCE_USD) -> RiskManager:
    global _risk_manager
    _risk_manager = RiskManager(session_config, cash_balance)
    return _risk_manager


def get_risk_manager() -> RiskManager:
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager
