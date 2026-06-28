"""
strategy/base_strategy.py — Abstract base for all trading strategies.
Defines the shared interface every strategy module must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List
import pandas as pd

from analysis.regime_classifier import RegimeState
from analysis.volatility_engine import VolatilityState
from analysis.structure_analyzer import StructureMap
from analysis.liquidity_mapper import LiquidityMap


@dataclass
class TradeSignal:
    """
    A candidate trade proposal from a strategy module.
    Validated by signal_validator before reaching execution.
    """
    # Direction and type
    direction:      str   = ""     # "long" or "short"
    strategy_name:  str   = ""
    setup_type:     str   = ""     # Human-readable e.g. "ORB Long", "Sweep Reversal Short"

    # Price levels
    entry_price:    float = 0.0    # Proposed entry (limit) price
    stop_price:     float = 0.0    # Initial stop loss
    target_1:       float = 0.0    # First target (partial exit)
    target_2:       float = 0.0    # Second target (trail remainder)

    # Quality
    confluence_factors: List[str] = field(default_factory=list)
    conviction:     float = 0.0    # 0.0 – 1.0 raw strategy conviction

    # Context
    regime:         str   = ""
    atr:            float = 0.0
    vwap:           float = 0.0
    notes:          str   = ""

    # Computed fields (filled by risk_manager)
    risk_r:         float = 0.0    # R distance from entry to stop
    reward_r1:      float = 0.0    # R distance to target_1
    reward_r2:      float = 0.0    # R distance to target_2
    rrr_1:          float = 0.0    # R:R ratio to target_1
    rrr_2:          float = 0.0    # R:R ratio to target_2

    @property
    def is_valid(self) -> bool:
        """Minimum validity check before passing to validator."""
        return (
            self.direction in ("long", "short") and
            self.entry_price > 0 and
            self.stop_price > 0 and
            self.target_1 > 0
        )

    def compute_ratios(self):
        """Calculate R distances and R:R ratios."""
        if self.direction == "long":
            self.risk_r    = self.entry_price - self.stop_price
            self.reward_r1 = self.target_1 - self.entry_price
            self.reward_r2 = (self.target_2 - self.entry_price) if self.target_2 else 0
        else:
            self.risk_r    = self.stop_price - self.entry_price
            self.reward_r1 = self.entry_price - self.target_1
            self.reward_r2 = (self.entry_price - self.target_2) if self.target_2 else 0

        self.rrr_1 = self.reward_r1 / self.risk_r if self.risk_r else 0
        self.rrr_2 = self.reward_r2 / self.risk_r if self.risk_r else 0


class BaseStrategy(ABC):
    """
    Abstract base class. All strategy modules inherit from this.
    Each strategy implements generate_signal() and is_applicable().
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging and notifications."""
        ...

    @abstractmethod
    def is_applicable(self, regime: RegimeState) -> bool:
        """
        Can this strategy trade in the current regime?
        Called by strategy_selector to confirm viability.
        """
        ...

    @abstractmethod
    def generate_signal(self,
                        regime:     RegimeState,
                        vol_state:  VolatilityState,
                        structure:  StructureMap,
                        liq_map:    LiquidityMap,
                        data:       dict,
                        current_price: float) -> Optional[TradeSignal]:
        """
        Analyze current conditions and return a TradeSignal if a setup exists.
        Returns None if no valid setup present.

        Args:
            regime:        Current regime classification
            vol_state:     Volatility analysis
            structure:     Market structure map
            liq_map:       Liquidity map
            data:          Dict of TF → DataFrame from DataCache
            current_price: Latest price

        Returns:
            TradeSignal or None
        """
        ...

    def _add_confluence(self, signal: TradeSignal, factor: str):
        """Helper to add a confluence factor to a signal."""
        signal.confluence_factors.append(factor)

    def _minimum_rrr(self) -> float:
        """Minimum acceptable risk:reward ratio. Override in subclass."""
        return 1.5

    def _validate_rrr(self, signal: TradeSignal) -> bool:
        """Check if the setup has acceptable R:R."""
        signal.compute_ratios()
        return signal.rrr_1 >= self._minimum_rrr()
