# diag_strategy.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Diagnostic: run full strategy stack on first 2000 candles
# v1.1 — 2026-06-28 — Add: progress bar + verbose output to diag_strategy.log

"""
Runs the full strategy stack on the first 2000 post-warmup candles of 2025-Q1.
Prints a progress bar to terminal and writes verbose per-candle detail to log.

Usage:
    python diag_strategy.py
    cat diag_strategy.log    # full verbose output
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

import bt_config as cfg
from backtest.data_fetcher import DataFetcher
from backtest.replay import CandleBuffer, ReplayConfig
from crypto_trader.strategy_bundle import StrategyBundle

LOG_FILE   = Path("diag_strategy.log")
MAX_CANDLES = 2000

# ── Logging to file ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, mode="w")]
)
log = logging.getLogger("diag")

def pbar(i, total, extra=""):
    pct    = i / total * 100
    filled = int(pct / 5)
    bar    = "█" * filled + "░" * (20 - filled)
    sys.stdout.write(f"\r  [{bar}] {pct:5.1f}%  {i}/{total}  {extra}")
    sys.stdout.flush()

# ── Setup ─────────────────────────────────────────────────────────────────────
print(f"\n  Strategy Stack Diagnostic — backtester_engine_v1")
print(f"  Verbose log → {LOG_FILE.absolute()}\n")

log.info("=== Strategy Stack Diagnostic ===")
log.info(f"Started: {datetime.now()}")

fetcher = DataFetcher(cfg.CACHE_DIR)
df      = fetcher.load_quarter("2025-Q1")
config  = ReplayConfig()
bundle  = StrategyBundle()
buf     = CandleBuffer()

log.info(f"Loaded {len(df):,} candles from 2025-Q1")

# ── Counters ──────────────────────────────────────────────────────────────────
warmup_count    = 0
indicators_ok   = 0
indicators_fail = 0
regime_ok       = 0
regime_fail     = 0
signal_ok       = 0
signal_none     = 0
validation_ok   = 0
validation_fail = 0
post_warmup     = 0

regime_counts   = {}
rejection_reasons = {}

# ── Main loop ─────────────────────────────────────────────────────────────────
for i, (_, row) in enumerate(df.iterrows()):
    candle = row.to_dict()
    candle["vwap"] = candle["close"]
    buf.push(candle)

    if not buf.ready():
        warmup_count += 1
        continue

    post_warmup += 1
    if post_warmup > MAX_CANDLES:
        break

    pbar(post_warmup, MAX_CANDLES,
         f"ind={indicators_ok} reg={regime_ok} sig={signal_ok} val={validation_ok}")

    ts = candle["timestamp"]
    windows = buf.windows

    # ── Stage 1: indicators ───────────────────────────────────────────────────
    try:
        ind = bundle.compute_indicators(windows, candle)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.warning(f"[{ts}] compute_indicators EXCEPTION: {e}")
        indicators_fail += 1
        continue

    if not ind:
        indicators_fail += 1
        log.debug(f"[{ts}] compute_indicators returned empty — warmup or insufficient data")
        continue

    indicators_ok += 1
    vol = ind.get("vol_state")
    atr_val = getattr(vol, 'atr_current', None)
    atr_str = f"{atr_val:.2f}" if atr_val else "N/A"
    log.debug(f"[{ts}] Indicators OK | atr={atr_str} | vwap={ind.get('vwap',0):.2f}")

    # ── Stage 2: regime ───────────────────────────────────────────────────────
    try:
        regime, conviction = bundle.classify_regime(ind, config)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.warning(f"[{ts}] classify_regime EXCEPTION: {e}")
        regime_fail += 1
        continue

    if regime is None:
        regime_fail += 1
        log.debug(f"[{ts}] classify_regime returned None")
        continue

    regime_ok += 1
    regime_label = str(getattr(regime, 'label', getattr(regime, 'regime', str(regime))))
    regime_counts[regime_label] = regime_counts.get(regime_label, 0) + 1
    log.debug(f"[{ts}] Regime: {regime_label}  conviction={conviction:.2f}")

    # ── Stage 3: signal ───────────────────────────────────────────────────────
    try:
        signal = bundle.generate_signal(regime, conviction, ind, candle, config)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.warning(f"[{ts}] generate_signal EXCEPTION: {e}")
        signal_none += 1
        continue

    if signal is None:
        signal_none += 1
        log.debug(f"[{ts}] generate_signal returned None | regime={regime_label}")
        continue

    signal_ok += 1
    log.info(f"[{ts}] SIGNAL: {signal.get('direction')} | entry={signal.get('entry',0):.2f}"
             f" stop={signal.get('stop',0):.2f} target={signal.get('target',0):.2f}"
             f" grade={signal.get('grade')} strategy={signal.get('strategy')}")

    # ── Stage 4: validation ───────────────────────────────────────────────────
    try:
        valid = bundle.validate_entry(signal, ind, config)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.warning(f"[{ts}] validate_entry EXCEPTION: {e}")
        validation_fail += 1
        continue

    if valid:
        validation_ok += 1
        log.info(f"[{ts}] ✓ VALID ENTRY — {signal}")
    else:
        validation_fail += 1
        direction = signal.get("direction")
        vwap      = ind.get("vwap", 0)
        entry     = signal.get("entry", 0)
        grade     = signal.get("grade", "B")
        if grade == "C":
            reason = "grade_c_disabled"
        elif config.vwap_filter_active and vwap > 0:
            if direction == "short" and entry > vwap:
                reason = f"vwap_gate:short_above_vwap entry={entry:.0f} vwap={vwap:.0f}"
            elif direction == "long" and entry < vwap:
                reason = f"vwap_gate:long_below_vwap entry={entry:.0f} vwap={vwap:.0f}"
            else:
                reason = f"rrr_fail entry={entry:.0f} stop={signal.get('stop',0):.0f} target={signal.get('target',0):.0f}"
        else:
            reason = "validation_failed"
        rejection_reasons[reason.split(":")[0]] = rejection_reasons.get(reason.split(":")[0], 0) + 1
        log.info(f"[{ts}] ✗ REJECTED: {reason}")

sys.stdout.write("\n\n")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"  {'─'*50}")
print(f"  RESULTS (first {MAX_CANDLES} post-warmup candles)")
print(f"  {'─'*50}")
print(f"  Warmup candles skipped:  {warmup_count:,}")
print(f"  Post-warmup candles:     {post_warmup:,}")
print(f"  Indicators computed:     {indicators_ok:,}  (failed: {indicators_fail})")
print(f"  Regimes classified:      {regime_ok:,}  (failed: {regime_fail})")
print(f"  Signals generated:       {signal_ok:,}  (none: {signal_none})")
print(f"  Signals validated:       {validation_ok:,}  (rejected: {validation_fail})")

if regime_counts:
    print(f"\n  Regime distribution:")
    for r, c in sorted(regime_counts.items(), key=lambda x: -x[1]):
        print(f"    {r:<25} {c:>6,}")

if rejection_reasons:
    print(f"\n  Rejection reasons:")
    for r, c in sorted(rejection_reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:<25} {c:>6,}")

if indicators_ok == 0:
    print(f"\n  ⚠  STUCK AT: indicators — vol_engine not computing")
elif regime_ok == 0:
    print(f"\n  ⚠  STUCK AT: regime — classifier returning None")
elif signal_ok == 0:
    print(f"\n  ⚠  STUCK AT: signal — strategy not generating setups")
elif validation_ok == 0:
    print(f"\n  ⚠  STUCK AT: validation — all signals rejected")
else:
    print(f"\n  ✓  Pipeline working — {validation_ok} valid signals in {MAX_CANDLES} candles")

print(f"\n  Full detail: cat {LOG_FILE.absolute()}\n")
log.info("=== Diagnostic Complete ===")
