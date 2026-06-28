"""
database/db_manager.py — SQLite connection manager and schema.
Creates all tables on first run.
"""

import sqlite3
import logging
import os
from config import DB_PATH

logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT UNIQUE NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    status              TEXT NOT NULL,
    regime              TEXT,
    regime_conviction   REAL,
    strategy            TEXT,
    setup_grade         TEXT,
    setup_score         REAL,
    entry_price         REAL,
    entry_time          TEXT,
    entry_order_id      TEXT,
    position_size       REAL,
    notional_usd        REAL,
    risk_usd            REAL,
    stop_price          REAL,
    initial_stop        REAL,
    target_1            REAL,
    target_2            REAL,
    atr_at_entry        REAL,
    exit_price          REAL,
    exit_time           TEXT,
    exit_reason         TEXT,
    exit_order_id       TEXT,
    pnl_usd             REAL,
    pnl_r               REAL,
    commission_usd      REAL DEFAULT 0,
    partial_exit_price  REAL,
    partial_exit_time   TEXT,
    partial_exit_size   REAL,
    partial_pnl_usd     REAL,
    vix_at_entry        REAL,
    dxy_at_entry        REAL,
    btc_personality     TEXT,
    session_name        TEXT,
    paper_trade         INTEGER DEFAULT 1,
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stop_adjustments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT NOT NULL,
    old_stop        REAL,
    new_stop        REAL,
    reason          TEXT,
    current_price   REAL,
    current_r       REAL,
    adjusted_at     TEXT NOT NULL,
    FOREIGN KEY(trade_id) REFERENCES trades(trade_id)
);

CREATE TABLE IF NOT EXISTS regime_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    regime              TEXT NOT NULL,
    conviction          REAL,
    macro_context       TEXT,
    btc_personality     TEXT,
    adx                 REAL,
    atr_normalized      REAL,
    bb_width_pct        REAL,
    session_name        TEXT,
    trigger             TEXT,
    classified_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_summary (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT UNIQUE NOT NULL,
    trades_taken        INTEGER DEFAULT 0,
    trades_won          INTEGER DEFAULT 0,
    trades_lost         INTEGER DEFAULT 0,
    gross_pnl_usd       REAL DEFAULT 0,
    commission_usd      REAL DEFAULT 0,
    net_pnl_usd         REAL DEFAULT 0,
    max_drawdown_usd    REAL DEFAULT 0,
    largest_win         REAL DEFAULT 0,
    largest_loss        REAL DEFAULT 0,
    regimes_seen        TEXT,
    circuit_breaker_hit INTEGER DEFAULT 0,
    paper_mode          INTEGER DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    TEXT,
    alert_type  TEXT NOT NULL,
    subject     TEXT,
    body_hash   TEXT,
    sent_at     TEXT NOT NULL,
    success     INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS circuit_breaker_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reason      TEXT NOT NULL,
    daily_loss  REAL,
    threshold   REAL,
    triggered_at TEXT NOT NULL,
    reset_at    TEXT
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_trades_status      ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time  ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_trade_id    ON trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_regime_classified  ON regime_log(classified_at);
CREATE INDEX IF NOT EXISTS idx_daily_date         ON daily_summary(date);
"""


class DBManager:

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_dir()
        self._conn = None

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    def connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            logger.info(f"Database connected: {self.db_path}")
        return self._conn

    def initialize_schema(self):
        conn = self.connect()
        conn.executescript(SCHEMA_SQL)
        conn.executescript(INDEX_SQL)
        conn.commit()
        logger.info("Database schema initialized.")

    def execute(self, sql, params=()):
        conn = self.connect()
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor

    def executemany(self, sql, params_list):
        conn = self.connect()
        cursor = conn.executemany(sql, params_list)
        conn.commit()
        return cursor

    def fetchone(self, sql, params=()):
        conn = self.connect()
        return conn.execute(sql, params).fetchone()

    def fetchall(self, sql, params=()):
        conn = self.connect()
        return conn.execute(sql, params).fetchall()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()


_db = None


def get_db() -> DBManager:
    global _db
    if _db is None:
        _db = DBManager()
        _db.initialize_schema()
    return _db
