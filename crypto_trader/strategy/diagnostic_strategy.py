import logging
from typing import Optional
from analysis.regime_classifier import RegimeState
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap
from strategy.base_strategy import BaseStrategy, TradeSignal

logger = logging.getLogger(__name__)

_trade_fired  = False
_trade_closed = False


class DiagnosticStrategy(BaseStrategy):

    @property
    def name(self) -> str:
        return "Diagnostic"

    def is_applicable(self, regime: RegimeState) -> bool:
        return True

    def generate_signal(self, regime: RegimeState, vol_state: VolatilityState,
                        structure: StructureMap, liq_map: LiquidityMap,
                        data: dict, current_price: float) -> Optional[TradeSignal]:
        global _trade_fired, _trade_closed

        if _trade_closed:
            logger.info("DIAGNOSTIC COMPLETE — No further trades.")
            return None

        if _trade_fired:
            logger.info("DIAGNOSTIC: Waiting for open trade to close...")
            return None

        atr = vol_state.atr_current if vol_state and vol_state.atr_current else 0
        if not atr:
            logger.info("DIAGNOSTIC: Waiting for ATR...")
            return None

        stop_distance = atr * 0.1
        stop_price    = current_price - stop_distance
        target_1      = current_price + (stop_distance * 1.5)

        signal = TradeSignal()
        signal.direction     = "long"
        signal.entry_price   = current_price
        signal.stop_price    = stop_price
        signal.target_1      = target_1
        signal.target_2      = 0.0
        signal.atr           = atr
        signal.regime        = "DIAGNOSTIC"
        signal.strategy_name = "Diagnostic"
        signal.conviction    = 1.0
        signal.notes         = "DIAGNOSTIC: single test trade"

        _trade_fired = True
        logger.info(
            f"DIAGNOSTIC SIGNAL: LONG @ {current_price:.4f} "
            f"stop={stop_price:.4f} target={target_1:.4f}"
        )
        return signal

    @staticmethod
    def mark_closed():
        global _trade_closed
        _trade_closed = True
        logger.info("DIAGNOSTIC TRADE CLOSED — halting new entries.")
