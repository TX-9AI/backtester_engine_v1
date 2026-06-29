# backtest/backtest_logger.py — backtester_engine_v1
# v1.0 — 2026-06-28 — SQLite trade logger for backtest results
# v1.1 — 2026-06-29 — Fix: convert Timestamp to str before SQLite insert (TypeError fix)

"""
Persists completed backtest trades to SQLite for querying, reporting, and
comparison across parameter sets and quarters.

Schema:
  - runs      : one row per backtest run (config snapshot, summary stats)
  - trades    : one row per completed trade (links to run_id)
  - equity    : one row per candle equity snapshot (optional, large)

Usage:
    from backtest.backtest_logger import BacktestLogger
    logger = BacktestLogger("data/backtest_results.db")
    run_id = logger.create_run(config, quarter_label, param_set)
    logger.log_trades(run_id, trades)
    logger.log_equity(run_id, equity_curve)
    logger.finalize_run(run_id, summary_stats)
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/backtest_results.db")


class BacktestLogger:

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at      TEXT NOT NULL,
                    quarter         TEXT,
                    start_balance   REAL,
                    end_balance     REAL,
                    total_trades    INTEGER,
                    win_rate        REAL,
                    avg_r           REAL,
                    profit_factor   REAL,
                    max_drawdown    REAL,
                    sharpe          REAL,
                    net_pnl         REAL,
                    param_set       TEXT,   -- JSON blob of parameter overrides
                    notes           TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          INTEGER NOT NULL,
                    entry_time      TEXT,
                    exit_time       TEXT,
                    direction       TEXT,
                    entry_price     REAL,
                    exit_price      REAL,
                    stop_price      REAL,
                    target_price    REAL,
                    contracts       REAL,
                    notional_usd    REAL,
                    gross_pnl       REAL,
                    fees            REAL,
                    net_pnl         REAL,
                    r_achieved      REAL,
                    grade           TEXT,
                    strategy        TEXT,
                    regime          TEXT,
                    exit_reason     TEXT,
                    partial_taken   INTEGER,
                    trail_active    INTEGER,
                    cash_after      REAL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS equity (
                    equity_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          INTEGER NOT NULL,
                    timestamp       TEXT,
                    cash            REAL,
                    unrealized      REAL,
                    equity          REAL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_trades_run    ON trades(run_id);
                CREATE INDEX IF NOT EXISTS idx_equity_run    ON equity(run_id);
                CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);
                CREATE INDEX IF NOT EXISTS idx_trades_grade  ON trades(grade);
            """)
        logger.debug(f"[BacktestLogger] DB initialized: {self.db_path}")

    # ── Run management ────────────────────────────────────────────────────────

    def create_run(
        self,
        start_balance: float,
        quarter: Optional[str] = None,
        param_set: Optional[dict] = None,
        notes: str = "",
    ) -> int:
        """Create a new run record and return run_id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO runs (created_at, quarter, start_balance, param_set, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (now, quarter, start_balance,
                 json.dumps(param_set or {}), notes)
            )
            run_id = cur.lastrowid
        logger.info(f"[BacktestLogger] Created run {run_id} | quarter={quarter}")
        return run_id

    def finalize_run(self, run_id: int, summary: dict) -> None:
        """Update run record with final statistics after replay completes."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE runs SET
                    end_balance   = ?,
                    total_trades  = ?,
                    win_rate      = ?,
                    avg_r         = ?,
                    profit_factor = ?,
                    max_drawdown  = ?,
                    sharpe        = ?,
                    net_pnl       = ?
                   WHERE run_id = ?""",
                (
                    summary.get("end_balance"),
                    summary.get("total_trades"),
                    summary.get("win_rate"),
                    summary.get("avg_r"),
                    summary.get("profit_factor"),
                    summary.get("max_drawdown"),
                    summary.get("sharpe"),
                    summary.get("net_pnl"),
                    run_id,
                )
            )
        logger.info(f"[BacktestLogger] Finalized run {run_id}: "
                    f"trades={summary.get('total_trades')} "
                    f"net=${summary.get('net_pnl', 0):.2f} "
                    f"max_dd={summary.get('max_drawdown', 0):.1%}")

    # ── Trade logging ─────────────────────────────────────────────────────────

    def log_trades(self, run_id: int, trades: list[dict]) -> None:
        """Bulk insert all trades from a completed run."""
        if not trades:
            logger.warning(f"[BacktestLogger] No trades to log for run {run_id}")
            return

        rows = [
            (
                run_id,
                str(t.get("entry_time", "")),
                str(t.get("exit_time", "")) if t.get("exit_time") else None,
                t.get("direction"),
                t.get("entry_price"),
                t.get("exit_price"),
                t.get("stop_price"),
                t.get("target_price"),
                t.get("contracts"),
                t.get("notional_usd"),
                t.get("gross_pnl"),
                t.get("fees"),
                t.get("net_pnl"),
                t.get("r_achieved"),
                t.get("grade"),
                t.get("strategy"),
                t.get("regime"),
                t.get("exit_reason"),
                1 if t.get("partial_taken") else 0,
                1 if t.get("trail_active") else 0,
                t.get("cash_after"),
            )
            for t in trades
        ]

        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO trades (
                    run_id, entry_time, exit_time, direction,
                    entry_price, exit_price, stop_price, target_price,
                    contracts, notional_usd, gross_pnl, fees, net_pnl,
                    r_achieved, grade, strategy, regime, exit_reason,
                    partial_taken, trail_active, cash_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        logger.info(f"[BacktestLogger] Logged {len(trades)} trades for run {run_id}")

    # ── Equity logging ────────────────────────────────────────────────────────

    def log_equity(self, run_id: int, equity_curve: list[dict], stride: int = 5) -> None:
        """
        Insert equity curve snapshots. stride=5 keeps every 5th candle
        to reduce DB size on large runs (1m data = ~130k rows/quarter).
        """
        if not equity_curve:
            return

        sampled = equity_curve[::stride]
        rows = [
            (run_id, str(e["timestamp"]), e["cash"], e["unrealized"], e["equity"])
            for e in sampled
        ]
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO equity (run_id, timestamp, cash, unrealized, equity) VALUES (?,?,?,?,?)",
                rows,
            )
        logger.debug(f"[BacktestLogger] Logged {len(rows)} equity points for run {run_id}")

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get_trades(self, run_id: int) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query(
                "SELECT * FROM trades WHERE run_id = ? ORDER BY entry_time",
                conn, params=(run_id,)
            )

    def get_equity(self, run_id: int) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query(
                "SELECT * FROM equity WHERE run_id = ? ORDER BY timestamp",
                conn, params=(run_id,)
            )

    def get_runs(self) -> pd.DataFrame:
        with self._conn() as conn:
            return pd.read_sql_query(
                "SELECT * FROM runs ORDER BY created_at DESC",
                conn
            )

    def get_run_summary(self, run_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def compare_runs(self, run_ids: list[int]) -> pd.DataFrame:
        """Return a side-by-side comparison DataFrame of multiple runs."""
        placeholders = ",".join("?" * len(run_ids))
        with self._conn() as conn:
            return pd.read_sql_query(
                f"SELECT * FROM runs WHERE run_id IN ({placeholders})",
                conn, params=run_ids
            )

    # ── Statistics calculator ─────────────────────────────────────────────────

    @staticmethod
    def compute_summary(
        trades: list[dict],
        equity_curve: list[dict],
        start_balance: float,
    ) -> dict:
        """
        Compute standard backtest statistics from completed trades.
        Called by main.py before finalize_run().
        """
        if not trades:
            return {
                "end_balance": start_balance, "total_trades": 0,
                "win_rate": 0, "avg_r": 0, "profit_factor": 0,
                "max_drawdown": 0, "sharpe": 0, "net_pnl": 0,
            }

        df = pd.DataFrame(trades)
        wins  = df[df["net_pnl"] > 0]
        losses = df[df["net_pnl"] <= 0]

        win_rate      = len(wins) / len(df) if len(df) > 0 else 0
        avg_r         = df["r_achieved"].mean()
        gross_profit  = wins["net_pnl"].sum()
        gross_loss    = abs(losses["net_pnl"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        net_pnl       = df["net_pnl"].sum()
        end_balance   = start_balance + net_pnl

        # Max drawdown from equity curve
        max_drawdown = 0.0
        if equity_curve:
            eq = pd.DataFrame(equity_curve)["equity"]
            rolling_max = eq.cummax()
            drawdowns   = (eq - rolling_max) / rolling_max
            max_drawdown = drawdowns.min()  # negative number

        # Sharpe (annualized, assuming 1m candle returns)
        sharpe = 0.0
        if equity_curve and len(equity_curve) > 1:
            eq = pd.DataFrame(equity_curve)["equity"]
            returns = eq.pct_change().dropna()
            if returns.std() > 0:
                # 1m candles: 525,600 per year
                sharpe = (returns.mean() / returns.std()) * (525600 ** 0.5)

        return {
            "end_balance":   round(end_balance, 2),
            "total_trades":  len(df),
            "win_rate":      round(win_rate, 4),
            "avg_r":         round(avg_r, 3),
            "profit_factor": round(profit_factor, 3),
            "max_drawdown":  round(max_drawdown, 4),
            "sharpe":        round(sharpe, 3),
            "net_pnl":       round(net_pnl, 2),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
