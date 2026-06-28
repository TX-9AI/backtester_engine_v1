"""
execution/order_router.py — Kraken order routing: place, cancel, fetch status.
Handles paper mode transparently. All live orders go through here.
"""

import logging
import time
from typing import Optional, Tuple
import uuid

from data.market_data import get_exchange
from config import TRADING_SYMBOL, KRAKEN_SYMBOL, LEVERAGE, PAPER_TRADING

logger = logging.getLogger(__name__)


class OrderRouter:
    """
    Routes orders to Kraken (or simulates in paper mode).
    Handles retries, cancellations, and fill confirmations.
    """

    def __init__(self, paper_trading: bool = PAPER_TRADING):
        self.paper_trading = paper_trading

    def close_position(self,
                       direction:    str,
                       size_btc:     float,
                       reason:       str = "manual") -> Tuple[Optional[float], Optional[str]]:
        """
        Close an open position at market price.

        Args:
            direction:  Original trade direction ("long" or "short")
            size_btc:   Position size to close in BTC
            reason:     Logging label

        Returns:
            (fill_price, order_id) or (None, None) on failure
        """
        close_side = "sell" if direction == "long" else "buy"

        if self.paper_trading:
            from data.market_data import get_current_price
            price = get_current_price() or 0.0
            order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
            logger.info(
                f"[PAPER] Close {direction} @ {price:.2f} "
                f"reason={reason} id={order_id}"
            )
            return price, order_id

        try:
            import hashlib, hmac, base64, time, urllib.parse, requests, os

            try:
                from config import KRAKEN_API_KEY as api_key, KRAKEN_API_SECRET as api_secret
            except ImportError:
                api_key    = os.environ.get("KRAKEN_API_KEY", "")
                api_secret = os.environ.get("KRAKEN_API_SECRET", "")

            if not api_key or not api_secret:
                logger.error("Kraken API credentials not found")
                return None, None

            # Strip slash for Kraken REST API pair format
            pair  = KRAKEN_SYMBOL.replace("/", "")
            url   = "https://api.kraken.com/0/private/AddOrder"
            nonce = str(int(time.time() * 1000))

            data = {
                "nonce":     nonce,
                "ordertype": "market",
                "type":      close_side,
                "volume":    f"{size_btc:.8f}",
                "pair":      pair,
                "leverage":  str(LEVERAGE),
                "oflags":    "fciq",
            }

            post_data = urllib.parse.urlencode(data)
            encoded   = (nonce + post_data).encode()
            message   = "/0/private/AddOrder".encode() + hashlib.sha256(encoded).digest()
            signature = base64.b64encode(
                hmac.new(base64.b64decode(api_secret), message, hashlib.sha512).digest()
            ).decode()

            headers = {"API-Key": api_key, "API-Sign": signature}
            response = requests.post(url, data=data, headers=headers, timeout=10)
            result   = response.json()

            if result.get("error"):
                logger.error(f"Close order failed ({reason}): kraken {result['error']}")
                return None, None

            res      = result.get("result", {})
            order_id = res.get("txid", ["unknown"])[0] if res.get("txid") else "unknown"
            from data.market_data import get_current_price
            fill_price = get_current_price() or 0.0
            logger.info(
                f"[LIVE] Position closed: {direction} {size_btc:.8f} "
                f"@ {fill_price:.2f} reason={reason} id={order_id} pair={pair}"
            )
            return float(fill_price), str(order_id)

        except Exception as e:
            logger.error(f"Close order failed ({reason}): {e}")
            return None, None

    def close_partial(self,
                      direction: str,
                      size_btc:  float,
                      reason:    str = "partial") -> Tuple[Optional[float], Optional[str]]:
        """Close a fraction of the position."""
        return self.close_position(direction, size_btc, reason=reason)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending limit order."""
        if self.paper_trading:
            logger.debug(f"[PAPER] Cancel order {order_id}")
            return True
        try:
            exchange = get_exchange()
            exchange.cancel_order(order_id, TRADING_SYMBOL)
            logger.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    def get_order_status(self, order_id: str) -> Optional[str]:
        """
        Fetch current order status from Kraken.
        Returns 'open', 'closed', 'canceled', or None on error.
        """
        if self.paper_trading:
            return "closed"  # Paper orders always fill
        try:
            exchange = get_exchange()
            order  = exchange.fetch_order(order_id, TRADING_SYMBOL)
            return order.get("status")
        except Exception as e:
            logger.warning(f"Could not fetch order status {order_id}: {e}")
            return None

    def get_open_positions(self) -> list:
        """
        Fetch current open positions from Kraken margin.
        Returns list of position dicts.
        """
        if self.paper_trading:
            return []   # Position manager tracks paper positions
        try:
            exchange   = get_exchange()
            # Kraken margin positions via private endpoint
            response   = exchange.fetch_positions([TRADING_SYMBOL])
            return response or []
        except Exception as e:
            logger.warning(f"Could not fetch open positions: {e}")
            return []


# Singleton
_router: Optional[OrderRouter] = None


def get_order_router(paper_trading: bool = PAPER_TRADING) -> OrderRouter:
    global _router
    if _router is None:
        _router = OrderRouter(paper_trading)
    return _router
