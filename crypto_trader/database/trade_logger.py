"""
database/trade_logger.py — Logs every trade event to SQLite.
Updated: symbol derived from config.TRADING_SYMBOL (instrument-agnostic).
"""

import uuid
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import date

from database.db_manager import get_db
from utils.time_utils import ts_for_db, now_et, fmt_et

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    trade_id:           str   = field(default_factory=lambda: str(uuid.uuid4()))
    symbol:             str   = ""          # Set from config.TRADING_SYMBOL
    direction:          str   = ""
    status:             str   = "open"
    regime:             str   = ""
    regime_conviction:  float = 0.0
    strategy:           str   = ""
    setup_grade:        str   = ""
    setup_score:        float = 0.0
    entry_price:        float = 0.0
    entry_time:         str   = ""
    entry_order_id:     str   = ""
    position_size:      float = 0.0
    notional_usd:       float = 0.0
    risk_usd:           float = 0.0
    stop_price:         float = 0.0
    initial_stop:       float = 0.0
    target_1:           float = 0.0
    target_2:           float = 0.0
    atr_at_entry:       float = 0.0
    exit_price:         float = 0.0
    exit_time:          str   = ""
    exit_reason:        str   = ""
    exit_order_id:      str   = ""
    pnl_usd:            float = 0.0
    pnl_r:              float = 0.0
    commission_usd:     float = 0.0
    fee_adjusted_pnl_usd: float = 0.0   # pnl_usd - commission_usd
    partial_exit_price: float = 0.0
    partial_exit_time:  str   = ""
    partial_exit_size:  float = 0.0
    partial_pnl_usd:    float = 0.0
    vix_at_entry:       float = 0.0
    dxy_at_entry:       float = 0.0
    btc_personality:    str   = ""
    session_name:       str   = ""
    paper_trade:        int   = 1
    notes:              str   = ""
    created_at:         str   = field(default_factory=ts_for_db)
    updated_at:         str   = field(default_factory=ts_for_db)


