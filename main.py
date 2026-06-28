# main.py — btc_backtester
# v1.0 — 2026-06-28 — Entry point: interactive session setup, quarter selection, run dispatch
# v1.1 — 2026-06-28 — Auto-generate HTML report after each quarter run

"""
Run a backtest session:
    python main.py

Or non-interactively:
    python main.py --quarter 2025-Q3 --balance 5000
    python main.py --all-quarters --balance 10000
    python main.py --optimize --quarter 2025-Q3 --balance 1000

Session flow:
  1. Print available quarters (cached = ✓, not cached = ○)
  2. Prompt user for starting balance
  3. Prompt user for quarter selection (single, range, or all)
  4. For each selected quarter:
       a. Load/fetch OHLCV data
       b. Run replay engine
       c. Compute summary stats
       d. Log to SQLite
       e. Generate HTML report
  5. Print session summary table
"""

import argparse
import logging
import sys
from pathlib import Path

# ─── PATH SETUP ──────────────────────────────────────────────────────────────
# Ensure crypto_trader module can be imported from local copy
sys.path.insert(0, str(Path(__file__).parent))

import config as cfg
from backtest.data_fetcher import DataFetcher
from backtest.replay import ReplayConfig, ReplayEngine
from backtest.backtest_logger import BacktestLogger
from crypto_trader.strategy_bundle import StrategyBundle

logger = logging.getLogger(__name__)

