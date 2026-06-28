"""
strategy/momentum_strategy.py — Momentum/continuation strategy.
Used in TRENDING_BULL, TRENDING_BEAR, and BREAKOUT_VOLATILE regimes.

v1.2: Principled adaptive stop sizing
- Stop distance computed BEFORE position sizing so dollar risk is always fixed
- ADX scaling widens the stop (and shrinks size proportionally) — 1R stays 1R
- Minimum stop floor = max(ATR × 1.5, price × 0.003) — instrument-agnostic noise floor
- Works correctly for BTC, ETH, SOL, NQ, Silver, any instrument
"""

import logging
from typing import Optional
import pandas as pd

from strategy.base_strategy import BaseStrategy, TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from config import EMA_FAST, EMA_MID, EMA_SLOW, ATR_STOP_MULTIPLIER
from utils.math_utils import ema_series, atr_series, r_multiple

logger = logging.getLogger(__name__)


def adx_stop_multiplier(adx: float) -> float:
    """Scale ATR stop multiplier based on ADX strength."""
    if adx > 60:
        return ATR_STOP_MULTIPLIER * 2.5
    elif adx > 40:
        return ATR_STOP_MULTIPLIER * 2.0
    elif adx > 25:
        return ATR_STOP_MULTIPLIER * 1.5
    else:
        return ATR_STOP_MULTIPLIER * 1.0


def adaptive_min_stop(price: float, atr: float, adx_mult: float) -> float:
    """
    Instrument-agnostic minimum stop distance.

    Two floors, take the larger:
    1. ATR-based: atr × adx_multiplier — scales with instrument volatility
    2. Price-based: price × 0.003 (0.3%) — noise floor as % of price

    This ensures:
    - SOL at $69: min stop = max($0.20, $0.21) = $0.21
    - BTC at $63k: min stop = max($285, $189) = $285
    - NQ at $19k:  min stop = max($57, $57) = $57
    - Silver at $32: min stop = max($0.48, $0.10) = $0.48

    Dollar risk stays fixed because position size = risk_usd / stop_distance.
    Wider stop → smaller position → same dollar loss if stopped out.
    """
    atr_floor   = atr * adx_mult
    price_floor = price * 0.003
    return max(atr_floor, price_floor)


