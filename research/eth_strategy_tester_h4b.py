# research/eth_strategy_tester_h4b.py — backtester_engine_v1
# v1.0 — 2026-06-29 — H4b: Refined Extreme Capitulation strategy
#                      Filters derived from anatomy analysis:
#                      1. Asia/overnight hours only (00-06 UTC, 10-12 UTC)
#                      2. Minimum dump size 4%+
#                      3. Wait for exhaustion candle (max 35 bars)
#                      4. Enter on exhaustion candle close, not at detection
#                      5. Exhaustion candle must have lower wick > 25% of range
#                      6. Exhaustion candle must close in upper 40% of range

"""
H4b — Refined Extreme Capitulation

Key differences from H4:
- Entry timing: wait for exhaustion candle instead of entering immediately
- Session filter: Asia + London open only (worst hours eliminated)
- Minimum dump threshold raised from 5% to 4%+ WITH 35-bar exhaustion cap
- Candle quality filter on exhaustion candle

Uses cached H4 signals as the universe, applies additional filters.
Completes in ~60 seconds.
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
OUTPUT_FILE  = OUTPUT_DIR / "eth_h4b_results.txt"

COLS = ["timestamp","open","high","low","close","volume","trades"]

# ── H4b Parameters (derived from anatomy analysis) ────────────────────────────
GOOD_HOURS      = list(range(0, 7)) + [10, 11, 12]  # Asia + London open
MIN_DUMP_PCT    = 4.0       # Minimum move size (anatomy: winners avg 3.88%)
MAX_EXHAUST_BARS= 35        # Max bars to wait for exhaustion (winners: 31 bars)
MIN_LOWER_WICK  = 0.25      # Exhaustion candle lower wick > 25% of range
MIN_CLOSE_POS   = 0.40      # Exhaustion candle must close in upper 40%
ATR_STOP_MULT   = 1.5
FEE_RT          = 0.004     # 0.4% round trip maker
POSITION_PCT    = 0.20
STARTING_BAL    = 10000.0
TRAIN_END_YEAR  = 2021
VALIDATE_START  = 2022
RRR_TARGETS     = [1.5, 2.0, 3.0]


def section(lines, title):
    lines += ["", "="*70, f"  {title}", "="*70]

def sub(lines, title):
    lines += ["", f"  ── {title} ──"]

def fmt(r, lines):
    if r.get("n", 0) == 0:
        lines.append("    No trades"); return
    lines.append(f"    Trades:        {r['n']:,}")
    lines.append(f"    Win rate:      {r['win_rate']:.1f}%")
    lines.append(f"    Avg R:         {r['avg_r']:+.2f}")
    lines.append(f"    Profit factor: {r['profit_factor']:.2f}")
    lines.append(f"    Max drawdown:  {r['max_dd']:.1f}%")
    lines.append(f"    Total return:  {r['total_return']:+.1f}%")
    lines.append(f"    Final balance: ${r['final_balance']:,.0f}")
    lines.append(f"    Sharpe:        {r['sharpe']:.2f}")
    ex = r.get("exits", {})
    if ex:
        lines.append(f"    Exits:         " + "  ".join(f"{k}={v}" for k,v in ex.items()))

def perf(trades, bal=STARTING_BAL, pos=POSITION_PCT):
    if trades is None or len(trades) == 0:
        return {"n": 0}
    n    = len(trades)
    wins = trades["win"].sum()
    wr   = wins / n
    ar   = trades["r_achieved"].mean()
    gw   = trades[trades["net_pnl"]>0]["net_pnl"].sum()
    gl   = abs(trades[trades["net_pnl"]<=0]["net_pnl"].sum())
    pf   = gw/gl if gl>0 else float("inf")
    balance = bal; peak = bal; max_dd = 0.0
    for _, row in trades.iterrows():
        balance += balance * pos * row["net_pnl"]
        peak     = max(peak, balance)
        max_dd   = max(max_dd, (peak-balance)/peak)
    ret = (balance - bal) / bal * 100
    ps  = trades["net_pnl"] * pos
    sharpe = ps.mean()/ps.std()*np.sqrt(252) if ps.std()>0 else 0
    return {"n": n, "win_rate": wr*100, "avg_r": ar,
            "profit_factor": pf, "max_dd": max_dd*100,
            "total_return": ret, "final_balance": balance, "sharpe": sharpe,
            "exits": trades["exit_reason"].value_counts().to_dict()}


def main():
    print(f"\n  ETH H4b Strategy Tester — Refined Capitulation")
    print(f"  {'─'*52}")
    print(f"  Filters from anatomy analysis:")
    print(f"    Hours:        {GOOD_HOURS}")
    print(f"    Min dump:     {MIN_DUMP_PCT}%")
    print(f"    Max exhaust:  {MAX_EXHAUST_BARS} bars")
    print(f"    Lower wick:   >{MIN_LOWER_WICK:.0%} of range")
    print(f"    Close pos:    >{MIN_CLOSE_POS:.0%} of range")
    print()

    t0 = time.time()

    # Load signals and raw data
    print("  Loading signals...")
    with open(SIGNALS_FILE) as f:
        signals = json.load(f)
    print(f"  {len(signals):,} base H4 signals")

    print("  Loading 1m data...")
    df = pd.read_csv(INPUT_FILE, header=None, names=COLS,
                     dtype={"timestamp":"int64","open":"float64","high":"float64",
                            "low":"float64","close":"float64","volume":"float64",
                            "trades":"int32"})
    df = df[df["close"] >= 5.0].reset_index(drop=True)
    ts_arr   = df["timestamp"].values
    open_arr = df["open"].values
    high_arr = df["high"].values
    low_arr  = df["low"].values
    close_arr= df["close"].values
    vol_arr  = df["volume"].values
    ts_to_idx = {int(ts): i for i, ts in enumerate(ts_arr)}
    n_df = len(df)
    print(f"  {n_df:,} candles indexed")
    print()

    # ── Apply H4b filters and simulate ───────────────────────────────────────
    print("  Applying filters and simulating trades...")
    trades = []
    filtered_hour    = 0
    filtered_dump    = 0
    filtered_no_exh  = 0
    filtered_candle  = 0
    accepted         = 0

    for si, sig in enumerate(signals):
        if si % 1000 == 0:
            sys.stdout.write(f"\r  Processing: {si:,}/{len(signals):,} "
                             f"accepted={accepted:,}  filtered={si-accepted:,}")
            sys.stdout.flush()

        ts   = int(sig["timestamp"])
        dirn = sig["direction"]
        atr_v= float(sig["atr"])

        idx = ts_to_idx.get(ts)
        if idx is None or idx < 60 or idx + MAX_EXHAUST_BARS + 120 >= n_df:
            continue

        # ── Filter 1: Session hour ────────────────────────────────────────────
        dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour
        if hour not in GOOD_HOURS:
            filtered_hour += 1
            continue

        # ── Filter 2: Minimum dump size ───────────────────────────────────────
        entry_price = float(close_arr[idx])
        pre_window  = 30
        pre_start   = max(0, idx - pre_window)
        if dirn == "long":
            pre_high  = float(high_arr[pre_start:idx].max())
            dump_pct  = (pre_high - entry_price) / pre_high * 100
        else:
            pre_low   = float(low_arr[pre_start:idx].min())
            dump_pct  = (entry_price - pre_low) / pre_low * 100

        if dump_pct < MIN_DUMP_PCT:
            filtered_dump += 1
            continue

        # ── Filter 3: Wait for exhaustion candle ──────────────────────────────
        # Scan forward up to MAX_EXHAUST_BARS for exhaustion pattern
        exhaustion_idx  = None
        exhaustion_entry= None
        exhaustion_stop = None

        vol_mean_60 = float(vol_arr[max(0,idx-60):idx].mean())

        for j in range(idx, min(idx + MAX_EXHAUST_BARS, n_df - 2)):
            o = float(open_arr[j])
            h = float(high_arr[j])
            l = float(low_arr[j])
            c = float(close_arr[j])
            total_range = h - l if h != l else 0.0001

            lower_wick  = (min(o, c) - l) / total_range
            upper_wick  = (h - max(o, c)) / total_range
            close_pos   = (c - l) / total_range

            if dirn == "long":
                # Looking for bullish exhaustion candle
                # Long lower wick = buyers stepping in
                if lower_wick >= MIN_LOWER_WICK and close_pos >= MIN_CLOSE_POS:
                    exhaustion_idx   = j
                    exhaustion_entry = c  # enter on close of exhaustion candle
                    exhaustion_stop  = l - atr_v * ATR_STOP_MULT
                    break
            else:
                # Looking for bearish exhaustion candle
                # Long upper wick = sellers stepping in
                if upper_wick >= MIN_LOWER_WICK and close_pos <= (1 - MIN_CLOSE_POS):
                    exhaustion_idx   = j
                    exhaustion_entry = c
                    exhaustion_stop  = h + atr_v * ATR_STOP_MULT
                    break

        if exhaustion_idx is None:
            filtered_no_exh += 1
            continue

        accepted += 1

        # ── Simulate trade from exhaustion entry ──────────────────────────────
        e_entry = exhaustion_entry
        e_stop  = exhaustion_stop
        e_risk  = abs(e_entry - e_stop)

        if e_risk <= 0:
            continue

        year = dt.year
        sim_start = exhaustion_idx + 1

        for rrr in RRR_TARGETS:
            target = (e_entry + e_risk * rrr) if dirn == "long" else (e_entry - e_risk * rrr)
            hit_reason = "timeout"
            exit_price = float(close_arr[min(sim_start + 480, n_df-1)])

            for k in range(sim_start, min(sim_start + 481, n_df)):
                h = float(high_arr[k])
                l = float(low_arr[k])
                if dirn == "long":
                    if l <= e_stop:
                        hit_reason = "stop"; exit_price = e_stop; break
                    if h >= target:
                        hit_reason = "target"; exit_price = target; break
                else:
                    if h >= e_stop:
                        hit_reason = "stop"; exit_price = e_stop; break
                    if l <= target:
                        hit_reason = "target"; exit_price = target; break

            if dirn == "long":
                gross = (exit_price - e_entry) / e_entry
            else:
                gross = (e_entry - exit_price) / e_entry
            net   = gross - FEE_RT
            r_ach = gross / (e_risk / e_entry) if e_risk > 0 else 0

            trades.append({
                "timestamp":   ts,
                "year":        year,
                "hour_utc":    hour,
                "direction":   dirn,
                "rrr":         rrr,
                "entry":       e_entry,
                "exit":        exit_price,
                "stop":        e_stop,
                "dump_pct":    dump_pct,
                "exh_bars":    exhaustion_idx - idx,
                "exit_reason": hit_reason,
                "net_pnl":     net,
                "r_achieved":  r_ach,
                "win":         net > 0,
            })

    sys.stdout.write(f"\r  Processing: {len(signals):,}/{len(signals):,} complete\n")
    print()
    print(f"  Filter summary:")
    print(f"    Base signals:        {len(signals):,}")
    print(f"    Filtered (hour):     {filtered_hour:,}")
    print(f"    Filtered (dump<4%):  {filtered_dump:,}")
    print(f"    Filtered (no exhaust):{filtered_no_exh:,}")
    print(f"    Accepted:            {accepted:,}")
    print()

    if not trades:
        print("  No trades generated"); sys.exit(0)

    all_df = pd.DataFrame(trades)
    print(f"  Total trade records: {len(all_df):,}  ({len(RRR_TARGETS)} RRR × {accepted} signals)")
    print(f"  Simulation complete in {time.time()-t0:.1f}s")
    print()

    # ── Results ───────────────────────────────────────────────────────────────
    lines = []
    lines += ["="*70,
              "  ETH H4b — Refined Capitulation Strategy Results",
              f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
              f"  Balance: ${STARTING_BAL:,.0f}  |  Position: {POSITION_PCT*100:.0f}%  |  "
              f"Fees: {FEE_RT*100:.1f}% RT",
              f"  Filters: hours={GOOD_HOURS[:3]}...  dump>={MIN_DUMP_PCT}%  "
              f"exhaust<={MAX_EXHAUST_BARS}bars  wick>{MIN_LOWER_WICK:.0%}",
              "="*70,
              f"",
              f"  Base signals:  {len(signals):,}",
              f"  Accepted:      {accepted:,}  ({accepted/len(signals)*100:.1f}% pass rate)",
              f"  Filtered hour: {filtered_hour:,}",
              f"  Filtered dump: {filtered_dump:,}",
              f"  No exhaustion: {filtered_no_exh:,}"]

    for rrr in RRR_TARGETS:
        section(lines, f"RRR = {rrr}:1")

        rrr_df = all_df[all_df["rrr"] == rrr]
        train  = rrr_df[rrr_df["year"] <= TRAIN_END_YEAR]
        val    = rrr_df[rrr_df["year"] >= VALIDATE_START]

        sub(lines, "FULL PERIOD")
        fmt(perf(rrr_df), lines)

        sub(lines, f"TRAIN ≤{TRAIN_END_YEAR}")
        fmt(perf(train), lines)

        sub(lines, f"VALIDATE ≥{VALIDATE_START} (out-of-sample)")
        fmt(perf(val), lines)

        sub(lines, "Annual breakdown")
        for yr, grp in rrr_df.groupby("year"):
            wins = grp["win"].sum()
            ret  = (grp["net_pnl"] * POSITION_PCT).sum() * 100
            lines.append(f"    {yr}  n={len(grp):>4,}  "
                         f"win={wins/len(grp)*100:.0f}%  "
                         f"return={ret:+.1f}%")

        sub(lines, "By hour (UTC)")
        hour_stats = rrr_df.groupby("hour_utc").agg(
            n=("win","count"),
            wr=("win","mean"),
            ret=("net_pnl","mean")
        ).sort_values("wr", ascending=False)
        for hr, row in hour_stats.iterrows():
            lines.append(f"    {hr:02d}:00  n={row['n']:>4,}  "
                         f"win={row['wr']*100:.1f}%  avg={row['ret']*100:.2f}%")

        sub(lines, "By direction")
        for dirn, grp in rrr_df.groupby("direction"):
            wins = grp["win"].sum()
            ret  = (grp["net_pnl"] * POSITION_PCT).sum() * 100
            lines.append(f"    {dirn:<6}  n={len(grp):>4,}  "
                         f"win={wins/len(grp)*100:.0f}%  return={ret:+.1f}%")

    # ── Compare H4 vs H4b ─────────────────────────────────────────────────────
    section(lines, "H4 vs H4b COMPARISON (at 2.0R)")
    lines += ["",
              "  H4 (original):",
              "    Trades:      10,118  Win: 34.1%  Return: -100%  MaxDD: 100%",
              "",
              "  H4b (refined):"]
    h4b_2r = all_df[all_df["rrr"]==2.0]
    r = perf(h4b_2r)
    if r["n"] > 0:
        lines.append(f"    Trades:      {r['n']:,}  "
                     f"Win: {r['win_rate']:.1f}%  "
                     f"Return: {r['total_return']:+.1f}%  "
                     f"MaxDD: {r['max_dd']:.1f}%")
        improvement = r['win_rate'] - 34.1
        lines.append(f"    Win rate improvement: {improvement:+.1f}%")

    lines += ["", "="*70,
              f"  Total runtime: {time.time()-t0:.1f}s",
              f"  Saved: {OUTPUT_FILE}",
              "="*70]

    output = "\n".join(lines)
    print(output)
    OUTPUT_FILE.write_text(output)
    print(f"\n  Saved: {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()
