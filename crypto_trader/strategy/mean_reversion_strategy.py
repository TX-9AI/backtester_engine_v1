"""
strategy/mean_reversion_strategy.py — Mean reversion in ranging markets.
Fades extremes: sells Bollinger Band upper, buys lower.
Only activates in confirmed RANGING regime with defined S/R.
"""

import logging
from typing import Optional
import pandas as pd

from strategy.base_strategy import BaseStrategy, TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from config import ATR_STOP_MULTIPLIER, MIN_DAILY_RANGE_PCT
from utils.math_utils import bollinger_bands

logger = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    In range-bound markets:
    - Buy at lower BB/support confluence, target middle (VWAP) → upper BB/resistance
    - Sell at upper BB/resistance confluence, target middle → lower BB/support

    Requirements:
    - Price at BB extreme (top/bottom 10%)
    - Confirmed S/R level at or near the extreme
    - ADX confirming low (ranging)
    - No major liquidity pool directly beyond entry
    """

    @property
    def name(self) -> str:
        return "MeanReversion"

    def is_applicable(self, regime: RegimeState) -> bool:
        return regime.primary_regime == Regime.RANGING

    def generate_signal(self, regime: RegimeState, vol_state: VolatilityState,
                        structure: StructureMap, liq_map: LiquidityMap,
                        data: dict, current_price: float) -> Optional[TradeSignal]:

        df_5m = data.get("5m")
        if df_5m is None or len(df_5m) < 25:
            return None

        # ── Daily range filter ───────────────────────────────────────────────
        df_1d = data.get("1d")
        if df_1d is not None and len(df_1d) >= 1:
            day_high  = float(df_1d["high"].iloc[-1])
            day_low   = float(df_1d["low"].iloc[-1])
            day_range = (day_high - day_low) / current_price if current_price > 0 else 0
            if day_range < MIN_DAILY_RANGE_PCT:
                logger.debug(
                    f"MeanReversion skipped: daily range {day_range:.3%} "
                    f"< min {MIN_DAILY_RANGE_PCT:.3%} (range_too_compressed)"
                )
                return None

        atr = vol_state.atr_current
        if atr == 0:
            return None

        # Need defined range: both support and resistance must exist
        if structure.nearest_support is None or structure.nearest_resistance is None:
            logger.debug("MeanReversion: no defined S/R range, skipping")
            return None

        range_size = structure.nearest_resistance - structure.nearest_support
        if range_size < atr * 2:
            logger.debug("MeanReversion: range too narrow for viable trade")
            return None

        # Price position within the range
        range_position = (current_price - structure.nearest_support) / range_size
        # 0.0 = at support, 1.0 = at resistance

        signal = TradeSignal(
            strategy_name=self.name,
            regime=regime.primary_regime,
            atr=atr,
            vwap=vol_state.vwap,
        )

        # ── LONG at Range Bottom ───────────────────────────────────────────────
        if range_position <= 0.20:  # Bottom 20% of range
            return self._long_at_range_bottom(
                signal, regime, vol_state, structure, liq_map,
                current_price, atr, range_size, df_5m
            )

        # ── SHORT at Range Top ────────────────────────────────────────────────
        elif range_position >= 0.80:  # Top 20% of range
            return self._short_at_range_top(
                signal, regime, vol_state, structure, liq_map,
                current_price, atr, range_size, df_5m
            )

        # In the middle of the range — no edge, skip
        logger.debug(f"MeanReversion: price in middle of range ({range_position:.0%}), no signal")
        return None

    def _long_at_range_bottom(self, signal, regime, vol_state, structure,
                               liq_map, price, atr, range_size, df_5m) -> Optional[TradeSignal]:
        """Buy at range support with BB lower band confluence."""

        signal.direction  = "long"
        signal.setup_type = "Mean Reversion Long — Range Bottom"

        # Confluence factors
        at_support = (abs(price - structure.nearest_support) / price < 0.003)
        at_bb_low  = (vol_state.bb_lower > 0 and
                      price <= vol_state.bb_lower * 1.002)
        below_vwap_extreme = (vol_state.vwap > 0 and
                               price < vol_state.vwap * 0.995)

        if at_support:
            self._add_confluence(signal, "At defined support level")
        if at_bb_low:
            self._add_confluence(signal, "At Bollinger Band lower")
        if below_vwap_extreme:
            self._add_confluence(signal, "Extended below VWAP")

        # Look for bullish reversal candle in last 3 bars
        if len(df_5m) >= 3:
            last = df_5m.iloc[-1]
            if float(last["close"]) > float(last["open"]):
                self._add_confluence(signal, "Bullish close at support")

        if len(signal.confluence_factors) < 2:
            return None

        # Check: no unswept low below support (would eat our stop)
        if liq_map.near_pool_below is not None:
            logger.debug("MeanReversion long: unswept pool below support, skip")
            return None

        # Prices
        signal.entry_price = price
        signal.stop_price  = structure.nearest_support - atr * 0.5
        risk               = signal.entry_price - signal.stop_price

        if risk < atr * 0.4:  # Minimum meaningful risk
            signal.stop_price = price - atr * ATR_STOP_MULTIPLIER
            risk = signal.entry_price - signal.stop_price

        # Target: VWAP (middle of range) then resistance
        signal.target_1 = vol_state.vwap if vol_state.vwap > price else (price + risk * 1.5)
        signal.target_2 = structure.nearest_resistance * 0.998
        signal.conviction = regime.conviction * 0.7

        signal.notes = (f"Range {structure.nearest_support:.0f} – "
                        f"{structure.nearest_resistance:.0f} "
                        f"({range_size:.0f} pts wide)")

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"MeanReversion long: RRR {signal.rrr_1:.2f} too low")
            return None

        logger.info(f"MeanReversion LONG @ {price:.2f} stop={signal.stop_price:.2f} "
                    f"T1={signal.target_1:.2f} RRR={signal.rrr_1:.2f}")
        return signal

    def _short_at_range_top(self, signal, regime, vol_state, structure,
                             liq_map, price, atr, range_size, df_5m) -> Optional[TradeSignal]:
        """Sell at range resistance with BB upper band confluence."""

        signal.direction  = "short"
        signal.setup_type = "Mean Reversion Short — Range Top"

        at_resistance = (abs(price - structure.nearest_resistance) / price < 0.003)
        at_bb_high    = (vol_state.bb_upper > 0 and
                         price >= vol_state.bb_upper * 0.998)
        above_vwap_extreme = (vol_state.vwap > 0 and
                               price > vol_state.vwap * 1.005)

        if at_resistance:
            self._add_confluence(signal, "At defined resistance level")
        if at_bb_high:
            self._add_confluence(signal, "At Bollinger Band upper")
        if above_vwap_extreme:
            self._add_confluence(signal, "Extended above VWAP")

        if len(df_5m) >= 3:
            last = df_5m.iloc[-1]
            if float(last["close"]) < float(last["open"]):
                self._add_confluence(signal, "Bearish close at resistance")

        if len(signal.confluence_factors) < 2:
            return None

        if liq_map.near_pool_above is not None:
            logger.debug("MeanReversion short: unswept pool above resistance, skip")
            return None

        signal.entry_price = price
        signal.stop_price  = structure.nearest_resistance + atr * 0.5
        risk               = signal.stop_price - signal.entry_price

        if risk < atr * 0.4:
            signal.stop_price = price + atr * ATR_STOP_MULTIPLIER
            risk = signal.stop_price - signal.entry_price

        signal.target_1 = vol_state.vwap if vol_state.vwap < price else (price - risk * 1.5)
        signal.target_2 = structure.nearest_support * 1.002
        signal.conviction = regime.conviction * 0.7

        signal.notes = (f"Range {structure.nearest_support:.0f} – "
                        f"{structure.nearest_resistance:.0f} "
                        f"({range_size:.0f} pts wide)")

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"MeanReversion short: RRR {signal.rrr_1:.2f} too low")
            return None

        logger.info(f"MeanReversion SHORT @ {price:.2f} stop={signal.stop_price:.2f} "
                    f"T1={signal.target_1:.2f} RRR={signal.rrr_1:.2f}")
        return signal

    def _minimum_rrr(self) -> float:
        """Mean reversion needs at least 1.3R — range trades have lower extensions."""
        return 1.3
