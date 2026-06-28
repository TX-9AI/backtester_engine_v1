"""
notifications/alert_manager.py — Trade alert dispatcher for crypto_trader.
v1.0 — original release (Twilio SMS via sms_sender.py)
v2.0 — 2026-06-27 — replaced SmsSender with TelegramSender, no Twilio

Sends Telegram messages for critical events only:
  - Bot startup
  - Trade entry
  - Trade exit
  - Circuit breaker fired
  - Bot restarted with open live position
  - Orphan position detected

Regime changes, consecutive losses, and PnL updates are suppressed
to avoid noise during active trading sessions.
"""

import logging
from database.trade_logger import TradeRecord
from notifications.telegram_sender import TelegramSender

logger = logging.getLogger(__name__)

_sent_ids: set = set()


class AlertManager:
    def __init__(self):
        self._tg = TelegramSender()

    def _send(self, message: str, dedup_key: str = None):
        if dedup_key:
            if dedup_key in _sent_ids:
                return
            _sent_ids.add(dedup_key)
        self._tg.send(message)

    def _send_deduped(self, trade_id: str, alert_type: str,
                      subject: str, body: str):
        key = f"{trade_id}:{alert_type}"
        self._send(f"<b>{subject}</b>\n{body}", dedup_key=key)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def send_entry_alert(self, record: TradeRecord):
        mode  = "📄 PAPER" if record.paper_trade else "🔴 LIVE"
        emoji = "📈" if record.direction == "long" else "📉"
        msg = (
            f"{emoji} <b>TRADE ENTERED [{mode}]</b>\n"
            f"{'─' * 28}\n"
            f"Instrument: {record.symbol}\n"
            f"Direction:  {record.direction.upper()}\n"
            f"Entry:      ${record.entry_price:,.4f}\n"
            f"Stop:       ${record.stop_price:,.4f}\n"
            f"Target:     ${record.target_1:,.4f}\n"
            f"Risk:       ${record.risk_usd:.2f}\n"
            f"Grade:      {record.setup_grade}\n"
            f"Strategy:   {record.strategy}\n"
            f"Regime:     {record.regime}"
        )
        self._send(msg, dedup_key=f"{record.trade_id}:entry")

    # ── Exit ──────────────────────────────────────────────────────────────────

    def send_exit_alert(self, record: TradeRecord):
        pnl   = record.pnl_usd or 0
        r     = record.pnl_r   or 0
        emoji = "✅" if pnl >= 0 else "❌"
        mode  = "📄 PAPER" if record.paper_trade else "🔴 LIVE"
        sign  = "+" if pnl >= 0 else ""
        msg = (
            f"{emoji} <b>TRADE CLOSED [{mode}]</b>\n"
            f"{'─' * 28}\n"
            f"Direction:  {record.direction.upper()}\n"
            f"Entry:      ${record.entry_price:,.4f}\n"
            f"Exit:       ${record.exit_price:,.4f}\n"
            f"P&L:        {sign}${abs(pnl):,.2f}  ({r:+.2f}R)\n"
            f"Reason:     {record.exit_reason}\n"
            f"Strategy:   {record.strategy}\n"
            f"Grade:      {record.setup_grade}"
        )
        self._send(msg, dedup_key=f"{record.trade_id}:exit")

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def send_circuit_breaker_alert(self, reason: str,
                                   daily_loss: float, threshold: float):
        msg = (
            f"🚨 <b>CIRCUIT BREAKER FIRED</b>\n"
            f"{'─' * 28}\n"
            f"Reason:     {reason}\n"
            f"Daily loss: ${daily_loss:,.2f}\n"
            f"Threshold:  ${threshold:,.2f}\n\n"
            f"Bot has stopped trading. Restart manually to resume."
        )
        self._send(msg, dedup_key=f"circuit_breaker:{reason}")

    # ── Restart with open position ────────────────────────────────────────────

    def send_recovery_alert(self, trade_id: str, direction: str,
                             entry: float, stop: float,
                             size: float, risk: float):
        msg = (
            f"⚠️ <b>BOT RESTARTED — OPEN POSITION RECOVERED</b>\n"
            f"{'─' * 28}\n"
            f"ID:         {trade_id[:8]}\n"
            f"Direction:  {direction.upper()}\n"
            f"Entry:      ${entry:,.4f}\n"
            f"Stop:       ${stop:,.4f}\n"
            f"Size:       {size:.4f}\n"
            f"Risk:       ${risk:.2f}\n\n"
            f"Bot is resuming management of this position."
        )
        self._send(msg, dedup_key=f"{trade_id}:recovery")

    # ── Orphan position ───────────────────────────────────────────────────────

    def send_orphan_alert(self, size: float, side: str):
        msg = (
            f"🚨 <b>ORPHAN POSITION DETECTED</b>\n"
            f"{'─' * 28}\n"
            f"Size: {size:.4f}  Side: {side}\n\n"
            f"No matching DB record found.\n"
            f"Bot will NOT manage this position.\n"
            f"ACTION: Close manually on Kraken, then restart bot."
        )
        self._send(msg, dedup_key="orphan_position")

    # ── Startup ───────────────────────────────────────────────────────────────

    def send_startup_alert(self, paper_trading: bool, balance: float, **kwargs):
        mode = "📄 PAPER" if paper_trading else "🔴 LIVE"
        self._tg.send(
            f"🚀 <b>crypto_trader v6.0 started</b>\n"
            f"Mode: {mode}\n"
            f"Cash: ${balance:,.2f} | Margin: ${balance*10:,.2f}\n"
            f"Instrument: BTC/USD | Kraken margin"
        )

    # ── Suppressed ────────────────────────────────────────────────────────────

    def send_regime_alert(self, *args, **kwargs):
        pass

    def send_consecutive_loss_alert(self, *args, **kwargs):
        pass

    def send_pnl_update(self, *args, **kwargs):
        pass


_alert_manager = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager
