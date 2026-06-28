"""
analysis/regime_classifier.py — Market regime classification.
THE most important module. Synthesizes all analysis into a single RegimeState.
Everything downstream depends on getting this right.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional
import pandas as pd

from config import (
    ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
    REGIME_REASSESS_MINUTES
)
from analysis.volatility_engine import VolatilityState
from analysis.trend_engine import TrendState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from data.macro_data import MacroSnapshot
from utils.time_utils import now_utc, fmt_et_full

logger = logging.getLogger(__name__)


# ─── REGIME CONSTANTS ─────────────────────────────────────────────────────────

class Regime:
    TRENDING_BULL      = "TRENDING_BULL"
    TRENDING_BEAR      = "TRENDING_BEAR"
    RANGING            = "RANGING"
    BREAKOUT_VOLATILE  = "BREAKOUT_VOLATILE"
    COMPRESSION        = "COMPRESSION"
    SWEEP_REVERSAL     = "SWEEP_REVERSAL"
    UNKNOWN            = "UNKNOWN"


class BtcPersonality:
    INSTITUTIONAL_ACCUMULATION = "INSTITUTIONAL_ACCUMULATION"
    RETAIL_FOMO                = "RETAIL_FOMO"
    DISTRIBUTION               = "DISTRIBUTION"
    CAPITULATION               = "CAPITULATION"
    CONSOLIDATION              = "CONSOLIDATION"
    NEUTRAL                    = "NEUTRAL"


@dataclass
class RegimeState:
    """
    Complete regime classification — the single source of truth
    that drives strategy selection and all downstream decisions.
    """
    primary_regime:     str   = Regime.UNKNOWN
    conviction:         float = 0.0           # 0.0 – 1.0
    macro_context:      str   = "NEUTRAL"
    btc_personality:    str   = BtcPersonality.NEUTRAL

    # Supporting data
    adx:                float = 0.0
    atr_normalized:     float = 0.0
    bb_width_pct:       float = 0.5
    trend_direction:    str   = "NEUTRAL"
    trend_conviction:   float = 0.0
    structure_sequence: str   = "NEUTRAL"
    sweep_recent:       bool  = False
    sweep_age_bars:     int   = 999
    vix_regime:         str   = "UNKNOWN"

    # Timeframe vote breakdown
    timeframe_alignment: Dict[str, str] = field(default_factory=dict)

    # Meta
    classified_at:      str   = ""
    valid_until:        str   = ""
    trigger:            str   = "scheduled"   # scheduled / consecutive_loss / manual
    notes:              str   = ""

    @property
    def is_trending(self) -> bool:
        return self.primary_regime in (Regime.TRENDING_BULL, Regime.TRENDING_BEAR)

    @property
    def is_bullish(self) -> bool:
        return self.primary_regime == Regime.TRENDING_BULL

    @property
    def is_bearish(self) -> bool:
        return self.primary_regime == Regime.TRENDING_BEAR

    @property
    def is_ranging(self) -> bool:
        return self.primary_regime == Regime.RANGING

    @property
    def is_compression(self) -> bool:
        return self.primary_regime == Regime.COMPRESSION

    @property
    def is_sweep_reversal(self) -> bool:
        return self.primary_regime == Regime.SWEEP_REVERSAL

    @property
    def is_breakout(self) -> bool:
        return self.primary_regime == Regime.BREAKOUT_VOLATILE


class RegimeClassifier:
    """
    Synthesizes volatility, trend, structure, and liquidity data
    into a single definitive regime classification.

    Decision hierarchy:
    1. SWEEP_REVERSAL  — highest priority, very specific setup
    2. BREAKOUT_VOLATILE — ATR expansion breaking structure
    3. COMPRESSION     — BB squeeze, ATR declining
    4. TRENDING_BULL/BEAR — strong ADX + aligned EMAs
    5. RANGING         — default when nothing else is clear
    """

    def classify(self,
                 vol_state:   VolatilityState,
                 trend_state: TrendState,
                 structure:   StructureMap,
                 liq_map:     LiquidityMap,
                 macro:       Optional[MacroSnapshot] = None,
                 trigger:     str = "scheduled") -> RegimeState:
        """
        Run full regime classification.

        Args:
            vol_state:   Output from VolatilityEngine
            trend_state: Output from TrendEngine
            structure:   Output from StructureAnalyzer
            liq_map:     Output from LiquidityMapper
            macro:       Optional macro snapshot
            trigger:     What caused this reassessment

        Returns:
            RegimeState with primary_regime and conviction
        """
        state = RegimeState(
            adx=trend_state.primary_adx,
            atr_normalized=vol_state.atr_normalized,
            bb_width_pct=vol_state.bb_width_pct,
            trend_direction=trend_state.overall_direction,
            trend_conviction=trend_state.overall_conviction,
            structure_sequence=structure.structure_sequence,
            sweep_recent=liq_map.recent_sweep is not None,
            sweep_age_bars=liq_map.sweep_age_bars,
            vix_regime=macro.vix_regime if macro else "UNKNOWN",
            macro_context=macro.macro_context if macro else "NEUTRAL",
            classified_at=fmt_et_full(),
            trigger=trigger,
            timeframe_alignment={
                tf: v.direction for tf, v in trend_state.votes.items()
            }
        )

        # BTC personality
        state.btc_personality = self._classify_personality(
            trend_state, vol_state, structure, macro
        )

        # ── REGIME DECISION TREE ───────────────────────────────────────────────

        # Priority 1: SWEEP_REVERSAL
        # Recent confirmed sweep + rejection + imbalance left behind
        if self._is_sweep_reversal(liq_map, vol_state, trend_state):
            state.primary_regime = Regime.SWEEP_REVERSAL
            state.conviction     = self._sweep_conviction(liq_map, trend_state)
            state.notes          = self._note_sweep(liq_map)
            return self._finalize(state)

        # Priority 2: BREAKOUT_VOLATILE
        # ATR explosion + price breaking key structure level
        if self._is_breakout(vol_state, structure, trend_state):
            state.primary_regime = Regime.BREAKOUT_VOLATILE
            state.conviction     = self._breakout_conviction(vol_state, trend_state)
            state.notes          = "ATR expanding, price breaking key level"
            return self._finalize(state)

        # Priority 3: COMPRESSION (pre-breakout squeeze)
        # BB width at multi-period low, ATR declining
        if self._is_compression(vol_state):
            state.primary_regime = Regime.COMPRESSION
            state.conviction     = self._compression_conviction(vol_state)
            state.notes          = f"BB squeeze at {vol_state.bb_width_pct:.0%} percentile"
            return self._finalize(state)

        # Priority 4: TRENDING (requires strong ADX + EMA alignment)
        if self._is_trending(trend_state, structure):
            if trend_state.is_bullish:
                state.primary_regime = Regime.TRENDING_BULL
            else:
                state.primary_regime = Regime.TRENDING_BEAR
            state.conviction     = self._trend_conviction(trend_state, vol_state, macro)
            state.notes          = (
                f"ADX={trend_state.primary_adx:.1f} "
                f"aligned={trend_state.aligned_timeframes}/{trend_state.total_timeframes} "
                f"struct={structure.structure_sequence}"
            )
            return self._finalize(state)

        # Default: RANGING
        state.primary_regime = Regime.RANGING
        state.conviction     = self._ranging_conviction(trend_state, vol_state)
        state.notes          = (
            f"ADX={trend_state.primary_adx:.1f} (below threshold) "
            f"price oscillating in structure"
        )
        return self._finalize(state)

    # ── REGIME CONDITION CHECKS ────────────────────────────────────────────────

    def _is_sweep_reversal(self, liq_map: LiquidityMap,
                            vol_state: VolatilityState,
                            trend_state: TrendState) -> bool:
        """
        SWEEP_REVERSAL conditions:
        - Confirmed sweep within last 8 bars (standard)
        - OR confirmed sweep within last 3 bars that overrides TRENDING regime
          (close-based confirmation — sweep must have rejection, not just wick)
        - Rejection of at least 0.3%

        Key fix: SWEEP_REVERSAL overrides TRENDING when sweep is very recent (≤3 bars).
        This prevents the regime from calling TRENDING_BULL on a spike that is
        actually a liquidity sweep about to reverse.
        """
        if not liq_map.recent_sweep:
            return False

        # Standard: sweep within 8 bars with normal rejection
        if liq_map.sweep_age_bars <= 8 and liq_map.recent_sweep.rejection_pct >= 0.003:
            return True

        # Override: very fresh sweep (≤3 bars) with strong rejection overrides trending
        # This catches the whipsaw case where regime flips to TRENDING on spike candle
        if (liq_map.sweep_age_bars <= 3 and
                liq_map.recent_sweep.rejection_pct >= 0.005 and
                trend_state.primary_adx < 50):
            return True

        return False

    def _is_breakout(self, vol_state: VolatilityState,
                      structure: StructureMap,
                      trend_state: TrendState) -> bool:
        """
        BREAKOUT conditions:
        - ATR expanding significantly
        - Price outside Bollinger Bands
        - OR price breaking recent swing high/low
        """
        if vol_state.is_expanding and vol_state.price_vs_bb != "INSIDE":
            return True
        if (vol_state.atr_state == "EXPANDING" and
                trend_state.primary_adx > ADX_TREND_THRESHOLD and
                structure.structure_sequence in ("HH_HL", "LH_LL")):
            return True
        return False

    def _is_compression(self, vol_state: VolatilityState) -> bool:
        """
        COMPRESSION conditions:
        - BB width at 20th percentile or lower
        - ATR contracting or stable
        - NOT already expanded
        """
        return (vol_state.bb_width_pct <= 0.20 and
                vol_state.atr_state in ("CONTRACTING", "STABLE") and
                not vol_state.is_expanding)

    def _is_trending(self, trend_state: TrendState,
                      structure: StructureMap) -> bool:
        """
        TRENDING conditions:
        - ADX above threshold
        - At least 3 of 5 timeframes aligned
        - Structure confirms (HH_HL or LH_LL)
        """
        if trend_state.primary_adx < ADX_TREND_THRESHOLD:
            return False
        if trend_state.overall_direction == "NEUTRAL":
            return False
        if trend_state.aligned_timeframes < 2:
            return False
        return True

    # ── CONVICTION SCORING ─────────────────────────────────────────────────────

    def _sweep_conviction(self, liq_map: LiquidityMap,
                           trend_state: TrendState) -> float:
        """Sweep reversal conviction based on rejection strength and age."""
        sweep = liq_map.recent_sweep
        if not sweep:
            return 0.3
        rejection_score = min(sweep.rejection_pct / 0.01, 1.0)   # Max at 1%
        age_score       = max(0, 1 - (liq_map.sweep_age_bars / 8))
        return (rejection_score * 0.5 + age_score * 0.5) * 0.9 + 0.1

    def _breakout_conviction(self, vol_state: VolatilityState,
                              trend_state: TrendState) -> float:
        """Breakout conviction from ATR ratio and trend alignment."""
        atr_ratio  = vol_state.atr_current / max(vol_state.atr_avg_20, 0.001)
        atr_score  = min((atr_ratio - 1) / 0.5, 1.0) if atr_ratio > 1 else 0
        tf_score   = trend_state.aligned_timeframes / max(trend_state.total_timeframes, 1)
        return atr_score * 0.5 + tf_score * 0.5

    def _compression_conviction(self, vol_state: VolatilityState) -> float:
        """Compression conviction: tighter squeeze = higher conviction."""
        return max(0, 1.0 - vol_state.bb_width_pct) * 0.8 + 0.2

    def _trend_conviction(self, trend_state: TrendState,
                           vol_state: VolatilityState,
                           macro: Optional[MacroSnapshot]) -> float:
        """Trend conviction from ADX, TF alignment, and macro."""
        adx_score  = min(trend_state.primary_adx / 50, 1.0)
        tf_score   = trend_state.aligned_timeframes / max(trend_state.total_timeframes, 1)
        macro_mult = 1.1 if (macro and macro.macro_context == "RISK_ON"
                              and trend_state.is_bullish) else 1.0
        base       = adx_score * 0.5 + tf_score * 0.3 + trend_state.overall_conviction * 0.2
        return min(base * macro_mult, 1.0)

    def _ranging_conviction(self, trend_state: TrendState,
                             vol_state: VolatilityState) -> float:
        """Ranging conviction: low ADX + stable volatility = cleaner range."""
        adx_score = max(0, 1 - trend_state.primary_adx / ADX_RANGE_THRESHOLD)
        vol_score = 1.0 if vol_state.atr_state == "STABLE" else 0.6
        return adx_score * 0.6 + vol_score * 0.4

    # ── BTC PERSONALITY ────────────────────────────────────────────────────────

    def _classify_personality(self, trend_state: TrendState,
                               vol_state: VolatilityState,
                               structure: StructureMap,
                               macro: Optional[MacroSnapshot]) -> str:
        """
        BTC personality captures the character of the current move.
        This modifies how aggressively the bot enters and where it targets.

        INSTITUTIONAL_ACCUMULATION: slow grind up, low volatility, HH_HL structure
        RETAIL_FOMO: explosive up move with high ATR, expanded BBs
        DISTRIBUTION: grinding down in range with intermittent bounces
        CAPITULATION: rapid high-ATR sell-off
        CONSOLIDATION: tight range after strong move
        """
        is_high_vol = vol_state.atr_state == "EXPANDING"
        is_low_vol  = vol_state.atr_state == "CONTRACTING"
        is_up       = trend_state.is_bullish
        is_down     = trend_state.is_bearish
        hh_hl       = structure.structure_sequence == "HH_HL"
        lh_ll       = structure.structure_sequence == "LH_LL"

        if is_up and is_high_vol:
            return BtcPersonality.RETAIL_FOMO
        if is_up and is_low_vol and hh_hl:
            return BtcPersonality.INSTITUTIONAL_ACCUMULATION
        if is_down and is_high_vol:
            return BtcPersonality.CAPITULATION
        if is_down and lh_ll:
            return BtcPersonality.DISTRIBUTION
        if is_low_vol and not is_up and not is_down:
            return BtcPersonality.CONSOLIDATION
        return BtcPersonality.NEUTRAL

    # ── NOTES ─────────────────────────────────────────────────────────────────

    def _note_sweep(self, liq_map: LiquidityMap) -> str:
        if not liq_map.recent_sweep:
            return ""
        s = liq_map.recent_sweep
        return (f"{s.kind} @ {s.pool_price:.0f} "
                f"rejection={s.rejection_pct:.1%} "
                f"{liq_map.sweep_age_bars} bars ago")

    def _finalize(self, state: RegimeState) -> RegimeState:
        """Add timestamps and return."""
        state.classified_at = fmt_et_full()
        logger.info(
            f"REGIME: {state.primary_regime} "
            f"conviction={state.conviction:.2f} "
            f"personality={state.btc_personality} "
            f"macro={state.macro_context}"
        )
        return state


# Module-level singleton
_classifier: Optional[RegimeClassifier] = None


def get_regime_classifier() -> RegimeClassifier:
    global _classifier
    if _classifier is None:
        _classifier = RegimeClassifier()
    return _classifier
