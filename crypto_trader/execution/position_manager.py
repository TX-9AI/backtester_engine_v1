"""
execution/position_manager.py — Tracks open position state across ticks.
Single source of truth for: is there an open trade? what is its status?
Recovers open trades automatically on restart.
"""

import logging
from typing import Optional
from datetime import datetime

from database.trade_logger import TradeRecord, get_trade_logger
from execution.order_router import get_order_router
from execution.exit_engine import get_exit_engine, ExitDecision
from analysis.structure_analyzer import StructureMap
from risk.risk_manager import get_risk_manager
from notifications.alert_manager import get_alert_manager
from config import PAPER_TRADING
from utils.math_utils import r_multiple, unrealized_pnl
from utils.time_utils import fmt_et_short, ts_for_db

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Manages the lifecycle of the current open position.
    On each tick, evaluates exits, updates stops, and logs changes.
    Automatically recovers open trades from DB on startup/restart.
    """

    def __init__(self, paper_trading: bool = PAPER_TRADING):
        self.paper_trading  = paper_trading
        self._trade_logger  = get_trade_logger()
        self._exit_engine   = get_exit_engine()
        self._order_router  = get_order_router(paper_trading)
        self._risk_manager  = get_risk_manager()
        self._alert_manager = get_alert_manager()

        # Cached open trade record (refreshed from DB on each tick)
        self._open_record: Optional[dict] = None

        # On startup, recover any open trade left from previous session
        self._recover_open_trade()

    def _recover_open_trade(self):
        """
        Kraken-first position recovery on every startup, restart, and reboot.

        Kraken is the source of truth — not the DB.
        The DB is queried only to recover stop/risk parameters for a position
        that Kraken confirms actually exists.

        States:
          Kraken=open  DB=found   → Resume managing with known stop/risk params.
          Kraken=open  DB=missing → Orphan position. Alert operator. Do not manage
                                    automatically — stop/risk params unknown.
          Kraken=none             → Nothing to manage. If DB has a stale open
                                    record, mark it closed (position resolved
                                    while bot was offline).
          Paper mode              → DB only (no real Kraken position exists).
        """
        from execution.order_router import get_order_router

        # ── Paper mode: DB is the only source ─────────────────────────────
        if self.paper_trading:
            db_record = self._trade_logger.get_open_trade()
            if db_record is None:
                logger.info("Startup recovery: no open paper position. Clean start.")
                return
            self._open_record = db_record
            trade_id  = db_record["trade_id"]
            direction = db_record["direction"]
            entry     = db_record["entry_price"]
            stop      = db_record["stop_price"]
            size      = db_record["position_size"]
            risk      = db_record.get("risk_usd") or abs(entry - stop) * size
            self._risk_manager.add_open_risk(risk)
            logger.warning(
                f"⚠️  [PAPER] Recovered open trade: {trade_id[:8]} "
                f"{direction.upper()} entry=${entry:,.2f} stop=${stop:,.2f}"
            )
            return

        # ── Live mode: Kraken is the source of truth ───────────────────────
        kraken_position = None
        try:
            router    = get_order_router(paper_trading=False)
            positions = router.get_open_positions()
            for p in (positions or []):
                contracts = float(p.get("contracts") or
                                  p.get("info", {}).get("vol", 0) or 0)
                if contracts > 0:
                    kraken_position = p
                    break
        except Exception as e:
            logger.warning(
                f"Could not query Kraken positions on startup: {e}. "
                f"Falling back to DB record."
            )
            # If Kraken is unreachable, fall back to DB to avoid going blind
            db_record = self._trade_logger.get_open_trade()
            if db_record:
                self._open_record = db_record
                risk = db_record.get("risk_usd") or 0
                self._risk_manager.add_open_risk(risk)
                logger.warning(
                    f"⚠️  Kraken unreachable — resuming from DB record "
                    f"{db_record['trade_id'][:8]}. Verify position manually."
                )
            return

        # ── Kraken has no open position ────────────────────────────────────
        if kraken_position is None:
            logger.info("Startup recovery: no open position on Kraken. Clean start.")
            # Clean up any stale DB record so it doesn't cause confusion
            db_record = self._trade_logger.get_open_trade()
            if db_record:
                trade_id = db_record["trade_id"]
                logger.warning(
                    f"Stale open record {trade_id[:8]} in DB but no position "
                    f"on Kraken — marking closed (resolved while offline)."
                )
                try:
                    from data.market_data import get_current_price
                    last_price = get_current_price() or db_record["entry_price"]
                    from database.trade_logger import TradeRecord as TR
                    db_r = self._trade_logger.get_open_trade()
                    exit_r = TR()
                    exit_r.trade_id      = trade_id
                    exit_r.direction     = db_r.get("direction", "") if db_r else ""
                    exit_r.entry_price   = db_r.get("entry_price", 0) if db_r else 0
                    exit_r.exit_price    = last_price
                    exit_r.exit_reason   = "closed_while_offline"
                    exit_r.exit_order_id = ""
                    exit_r.pnl_usd       = 0.0
                    exit_r.pnl_r         = 0.0
                    exit_r.commission_usd = 0.0
                    exit_r.paper_trade   = 0
                    exit_r.status        = "closed"
                    self._trade_logger.log_exit(exit_r)
                    self._alert_manager._send_deduped(
                        trade_id=trade_id,
                        alert_type="closed_offline",
                        subject=f"⚠️ Position closed while bot was offline",
                        body=(
                            f"No open position found on Kraken at restart.\n"
                            f"DB record {trade_id[:8]} marked closed.\n"
                            f"Check Kraken trade history for final P&L."
                        )
                    )
                except Exception as e:
                    logger.error(f"Could not clean up stale DB record: {e}")
            return

        # ── Kraken has an open position ────────────────────────────────────
        size = float(kraken_position.get("contracts") or
                     kraken_position.get("info", {}).get("vol", 0) or 0)
        side = kraken_position.get("side", "unknown")

        # Try to find matching DB record for stop/risk params
        db_record = self._trade_logger.get_open_trade()

        if db_record is not None:
            # Normal recovery — Kraken confirms position, DB has the params
            self._open_record = db_record
            trade_id  = db_record["trade_id"]
            direction = db_record["direction"]
            entry     = db_record["entry_price"]
            stop      = db_record["stop_price"]
            risk      = db_record.get("risk_usd") or abs(entry - stop) * size
            self._risk_manager.add_open_risk(risk)
            logger.warning(
                f"⚠️  [LIVE] Recovered open position from Kraken: "
                f"{trade_id[:8]} {direction.upper()} "
                f"entry=${entry:,.2f} stop=${stop:,.2f} "
                f"size={size:.4f} risk=${risk:.2f}"
            )
            try:
                self._alert_manager._send_deduped(
                    trade_id=trade_id,
                    alert_type="recovery",
                    subject=f"⚠️ Live position recovered after restart: {direction.upper()} @ ${entry:,.2f}",
                    body=(
                        f"BOT RESTARTED — LIVE POSITION RECOVERED\n"
                        f"{'─'*40}\n"
                        f"ID:        {trade_id[:8]}\n"
                        f"Direction: {direction.upper()}\n"
                        f"Entry:     ${entry:,.2f}\n"
                        f"Stop:      ${stop:,.2f}\n"
                        f"Size:      {size:.4f} BTC\n"
                        f"Risk:      ${risk:.2f}\n\n"
                        f"Confirmed open on Kraken. Resuming management."
                    )
                )
            except Exception as e:
                logger.warning(f"Could not send recovery alert: {e}")

        else:
            # Orphan — Kraken has a position but no DB record
            # Cannot manage safely without stop/risk params
            logger.critical(
                f"🚨 ORPHAN POSITION on Kraken (size={size:.4f} side={side}) "
                f"with NO matching DB record. "
                f"Bot will NOT manage this position. Close manually on Kraken."
            )
            try:
                self._alert_manager._send_deduped(
                    trade_id="orphan",
                    alert_type="orphan_position",
                    subject="🚨 ORPHAN POSITION — manual action required",
                    body=(
                        f"OPEN POSITION ON KRAKEN WITH NO DB RECORD\n"
                        f"{'─'*40}\n"
                        f"Size: {size:.4f} BTC  Side: {side}\n\n"
                        f"Cannot manage — stop and risk parameters unknown.\n"
                        f"ACTION REQUIRED:\n"
                        f"  1. Close position manually on Kraken\n"
                        f"  2. Restart bot"
                    )
                )
            except Exception as e:
                logger.warning(f"Could not send orphan alert: {e}")
            # _open_record stays None — bot will not enter new trades
            # until the orphan is resolved and bot is restarted

    def has_open_position(self) -> bool:
        """Check if there is currently an open trade."""
        record = self._trade_logger.get_open_trade()
        self._open_record = record
        return record is not None

    def get_open_record(self) -> Optional[dict]:
        """Return the current open trade record."""
        return self._open_record

    def manage_open_position(self,
                              current_price:  float,
                              structure:      StructureMap,
                              current_regime: str,
                              atr:            float) -> bool:
        """
        Called every tick when a position is open.
        Evaluates exit conditions and acts accordingly.

        In live mode, periodically verifies the position still exists on Kraken
        so manual closes are detected quickly without waiting for a failed order.

        Returns:
            True if position is still open, False if it was closed.
        """
        record = self._trade_logger.get_open_trade()
        if record is None:
            self._open_record = None
            return False

        # ── Periodic Kraken position verification (live mode only) ────────────
        # Every 10 ticks, confirm the position still exists on Kraken.
        # Catches manual closes by the operator without waiting for a failed order.
        if not self.paper_trading:
            self._kraken_check_counter = getattr(self, "_kraken_check_counter", 0) + 1
            if self._kraken_check_counter >= 10:
                self._kraken_check_counter = 0
                if self._verify_position_closed_on_kraken():
                    trade_id  = record["trade_id"]
                    direction = record["direction"]
                    entry     = record["entry_price"]
                    logger.warning(
                        f"⚠️  Manual close detected on Kraken for {trade_id[:8]}. "
                        f"Recording exit and resuming normal operation."
                    )
                    try:
                        from database.trade_logger import TradeRecord as TR
                        from utils.math_utils import unrealized_pnl, r_multiple
                        size   = record["position_size"]
                        entry  = record["entry_price"]
                        stop   = record.get("initial_stop") or record["stop_price"]
                        pnl    = unrealized_pnl(entry, current_price, size, direction)
                        r      = r_multiple(entry, current_price, stop, direction)
                        exit_r = TR()
                        exit_r.trade_id      = trade_id
                        exit_r.direction     = direction
                        exit_r.entry_price   = entry
                        exit_r.exit_price    = current_price
                        exit_r.exit_reason   = "manual_close_detected"
                        exit_r.exit_order_id = "MANUAL"
                        exit_r.pnl_usd       = pnl
                        exit_r.pnl_r         = r
                        exit_r.commission_usd = 0.0
                        exit_r.paper_trade   = record.get("paper_trade", 0)
                        exit_r.status        = "closed"
                        self._trade_logger.log_exit(exit_r)
                        self._risk_manager.remove_open_risk(record.get("risk_usd", 0))
                        self._risk_manager.record_trade_result(pnl)
                        logger.info(f"[DB] Manual close logged: {trade_id[:8]} P&L=${pnl:.2f} ({r:.2f}R)")
                        self._alert_manager._send_deduped(
                            trade_id=trade_id,
                            alert_type="manual_close",
                            subject=f"Position manually closed: {direction.upper()} @ ${current_price:,.2f}",
                            body=(
                                f"MANUAL CLOSE DETECTED\n"
                                f"{'─'*40}\n"
                                f"ID:        {trade_id[:8]}\n"
                                f"Direction: {direction.upper()}\n"
                                f"Entry:     ${entry:,.2f}\n"
                                f"Detected:  ${current_price:,.2f}\n"
                                f"P&L:       ${pnl:.2f} ({r:.2f}R)\n\n"
                                f"Bot resuming normal operation."
                            )
                        )
                    except Exception as e:
                        logger.error(f"Could not record manual close: {e}")
                    self._open_record = None
                    return False

        self._open_record = record
        trade_id    = record["trade_id"]
        entry_regime = record.get("regime", current_regime)

        # Evaluate exit conditions
        decision: ExitDecision = self._exit_engine.evaluate(
            record=record,
            current_price=current_price,
            structure=structure,
            current_regime=current_regime,
            entry_regime=entry_regime,
            atr=atr
        )

        # ── FULL EXIT ─────────────────────────────────────────────────────────
        if decision.should_exit:
            return self._execute_full_exit(record, current_price, decision)

        # ── PARTIAL EXIT ──────────────────────────────────────────────────────
        if decision.should_partial and not record.get("partial_exit_time"):
            self._execute_partial_exit(record, current_price, decision)

        # ── STOP ADJUSTMENT ───────────────────────────────────────────────────
        if decision.new_stop is not None:
            old_stop = record["stop_price"]
            if abs(decision.new_stop - old_stop) > 0.10:  # Min $0.10 move
                self._update_stop(record, decision.new_stop, current_price, decision.current_r)

        return True


    def _verify_position_closed_on_kraken(self) -> bool:
        """
        Check whether the position is still open on Kraken using direct REST API.
        Returns True if position is gone, False if still open or unreachable.
        """
        import hashlib, hmac, base64, time, urllib.parse, requests, os
        try:
            try:
                from config import KRAKEN_API_KEY as api_key, KRAKEN_API_SECRET as api_secret
            except ImportError:
                api_key    = os.environ.get("KRAKEN_API_KEY", "")
                api_secret = os.environ.get("KRAKEN_API_SECRET", "")

            if not api_key or not api_secret:
                return False

            url   = "https://api.kraken.com/0/private/OpenPositions"
            nonce = str(int(time.time() * 1000))
            data  = {"nonce": nonce}

            post_data = urllib.parse.urlencode(data)
            encoded   = (nonce + post_data).encode()
            message   = "/0/private/OpenPositions".encode() + hashlib.sha256(encoded).digest()
            signature = base64.b64encode(
                hmac.new(base64.b64decode(api_secret), message, hashlib.sha512).digest()
            ).decode()

            headers  = {"API-Key": api_key, "API-Sign": signature}
            response = requests.post(url, data=data, headers=headers, timeout=10)
            result   = response.json()

            if result.get("error"):
                logger.warning(f"OpenPositions error: {result['error']}")
                return False  # Assume still open on error

            positions = result.get("result", {})
            # If any open positions exist, return False (still open)
            if positions:
                return False
            return True  # No open positions — was closed

        except Exception as e:
            logger.warning(f"Could not verify position on Kraken: {e}")
            return False  # Assume still open if unreachable

    def _execute_full_exit(self, record: dict,
                            current_price: float,
                            decision: ExitDecision) -> bool:
        """Place close order and record the full exit."""
        trade_id  = record["trade_id"]
        direction = record["direction"]
        size_btc  = record["position_size"]
        entry     = record["entry_price"]

        fill_price, order_id = self._order_router.close_position(
            direction=direction,
            size_btc=size_btc,
            reason=decision.exit_reason
        )

        if fill_price is None:
            # Close order failed — check if position still exists on Kraken
            # It may have been manually closed by the operator
            if not self.paper_trading:
                kraken_gone = self._verify_position_closed_on_kraken()
                if kraken_gone:
                    logger.warning(
                        f"Close order failed for {trade_id[:8]} but position "
                        f"no longer exists on Kraken — manually closed by operator. "
                        f"Recording exit at current price."
                    )
                    from data.market_data import get_current_price
                    fill_price = get_current_price() or entry
                    order_id   = "MANUAL-CLOSE"
                    decision.exit_reason = "manual_close_detected"
                    # Fall through to record the exit below
                else:
                    logger.error(f"Full exit failed for {trade_id[:8]} — order not placed")
                    return True  # Position still open, retry next tick
            else:
                logger.error(f"Full exit failed for {trade_id[:8]} — order not placed")
                return True

        pnl         = unrealized_pnl(entry, fill_price, size_btc, direction)
        partial_pnl = record.get("partial_pnl_usd") or 0.0
        total_pnl   = pnl + partial_pnl

        stop_initial = record.get("initial_stop") or record["stop_price"]
        final_r = r_multiple(entry, fill_price, stop_initial, direction)

        # Update DB directly using the original trade_id from the record
        # Do NOT reconstruct a TradeRecord — that generates a new UUID
        from utils.time_utils import ts_for_db
        self._trade_logger.db.execute("""
            UPDATE trades SET
                status='closed',
                exit_price=?, exit_time=?, exit_reason=?,
                exit_order_id=?, pnl_usd=?, pnl_r=?,
                updated_at=?
            WHERE trade_id=?
        """, (
            fill_price, ts_for_db(), decision.exit_reason,
            order_id or "", total_pnl, final_r,
            ts_for_db(), trade_id
        ))
        logger.info(f"[DB] Exit logged: {trade_id[:8]} "
                    f"P&L=${total_pnl:.2f} ({final_r:.2f}R) "
                    f"reason={decision.exit_reason}")

        self._trade_logger.update_daily_summary()
        self._risk_manager.remove_open_risk(record.get("risk_usd", 0))
        self._risk_manager.record_trade_result(total_pnl)

        # Build a minimal record for the exit email
        from database.trade_logger import TradeRecord as TR
        exit_record = TR()
        exit_record.trade_id      = trade_id
        exit_record.direction     = direction
        exit_record.entry_price   = entry
        exit_record.exit_price    = fill_price
        exit_record.exit_reason   = decision.exit_reason
        exit_record.exit_order_id = order_id or ""
        exit_record.pnl_usd       = total_pnl
        exit_record.pnl_r         = final_r
        exit_record.strategy      = record.get("strategy", "")
        exit_record.setup_grade   = record.get("setup_grade", "")
        exit_record.regime        = record.get("regime", "")
        exit_record.paper_trade   = record.get("paper_trade", 1)
        exit_record.partial_exit_price = record.get("partial_exit_price") or 0.0
        exit_record.partial_exit_size  = record.get("partial_exit_size") or 0.0
        exit_record.status        = "closed"

        self._alert_manager.send_exit_alert(exit_record)
        self._exit_engine.clear_partial_flag(trade_id)
        self._open_record = None

        emoji = "✅" if total_pnl > 0 else "❌"
        logger.info(
            f"{emoji} TRADE CLOSED: {direction.upper()} "
            f"exit={fill_price:.2f} pnl=${total_pnl:.2f} ({final_r:.2f}R) "
            f"reason={decision.exit_reason}"
        )

        # Diagnostic mode: halt after first trade closes
        try:
            from config import DIAGNOSTIC_MODE
            if DIAGNOSTIC_MODE:
                from strategy.diagnostic_strategy import DiagnosticStrategy
                DiagnosticStrategy.mark_closed()
                logger.info(
                    "🔬 DIAGNOSTIC COMPLETE\n"
                    f"  Entry:  ${record['entry_price']:.4f}\n"
                    f"  Exit:   ${fill_price:.4f}\n"
                    f"  P&L:    ${total_pnl:.4f}\n"
                    f"  R:      {final_r:.2f}R\n"
                    f"  Reason: {decision.exit_reason}\n"
                    "  ✅ Order lifecycle validated — bot halting new entries."
                )
        except ImportError:
            pass

        return False

    def _execute_partial_exit(self, record: dict,
                               current_price: float,
                               decision: ExitDecision):
        """Exit a portion of the position at a structure level."""
        trade_id  = record["trade_id"]
        direction = record["direction"]
        size_btc  = record["position_size"]
        entry     = record["entry_price"]

        partial_size = size_btc * decision.partial_pct

        fill_price, _ = self._order_router.close_partial(
            direction=direction,
            size_btc=partial_size,
            reason=f"partial_{decision.partial_reason}"
        )

        if fill_price is None:
            logger.error(f"Partial exit order failed for {trade_id[:8]}")
            return

        partial_pnl = unrealized_pnl(entry, fill_price, partial_size, direction)

        self._trade_logger.log_partial_exit(
            trade_id=trade_id,
            price=fill_price,
            size=partial_size,
            pnl=partial_pnl
        )

        logger.info(
            f"⚡ PARTIAL EXIT: {trade_id[:8]} "
            f"{decision.partial_pct:.0%} ({partial_size:.4f} BTC) "
            f"@ {fill_price:.2f} pnl=${partial_pnl:.2f} "
            f"reason={decision.partial_reason}"
        )

    def _update_stop(self, record: dict, new_stop: float,
                      current_price: float, current_r: float):
        """Log and apply a stop adjustment."""
        trade_id = record["trade_id"]
        old_stop = record["stop_price"]

        self._trade_logger.log_stop_update(
            trade_id=trade_id,
            old_stop=old_stop,
            new_stop=new_stop,
            reason="trail",
            current_price=current_price,
            current_r=current_r
        )

        logger.debug(
            f"Stop adjusted: {trade_id[:8]} "
            f"{old_stop:.2f} → {new_stop:.2f} "
            f"(price={current_price:.2f} R={current_r:.2f})"
        )

    def open_trade_pnl(self, current_price: float) -> float:
        """Current unrealized P&L of open trade."""
        r = self._open_record
        if r is None:
            return 0.0
        return unrealized_pnl(
            r["entry_price"], current_price,
            r["position_size"], r["direction"]
        )


# Singleton
_position_manager: Optional[PositionManager] = None


def get_position_manager(paper_trading: bool = PAPER_TRADING) -> PositionManager:
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager(paper_trading)
    return _position_manager
