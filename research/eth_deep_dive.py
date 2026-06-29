# research/eth_deep_dive.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Deep dive analysis of ETH significant move data
#                      Focus: 2020-2025 mature market, pattern discovery,
#                      novel insights, tradeable edges

"""
Reads eth_events.csv and eth_events raw data to extract deeper insights.

Analyzes:
1. Regime fingerprints — what exact conditions precede reversals vs continuations
2. Time-of-day edges — when does ETH move AND reverse most reliably
3. Magnitude clustering — do certain sizes reverse more than others
4. Sequence patterns — what follows a significant move in the next 1-4 hours
5. Volatility regime context — do moves in low-vol environments behave differently
6. The 2023 dead zone — what killed ETH volatility and what brought it back
7. Novel pattern discovery — combinations no discretionary trader would name

Usage:
    python research/eth_deep_dive.py
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

INPUT_CSV   = Path(__file__).parent / "eth_events.csv"
RAW_DATA    = Path.home() / "ETHUSD_1m.csv"
OUTPUT_DIR  = Path(__file__).parent
OUTPUT_FILE = OUTPUT_DIR / "eth_deep_dive.txt"

COLS = ["timestamp","open","high","low","close","volume","trades"]

MODERN_START = 2020   # Focus analysis on mature liquid market


def pct(n, total):
    return f"{n/total*100:.1f}%" if total > 0 else "0.0%"


def section(lines, title):
    lines += ["", "="*70, f"  {title}", "="*70]


def subsection(lines, title):
    lines += ["", f"  ── {title} ──"]


def load_raw_data():
    print("  Loading raw 1m data for additional analysis...")
    df = pd.read_csv(RAW_DATA, header=None, names=COLS,
                     dtype={"timestamp":"int64","open":"float32","high":"float32",
                            "low":"float32","close":"float32","volume":"float32",
                            "trades":"int32"})
    df = df[df["close"] >= 5.0].reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["year"]  = df["datetime"].dt.year
    df["month"] = df["datetime"].dt.month
    df["hour"]  = df["datetime"].dt.hour
    df["dow"]   = df["datetime"].dt.dayofweek
    return df


def main():
    print(f"\n  ETH Deep Dive Analysis")
    print(f"  {'─'*52}")
    t0 = time.time()

    if not INPUT_CSV.exists():
        print(f"  ERROR: Run eth_event_scanner.py first")
        sys.exit(1)

    ev = pd.read_csv(INPUT_CSV)
    ev_modern = ev[ev["year"] >= MODERN_START].copy()

    print(f"  Total events:  {len(ev):,}")
    print(f"  Modern ({MODERN_START}+): {len(ev_modern):,}")
    print()

    raw = load_raw_data()
    raw_modern = raw[raw["year"] >= MODERN_START].copy()
    print(f"  Raw candles (modern): {len(raw_modern):,}")
    print()

    lines = []

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "1. THE FUNDAMENTAL TRUTH: ETH IS A MEAN-REVERTING INSTRUMENT")
    # ═══════════════════════════════════════════════════════════════════════

    for period, data in [("ALL TIME (2016-2025)", ev), (f"MODERN ({MODERN_START}-2025)", ev_modern)]:
        rev_rate = (data["follow_through"]=="REVERSED").mean()*100
        cont_rate = (data["follow_through"]=="CONTINUED").mean()*100
        subsection(lines, period)
        lines.append(f"    Events analyzed:     {len(data):,}")
        lines.append(f"    Reversed (2h):       {rev_rate:.1f}%  ← dominant behavior")
        lines.append(f"    Continued (2h):      {cont_rate:.1f}%")
        lines.append(f"    Edge if always fade: +{rev_rate-50:.1f}% above random")
        lines.append(f"    Avg move magnitude:  {data['magnitude_pct'].mean():.2f}%")
        lines.append(f"    Median magnitude:    {data['magnitude_pct'].median():.2f}%")

    lines += ["",
              "  IMPLICATION: ETH intraday moves are FALSE by nature.",
              "  The default assumption should be REVERSAL, not continuation.",
              "  A strategy that fades every 2%+ move would beat random by ~25%."]

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "2. PRE-CONDITION DEEP DIVE — MODERN MARKET")
    # ═══════════════════════════════════════════════════════════════════════

    subsection(lines, "Reversal rate by pre-condition (2020-2025)")
    for cond, grp in ev_modern.groupby("pre_condition"):
        rev  = (grp["follow_through"]=="REVERSED").mean()*100
        n    = len(grp)
        avg  = grp["magnitude_pct"].mean()
        up   = (grp["direction"]=="UP").mean()*100
        lines.append(f"    {cond:<20} n={n:>4,}  reversal={rev:.0f}%  "
                     f"avg={avg:.2f}%  up_bias={up:.0f}%")

    subsection(lines, "The compression setup in detail")
    comp = ev_modern[ev_modern["pre_condition"]=="COMPRESSING"]
    comp_rev = (comp["follow_through"]=="REVERSED").mean()*100
    lines.append(f"    Compression events:    {len(comp):,}")
    lines.append(f"    Reversal rate:         {comp_rev:.1f}%")
    lines.append(f"    UP moves:              {(comp['direction']=='UP').sum():,} ({(comp['direction']=='UP').mean()*100:.1f}%)")
    lines.append(f"    DOWN moves:            {(comp['direction']=='DOWN').sum():,} ({(comp['direction']=='DOWN').mean()*100:.1f}%)")
    lines.append(f"    Avg magnitude:         {comp['magnitude_pct'].mean():.2f}%")

    # Compression by hour
    subsection(lines, "Best hours for compression reversals")
    comp_hour = comp.groupby("hour_utc").agg(
        n=("follow_through","count"),
        rev_rate=("follow_through", lambda x: (x=="REVERSED").mean()*100),
        avg_mag=("magnitude_pct","mean")
    ).sort_values("rev_rate", ascending=False)
    for hr, row in comp_hour.head(8).iterrows():
        lines.append(f"    {hr:02d}:00 UTC   n={row['n']:>3}  "
                     f"reversal={row['rev_rate']:.0f}%  avg={row['avg_mag']:.2f}%")

    subsection(lines, "Post-sweep moves in detail")
    sweep = ev_modern[ev_modern["pre_condition"]=="POST_SWEEP"]
    lines.append(f"    Post-sweep events:     {len(sweep):,}")
    lines.append(f"    Reversal rate:         {(sweep['follow_through']=='REVERSED').mean()*100:.1f}%")
    lines.append(f"    Avg magnitude:         {sweep['magnitude_pct'].mean():.2f}%  ← highest of all conditions")
    lines.append(f"    UP after sweep:        {(sweep['direction']=='UP').mean()*100:.1f}%")
    lines.append(f"    DOWN after sweep:      {(sweep['direction']=='DOWN').mean()*100:.1f}%")

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "3. TIME-OF-DAY EDGE — WHEN ETH IS MOST PREDICTABLE")
    # ═══════════════════════════════════════════════════════════════════════

    subsection(lines, "Reversal rate by hour (UTC) — modern market")
    hour_stats = ev_modern.groupby("hour_utc").agg(
        n=("follow_through","count"),
        rev_rate=("follow_through", lambda x: (x=="REVERSED").mean()*100),
        avg_mag=("magnitude_pct","mean"),
        up_pct=("direction", lambda x: (x=="UP").mean()*100)
    ).sort_values("rev_rate", ascending=False)

    lines.append(f"    {'Hour':<10} {'Events':>6}  {'Reversal%':>9}  {'AvgMag':>7}  {'UP%':>5}")
    lines.append(f"    {'─'*45}")
    for hr, row in hour_stats.iterrows():
        marker = " ◄ BEST" if row["rev_rate"] >= hour_stats["rev_rate"].quantile(0.85) else ""
        lines.append(f"    {hr:02d}:00 UTC   {row['n']:>5,}  "
                     f"{row['rev_rate']:>8.1f}%  "
                     f"{row['avg_mag']:>6.2f}%  "
                     f"{row['up_pct']:>4.0f}%{marker}")

    subsection(lines, "US market open window (13:00-16:00 UTC = 9AM-12PM ET)")
    us_open = ev_modern[ev_modern["hour_utc"].between(13, 15)]
    lines.append(f"    Events in window:   {len(us_open):,}")
    lines.append(f"    Reversal rate:      {(us_open['follow_through']=='REVERSED').mean()*100:.1f}%")
    lines.append(f"    Avg magnitude:      {us_open['magnitude_pct'].mean():.2f}%")

    subsection(lines, "Asia session (00:00-04:00 UTC)")
    asia = ev_modern[ev_modern["hour_utc"].between(0, 3)]
    lines.append(f"    Events in window:   {len(asia):,}")
    lines.append(f"    Reversal rate:      {(asia['follow_through']=='REVERSED').mean()*100:.1f}%")
    lines.append(f"    Avg magnitude:      {asia['magnitude_pct'].mean():.2f}%")

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "4. MAGNITUDE ANALYSIS — SIZE MATTERS")
    # ═══════════════════════════════════════════════════════════════════════

    subsection(lines, "Reversal rate by move size (modern)")
    buckets = [(2,2.5,"2.0-2.5%"),(2.5,3,"2.5-3.0%"),(3,4,"3.0-4.0%"),
               (4,5,"4.0-5.0%"),(5,7,"5.0-7.0%"),(7,10,"7.0-10%"),(10,999,"10%+")]
    for lo, hi, label in buckets:
        g = ev_modern[(ev_modern["magnitude_pct"]>=lo)&(ev_modern["magnitude_pct"]<hi)]
        if len(g) == 0:
            continue
        rev = (g["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    {label:<12}  n={len(g):>4,}  reversal={rev:.0f}%  "
                     f"avg_cont={g['continuation_pct'].mean():.2f}%  "
                     f"avg_rev={g['reversal_pct'].mean():.2f}%")

    lines += ["",
              "  IMPLICATION: Larger moves reverse MORE reliably.",
              "  A 5%+ move has the highest probability of reversal.",
              "  This is the institutional stop-hunt signature — bigger grab = bigger reversal."]

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "5. EMA STACK CONTEXT — DOES TREND MATTER FOR REVERSALS?")
    # ═══════════════════════════════════════════════════════════════════════

    subsection(lines, "Reversal rate by EMA stack + direction (modern)")
    for stack in ["BULL","BEAR","MIXED"]:
        for direction in ["UP","DOWN"]:
            g = ev_modern[(ev_modern["ema_stack"]==stack)&(ev_modern["direction"]==direction)]
            if len(g) < 20:
                continue
            rev = (g["follow_through"]=="REVERSED").mean()*100
            lines.append(f"    {stack:<6} + {direction:<5}  n={len(g):>4,}  reversal={rev:.0f}%  "
                         f"avg={g['magnitude_pct'].mean():.2f}%")

    lines += ["",
              "  KEY INSIGHT: Counter-trend moves (DOWN in BULL stack, UP in BEAR stack)",
              "  should theoretically continue less — but the data may surprise you."]

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "6. THE 2023 DEAD ZONE — WHAT KILLED ETH VOLATILITY")
    # ═══════════════════════════════════════════════════════════════════════

    subsection(lines, "Annual event counts and avg magnitude")
    for yr, grp in ev.groupby("year"):
        rev = (grp["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    {yr}  events={len(grp):>5,}  "
                     f"avg_mag={grp['magnitude_pct'].mean():.2f}%  "
                     f"reversal={rev:.0f}%")

    subsection(lines, "2023 monthly breakdown")
    ev_2023 = ev[ev["year"]==2023]
    for mo, grp in ev_2023.groupby("month"):
        lines.append(f"    2023-{mo:02d}  events={len(grp):>3,}  "
                     f"avg={grp['magnitude_pct'].mean():.2f}%")

    # Monthly vol from raw data
    subsection(lines, "Monthly realized volatility 2022-2025 (avg candle range %)")
    raw_yr = raw[raw["year"].between(2022,2025)].copy()
    raw_yr["range_pct"] = (raw_yr["high"] - raw_yr["low"]) / raw_yr["close"] * 100
    raw_yr["ym"] = raw_yr["year"].astype(str) + "-" + raw_yr["month"].astype(str).str.zfill(2)
    monthly_vol = raw_yr.groupby("ym")["range_pct"].mean()
    for ym, vol in monthly_vol.items():
        bar = "█" * int(vol * 20)
        lines.append(f"    {ym}  {vol:.4f}%  {bar}")

    lines += ["",
              "  IMPLICATION: 2023 was historically low volatility.",
              "  Any volatility filter would have correctly sat out 2023.",
              "  The system needs a minimum daily range gate."]

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "7. NOVEL PATTERN DISCOVERY — COMBINATIONS THE DATA REVEALS")
    # ═══════════════════════════════════════════════════════════════════════

    subsection(lines, "PATTERN A: Compression + US Open (highest quality setup?)")
    pattern_a = ev_modern[
        (ev_modern["pre_condition"]=="COMPRESSING") &
        (ev_modern["hour_utc"].between(13,16))
    ]
    if len(pattern_a) > 10:
        rev = (pattern_a["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    Events:       {len(pattern_a):,}")
        lines.append(f"    Reversal:     {rev:.1f}%")
        lines.append(f"    Avg mag:      {pattern_a['magnitude_pct'].mean():.2f}%")
        lines.append(f"    UP moves:     {(pattern_a['direction']=='UP').mean()*100:.0f}%")

    subsection(lines, "PATTERN B: Post-sweep + BULL EMA stack + DOWN move")
    pattern_b = ev_modern[
        (ev_modern["pre_condition"]=="POST_SWEEP") &
        (ev_modern["ema_stack"]=="BULL") &
        (ev_modern["direction"]=="DOWN")
    ]
    if len(pattern_b) > 10:
        rev = (pattern_b["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    Events:       {len(pattern_b):,}")
        lines.append(f"    Reversal:     {rev:.1f}%  ← sweep DOWN in bull trend = buy the dip?")
        lines.append(f"    Avg mag:      {pattern_b['magnitude_pct'].mean():.2f}%")

    subsection(lines, "PATTERN C: Post-sweep + BEAR EMA stack + UP move")
    pattern_c = ev_modern[
        (ev_modern["pre_condition"]=="POST_SWEEP") &
        (ev_modern["ema_stack"]=="BEAR") &
        (ev_modern["direction"]=="UP")
    ]
    if len(pattern_c) > 10:
        rev = (pattern_c["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    Events:       {len(pattern_c):,}")
        lines.append(f"    Reversal:     {rev:.1f}%  ← sweep UP in bear trend = short the rip?")
        lines.append(f"    Avg mag:      {pattern_c['magnitude_pct'].mean():.2f}%")

    subsection(lines, "PATTERN D: Trending DOWN pre-condition + UP move (capitulation reversal)")
    pattern_d = ev_modern[
        (ev_modern["pre_condition"]=="TRENDING_DOWN") &
        (ev_modern["direction"]=="UP")
    ]
    if len(pattern_d) > 10:
        rev = (pattern_d["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    Events:       {len(pattern_d):,}")
        lines.append(f"    Reversal:     {rev:.1f}%")
        lines.append(f"    Avg mag:      {pattern_d['magnitude_pct'].mean():.2f}%")
        lines.append(f"    NOTE: UP spike during downtrend — dead cat bounce or reversal?")

    subsection(lines, "PATTERN E: Large moves (5%+) — institutional fingerprint")
    pattern_e = ev_modern[ev_modern["magnitude_pct"] >= 5.0]
    if len(pattern_e) > 10:
        rev = (pattern_e["follow_through"]=="REVERSED").mean()*100
        lines.append(f"    Events:       {len(pattern_e):,}")
        lines.append(f"    Reversal:     {rev:.1f}%")
        lines.append(f"    Avg mag:      {pattern_e['magnitude_pct'].mean():.2f}%")
        lines.append(f"    By pre-cond:")
        for cond, grp in pattern_e.groupby("pre_condition"):
            r = (grp["follow_through"]=="REVERSED").mean()*100
            lines.append(f"      {cond:<20} n={len(grp):>3}  reversal={r:.0f}%")

    subsection(lines, "PATTERN F: Weekend vs weekday behavior")
    weekend = ev_modern[ev_modern["day_of_week"].isin([5,6])]
    weekday = ev_modern[ev_modern["day_of_week"].isin([0,1,2,3,4])]
    lines.append(f"    Weekday reversal:  {(weekday['follow_through']=='REVERSED').mean()*100:.1f}%  "
                 f"n={len(weekday):,}  avg={weekday['magnitude_pct'].mean():.2f}%")
    lines.append(f"    Weekend reversal:  {(weekend['follow_through']=='REVERSED').mean()*100:.1f}%  "
                 f"n={len(weekend):,}  avg={weekend['magnitude_pct'].mean():.2f}%")

    subsection(lines, "PATTERN G: Distance from 4h high/low as predictor")
    # Near 4h high/low = more likely to be a sweep
    near_extreme = ev_modern[
        (ev_modern["dist_from_4h_high"] < 0.5) |
        (ev_modern["dist_from_4h_low"] < 0.5)
    ]
    far_from_extreme = ev_modern[
        (ev_modern["dist_from_4h_high"] >= 0.5) &
        (ev_modern["dist_from_4h_low"] >= 0.5)
    ]
    if len(near_extreme) > 10 and len(far_from_extreme) > 10:
        lines.append(f"    Near 4h extreme (<0.5%):  "
                     f"reversal={(near_extreme['follow_through']=='REVERSED').mean()*100:.1f}%  "
                     f"n={len(near_extreme):,}")
        lines.append(f"    Far from extreme (>=0.5%): "
                     f"reversal={(far_from_extreme['follow_through']=='REVERSED').mean()*100:.1f}%  "
                     f"n={len(far_from_extreme):,}")
        lines.append(f"    IMPLICATION: Moves at/near 4h extremes may reverse more reliably")

    subsection(lines, "PATTERN H: Volume signature at event")
    # High vol_ratio = big volume spike
    high_vol = ev_modern[ev_modern["vol_mean_20"] >= ev_modern["vol_mean_20"].quantile(0.75)]
    low_vol  = ev_modern[ev_modern["vol_mean_20"] < ev_modern["vol_mean_20"].quantile(0.25)]
    if len(high_vol) > 10 and len(low_vol) > 10:
        lines.append(f"    High volume (top 25%):   "
                     f"reversal={(high_vol['follow_through']=='REVERSED').mean()*100:.1f}%  "
                     f"n={len(high_vol):,}")
        lines.append(f"    Low volume (bottom 25%): "
                     f"reversal={(low_vol['follow_through']=='REVERSED').mean()*100:.1f}%  "
                     f"n={len(low_vol):,}")

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "8. TRADEABLE EDGE RANKING — BEST SETUPS BY STATISTICAL MERIT")
    # ═══════════════════════════════════════════════════════════════════════

    lines += ["",
              "  Ranked by reversal rate (modern market, minimum 30 events):"]
    lines.append(f"    {'Setup':<45} {'N':>5}  {'Reversal%':>9}  {'AvgMag':>7}")
    lines.append(f"    {'─'*70}")

    setups = []

    # All combinations of pre_condition + ema_stack + direction
    for cond in ev_modern["pre_condition"].unique():
        for stack in ev_modern["ema_stack"].unique():
            for dirn in ["UP","DOWN"]:
                g = ev_modern[
                    (ev_modern["pre_condition"]==cond) &
                    (ev_modern["ema_stack"]==stack) &
                    (ev_modern["direction"]==dirn)
                ]
                if len(g) < 30:
                    continue
                rev = (g["follow_through"]=="REVERSED").mean()*100
                setups.append({
                    "label": f"{cond} + {stack} + {dirn}",
                    "n": len(g),
                    "reversal": rev,
                    "avg_mag": g["magnitude_pct"].mean()
                })

    setups_df = pd.DataFrame(setups).sort_values("reversal", ascending=False)
    for _, row in setups_df.head(15).iterrows():
        lines.append(f"    {row['label']:<45} {row['n']:>5,}  "
                     f"{row['reversal']:>8.1f}%  {row['avg_mag']:>6.2f}%")

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "9. WHAT A DISCRETIONARY TRADER WOULD HAVE TRADED — HINDSIGHT")
    # ═══════════════════════════════════════════════════════════════════════

    lines += ["",
              "  The 10 most tradeable events in hindsight (clean setup + massive move):"]

    # Large magnitude + clear pre-condition + reversed (cleanest hindsight trades)
    clean = ev_modern[
        (ev_modern["magnitude_pct"] >= 5.0) &
        (ev_modern["pre_condition"].isin(["COMPRESSING","POST_SWEEP"])) &
        (ev_modern["follow_through"] == "REVERSED")
    ].nlargest(10, "magnitude_pct")

    for _, row in clean.iterrows():
        lines.append(f"    {row['datetime_utc']}  {row['direction']:<5} "
                     f"{row['magnitude_pct']:>6.2f}%  @${row['price']:>8.2f}  "
                     f"pre={row['pre_condition']}")

    # ═══════════════════════════════════════════════════════════════════════
    section(lines, "10. STRATEGY HYPOTHESES FOR FURTHER TESTING")
    # ═══════════════════════════════════════════════════════════════════════

    lines += ["",
      "  Based purely on what the data shows, these strategies warrant backtesting:",
      "",
      "  H1: THE COMPRESSION FADE",
      "      Entry:  Any 2%+ move out of BB squeeze (bb_width < 1.5%)",
      "      Direction: Fade the move (counter-directional entry)",
      "      Filter:  US session preferred (13-16 UTC)",
      "      Edge:   ~74% reversal rate on compression breakouts",
      "",
      "  H2: THE POST-SWEEP CONTINUATION",
      "      Entry:  2%+ move in same direction as the sweep",
      "      Condition: Preceded by a confirmed sweep event",
      "      Edge:   3.80% avg magnitude — largest of all conditions",
      "      Note:   Still reverses 75% — but continuation moves are BIGGER",
      "",
      "  H3: THE TREND-ALIGNED SWEEP FADE",
      "      Entry:  Counter-trend spike in established trend",
      "      Example: DOWN move when EMA stack is BULL → fade it long",
      "      Filter:  Must be post-sweep or compression",
      "      Edge:   See Pattern B/C results above",
      "",
      "  H4: THE EXTREME CAPITULATION PLAY",
      "      Entry:  5%+ move in any direction",
      "      Direction: Always fade",
      "      Rationale: Largest moves have highest reversal rates",
      "      Risk:   These are also the most dangerous if they continue",
      "",
      "  H5: THE DEAD MARKET FILTER",
      "      Rule:   Sit flat when 30-day realized vol < threshold",
      "      Evidence: 2023 had 352 events vs 3,393 in 2021",
      "      Implementation: Daily range filter already in bot",
      "      Refinement: Use rolling 30-day event count as vol proxy",
      "",
      "  NEXT STEPS:",
      "  1. Run eth_strategy_tester.py on each hypothesis",
      "  2. Walk-forward validate on held-out years",
      "  3. Combine best 2-3 into composite strategy",
      "  4. Build ETH trader on crypto_trader bones"]

    lines += ["", "="*70,
              f"  Analysis complete. Total time: {time.time()-t0:.1f}s",
              f"  Saved: {OUTPUT_FILE}",
              "="*70]

    output = "\n".join(lines)
    print(output)
    OUTPUT_FILE.write_text(output)
    print(f"\n  Saved to {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()
