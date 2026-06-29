# diag_volstate.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Diagnostic: inspect VolatilityState fields and live bot's TrendState source

"""
Inspects what fields VolatilityState has, whether TrendState is on it,
and where TrendState actually comes from in the live bot.

Usage:
    python diag_volstate.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "crypto_trader"))

print("\n=== VolatilityState Field Diagnostic ===\n")

# ── VolatilityState fields ────────────────────────────────────────────────────
try:
    from analysis.volatility_engine import VolatilityState, get_volatility_engine
    fields = list(VolatilityState.__dataclass_fields__.keys())
    print(f"VolatilityState fields ({len(fields)}):")
    for f in fields:
        print(f"  {f}")
except Exception as e:
    print(f"ERROR importing VolatilityState: {e}")

print()

# ── Check if TrendState is on vol_state ───────────────────────────────────────
print("Does VolatilityState have 'trend_state'?",
      "YES" if "trend_state" in fields else "NO")

print()

# ── Find TrendState in codebase ───────────────────────────────────────────────
print("Searching for TrendState in crypto_trader/analysis/...")
ct_dir = Path(__file__).parent / "crypto_trader" / "analysis"
for py in sorted(ct_dir.glob("*.py")):
    content = py.read_text(errors="ignore")
    if "TrendState" in content or "trend_state" in content:
        lines = [l.strip() for l in content.splitlines()
                 if "TrendState" in l or ("trend_state" in l and "def " in l)]
        print(f"\n  {py.name}:")
        for l in lines[:5]:
            print(f"    {l}")

print()

# ── Check what regime_classifier.classify() expects ──────────────────────────
print("regime_classifier.classify() signature:")
try:
    from analysis.regime_classifier import RegimeClassifier
    import inspect
    sig = inspect.signature(RegimeClassifier.classify)
    for name, param in sig.parameters.items():
        if name != "self":
            print(f"  {name}: {param.annotation}")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n=== Done ===\n")
