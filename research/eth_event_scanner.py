# research/eth_event_scanner.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Scan full ETH/USD 1m history for significant moves
#                      Parallel processing across 4 cores
#                      No preconceptions — let the data reveal the patterns

import argparse
import multiprocessing as mp
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

MOVE_PCT    = 2.0
MOVE_WINDOW = 30
LOOKBACK    = 240
LOOKAHEAD   = 120
MIN_PRICE   = 5.0
CHUNK_SIZE  = 50000
N_CORES     = 4

INPUT_FILE     = Path.home() / "ETHUSD_1m.csv"
OUTPUT_DIR     = Path(__file__).parent
OUTPUT_EVENTS  = OUTPUT_DIR / "eth_events.csv"
OUTPUT_SUMMARY = OUTPUT_DIR / "eth_summary.txt"

COLS = ["timestamp", "open", "high", "low", "close", "volume", "trades"]


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def atr_val(df, period=14):
    h, l, c = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h-l, (h-c).abs(), (l-c).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1]) if len(df) >= period else 0.0

def bb_width_val(closes, period=20):
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    bw  = ((mid + 2*std) - (mid - 2*std)) / mid
    return float(bw.iloc[-1]) if len(closes) >= period else 0.02

def classify_pre(df_pre):
    if len(df_pre) < 60:
        return "UNKNOWN"
    closes = df_pre["close"]
    price  = float(closes.iloc[-1])
    chg    = (price - float(closes.iloc[0])) / float(closes.iloc[0])
    e9     = float(ema(closes, 9).iloc[-1])
    e21    = float(ema(closes, 21).iloc[-1])
    e50    = float(ema(closes, 50).iloc[-1])
    bw     = bb_width_val(closes)
    if bw < 0.015:
        return "COMPRESSING"
    if price > e9 > e21 > e50 and chg > 0.005:
        return "TRENDING_UP"
    if price < e9 < e21 < e50 and chg < -0.005:
        return "TRENDING_DOWN"
    highs = df_pre["high"].iloc[-20:]
    lows  = df_pre["low"].iloc[-20:]
    av    = atr_val(df_pre)
    if av > 0 and (highs.max()-highs.min() > av*3 or lows.max()-lows.min() > av*3):
        return "POST_SWEEP"
    return "RANGING"


def analyze_chunk(args):
    chunk_df, chunk_id = args
    events = []
    prices = chunk_df["close"].values
    highs  = chunk_df["high"].values
    lows   = chunk_df["low"].values
    times  = chunk_df["timestamp"].values
    n      = len(chunk_df)

    for i in range(LOOKBACK, n - LOOKAHEAD - MOVE_WINDOW):
        price_now = float(prices[i])
        if price_now < MIN_PRICE:
            continue

        fh = float(highs[i:i+MOVE_WINDOW].max())
        fl = float(lows[i:i+MOVE_WINDOW].min())
        up   = (fh - price_now) / price_now * 100
        down = (price_now - fl) / price_now * 100

        if up >= MOVE_PCT and up > down:
            direction, magnitude = "UP", up
        elif down >= MOVE_PCT and down > up:
            direction, magnitude = "DOWN", down
        else:
            continue

        pre_df     = chunk_df.iloc[i-LOOKBACK:i].copy()
        pre_closes = pre_df["close"]
        pre_vols   = pre_df["volume"]

        ts = int(times[i])
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)

        av  = atr_val(pre_df)
        bw  = bb_width_val(pre_closes)
        vm  = float(pre_vols.iloc[-20:].mean()) if len(pre_vols) >= 20 else 1.0
        pre_chg = (float(pre_closes.iloc[-1]) - float(pre_closes.iloc[0])) / float(pre_closes.iloc[0]) * 100

        e9  = float(ema(pre_closes, 9).iloc[-1])
        e21 = float(ema(pre_closes, 21).iloc[-1])
        e50 = float(ema(pre_closes, 50).iloc[-1])

        if price_now > e9 > e21 > e50:
            stack = "BULL"
        elif price_now < e9 < e21 < e50:
            stack = "BEAR"
        else:
            stack = "MIXED"

        h4h = float(highs[max(0,i-240):i].max())
        h4l = float(lows[max(0,i-240):i].min())

        post_h = float(highs[i+MOVE_WINDOW:i+MOVE_WINDOW+LOOKAHEAD].max()) if i+MOVE_WINDOW+LOOKAHEAD <= n else price_now
        post_l = float(lows[i+MOVE_WINDOW:i+MOVE_WINDOW+LOOKAHEAD].min()) if i+MOVE_WINDOW+LOOKAHEAD <= n else price_now
        peak   = fh if direction == "UP" else fl
        if direction == "UP":
            cont = (post_h - peak) / peak * 100
            rev  = (peak - post_l) / peak * 100
        else:
            cont = (peak - post_l) / peak * 100
            rev  = (post_h - peak) / peak * 100
        follow = "CONTINUED" if cont > rev else "REVERSED"

        events.append({
            "timestamp":         ts,
            "datetime_utc":      dt.strftime("%Y-%m-%d %H:%M"),
            "year":              dt.year,
            "month":             dt.month,
            "hour_utc":          dt.hour,
            "day_of_week":       dt.weekday(),
            "price":             round(price_now, 4),
            "direction":         direction,
            "magnitude_pct":     round(magnitude, 3),
            "pre_condition":     classify_pre(pre_df),
            "ema_stack":         stack,
            "atr":               round(av, 4),
            "bb_width":          round(bw, 5),
            "vol_mean_20":       round(vm, 4),
            "pre_change_pct":    round(pre_chg, 3),
            "dist_from_4h_high": round((h4h-price_now)/price_now*100, 3),
            "dist_from_4h_low":  round((price_now-h4l)/price_now*100, 3),
            "follow_through":    follow,
            "continuation_pct":  round(cont, 3),
            "reversal_pct":      round(rev, 3),
        })

    return events


