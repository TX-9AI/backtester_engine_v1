# crypto_trader/strategy_bundle.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Adapter connecting ReplayEngine to live crypto_trader strategy stack
# v1.1 — 2026-06-28 — Fix: add crypto_trader/ to sys.path so bare imports resolve correctly
# v1.2 — 2026-06-28 — Fix: import from analysis/, strategy/, risk/ directly (not crypto_trader.x)
# v1.3 — 2026-06-28 — Fix: init_risk_manager() signature — no paper kwarg, use cash_balance only
# v1.4 — 2026-06-28 — Fix: raise minimum candles for vol_engine (30 5m + 10 1h for BB_PERIOD)
#                      Fix: re-raise KeyboardInterrupt through all except Exception blocks
# v1.5 — 2026-06-28 — Fix: add TrendEngine — was missing entirely, causing regime=None always
# v1.6 — 2026-06-28 — Full rewrite: correct method signatures from all live bot source files
# v1.7 — 2026-06-28 — Fix: use StrategySelector, get_trend_engine() singleton, conviction cap
# v1.8 — 2026-06-29 — Fix: set DatetimeIndex on df_5m/df_1h before vol_engine (fixes VWAP=0)

"""
StrategyBundle wraps the live bot's strategy classes into the interface
expected by ReplayEngine. Calls all engines exactly as the live bot does.

Pipeline per candle:
    compute_indicators → classify_regime → generate_signal → validate_entry → compute_size
"""

import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# ── Path setup ───────────────────────────────────────────────────────────────
_CT_DIR   = Path(__file__).parent
_ROOT_DIR = _CT_DIR.parent

