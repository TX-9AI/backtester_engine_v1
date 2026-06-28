# simulated_broker.py — btc_backtester
# v1.0 — 2026-06-28 — Paper fill engine with Kraken fee structure and slippage model

"""
Simulated broker for backtesting. Mirrors the live bot's paper fill logic
from execution/order_manager.py but operates on historical candle data.

Responsibilities:
  - Apply entry slippage (0.1% on fill price)
  - Calculate round-trip Kraken fees (taker + margin open)
  - Enforce minimum order size (0.0001 BTC)
  - Enforce fee floor: projected 1R profit must exceed round-trip fees
  - Return fill details consumed by ReplayEngine and BacktestLogger

Does NOT manage position state — that belongs to ReplayEngine.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ─── KRAKEN FEE CONSTANTS (from live config.py) ───────────────────────────────

KRAKEN_TAKER_FEE      = 0.0080   # 0.80%
KRAKEN_MARGIN_OPEN    = 0.0002   # 0.02%
KRAKEN_ROLLOVER_FEE   = 0.0002   # 0.02% per 4h (not charged in backtest by default)
PAPER_SLIPPAGE_PCT    = 0.001    # 0.10% per fill (matches live config)
MIN_ORDER_SIZE_BTC    = 0.0001


# ─── FILL RESULT ──────────────────────────────────────────────────────────────

@dataclass
class FillResult:
    """Returned by SimulatedBroker.fill_entry() and fill_exit()."""
    filled:        bool
    fill_price:    float
    contracts:     float        # BTC
    notional_usd:  float
    fee_usd:       float
    reject_reason: Optional[str] = None


# ─── SIMULATED BROKER ─────────────────────────────────────────────────────────

class SimulatedBroker:
    """
    Stateless fill engine. Called by ReplayEngine for every entry and exit.

    All slippage and fee logic is centralized here so the optimizer can
    vary fee assumptions without touching replay logic.
    """

    def __init__(
        self,
        slippage_pct:      float = PAPER_SLIPPAGE_PCT,
        taker_fee:         float = KRAKEN_TAKER_FEE,
        margin_open_fee:   float = KRAKEN_MARGIN_OPEN,
        charge_rollover:   bool  = False,
        rollover_fee:      float = KRAKEN_ROLLOVER_FEE,
    ):
        self.slippage_pct    = slippage_pct
        self.taker_fee       = taker_fee
        self.margin_open_fee = margin_open_fee
        self.charge_rollover = charge_rollover
        self.rollover_fee    = rollover_fee

    # ── Entry fill ────────────────────────────────────────────────────────────

    def fill_entry(
        self,
        direction:    str,    # "long" or "short"
        mark_price:   float,  # candle close at signal
        contracts:    float,  # BTC contracts requested
    ) -> FillResult:
        """
        Simulate an entry market order fill with slippage.
        Returns FillResult. If rejected (e.g. min size), filled=False.
        """
        if contracts < MIN_ORDER_SIZE_BTC:
            return FillResult(
                filled=False,
                fill_price=mark_price,
                contracts=contracts,
                notional_usd=0,
                fee_usd=0,
                reject_reason=f"contracts {contracts:.6f} below min {MIN_ORDER_SIZE_BTC}",
            )

        # Slippage: long fills higher, short fills lower
        if direction == "long":
            fill_price = mark_price * (1 + self.slippage_pct)
        else:
            fill_price = mark_price * (1 - self.slippage_pct)

        notional = fill_price * contracts
        fee = (notional * self.taker_fee) + (notional * self.margin_open_fee)

        logger.debug(f"[Broker] ENTRY {direction.upper()} fill: "
                     f"mark={mark_price:.2f} fill={fill_price:.2f} "
                     f"contracts={contracts:.6f} notional=${notional:.2f} fee=${fee:.4f}")

        return FillResult(
            filled=True,
            fill_price=fill_price,
            contracts=contracts,
            notional_usd=notional,
            fee_usd=fee,
        )

    # ── Exit fill ─────────────────────────────────────────────────────────────

    def fill_exit(
        self,
        direction:    str,
        mark_price:   float,
        contracts:    float,
        entry_notional: float,
        hold_hours:   float = 0.0,   # For optional rollover fee
    ) -> FillResult:
        """
        Simulate an exit market order fill with slippage.
        Applies taker fee on exit notional.
        Optionally applies rollover fee based on hold duration.
        """
        # Exit slippage is adverse (opposite direction from entry)
        if direction == "long":
            fill_price = mark_price * (1 - self.slippage_pct)
        else:
            fill_price = mark_price * (1 + self.slippage_pct)

        notional = fill_price * contracts
        exit_fee = notional * self.taker_fee

        # Rollover: 0.02% per 4h period held
        rollover_fee = 0.0
        if self.charge_rollover and hold_hours > 0:
            periods = hold_hours / 4
            rollover_fee = entry_notional * self.rollover_fee * periods

        total_fee = exit_fee + rollover_fee

        logger.debug(f"[Broker] EXIT {direction.upper()} fill: "
                     f"mark={mark_price:.2f} fill={fill_price:.2f} "
                     f"contracts={contracts:.6f} fee=${total_fee:.4f}")

        return FillResult(
            filled=True,
            fill_price=fill_price,
            contracts=contracts,
            notional_usd=notional,
            fee_usd=total_fee,
        )

    # ── Fee floor check ───────────────────────────────────────────────────────

    def passes_fee_floor(
        self,
        entry_price:   float,
        stop_price:    float,
        target_price:  float,
        contracts:     float,
        min_fee_r:     float = 1.0,
    ) -> tuple[bool, str]:
        """
        Reject trade if projected 1R profit doesn't exceed round-trip fees.
        Matches live entry_engine.py fee floor gate.

        Returns (passes: bool, reason: str)
        """
        r_distance   = abs(entry_price - stop_price)
        r_profit_usd = r_distance * contracts

        # Round-trip: entry taker + margin open + exit taker
        entry_notional = entry_price * contracts
        exit_notional  = target_price * contracts
        round_trip_fee = (
            entry_notional * self.taker_fee +
            entry_notional * self.margin_open_fee +
            exit_notional  * self.taker_fee
        )

        threshold = round_trip_fee * min_fee_r

        if r_profit_usd < threshold:
            reason = (
                f"Fee floor fail: 1R=${r_profit_usd:.4f} < "
                f"fees×{min_fee_r}=${threshold:.4f}"
            )
            return False, reason

        return True, "ok"

    # ── Round-trip fee estimate ────────────────────────────────────────────────

    def estimate_round_trip_fee(self, notional_usd: float) -> float:
        """
        Quick estimate of total round-trip fees for a given notional.
        Used by optimizer to pre-filter parameter combinations.
        """
        return notional_usd * (self.taker_fee * 2 + self.margin_open_fee)

    # ── Summary ───────────────────────────────────────────────────────────────

    def fee_summary(self) -> dict:
        return {
            "slippage_pct":    self.slippage_pct,
            "taker_fee":       self.taker_fee,
            "margin_open_fee": self.margin_open_fee,
            "charge_rollover": self.charge_rollover,
            "rollover_fee":    self.rollover_fee,
        }
