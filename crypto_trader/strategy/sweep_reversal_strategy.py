"""
strategy/sweep_reversal_strategy.py — Post-liquidity-sweep reversal strategy.
BTC's most reliable pattern: sweep stops, reverse sharply, leave imbalance.
This is the highest-conviction setup in the system.
"""

import logging
from typing import Optional

from strategy.base_strategy import BaseStrategy, TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap, LiquiditySweep
from config import ATR_STOP_MULTIPLIER, MIN_DAILY_RANGE_PCT

logger = logging.getLogger(__name__)


class SweepReversalStrategy(BaseStrategy):
    """
    After a confirmed liquidity sweep (equal highs/lows taken out then
    sharply rejected), enter in the opposite direction.

    Long setup: lows swept → sharp recovery → enter long
    Short setup: highs swept → sharp rejection → enter short

    This is THE highest-probability BTC pattern. Institutions take stops,
    fill their books, then reverse. We follow the reversal.

    Entry timing: Enter on first 5m candle that closes BACK INSIDE the
    swept level or on the retest of the sweep candle's close.
    """

    @property
    def name(self) -> str:
        return "SweepReversal"

    def is_applicable(self, regime: RegimeState) -> bool:
        return regime.primary_regime == Regime.SWEEP_REVERSAL

    def generate_signal(self, regime: RegimeState, vol_state: VolatilityState,
                        structure: StructureMap, liq_map: LiquidityMap,
                        data: dict, current_price: float) -> Optional[TradeSignal]:

        df_5m  = data.get("5m")
        df_15m = data.get("15m")

        if df_5m is None or len(df_5m) < 10:
            return None

        # ── Daily range filter ───────────────────────────────────────────────
        # Don't trade sweeps in compressed markets where fees eat the profit
        df_1d = data.get("1d")
        if df_1d is not None and len(df_1d) >= 1:
            day_high  = float(df_1d["high"].iloc[-1])
            day_low   = float(df_1d["low"].iloc[-1])
            day_range = (day_high - day_low) / current_price if current_price > 0 else 0
            if day_range < MIN_DAILY_RANGE_PCT:
                logger.debug(
                    f"SweepReversal skipped: daily range {day_range:.3%} "
                    f"< min {MIN_DAILY_RANGE_PCT:.3%} (range_too_compressed)"
                )
                return None

        sweep = liq_map.recent_sweep
        if not sweep or not sweep.confirmed:
            return None

        atr = vol_state.atr_current
        if atr == 0:
            return None

        # Determine reversal direction based on sweep type
        if sweep.kind == "low_sweep":
            return self._long_reversal(
                sweep, regime, vol_state, structure, liq_map,
                current_price, atr, df_5m
            )
        elif sweep.kind == "high_sweep":
            return self._short_reversal(
                sweep, regime, vol_state, structure, liq_map,
                current_price, atr, df_5m
            )

        return None

    def _long_reversal(self, sweep: LiquiditySweep, regime: RegimeState,
                        vol_state: VolatilityState, structure: StructureMap,
                        liq_map: LiquidityMap, price: float,
                        atr: float, df_5m) -> Optional[TradeSignal]:
        """
        Low sweep reversal — enter long after lows taken, sharp recovery.
        """
        # Price must have recovered back above the swept level
        if price <= sweep.pool_price:
            logger.debug("SweepReversal long: price not recovered above swept level yet")
            return None

        # Price shouldn't be too far from the sweep — want early entry
        recovery_pct = (price - sweep.sweep_price) / sweep.sweep_price
        if recovery_pct > 0.02:  # More than 2% away — too late
            logger.debug(f"SweepReversal long: price too far from sweep ({recovery_pct:.1%})")
            return None

        signal = TradeSignal(
            direction="long",
            strategy_name=self.name,
            setup_type="Sweep Reversal Long — Low Sweep",
            regime=regime.primary_regime,
            atr=atr,
            vwap=vol_state.vwap,
        )

        # ── Confluence Factors ────────────────────────────────────────────────
        self._add_confluence(signal, f"Low sweep confirmed ({sweep.rejection_pct:.1%} rejection)")

        if liq_map.sweep_age_bars <= 3:
            self._add_confluence(signal, "Fresh sweep (≤3 bars)")
        elif liq_map.sweep_age_bars <= 6:
            self._add_confluence(signal, "Recent sweep (≤6 bars)")

        if vol_state.vwap > 0 and price > vol_state.vwap:
            self._add_confluence(signal, "Recovered above VWAP")

        # Named level boost — sweeps of PDL/session lows are highest quality
        if sweep.swept_named_level:
            self._add_confluence(signal, f"Swept named level: {sweep.swept_named_level}")
        elif liq_map.prev_day_low and abs(sweep.pool_price - liq_map.prev_day_low) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "PDL swept")
        elif liq_map.asia_session_low and abs(sweep.pool_price - liq_map.asia_session_low) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "Asia session low swept")

        # FVG left behind from the sweep move
        bullish_fvgs = [f for f in structure.fvgs if f.direction == "bullish"
                        and not f.filled]
        if bullish_fvgs:
            self._add_confluence(signal, "Bullish FVG from sweep")

        if structure.nearest_support and abs(price - structure.nearest_support) / price < 0.005:
            self._add_confluence(signal, "At structure support")

        if regime.conviction >= 0.65:
            self._add_confluence(signal, f"High regime conviction ({regime.conviction:.0%})")

        # Minimum 2 factors beyond the sweep itself
        if len(signal.confluence_factors) < 2:
            logger.debug("SweepReversal long: insufficient confluence")
            return None

        # ── Entry, Stop, Targets ──────────────────────────────────────────────
        signal.entry_price = price

        # Stop: below the sweep extreme with small buffer
        signal.stop_price = sweep.sweep_price - atr * 0.25
        risk = signal.entry_price - signal.stop_price

        if risk < atr * 0.3:
            signal.stop_price = price - atr * ATR_STOP_MULTIPLIER
            risk = signal.entry_price - signal.stop_price

        # T1: nearest resistance or 1.5R (whichever is closer and meaningful)
        if structure.nearest_resistance and structure.nearest_resistance > price + risk * 0.75:
            signal.target_1 = min(structure.nearest_resistance * 0.998, price + risk * 2.0)
        else:
            signal.target_1 = price + risk * 1.5

        # T2: extended target — sweep reversals often run hard
        signal.target_2   = price + risk * 3.0
        signal.conviction = regime.conviction
        signal.notes = (f"Pool={sweep.pool_price:.0f} swept to {sweep.sweep_price:.0f} "
                        f"rejection={sweep.rejection_pct:.1%} "
                        f"age={liq_map.sweep_age_bars}bars")

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"SweepReversal long: RRR {signal.rrr_1:.2f} insufficient")
            return None

        logger.info(f"🔥 SweepReversal LONG @ {price:.2f} "
                    f"stop={signal.stop_price:.2f} T1={signal.target_1:.2f} "
                    f"T2={signal.target_2:.2f} confluence={signal.confluence_factors}")
        return signal

    def _short_reversal(self, sweep: LiquiditySweep, regime: RegimeState,
                         vol_state: VolatilityState, structure: StructureMap,
                         liq_map: LiquidityMap, price: float,
                         atr: float, df_5m) -> Optional[TradeSignal]:
        """
        High sweep reversal — enter short after highs taken, sharp rejection.
        """
        if price >= sweep.pool_price:
            logger.debug("SweepReversal short: price not rejected below swept level yet")
            return None

        recovery_pct = (sweep.sweep_price - price) / sweep.sweep_price
        if recovery_pct > 0.02:
            logger.debug(f"SweepReversal short: price too far from sweep ({recovery_pct:.1%})")
            return None

        signal = TradeSignal(
            direction="short",
            strategy_name=self.name,
            setup_type="Sweep Reversal Short — High Sweep",
            regime=regime.primary_regime,
            atr=atr,
            vwap=vol_state.vwap,
        )

        self._add_confluence(signal, f"High sweep confirmed ({sweep.rejection_pct:.1%} rejection)")

        if liq_map.sweep_age_bars <= 3:
            self._add_confluence(signal, "Fresh sweep (≤3 bars)")
        elif liq_map.sweep_age_bars <= 6:
            self._add_confluence(signal, "Recent sweep (≤6 bars)")

        if vol_state.vwap > 0 and price < vol_state.vwap:
            self._add_confluence(signal, "Rejected below VWAP")

        # Named level boost — sweeps of PDH/session highs are highest quality
        if sweep.swept_named_level:
            self._add_confluence(signal, f"Swept named level: {sweep.swept_named_level}")
        elif liq_map.prev_day_high and abs(sweep.pool_price - liq_map.prev_day_high) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "PDH swept")
        elif liq_map.asia_session_high and abs(sweep.pool_price - liq_map.asia_session_high) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "Asia session high swept")

        bearish_fvgs = [f for f in structure.fvgs if f.direction == "bearish"
                        and not f.filled]
        if bearish_fvgs:
            self._add_confluence(signal, "Bearish FVG from sweep")

        if (structure.nearest_resistance and
                abs(price - structure.nearest_resistance) / price < 0.005):
            self._add_confluence(signal, "At structure resistance")

        if regime.conviction >= 0.65:
            self._add_confluence(signal, f"High regime conviction ({regime.conviction:.0%})")

        if len(signal.confluence_factors) < 2:
            logger.debug("SweepReversal short: insufficient confluence")
            return None

        signal.entry_price = price
        signal.stop_price  = sweep.sweep_price + atr * 0.25
        risk               = signal.stop_price - signal.entry_price

        if risk < atr * 0.3:
            signal.stop_price = price + atr * ATR_STOP_MULTIPLIER
            risk = signal.stop_price - signal.entry_price

        if structure.nearest_support and structure.nearest_support < price - risk * 0.75:
            signal.target_1 = max(structure.nearest_support * 1.002, price - risk * 2.0)
        else:
            signal.target_1 = price - risk * 1.5

        signal.target_2   = price - risk * 3.0
        signal.conviction = regime.conviction
        signal.notes = (f"Pool={sweep.pool_price:.0f} swept to {sweep.sweep_price:.0f} "
                        f"rejection={sweep.rejection_pct:.1%} "
                        f"age={liq_map.sweep_age_bars}bars")

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"SweepReversal short: RRR {signal.rrr_1:.2f} insufficient")
            return None

        logger.info(f"🔥 SweepReversal SHORT @ {price:.2f} "
                    f"stop={signal.stop_price:.2f} T1={signal.target_1:.2f} "
                    f"T2={signal.target_2:.2f} confluence={signal.confluence_factors}")
        return signal

    def _minimum_rrr(self) -> float:
        """Sweep reversals should have at least 1.8R — they run fast and far."""
        return 1.8
