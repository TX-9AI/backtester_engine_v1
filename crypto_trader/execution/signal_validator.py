"""
execution/signal_validator.py — Signal validation.

Changes from previous version:
- Macro filter REMOVED entirely — BTC doesn't correlate with DXY/VIX enough to block trades
- Sweep reversals bypass VWAP and TF confluence (counter-trend by design)
- Conviction threshold lowered to 0.50 for all setups
- All rejections logged at INFO level
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime

from strategy.base_strategy import TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from data.macro_data import MacroSnapshot
from config import (
    MIN_TF_CONFLUENCE, LIQUIDITY_BUFFER_PCT,
    VWAP_FILTER_ACTIVE, ENTRY_COOLDOWN_MINUTES,
    VIX_CRISIS_NO_LONG
)
from utils.time_utils import now_utc, is_within_minutes, current_session

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    passed:          bool        = False
    gates_passed:    List[str]   = field(default_factory=list)
    gates_failed:    List[str]   = field(default_factory=list)
    warnings:        List[str]   = field(default_factory=list)
    session_quality: float       = 1.0
    adjusted_size:   float       = 1.0

    def summary(self) -> str:
        status = "✅ PASSED" if self.passed else "❌ REJECTED"
        return (f"{status} | ✓ {', '.join(self.gates_passed)} "
                f"{'| ✗ ' + ', '.join(self.gates_failed) if self.gates_failed else ''}")


class SignalValidator:
    """
    Streamlined validator — fewer gates, more trades.
    Hard blocks: invalid signal, RRR < 1.3, entry cooldown.
    VWAP: hard block for non-sweep trades only.
    Macro filter: REMOVED — was blocking too many valid setups.
    TF confluence: bypassed for sweep reversals.
    """

    def __init__(self):
        self._last_entry_time: Optional[datetime] = None
        self._last_entry_direction: Optional[str] = None

    def validate(self, signal, regime, vol_state, structure,
                 liq_map, macro, data, current_price) -> ValidationResult:

        # Diagnostic mode: bypass all validation gates
        try:
            from config import DIAGNOSTIC_MODE
            if DIAGNOSTIC_MODE and signal.strategy_name == "Diagnostic":
                result = ValidationResult()
                result.passed = True
                result.gates_passed = ["diagnostic_bypass"]
                logger.info("VALIDATING: DIAGNOSTIC — all gates bypassed")
                return result
        except ImportError:
            pass

        result = ValidationResult()
        session_name, session_quality = current_session()
        result.session_quality = session_quality
        result.adjusted_size   = session_quality
        is_sweep = regime.primary_regime == Regime.SWEEP_REVERSAL

        logger.info(
            f"VALIDATING: {signal.direction.upper()} @ {signal.entry_price:.2f} "
            f"stop={signal.stop_price:.2f} t1={signal.target_1:.2f} "
            f"strategy={signal.strategy_name} regime={regime.primary_regime} "
            f"conviction={signal.conviction:.2f} "
            f"vwap={vol_state.vwap:.2f} price_vs_vwap={vol_state.price_vs_vwap}"
        )

        # ── Gate 1: Signal valid ───────────────────────────────────────────────
        if not signal.is_valid:
            result.gates_failed.append("signal_invalid")
            logger.info(f"❌ REJECTED signal_invalid")
            return result
        result.gates_passed.append("signal_valid")

        # ── Gate 2: Minimum RRR 1.3 ───────────────────────────────────────────
        signal.compute_ratios()
        if signal.rrr_1 < 1.3:
            result.gates_failed.append(f"rrr_low({signal.rrr_1:.2f})")
            logger.info(f"❌ REJECTED rrr {signal.rrr_1:.2f} < 1.3")
            return result
        result.gates_passed.append(f"rrr_ok({signal.rrr_1:.2f})")

        # ── Gate 3: Entry cooldown ─────────────────────────────────────────────
        if (self._last_entry_time is not None and
                is_within_minutes(self._last_entry_time, ENTRY_COOLDOWN_MINUTES)):
            result.gates_failed.append("cooldown")
            logger.info(f"❌ REJECTED cooldown — within {ENTRY_COOLDOWN_MINUTES}min of last entry")
            return result
        result.gates_passed.append("cooldown_clear")

        # ── Gate 4: VIX crisis — only blocks longs in extreme fear ────────────
        if (VIX_CRISIS_NO_LONG and macro and
                macro.vix_regime == "CRISIS" and signal.direction == "long"):
            result.gates_failed.append("vix_crisis_no_long")
            logger.info(f"❌ REJECTED vix crisis — suppressing long")
            return result
        result.gates_passed.append("vix_ok")

        # ── Gate 5: VWAP alignment — hard block for non-sweep trades ──────────
        # Sweep reversals bypass — they enter at extremes by definition
        if VWAP_FILTER_ACTIVE and vol_state.vwap > 0 and not is_sweep:
            if signal.direction == "long" and vol_state.price_vs_vwap == "BELOW":
                result.gates_failed.append("vwap_long_below")
                logger.info(
                    f"❌ REJECTED VWAP — long below VWAP "
                    f"price={current_price:.2f} vwap={vol_state.vwap:.2f}"
                )
                return result
            if signal.direction == "short" and vol_state.price_vs_vwap == "ABOVE":
                result.gates_failed.append("vwap_short_above")
                logger.info(
                    f"❌ REJECTED VWAP — short above VWAP "
                    f"price={current_price:.2f} vwap={vol_state.vwap:.2f}"
                )
                return result
            result.gates_passed.append(f"vwap_aligned({vol_state.price_vs_vwap})")
        else:
            result.gates_passed.append("vwap_sweep_bypass" if is_sweep else "vwap_skip")

        # ── Gate 6: Liquidity path ─────────────────────────────────────────────
        for pool in liq_map.pools:
            if pool.swept:
                continue
            dist_pct = abs(pool.price - current_price) / current_price
            if dist_pct > LIQUIDITY_BUFFER_PCT * 3:
                continue
            if signal.direction == "long" and pool.kind == "high":
                if current_price < pool.price < signal.target_1 and dist_pct < LIQUIDITY_BUFFER_PCT:
                    result.gates_failed.append(f"liq_pool({pool.price:.0f})")
                    logger.info(f"❌ REJECTED liq pool in path at {pool.price:.0f}")
                    return result
            if signal.direction == "short" and pool.kind == "low":
                if signal.target_1 < pool.price < current_price and dist_pct < LIQUIDITY_BUFFER_PCT:
                    result.gates_failed.append(f"liq_pool({pool.price:.0f})")
                    logger.info(f"❌ REJECTED liq pool in path at {pool.price:.0f}")
                    return result
        result.gates_passed.append("liq_clear")

        # ── Gate 7: Structure ──────────────────────────────────────────────────
        if not is_sweep:
            near_level = (
                structure.in_sr_zone or
                structure.nearest_sr_distance_pct < 0.008 or
                len(signal.confluence_factors) >= 3
            )
            if not near_level:
                result.gates_failed.append("no_structure_level")
                logger.info(f"❌ REJECTED no structure level")
                return result
        result.gates_passed.append("structure_ok")

        # ── Gate 8: TF confluence — bypassed for sweeps ────────────────────────
        if not is_sweep:
            agreeing = 0
            checked  = 0
            for tf in ["15m", "1h", "4h"]:
                df = data.get(tf)
                if df is None or df.empty:
                    continue
                checked += 1
                if len(df) >= 50:
                    from utils.math_utils import ema_series
                    ema_50 = float(ema_series(df["close"], 50).iloc[-1])
                    if signal.direction == "long" and current_price > ema_50:
                        agreeing += 1
                    elif signal.direction == "short" and current_price < ema_50:
                        agreeing += 1
            if checked > 0 and agreeing < min(MIN_TF_CONFLUENCE, checked):
                result.gates_failed.append(f"tf_confluence({agreeing}/{checked})")
                logger.info(f"❌ REJECTED tf confluence {agreeing}/{checked}")
                return result
            result.gates_passed.append(f"tf_ok({agreeing}/{checked if checked else '?'})")
        else:
            result.gates_passed.append("tf_sweep_bypass")

        # ── PASSED ────────────────────────────────────────────────────────────
        result.passed = True
        logger.info(
            f"✅ VALIDATED: {signal.direction.upper()} @ {signal.entry_price:.2f} "
            f"stop={signal.stop_price:.2f} RRR={signal.rrr_1:.2f} "
            f"gates={result.gates_passed}"
        )
        return result

    def record_entry(self, direction: str):
        self._last_entry_time      = now_utc()
        self._last_entry_direction = direction
        logger.info(f"Entry cooldown started: {ENTRY_COOLDOWN_MINUTES}min")

    def reset_cooldown(self):
        self._last_entry_time = None
        logger.info("Entry cooldown reset")


_validator: Optional[SignalValidator] = None


def get_signal_validator() -> SignalValidator:
    global _validator
    if _validator is None:
        _validator = SignalValidator()
    return _validator
