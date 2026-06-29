# research/eth_trade_anatomy.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Deep anatomy of H4 wins vs losses
#                      Find the real entry signal using hindsight

"""
For every H4 signal, simulates the trade and extracts full candle-by-candle
anatomy of the 30 candles before and 60 candles after the trigger.

Compares winning trades vs losing trades to find:
- Volume signature differences
- Candle pattern at the actual low
- Optimal entry timing vs our entry
- Wick ratio, body ratio, close position
- How far price continued before reversing

Output:
    research/eth_anatomy_results.txt  — full analysis
    research/eth_anatomy_wins.csv     — winning trade details
    research/eth_anatomy_losses.csv   — losing trade details

Usage:
    python research/eth_trade_anatomy.py
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
OUTPUT_TXT   = OUTPUT_DIR / "eth_anatomy_results.txt"
OUTPUT_WINS  = OUTPUT_DIR / "eth_anatomy_wins.csv"
OUTPUT_LOSS  = OUTPUT_DIR / "eth_anatomy_losses.csv"

COLS        = ["timestamp","open","high","low","close","volume","trades"]
ATR_STOP    = 1.5
FEE_RT      = 0.004
PRE_CANDLES = 60    # candles before trigger to analyze
POST_CANDLES= 120   # candles after trigger to analyze
RRR         = 2.0   # use 2R for win/loss classification


def candle_features(row):
    """Extract features from a single candle."""
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    total_range = h - l if h != l else 0.0001
    body        = abs(c - o)
    upper_wick  = h - max(o, c)
    lower_wick  = min(o, c) - l
    return {
        "body_pct":        body / total_range,
        "upper_wick_pct":  upper_wick / total_range,
        "lower_wick_pct":  lower_wick / total_range,
        "close_position":  (c - l) / total_range,  # 0=at low, 1=at high
        "is_bullish":      c > o,
        "range_pct":       total_range / l if l > 0 else 0,
    }


def find_exhaustion_candle(df_post, direction, entry_price, atr):
    """
    Find the candle where price actually exhausted and reversed.
    Returns (bars_to_low, exhaustion_features) or None.
    """
    if len(df_post) < 3:
        return None, None

    if direction == "long":
        # Looking for the actual low after a dump
        low_idx = df_post["low"].idxmin()
        low_pos = df_post.index.get_loc(low_idx)
        if low_pos >= len(df_post) - 2:
            return None, None
        exh_candle = df_post.iloc[low_pos]
        next_candle = df_post.iloc[low_pos + 1]
        features = candle_features(exh_candle)
        features["bars_to_exhaustion"] = low_pos
        features["exhaustion_price"]   = float(exh_candle["low"])
        features["entry_vs_exhaustion"] = (entry_price - float(exh_candle["low"])) / entry_price * 100
        features["next_candle_bullish"] = float(next_candle["close"]) > float(next_candle["open"])
        features["next_candle_vol_ratio"] = float(next_candle["volume"]) / float(exh_candle["volume"]) if float(exh_candle["volume"]) > 0 else 1
        return low_pos, features
    else:
        high_idx = df_post["high"].idxmax()
        high_pos = df_post.index.get_loc(high_idx)
        if high_pos >= len(df_post) - 2:
            return None, None
        exh_candle = df_post.iloc[high_pos]
        next_candle = df_post.iloc[high_pos + 1]
        features = candle_features(exh_candle)
        features["bars_to_exhaustion"] = high_pos
        features["exhaustion_price"]   = float(exh_candle["high"])
        features["entry_vs_exhaustion"] = (float(exh_candle["high"]) - entry_price) / entry_price * 100
        features["next_candle_bullish"] = float(next_candle["close"]) > float(next_candle["open"])
        features["next_candle_vol_ratio"] = float(next_candle["volume"]) / float(exh_candle["volume"]) if float(exh_candle["volume"]) > 0 else 1
        return high_pos, features


def main():
    print(f"\n  ETH Trade Anatomy Analyzer")
    print(f"  {'─'*52}")
    t0 = time.time()

    if not SIGNALS_FILE.exists():
        print(f"  ERROR: Run save_signals.py first"); sys.exit(1)
    if not INPUT_FILE.exists():
        print(f"  ERROR: {INPUT_FILE} not found"); sys.exit(1)

    print("  Loading signals...")
    with open(SIGNALS_FILE) as f:
        signals = json.load(f)
    print(f"  {len(signals):,} signals loaded")

    print("  Loading 1m data...")
    df = pd.read_csv(INPUT_FILE, header=None, names=COLS,
                     dtype={"timestamp":"int64","open":"float64","high":"float64",
                            "low":"float64","close":"float64","volume":"float64",
                            "trades":"int32"})
    df = df[df["close"] >= 5.0].reset_index(drop=True)

    # Build timestamp index
    ts_to_idx = {int(ts): i for i, ts in enumerate(df["timestamp"].values)}
    print(f"  {len(df):,} candles indexed")
    print()

    # ── Simulate and extract anatomy ─────────────────────────────────────────
    print("  Analyzing trade anatomy...")
    wins   = []
    losses = []
    n      = len(signals)

    for si, sig in enumerate(signals):
        if si % 500 == 0:
            sys.stdout.write(f"\r  Processing: {si:,}/{n:,} ({si/n*100:.1f}%)")
            sys.stdout.flush()

        ts    = sig["timestamp"]
        dirn  = sig["direction"]
        entry = sig["entry_price"]
        stop  = sig["stop_price"]
        atr_v = sig["atr"]
        risk  = abs(entry - stop)

        if risk <= 0 or atr_v <= 0:
            continue

        idx = ts_to_idx.get(int(ts))
        if idx is None or idx < PRE_CANDLES or idx + POST_CANDLES >= len(df):
            continue

        df_pre  = df.iloc[idx - PRE_CANDLES:idx].copy().reset_index(drop=True)
        df_post = df.iloc[idx:idx + POST_CANDLES].copy().reset_index(drop=True)

        target = (entry + risk * RRR) if dirn == "long" else (entry - risk * RRR)

        # Simulate
        hit_reason = "timeout"
        exit_price = float(df_post["close"].iloc[-1])
        for j, row in df_post.iterrows():
            if dirn == "long":
                if row["low"] <= stop:
                    hit_reason = "stop"; exit_price = stop; break
                if row["high"] >= target:
                    hit_reason = "target"; exit_price = target; break
            else:
                if row["high"] >= stop:
                    hit_reason = "stop"; exit_price = stop; break
                if row["low"] <= target:
                    hit_reason = "target"; exit_price = target; break

        if dirn == "long":
            net_pnl = (exit_price - entry) / entry - FEE_RT
        else:
            net_pnl = (entry - exit_price) / entry - FEE_RT

        win = net_pnl > 0

        # ── Pre-move features ─────────────────────────────────────────────────
        pre_vol_mean   = float(df_pre["volume"].mean())
        pre_vol_last5  = float(df_pre["volume"].iloc[-5:].mean())
        vol_ratio      = pre_vol_last5 / pre_vol_mean if pre_vol_mean > 0 else 1.0

        # The dump/spike candles (last 30 of pre)
        dump_window    = df_pre.iloc[-30:]
        dump_vol_max   = float(dump_window["volume"].max())
        dump_vol_mean  = float(dump_window["volume"].mean())
        peak_vol_ratio = dump_vol_max / pre_vol_mean if pre_vol_mean > 0 else 1.0

        # Price action in the dump
        if dirn == "long":
            dump_start = float(dump_window["close"].iloc[0])
            dump_end   = float(dump_window["low"].min())
            dump_pct   = (dump_start - dump_end) / dump_start * 100
            # Speed — how many candles to reach the low
            low_candle = dump_window["low"].idxmin()
            dump_speed = len(dump_window) - dump_window.index.get_loc(low_candle)
        else:
            dump_start = float(dump_window["close"].iloc[0])
            dump_end   = float(dump_window["high"].max())
            dump_pct   = (dump_end - dump_start) / dump_start * 100
            high_candle = dump_window["high"].idxmax()
            dump_speed = len(dump_window) - dump_window.index.get_loc(high_candle)

        # Trigger candle features
        trigger_candle = df_pre.iloc[-1]
        trig_features  = candle_features(trigger_candle)

        # Find exhaustion candle in post data
        exh_pos, exh_features = find_exhaustion_candle(df_post, dirn, entry, atr_v)

        # Optimal entry — what if we entered at the actual exhaustion low/high?
        if exh_features and exh_features.get("exhaustion_price"):
            optimal_entry = exh_features["exhaustion_price"]
            optimal_risk  = abs(entry - optimal_entry)
            optimal_improvement = (optimal_risk / risk * 100) if risk > 0 else 0
        else:
            optimal_entry = entry
            optimal_improvement = 0

        # Post-move features
        if dirn == "long":
            actual_low   = float(df_post["low"].min())
            max_favorable = (float(df_post["high"].max()) - entry) / entry * 100
            max_adverse   = (entry - actual_low) / entry * 100
        else:
            actual_high  = float(df_post["high"].max())
            max_favorable = (entry - float(df_post["low"].min())) / entry * 100
            max_adverse   = (actual_high - entry) / entry * 100

        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)

        record = {
            "timestamp":          ts,
            "datetime":           dt.strftime("%Y-%m-%d %H:%M"),
            "year":               dt.year,
            "hour_utc":           dt.hour,
            "direction":          dirn,
            "entry_price":        entry,
            "stop_price":         stop,
            "exit_price":         exit_price,
            "exit_reason":        hit_reason,
            "net_pnl_pct":        net_pnl * 100,
            "win":                win,
            # Pre-move
            "dump_pct":           dump_pct,
            "dump_speed_candles": dump_speed,
            "peak_vol_ratio":     peak_vol_ratio,
            "vol_ratio_last5":    vol_ratio,
            # Trigger candle
            "trig_body_pct":      trig_features["body_pct"],
            "trig_lower_wick":    trig_features["lower_wick_pct"],
            "trig_upper_wick":    trig_features["upper_wick_pct"],
            "trig_close_pos":     trig_features["close_position"],
            "trig_is_bullish":    trig_features["is_bullish"],
            "trig_range_pct":     trig_features["range_pct"],
            # Exhaustion
            "bars_to_exhaustion": exh_features["bars_to_exhaustion"] if exh_features else None,
            "entry_vs_exh_pct":   exh_features["entry_vs_exhaustion"] if exh_features else None,
            "exh_lower_wick":     exh_features["lower_wick_pct"] if exh_features else None,
            "exh_close_pos":      exh_features["close_position"] if exh_features else None,
            "next_candle_bullish":exh_features["next_candle_bullish"] if exh_features else None,
            "next_vol_ratio":     exh_features["next_candle_vol_ratio"] if exh_features else None,
            # Post-move
            "max_favorable_pct":  max_favorable,
            "max_adverse_pct":    max_adverse,
            "mae_vs_stop":        max_adverse / (risk/entry*100) if risk > 0 else 0,
            "optimal_improvement":optimal_improvement,
        }

        if win:
            wins.append(record)
        else:
            losses.append(record)

    sys.stdout.write(f"\r  Processing: {n:,}/{n:,} (100.0%)\n")
    print(f"  Done in {time.time()-t0:.1f}s")
    print(f"  Wins: {len(wins):,}  Losses: {len(losses):,}")
    print()

    wins_df   = pd.DataFrame(wins)
    losses_df = pd.DataFrame(losses)

    wins_df.to_csv(OUTPUT_WINS, index=False)
    losses_df.to_csv(OUTPUT_LOSS, index=False)

    # ── Analysis ──────────────────────────────────────────────────────────────
    lines = []
    lines += ["="*70,
              "  ETH H4 Trade Anatomy — Wins vs Losses",
              f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
              f"  {len(wins):,} wins  |  {len(losses):,} losses  |  RRR={RRR}",
              "="*70]

    def sec(title):
        lines.extend(["", "="*70, f"  {title}", "="*70])
    def sub(title):
        lines.extend(["", f"  ── {title} ──"])
    def compare(label, w_val, l_val, higher_is_better=True):
        edge = "WIN ▲" if (w_val > l_val) == higher_is_better else "LOSS ▲"
        lines.append(f"    {label:<35} wins={w_val:>8.3f}  losses={l_val:>8.3f}  {edge}")

    # ── 1. DUMP CHARACTERISTICS ───────────────────────────────────────────────
    sec("1. DUMP CHARACTERISTICS — WHAT PRECEDED WINS VS LOSSES")

    sub("Move magnitude before entry")
    compare("Dump size (%)",
            wins_df["dump_pct"].mean(), losses_df["dump_pct"].mean(), True)
    compare("Dump speed (candles to low)",
            wins_df["dump_speed_candles"].mean(), losses_df["dump_speed_candles"].mean(), False)

    sub("Volume signature")
    compare("Peak volume ratio (vs 60-bar avg)",
            wins_df["peak_vol_ratio"].mean(), losses_df["peak_vol_ratio"].mean(), True)
    compare("Last 5 candle vol ratio",
            wins_df["vol_ratio_last5"].mean(), losses_df["vol_ratio_last5"].mean(), False)

    lines += ["",
              "  INSIGHT: If peak_vol_ratio is higher for wins, volume spike",
              "  at the move peak is a genuine institutional signature."]

    # ── 2. TRIGGER CANDLE ANATOMY ─────────────────────────────────────────────
    sec("2. TRIGGER CANDLE — THE CANDLE AT ENTRY")

    sub("Candle structure at entry point")
    compare("Body size (% of range)",
            wins_df["trig_body_pct"].mean(), losses_df["trig_body_pct"].mean(), False)
    compare("Lower wick (% of range)",
            wins_df["trig_lower_wick"].mean(), losses_df["trig_lower_wick"].mean(), True)
    compare("Close position (0=low, 1=high)",
            wins_df["trig_close_pos"].mean(), losses_df["trig_close_pos"].mean(), True)
    compare("Range size (% of price)",
            wins_df["trig_range_pct"].mean(), losses_df["trig_range_pct"].mean(), True)

    w_bull = wins_df["trig_is_bullish"].mean() * 100
    l_bull = losses_df["trig_is_bullish"].mean() * 100
    lines.append(f"    {'Bullish trigger candle':<35} wins={w_bull:>7.1f}%  losses={l_bull:>7.1f}%")

    lines += ["",
              "  INSIGHT: A large lower wick and high close position on the trigger",
              "  candle = price already showing reversal. This is the real entry signal."]

    # ── 3. EXHAUSTION CANDLE ──────────────────────────────────────────────────
    sec("3. EXHAUSTION — WHERE PRICE ACTUALLY TURNED")

    w_exh = wins_df.dropna(subset=["bars_to_exhaustion"])
    l_exh = losses_df.dropna(subset=["bars_to_exhaustion"])

    sub("How far we entered vs optimal")
    compare("Bars to actual exhaustion",
            w_exh["bars_to_exhaustion"].mean(), l_exh["bars_to_exhaustion"].mean(), False)
    compare("Entry vs exhaustion price (%)",
            w_exh["entry_vs_exh_pct"].mean(), l_exh["entry_vs_exh_pct"].mean(), False)

    sub("Exhaustion candle features")
    compare("Exhaustion lower wick",
            w_exh["exh_lower_wick"].mean(), l_exh["exh_lower_wick"].mean(), True)
    compare("Exhaustion close position",
            w_exh["exh_close_pos"].mean(), l_exh["exh_close_pos"].mean(), True)

    w_next = w_exh["next_candle_bullish"].mean() * 100
    l_next = l_exh["next_candle_bullish"].mean() * 100
    lines.append(f"    {'Next candle bullish after exh':<35} wins={w_next:>7.1f}%  losses={l_next:>7.1f}%")

    compare("Next candle vol vs exhaustion",
            w_exh["next_vol_ratio"].mean(), l_exh["next_vol_ratio"].mean(), True)

    lines += ["",
              "  CRITICAL INSIGHT: If wins have higher exhaustion lower wick and",
              "  higher close position, waiting for the exhaustion candle to CLOSE",
              "  before entering would dramatically improve win rate."]

    # ── 4. POST-ENTRY BEHAVIOR ────────────────────────────────────────────────
    sec("4. POST-ENTRY BEHAVIOR — MAE AND MFE")

    sub("Maximum adverse excursion (how far against us before reversal)")
    compare("Max adverse move (%)",
            wins_df["max_adverse_pct"].mean(), losses_df["max_adverse_pct"].mean(), False)
    compare("MAE as multiple of stop distance",
            wins_df["mae_vs_stop"].mean(), losses_df["mae_vs_stop"].mean(), False)
    compare("Max favorable move (%)",
            wins_df["max_favorable_pct"].mean(), losses_df["max_favorable_pct"].mean(), True)

    lines += ["",
              "  INSIGHT: If winning trades still go against us before reversing,",
              "  our stop is too tight. Widening stops may convert losses to wins."]

    # ── 5. OPTIMAL ENTRY IMPROVEMENT ─────────────────────────────────────────
    sec("5. OPTIMAL ENTRY — WHAT WAITING WOULD HAVE DONE")

    all_df = pd.concat([wins_df, losses_df])
    all_exh = all_df.dropna(subset=["entry_vs_exh_pct"])

    lines.append(f"    Avg entry was {all_exh['entry_vs_exh_pct'].mean():.2f}% away from optimal")
    lines.append(f"    Wins: entry was {w_exh['entry_vs_exh_pct'].mean():.2f}% from exhaustion")
    lines.append(f"    Losses: entry was {l_exh['entry_vs_exh_pct'].mean():.2f}% from exhaustion")
    lines += ["",
              "  If we entered AT the exhaustion candle instead of immediately:",
              f"    Better stop placement would tighten risk by ~{all_exh['entry_vs_exh_pct'].mean():.1f}%",
              "    More signals would be filtered (exhaustion candle must confirm)"]

    # ── 6. TIME OF DAY BREAKDOWN ──────────────────────────────────────────────
    sec("6. TIME-OF-DAY BREAKDOWN")

    sub("Win rate by hour (UTC)")
    all_df = pd.concat([wins_df, losses_df])
    hour_stats = all_df.groupby("hour_utc").agg(
        n=("win","count"),
        win_rate=("win","mean"),
        avg_pnl=("net_pnl_pct","mean")
    ).sort_values("win_rate", ascending=False)

    lines.append(f"    {'Hour':<8} {'N':>5}  {'Win%':>6}  {'AvgP&L':>8}")
    lines.append(f"    {'─'*32}")
    for hr, row in hour_stats.iterrows():
        marker = " ◄" if row["win_rate"] >= hour_stats["win_rate"].quantile(0.75) else ""
        lines.append(f"    {hr:02d}:00    {row['n']:>4,}  "
                     f"{row['win_rate']*100:>5.1f}%  "
                     f"{row['avg_pnl']:>7.2f}%{marker}")

    # ── 7. NOVEL ENTRY CRITERIA ───────────────────────────────────────────────
    sec("7. DERIVED ENTRY CRITERIA — WHAT THE DATA SUGGESTS")

    # Filter: only take trades where trigger candle has lower wick > 30% of range
    strong_wick = all_df[all_df["trig_lower_wick"] > 0.30]
    weak_wick   = all_df[all_df["trig_lower_wick"] <= 0.30]
    if len(strong_wick) > 50 and len(weak_wick) > 50:
        sub("Filter A: Strong lower wick on trigger candle (>30% of range)")
        lines.append(f"    With strong wick:   n={len(strong_wick):,}  "
                     f"win={strong_wick['win'].mean()*100:.1f}%")
        lines.append(f"    Without:            n={len(weak_wick):,}  "
                     f"win={weak_wick['win'].mean()*100:.1f}%")

    # Filter: high close position
    high_close = all_df[all_df["trig_close_pos"] > 0.50]
    low_close  = all_df[all_df["trig_close_pos"] <= 0.50]
    if len(high_close) > 50 and len(low_close) > 50:
        sub("Filter B: Close in upper half of candle range")
        lines.append(f"    High close pos:     n={len(high_close):,}  "
                     f"win={high_close['win'].mean()*100:.1f}%")
        lines.append(f"    Low close pos:      n={len(low_close):,}  "
                     f"win={low_close['win'].mean()*100:.1f}%")

    # Filter: volume spike
    high_vol = all_df[all_df["peak_vol_ratio"] > 2.0]
    low_vol  = all_df[all_df["peak_vol_ratio"] <= 2.0]
    if len(high_vol) > 50 and len(low_vol) > 50:
        sub("Filter C: High volume spike (>2x average)")
        lines.append(f"    High vol spike:     n={len(high_vol):,}  "
                     f"win={high_vol['win'].mean()*100:.1f}%")
        lines.append(f"    Normal vol:         n={len(low_vol):,}  "
                     f"win={low_vol['win'].mean()*100:.1f}%")

    # Combined filters
    combined = all_df[
        (all_df["trig_lower_wick"] > 0.25) &
        (all_df["trig_close_pos"]  > 0.40) &
        (all_df["peak_vol_ratio"]  > 1.5)
    ]
    sub("Filter COMBINED: Wick + close position + volume")
    lines.append(f"    Combined filter:    n={len(combined):,}  "
                 f"win={combined['win'].mean()*100:.1f}%  "
                 f"(filtered out {len(all_df)-len(combined):,} trades)")
    if len(combined) > 10:
        lines.append(f"    Avg P&L per trade:  {combined['net_pnl_pct'].mean():.2f}%")
        lines.append(f"    Best hour:          {combined.groupby('hour_utc')['win'].mean().idxmax():02d}:00 UTC")

    # ── 8. TOP 20 BEST AND WORST TRADES ──────────────────────────────────────
    sec("8. TOP 20 BEST TRADES — WHAT DID THEY LOOK LIKE?")
    top20 = wins_df.nlargest(20, "net_pnl_pct")
    lines.append(f"    {'Date':<18} {'Dir':<6} {'Entry':>8} {'Exit':>8} "
                 f"{'P&L%':>7} {'DumpPct':>8} {'PeakVol':>8} {'TrigWick':>9}")
    lines.append(f"    {'─'*75}")
    for _, r in top20.iterrows():
        lines.append(f"    {r['datetime']:<18} {r['direction']:<6} "
                     f"{r['entry_price']:>8.2f} {r['exit_price']:>8.2f} "
                     f"{r['net_pnl_pct']:>7.2f}% {r['dump_pct']:>7.2f}% "
                     f"{r['peak_vol_ratio']:>7.2f}x {r['trig_lower_wick']:>8.2f}")

    sec("9. TOP 20 WORST TRADES — WHAT WENT WRONG?")
    bot20 = losses_df.nsmallest(20, "net_pnl_pct")
    lines.append(f"    {'Date':<18} {'Dir':<6} {'Entry':>8} {'Exit':>8} "
                 f"{'P&L%':>7} {'DumpPct':>8} {'PeakVol':>8} {'TrigWick':>9}")
    lines.append(f"    {'─'*75}")
    for _, r in bot20.iterrows():
        lines.append(f"    {r['datetime']:<18} {r['direction']:<6} "
                     f"{r['entry_price']:>8.2f} {r['exit_price']:>8.2f} "
                     f"{r['net_pnl_pct']:>7.2f}% {r['dump_pct']:>7.2f}% "
                     f"{r['peak_vol_ratio']:>7.2f}x {r['trig_lower_wick']:>8.2f}")

    lines += ["", "="*70,
              f"  Complete. {time.time()-t0:.1f}s",
              f"  Saved: {OUTPUT_TXT}",
              f"  CSVs: {OUTPUT_WINS.name}  {OUTPUT_LOSS.name}",
              "="*70]

    output = "\n".join(lines)
    print(output)
    OUTPUT_TXT.write_text(output)
    print(f"\n  Saved: {OUTPUT_TXT}\n")


if __name__ == "__main__":
    main()
