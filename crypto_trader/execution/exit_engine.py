"""
execution/exit_engine.py — All exit logic: stops, trailing, targets, time, regime.
This is where the aggressive trailing stop rules live.
Structure-aware partial exits: 50% at S/R if clean level exists.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

from database.trade_logger import TradeRecord, get_trade_logger
from analysis.structure_analyzer import StructureMap, get_structure_analyzer
from config import (
    TRAIL_ACTIVATION_R, TRAIL_STEP_1_R, TRAIL_STEP_2_R, TRAIL_STEP_3_R,
    TRAIL_TIGHTEN_ON_REGIME, PARTIAL_EXIT_PCT, PARTIAL_MINIMUM_R,
    STAGNANT_TRADE_MINUTES
)
from strategy.momentum_strategy import adx_stop_multiplier
from utils.math_utils import r_multiple, unrealized_pnl
from utils.time_utils import now_utc, minutes_since

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """Outcome of the exit evaluation for an open trade."""
    should_exit:       bool   = False
    should_partial:    bool   = False
    partial_pct:       float  = 0.0     # Fraction to exit (0.5 = 50%)
    new_stop:          Optional[float]  = None
    exit_reason:       str    = ""
    partial_reason:    str    = ""
    current_r:         float  = 0.0
    unrealized_pnl:    float  = 0.0


class ExitEngine:
    """
    Evaluates every open trade on each tick and decides:
    1. Should we exit fully? (stop hit, target hit, time, regime change)
    2. Should we take a partial? (structure-aware 50% at clean S/R)
    3. Should we adjust the trailing stop?
    """

    def __init__(self):
        self._trade_logger = get_trade_logger()
        self._partial_taken: dict = {}   # trade_id → bool

    def evaluate(self,
                 record:        TradeRecord,
                 current_price: float,
                 structure:     StructureMap,
                 current_regime: str,
                 entry_regime:  str,
                 atr:           float) -> ExitDecision:
        """
        Full exit evaluation for an open trade.

        Args:
            record:         Current trade record from DB
            current_price:  Latest price
            structure:      Current market structure
            current_regime: Active regime (may differ from entry regime)
            entry_regime:   Regime when trade was entered
            atr:            Current ATR in USD

        Returns:
            ExitDecision
        """
        decision = ExitDecision()

        direction   = record["direction"]
        entry_price = record["entry_price"]
        stop_price  = record["stop_price"]
        target_1    = record["target_1"]
        target_2    = record.get("target_2") or 0.0
        entry_time  = record["entry_time"]
        trade_id    = record["trade_id"]
        partial_done = self._partial_taken.get(trade_id, False)

        # ── Current position metrics ───────────────────────────────────────────
        current_r = r_multiple(entry_price, current_price, stop_price, direction)
        pnl       = unrealized_pnl(entry_price, current_price, record["position_size"], direction)
        decision.current_r      = current_r
        decision.unrealized_pnl = pnl

        # ── STOP HIT ──────────────────────────────────────────────────────────
        if direction == "long" and current_price <= stop_price:
            decision.should_exit = True
            decision.exit_reason = "stop_hit"
            logger.info(f"STOP HIT: {trade_id[:8]} price={current_price:.2f} stop={stop_price:.2f}")
            return decision

        if direction == "short" and current_price >= stop_price:
            decision.should_exit = True
            decision.exit_reason = "stop_hit"
            logger.info(f"STOP HIT: {trade_id[:8]} price={current_price:.2f} stop={stop_price:.2f}")
            return decision

        # ── TARGET 1 HIT ──────────────────────────────────────────────────────
        if target_1 > 0:
            if direction == "long" and current_price >= target_1 and not partial_done:
                # Check for structure-aware partial vs full exit
                partial_decision = self._check_partial_exit(
                    direction, entry_price, current_price,
                    stop_price, target_1, target_2, structure,
                    current_r, partial_done, atr
                )
                if partial_decision.should_partial:
                    decision.should_partial = True
                    decision.partial_pct    = partial_decision.partial_pct
                    decision.partial_reason = partial_decision.partial_reason
                    self._partial_taken[trade_id] = True
                    # Also tighten stop to breakeven
                    decision.new_stop = entry_price * 1.001
                    logger.info(
                        f"PARTIAL EXIT: {trade_id[:8]} "
                        f"{decision.partial_pct:.0%} @ {current_price:.2f} "
                        f"({decision.partial_reason})"
                    )
                    return decision
                else:
                    # Full exit at T1 (no meaningful S/R exists above, or no T2)
                    if not target_2:
                        decision.should_exit = True
                        decision.exit_reason = "target_1_hit"
                        return decision

            if direction == "short" and current_price <= target_1 and not partial_done:
                partial_decision = self._check_partial_exit(
                    direction, entry_price, current_price,
                    stop_price, target_1, target_2, structure,
                    current_r, partial_done, atr
                )
                if partial_decision.should_partial:
                    decision.should_partial = True
                    decision.partial_pct    = partial_decision.partial_pct
                    decision.partial_reason = partial_decision.partial_reason
                    self._partial_taken[trade_id] = True
                    decision.new_stop = entry_price * 0.999
                    return decision
                else:
                    if not target_2:
                        decision.should_exit = True
                        decision.exit_reason = "target_1_hit"
                        return decision

        # ── TARGET 2 HIT ──────────────────────────────────────────────────────
        if target_2 and partial_done:
            if direction == "long" and current_price >= target_2:
                decision.should_exit = True
                decision.exit_reason = "target_2_hit"
                return decision
            if direction == "short" and current_price <= target_2:
                decision.should_exit = True
                decision.exit_reason = "target_2_hit"
                return decision

        # ── TRAILING STOP ─────────────────────────────────────────────────────
        adx = getattr(record, 'adx', 0.0) if hasattr(record, 'adx') else record.get('adx', 0.0) if hasattr(record, 'get') else 0.0
        new_stop = self._compute_trail(
            direction, entry_price, current_price,
            stop_price, current_r, atr,
            current_regime, entry_regime, adx=adx
        )

        if new_stop is not None and new_stop != stop_price:
            # Only move stop in the favorable direction
            if direction == "long" and new_stop > stop_price:
                decision.new_stop = new_stop
            elif direction == "short" and new_stop < stop_price:
                decision.new_stop = new_stop

        # ── TIME-BASED EXIT ───────────────────────────────────────────────────
        if entry_time:
            try:
                from datetime import timezone
                entry_dt = datetime.fromisoformat(entry_time)
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                mins_in_trade = minutes_since(entry_dt)
                if mins_in_trade >= STAGNANT_TRADE_MINUTES and current_r < 0.5:
                    decision.should_exit = True
                    decision.exit_reason = f"time_stagnant({mins_in_trade:.0f}min)"
                    logger.info(f"TIME EXIT: {trade_id[:8]} "
                                f"{mins_in_trade:.0f}min in trade, <0.5R progress")
                    return decision
            except Exception as e:
                logger.debug(f"Could not parse entry time: {e}")

        # ── REGIME CHANGE — ORPHANED TRADE ────────────────────────────────────
        if current_regime != entry_regime:
            # Don't force exit — tighten the trail to 0.25 ATR
            if current_r > 0 and decision.new_stop is None:
                orphan_stop = self._orphan_trail_stop(
                    direction, current_price, atr
                )
                if orphan_stop != stop_price:
                    decision.new_stop = orphan_stop
                    logger.debug(
                        f"Orphaned trade ({entry_regime}→{current_regime}): "
                        f"tightening trail to {orphan_stop:.2f}"
                    )

        return decision

    def _check_partial_exit(self, direction, entry_price, current_price,
                             stop_price, target_1, target_2,
                             structure: StructureMap,
                             current_r: float, partial_done: bool,
                             atr: float) -> ExitDecision:
        """
        Structure-aware partial exit logic.

        Rules:
        - If a clean S/R level exists between entry and target_1 → take 50% there
        - If no clean S/R → hold full position, trail aggressively
        """
        decision = ExitDecision()
        decision.partial_pct = PARTIAL_EXIT_PCT

        # Check minimum R for partial
        if current_r < PARTIAL_MINIMUM_R:
            return decision  # Not enough profit yet

        # Look for S/R between entry and target_1
        sr_levels = get_structure_analyzer().sr_between(
            structure, entry_price, target_1
        )

        strong_sr = [l for l in sr_levels if l.touches >= 3 and l.strength >= 0.5]

        if strong_sr:
            # Clean S/R exists — take the partial
            decision.should_partial = True
            decision.partial_reason = (
                f"S/R at {strong_sr[0].price:.0f} "
                f"({strong_sr[0].touches} touches)"
            )
        elif target_2 and target_2 > 0:
            # No clean S/R but we have T2 — still take partial at T1
            # to lock profit while letting rest run
            decision.should_partial = True
            decision.partial_reason = "T1 reached, holding for T2 (no S/R obstruction)"
        else:
            # No S/R, no T2 — full exit
            decision.should_partial = False

        return decision

    def _compute_trail(self, direction, entry_price, current_price,
                        current_stop, current_r, atr,
                        current_regime, entry_regime,
                        adx: float = 0.0) -> Optional[float]:
        """
        Aggressive trailing stop rules:
          1R:  Move stop to breakeven
          2R:  Trail stop to 0.5R profit
          3R:  Trail stop to 1.5R profit
          Orphaned (regime changed): tighten to 0.25 ATR trail
        """
        risk = abs(entry_price - current_stop)
        if risk == 0 or atr == 0:
            return None

        new_stop = None

        if direction == "long":
            if current_r >= TRAIL_STEP_3_R:   # 3R → trail to 1.5R profit
                new_stop = max(current_stop, entry_price + risk * 1.5)
            elif current_r >= TRAIL_STEP_2_R:  # 2R → trail to 0.5R profit
                new_stop = max(current_stop, entry_price + risk * 0.5)
            elif current_r >= TRAIL_STEP_1_R:  # 1R → move to breakeven
                new_stop = max(current_stop, entry_price)

            # ADX-scaled ATR trail after 1R — stronger trend = wider trail
            if current_r >= TRAIL_ACTIVATION_R and atr > 0:
                trail_mult = adx_stop_multiplier(adx) * 0.5
                # Enforce minimum trail distance — same adaptive floor as entry
                from strategy.momentum_strategy import adaptive_min_stop
                min_trail = adaptive_min_stop(current_price, atr, trail_mult)
                atr_trail = current_price - max(atr * trail_mult, min_trail)
                if atr_trail > (new_stop or current_stop):
                    new_stop = atr_trail

        else:  # short
            if current_r >= TRAIL_STEP_3_R:
                new_stop = min(current_stop, entry_price - risk * 1.5)
            elif current_r >= TRAIL_STEP_2_R:
                new_stop = min(current_stop, entry_price - risk * 0.5)
            elif current_r >= TRAIL_STEP_1_R:
                new_stop = min(current_stop, entry_price)

            if current_r >= TRAIL_ACTIVATION_R and atr > 0:
                trail_mult = adx_stop_multiplier(adx) * 0.5
                from strategy.momentum_strategy import adaptive_min_stop
                min_trail = adaptive_min_stop(current_price, atr, trail_mult)
                atr_trail = current_price + max(atr * trail_mult, min_trail)
                if atr_trail < (new_stop or current_stop):
                    new_stop = atr_trail

        return new_stop

    def _orphan_trail_stop(self, direction, current_price, atr) -> float:
        """Tighten trail to 0.25 ATR for orphaned trade (regime changed)."""
        if direction == "long":
            return current_price - (atr * TRAIL_TIGHTEN_ON_REGIME)
        else:
            return current_price + (atr * TRAIL_TIGHTEN_ON_REGIME)

    def clear_partial_flag(self, trade_id: str):
        """Reset partial exit flag (called on trade close)."""
        self._partial_taken.pop(trade_id, None)


# Singleton
_exit_engine: Optional[ExitEngine] = None


def get_exit_engine() -> ExitEngine:
    global _exit_engine
    if _exit_engine is None:
        _exit_engine = ExitEngine()
    return _exit_engine
