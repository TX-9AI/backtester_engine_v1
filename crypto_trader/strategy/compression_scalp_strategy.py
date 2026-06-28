"""
strategy/compression_scalp_strategy.py — Compression/squeeze scalp strategy.
When BB is in a tight squeeze and ATR is contracting, the next move is often
explosive. This strategy either:
1. Trades the breakout direction as soon as it shows conviction, OR
2. Fades false breakouts for mean reversion back to center

Conservative sizing — compression can resolve either direction.
"""

import logging
from typing import Optional

from strategy.base_strategy import BaseStrategy, TradeSignal
from analysis.regime_classifier import RegimeState, Regime
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from config import ATR_STOP_MULTIPLIER
from utils.math_utils import ema_series
from strategy.momentum_strategy import adaptive_min_stop

logger = logging.getLogger(__name__)


class CompressionScalpStrategy(BaseStrategy):
    """
    In compression regimes, two setups:

    1. BREAKOUT SCALP: First candle that closes convincingly outside BB bands
       with volume expansion. Enter in breakout direction.
       Stop: back inside the band. Target: 1.5x the band width.

    2. FALSE BREAKOUT FADE: Price spikes outside band then immediately
       closes back inside. Enter opposite direction.
       Stop: just beyond the spike high/low. Target: VWAP / mid-band.
    """

    @property
    def name(self) -> str:
        return "CompressionScalp"

    def is_applicable(self, regime: RegimeState) -> bool:
        return regime.primary_regime == Regime.COMPRESSION

    def generate_signal(self, regime: RegimeState, vol_state: VolatilityState,
                        structure: StructureMap, liq_map: LiquidityMap,
                        data: dict, current_price: float) -> Optional[TradeSignal]:

        df_5m = data.get("5m")
        if df_5m is None or len(df_5m) < 25:
            return None

        atr = vol_state.atr_current
        if atr == 0:
            return None

        # Can't trade if bands aren't calculated
        if vol_state.bb_upper == 0 or vol_state.bb_lower == 0:
            return None

        band_width = vol_state.bb_upper - vol_state.bb_lower
        band_mid   = vol_state.bb_middle

        # Entry filter: only trade compression when BB width is genuinely tight
        # Calculate BB width as % of price — must be below 1.5% to enter
        # This prevents CompressionScalp from firing in normal ranging markets
        bb_width_pct = band_width / current_price if current_price > 0 else 1.0
        if bb_width_pct > 0.020:  # 2.0% threshold — above this is ranging, not compression
            logger.debug(
                f"CompressionScalp SKIPPED: BB width {bb_width_pct:.2%} > 2.0% threshold "
                f"— market not tight enough for scalp"
            )
            return None

        last  = df_5m.iloc[-1]
        prev  = df_5m.iloc[-2]
        close = float(last["close"])
        open_ = float(last["open"])
        high  = float(last["high"])
        low   = float(last["low"])

        # ── Setup 1: Breakout from compression ────────────────────────────────
        breakout_long  = close > vol_state.bb_upper and close > open_
        breakout_short = close < vol_state.bb_lower and close < open_

        # ── Setup 2: False breakout fade ──────────────────────────────────────
        # Previous candle spiked outside, current candle closed back inside
        prev_close = float(prev["close"])
        prev_high  = float(prev["high"])
        prev_low   = float(prev["low"])

        fake_high_sweep = (prev_high > vol_state.bb_upper and
                           prev_close < vol_state.bb_upper and
                           close < band_mid)

        fake_low_sweep  = (prev_low < vol_state.bb_lower and
                           prev_close > vol_state.bb_lower and
                           close > band_mid)

        signal = TradeSignal(
            strategy_name=self.name,
            regime=regime.primary_regime,
            atr=atr,
            vwap=vol_state.vwap,
        )

        if fake_high_sweep:
            return self._fade_high_spike(
                signal, vol_state, structure, current_price, atr, band_width,
                prev_high, regime
            )
        elif fake_low_sweep:
            return self._fade_low_spike(
                signal, vol_state, structure, current_price, atr, band_width,
                prev_low, regime
            )
        elif breakout_long:
            return self._breakout_long(
                signal, vol_state, structure, current_price, atr, band_width, regime
            )
        elif breakout_short:
            return self._breakout_short(
                signal, vol_state, structure, current_price, atr, band_width, regime
            )

        logger.debug("CompressionScalp: still in squeeze, no breakout yet")
        return None

    def _fade_high_spike(self, signal, vol_state, structure, price,
                          atr, band_width, spike_high, regime) -> Optional[TradeSignal]:
        """Short a false breakout above the upper band."""
        signal.direction  = "short"
        signal.setup_type = "Compression Scalp — False High Breakout Fade"
        signal.entry_price = price
        # Enforce minimum stop floor — prevents oversized positions on tight compression
        raw_stop_dist  = abs(spike_high + atr * 0.3 - price)
        min_stop       = adaptive_min_stop(price, atr, 1.0)
        min_stop       = max(min_stop, price * 0.0075)  # 0.75% floor — compression noise
        stop_dist      = max(raw_stop_dist, min_stop)
        signal.stop_price  = price + stop_dist

        self._add_confluence(signal, "False BB upper breakout")
        self._add_confluence(signal, "Closed back inside bands")
        if vol_state.price_vs_vwap == "BELOW":
            self._add_confluence(signal, "Below VWAP")

        risk = signal.stop_price - signal.entry_price
        signal.target_1   = vol_state.bb_middle
        signal.target_2   = vol_state.bb_lower * 1.002
        signal.conviction = regime.conviction * 0.6
        signal.notes      = f"Spike to {spike_high:.0f}, fading back inside"

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            return None

        logger.info(f"CompressionScalp SHORT (false break fade) @ {price:.2f}")
        return signal

    def _fade_low_spike(self, signal, vol_state, structure, price,
                         atr, band_width, spike_low, regime) -> Optional[TradeSignal]:
        """Long a false breakdown below the lower band."""
        signal.direction  = "long"
        signal.setup_type = "Compression Scalp — False Low Breakout Fade"
        signal.entry_price = price
        raw_stop_dist  = abs(price - (spike_low - atr * 0.3))
        min_stop       = adaptive_min_stop(price, atr, 1.0)
        min_stop       = max(min_stop, price * 0.0075)  # 0.75% floor — compression noise
        stop_dist      = max(raw_stop_dist, min_stop)
        signal.stop_price  = price - stop_dist

        self._add_confluence(signal, "False BB lower breakdown")
        self._add_confluence(signal, "Closed back inside bands")
        if vol_state.price_vs_vwap == "ABOVE":
            self._add_confluence(signal, "Above VWAP")

        risk = signal.entry_price - signal.stop_price
        signal.target_1   = vol_state.bb_middle
        signal.target_2   = vol_state.bb_upper * 0.998
        signal.conviction = regime.conviction * 0.6
        signal.notes      = f"Spike to {spike_low:.0f}, fading recovery"

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            return None

        logger.info(f"CompressionScalp LONG (false break fade) @ {price:.2f}")
        return signal

    def _breakout_long(self, signal, vol_state, structure, price,
                        atr, band_width, regime) -> Optional[TradeSignal]:
        """Long a genuine upside breakout from squeeze."""
        signal.direction   = "long"
        signal.setup_type  = "Compression Scalp — Upside Breakout"
        signal.entry_price = price
        raw_stop_dist  = abs(price - (vol_state.bb_middle - atr * 0.2))
        min_stop       = adaptive_min_stop(price, atr, 1.0)
        min_stop       = max(min_stop, price * 0.0075)  # 0.75% floor — compression noise
        stop_dist      = max(raw_stop_dist, min_stop)
        signal.stop_price  = price - stop_dist

        self._add_confluence(signal, "BB upper breakout")
        self._add_confluence(signal, f"Squeeze at {vol_state.bb_width_pct:.0%} percentile")
        if vol_state.price_vs_vwap == "ABOVE":
            self._add_confluence(signal, "Above VWAP")
        if structure.nearest_resistance:
            if price > structure.nearest_resistance:
                self._add_confluence(signal, "Breaking resistance")

        risk = signal.entry_price - signal.stop_price
        signal.target_1 = price + band_width        # First target: one band width
        signal.target_2 = price + band_width * 2.0  # Extended: double band width
        signal.conviction = regime.conviction * 0.65

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            return None

        logger.info(f"CompressionScalp LONG (breakout) @ {price:.2f}")
        return signal

    def _breakout_short(self, signal, vol_state, structure, price,
                         atr, band_width, regime) -> Optional[TradeSignal]:
        """Short a genuine downside breakout from squeeze."""
        signal.direction   = "short"
        signal.setup_type  = "Compression Scalp — Downside Breakout"
        signal.entry_price = price
        raw_stop_dist  = abs((vol_state.bb_middle + atr * 0.2) - price)
        min_stop       = adaptive_min_stop(price, atr, 1.0)
        min_stop       = max(min_stop, price * 0.0075)  # 0.75% floor — compression noise
        stop_dist      = max(raw_stop_dist, min_stop)
        signal.stop_price  = price + stop_dist

        self._add_confluence(signal, "BB lower breakdown")
        self._add_confluence(signal, f"Squeeze at {vol_state.bb_width_pct:.0%} percentile")
        if vol_state.price_vs_vwap == "BELOW":
            self._add_confluence(signal, "Below VWAP")
        if structure.nearest_support:
            if price < structure.nearest_support:
                self._add_confluence(signal, "Breaking support")

        risk = signal.stop_price - signal.entry_price
        signal.target_1 = price - band_width
        signal.target_2 = price - band_width * 2.0
        signal.conviction = regime.conviction * 0.65

        signal.compute_ratios()
        if not self._validate_rrr(signal):
            return None

        logger.info(f"CompressionScalp SHORT (breakdown) @ {price:.2f}")
        return signal

    def _minimum_rrr(self) -> float:
        """Compression scalps: 1.3R acceptable — these are faster, tighter trades."""
        return 1.3