class MomentumStrategy(BaseStrategy):
    """
    Enters in the direction of the trend on pullbacks to EMA or structure.
    Stop distance computed adaptively — wider in strong trends and volatile
    instruments — but dollar risk is always fixed at 1R.
    """

    @property
    def name(self) -> str:
        return "Momentum"

    def is_applicable(self, regime: RegimeState) -> bool:
        return regime.primary_regime in (
            Regime.TRENDING_BULL,
            Regime.TRENDING_BEAR,
            Regime.BREAKOUT_VOLATILE
        )

    def generate_signal(self, regime: RegimeState, vol_state: VolatilityState,
                        structure: StructureMap, liq_map: LiquidityMap,
                        data: dict, current_price: float) -> Optional[TradeSignal]:

        df_5m = data.get("5m")
        if df_5m is None or len(df_5m) < 60:
            return None

        direction = "long" if regime.is_bullish else "short"
        atr = vol_state.atr_current
        if atr == 0:
            return None

        closes   = df_5m["close"]
        ema_fast = float(ema_series(closes, EMA_FAST).iloc[-1])
        ema_mid  = float(ema_series(closes, EMA_MID).iloc[-1])
        ema_slow = float(ema_series(closes, EMA_SLOW).iloc[-1])

        adx       = regime.adx
        stop_mult = adx_stop_multiplier(adx)
        min_stop  = adaptive_min_stop(current_price, atr, stop_mult)

        signal = TradeSignal(
            direction=direction,
            strategy_name=self.name,
            regime=regime.primary_regime,
            atr=atr,
            vwap=vol_state.vwap,
        )

        if direction == "long":
            setup = self._check_long_setup(
                signal, regime, vol_state, structure, liq_map,
                current_price, ema_fast, ema_mid, ema_slow, atr, df_5m,
                stop_mult, min_stop
            )
        else:
            setup = self._check_short_setup(
                signal, regime, vol_state, structure, liq_map,
                current_price, ema_fast, ema_mid, ema_slow, atr, df_5m,
                stop_mult, min_stop
            )

        if not setup or not signal.is_valid:
            return None

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            logger.debug(f"Momentum: RRR too low ({signal.rrr_1:.2f}), skipping")
            return None

        stop_dist = abs(signal.entry_price - signal.stop_price)
        stop_pct  = stop_dist / current_price * 100
        logger.info(f"Momentum signal: {direction} entry={signal.entry_price:.4f} "
                    f"stop={signal.stop_price:.4f} stop_dist={stop_dist:.4f} ({stop_pct:.2f}%) "
                    f"T1={signal.target_1:.4f} RRR={signal.rrr_1:.2f} "
                    f"ADX={adx:.1f} mult={stop_mult:.1f}x "
                    f"confluence={len(signal.confluence_factors)}")
        return signal

    def _check_long_setup(self, signal, regime, vol_state, structure, liq_map,
                           price, ema_fast, ema_mid, ema_slow, atr, df_5m,
                           stop_mult, min_stop) -> bool:

        pullback_to_fast = (price <= ema_fast * 1.005 and
                            price >= ema_fast * 0.997 and
                            ema_fast > ema_slow)
        pullback_to_mid  = (price <= ema_mid * 1.005 and
                            price >= ema_mid * 0.996 and
                            ema_fast > ema_slow)
        at_support = (structure.nearest_support is not None and
                      abs(price - structure.nearest_support) / price < 0.003 and
                      regime.is_bullish)

        if not any([pullback_to_fast, pullback_to_mid, at_support]):
            return False

        if pullback_to_fast: self._add_confluence(signal, "Pullback to EMA9")
        if pullback_to_mid:  self._add_confluence(signal, "Pullback to EMA21")
        if at_support:       self._add_confluence(signal, "At structure support")
        if vol_state.price_vs_vwap == "ABOVE":
            self._add_confluence(signal, "Above VWAP")
        if regime.structure_sequence == "HH_HL":
            self._add_confluence(signal, "HH/HL structure")
        if regime.trend_conviction > 0.6:
            self._add_confluence(signal, "High trend conviction")
        if liq_map.prev_day_low and abs(price - liq_map.prev_day_low) / price < 0.005:
            self._add_confluence(signal, "At PDL support")
        if liq_map.asia_session_low and abs(price - liq_map.asia_session_low) / price < 0.003:
            self._add_confluence(signal, "At Asia session low")

        if len(signal.confluence_factors) < 2:
            return False

        signal.entry_price = price
        signal.setup_type  = "Momentum Long — Pullback"

        # Compute stop: start from recent low, then enforce adaptive floor
        recent_low = float(df_5m["low"].iloc[-10:].min())
        raw_stop   = recent_low - (atr * 0.3)
        raw_dist   = price - raw_stop

        # Enforce minimum stop — this is the key fix
        # Stop distance is set BEFORE sizing so dollar risk stays fixed
        stop_dist         = max(raw_dist, min_stop)
        signal.stop_price = price - stop_dist

        risk = stop_dist
        if structure.nearest_resistance and structure.nearest_resistance > price + risk:
            signal.target_1 = structure.nearest_resistance * 0.998
        else:
            signal.target_1 = price + risk * 1.5

        signal.target_2   = price + risk * 2.5
        signal.conviction = regime.conviction * 0.8 + regime.trend_conviction * 0.2
        signal.notes      = (f"EMA: {ema_fast:.2f}/{ema_mid:.2f}/{ema_slow:.2f} "
                             f"ADX={regime.adx:.1f} mult={stop_mult:.1f}x "
                             f"min_stop={min_stop:.4f}")
        return True

    def _check_short_setup(self, signal, regime, vol_state, structure, liq_map,
                            price, ema_fast, ema_mid, ema_slow, atr, df_5m,
                            stop_mult, min_stop) -> bool:

        bounce_to_fast = (price >= ema_fast * 0.997 and
                          price <= ema_fast * 1.005 and
                          ema_fast < ema_slow)
        bounce_to_mid  = (price >= ema_mid * 0.997 and
                          price <= ema_mid * 1.006 and
                          ema_fast < ema_slow)
        at_resistance = (structure.nearest_resistance is not None and
                         abs(price - structure.nearest_resistance) / price < 0.003 and
                         regime.is_bearish)

        if not any([bounce_to_fast, bounce_to_mid, at_resistance]):
            return False

        if bounce_to_fast: self._add_confluence(signal, "Bounce to EMA9 in downtrend")
        if bounce_to_mid:  self._add_confluence(signal, "Bounce to EMA21 in downtrend")
        if at_resistance:  self._add_confluence(signal, "At structure resistance")
        if vol_state.price_vs_vwap == "BELOW":
            self._add_confluence(signal, "Below VWAP")
        if regime.structure_sequence == "LH_LL":
            self._add_confluence(signal, "LH/LL structure")
        if regime.trend_conviction > 0.6:
            self._add_confluence(signal, "High trend conviction")
        if liq_map.prev_day_high and abs(price - liq_map.prev_day_high) / price < 0.005:
            self._add_confluence(signal, "At PDH resistance")
        if liq_map.asia_session_high and abs(price - liq_map.asia_session_high) / price < 0.003:
            self._add_confluence(signal, "At Asia session high")

        if len(signal.confluence_factors) < 2:
            return False

        signal.entry_price = price
        signal.setup_type  = "Momentum Short — Bounce"

        recent_high = float(df_5m["high"].iloc[-10:].max())
        raw_stop    = recent_high + (atr * 0.3)
        raw_dist    = raw_stop - price

        # Enforce minimum stop — dollar risk stays fixed
        stop_dist         = max(raw_dist, min_stop)
        signal.stop_price = price + stop_dist

        risk = stop_dist
        if structure.nearest_support and structure.nearest_support < price - risk:
            signal.target_1 = structure.nearest_support * 1.002
        else:
            signal.target_1 = price - risk * 1.5

        signal.target_2   = price - risk * 2.5
        signal.conviction = regime.conviction * 0.8 + regime.trend_conviction * 0.2
        signal.notes      = (f"EMA: {ema_fast:.2f}/{ema_mid:.2f}/{ema_slow:.2f} "
                             f"ADX={regime.adx:.1f} mult={stop_mult:.1f}x "
                             f"min_stop={min_stop:.4f}")
        return True
