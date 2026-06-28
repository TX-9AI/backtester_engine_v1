"""
execution/entry_engine.py — Handles trade entry order placement.
Supports limit orders (preferred) with market fallback.
Full paper trading simulation built in.
"""

import logging
import uuid
from typing import Optional

from strategy.base_strategy import TradeSignal
from risk.setup_scorer import SetupScore
from database.trade_logger import TradeRecord, get_trade_logger
from data.market_data import get_exchange
from data.macro_data import get_macro_manager
from config import (
    TRADING_SYMBOL, KRAKEN_SYMBOL, INSTRUMENT, ORDER_TYPE_DEFAULT, LIMIT_ORDER_SLIPPAGE_PCT,
    LEVERAGE, PAPER_TRADING, PAPER_FILL_SLIPPAGE_PCT
)
from utils.time_utils import ts_for_db, fmt_et_short, current_session

logger = logging.getLogger(__name__)


class EntryEngine:
    """
    Executes trade entries on Kraken (or simulates them in paper mode).
    Creates and persists the TradeRecord to the database.
    """

    def __init__(self, paper_trading: bool = PAPER_TRADING):
        self.paper_trading = paper_trading
        self._trade_logger = get_trade_logger()

    def enter(self,
              signal:       TradeSignal,
              score:        SetupScore,
              size_btc:     float,
              risk_usd:     float,
              notional_usd: float,
              estimated_fees: float = 0.0) -> Optional[TradeRecord]:
        """
        Place the entry order and return a populated TradeRecord.

        Args:
            signal:       Validated trade signal
            score:        Setup grade and score
            size_btc:     Position size in BTC
            risk_usd:     Dollar amount at risk
            notional_usd: Notional value of position

        Returns:
            TradeRecord if entry succeeded, None if order failed
        """
        mode = "PAPER" if self.paper_trading else "LIVE"
        logger.info(
            f"[{mode}] Entering {signal.direction.upper()} "
            f"{size_btc:.4f} {INSTRUMENT} @ {signal.entry_price:.2f} "
            f"stop={signal.stop_price:.2f} "
            f"grade={score.grade} risk=${risk_usd:.2f}"
        )

        # Execute the order
        if self.paper_trading:
            fill_price, order_id = self._paper_fill(signal)
        else:
            fill_price, order_id = self._live_order(signal, size_btc)

        if fill_price is None:
            logger.error("Entry order failed — no fill price")
            return None

        # Build trade record
        macro   = get_macro_manager().get()
        session_name, _ = current_session()

        record = TradeRecord(
            trade_id=str(uuid.uuid4()),
            symbol=TRADING_SYMBOL,
            direction=signal.direction,
            status="open",
            regime=signal.regime,
            regime_conviction=0.0,   # Set by caller
            strategy=signal.strategy_name,
            setup_grade=score.grade,
            setup_score=score.score,
            entry_price=fill_price,
            entry_order_id=order_id,
            position_size=size_btc,
            notional_usd=notional_usd,
            risk_usd=risk_usd,
            stop_price=signal.stop_price,
            initial_stop=signal.stop_price,
            target_1=signal.target_1,
            target_2=signal.target_2 or 0.0,
            atr_at_entry=signal.atr,
            vix_at_entry=macro.vix or 0.0,
            dxy_at_entry=macro.dxy or 0.0,
            session_name=session_name,
            paper_trade=1 if self.paper_trading else 0,
            commission_usd=estimated_fees,
            notes=signal.notes,
        )

        # Persist to database
        self._trade_logger.log_entry(record)

        logger.info(
            f"✅ Entry confirmed [{mode}]: "
            f"{signal.direction.upper()} {size_btc:.4f} BTC "
            f"@ {fill_price:.2f} | ID={record.trade_id[:8]}"
        )
        return record

    def _paper_fill(self, signal: TradeSignal) -> tuple:
        """
        Simulate order fill with configurable slippage.
        Paper mode fills at current price ± slippage.
        """
        slippage = signal.entry_price * PAPER_FILL_SLIPPAGE_PCT
        if signal.direction == "long":
            fill_price = signal.entry_price + slippage   # Buy slightly above
        else:
            fill_price = signal.entry_price - slippage   # Sell slightly below

        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        logger.debug(f"Paper fill: {fill_price:.2f} (slippage={slippage:.2f})")
        return fill_price, order_id

    def _live_order(self, signal: TradeSignal, size_btc: float) -> tuple:
        """
        Place a market order on Kraken using the REST API directly.
        Bypasses ccxt market registry so :BTNL pairs always work.
        """
        import hashlib, hmac, base64, time, urllib.parse, requests, os

        try:
            from config import KRAKEN_API_KEY as api_key, KRAKEN_API_SECRET as api_secret
        except ImportError:
            api_key    = os.environ.get("KRAKEN_API_KEY", "")
            api_secret = os.environ.get("KRAKEN_API_SECRET", "")

        if not api_key or not api_secret:
            logger.error("Kraken API credentials not found in config or environment")
            return None, None

        try:
            side   = "buy" if signal.direction == "long" else "sell"
            url    = "https://api.kraken.com/0/private/AddOrder"
            nonce  = str(int(time.time() * 1000))

            data = {
                "nonce":     nonce,
                "ordertype": "market",
                "type":      side,
                "volume":    f"{size_btc:.8f}",
                "pair":      KRAKEN_SYMBOL.replace("/", "").replace(":", ":"),
                "leverage":  str(LEVERAGE),
                "oflags":    "fciq",
            }

            # Sign the request
            post_data  = urllib.parse.urlencode(data)
            encoded    = (nonce + post_data).encode()
            message    = "/0/private/AddOrder".encode() + hashlib.sha256(encoded).digest()
            signature  = base64.b64encode(
                hmac.new(base64.b64decode(api_secret), message, hashlib.sha512).digest()
            ).decode()

            headers = {
                "API-Key":  api_key,
                "API-Sign": signature,
            }

            response = requests.post(url, data=data, headers=headers, timeout=10)
            result   = response.json()

            if result.get("error"):
                logger.error(f"Live order failed: kraken {result['error']}")
                return None, None

            res       = result.get("result", {})
            order_id  = res.get("txid", ["unknown"])[0] if res.get("txid") else "unknown"
            # Market orders fill immediately — use signal price as fill estimate
            fill_price = signal.entry_price
            logger.info(f"Live order placed: {order_id} pair={data['pair']} side={side} vol={size_btc:.8f}")
            return float(fill_price), str(order_id)

        except Exception as e:
            logger.error(f"Live order failed: {e}")
            return None, None


# Singleton
_entry_engine: Optional[EntryEngine] = None


def get_entry_engine(paper_trading: bool = PAPER_TRADING) -> EntryEngine:
    global _entry_engine
    if _entry_engine is None:
        _entry_engine = EntryEngine(paper_trading)
    return _entry_engine
