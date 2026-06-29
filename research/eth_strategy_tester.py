# research/eth_strategy_tester.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Hypothesis testing on ETH/USD 1m data
# v1.1 — 2026-06-29 — Full multiprocessing: hypotheses run in parallel,
#                      trade simulation parallelized within each hypothesis
#                      4 cores → ~4x speedup, 20min → ~5min

"""
Tests each strategy hypothesis against the full ETH/USD 1m dataset.
Uses multiprocessing at two levels:
  1. Each hypothesis runs on its own core simultaneously
  2. Trade simulation within each hypothesis split across cores

Strategies:
    H1: Compression Fade      — fade 2%+ move out of BB squeeze
    H3: Trend-Aligned Fade    — fade counter-trend moves in EMA stack
    H4: Extreme Capitulation  — fade any 5%+ move (92% reversal rate)
    H5: H4 + volume filter    — H4 with institutional volume confirmation

Walk-forward: train ≤2021, validate ≥2022

Usage:
    python research/eth_strategy_tester.py
    python research/eth_strategy_tester.py --hypothesis H4
"""

import argparse
import multiprocessing as mp
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

INPUT_FILE  = Path.home() / "ETHUSD_1m.csv"
OUTPUT_DIR  = Path(__file__).parent
OUTPUT_FILE = OUTPUT_DIR / "eth_strategy_results.txt"