# ─── LOGGING SETUP ────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libraries
    for name in ("ccxt", "urllib3", "requests"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ─── INTERACTIVE SESSION SETUP ────────────────────────────────────────────────

def prompt_balance(default: float = cfg.DEFAULT_STARTING_BALANCE) -> float:
    print(f"\n  Starting balance (default ${default:,.0f}): ", end="")
    raw = input().strip()
    if not raw:
        return default
    try:
        val = float(raw.replace(",", "").replace("$", ""))
        if val <= 0:
            raise ValueError
        return val
    except ValueError:
        print(f"  Invalid input. Using default ${default:,.0f}")
        return default


def prompt_quarter(available: list[str], fetcher: DataFetcher) -> list[str]:
    print("\n  Available quarters:")
    for i, label in enumerate(available, 1):
        mark = "✓" if fetcher.is_cached(label) else "○"
        print(f"    {i:3d}.  {mark}  {label}")

    print("\n  Enter quarter(s) to run:")
    print("    - Single: 2025-Q3")
    print("    - Range:  2024-Q1:2024-Q4")
    print("    - All:    all")
    print(f"\n  Selection: ", end="")
    raw = input().strip().lower()

    if raw == "all":
        return available

    if ":" in raw:
        parts = raw.split(":")
        try:
            start_i = available.index(parts[0].upper())
            end_i   = available.index(parts[1].upper())
            return available[start_i:end_i + 1]
        except ValueError:
            print(f"  Invalid range. Defaulting to last quarter.")
            return [available[-1]]

    label = raw.upper()
    if label in available:
        return [label]

    print(f"  '{raw}' not found. Defaulting to last quarter.")
    return [available[-1]]


def change_balance_prompt(current: float) -> float:
    print(f"\n  Change starting balance? Current: ${current:,.2f}")
    print(f"  New balance (or ENTER to keep): ", end="")
    raw = input().strip()
    if not raw:
        return current
    try:
        val = float(raw.replace(",", "").replace("$", ""))
        return val if val > 0 else current
    except ValueError:
        return current


# ─── SINGLE QUARTER RUN ───────────────────────────────────────────────────────

def run_quarter(
    label: str,
    balance: float,
    fetcher: DataFetcher,
    bt_logger: BacktestLogger,
    replay_config: ReplayConfig,
    bundle: StrategyBundle,
    notes: str = "",
) -> dict:
    """
    Full pipeline for a single quarter.
    Returns summary dict for session table display.
    """
    print(f"\n{'─'*60}")
    print(f"  Running: {label}  |  Balance: ${balance:,.2f}")
    print(f"{'─'*60}")

    # Load data
    df = fetcher.load_quarter(label)
    print(f"  Loaded {len(df):,} candles  "
          f"({df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()})")

    # Create DB run
    run_id = bt_logger.create_run(
        start_balance=balance,
        quarter=label,
        param_set=_config_to_dict(replay_config),
        notes=notes,
    )

    # Replay
    replay_config.starting_balance_usd = balance
    engine = ReplayEngine(replay_config, bundle)
    trades = engine.run(df)

    # Summary stats
    summary = BacktestLogger.compute_summary(trades, engine.equity_curve, balance)

    # Log to DB
    bt_logger.log_trades(run_id, trades)
    bt_logger.log_equity(run_id, engine.equity_curve, stride=cfg.EQUITY_LOG_STRIDE)
    bt_logger.finalize_run(run_id, summary)

    # Print quick results
    _print_quarter_summary(label, run_id, summary)

    # Auto-generate HTML report
    try:
        from reports.html_report import compute_report_stats, build_html
        from pathlib import Path
        trades_df = bt_logger.get_trades(run_id)
        equity_df = bt_logger.get_equity(run_id)
        run_meta  = bt_logger.get_run_summary(run_id)
        rpt_stats = compute_report_stats(trades_df, equity_df, run_meta)
        html_out  = build_html(run_meta, rpt_stats, trades_df)
        quarter_slug = label.replace("-", "_")
        rpt_path  = Path(cfg.REPORTS_DIR) / f"backtest_run{run_id}_{quarter_slug}.html"
        rpt_path.parent.mkdir(parents=True, exist_ok=True)
        rpt_path.write_text(html_out)
        print(f"\n  📄 Report: {rpt_path}")
    except Exception as e:
        print(f"\n  ⚠  Report generation failed: {e}")

    return {
        "quarter":       label,
        "run_id":        run_id,
        "trades":        summary["total_trades"],
        "win_rate":      f"{summary['win_rate']:.1%}",
        "avg_r":         f"{summary['avg_r']:.2f}",
        "profit_factor": f"{summary['profit_factor']:.2f}",
        "max_dd":        f"{summary['max_drawdown']:.1%}",
        "net_pnl":       f"${summary['net_pnl']:,.2f}",
        "end_balance":   f"${summary['end_balance']:,.2f}",
    }


def _print_quarter_summary(label: str, run_id: int, summary: dict) -> None:
    print(f"\n  ── Results: {label} (run #{run_id}) ──")
    print(f"  Trades:        {summary['total_trades']}")
    print(f"  Win rate:      {summary['win_rate']:.1%}")
    print(f"  Avg R:         {summary['avg_r']:.2f}")
    print(f"  Profit factor: {summary['profit_factor']:.2f}")
    print(f"  Max drawdown:  {summary['max_drawdown']:.1%}")
    print(f"  Net P&L:       ${summary['net_pnl']:,.2f}")
    print(f"  End balance:   ${summary['end_balance']:,.2f}")


def _config_to_dict(rc: ReplayConfig) -> dict:
    import dataclasses
    return dataclasses.asdict(rc)


# ─── SESSION SUMMARY TABLE ────────────────────────────────────────────────────

def print_session_table(results: list[dict]) -> None:
    if not results:
        return
    print(f"\n{'═'*80}")
    print(f"  SESSION SUMMARY")
    print(f"{'═'*80}")
    headers = ["Quarter", "Run", "Trades", "Win%", "AvgR", "PF", "MaxDD", "Net P&L", "Balance"]
    row_fmt = "  {:<10} {:>5} {:>7} {:>7} {:>6} {:>6} {:>8} {:>12} {:>12}"
    print(row_fmt.format(*headers))
    print(f"  {'─'*76}")
    for r in results:
        print(row_fmt.format(
            r["quarter"], r["run_id"], r["trades"],
            r["win_rate"], r["avg_r"], r["profit_factor"],
            r["max_dd"], r["net_pnl"], r["end_balance"],
        ))
    print(f"{'═'*80}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="BTC Backtester — Vertigo Capital")
    p.add_argument("--quarter",      type=str,   help="Single quarter, e.g. 2025-Q3")
    p.add_argument("--balance",      type=float, help="Starting balance USD")
    p.add_argument("--all-quarters", action="store_true")
    p.add_argument("--optimize",     action="store_true", help="Run parameter optimizer")
    p.add_argument("--verbose",      action="store_true")
    return p.parse_args()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    setup_logging(args.verbose)

    print("\n" + "═"*60)
    print("  BTC BACKTESTER — Vertigo Capital")
    print("═"*60)

    fetcher   = DataFetcher(cfg.CACHE_DIR)
    bt_logger = BacktestLogger(cfg.RESULTS_DB)
    bundle    = StrategyBundle()          # Wraps the crypto_trader strategy stack

    available = fetcher.list_available_quarters()

    # ── Balance ─────────────────────────────────────────────────────────────
    if args.balance:
        balance = args.balance
        print(f"\n  Starting balance: ${balance:,.2f}")
    else:
        balance = prompt_balance()

    # ── Quarter selection ────────────────────────────────────────────────────
    if args.all_quarters:
        quarters = available
    elif args.quarter:
        q = args.quarter.upper()
        quarters = [q] if q in available else [available[-1]]
    else:
        quarters = prompt_quarter(available, fetcher)

    print(f"\n  Quarters to run: {', '.join(quarters)}")

    # ── Confirm ─────────────────────────────────────────────────────────────
    print(f"\n  Balance: ${balance:,.2f}  |  {len(quarters)} quarter(s)")
    print(f"  Change balance? (ENTER to continue, or type new amount): ", end="")
    raw = input().strip()
    if raw:
        try:
            new_bal = float(raw.replace(",", "").replace("$", ""))
            if new_bal > 0:
                balance = new_bal
        except ValueError:
            pass

    # ── Build replay config ──────────────────────────────────────────────────
    replay_config = ReplayConfig(
        starting_balance_usd     = balance,
        leverage                 = cfg.DEFAULT_LEVERAGE,
        paper_slippage_pct       = 0.001,
        grade_a_notional_pct     = cfg.DEFAULT_GRADE_A_NOTIONAL_PCT,
        grade_b_notional_pct     = cfg.DEFAULT_GRADE_B_NOTIONAL_PCT,
        min_rrr                  = cfg.DEFAULT_MIN_RRR,
        min_fee_adjusted_r       = cfg.DEFAULT_MIN_FEE_ADJUSTED_R,
        atr_stop_multiplier      = cfg.DEFAULT_ATR_STOP_MULTIPLIER,
        adx_trend_threshold      = cfg.DEFAULT_ADX_TREND_THRESHOLD,
        adx_range_threshold      = cfg.DEFAULT_ADX_RANGE_THRESHOLD,
        bb_width_compression_pct = cfg.DEFAULT_BB_WIDTH_COMPRESSION,
        trail_activation_r       = cfg.DEFAULT_TRAIL_ACTIVATION_R,
        partial_exit_pct         = cfg.DEFAULT_PARTIAL_EXIT_PCT,
        partial_minimum_r        = cfg.DEFAULT_PARTIAL_MINIMUM_R,
        stagnant_trade_minutes   = cfg.DEFAULT_STAGNANT_TRADE_MIN,
        entry_cooldown_minutes   = cfg.DEFAULT_ENTRY_COOLDOWN_MIN,
        vwap_filter_active       = cfg.DEFAULT_VWAP_FILTER_ACTIVE,
        sweep_rejection_candles  = cfg.DEFAULT_SWEEP_REJECTION_CANDLES,
        min_daily_range_pct      = cfg.DEFAULT_MIN_DAILY_RANGE_PCT,
    )

    # ── Run quarters ─────────────────────────────────────────────────────────
    all_results = []
    for label in quarters:
        result = run_quarter(
            label        = label,
            balance      = balance,
            fetcher      = fetcher,
            bt_logger    = bt_logger,
            replay_config= replay_config,
            bundle       = bundle,
        )
        all_results.append(result)

        # Offer balance change between quarters
        if len(quarters) > 1 and label != quarters[-1]:
            print(f"\n  Next quarter: {quarters[quarters.index(label)+1]}")
            balance = change_balance_prompt(balance)

    # ── Session summary ───────────────────────────────────────────────────────
    print_session_table(all_results)

    print("  Results saved to:", cfg.RESULTS_DB)
    print("  Run reports/html_report.py to generate HTML output.\n")


if __name__ == "__main__":
    main()
