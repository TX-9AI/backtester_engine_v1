"""
notifications/telegram_sender.py — Telegram alerts for crypto_trader.
v1.0 — 2026-06-27 — initial release, replaces sms_sender.py (Twilio)

Credentials from systemd environment — never hardcoded.
"""

import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _get_env(key: str) -> str:
    import os, subprocess, re
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "show", "cryptobot", "--property=Environment"],
            capture_output=True, text=True
        )
        m = re.search(rf'{key}=([^ ]+)', result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return os.environ.get(key, "")


class TelegramSender:
    def __init__(self):
        self._token   = _get_env("TELEGRAM_TOKEN")
        self._chat_id = _get_env("TELEGRAM_CHAT_ID")
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            logger.info("Telegram alerts disabled — token or chat ID not configured")
        else:
            logger.info(f"Telegram alerts enabled — chat_id={self._chat_id}")

    def send(self, message: str) -> bool:
        if not self._enabled:
            logger.debug(f"Telegram (disabled): {message}")
            return False
        try:
            url  = TELEGRAM_API.format(token=self._token)
            resp = requests.post(url, json={
                "chat_id":    self._chat_id,
                "text":       message[:4096],
                "parse_mode": "HTML",
            }, timeout=10)
            if resp.status_code == 200:
                return True
            logger.error(f"Telegram send failed: {resp.status_code} {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False