for _p in [str(_CT_DIR), str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from analysis.regime_classifier   import get_regime_classifier
from analysis.volatility_engine   import get_volatility_engine
from analysis.liquidity_mapper    import get_liquidity_mapper, LiquidityMap
from analysis.structure_analyzer  import get_structure_analyzer, StructureMap
from analysis.trend_engine        import get_trend_engine
from execution.signal_validator   import get_signal_validator
from strategy.strategy_selector   import get_strategy_selector
from risk.risk_manager            import init_risk_manager, get_risk_manager
from config                       import SessionConfig

logger = logging.getLogger(__name__)


class StrategyBundle:
    """
    Stateful adapter. One instance per backtest run.
    """

    def __init__(self):
        self.vol_engine    = get_volatility_engine()
        self.trend_engine  = get_trend_engine()
        self.liq_mapper    = get_liquidity_mapper()
        self.structure     = get_structure_analyzer()
        self.classifier    = get_regime_classifier()
        self.validator     = get_signal_validator()
        self.selector      = get_strategy_selector()
        init_risk_manager(cash_balance=1000.0)

    # ── Step 1: Compute indicators ────────────────────────────────────────────

    def compute_indicators(self, windows: dict, candle: dict) -> dict:
        df_5m  = windows.get("5m",  pd.DataFrame())
        df_1h  = windows.get("1h",  pd.DataFrame())
        df_15m = windows.get("15m", pd.DataFrame())
        df_1d  = windows.get("1d",  pd.DataFrame())
        df_1m  = windows.get("1m",  pd.DataFrame())

        current_price = float(candle.get("close", 0))
        if current_price <= 0:
            return {}

        # ── Volatility ───────────────────────────────────────────────────────
        # vol_engine expects DatetimeIndex on df_5m/df_1h for VWAP calculation
        vol_state = None
        if len(df_5m) >= 30 and len(df_1h) >= 10:
            try:
                df_5m_idx = df_5m.copy()
                df_1h_idx = df_1h.copy()
                if "timestamp" in df_5m_idx.columns:
                    df_5m_idx["timestamp"] = pd.to_datetime(df_5m_idx["timestamp"], utc=True)
                    df_5m_idx = df_5m_idx.set_index("timestamp")
                if "timestamp" in df_1h_idx.columns:
                    df_1h_idx["timestamp"] = pd.to_datetime(df_1h_idx["timestamp"], utc=True)
                    df_1h_idx = df_1h_idx.set_index("timestamp")
                vol_state = self.vol_engine.analyze(
                    df_5m=df_5m_idx, df_1h=df_1h_idx, current_price=current_price
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.debug(f"[Bundle] vol_engine: {e}")

        if vol_state is None:
            return {}

        # ── Trend ────────────────────────────────────────────────────────────
        trend_state = None
        try:
            trend_state = self.trend_engine.analyze({
                "1m":  df_1m  if not df_1m.empty  else None,
                "5m":  df_5m  if not df_5m.empty  else None,
                "15m": df_15m if not df_15m.empty else None,
                "1h":  df_1h  if not df_1h.empty  else None,
                "1d":  df_1d  if not df_1d.empty  else None,
                "4h":  None,
            })
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.debug(f"[Bundle] trend_engine: {e}")

        if trend_state is None:
            return {}

        # ── Liquidity ────────────────────────────────────────────────────────
        # Use empty LiquidityMap as safe default — classifier handles it fine
        liq_map = LiquidityMap()
        if len(df_5m) >= 10:
            try:
                liq_map = self.liq_mapper.analyze(
                    df_5m=df_5m,
                    df_15m=df_15m if not df_15m.empty else None,
                    current_price=current_price,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.debug(f"[Bundle] liq_mapper: {e}")

        # ── Structure ────────────────────────────────────────────────────────
        structure = StructureMap()
        if len(df_5m) >= 10:
            try:
                structure = self.structure.analyze(
                    df_5m=df_5m,
                    df_15m=df_15m if not df_15m.empty else None,
                    df_1h=df_1h   if not df_1h.empty  else None,
                    current_price=current_price,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.debug(f"[Bundle] structure: {e}")

        return {
            "vol_state":     vol_state,
            "trend_state":   trend_state,
            "liq_map":       liq_map,
            "structure":     structure,
            "current_price": current_price,
            "vwap":          vol_state.vwap if vol_state.vwap else candle.get("vwap", current_price),
            "windows":       windows,
        }

    # ── Step 2: Classify regime ───────────────────────────────────────────────

    def classify_regime(self, indicators: dict, config) -> tuple:
        if not indicators:
            return None, 0.0

        vol_state   = indicators.get("vol_state")
        trend_state = indicators.get("trend_state")
        structure   = indicators.get("structure")
        liq_map     = indicators.get("liq_map")

        if vol_state is None or trend_state is None or liq_map is None:
            return None, 0.0

        try:
            regime_state = self.classifier.classify(
                vol_state=vol_state,
                trend_state=trend_state,
                structure=structure,
                liq_map=liq_map,
                macro=None,
                trigger="scheduled",
            )
            # Cap conviction at 1.0 — sweep_conviction formula can exceed 1.0
            conviction = min(float(getattr(regime_state, "conviction", 0.5)), 1.0)
            regime_state.conviction = conviction
            indicators["_last_regime"] = regime_state
            return regime_state, conviction
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.debug(f"[Bundle] classifier: {e}")
            return None, 0.0

    # ── Step 3: Generate signal ───────────────────────────────────────────────

    def generate_signal(self, regime_state, conviction: float,
                        indicators: dict, candle: dict, config) -> Optional[dict]:
        if regime_state is None or not indicators:
            return None

        # Use StrategySelector — handles conviction gate (0.35) and diagnostic mode
        vol_state     = indicators.get("vol_state")
        liq_map       = indicators.get("liq_map")
        structure     = indicators.get("structure")
        windows       = indicators.get("windows", {})
        current_price = indicators.get("current_price", float(candle.get("close", 0)))

        try:
            signal = self.selector.generate_signal(
                regime=regime_state,
                vol_state=vol_state,
                structure=structure,
                liq_map=liq_map,
                data=windows,
                current_price=current_price,
            )
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.debug(f"[Bundle] generate_signal: {e}")
            return None

        if signal is None:
            return None

        # Convert TradeSignal dataclass to dict, keep raw object for validator
        return {
            "direction":  signal.direction,
            "entry":      signal.entry_price,
            "stop":       signal.stop_price,
            "target":     signal.target_1,
            "target_2":   getattr(signal, "target_2", None),
            "grade":      getattr(signal, "grade", "B"),
            "strategy":   getattr(signal, "strategy_name", ""),
            "regime":     getattr(signal, "regime", ""),
            "atr":        getattr(signal, "atr", 0.0),
            "conviction": getattr(signal, "conviction", conviction),
            "confluence_factors": getattr(signal, "confluence_factors", []),
            "_signal_obj": signal,
        }

    # ── Step 4: Validate entry ────────────────────────────────────────────────

    def validate_entry(self, signal: dict, indicators: dict, config) -> bool:
        if not signal or not indicators:
            return False

        signal_obj = signal.get("_signal_obj")
        if signal_obj is None:
            return False

        regime_state  = indicators.get("_last_regime")
        vol_state     = indicators.get("vol_state")
        structure     = indicators.get("structure")
        liq_map       = indicators.get("liq_map")
        windows       = indicators.get("windows", {})
        current_price = indicators.get("current_price", 0.0)

        if regime_state is None or vol_state is None:
            return False

        try:
            result = self.validator.validate(
                signal=signal_obj,
                regime=regime_state,
                vol_state=vol_state,
                structure=structure,
                liq_map=liq_map,
                macro=None,
                data=windows,
                current_price=current_price,
            )
            return result.passed
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.debug(f"[Bundle] validate_entry: {e}")
            return False

    # ── Step 5: Compute size ──────────────────────────────────────────────────

    def compute_size(self, signal: dict, indicators: dict,
                     cash_balance: float, config) -> tuple:
        if not signal:
            return 0.0, 0.0

        risk_mgr = get_risk_manager()
        risk_mgr.update_cash_balance(cash_balance)

        entry = signal.get("entry", 0.0)
        stop  = signal.get("stop", 0.0)
        grade = signal.get("grade", "B")

        if entry <= 0 or stop <= 0:
            return 0.0, 0.0

        try:
            # Check bypass flag from bt_config
            try:
                import bt_config as _btcfg
                bypass_fee_floor = getattr(_btcfg, "BYPASS_FEE_FLOOR", False)
                maker_fee        = getattr(_btcfg, "BACKTEST_MAKER_FEE", None)
            except ImportError:
                bypass_fee_floor = False
                maker_fee        = None

            if bypass_fee_floor:
                # Compute size without fee floor check
                from config import LEVERAGE, GRADE_A_NOTIONAL_PCT, GRADE_B_NOTIONAL_PCT
                from utils.math_utils import round_size
                from config import MIN_ORDER_SIZE_BTC
                grade_pct = {"A": GRADE_A_NOTIONAL_PCT, "B": GRADE_B_NOTIONAL_PCT}.get(grade, GRADE_B_NOTIONAL_PCT)
                buying_power = cash_balance * LEVERAGE
                notional     = buying_power * grade_pct
                size_btc     = round_size(notional / entry, MIN_ORDER_SIZE_BTC)
                if size_btc < MIN_ORDER_SIZE_BTC:
                    return 0.0, 0.0
                notional = size_btc * entry

                # Apply maker fee rate if configured
                if maker_fee is not None:
                    risk_mgr._maker_fee_override = maker_fee

                return size_btc, notional

            result = risk_mgr.compute_size(
                entry_price=entry,
                stop_price=stop,
                grade=grade,
                current_balance=cash_balance,
            )
            if not result.allowed:
                logger.debug(f"[Bundle] sizing rejected: {result.reject_reason}")
                return 0.0, 0.0
            return result.size_btc, result.notional_usd
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.debug(f"[Bundle] compute_size: {e}")
            return 0.0, 0.0