def main():
    global MOVE_PCT, MOVE_WINDOW, N_CORES

    parser = argparse.ArgumentParser()
    parser.add_argument("--min-pct", type=float, default=MOVE_PCT)
    parser.add_argument("--window",  type=int,   default=MOVE_WINDOW)
    parser.add_argument("--cores",   type=int,   default=N_CORES)
    args = parser.parse_args()

    MOVE_PCT    = args.min_pct
    MOVE_WINDOW = args.window
    N_CORES     = args.cores

    print(f"\n  ETH/USD Event Scanner")
    print(f"  {'─'*52}")
    print(f"  Criteria: >= {MOVE_PCT}% in <= {MOVE_WINDOW}min")
    print(f"  Cores:    {N_CORES}")
    print()

    if not INPUT_FILE.exists():
        print(f"  ERROR: {INPUT_FILE} not found"); sys.exit(1)

    print(f"  Loading data...")
    t0 = time.time()
    df = pd.read_csv(INPUT_FILE, header=None, names=COLS,
                     dtype={"timestamp":"int64","open":"float32","high":"float32",
                            "low":"float32","close":"float32","volume":"float32","trades":"int32"})
    df = df[df["close"] >= MIN_PRICE].reset_index(drop=True)
    n  = len(df)

    dt0 = datetime.fromtimestamp(int(df["timestamp"].iloc[0]),  tz=timezone.utc)
    dt1 = datetime.fromtimestamp(int(df["timestamp"].iloc[-1]), tz=timezone.utc)
    print(f"  {n:,} rows  |  {dt0.date()} → {dt1.date()}")
    print(f"  ${df['close'].min():.2f} → ${df['close'].max():.2f}")
    print()

    overlap = LOOKBACK + LOOKAHEAD + MOVE_WINDOW
    chunks  = []
    step    = CHUNK_SIZE
    for start in range(0, n - overlap, step):
        end      = min(start + step + overlap, n)
        chunk_df = df.iloc[start:end].copy().reset_index(drop=True)
        chunks.append((chunk_df, len(chunks)))

    print(f"  Scanning {len(chunks)} chunks on {N_CORES} cores...")
    t1 = time.time()
    with mp.Pool(processes=N_CORES) as pool:
        results = pool.map(analyze_chunk, chunks)

    all_events = [e for chunk in results for e in chunk]
    print(f"  Scan: {time.time()-t1:.1f}s  |  Raw events: {len(all_events):,}")

    if not all_events:
        print("  No events found"); sys.exit(0)

    ev = pd.DataFrame(all_events).sort_values("timestamp").reset_index(drop=True)
    ev["ts_group"] = ev["timestamp"] // (MOVE_WINDOW * 60)
    ev = (ev.sort_values(["ts_group","magnitude_pct"], ascending=[True,False])
            .drop_duplicates(subset=["ts_group","direction"])
            .drop(columns=["ts_group"])
            .sort_values("timestamp")
            .reset_index(drop=True))

    print(f"  Deduplicated: {len(ev):,} unique events")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ev.to_csv(OUTPUT_EVENTS, index=False)
    print(f"  Saved: {OUTPUT_EVENTS}")

    # ── Summary ───────────────────────────────────────────────────────────────
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    lines = []
    lines += ["="*70,
              "  ETH/USD Significant Move Analysis",
              f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
              "="*70,
              f"  Total events:  {len(ev):,}",
              f"  Date range:    {dt0.date()} → {dt1.date()}",
              f"  Criteria:      >= {MOVE_PCT}% in <= {MOVE_WINDOW} minutes",
              ""]

    up   = ev[ev["direction"]=="UP"]
    down = ev[ev["direction"]=="DOWN"]
    lines += [f"  Direction split:",
              f"    UP:    {len(up):,}  ({len(up)/len(ev)*100:.1f}%)",
              f"    DOWN:  {len(down):,}  ({len(down)/len(ev)*100:.1f}%)", ""]

    lines.append("  Pre-condition when move started:")
    for cond, grp in ev.groupby("pre_condition"):
        cr = (grp["follow_through"]=="CONTINUED").mean()*100
        lines.append(f"    {cond:<20} {len(grp):>5,} events  "
                     f"avg={grp['magnitude_pct'].mean():.2f}%  "
                     f"continued={cr:.0f}%")

    lines += ["", "  EMA stack at event:"]
    for stack, grp in ev.groupby("ema_stack"):
        lines.append(f"    {stack:<8} {len(grp):>5,} ({len(grp)/len(ev)*100:.1f}%)")

    cont_rate = (ev["follow_through"]=="CONTINUED").mean()*100
    lines += ["", f"  Follow-through: {cont_rate:.1f}% continued  |  {100-cont_rate:.1f}% reversed"]

    lines += ["", "  Top hours for significant moves (UTC):"]
    hc = ev.groupby("hour_utc").size().sort_values(ascending=False)
    for h, c in hc.head(8).items():
        lines.append(f"    {h:02d}:00   {c:>4,}")

    lines += ["", "  Day of week:"]
    for d, c in ev.groupby("day_of_week").size().items():
        lines.append(f"    {days[d]}  {c:>4,}")

    lines += ["", "  Magnitude buckets:"]
    for lo, hi, label in [(2,3,"2-3%"),(3,5,"3-5%"),(5,10,"5-10%"),(10,999,"10%+")]:
        g = ev[(ev["magnitude_pct"]>=lo)&(ev["magnitude_pct"]<hi)]
        lines.append(f"    {label:<8}  {len(g):>4,}  ({len(g)/len(ev)*100:.1f}%)")

    lines += ["", "  Top 25 largest moves:"]
    for _, row in ev.nlargest(25, "magnitude_pct").iterrows():
        lines.append(f"    {row['datetime_utc']}  {row['direction']:<5} "
                     f"{row['magnitude_pct']:>6.2f}%  @${row['price']:>8.2f}  "
                     f"{row['pre_condition']:<15}  {row['follow_through']}")

    lines += ["", "  Annual summary:"]
    for yr, grp in ev.groupby("year"):
        u = (grp["direction"]=="UP").sum()
        d = (grp["direction"]=="DOWN").sum()
        lines.append(f"    {yr}  {len(grp):>4,} events  up={u:>3}  down={d:>3}  "
                     f"avg={grp['magnitude_pct'].mean():.2f}%")

    lines += ["", f"  Full data: {OUTPUT_EVENTS}", "="*70]
    summary = "\n".join(lines)
    print("\n" + summary)
    OUTPUT_SUMMARY.write_text(summary)
    print(f"\n  Saved: {OUTPUT_SUMMARY}")
    print(f"  Total: {time.time()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
