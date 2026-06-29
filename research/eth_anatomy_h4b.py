# research/eth_anatomy_h4b.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Anatomy analysis specifically on H4b filtered trades
#                      Uses same filters as H4b: hours, dump size, exhaustion candle
#                      Analyzes top 20 wins vs top 20 losses in detail

"""
Applies H4b filters to cached signals, simulates at 2.0R,
then performs deep anatomy on wins vs losses.

Answers:
- What did the 5 candles BEFORE the exhaustion candle look like?
- What was the volume signature on winning vs losing exhaustion candles?
- How far did price move before hitting stop vs target?
- What did the best and worst trades have in common?
- Which additional filter would cut the most losses?

Usage:
    python research/eth_anatomy_h4b.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

INPUT_FILE   = Path.home() / "ETHUSD_1m.csv"
SIGNALS_FILE = Path(__file__).parent / "signals_H4_2018.json"
OUTPUT_DIR   = Path(__file__).parent
OUTPUT_FILE  = OUTPUT_DIR / "eth_h4b_anatomy.txt"
OUTPUT_WINS  = OUTPUT_DIR / "eth_h4b_wins.csv"
OUTPUT_LOSS  = OUTPUT_DIR / "eth_h4b_losses.csv"

COLS = ["timestamp","open","high","low","close","volume","trades"]

# H4b parameters (must match h4b tester)
GOOD_HOURS       = list(range(0, 7)) + [10, 11, 12]
MIN_DUMP_PCT     = 4.0
MAX_EXHAUST_BARS = 35
MIN_LOWER_WICK   = 0.25
MIN_CLOSE_POS    = 0.40
ATR_STOP_MULT    = 1.5
FEE_RT           = 0.004
RRR              = 2.0
MAX_HOLD         = 480


def main():
    print(f"\n  ETH H4b Trade Anatomy")
    print(f"  {'─'*52}")
    t0 = time.time()

    print("  Loading signals and data...")
    with open(SIGNALS_FILE) as f:
        signals = json.load(f)

    df = pd.read_csv(INPUT_FILE, header=None, names=COLS,
                     dtype={"timestamp":"int64","open":"float64","high":"float64",
                            "low":"float64","close":"float64","volume":"float64",
                            "trades":"int32"})
    df = df[df["close"] >= 5.0].reset_index(drop=True)

    ts_arr    = df["timestamp"].values
    open_arr  = df["open"].values
    high_arr  = df["high"].values
    low_arr   = df["low"].values
    close_arr = df["close"].values
    vol_arr   = df["volume"].values
    ts_to_idx = {int(ts): i for i, ts in enumerate(ts_arr)}
    n_df = len(df)
    print(f"  {len(signals):,} signals  |  {n_df:,} candles")
    print()

    wins = []
    losses = []
    n = len(signals)

    for si, sig in enumerate(signals):
        if si % 1000 == 0:
            sys.stdout.write(f"\r  Processing: {si:,}/{n:,}  "
                             f"wins={len(wins):,}  losses={len(losses):,}")
            sys.stdout.flush()

        ts   = int(sig["timestamp"])
        dirn = sig["direction"]
        atr_v= float(sig["atr"])

        idx = ts_to_idx.get(ts)
        if idx is None or idx < 60 or idx + MAX_EXHAUST_BARS + MAX_HOLD >= n_df:
            continue

        dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour

        # Apply H4b filters
        if hour not in GOOD_HOURS:
            continue

        entry_price = float(close_arr[idx])

        if dirn == "long":
            pre_high = float(high_arr[max(0,idx-30):idx].max())
            dump_pct = (pre_high - entry_price) / pre_high * 100
            if dump_pct < MIN_DUMP_PCT:
                continue
        else:
            pre_low = float(low_arr[max(0,idx-30):idx].min())
            dump_pct = (entry_price - pre_low) / entry_price * 100
            if dump_pct < MIN_DUMP_PCT:
                continue

        # Find exhaustion candle
        exh_idx = None
        exh_o = exh_h = exh_l = exh_c = 0.0
        exh_vol = 0.0

        for j in range(idx, min(idx + MAX_EXHAUST_BARS, n_df - 2)):
            o = float(open_arr[j]); h = float(high_arr[j])
            l = float(low_arr[j]);  c = float(close_arr[j])
            v = float(vol_arr[j])
            tr = h - l if h != l else 0.0001
            lw = (min(o,c) - l) / tr
            uw = (h - max(o,c)) / tr
            cp = (c - l) / tr

            if dirn == "long" and lw >= MIN_LOWER_WICK and cp >= MIN_CLOSE_POS:
                exh_idx = j; exh_o=o; exh_h=h; exh_l=l; exh_c=c; exh_vol=v
                break
            elif dirn == "short" and uw >= MIN_LOWER_WICK and cp <= (1-MIN_CLOSE_POS):
                exh_idx = j; exh_o=o; exh_h=h; exh_l=l; exh_c=c; exh_vol=v
                break

        if exh_idx is None:
            continue

        e_entry = exh_c
        e_stop  = (exh_l - atr_v*ATR_STOP_MULT) if dirn=="long" else (exh_h + atr_v*ATR_STOP_MULT)
        e_risk  = abs(e_entry - e_stop)
        if e_risk <= 0:
            continue

        target = (e_entry + e_risk*RRR) if dirn=="long" else (e_entry - e_risk*RRR)

        # Simulate
        hit = "timeout"
        ep  = float(close_arr[min(exh_idx+MAX_HOLD, n_df-1)])
        for k in range(exh_idx+1, min(exh_idx+MAX_HOLD+1, n_df)):
            hk = float(high_arr[k]); lk = float(low_arr[k])
            if dirn == "long":
                if lk <= e_stop: hit="stop"; ep=e_stop; break
                if hk >= target: hit="target"; ep=target; break
            else:
                if hk >= e_stop: hit="stop"; ep=e_stop; break
                if lk <= target: hit="target"; ep=target; break

        gross = (ep-e_entry)/e_entry if dirn=="long" else (e_entry-ep)/e_entry
        net   = gross - FEE_RT
        win   = net > 0

        # Pre-exhaustion window features (5 candles before exhaustion)
        pre5_start = max(0, exh_idx-5)
        pre5_vols  = vol_arr[pre5_start:exh_idx]
        pre5_vol_mean = float(pre5_vols.mean()) if len(pre5_vols) > 0 else 1.0

        # 60-bar volume mean
        vol60_mean = float(vol_arr[max(0,exh_idx-60):exh_idx].mean())

        # Exhaustion candle features
        exh_range = exh_h - exh_l if exh_h != exh_l else 0.0001
        exh_body  = abs(exh_c - exh_o) / exh_range
        exh_lwick = (min(exh_o,exh_c) - exh_l) / exh_range
        exh_uwick = (exh_h - max(exh_o,exh_c)) / exh_range
        exh_close_pos = (exh_c - exh_l) / exh_range
        exh_vol_ratio = exh_vol / vol60_mean if vol60_mean > 0 else 1.0
        exh_range_pct = exh_range / exh_l if exh_l > 0 else 0

        # Next candle
        if exh_idx + 1 < n_df:
            next_o = float(open_arr[exh_idx+1])
            next_c = float(close_arr[exh_idx+1])
            next_v = float(vol_arr[exh_idx+1])
            next_bullish = next_c > next_o
            next_vol_ratio = next_v / exh_vol if exh_vol > 0 else 1.0
            next_gap = (next_o - exh_c) / exh_c * 100  # gap from exhaustion close
        else:
            next_bullish = False; next_vol_ratio = 1.0; next_gap = 0.0

        # Post-entry MAE/MFE
        post_h = float(high_arr[exh_idx+1:min(exh_idx+481,n_df)].max()) if exh_idx+1 < n_df else e_entry
        post_l = float(low_arr[exh_idx+1:min(exh_idx+481,n_df)].min()) if exh_idx+1 < n_df else e_entry

        if dirn == "long":
            mae = (e_entry - post_l) / e_entry * 100
            mfe = (post_h - e_entry) / e_entry * 100
        else:
            mae = (post_h - e_entry) / e_entry * 100
            mfe = (e_entry - post_l) / e_entry * 100

        # Bars from signal to exhaustion
        bars_to_exh = exh_idx - idx

        record = {
            "timestamp":       ts,
            "datetime":        dt.strftime("%Y-%m-%d %H:%M"),
            "year":            dt.year,
            "hour_utc":        hour,
            "direction":       dirn,
            "entry_price":     e_entry,
            "exit_price":      ep,
            "stop_price":      e_stop,
            "target_price":    target,
            "exit_reason":     hit,
            "net_pnl_pct":     net * 100,
            "win":             win,
            "dump_pct":        dump_pct,
            "bars_to_exh":     bars_to_exh,
            # Exhaustion candle
            "exh_body_pct":    exh_body,
            "exh_lower_wick":  exh_lwick,
            "exh_upper_wick":  exh_uwick,
            "exh_close_pos":   exh_close_pos,
            "exh_vol_ratio":   exh_vol_ratio,
            "exh_range_pct":   exh_range_pct,
            # Next candle confirmation
            "next_bullish":    next_bullish,
            "next_vol_ratio":  next_vol_ratio,
            "next_gap_pct":    next_gap,
            # MAE/MFE
            "mae_pct":         mae,
            "mfe_pct":         mfe,
            "mae_vs_stop":     mae / (e_risk/e_entry*100) if e_risk > 0 else 0,
        }

        if win:
            wins.append(record)
        else:
            losses.append(record)

    sys.stdout.write(f"\r  Processing: {n:,}/{n:,} complete\n")
    print(f"  Wins: {len(wins):,}  Losses: {len(losses):,}")
    print(f"  Total: {time.time()-t0:.1f}s")
    print()

    wins_df   = pd.DataFrame(wins)   if wins   else pd.DataFrame()
    losses_df = pd.DataFrame(losses) if losses else pd.DataFrame()

    if len(wins_df) == 0 or len(losses_df) == 0:
        print("  Not enough data"); sys.exit(0)

    wins_df.to_csv(OUTPUT_WINS, index=False)
    losses_df.to_csv(OUTPUT_LOSS, index=False)

    all_df = pd.concat([wins_df, losses_df])

    lines = []
    lines += ["="*70,
              "  ETH H4b Trade Anatomy — Wins vs Losses",
              f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
              f"  {len(wins_df):,} wins  |  {len(losses_df):,} losses  |  RRR={RRR}",
              "="*70]

    def sec(t): lines.extend(["","="*70,f"  {t}","="*70])
    def sub(t): lines.extend(["",f"  ── {t} ──"])
    def cmp(label, wv, lv, hib=True):
        edge = "WIN ▲" if (wv > lv) == hib else "LOSS ▲"
        lines.append(f"    {label:<40} wins={wv:>8.3f}  losses={lv:>8.3f}  {edge}")

    sec("1. DUMP CHARACTERISTICS")
    cmp("Dump size (%)", wins_df["dump_pct"].mean(), losses_df["dump_pct"].mean(), True)
    cmp("Bars to exhaustion", wins_df["bars_to_exh"].mean(), losses_df["bars_to_exh"].mean(), False)

    sec("2. EXHAUSTION CANDLE FEATURES")
    cmp("Body size (% of range)",    wins_df["exh_body_pct"].mean(),   losses_df["exh_body_pct"].mean(),  False)
    cmp("Lower wick (% of range)",   wins_df["exh_lower_wick"].mean(), losses_df["exh_lower_wick"].mean(), True)
    cmp("Upper wick (% of range)",   wins_df["exh_upper_wick"].mean(), losses_df["exh_upper_wick"].mean(), True)
    cmp("Close position (0=low 1=high)", wins_df["exh_close_pos"].mean(), losses_df["exh_close_pos"].mean(), True)
    cmp("Volume ratio vs 60-bar avg",wins_df["exh_vol_ratio"].mean(),  losses_df["exh_vol_ratio"].mean(),  True)
    cmp("Candle range (% of price)", wins_df["exh_range_pct"].mean(),  losses_df["exh_range_pct"].mean(),  True)

    sec("3. NEXT CANDLE CONFIRMATION")
    w_next = wins_df["next_bullish"].mean()*100
    l_next = losses_df["next_bullish"].mean()*100
    lines.append(f"    {'Next candle bullish':<40} wins={w_next:>7.1f}%  losses={l_next:>7.1f}%")
    cmp("Next candle vol vs exhaustion", wins_df["next_vol_ratio"].mean(), losses_df["next_vol_ratio"].mean(), True)
    cmp("Next candle gap from close (%)", wins_df["next_gap_pct"].mean(), losses_df["next_gap_pct"].mean(), True)

    sec("4. POST-ENTRY BEHAVIOR (MAE/MFE)")
    cmp("Max adverse excursion (%)",  wins_df["mae_pct"].mean(),      losses_df["mae_pct"].mean(),      False)
    cmp("MAE as multiple of stop",    wins_df["mae_vs_stop"].mean(),   losses_df["mae_vs_stop"].mean(),   False)
    cmp("Max favorable excursion (%)",wins_df["mfe_pct"].mean(),      losses_df["mfe_pct"].mean(),      True)
    lines += ["",
              "  Stop sizing insight:",
              f"    Avg MAE on WINS:   {wins_df['mae_pct'].mean():.2f}%  "
              f"(if stop < this → would have been stopped out incorrectly)",
              f"    Avg MAE on LOSSES: {losses_df['mae_pct'].mean():.2f}%  "
              f"(went further against us before failing)"]

    sec("5. FILTER CANDIDATES — WHAT SEPARATES WINS FROM LOSSES")

    # Next candle confirmation filter
    sub("Filter: Wait for next candle to confirm (bullish after long exhaustion)")
    confirmed = all_df[all_df["next_bullish"] == (all_df["direction"]=="long")]
    unconfirmed = all_df[all_df["next_bullish"] != (all_df["direction"]=="long")]
    if len(confirmed) > 10:
        lines.append(f"    Confirmed:    n={len(confirmed):,}  win={confirmed['win'].mean()*100:.1f}%")
        lines.append(f"    Unconfirmed:  n={len(unconfirmed):,}  win={unconfirmed['win'].mean()*100:.1f}%")

    # Volume spike filter
    sub("Filter: High volume on exhaustion candle (>1.5x avg)")
    hi_vol = all_df[all_df["exh_vol_ratio"] >= 1.5]
    lo_vol = all_df[all_df["exh_vol_ratio"] < 1.5]
    if len(hi_vol) > 10:
        lines.append(f"    High vol:  n={len(hi_vol):,}  win={hi_vol['win'].mean()*100:.1f}%")
        lines.append(f"    Low vol:   n={len(lo_vol):,}  win={lo_vol['win'].mean()*100:.1f}%")

    # Dump size filter
    sub("Filter: Larger dumps (>6%)")
    big_dump = all_df[all_df["dump_pct"] >= 6.0]
    sml_dump = all_df[all_df["dump_pct"] < 6.0]
    if len(big_dump) > 10:
        lines.append(f"    Big dump (>6%):  n={len(big_dump):,}  win={big_dump['win'].mean()*100:.1f}%")
        lines.append(f"    Small dump (<6%): n={len(sml_dump):,}  win={sml_dump['win'].mean()*100:.1f}%")

    # Fast exhaustion filter
    sub("Filter: Fast exhaustion (<=15 bars)")
    fast = all_df[all_df["bars_to_exh"] <= 15]
    slow = all_df[all_df["bars_to_exh"] > 15]
    if len(fast) > 10:
        lines.append(f"    Fast (<= 15 bars): n={len(fast):,}  win={fast['win'].mean()*100:.1f}%")
        lines.append(f"    Slow (>15 bars):   n={len(slow):,}  win={slow['win'].mean()*100:.1f}%")

    # Combined best filters
    sub("Combined filters (vol>1.5x AND dump>6% AND bars<=20)")
    combined = all_df[
        (all_df["exh_vol_ratio"] >= 1.5) &
        (all_df["dump_pct"]      >= 6.0) &
        (all_df["bars_to_exh"]   <= 20)
    ]
    if len(combined) > 5:
        lines.append(f"    Combined: n={len(combined):,}  win={combined['win'].mean()*100:.1f}%  "
                     f"(filtered {len(all_df)-len(combined):,} trades)")
        lines.append(f"    Avg P&L:  {combined['net_pnl_pct'].mean():.2f}%")

    sec("6. TOP 20 WINNING TRADES")
    lines.append(f"    {'Date':<18} {'Dir':<6} {'Entry':>8} {'Exit':>8} "
                 f"{'P&L%':>7} {'Dump%':>7} {'VolR':>6} {'LWick':>6} {'Bars':>5} {'Exit'}")
    lines.append(f"    {'─'*80}")
    for _, r in wins_df.nlargest(20, "net_pnl_pct").iterrows():
        lines.append(f"    {r['datetime']:<18} {r['direction']:<6} "
                     f"{r['entry_price']:>8.2f} {r['exit_price']:>8.2f} "
                     f"{r['net_pnl_pct']:>7.2f}% {r['dump_pct']:>6.2f}% "
                     f"{r['exh_vol_ratio']:>5.2f}x {r['exh_lower_wick']:>5.2f} "
                     f"{int(r['bars_to_exh']):>5} {r['exit_reason']}")

    sec("7. TOP 20 LOSING TRADES")
    lines.append(f"    {'Date':<18} {'Dir':<6} {'Entry':>8} {'Exit':>8} "
                 f"{'P&L%':>7} {'Dump%':>7} {'VolR':>6} {'LWick':>6} {'Bars':>5} {'Exit'}")
    lines.append(f"    {'─'*80}")
    for _, r in losses_df.nsmallest(20, "net_pnl_pct").iterrows():
        lines.append(f"    {r['datetime']:<18} {r['direction']:<6} "
                     f"{r['entry_price']:>8.2f} {r['exit_price']:>8.2f} "
                     f"{r['net_pnl_pct']:>7.2f}% {r['dump_pct']:>6.2f}% "
                     f"{r['exh_vol_ratio']:>5.2f}x {r['exh_lower_wick']:>5.2f} "
                     f"{int(r['bars_to_exh']):>5} {r['exit_reason']}")

    sec("8. EXIT REASON BREAKDOWN")
    for reason, grp in all_df.groupby("exit_reason"):
        wins_n = grp["win"].sum()
        lines.append(f"    {reason:<12}  n={len(grp):>4,}  "
                     f"win={wins_n/len(grp)*100:.0f}%  "
                     f"avg_pnl={grp['net_pnl_pct'].mean():.2f}%  "
                     f"avg_mae={grp['mae_pct'].mean():.2f}%")

    lines += ["","="*70,
              f"  Complete: {time.time()-t0:.1f}s  |  Saved: {OUTPUT_FILE}",
              "="*70]

    output = "\n".join(lines)
    print(output)
    OUTPUT_FILE.write_text(output)
    print(f"\n  Saved: {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()
