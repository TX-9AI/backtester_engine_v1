# backtest/replay.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Sequential candle replay through bot strategy stack
# v1.1 — 2026-06-28 — Perf: resample higher TFs only on candle boundary, not every tick
#                      Use deque for rolling window, avoid full DataFrame rebuild each candle
# v1.2 — 2026-06-28 — Add: progress bar with candle count, trades, balance, elapsed time
# v1.3 — 2026-06-28 — Add: rejection counter — tallies why signals were blocked, prints summary
# v1.4 — 2026-06-28 — Fix: resample only OHLCV columns (vwap column was causing pandas error)
#                      Fix: boundary check uses absolute minute count not minute_of_day
# v1.5 — 2026-06-29 — Add: ruin check — halts replay if balance drops below 10% of starting

"""
Feeds historical 1m OHLCV candles into the strategy code one candle at a time,
simulating the bot's live tick loop without real time or network calls.

The replay engine:
  1. Builds a rolling window of candles per timeframe (1m/5m/15m/1h/1d)
  2. On each 1m close, resamples higher timeframes
  3. Calls volatility_engine → regime_classifier → strategy → entry validation
  4. Hands valid signals to SimulatedBroker for fill and position management
  5. Returns completed trade list to BacktestLogger

Key design decisions:
  - Candle-close based (no intra-candle fills except partial/stop)
  - Stop and trail evaluated at every 1m candle using high/low of that candle
  - VWAP resets at midnight UTC (matches live bot behavior)
  - No Telegram, no SQLite writes during replay (that's BacktestLogger's job)
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ─── CANDLE WINDOW SIZES (match live bot TIMEFRAMES config) ──────────────────

WINDOW_SIZES = {
    "1m":  60,
    "5m":  100,
    "15m": 50,
    "1h":  50,
    "1d":  10,
}

RESAMPLE_MAP = {
    "5m":  5,
    "15m": 15,
    "1h":  60,
    "1d":  1440,
}


# ─── REPLAY CONFIG ────────────────────────────────────────────────────────────

@dataclass
class ReplayConfig:
    """User-defined session parameters for a backtest run."""
    starting_balance_usd: float = 1000.0
    leverage: int = 10
    paper_slippage_pct: float = 0.001        # 0.1% per fill
    kraken_taker_fee: float = 0.0080
    kraken_margin_open_fee: float = 0.0002
    grade_a_notional_pct: float = 0.90
    grade_b_notional_pct: float = 0.75
    min_rrr: float = 1.5
    min_fee_adjusted_r: float = 1.0
    atr_stop_multiplier: float = 1.5
    adx_trend_threshold: int = 20
    adx_range_threshold: int = 15
    bb_width_compression_pct: float = 0.20
    trail_activation_r: float = 1.0
    partial_exit_pct: float = 0.50
    partial_minimum_r: float = 0.75
    stagnant_trade_minutes: int = 120
    entry_cooldown_minutes: int = 5
    vwap_filter_active: bool = True
    sweep_rejection_candles: int = 2
    # ADX stop multiplier breakpoints
    adx_mult_low: float = 1.0       # ADX < 25
    adx_mult_mid: float = 1.5       # ADX 25–40
    adx_mult_high: float = 2.0      # ADX 40–60
    adx_mult_extreme: float = 2.5   # ADX > 60
    min_daily_range_pct: float = 0.008

    def adx_multiplier(self, adx: float) -> float:
        if adx < 25:
            return self.adx_mult_low
        elif adx < 40:
            return self.adx_mult_mid
        elif adx < 60:
            return self.adx_mult_high
        else:
            return self.adx_mult_extreme


# ─── CANDLE BUFFER ────────────────────────────────────────────────────────────

class CandleBuffer:
    """
    Maintains a rolling window of OHLCV candles per timeframe.
    1m is fed directly. Higher TFs are resampled only when a new
    higher-TF candle boundary is crossed — not on every 1m tick.
    """

    def __init__(self):
        # Rolling deque capped at max window size needed for resampling
        # Keep enough 1m candles to build the largest TF window
        max_1m = max(WINDOW_SIZES["1m"],
                     WINDOW_SIZES["1h"]  * 60,
                     WINDOW_SIZES["15m"] * 15,
                     WINDOW_SIZES["5m"]  * 5,
                     WINDOW_SIZES["1d"]  * 1440)
        self._1m_history: deque = deque(maxlen=max_1m)
        self.windows: dict[str, pd.DataFrame] = {tf: pd.DataFrame() for tf in WINDOW_SIZES}
        self._candle_count: int = 0

    def push(self, candle: dict) -> None:
        """Ingest a new closed 1m candle. Resample higher TFs only on boundary."""
        self._1m_history.append(candle)
        self._candle_count += 1

        # Always update 1m window (drop vwap — not an OHLCV column)
        history_slice = list(self._1m_history)[-WINDOW_SIZES["1m"]:]
        df_1m = pd.DataFrame(history_slice)[["timestamp","open","high","low","close","volume"]]
        self.windows["1m"] = df_1m

        if len(self._1m_history) < 2:
            return

        # Resample only when a higher TF boundary is crossed
        # Use absolute candle count modulo to avoid midnight reset issues
        needs_resample = False
        for tf, minutes in RESAMPLE_MAP.items():
            if self._candle_count % minutes == 0:
                needs_resample = True
                break

        if not needs_resample:
            return

        # Build OHLCV-only DataFrame for resampling (exclude vwap and other extras)
        history_list = list(self._1m_history)
        full_1m = pd.DataFrame(history_list)[["timestamp","open","high","low","close","volume"]]
        full_1m["timestamp"] = pd.to_datetime(full_1m["timestamp"], utc=True)
        full_1m = full_1m.set_index("timestamp")

        for tf, minutes in RESAMPLE_MAP.items():
            rule = f"{minutes}min"
            try:
                resampled = full_1m.resample(rule, closed="right", label="right").agg({
                    "open":   "first",
                    "high":   "max",
                    "low":    "min",
                    "close":  "last",
                    "volume": "sum",
                }).dropna()
                self.windows[tf] = resampled.reset_index().tail(
                    WINDOW_SIZES[tf]).reset_index(drop=True)
            except Exception:
                pass

    def ready(self) -> bool:
        """True once we have enough candles to compute indicators reliably."""
        return len(self._1m_history) >= 200

    def get(self, tf: str) -> pd.DataFrame:
        return self.windows.get(tf, pd.DataFrame())

    def vwap_reset_needed(self, prev_candle: Optional[dict], cur_candle: dict) -> bool:
        """True if we crossed midnight UTC — VWAP resets daily."""
        if prev_candle is None:
            return True
        prev_ts = pd.to_datetime(prev_candle["timestamp"], utc=True)
        cur_ts  = pd.to_datetime(cur_candle["timestamp"], utc=True)
        return prev_ts.date() != cur_ts.date()


# ─── OPEN POSITION TRACKER ────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    direction: str          # "long" or "short"
    entry_price: float
    stop_price: float
    target_price: float
    contracts: float        # BTC contracts
    notional_usd: float
    grade: str
    strategy: str
    regime: str
    entry_time: datetime
    r_distance: float       # price distance of 1R at entry

    # Trail state
    partial_taken: bool = False
    trail_active: bool = False
    trail_stop: Optional[float] = None
    highest_r_reached: float = 0.0

    # Stagnant timer
    stagnant_exit_time: Optional[datetime] = None


# ─── REPLAY ENGINE ────────────────────────────────────────────────────────────

class ReplayEngine:
    """
    Core backtest replay loop.

    Usage:
        engine = ReplayEngine(config, strategy_bundle)
        trades = engine.run(df_1m_candles)
    """

    def __init__(self, config: ReplayConfig, strategy_bundle):
        """
        config         — ReplayConfig instance
        strategy_bundle — object exposing:
            .compute_indicators(windows) → indicators dict
            .classify_regime(indicators) → (regime, conviction)
            .generate_signal(regime, conviction, indicators) → signal or None
            .validate_entry(signal, indicators, config) → bool
            .compute_size(signal, indicators, cash_balance, config) → (contracts, notional)
        """
        self.config  = config
        self.bundle  = strategy_bundle
        self.buffer  = CandleBuffer()

        self.cash_balance    = config.starting_balance_usd
        self.open_position: Optional[OpenPosition] = None
        self.completed_trades: list[dict] = []
        self.equity_curve: list[dict] = []

        self._last_candle: Optional[dict] = None
        self._last_entry_time: Optional[datetime] = None
        self._last_stop_time: Optional[datetime] = None
        self._vwap_cumulative_pv: float = 0.0
        self._vwap_cumulative_vol: float = 0.0
        self._candle_index: int = 0

        # Rejection counter — tracks why entries were blocked
        self.rejection_counts: dict[str, int] = {
            "warmup":            0,
            "in_position":       0,
            "cooldown":          0,
            "whipsaw_guard":     0,
            "daily_range":       0,
            "no_indicators":     0,
            "no_regime":         0,
            "no_signal":         0,
            "strategy_error":    0,
            "vwap_gate":         0,
            "rrr_too_low":       0,
            "grade_c":           0,
            "validation_error":  0,
            "sizing_error":      0,
            "zero_size":         0,
        }

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame) -> list[dict]:
        """
        Feed entire DataFrame of 1m candles through the replay engine.
        Returns list of completed trade dicts.
        """
        import sys
        import time

        total    = len(df)
        bar_width = 30
        start_t  = time.time()

        logger.info(f"[Replay] Starting replay: {total:,} candles | "
                    f"Balance: ${self.cash_balance:,.2f}")

        starting_balance = self.cash_balance
        ruin_threshold   = starting_balance * 0.10   # Halt at 10% of starting balance
        ruined           = False

        for i, (_, row) in enumerate(df.iterrows()):
            candle = row.to_dict()
            self._process_candle(candle)
            self._candle_index += 1

            # Ruin check — halt if balance falls below 10% of start
            if self.cash_balance < ruin_threshold and self.open_position is None:
                sys.stdout.write("\n")
                print(f"\n  ⚠  RUIN: Balance ${self.cash_balance:,.2f} fell below "
                      f"10% of starting ${starting_balance:,.2f} — halting replay.")
                ruined = True
                break

            # Progress bar every 500 candles
            if i % 500 == 0 or i == total - 1:
                pct      = (i + 1) / total
                filled   = int(bar_width * pct)
                bar      = "█" * filled + "░" * (bar_width - filled)
                elapsed  = time.time() - start_t
                trades   = len(self.completed_trades)
                balance  = self.cash_balance
                sys.stdout.write(
                    f"\r  [{bar}] {pct*100:5.1f}%  "
                    f"{i+1:,}/{total:,}  "
                    f"trades={trades}  "
                    f"balance=${balance:,.0f}  "
                    f"{elapsed:.0f}s"
                )
                sys.stdout.flush()

        sys.stdout.write("\n")
        sys.stdout.flush()

        # Force-close any open position at end of data
        if self.open_position is not None:
            last = self._last_candle
            if last:
                self._close_position(last["close"], last["timestamp"], "end_of_data")

        logger.info(f"[Replay] Complete. Trades: {len(self.completed_trades)} | "
                    f"Final balance: ${self.cash_balance:,.2f}")

        self._print_rejection_summary()
        return self.completed_trades

    def _print_rejection_summary(self) -> None:
        """Print a summary of why entries were blocked during the replay."""
        total = sum(self.rejection_counts.values())
        trades = len(self.completed_trades)
        if total == 0 and trades == 0:
            print("\n  ⚠  No signals generated — strategy stack may not be computing indicators correctly.")
            return

        labels = {
            "no_signal":        "No signal from strategy",
            "no_indicators":    "Indicators failed to compute",
            "no_regime":        "No regime classified",
            "strategy_error":   "Strategy threw exception",
            "vwap_gate":        "VWAP gate block",
            "rrr_too_low":      "R:R below minimum",
            "grade_c":          "Grade C disabled",
            "cooldown":         "Entry cooldown active",
            "whipsaw_guard":    "Whipsaw guard (post-stop block)",
            "daily_range":      "Daily range too small",
            "validation_error": "Validation threw exception",
            "sizing_error":     "Position sizing threw exception",
            "zero_size":        "Position size was zero",
            "warmup":           "Warmup period",
            "in_position":      "Already in position",
        }

        print(f"\n  {'─'*52}")
        print(f"  SIGNAL REJECTION BREAKDOWN")
        print(f"  {'─'*52}")
        print(f"  Trades taken:     {trades}")
        print(f"  Total rejections: {total:,}")
        print(f"  {'─'*52}")

        # Sort by count descending, skip zeros
        sorted_counts = sorted(
            [(k, v) for k, v in self.rejection_counts.items() if v > 0],
            key=lambda x: -x[1]
        )
        for key, count in sorted_counts:
            label = labels.get(key, key)
            pct   = count / total * 100 if total > 0 else 0
            bar   = "█" * min(int(pct / 2), 25)
            print(f"  {label:<35} {count:>7,}  ({pct:5.1f}%)  {bar}")
        print(f"  {'─'*52}\n")

    # ── Candle processing ─────────────────────────────────────────────────────

    def _process_candle(self, candle: dict) -> None:
        ts = pd.to_datetime(candle["timestamp"], utc=True)

        # VWAP reset at midnight
        if self.buffer.vwap_reset_needed(self._last_candle, candle):
            self._vwap_cumulative_pv  = 0.0
            self._vwap_cumulative_vol = 0.0

        # Update VWAP accumulators
        typical_price = (candle["high"] + candle["low"] + candle["close"]) / 3
        self._vwap_cumulative_pv  += typical_price * candle["volume"]
        self._vwap_cumulative_vol += candle["volume"]
        candle["vwap"] = (self._vwap_cumulative_pv / self._vwap_cumulative_vol
                          if self._vwap_cumulative_vol > 0 else candle["close"])

        # Push candle to buffer
        self.buffer.push(candle)

        # Wait for warm-up
        if not self.buffer.ready():
            self._last_candle = candle
            return

        # ── Manage open position ─────────────────────────────────────────────
        if self.open_position is not None:
            self._manage_position(candle)
            # If position was closed by stop/target intra-candle, don't enter new one same bar
            if self.open_position is None:
                self._record_equity(candle)
                self._last_candle = candle
                return

        # ── Seek new entry ────────────────────────────────────────────────────
        if self.open_position is None:
            self._seek_entry(candle)

        self._record_equity(candle)
        self._last_candle = candle

    # ── Position management ───────────────────────────────────────────────────

    def _manage_position(self, candle: dict) -> None:
        pos   = self.open_position
        high  = candle["high"]
        low   = candle["low"]
        close = candle["close"]
        ts    = pd.to_datetime(candle["timestamp"], utc=True)

        r = pos.r_distance
        entry = pos.entry_price

        # Current R achieved (use candle high/low for position direction)
        if pos.direction == "long":
            current_r = (high - entry) / r if r > 0 else 0
        else:
            current_r = (entry - low) / r if r > 0 else 0

        pos.highest_r_reached = max(pos.highest_r_reached, current_r)

        # ── Stagnant exit ────────────────────────────────────────────────────
        if pos.stagnant_exit_time is None:
            pos.stagnant_exit_time = pos.entry_time + pd.Timedelta(
                minutes=self.config.stagnant_trade_minutes
            )
        if ts >= pos.stagnant_exit_time:
            self._close_position(close, ts, "stagnant_timeout")
            return

        # ── Partial exit at 0.75R ─────────────────────────────────────────
        if not pos.partial_taken and pos.highest_r_reached >= self.config.partial_minimum_r:
            partial_contracts = pos.contracts * self.config.partial_exit_pct
            partial_pnl = self._calc_pnl(pos, close, partial_contracts)
            self.cash_balance += partial_pnl
            pos.contracts -= partial_contracts
            pos.partial_taken = True
            logger.debug(f"[Replay] Partial exit at {close:.2f} | R={pos.highest_r_reached:.2f} | "
                         f"PnL=${partial_pnl:.2f}")

        # ── Trail activation ─────────────────────────────────────────────────
        if not pos.trail_active and pos.highest_r_reached >= self.config.trail_activation_r:
            pos.trail_active = True
            pos.trail_stop   = self._trail_stop_price(pos, 1)

        # ── Update trail stop ─────────────────────────────────────────────────
        if pos.trail_active:
            r_step = int(pos.highest_r_reached)
            new_trail = self._trail_stop_price(pos, r_step)
            if pos.direction == "long":
                pos.trail_stop = max(pos.trail_stop or 0, new_trail)
            else:
                pos.trail_stop = min(pos.trail_stop or float("inf"), new_trail)

        # ── Check stop hit (use low/high of candle) ───────────────────────────
        effective_stop = pos.trail_stop if pos.trail_active else pos.stop_price

        if pos.direction == "long" and low <= effective_stop:
            exit_price = min(effective_stop, close)   # Assume fill at stop or worse
            self._close_position(exit_price, ts, "stop_loss")
            self._last_stop_time = ts
            return

        if pos.direction == "short" and high >= effective_stop:
            exit_price = max(effective_stop, close)
            self._close_position(exit_price, ts, "stop_loss")
            self._last_stop_time = ts
            return

        # ── Check target hit ─────────────────────────────────────────────────
        if pos.direction == "long" and high >= pos.target_price:
            self._close_position(pos.target_price, ts, "target")
            return

        if pos.direction == "short" and low <= pos.target_price:
            self._close_position(pos.target_price, ts, "target")
            return

    def _trail_stop_price(self, pos: OpenPosition, r_step: int) -> float:
        """Calculate trail stop price for a given R step."""
        r = pos.r_distance
        if pos.direction == "long":
            return pos.entry_price + (r_step - 1) * r
        else:
            return pos.entry_price - (r_step - 1) * r

    # ── Entry seeking ─────────────────────────────────────────────────────────

    def _seek_entry(self, candle: dict) -> None:
        ts = pd.to_datetime(candle["timestamp"], utc=True)

        # Cooldown after any entry
        if self._last_entry_time is not None:
            elapsed = (ts - self._last_entry_time).total_seconds() / 60
            if elapsed < self.config.entry_cooldown_minutes:
                self.rejection_counts["cooldown"] += 1
                return

        # Whipsaw guard — 15 min block after stop hit
        if self._last_stop_time is not None:
            elapsed = (ts - self._last_stop_time).total_seconds() / 60
            if elapsed < 15:
                self.rejection_counts["whipsaw_guard"] += 1
                return

        # Daily range filter
        windows = self.buffer.windows
        if "1d" in windows and len(windows["1d"]) > 0:
            daily = windows["1d"].iloc[-1]
            if daily["high"] > 0 and daily["low"] > 0:
                daily_range = (daily["high"] - daily["low"]) / daily["low"]
                if daily_range < self.config.min_daily_range_pct:
                    self.rejection_counts["daily_range"] += 1
                    return

        # Run strategy stack
        try:
            indicators = self.bundle.compute_indicators(windows, candle)
            if not indicators:
                self.rejection_counts["no_indicators"] += 1
                return
            regime, conviction = self.bundle.classify_regime(indicators, self.config)
            if regime is None:
                self.rejection_counts["no_regime"] += 1
                return
            signal = self.bundle.generate_signal(regime, conviction, indicators, candle, self.config)
        except Exception as e:
            logger.warning(f"[Replay] Strategy error at {ts}: {e}")
            self.rejection_counts["strategy_error"] += 1
            return

        if signal is None:
            self.rejection_counts["no_signal"] += 1
            return

        # Entry validation
        try:
            valid = self.bundle.validate_entry(signal, indicators, self.config)
        except Exception as e:
            logger.warning(f"[Replay] Validation error at {ts}: {e}")
            self.rejection_counts["validation_error"] += 1
            return

        if not valid:
            # Determine which gate blocked it
            direction = signal.get("direction")
            vwap = indicators.get("vwap", 0)
            entry = signal.get("entry", 0)
            grade = signal.get("grade", "B")
            if grade == "C":
                self.rejection_counts["grade_c"] += 1
            elif self.config.vwap_filter_active and vwap > 0:
                if (direction == "short" and entry > vwap) or \
                   (direction == "long"  and entry < vwap):
                    self.rejection_counts["vwap_gate"] += 1
                else:
                    self.rejection_counts["rrr_too_low"] += 1
            else:
                self.rejection_counts["rrr_too_low"] += 1
            return

        # Size the position
        try:
            contracts, notional = self.bundle.compute_size(
                signal, indicators, self.cash_balance, self.config
            )
        except Exception as e:
            logger.warning(f"[Replay] Sizing error at {ts}: {e}")
            self.rejection_counts["sizing_error"] += 1
            return

        if contracts <= 0 or notional <= 0:
            self.rejection_counts["zero_size"] += 1
            return

        # Open position
        entry_price = candle["close"] * (1 + self.config.paper_slippage_pct
                                          if signal["direction"] == "long"
                                          else 1 - self.config.paper_slippage_pct)
        r_distance  = abs(entry_price - signal["stop"])

        self.open_position = OpenPosition(
            direction    = signal["direction"],
            entry_price  = entry_price,
            stop_price   = signal["stop"],
            target_price = signal["target"],
            contracts    = contracts,
            notional_usd = notional,
            grade        = signal.get("grade", "B"),
            strategy     = signal.get("strategy", "unknown"),
            regime       = signal.get("regime", "unknown"),
            entry_time   = ts,
            r_distance   = r_distance if r_distance > 0 else entry_price * 0.005,
        )
        self._last_entry_time = ts

        logger.info(f"[Replay] ENTRY {signal['direction'].upper()} @ {entry_price:.2f} | "
                    f"Stop={signal['stop']:.2f} Target={signal['target']:.2f} | "
                    f"Grade={signal.get('grade','?')} | Regime={signal.get('regime','?')} | "
                    f"Contracts={contracts:.6f} | Notional=${notional:.2f}")

    # ── Position close ────────────────────────────────────────────────────────

    def _close_position(self, exit_price: float, ts, exit_reason: str) -> None:
        pos = self.open_position
        if pos is None:
            return

        # Apply slippage on exit
        if pos.direction == "long":
            fill_price = exit_price * (1 - self.config.paper_slippage_pct)
        else:
            fill_price = exit_price * (1 + self.config.paper_slippage_pct)

        gross_pnl = self._calc_pnl(pos, fill_price, pos.contracts)

        # Fees: taker on entry + exit, margin open fee
        entry_fee = pos.notional_usd * self.config.kraken_taker_fee
        exit_fee  = (fill_price * pos.contracts) * self.config.kraken_taker_fee
        margin_fee = pos.notional_usd * self.config.kraken_margin_open_fee
        total_fees = entry_fee + exit_fee + margin_fee

        net_pnl = gross_pnl - total_fees
        self.cash_balance += net_pnl

        r_achieved = gross_pnl / (pos.r_distance * pos.contracts) if pos.r_distance > 0 else 0

        trade = {
            "entry_time":    pos.entry_time.isoformat(),
            "exit_time":     pd.to_datetime(ts, utc=True).isoformat(),
            "direction":     pos.direction,
            "entry_price":   pos.entry_price,
            "exit_price":    fill_price,
            "stop_price":    pos.stop_price,
            "target_price":  pos.target_price,
            "contracts":     pos.contracts,
            "notional_usd":  pos.notional_usd,
            "gross_pnl":     round(gross_pnl, 4),
            "fees":          round(total_fees, 4),
            "net_pnl":       round(net_pnl, 4),
            "r_achieved":    round(r_achieved, 3),
            "grade":         pos.grade,
            "strategy":      pos.strategy,
            "regime":        pos.regime,
            "exit_reason":   exit_reason,
            "partial_taken": pos.partial_taken,
            "trail_active":  pos.trail_active,
            "cash_after":    round(self.cash_balance, 2),
        }

        self.completed_trades.append(trade)
        logger.info(f"[Replay] EXIT {exit_reason.upper()} @ {fill_price:.2f} | "
                    f"Net PnL=${net_pnl:.2f} | R={r_achieved:.2f} | "
                    f"Balance=${self.cash_balance:,.2f}")

        self.open_position = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _calc_pnl(self, pos: OpenPosition, exit_price: float, contracts: float) -> float:
        if pos.direction == "long":
            return (exit_price - pos.entry_price) * contracts
        else:
            return (pos.entry_price - exit_price) * contracts

    def _record_equity(self, candle: dict) -> None:
        unrealized = 0.0
        if self.open_position is not None:
            pos = self.open_position
            price = candle["close"]
            unrealized = self._calc_pnl(pos, price, pos.contracts)

        self.equity_curve.append({
            "timestamp":  candle["timestamp"],
            "cash":       round(self.cash_balance, 2),
            "unrealized": round(unrealized, 2),
            "equity":     round(self.cash_balance + unrealized, 2),
        })
