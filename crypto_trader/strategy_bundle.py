# strategy_bundle.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Adapter connecting ReplayEngine to live crypto_trader strategy stack
# v1.1 — 2026-06-28 — Fix: add crypto_trader/ to sys.path so bare imports resolve correctly
# v1.2 — 2026-06-28 — Fix: import from analysis/, strategy/, risk/ directly (not crypto_trader.x)
# v1.3 — 2026-06-28 — Fix: init_risk_manager() signature — no paper kwarg, use cash_balance only

"""
StrategyBundle wraps the live bot's strategy classes into the interface
expected by ReplayEngine. All method signatures are called exactly as
the live bot calls them — no translation layer, no mocking.

ReplayEngine calls:
    bundle.compute_indicators(windows, candle)   → (vol_state, trend_state, structure, liq_map)
    bundle.classify_regime(indicators, config)   → (regime_state, conviction)
    bundle.generate_signal(regime, conviction, indicators, candle, config) → signal dict or None
    bundle.validate_entry(signal, indicators, config) → bool
    bundle.compute_size(signal, indicators, cash_balance, config) → (contracts, notional)
"""

import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# ── Path setup ───────────────────────────────────────────────────────────────
# crypto_trader/ must be on sys.path so the strategy files' bare imports
# (e.g. "from config import ...") resolve to crypto_trader/config.py
# Project root must also be on sys.path for "from crypto_trader.x import ..."
_CT_DIR   = Path(__file__).parent          # .../btc-backtester/crypto_trader/
_ROOT_DIR = _CT_DIR.parent                 # .../btc-backtester/

