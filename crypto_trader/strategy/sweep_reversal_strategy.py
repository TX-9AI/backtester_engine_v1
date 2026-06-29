# crypto_trader/strategy/sweep_reversal_strategy.py — backtester_engine_v1
# v1.0 — original — Post-liquidity-sweep reversal strategy
# v1.1 — 2026-06-29 — Add: trend alignment filter using EMA9/21/50 on 1h and 15m
#                      Short sweeps blocked when BOTH 1h AND 15m show full bull EMA stack
#                      Long sweeps blocked when BOTH 1h AND 15m show full bear EMA stack
#                      Either TF neutral/opposite = reversal is valid (intraday catalyst)
#                      Trend context also added as confluence factor for signal quality

import logging
from typing import Optional

from strategy.base_strategy import BaseStrategy, TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap, LiquiditySweep
from config import ATR_STOP_MULTIPLIER, MIN_DAILY_RANGE_PCT, EMA_FAST, EMA_MID, EMA_SLOW
from utils.math_utils import ema_series

logger = logging.getLogger(__name__)


def _ema_direction(df) -> str:
    """
    Returns BULLISH, BEARISH, or NEUTRAL based on EMA9/21/50 stack.
    Full bull: price > EMA9 > EMA21 > EMA50
    Full bear: price < EMA9 < EMA21 < EMA50
    Anything else: NEUTRAL
    """
    if df is None or len(df) < EMA_SLOW + 5:
        return "NEUTRAL"
    try:
        closes = df["close"]
        price  = float(closes.iloc[-1])
        ema9   = float(ema_series(closes, EMA_FAST).iloc[-1])
        ema21  = float(ema_series(closes, EMA_MID).iloc[-1])
        ema50  = float(ema_series(closes, EMA_SLOW).iloc[-1])
        if price > ema9 > ema21 > ema50:
            return "BULLISH"
        if price < ema9 < ema21 < ema50:
            return "BEARISH"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


