"""
risk/setup_scorer.py — Scores and grades trade signals A/B/C.
Grade determines position size: A=1.5x, B=1.0x, C=0.5x.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from strategy.base_strategy import TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from data.macro_data import MacroSnapshot
from config import SCORE_WEIGHTS, GRADE_A_MIN_SCORE, GRADE_B_MIN_SCORE, GRADE_SIZE_MULTIPLIER
from utils.time_utils import current_session

logger = logging.getLogger(__name__)


@dataclass
class SetupScore:
    """Result of setup grading."""
    grade:           str   = "C"
    score:           float = 0.0
    size_multiplier: float = 0.5
    breakdown:       dict  = None

    def __post_init__(self):
        if self.breakdown is None:
            self.breakdown = {}


# ─── Strategy-specific scoring profiles ──────────────────────────────────────
# Each strategy uses different TF weights and grade thresholds.
# Lower timeframes matter more for scalps; higher TFs matter for momentum/trend.
# This allows C grade compression scalps to be taken freely while keeping
# momentum and sweep trades to a higher standard.

STRATEGY_PROFILES = {
    "CompressionScalp": {
        "tf_weights":    {"1d": 0.05, "4h": 0.05, "1h": 0.15, "15m": 0.35, "5m": 0.40},
        "score_weights": {
            "regime_conviction":   0.15,  # Less important — regime is COMPRESSION by def
            "structure_alignment": 0.20,  # Still want S/R context
            "tf_confluence":       0.10,  # Low TF only — daily/4h irrelevant to scalp
            "liquidity_clear":     0.30,  # Most important — path must be clear
            "vwap_context":        0.15,  # VWAP still useful reference
            "macro_alignment":     0.10,  # Minor factor
        },
        "grade_a": 0.72,
        "grade_b": 0.45,   # Lower B threshold — C scalps are acceptable
    },
    "MomentumStrategy": {
        "tf_weights":    {"1d": 0.10, "4h": 0.20, "1h": 0.35, "15m": 0.25, "5m": 0.10},
        "score_weights": {
            "regime_conviction":   0.25,
            "structure_alignment": 0.20,
            "tf_confluence":       0.25,  # Higher weight — momentum needs TF agreement
            "liquidity_clear":     0.15,
            "vwap_context":        0.10,
            "macro_alignment":     0.05,
        },
        "grade_a": 0.78,
        "grade_b": 0.58,
    },
    "SweepReversal": {
        "tf_weights":    {"1d": 0.05, "4h": 0.10, "1h": 0.20, "15m": 0.30, "5m": 0.35},
        "score_weights": {
            "regime_conviction":   0.20,
            "structure_alignment": 0.15,
            "tf_confluence":       0.10,  # Sweeps are short-term by nature
            "liquidity_clear":     0.35,  # Path to target is critical
            "vwap_context":        0.10,
            "macro_alignment":     0.10,
        },
        "grade_a": 0.74,
        "grade_b": 0.50,
    },
    "MeanReversion": {
        "tf_weights":    {"1d": 0.10, "4h": 0.15, "1h": 0.30, "15m": 0.30, "5m": 0.15},
        "score_weights": {
            "regime_conviction":   0.20,
            "structure_alignment": 0.30,  # Structure is the whole thesis
            "tf_confluence":       0.15,
            "liquidity_clear":     0.20,
            "vwap_context":        0.10,
            "macro_alignment":     0.05,
        },
        "grade_a": 0.76,
        "grade_b": 0.55,
    },
    "default": {
        "tf_weights":    {"1d": 0.10, "4h": 0.15, "1h": 0.25, "15m": 0.25, "5m": 0.25},
        "score_weights": {
            "regime_conviction":   0.25,
            "structure_alignment": 0.20,
            "tf_confluence":       0.20,
            "liquidity_clear":     0.15,
            "vwap_context":        0.10,
            "macro_alignment":     0.10,
        },
        "grade_a": 0.78,
        "grade_b": 0.55,
    },
}


class SetupScorer:
    """
    Scores a trade setup using strategy-specific weights and thresholds.
    CompressionScalp uses low-TF weights and lower grade thresholds.
    Momentum uses high-TF weights and stricter thresholds.
    This allows each strategy to be graded on its own terms.
    """

    def score(self,
              signal:    TradeSignal,
              regime:    RegimeState,
              vol_state: VolatilityState,
              structure: StructureMap,
              liq_map:   LiquidityMap,
              macro:     Optional[MacroSnapshot] = None) -> SetupScore:

        breakdown = {}

        # Select strategy profile
        strategy_name = getattr(signal, "strategy_name", "default")
        profile = STRATEGY_PROFILES.get(strategy_name, STRATEGY_PROFILES["default"])
        weights      = profile["score_weights"]
        tf_weights   = profile["tf_weights"]
        grade_a_min  = profile["grade_a"]
        grade_b_min  = profile["grade_b"]

        # ── 1. Regime Conviction ──────────────────────────────────────────────
        reg_score = regime.conviction
        if regime.primary_regime == Regime.SWEEP_REVERSAL:
            reg_score = min(reg_score * 1.15, 1.0)
        breakdown["regime_conviction"] = round(reg_score, 3)

        # ── 2. Structure Alignment ────────────────────────────────────────────
        struct_score = 0.0
        if structure.in_sr_zone:
            struct_score += 0.4
        if structure.nearest_sr_distance_pct < 0.003:
            struct_score += 0.3
        if signal.direction == "long" and structure.structure_sequence == "HH_HL":
            struct_score += 0.3
        elif signal.direction == "short" and structure.structure_sequence == "LH_LL":
            struct_score += 0.3
        elif structure.structure_sequence == "MIXED":
            struct_score += 0.1
        struct_score += min(len(signal.confluence_factors) * 0.1, 0.3)
        struct_score = min(struct_score, 1.0)
        breakdown["structure_alignment"] = round(struct_score, 3)

        # ── 3. TF Confluence (strategy-weighted) ──────────────────────────────
        tf_score = 0.0
        tf_votes = regime.timeframe_alignment
        if tf_votes:
            weighted_aligned = 0.0
            total_tf_weight  = 0.0
            for tf, direction in tf_votes.items():
                w = tf_weights.get(tf, 0.20)
                total_tf_weight += w
                if ((signal.direction == "long"  and direction == "BULLISH") or
                        (signal.direction == "short" and direction == "BEARISH")):
                    weighted_aligned += w
            tf_score = weighted_aligned / max(total_tf_weight, 0.001)
        breakdown["tf_confluence"] = round(tf_score, 3)

        # ── 4. Liquidity Path Clear ───────────────────────────────────────────
        liq_score = 1.0
        pools_in_path = [
            p for p in liq_map.pools
            if not p.swept and (
                (signal.direction == "long" and
                 p.kind == "high" and
                 signal.entry_price < p.price < signal.target_1) or
                (signal.direction == "short" and
                 p.kind == "low" and
                 signal.target_1 < p.price < signal.entry_price)
            )
        ]
        liq_score -= len(pools_in_path) * 0.25
        liq_score = max(liq_score, 0.0)
        if liq_map.recent_sweep and liq_map.sweep_age_bars <= 6:
            sweep = liq_map.recent_sweep
            if ((sweep.kind == "low_sweep" and signal.direction == "long") or
                    (sweep.kind == "high_sweep" and signal.direction == "short")):
                liq_score = min(liq_score + 0.3, 1.0)
        breakdown["liquidity_clear"] = round(liq_score, 3)

        # ── 5. VWAP Context ───────────────────────────────────────────────────
        vwap_score = 0.5
        if vol_state.vwap > 0:
            if signal.direction == "long" and vol_state.price_vs_vwap == "ABOVE":
                vwap_score = 1.0
            elif signal.direction == "short" and vol_state.price_vs_vwap == "BELOW":
                vwap_score = 1.0
            else:
                vwap_score = 0.25
        breakdown["vwap_context"] = round(vwap_score, 3)

        # ── 6. Macro Alignment ────────────────────────────────────────────────
        macro_score = 0.5
        if macro:
            if macro.macro_context == "RISK_ON" and signal.direction == "long":
                macro_score = 1.0
            elif macro.macro_context == "RISK_OFF" and signal.direction == "short":
                macro_score = 1.0
            elif macro.macro_context == "NEUTRAL":
                macro_score = 0.5
            else:
                macro_score = 0.2
            if macro.vix_regime == "LOW":
                macro_score = min(macro_score + 0.1, 1.0)
            elif macro.vix_regime in ("ELEVATED", "CRISIS"):
                macro_score = max(macro_score - 0.2, 0.0)
        breakdown["macro_alignment"] = round(macro_score, 3)

        # ── Weighted Total ────────────────────────────────────────────────────
        total = sum(breakdown.get(dim, 0.5) * w for dim, w in weights.items())

        # ── Session quality modifier ───────────────────────────────────────────
        _, session_quality = current_session()
        if session_quality < 0.5:
            total = min(total, grade_a_min - 0.01)

        # ── Grade (strategy-specific thresholds) ──────────────────────────────
        if total >= grade_a_min:
            grade = "A"
        elif total >= grade_b_min:
            grade = "B"
        else:
            grade = "C"

        multiplier = GRADE_SIZE_MULTIPLIER[grade]

        result = SetupScore(
            grade=grade,
            score=round(total, 3),
            size_multiplier=multiplier,
            breakdown=breakdown
        )

        logger.info(
            f"Setup grade: {grade} (score={total:.2f}) "
            f"strategy={strategy_name} "
            f"multiplier={multiplier}x "
            f"breakdown={breakdown}"
        )
        return result


# Singleton
_scorer: Optional[SetupScorer] = None


def get_setup_scorer() -> SetupScorer:
    global _scorer
    if _scorer is None:
        _scorer = SetupScorer()
    return _scorer