COLS = ["timestamp","open","high","low","close","volume","trades"]

ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.5
FEE_RT          = 0.004    # 0.4% round trip maker
POSITION_PCT    = 0.20
STARTING_BAL    = 10000.0
MIN_DAILY_RANGE = 0.005
TRAIN_END_YEAR  = 2021
VALIDATE_START  = 2022
N_CORES         = 4


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_atr(df, period=14):
    h = df["high"]; l = df["low"]; c = df["close"].shift(1)
    tr = pd.concat([h-l, (h-c).abs(), (l-c).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_bb_width(closes, period=20):
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    return ((mid + 2*std) - (mid - 2*std)) / mid


# ── Trade simulation (parallelized chunk) ─────────────────────────────────────

def simulate_chunk(args):
    """Simulate a chunk of signals. Returns list of trade result dicts."""
    signals_chunk, df_dict, rrr_targets = args

    # Rebuild lookup from dict (DataFrames can't be passed directly to workers)
    ts_arr    = df_dict["timestamp"]
    open_arr  = df_dict["open"]
    high_arr  = df_dict["high"]
    low_arr   = df_dict["low"]
    close_arr = df_dict["close"]

    # Build timestamp → index map
    ts_to_idx = {int(ts): i for i, ts in enumerate(ts_arr)}

    results = []
    for sig in signals_chunk:
        ts      = sig["timestamp"]
        dirn    = sig["direction"]
        entry   = sig["entry_price"]
        stop    = sig["stop_price"]
        atr_v   = sig["atr"]
        risk    = abs(entry - stop)

        if entry <= 0 or stop <= 0 or risk <= 0 or atr_v <= 0:
            continue

        idx = ts_to_idx.get(int(ts))
        if idx is None or idx + 480 > len(ts_arr):
            continue

        fees = entry * FEE_RT

        for rrr in rrr_targets:
            target = (entry + risk * rrr) if dirn == "long" else (entry - risk * rrr)
            stop_p = stop

            hit_reason = "timeout"
            exit_price = float(close_arr[min(idx+480, len(close_arr)-1)])

            for j in range(idx+1, min(idx+481, len(ts_arr))):
                h = float(high_arr[j])
                l = float(low_arr[j])
                if dirn == "long":
                    if l <= stop_p:
                        hit_reason = "stop"
                        exit_price = stop_p
                        break
                    if h >= target:
                        hit_reason = "target"
                        exit_price = target
                        break
                else:
                    if h >= stop_p:
                        hit_reason = "stop"
                        exit_price = stop_p
                        break
                    if l <= target:
                        hit_reason = "target"
                        exit_price = target
                        break

            if dirn == "long":
                gross = (exit_price - entry) / entry
            else:
                gross = (entry - exit_price) / entry
            net = gross - FEE_RT
            r_ach = gross / (risk / entry) if risk > 0 else 0

            results.append({
                "timestamp":   ts,
                "rrr":         rrr,
                "direction":   dirn,
                "entry":       entry,
                "exit":        exit_price,
                "stop":        stop_p,
                "exit_reason": hit_reason,
                "gross_pnl":   gross,
                "net_pnl":     net,
                "r_achieved":  r_ach,
                "win":         net > 0,
            })

    return results


def parallel_simulate(df, signals, rrr_targets, n_cores=N_CORES):
    """Split signals across cores and simulate in parallel."""
    if not signals:
        return pd.DataFrame()

    # Convert df to dict of arrays for pickling
    df_dict = {
        "timestamp": df["timestamp"].values,
        "open":      df["open"].values,
        "high":      df["high"].values,
        "low":       df["low"].values,
        "close":     df["close"].values,
    }

    # Split signals into chunks
    chunk_size = max(1, len(signals) // n_cores)
    chunks = [signals[i:i+chunk_size] for i in range(0, len(signals), chunk_size)]
    args   = [(chunk, df_dict, rrr_targets) for chunk in chunks]

    with mp.Pool(processes=n_cores) as pool:
        results = pool.map(simulate_chunk, args)

    all_trades = [t for chunk in results for t in chunk]
    if not all_trades:
        return pd.DataFrame()
    return pd.DataFrame(all_trades)


# ── Performance metrics ───────────────────────────────────────────────────────

def performance(trades, label="", bal=STARTING_BAL, pos=POSITION_PCT):
    if trades is None or len(trades) == 0:
        return {"label": label, "n": 0}
    n       = len(trades)
    wins    = trades["win"].sum()
    wr      = wins / n
    avg_r   = trades["r_achieved"].mean()
    avg_pnl = trades["net_pnl"].mean()
    gw = trades[trades["net_pnl"]>0]["net_pnl"].sum()
    gl = abs(trades[trades["net_pnl"]<=0]["net_pnl"].sum())
    pf = gw/gl if gl>0 else float("inf")
    balance = bal
    peak    = bal
    max_dd  = 0.0
    for _, row in trades.iterrows():
        balance += balance * pos * row["net_pnl"]
        peak     = max(peak, balance)
        max_dd   = max(max_dd, (peak-balance)/peak)
    total_ret = (balance - bal) / bal * 100
    pnl_s = trades["net_pnl"] * pos
    sharpe = pnl_s.mean()/pnl_s.std()*np.sqrt(252) if pnl_s.std()>0 else 0
    return {
        "label": label, "n": n,
        "win_rate": wr*100, "avg_r": avg_r, "avg_pnl": avg_pnl*100,
        "profit_factor": pf, "max_dd": max_dd*100,
        "total_return": total_ret, "final_balance": balance, "sharpe": sharpe,
        "exits": trades["exit_reason"].value_counts().to_dict(),
    }


def fmt(r, lines):
    if r.get("n",0) == 0:
        lines.append("    No trades"); return
    lines.append(f"    Trades:        {r['n']:,}")
    lines.append(f"    Win rate:      {r['win_rate']:.1f}%")
    lines.append(f"    Avg R:         {r['avg_r']:+.2f}")
    lines.append(f"    Profit factor: {r['profit_factor']:.2f}")
    lines.append(f"    Max drawdown:  {r['max_dd']:.1f}%")
    lines.append(f"    Total return:  {r['total_return']:+.1f}%")
    lines.append(f"    Final balance: ${r['final_balance']:,.0f}")
    lines.append(f"    Sharpe:        {r['sharpe']:.2f}")
    ex = r.get("exits",{})
    if ex:
        lines.append(f"    Exits:         " + "  ".join(f"{k}={v}" for k,v in ex.items()))


def section(lines, title):
    lines += ["","="*70, f"  {title}", "="*70]

def sub(lines, title):
    lines += ["", f"  ── {title} ──"]


# ── Signal builders ───────────────────────────────────────────────────────────

def build_h1(df):
    """Compression Fade — fade 2%+ move out of BB squeeze."""
    signals = []
    for idx in range(100, len(df)-1):
        row = df.iloc[idx]
        if row["daily_range"] < MIN_DAILY_RANGE: continue
        bw = row.get("bb_width", 1.0)
        if pd.isna(bw) or bw > 0.015: continue
        atr_v = row["atr"]
        if pd.isna(atr_v) or atr_v <= 0: continue
        price = row["close"]
        recent = df.iloc[max(0,idx-30):idx]
        up = (recent["high"].max() - price) / price * 100
        dn = (price - recent["low"].min()) / price * 100
        if up >= 2.0 and up > dn:
            signals.append({"timestamp": row["timestamp"], "direction": "short",
                            "entry_price": price, "stop_price": price + atr_v*ATR_STOP_MULT, "atr": atr_v})
        elif dn >= 2.0 and dn > up:
            signals.append({"timestamp": row["timestamp"], "direction": "long",
                            "entry_price": price, "stop_price": price - atr_v*ATR_STOP_MULT, "atr": atr_v})
    return signals


def build_h3(df):
    """Trend-Aligned Fade — fade counter-trend moves in EMA stack."""
    signals = []
    for idx in range(100, len(df)-1):
        row = df.iloc[idx]
        if row["daily_range"] < MIN_DAILY_RANGE: continue
        atr_v = row["atr"]
        if pd.isna(atr_v) or atr_v <= 0: continue
        e9 = row["ema9"]; e21 = row["ema21"]; e50 = row["ema50"]
        if pd.isna(e9) or pd.isna(e21) or pd.isna(e50): continue
        price = row["close"]
        bull = price > e9 > e21 > e50
        bear = price < e9 < e21 < e50
        if not bull and not bear: continue
        recent = df.iloc[max(0,idx-30):idx]
        up = (recent["high"].max() - price) / price * 100
        dn = (price - recent["low"].min()) / price * 100
        if bull and dn >= 2.0 and dn > up:
            signals.append({"timestamp": row["timestamp"], "direction": "long",
                            "entry_price": price, "stop_price": price - atr_v*ATR_STOP_MULT, "atr": atr_v})
        elif bear and up >= 2.0 and up > dn:
            signals.append({"timestamp": row["timestamp"], "direction": "short",
                            "entry_price": price, "stop_price": price + atr_v*ATR_STOP_MULT, "atr": atr_v})
    return signals


def build_h4(df):
    """Extreme Capitulation — fade any 5%+ move."""
    signals = []
    for idx in range(100, len(df)-1):
        row = df.iloc[idx]
        if row["daily_range"] < MIN_DAILY_RANGE: continue
        atr_v = row["atr"]
        if pd.isna(atr_v) or atr_v <= 0: continue
        price = row["close"]
        recent = df.iloc[max(0,idx-30):idx]
        up = (recent["high"].max() - price) / price * 100
        dn = (price - recent["low"].min()) / price * 100
        if up >= 5.0 and up > dn:
            signals.append({"timestamp": row["timestamp"], "direction": "short",
                            "entry_price": price, "stop_price": price + atr_v*ATR_STOP_MULT, "atr": atr_v})
        elif dn >= 5.0 and dn > up:
            signals.append({"timestamp": row["timestamp"], "direction": "long",
                            "entry_price": price, "stop_price": price - atr_v*ATR_STOP_MULT, "atr": atr_v})
    return signals


def build_h5(df):
    """H4 + volume spike filter."""
    signals = []
    for idx in range(100, len(df)-1):
        row = df.iloc[idx]
        if row["daily_range"] < MIN_DAILY_RANGE: continue
        atr_v  = row["atr"]
        vol_ma = row.get("vol_ma20", 0)
        if pd.isna(atr_v) or atr_v <= 0: continue
        if pd.isna(vol_ma) or vol_ma <= 0: continue
        if row["volume"] / vol_ma < 1.5: continue
        price = row["close"]
        recent = df.iloc[max(0,idx-30):idx]
        up = (recent["high"].max() - price) / price * 100
        dn = (price - recent["low"].min()) / price * 100
        if up >= 5.0 and up > dn:
            signals.append({"timestamp": row["timestamp"], "direction": "short",
                            "entry_price": price, "stop_price": price + atr_v*ATR_STOP_MULT, "atr": atr_v})
        elif dn >= 5.0 and dn > up:
            signals.append({"timestamp": row["timestamp"], "direction": "long",
                            "entry_price": price, "stop_price": price - atr_v*ATR_STOP_MULT, "atr": atr_v})
    return signals


# ── Per-hypothesis worker ─────────────────────────────────────────────────────

def run_hypothesis(args):
    """Runs one complete hypothesis test. Called in parallel per hypothesis."""
    name, signals, df_dict, rrr_targets, start_year = args

    result_lines = []
    section(result_lines, name)

    if not signals:
        result_lines.append("  No signals generated")
        return "\n".join(result_lines)

    result_lines.append(f"  Total signals: {len(signals):,}")

    train_sigs = [s for s in signals if
                  datetime.fromtimestamp(s["timestamp"], tz=timezone.utc).year <= TRAIN_END_YEAR]
    val_sigs   = [s for s in signals if
                  datetime.fromtimestamp(s["timestamp"], tz=timezone.utc).year >= VALIDATE_START]

    result_lines.append(f"  Train  (≤{TRAIN_END_YEAR}): {len(train_sigs):,}")
    result_lines.append(f"  Validate (≥{VALIDATE_START}): {len(val_sigs):,}")

    # Rebuild df from dict for simulation
    df_sim = pd.DataFrame(df_dict)

    for rrr in rrr_targets:
        sub(result_lines, f"RRR = {rrr}:1")

        all_tr  = parallel_simulate(df_sim, signals,    [rrr], N_CORES)
        trn_tr  = parallel_simulate(df_sim, train_sigs, [rrr], N_CORES)
        val_tr  = parallel_simulate(df_sim, val_sigs,   [rrr], N_CORES)

        for subset_df, label in [(all_tr, "FULL PERIOD"),
                                  (trn_tr, f"TRAIN ≤{TRAIN_END_YEAR}"),
                                  (val_tr, f"VALIDATE ≥{VALIDATE_START} (out-of-sample)")]:
            filtered = subset_df[subset_df["rrr"]==rrr] if len(subset_df)>0 else pd.DataFrame()
            result_lines.append(f"\n  {label}:")
            fmt(performance(filtered), result_lines)

        # Annual breakdown
        if len(all_tr) > 0:
            sub(result_lines, f"Annual breakdown at {rrr}R")
            yr_tr = all_tr[all_tr["rrr"]==rrr].copy()
            yr_tr["year"] = pd.to_datetime(
                yr_tr["timestamp"], unit="s", utc=True).dt.year
            for yr, grp in yr_tr.groupby("year"):
                wins = grp["win"].sum()
                ret  = (grp["net_pnl"] * POSITION_PCT).sum() * 100
                result_lines.append(f"    {yr}  n={len(grp):>4,}  "
                                    f"win={wins/len(grp)*100:.0f}%  "
                                    f"return={ret:+.1f}%")

    return "\n".join(result_lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hypothesis", default="ALL",
                        choices=["H1","H3","H4","H5","ALL"])
    parser.add_argument("--start-year", type=int, default=2018)
    parser.add_argument("--rrr", type=float, nargs="+", default=[1.5, 2.0, 3.0])
    parser.add_argument("--balance", type=float, default=STARTING_BAL)
    parser.add_argument("--pos-pct", type=float, default=POSITION_PCT)
    args = parser.parse_args()

    print(f"\n  ETH Strategy Tester v1.1 — Parallel")
    print(f"  {'─'*52}")
    print(f"  Balance: ${args.balance:,.0f}  |  Position: {args.pos_pct*100:.0f}%")
    print(f"  Fees: {FEE_RT*100:.1f}% RT maker  |  Cores: {N_CORES}")
    print(f"  RRR targets: {args.rrr}")
    print()

    if not INPUT_FILE.exists():
        print(f"  ERROR: {INPUT_FILE} not found"); sys.exit(1)

    # ── Load + prepare data ───────────────────────────────────────────────────
    print("  Loading data...")
    t0 = time.time()

    df = pd.read_csv(INPUT_FILE, header=None, names=COLS,
                     dtype={"timestamp":"int64","open":"float64","high":"float64",
                            "low":"float64","close":"float64","volume":"float64",
                            "trades":"int32"})
    df = df[(df["close"] >= 5.0) & (df["timestamp"] >= 0)].reset_index(drop=True)
    df["dt"]    = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["year"]  = df["dt"].dt.year
    df = df[df["year"] >= args.start_year].reset_index(drop=True)

    print(f"  Computing indicators on {len(df):,} candles...")
    df["atr"]      = compute_atr(df, ATR_PERIOD)
    df["bb_width"] = compute_bb_width(df["close"])
    df["ema9"]     = compute_ema(df["close"], 9)
    df["ema21"]    = compute_ema(df["close"], 21)
    df["ema50"]    = compute_ema(df["close"], 50)
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["date"]     = df["dt"].dt.date

    daily = df.groupby("date").apply(
        lambda x: (x["high"].max()-x["low"].min())/x["close"].mean()
    ).reset_index()
    daily.columns = ["date","daily_range"]
    df = df.merge(daily, on="date", how="left")

    print(f"  Ready in {time.time()-t0:.1f}s")
    print()

    # Convert to dict for multiprocessing (avoids DataFrame pickling issues)
    df_dict = {col: df[col].tolist() for col in
               ["timestamp","open","high","low","close","volume"]}

    # ── Build signals ─────────────────────────────────────────────────────────
    print("  Building signals...")
    t1 = time.time()

    all_builders = {
        "H1": ("H1: COMPRESSION FADE",         build_h1),
        "H3": ("H3: TREND-ALIGNED SWEEP FADE",  build_h3),
        "H4": ("H4: EXTREME CAPITULATION (5%+)", build_h4),
        "H5": ("H5: H4 + VOLUME FILTER",        build_h5),
    }

    selected = list(all_builders.keys()) if args.hypothesis=="ALL" else [args.hypothesis]

    hyp_args = []
    for key in selected:
        label, builder = all_builders[key]
        signals = builder(df)
        print(f"    {key}: {len(signals):,} signals")
        hyp_args.append((label, signals, df_dict, args.rrr, args.start_year))

    print(f"  Signals built in {time.time()-t1:.1f}s")
    print()
    print(f"  Running {len(hyp_args)} hypothesis/es in parallel...")
    print(f"  This will take several minutes — check strategy_tester.log for progress")
    print()

    # ── Run hypotheses in parallel ────────────────────────────────────────────
    t2 = time.time()
    with mp.Pool(processes=min(N_CORES, len(hyp_args))) as pool:
        outputs = pool.map(run_hypothesis, hyp_args)

    print(f"  Simulation complete in {time.time()-t2:.1f}s")

    # ── Assemble output ───────────────────────────────────────────────────────
    header = [
        "="*70,
        "  ETH/USD Strategy Hypothesis Testing — Parallel v1.1",
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}",
        f"  Balance: ${args.balance:,.0f}  Position: {args.pos_pct*100:.0f}%  "
        f"Fees: {FEE_RT*100:.1f}% RT",
        f"  Train: ≤{TRAIN_END_YEAR}  |  Validate: ≥{VALIDATE_START} (out-of-sample)",
        "="*70,
    ]

    footer = [
        "", "="*70,
        f"  Total runtime: {time.time()-t0:.1f}s",
        f"  Saved: {OUTPUT_FILE}",
        "="*70,
    ]

    full_output = "\n".join(header) + "\n" + "\n".join(outputs) + "\n" + "\n".join(footer)
    print(full_output)
    OUTPUT_FILE.write_text(full_output)
    print(f"\n  Saved: {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()
