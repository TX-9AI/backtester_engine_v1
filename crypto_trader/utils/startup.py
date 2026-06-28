"""
utils/startup.py — Interactive session configuration prompt.
Runs once at bot launch to confirm mode.
v6.0: Auto-sized positions, no manual risk input, no circuit breaker.
"""

import sys
from config import LEVERAGE, PAPER_TRADING, SessionConfig
from utils.time_utils import fmt_et_full


BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║       crypto_trader v6.0  ·  Vertigo Capital                    ║
║       BTC/USD  ·  Kraken Margin  ·  Auto-Sized                  ║
╚══════════════════════════════════════════════════════════════════╝
"""


def _ask(prompt: str, default: str = "") -> str:
    val = input(f"  {prompt} [{default}]: ").strip()
    return val if val else default


def _ask_bool(prompt: str, default: bool = True) -> bool:
    default_str = "Y/n" if default else "y/N"
    val = input(f"  {prompt} [{default_str}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "1", "true")


def run_startup_prompt(cash_balance: float = 0.0) -> SessionConfig:
    """
    Display banner, confirm paper/live mode, return SessionConfig.
    Called once at bot start before the main loop begins.
    """
    print(BANNER)
    print(f"  Started at:  {fmt_et_full()}")
    print(f"  Instrument:  BTC/USD  |  Leverage: {LEVERAGE}x")
    print(f"  Cash bal:    ${cash_balance:,.2f}")
    print(f"  Margin:      ${cash_balance * LEVERAGE:,.2f}")
    print()
    print("─" * 66)
    print("  Position sizing: Grade A = 90% buying power | Grade B = 75%")
    print("─" * 66)

    paper = _ask_bool("Paper trading mode?", default=PAPER_TRADING)

    if not paper:
        print("\n  ⚠  ⚠  ⚠  LIVE TRADING MODE  ⚠  ⚠  ⚠")
        print("  Real money will be at risk. Orders will be sent to Kraken.\n")
        confirm = _ask("  Type CONFIRMED to proceed with live trading", "")
        if confirm != "CONFIRMED":
            print("  Defaulting to paper trading mode for safety.")
            paper = True

    notes = _ask("Session notes (optional)", "")

    print("\n" + "─" * 66)
    print("  SESSION SUMMARY")
    print("─" * 66)
    print(f"  Mode:            {'📄 PAPER TRADING' if paper else '🔴 LIVE TRADING'}")
    print(f"  Cash balance:    ${cash_balance:,.2f}")
    print(f"  Buying power:    ${cash_balance * LEVERAGE:,.2f}  ({LEVERAGE}x margin)")
    print(f"  Grade A:         90% of buying power (${cash_balance * LEVERAGE * 0.90:,.2f})")
    print(f"  Grade B:         75% of buying power (${cash_balance * LEVERAGE * 0.75:,.2f})")
    print(f"  Notes:           {notes or '—'}")
    print("─" * 66)

    go = _ask_bool("Confirm and start bot?", default=True)
    if not go:
        print("  Aborted. Exiting.")
        sys.exit(0)

    print("\n  ✅ Session confirmed. Bot starting...\n")

    return SessionConfig(
        paper_trading=paper,
        notes=notes,
        confirmed_at=fmt_et_full()
    )