for _p in [str(_CT_DIR), str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from analysis.regime_classifier   import get_regime_classifier
from analysis.volatility_engine   import get_volatility_engine
from analysis.liquidity_mapper    import get_liquidity_mapper
from analysis.structure_analyzer  import get_structure_analyzer
from strategy.momentum_strategy          import MomentumStrategy
from strategy.compression_scalp_strategy import CompressionScalpStrategy
from strategy.sweep_reversal_strategy    import SweepReversalStrategy
from strategy.mean_reversion_strategy    import MeanReversionStrategy
from risk.risk_manager            import init_risk_manager, get_risk_manager
from config                       import SessionConfig

logger = logging.getLogger(__name__)


class StrategyBundle:
    """
    Stateful adapter. One instance per backtest run.
    Holds singleton instances of each engine, matching live bot architecture.
    """

    def __init__(self):
        # Instantiate engines exactly as live bot does
        self.vol_engine    = get_volatility_engine()
        self.liq_mapper    = get_liquidity_mapper()
        self.structure     = get_structure_analyzer()
        self.classifier    = get_regime_classifier()

        # Strategies — same instances live bot uses
        self.strategies = {
            "momentum":    MomentumStrategy(),
            "compression": CompressionScalpStrategy(),
            "sweep":       SweepReversalStrategy(),
            "mean_rev":    MeanReversionStrategy(),
        }

        # RiskManager — pass session config and default cash balance
        session_cfg = SessionConfig()
        init_risk_manager(session_config=session_cfg, cash_balance=1000.0)

    # ── Step 1: Compute indicators ────────────────────────────────────────────

    def compute_indicators(self, windows: dict, candle: dict) -> dict:
        """
        Run VolatilityEngine, LiquidityMapper, StructureAnalyzer.
        Returns dict of computed states for use by classifier and strategies.
        """
        df_5m  = windows.get("5m",  pd.DataFrame())
        df_1h  = windows.get("1h",  pd.DataFrame())
        df_15m = windows.get("15m", pd.DataFrame())
        df_1d  = windows.get("1d",  pd.DataFrame())
        df_1m  = windows.get("1m",  pd.DataFrame())

        current_price = float(candle.get("close", 0))
        if current_price <= 0:
            return {}

        # Volatility state — needs 5m and 1h
        vol_state = None
        if len(df_5m) >= 20 and len(df_1h) >= 5:
            try:
                vol_state = self.vol_engine.analyze(
                    df_5m=df_5m,
                    df_1h=df_1h,
                    current_price=current_price,
                )
            except Exception as e:
                logger.debug(f"[Bundle] vol_engine error: {e}")
                return {}

        if vol_state is None:
            return {}

        # Liquidity map — needs 1m history for sweep detection
        liq_map = None
        try:
            liq_map = self.liq_mapper.build_map(
                df_1m=df_1m,
                df_5m=df_5m,
                df_1d=df_1d,
                current_price=current_price,
            )
        except Exception as e:
            logger.debug(f"[Bundle] liq_mapper error: {e}")

        # Structure map — BOS, FVG, order blocks
        structure = None
        try:
            structure = self.structure.analyze(
                df_5m=df_5m,
                df_15m=df_15m,
                df_1h=df_1h,
                current_price=current_price,
            )
        except Exception as e:
            logger.debug(f"[Bundle] structure error: {e}")

        # TrendState — derived from vol_state (live bot does this inline)
        trend_state = getattr(vol_state, "trend_state", None)

        return {
            "vol_state":     vol_state,
            "trend_state":   trend_state,
            "liq_map":       liq_map,
            "structure":     structure,
            "current_price": current_price,
            "vwap":          candle.get("vwap", current_price),
            "windows":       windows,
        }

    # ── Step 2: Classify regime ───────────────────────────────────────────────

    def classify_regime(self, indicators: dict, config) -> tuple:
        """
        Returns (RegimeState, conviction: float).
        """
        if not indicators:
            return None, 0.0

        vol_state   = indicators.get("vol_state")
        trend_state = indicators.get("trend_state")
        structure   = indicators.get("structure")
        liq_map     = indicators.get("liq_map")

        if vol_state is None or trend_state is None:
            return None, 0.0

        try:
            regime_state = self.classifier.classify(
                vol_state=vol_state,
                trend_state=trend_state,
                structure=structure,
                liq_map=liq_map,
            )
            conviction = float(getattr(regime_state, "conviction", 0.5))
            return regime_state, conviction
        except Exception as e:
            logger.debug(f"[Bundle] classifier error: {e}")
            return None, 0.0

    # ── Step 3: Generate signal ───────────────────────────────────────────────

    def generate_signal(self, regime_state, conviction: float,
                        indicators: dict, candle: dict, config) -> Optional[dict]:
        """
        Route regime to correct strategy, call generate_signal(),
        return normalized signal dict or None.
        """
        if regime_state is None or not indicators:
            return None

        vol_state   = indicators.get("vol_state")
        structure   = indicators.get("structure")
        liq_map     = indicators.get("liq_map")
        current_price = indicators.get("current_price", 0)
        windows     = indicators.get("windows", {})

        if vol_state is None or current_price <= 0:
            return None

        # Route to strategy based on regime
        strategy = None
        strategy_name = None

        if regime_state.is_sweep_reversal:
            strategy = self.strategies["sweep"]
            strategy_name = "sweep_reversal"
        elif regime_state.is_compression:
            strategy = self.strategies["compression"]
            strategy_name = "compression_scalp"
        elif regime_state.is_trending():
            strategy = self.strategies["momentum"]
            strategy_name = "momentum"
        elif regime_state.is_ranging():
            strategy = self.strategies["mean_rev"]
            strategy_name = "mean_reversion"

        if strategy is None:
            return None

        # Check strategy applicability
        try:
            if not strategy.is_applicable(regime_state):
                return None
        except Exception:
            pass

        # Call generate_signal with live bot signature
        try:
            trade_signal = strategy.generate_signal(
                regime=regime_state,
                vol_state=vol_state,
                structure=structure,
                liq_map=liq_map,
                data=windows,
                current_price=current_price,
            )
        except Exception as e:
            logger.debug(f"[Bundle] {strategy_name} signal error: {e}")
            return None

        if trade_signal is None:
            return None

        # Normalize TradeSignal → plain dict for ReplayEngine
        return self._normalize_signal(trade_signal, regime_state,
                                      strategy_name, indicators)

    # ── Step 4: Validate entry ────────────────────────────────────────────────

    def validate_entry(self, signal: dict, indicators: dict, config) -> bool:
        """
        Apply VWAP gate and grade filter.
        Sweep Reversal bypasses VWAP gate — matches live bot behavior.
        """
        if not signal:
            return False

        direction   = signal.get("direction")
        entry_price = signal.get("entry", 0)
        vwap        = indicators.get("vwap", 0)
        grade       = signal.get("grade", "B")
        strategy    = signal.get("strategy", "")

        # Grade C disabled
        if grade == "C":
            return False

        # VWAP gate — hard block (Sweep Reversal bypasses)
        if config.vwap_filter_active and strategy != "sweep_reversal" and vwap > 0:
            if direction == "short" and entry_price > vwap:
                logger.debug(f"[Bundle] VWAP gate: short above VWAP blocked")
                return False
            if direction == "long" and entry_price < vwap:
                logger.debug(f"[Bundle] VWAP gate: long below VWAP blocked")
                return False

        # Minimum R:R check
        entry  = signal.get("entry", 0)
        stop   = signal.get("stop",  0)
        target = signal.get("target", 0)
        if entry > 0 and stop > 0 and target > 0:
            r_distance = abs(entry - stop)
            r_reward   = abs(target - entry)
            if r_distance > 0:
                rrr = r_reward / r_distance
                if rrr < config.min_rrr:
                    logger.debug(f"[Bundle] RRR {rrr:.2f} < min {config.min_rrr}")
                    return False

        return True

    # ── Step 5: Compute size ──────────────────────────────────────────────────

    def compute_size(self, signal: dict, indicators: dict,
                     cash_balance: float, config) -> tuple[float, float]:
        """
        Returns (contracts_btc, notional_usd).
        Uses live RiskManager.compute_size() with cash balance injected.
        """
        if not signal or cash_balance <= 0:
            return 0.0, 0.0

        entry_price = signal.get("entry", 0)
        stop_price  = signal.get("stop",  0)
        grade       = signal.get("grade", "B")
        direction   = signal.get("direction", "long")

        if entry_price <= 0 or stop_price <= 0:
            return 0.0, 0.0

        try:
            rm = get_risk_manager()
            result = rm.compute_size(
                entry_price=entry_price,
                stop_price=stop_price,
                grade=grade,
                current_balance=cash_balance,
                direction=direction,
            )
            if not result.allowed:
                logger.debug(f"[Bundle] Size rejected: {result.reject_reason}")
                return 0.0, 0.0

            return float(result.size_btc), float(result.notional_usd)

        except Exception as e:
            logger.debug(f"[Bundle] compute_size error: {e}")
            return 0.0, 0.0

    # ── Normalizer ────────────────────────────────────────────────────────────

    def _normalize_signal(self, trade_signal, regime_state,
                          strategy_name: str, indicators: dict) -> dict:
        """
        Convert TradeSignal dataclass → plain dict for ReplayEngine.
        Handles both dataclass and dict-style TradeSignal objects.
        """
        def _get(obj, *keys, default=None):
            for k in keys:
                try:
                    v = getattr(obj, k, None)
                    if v is not None:
                        return v
                except Exception:
                    pass
                try:
                    v = obj.get(k) if isinstance(obj, dict) else None
                    if v is not None:
                        return v
                except Exception:
                    pass
            return default

        entry  = _get(trade_signal, "entry", "entry_price", default=indicators.get("current_price"))
        stop   = _get(trade_signal, "stop",  "stop_price",  default=0)
        target = _get(trade_signal, "target","target_price", default=0)
        direction = _get(trade_signal, "direction", default="long")
        grade  = _get(trade_signal, "grade", default="B")

        return {
            "direction": direction,
            "entry":     float(entry)  if entry  else 0.0,
            "stop":      float(stop)   if stop   else 0.0,
            "target":    float(target) if target else 0.0,
            "grade":     str(grade).upper(),
            "strategy":  strategy_name,
            "regime":    str(getattr(regime_state, "label",
                             getattr(regime_state, "regime", "unknown"))),
        }