class SweepReversalStrategy(BaseStrategy):

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
        df_1h  = data.get("1h")

        if df_5m is None or len(df_5m) < 10:
            return None

        # Daily range filter
        df_1d = data.get("1d")
        if df_1d is not None and len(df_1d) >= 1:
            day_high  = float(df_1d["high"].iloc[-1])
            day_low   = float(df_1d["low"].iloc[-1])
            day_range = (day_high - day_low) / current_price if current_price > 0 else 0
            if day_range < MIN_DAILY_RANGE_PCT:
                logger.debug(f"SweepReversal skipped: daily range {day_range:.3%} too compressed")
                return None

        sweep = liq_map.recent_sweep
        if not sweep or not sweep.confirmed:
            return None

        atr = vol_state.atr_current
        if atr == 0:
            return None

        # Trend alignment filter — compute EMA direction on 1h and 15m
        dir_1h  = _ema_direction(df_1h)
        dir_15m = _ema_direction(df_15m)

        if sweep.kind == "high_sweep":
            # Block shorts when BOTH 1h and 15m are in full bull stack
            # One neutral/bearish = intraday reversal is valid
            if dir_1h == "BULLISH" and dir_15m == "BULLISH":
                logger.debug(
                    f"SweepReversal SHORT blocked: 1h={dir_1h} 15m={dir_15m} "
                    f"— sustained uptrend, high sweep is continuation not reversal"
                )
                return None

        elif sweep.kind == "low_sweep":
            # Block longs when BOTH 1h and 15m are in full bear stack
            if dir_1h == "BEARISH" and dir_15m == "BEARISH":
                logger.debug(
                    f"SweepReversal LONG blocked: 1h={dir_1h} 15m={dir_15m} "
                    f"— sustained downtrend, low sweep is continuation not reversal"
                )
                return None

        if sweep.kind == "low_sweep":
            return self._long_reversal(
                sweep, regime, vol_state, structure, liq_map,
                current_price, atr, df_5m, dir_1h, dir_15m
            )
        elif sweep.kind == "high_sweep":
            return self._short_reversal(
                sweep, regime, vol_state, structure, liq_map,
                current_price, atr, df_5m, dir_1h, dir_15m
            )

        return None

    def _long_reversal(self, sweep, regime, vol_state, structure,
                        liq_map, price, atr, df_5m,
                        dir_1h, dir_15m) -> Optional[TradeSignal]:

        if price <= sweep.pool_price:
            logger.debug("SweepReversal long: price not recovered above swept level yet")
            return None

        recovery_pct = (price - sweep.sweep_price) / sweep.sweep_price
        if recovery_pct > 0.02:
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

        self._add_confluence(signal, f"Low sweep confirmed ({sweep.rejection_pct:.1%} rejection)")

        if liq_map.sweep_age_bars <= 3:
            self._add_confluence(signal, "Fresh sweep (<=3 bars)")
        elif liq_map.sweep_age_bars <= 6:
            self._add_confluence(signal, "Recent sweep (<=6 bars)")

        if vol_state.vwap > 0 and price > vol_state.vwap:
            self._add_confluence(signal, "Recovered above VWAP")

        # Trend context as confluence — counter-trend or partial adds confidence
        if dir_1h in ("BEARISH", "NEUTRAL") and dir_15m in ("BEARISH", "NEUTRAL"):
            self._add_confluence(signal, f"Counter-trend reversal (1h={dir_1h} 15m={dir_15m})")
        elif dir_1h == "BEARISH" or dir_15m == "BEARISH":
            self._add_confluence(signal, f"Partial trend support (1h={dir_1h} 15m={dir_15m})")

        if sweep.swept_named_level:
            self._add_confluence(signal, f"Swept named level: {sweep.swept_named_level}")
        elif liq_map.prev_day_low and abs(sweep.pool_price - liq_map.prev_day_low) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "PDL swept")
        elif liq_map.asia_session_low and abs(sweep.pool_price - liq_map.asia_session_low) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "Asia session low swept")

        bullish_fvgs = [f for f in structure.fvgs if f.direction == "bullish" and not f.filled]
        if bullish_fvgs:
            self._add_confluence(signal, "Bullish FVG from sweep")

        if structure.nearest_support and abs(price - structure.nearest_support) / price < 0.005:
            self._add_confluence(signal, "At structure support")

        if regime.conviction >= 0.65:
            self._add_confluence(signal, f"High regime conviction ({regime.conviction:.0%})")

        if len(signal.confluence_factors) < 2:
            logger.debug("SweepReversal long: insufficient confluence")
            return None

        signal.entry_price = price
        signal.stop_price  = sweep.sweep_price - atr * 0.25
        risk = signal.entry_price - signal.stop_price

        if risk < atr * 0.3:
            signal.stop_price = price - atr * ATR_STOP_MULTIPLIER
            risk = signal.entry_price - signal.stop_price

        if structure.nearest_resistance and structure.nearest_resistance > price + risk * 0.75:
            signal.target_1 = min(structure.nearest_resistance * 0.998, price + risk * 2.0)
        else:
            signal.target_1 = price + risk * 1.5

        signal.target_2   = price + risk * 3.0
        signal.conviction = regime.conviction
        signal.notes = (f"Pool={sweep.pool_price:.0f} swept to {sweep.sweep_price:.0f} "
                        f"rejection={sweep.rejection_pct:.1%} "
                        f"age={liq_map.sweep_age_bars}bars "
                        f"1h={dir_1h} 15m={dir_15m}")

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"SweepReversal long: RRR {signal.rrr_1:.2f} insufficient")
            return None

        logger.info(f"SweepReversal LONG @ {price:.2f} "
                    f"stop={signal.stop_price:.2f} T1={signal.target_1:.2f} "
                    f"1h={dir_1h} 15m={dir_15m} confluence={signal.confluence_factors}")
        return signal

    def _short_reversal(self, sweep, regime, vol_state, structure,
                         liq_map, price, atr, df_5m,
                         dir_1h, dir_15m) -> Optional[TradeSignal]:

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
            self._add_confluence(signal, "Fresh sweep (<=3 bars)")
        elif liq_map.sweep_age_bars <= 6:
            self._add_confluence(signal, "Recent sweep (<=6 bars)")

        if vol_state.vwap > 0 and price < vol_state.vwap:
            self._add_confluence(signal, "Rejected below VWAP")

        # Trend context as confluence
        if dir_1h in ("BEARISH", "NEUTRAL") and dir_15m in ("BEARISH", "NEUTRAL"):
            self._add_confluence(signal, f"Counter-trend reversal (1h={dir_1h} 15m={dir_15m})")
        elif dir_1h == "BEARISH" or dir_15m == "BEARISH":
            self._add_confluence(signal, f"Partial trend support (1h={dir_1h} 15m={dir_15m})")

        if sweep.swept_named_level:
            self._add_confluence(signal, f"Swept named level: {sweep.swept_named_level}")
        elif liq_map.prev_day_high and abs(sweep.pool_price - liq_map.prev_day_high) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "PDH swept")
        elif liq_map.asia_session_high and abs(sweep.pool_price - liq_map.asia_session_high) / max(sweep.pool_price, 1) < 0.003:
            self._add_confluence(signal, "Asia session high swept")

        bearish_fvgs = [f for f in structure.fvgs if f.direction == "bearish" and not f.filled]
        if bearish_fvgs:
            self._add_confluence(signal, "Bearish FVG from sweep")

        if structure.nearest_resistance and abs(price - structure.nearest_resistance) / price < 0.005:
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
                        f"age={liq_map.sweep_age_bars}bars "
                        f"1h={dir_1h} 15m={dir_15m}")

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"SweepReversal short: RRR {signal.rrr_1:.2f} insufficient")
            return None

        logger.info(f"SweepReversal SHORT @ {price:.2f} "
                    f"stop={signal.stop_price:.2f} T1={signal.target_1:.2f} "
                    f"1h={dir_1h} 15m={dir_15m} confluence={signal.confluence_factors}")
        return signal

    def _minimum_rrr(self) -> float:
        return 1.8
