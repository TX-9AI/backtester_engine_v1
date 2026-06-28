"""
strategy/strategy_selector.py — Maps current regime to the optimal strategy.
Manages strategy transitions without whipsawing open trades.
"""

import logging
from typing import Optional, Dict, Type
from datetime import datetime

from strategy.base_strategy import BaseStrategy, TradeSignal
from strategy.momentum_strategy import MomentumStrategy
from strategy.mean_reversion_strategy import MeanReversionStrategy
from strategy.sweep_reversal_strategy import SweepReversalStrategy
from strategy.compression_scalp_strategy import CompressionScalpStrategy
from strategy.diagnostic_strategy import DiagnosticStrategy
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from utils.time_utils import now_utc, fmt_et_short

logger = logging.getLogger(__name__)


# Registry: regime → strategy class
REGIME_STRATEGY_MAP: Dict[str, Type[BaseStrategy]] = {
    Regime.TRENDING_BULL:     MomentumStrategy,
    Regime.TRENDING_BEAR:     MomentumStrategy,
    Regime.RANGING:           MeanReversionStrategy,
    Regime.SWEEP_REVERSAL:    SweepReversalStrategy,
    Regime.COMPRESSION:       CompressionScalpStrategy,
    Regime.BREAKOUT_VOLATILE: MomentumStrategy,   # Widen stops, higher bar
    Regime.UNKNOWN:           MeanReversionStrategy,  # Defensive default
}


class StrategySelector:
    """
    Selects the active strategy based on current regime.
    Maintains strategy instances to avoid reinitializing each tick.
    Handles regime transitions gracefully — no forced exits from regime change.
    """

    def __init__(self):
        # Strategy instances (reused across ticks)
        self._strategies: Dict[str, BaseStrategy] = {
            "Momentum":          MomentumStrategy(),
            "MeanReversion":     MeanReversionStrategy(),
            "SweepReversal":     SweepReversalStrategy(),
            "CompressionScalp":  CompressionScalpStrategy(),
        }

        self._active_strategy: Optional[BaseStrategy] = None
        self._active_regime:   Optional[str]          = None
        self._strategy_since:  Optional[datetime]     = None
        self._transition_count: int                   = 0

    def select(self, regime: RegimeState) -> BaseStrategy:
        """
        Return the appropriate strategy for the current regime.
        Logs transitions when regime changes.
        In diagnostic mode, always returns DiagnosticStrategy.
        """
        try:
            from config import DIAGNOSTIC_MODE
            if DIAGNOSTIC_MODE:
                if "Diagnostic" not in self._strategies:
                    self._strategies["Diagnostic"] = DiagnosticStrategy()
                return self._strategies["Diagnostic"]
        except ImportError:
            pass

        target_class = REGIME_STRATEGY_MAP.get(
            regime.primary_regime,
            MeanReversionStrategy  # Safe default
        )

        # Find matching instance
        target = next(
            (s for s in self._strategies.values()
             if isinstance(s, target_class)),
            list(self._strategies.values())[0]
        )

        # Log regime transition
        if (self._active_strategy is None or
                type(self._active_strategy) != type(target)):

            prev_name = self._active_strategy.name if self._active_strategy else "None"
            logger.info(
                f"STRATEGY TRANSITION: {prev_name} → {target.name} "
                f"(regime: {regime.primary_regime} "
                f"conviction={regime.conviction:.2f}) @ {fmt_et_short()}"
            )
            self._transition_count += 1
            self._strategy_since = now_utc()

        self._active_strategy = target
        self._active_regime   = regime.primary_regime

        return target

    def generate_signal(self,
                        regime:        RegimeState,
                        vol_state:     VolatilityState,
                        structure:     StructureMap,
                        liq_map:       LiquidityMap,
                        data:          dict,
                        current_price: float) -> Optional[TradeSignal]:
        """
        Select strategy and generate a signal in one call.
        Returns None if no valid signal.
        """
        strategy = self.select(regime)

        # Conviction gate — don't trade low-conviction regimes
        if regime.conviction < 0.35:
            logger.debug(f"Regime conviction too low ({regime.conviction:.2f}), no signal")
            return None

        signal = strategy.generate_signal(
            regime, vol_state, structure, liq_map, data, current_price
        )

        if signal:
            signal.regime = regime.primary_regime

        return signal

    @property
    def active_strategy_name(self) -> str:
        return self._active_strategy.name if self._active_strategy else "None"

    @property
    def active_regime(self) -> str:
        return self._active_regime or "UNKNOWN"

    def is_orphaned_regime(self, current_regime: str) -> bool:
        """
        True if the regime has changed since the current trade was entered.
        Used by exit_engine to tighten trailing stops on orphaned trades.
        """
        return (self._active_regime is not None and
                current_regime != self._active_regime)

    def strategy_age_minutes(self) -> float:
        """How long has current strategy been active?"""
        if not self._strategy_since:
            return 0
        return (now_utc() - self._strategy_since).total_seconds() / 60


# Module-level singleton
_selector: Optional[StrategySelector] = None


def get_strategy_selector() -> StrategySelector:
    global _selector
    if _selector is None:
        _selector = StrategySelector()
    return _selector
