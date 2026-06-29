# research/save_signals.py — backtester_engine_v1
# v1.0 — 2026-06-29 — Emergency: rebuild and save H4 signals to cache
#                      Run this once to avoid the 14-minute rescan

import json, sys, time
import pandas as pd
from pathlib import Path

INPUT  = Path.home() / "ETHUSD_1m.csv"
OUTPUT = Path(__file__).parent / "signals_H4_2018.json"
COLS   = ["timestamp","open","high","low","close","volume","trades"]

print("\n  Rebuilding H4 signals and saving to cache...")
t0 = time.time()

df = pd.read_csv(INPUT, header=None, names=COLS,
                 dtype={"timestamp":"int64","open":"float64","high":"float64",
                        "low":"float64","close":"float64","volume":"float64","trades":"int32"})
df = df[(df["close"] >= 5.0) & (df["timestamp"] >= 0)].reset_index(drop=True)
df["dt"]   = pd.to_datetime(df["timestamp"], unit="s", utc=True)
df["year"] = df["dt"].dt.year
df = df[df["year"] >= 2018].reset_index(drop=True)

# ATR
h = df["high"]; l = df["low"]; c = df["close"].shift(1)
tr = pd.concat([h-l,(h-c).abs(),(l-c).abs()],axis=1).max(axis=1)
df["atr"] = tr.rolling(14).mean()

# Daily range
df["date"] = df["dt"].dt.date
daily = df.groupby("date").apply(
    lambda x: (x["high"].max()-x["low"].min())/x["close"].mean()
).reset_index()
daily.columns = ["date","daily_range"]
df = df.merge(daily, on="date", how="left")

n = len(df)
signals = []
for idx in range(100, n-1):
    if idx % 100000 == 0:
        sys.stdout.write(f"\r  Scanning: {idx:,}/{n:,} ({idx/n*100:.1f}%)  signals={len(signals):,}")
        sys.stdout.flush()
    row = df.iloc[idx]
    if row["daily_range"] < 0.005: continue
    atr_v = row["atr"]
    if pd.isna(atr_v) or atr_v <= 0: continue
    price = float(row["close"])
    recent = df.iloc[max(0,idx-30):idx]
    up = (float(recent["high"].max()) - price) / price * 100
    dn = (price - float(recent["low"].min())) / price * 100
    if up >= 5.0 and up > dn:
        signals.append({"timestamp": int(row["timestamp"]), "direction": "short",
                        "entry_price": price, "stop_price": float(price + atr_v*1.5), "atr": float(atr_v)})
    elif dn >= 5.0 and dn > up:
        signals.append({"timestamp": int(row["timestamp"]), "direction": "long",
                        "entry_price": price, "stop_price": float(price - atr_v*1.5), "atr": float(atr_v)})

sys.stdout.write(f"\r  Scanning: {n:,}/{n:,} (100.0%)  signals={len(signals):,}\n")
print(f"  Done in {time.time()-t0:.1f}s")
print(f"  Saving {len(signals):,} signals to {OUTPUT}...")
with open(OUTPUT, "w") as f:
    json.dump(signals, f)
print(f"  Saved. Run eth_strategy_tester.py --hypothesis H4 to simulate.\n")