class TradeLogger:

    def __init__(self):
        self.db = get_db()

    def log_entry(self, record: TradeRecord):
        # Set symbol from config if not already set
        if not record.symbol:
            try:
                from config import TRADING_SYMBOL
                record.symbol = TRADING_SYMBOL
            except Exception:
                record.symbol = "UNKNOWN"

        record.entry_time = ts_for_db()
        record.created_at = ts_for_db()
        record.updated_at = ts_for_db()

        self.db.execute("""
            INSERT INTO trades (
                trade_id, symbol, direction, status,
                regime, regime_conviction, strategy, setup_grade, setup_score,
                entry_price, entry_time, entry_order_id,
                position_size, notional_usd, risk_usd,
                stop_price, initial_stop, target_1, target_2, atr_at_entry,
                vix_at_entry, dxy_at_entry, btc_personality, session_name,
                paper_trade, notes, created_at, updated_at
            ) VALUES (
                ?,?,?,?,  ?,?,?,?,?,  ?,?,?,  ?,?,?,  ?,?,?,?,?,  ?,?,?,?,  ?,?,?,?
            )
        """, (
            record.trade_id, record.symbol, record.direction, record.status,
            record.regime, record.regime_conviction, record.strategy,
            record.setup_grade, record.setup_score,
            record.entry_price, record.entry_time, record.entry_order_id,
            record.position_size, record.notional_usd, record.risk_usd,
            record.stop_price, record.initial_stop,
            record.target_1, record.target_2, record.atr_at_entry,
            record.vix_at_entry, record.dxy_at_entry,
            record.btc_personality, record.session_name,
            record.paper_trade, record.notes,
            record.created_at, record.updated_at
        ))
        logger.info(f"[DB] Entry logged: {record.trade_id[:8]} "
                    f"{record.direction.upper()} @ {record.entry_price:.2f}")

    def log_stop_update(self, trade_id, old_stop, new_stop, reason, current_price, current_r):
        self.db.execute("""
            INSERT INTO stop_adjustments
                (trade_id, old_stop, new_stop, reason, current_price, current_r, adjusted_at)
            VALUES (?,?,?,?,?,?,?)
        """, (trade_id, old_stop, new_stop, reason, current_price, current_r, ts_for_db()))
        self.db.execute(
            "UPDATE trades SET stop_price=?, updated_at=? WHERE trade_id=?",
            (new_stop, ts_for_db(), trade_id)
        )

    def log_partial_exit(self, trade_id, price, size, pnl):
        self.db.execute("""
            UPDATE trades SET
                partial_exit_price=?, partial_exit_time=?,
                partial_exit_size=?, partial_pnl_usd=?, updated_at=?
            WHERE trade_id=?
        """, (price, ts_for_db(), size, pnl, ts_for_db(), trade_id))
        logger.info(f"[DB] Partial exit: {size:.4f} @ {price:.2f} P&L=${pnl:.2f}")

    def log_exit(self, record: TradeRecord):
        self.db.execute("""
            UPDATE trades SET
                status=?, exit_price=?, exit_time=?, exit_reason=?,
                exit_order_id=?, pnl_usd=?, pnl_r=?, commission_usd=?, updated_at=?
            WHERE trade_id=?
        """, (
            "closed", record.exit_price, ts_for_db(), record.exit_reason,
            record.exit_order_id, record.pnl_usd, record.pnl_r,
            record.commission_usd, ts_for_db(), record.trade_id
        ))

    def log_regime(self, regime, conviction, macro_context, btc_personality,
                   adx, atr_norm, bb_width_pct, session, trigger):
        self.db.execute("""
            INSERT INTO regime_log
                (regime, conviction, macro_context, btc_personality,
                 adx, atr_normalized, bb_width_pct, session_name, trigger, classified_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (regime, conviction, macro_context, btc_personality,
              adx, atr_norm, bb_width_pct, session, trigger, ts_for_db()))

    def update_daily_summary(self):
        today = now_et().date().isoformat()
        start = f"{today}T00:00:00"
        end   = f"{today}T23:59:59"
        rows = self.db.fetchall("""
            SELECT pnl_usd, regime FROM trades
            WHERE entry_time >= ? AND entry_time <= ? AND status='closed'
        """, (start, end))
        if not rows:
            return
        pnls    = [r["pnl_usd"] for r in rows]
        wins    = [p for p in pnls if p > 0]
        losses  = [p for p in pnls if p <= 0]
        regimes = list({r["regime"] for r in rows if r["regime"]})
        gross   = sum(pnls)
        running = peak = max_dd = 0.0
        for p in pnls:
            running += p
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
        self.db.execute("""
            INSERT INTO daily_summary
                (date, trades_taken, trades_won, trades_lost,
                 gross_pnl_usd, net_pnl_usd, max_drawdown_usd,
                 largest_win, largest_loss, regimes_seen, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                trades_taken=excluded.trades_taken,
                trades_won=excluded.trades_won,
                trades_lost=excluded.trades_lost,
                gross_pnl_usd=excluded.gross_pnl_usd,
                net_pnl_usd=excluded.net_pnl_usd,
                max_drawdown_usd=excluded.max_drawdown_usd,
                largest_win=excluded.largest_win,
                largest_loss=excluded.largest_loss,
                regimes_seen=excluded.regimes_seen,
                updated_at=excluded.updated_at
        """, (
            today, len(pnls), len(wins), len(losses),
            gross, gross, max_dd,
            max(wins) if wins else 0,
            min(losses) if losses else 0,
            ",".join(regimes), ts_for_db(), ts_for_db()
        ))

    def log_circuit_breaker(self, reason, daily_loss, threshold):
        self.db.execute("""
            INSERT INTO circuit_breaker_log (reason, daily_loss, threshold, triggered_at)
            VALUES (?,?,?,?)
        """, (reason, daily_loss, threshold, ts_for_db()))
        self.db.execute("""
            UPDATE daily_summary SET circuit_breaker_hit=1, updated_at=?
            WHERE date=?
        """, (ts_for_db(), now_et().date().isoformat()))

    def get_open_trade(self):
        row = self.db.fetchone(
            "SELECT * FROM trades WHERE status='open' ORDER BY created_at DESC LIMIT 1"
        )
        return dict(row) if row else None

    def get_todays_pnl(self):
        today = now_et().date().isoformat()
        row = self.db.fetchone("""
            SELECT COALESCE(SUM(pnl_usd),0) as total FROM trades
            WHERE status='closed' AND entry_time LIKE ?
        """, (f"{today}%",))
        return float(row["total"]) if row else 0.0

    def get_consecutive_losses(self):
        rows = self.db.fetchall("""
            SELECT pnl_usd FROM trades
            WHERE status='closed' ORDER BY exit_time DESC LIMIT 10
        """)
        count = 0
        for row in rows:
            if row["pnl_usd"] < 0:
                count += 1
            else:
                break
        return count

    def log_alert(self, trade_id, alert_type, subject, body_hash, success=True):
        self.db.execute("""
            INSERT INTO alert_log (trade_id, alert_type, subject, body_hash, sent_at, success)
            VALUES (?,?,?,?,?,?)
        """, (trade_id, alert_type, subject, body_hash, ts_for_db(), 1 if success else 0))


_logger_instance = None


def get_trade_logger() -> TradeLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = TradeLogger()
    return _logger_instance
