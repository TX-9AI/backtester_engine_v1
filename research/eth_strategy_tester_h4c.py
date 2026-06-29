# research/eth_strategy_tester_h4c.py — backtester_engine_v1
# v1.0 — 2026-06-29 — H4c: Next-candle confirmation + low volume exhaustion
#                      Derived from H4b anatomy analysis:
#                      1. Next candle must CONFIRM direction (54.6% vs 34.6%)
#                      2. Low volume exhaustion candle (<1.5x avg) — 47.6% vs 36.4%
#                      3. Two-sided exhaustion candle (both wicks present)
#                      4. Timeouts preserved at 480 candles (54% win, +0.46%)
#                      5. Session filter preserved from H4b
#                      6. Entry on OPEN of candle after confirmation, not exhaustion close

"""
H4c — Next-candle confirmed capitulation reversal

Key discoveries from H4b anatomy:
- Next candle confirmation: 54.6% win vs 34.6% without  ← biggest edge
- Low volume at exhaustion: 47.6% win vs 36.4% high vol  ← counter-intuitive
- Two-sided candle beats pure pin bar
- Timeouts profitable (54% win, +0.46%) — don't cut them
- Entry: wait one more candle for confirmation, enter on its open
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
OUTPUT_FILE  = OUTPUT_DIR / "eth_h4c_results.txt"

COLS = ["timestamp","open","high","low","close","volume","trades"]

# ── H4c Parameters ────────────────────────────────────────────────────────────
GOOD_HOURS       = list(range(0, 7)) + [10, 11, 12]  # from H4b
MIN_DUMP_PCT     = 4.0
MAX_EXHAUST_BARS = 35
MIN_LOWER_WICK   = 0.25      # exhaustion candle lower wick (long) / upper wick (short)
MIN_TWO_SIDE_WICK= 0.15      # opposite wick must also be present (two-sided candle)
MAX_VOL_RATIO    = 1.5       # exhaustion candle must be LOW volume (sellers dried up)
MAX_HOLD         = 480       # keep at 480 — timeouts are profitable
ATR_STOP_MULT    = 1.5
FEE_RT           = 0.004
POSITION_PCT     = 0.20
STARTING_BAL     = 10000.0
TRAIN_END_YEAR   = 2021
VALIDATE_START   = 2022
RRR_TARGETS      = [1.5, 2.0, 3.0]


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
        lines.append(f"    Exits:         " +
                     "  ".join(f"{k}={v}" for k,v in ex.items()))

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
    return {
        "n": n, "win_rate": wr*100, "avg_r": ar,
        "profit_factor": pf, "max_dd": max_dd*100,
        "total_return": ret, "final_balance": balance, "sharpe": sharpe,
        "exits": trades["exit_reason"].value_counts().to_dict()
    }


def main():
    print(f"\n  ETH H4c — Next-Candle Confirmed Capitulation")
    print(f"  {'─'*52}")
    print(f"  Key changes from H4b:")
    print(f"    + Next candle must confirm direction before entry")
    print(f"    + Low volume exhaustion required (<{MAX_VOL_RATIO}x avg)")
    print(f"    + Two-sided exhaustion candle (both wicks >{MIN_TWO_SIDE_WICK:.0%})")
    print(f"    + Entry on open of candle AFTER confirmation")
    print(f"    + Hold period preserved at {MAX_HOLD} candles (timeouts profitable)")
    print()

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

    # ── Simulate ──────────────────────────────────────────────────────────────
    print("  Applying H4c filters...")
    trades = []
    f_hour = f_dump = f_no_exh = f_vol = f_two_side = f_confirm = accepted = 0

    for si, sig in enumerate(signals):
        if si % 1000 == 0:
            sys.stdout.write(f"\r  {si:,}/{len(signals):,}  accepted={accepted}  "
                             f"f_hour={f_hour} f_dump={f_dump} "
                             f"f_exh={f_no_exh} f_vol={f_vol} "
                             f"f_2side={f_two_side} f_conf={f_confirm}")
            sys.stdout.flush()

        ts   = int(sig["timestamp"])
        dirn = sig["direction"]
        atr_v= float(sig["atr"])

        idx = ts_to_idx.get(ts)
        if idx is None or idx < 60 or idx + MAX_EXHAUST_BARS + MAX_HOLD + 2 >= n_df:
            continue

        dt   = datetime.fromtimestamp(ts, tz=timezone.utc)
        hour = dt.hour
        year = dt.year

        # ── Filter 1: Session hours ───────────────────────────────────────────
        if hour not in GOOD_HOURS:
            f_hour += 1
            continue

        entry_price = float(close_arr[idx])
        vol60_mean  = float(vol_arr[max(0,idx-60):idx].mean())

        # ── Filter 2: Minimum dump size ───────────────────────────────────────
        if dirn == "long":
            pre_high = float(high_arr[max(0,idx-30):idx].max())
            dump_pct = (pre_high - entry_price) / pre_high * 100
        else:
            pre_low  = float(low_arr[max(0,idx-30):idx].min())
            dump_pct = (entry_price - pre_low) / entry_price * 100

        if dump_pct < MIN_DUMP_PCT:
            f_dump += 1
            continue

        # ── Filter 3: Find exhaustion candle ──────────────────────────────────
        exh_idx  = None
        exh_data = {}

        for j in range(idx, min(idx + MAX_EXHAUST_BARS, n_df - 3)):
            o = float(open_arr[j]); h = float(high_arr[j])
            l = float(low_arr[j]);  c = float(close_arr[j])
            v = float(vol_arr[j])
            tr = h - l if h != l else 0.0001

            lw  = (min(o,c) - l) / tr    # lower wick
            uw  = (h - max(o,c)) / tr    # upper wick
            cp  = (c - l) / tr           # close position
            vol_ratio = v / vol60_mean if vol60_mean > 0 else 1.0

            if dirn == "long":
                # Long exhaustion: big lower wick, two-sided, LOW volume
                if (lw >= MIN_LOWER_WICK and
                    uw >= MIN_TWO_SIDE_WICK and
                    vol_ratio <= MAX_VOL_RATIO):
                    exh_idx  = j
                    exh_data = {"o":o,"h":h,"l":l,"c":c,"v":v,
                                "lw":lw,"uw":uw,"cp":cp,"vr":vol_ratio}
                    break
            else:
                # Short exhaustion: big upper wick, two-sided, LOW volume
                if (uw >= MIN_LOWER_WICK and
                    lw >= MIN_TWO_SIDE_WICK and
                    vol_ratio <= MAX_VOL_RATIO):
                    exh_idx  = j
                    exh_data = {"o":o,"h":h,"l":l,"c":c,"v":v,
                                "lw":lw,"uw":uw,"cp":cp,"vr":vol_ratio}
                    break

        if exh_idx is None:
            f_no_exh += 1
            continue

        # ── Filter 4: Next candle confirmation ────────────────────────────────
        # The candle AFTER exhaustion must confirm direction
        conf_idx = exh_idx + 1
        conf_o   = float(open_arr[conf_idx])
        conf_c   = float(close_arr[conf_idx])
        conf_h   = float(high_arr[conf_idx])
        conf_l   = float(low_arr[conf_idx])

        if dirn == "long":
            # Confirmation: next candle closes bullish
            if conf_c <= conf_o:
                f_confirm += 1
                continue
        else:
            # Confirmation: next candle closes bearish
            if conf_c >= conf_o:
                f_confirm += 1
                continue

        accepted += 1

        # ── Entry: open of candle AFTER confirmation ──────────────────────────
        entry_idx = conf_idx + 1
        if entry_idx >= n_df:
            continue

        e_entry = float(open_arr[entry_idx])

        # Stop: beyond the exhaustion candle extreme
        if dirn == "long":
            e_stop = exh_data["l"] - atr_v * ATR_STOP_MULT
        else:
            e_stop = exh_data["h"] + atr_v * ATR_STOP_MULT

        e_risk = abs(e_entry - e_stop)
        if e_risk <= 0:
            continue

        # ── Simulate ──────────────────────────────────────────────────────────
        for rrr in RRR_TARGETS:
            target = (e_entry + e_risk*rrr) if dirn=="long" else (e_entry - e_risk*rrr)
            hit    = "timeout"
            ep     = float(close_arr[min(entry_idx+MAX_HOLD, n_df-1)])

            for k in range(entry_idx+1, min(entry_idx+MAX_HOLD+1, n_df)):
                hk = float(high_arr[k]); lk = float(low_arr[k])
                if dirn == "long":
                    if lk <= e_stop: hit="stop";   ep=e_stop;  break
                    if hk >= target: hit="target";  ep=target;  break
                else:
                    if hk >= e_stop: hit="stop";   ep=e_stop;  break
                    if lk <= target: hit="target";  ep=target;  break

            gross = (ep-e_entry)/e_entry if dirn=="long" else (e_entry-ep)/e_entry
            net   = gross - FEE_RT
            r_ach = gross / (e_risk/e_entry)

            trades.append({
                "timestamp":   ts,
                "year":        year,
                "hour_utc":    hour,
                "direction":   dirn,
                "rrr":         rrr,
                "entry":       e_entry,
                "exit":        ep,
                "stop":        e_stop,
                "dump_pct":    dump_pct,
                "exh_lwick":   exh_data["lw"],
                "exh_uwick":   exh_data["uw"],
                "exh_vol_r":   exh_data["vr"],
                "exit_reason": hit,
                "net_pnl":     net,
                "r_achieved":  r_ach,
                "win":         net > 0,
            })

    sys.stdout.write(f"\r  {len(signals):,}/{len(signals):,} complete\n\n")

    print(f"  Filter summary:")
    print(f"    Base signals:         {len(signals):,}")
    print(f"    Filtered (hour):      {f_hour:,}")
    print(f"    Filtered (dump<4%):   {f_dump:,}")
    print(f"    Filtered (no exhaust):{f_no_exh:,}  (vol or wick failed)")
    print(f"    Filtered (no confirm):{f_confirm:,}  ← new filter")
    print(f"    Accepted:             {accepted:,}")
    print()

    if not trades:
        print("  No trades — relax filters"); sys.exit(0)

    all_df = pd.DataFrame(trades)
    print(f"  {accepted:,} signals × {len(RRR_TARGETS)} RRR = {len(all_df):,} records")
    print()

    # ── Results ───────────────────────────────────────────────────────────────
    lines = []
    lines += ["="*70,
              "  ETH H4c — Next-Candle Confirmed Capitulation",
              f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
              f"  Balance: ${STARTING_BAL:,.0f}  |  Position: {POSITION_PCT*100:.0f}%  |  "
              f"Fees: {FEE_RT*100:.1f}% RT maker",
              f"  New filters: next candle confirm + low vol exhaustion + two-sided wick",
              "="*70,
              f"",
              f"  Base:      {len(signals):,} signals",
              f"  Accepted:  {accepted:,}  ({accepted/len(signals)*100:.1f}% pass rate)",
              f"  Rejected (hour):    {f_hour:,}",
              f"  Rejected (dump):    {f_dump:,}",
              f"  Rejected (exhaust): {f_no_exh:,}",
              f"  Rejected (confirm): {f_confirm:,}  ← next candle failed to confirm"]

    for rrr in RRR_TARGETS:
        section(lines, f"RRR = {rrr}:1")
        rrr_df = all_df[all_df["rrr"]==rrr]
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
            ret  = (grp["net_pnl"]*POSITION_PCT).sum()*100
            n    = len(grp)
            lines.append(f"    {yr}  n={n:>3,}  "
                         f"win={wins/n*100:.0f}%  "
                         f"return={ret:+.1f}%")

        sub(lines, "By hour (UTC)")
        for hr, grp in rrr_df.groupby("hour_utc"):
            wins = grp["win"].sum()
            avg  = grp["net_pnl"].mean()*100
            lines.append(f"    {hr:02d}:00  n={len(grp):>3,}  "
                         f"win={wins/len(grp)*100:.0f}%  avg={avg:+.2f}%")

        sub(lines, "By direction")
        for dirn, grp in rrr_df.groupby("direction"):
            wins = grp["win"].sum()
            ret  = (grp["net_pnl"]*POSITION_PCT).sum()*100
            lines.append(f"    {dirn:<6}  n={len(grp):>3,}  "
                         f"win={wins/len(grp)*100:.0f}%  return={ret:+.1f}%")

        sub(lines, "Exit reason breakdown")
        for reason, grp in rrr_df.groupby("exit_reason"):
            wins = grp["win"].sum()
            avg  = grp["net_pnl"].mean()*100
            lines.append(f"    {reason:<10}  n={len(grp):>3,}  "
                         f"win={wins/len(grp)*100:.0f}%  avg={avg:+.2f}%")

    # ── Progression summary ───────────────────────────────────────────────────
    section(lines, "PROGRESSION: H4 → H4b → H4c (at 2.0R)")
    lines += ["",
              "  H4  (baseline):          10,118 trades  win=34.1%  return=-100%  dd=100%",
              "  H4b (+session+exhaust):     445 trades  win=44.5%  return=+46%   dd=50%"]

    h4c_2r = all_df[all_df["rrr"]==2.0]
    r = perf(h4c_2r)
    if r["n"] > 0:
        lines.append(f"  H4c (+confirm+lowvol):  "
                     f"{r['n']:>5,} trades  "
                     f"win={r['win_rate']:.1f}%  "
                     f"return={r['total_return']:+.1f}%  "
                     f"dd={r['max_dd']:.1f}%")

    lines += ["", "="*70,
              f"  Runtime: {time.time()-t0:.1f}s  |  Saved: {OUTPUT_FILE}",
              "="*70]

    output = "\n".join(lines)
    print(output)
    OUTPUT_FILE.write_text(output)
    print(f"\n  Saved: {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()
