# backtester_engine_v1 — Vertigo Capital

**BTC/USD | Kraken Historical | Regime-Aware | Parameter Optimizer | Walk-Forward | Monte Carlo**

Professional research framework for backtesting and optimizing `crypto_trader_v6` strategy logic against historical Kraken OHLCV data. Replays candle-by-candle through the real strategy stack — same regime classifier, same entry gates, same risk rules — with full fee simulation, equity tracking, and HTML reporting.

---

## Architecture

### Replay Pipeline

```
Historical 1m OHLCV (Kraken via CCXT)
│
├─ data_fetcher.py      Pull + cache by quarter (90-day chunks)
├─ replay.py            Sequential candle feed → rolling multi-TF windows
│                         VWAP reset at midnight UTC
│                         Stop / trail / partial evaluated on candle high/low
│                         Cash balance updates live with each closed trade
├─ simulated_broker.py  Fill engine: 0.1% slippage + Kraken fees
│                         Taker: 0.80% | Margin open: 0.02%
│                         Fee floor gate: 1R profit must exceed round-trip fees
└─ backtest_logger.py   SQLite: runs / trades / equity tables
```

### Strategy Stack (imported from crypto_trader_v6)

| Regime | Strategy |
|--------|----------|
| TRENDING_BULL / TRENDING_BEAR | MomentumStrategy |
| COMPRESSION | CompressionScalp |
| SWEEP_REVERSAL | SweepReversalStrategy |
| RANGING | MeanReversion |

### Position Management (replayed exactly as live)

- **ADX stop multiplier** applied before `compute_size` (preserves dollar risk)
  - ADX < 25: 1.0× | 25–40: 1.5× | 40–60: 2.0× | > 60: 2.5×
- **Partial exit** at 0.75R (50% of position)
- **Trail** activates at 1.0R, steps at 1R / 2R / 3R
- **Stagnant timeout** force-exits at 120 minutes
- **VWAP gate** hard block (Sweep Reversal bypasses)
- **Whipsaw guard** 15-minute block after stop hit
- **Entry cooldown** 5 minutes after any entry

---

## Key Tunable Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `starting_balance_usd` | 1000 | Session starting capital (user-defined) |
| `leverage` | 10 | Kraken margin multiplier |
| `grade_a_notional_pct` | 0.90 | Grade A position size (% of buying power) |
| `grade_b_notional_pct` | 0.75 | Grade B position size |
| `min_rrr` | 1.5 | Minimum risk:reward to take trade |
| `atr_stop_multiplier` | 1.5 | ATR stop distance multiplier |
| `adx_trend_threshold` | 20 | ADX threshold for TRENDING regime |
| `bb_width_compression_pct` | 0.20 | BB width for COMPRESSION detection |
| `trail_activation_r` | 1.0 | R multiple to activate trailing stop |
| `partial_minimum_r` | 0.75 | R multiple to trigger partial exit |
| `stagnant_trade_minutes` | 120 | Force-exit timer |
| `entry_cooldown_minutes` | 5 | Post-entry cooldown |
| `vwap_filter_active` | True | Hard VWAP gate on/off |

---

## Repo Structure

```
backtester_engine_v1/
├── main.py                        # Entry point — interactive session runner
├── config.py                      # All defaults and paths
├── install.sh                     # EC2 web installer (one-command deploy)
├── requirements.txt
├── README.md
├── .gitignore
├── .gitattributes
│
├── backtest/
│   ├── data_fetcher.py            # Pull + cache Kraken OHLCV by quarter
│   ├── replay.py                  # Candle replay engine + ReplayConfig
│   ├── simulated_broker.py        # Fill simulation with slippage + fees
│   └── backtest_logger.py         # SQLite logging (runs / trades / equity)
│
├── optimizer/
│   ├── parameter_grid.py          # Define parameter sweep ranges
│   ├── optimizer.py               # Grid search, rank by metric
│   └── walk_forward.py            # In-sample optimize, out-of-sample validate
│
├── analysis/
│   └── monte_carlo.py             # Randomize trade sequences, estimate drawdown
│
├── reports/
│   └── html_report.py             # Self-contained dark-theme HTML report
│
└── crypto_trader/                 # Strategy files copied from crypto_trader_v6
    ├── strategy_bundle.py         # Adapter wrapping live strategy stack
    ├── regime_classifier.py
    ├── volatility_engine.py
    ├── liquidity_mapper.py
    ├── structure_analyzer.py
    ├── momentum_strategy.py
    ├── compression_scalp_strategy.py
    ├── sweep_reversal_strategy.py
    └── risk_manager.py
```

---

## Deployment

### EC2 — One Command

SSH into a fresh EC2 (Ubuntu) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/TX-9AI/backtester_engine_v1/main/install.sh -o install.sh && bash install.sh
```

The installer will:
1. Prompt for default starting balance
2. Prompt for GitHub token (optional, for pushing results)
3. Install system packages
4. Clone repo and install files
5. Create Python venv and install dependencies
6. Patch `config.py` with your starting balance
7. Initialize git remote

### Local — Windows

```
1. Unpack tarball to C:\backtester_engine_v1\
2. SCP files to EC2 manually or use install.sh via Terminus
```

---

## Running a Backtest

### Interactive (recommended)

```bash
python main.py
```

Prompts for starting balance → quarter selection → runs replay → saves to DB → generates HTML report automatically.

### CLI flags

```bash
python main.py --quarter 2025-Q3              # Single quarter
python main.py --quarter 2024-Q1:2024-Q4      # Range (coming: range flag)
python main.py --all-quarters                 # Full history
python main.py --balance 10000                # Override starting balance
python main.py --balance 10000 --quarter 2025-Q2
```

### Report generation

```bash
python reports/html_report.py --run-id 1      # Specific run
python reports/html_report.py --latest        # Most recent run
python reports/html_report.py --compare 1,2,3 # Side-by-side comparison
```

Reports save to `reports/output/backtest_run{N}_{quarter}.html`.

### Query results

```bash
python -c "
from backtest.backtest_logger import BacktestLogger
import config as cfg
bl = BacktestLogger(cfg.RESULTS_DB)
print(bl.get_runs().to_string())
"
```

---

## Report Contents

Each HTML report includes:

- **Equity curve** — balance over time with start-balance reference line
- **Drawdown chart** — % drawdown from peak, continuously
- **R distribution** — bar chart of trade outcomes by R bucket
- **Parameter snapshot** — full `ReplayConfig` snapshot for the run
- **Breakdowns** — by strategy / regime / grade / exit reason / direction
- **Trade log** — every trade with entry, exit, fees, R, duration, flags

---

## Data

- Source: Kraken BTC/USD 1-minute OHLCV via CCXT
- Cache: `data/cache/BTC_USD_1m_YYYY-QN.csv` per quarter
- Coverage: 2020-Q1 through last completed quarter
- Quarters are fetched on demand and cached — subsequent runs are instant

**Cache and results DB are gitignored.** They live on the EC2 only.

---

## Security

- No credentials stored in files
- No tokens in source code
- GitHub token passed at install time only — not persisted to disk
- `.gitignore` blocks: `credentials.py`, `*.pem`, `*.key`, `*.env`, `data/`, `reports/output/`
- All sensitive values injected as EC2 environment variables via systemd (bot) or prompted at session start (backtester)

---

## Version Control Standard

Every file header:

```python
# filename.py — backtester_engine_v1
# v1.0 — YYYY-MM-DD — Initial description
# v1.1 — YYYY-MM-DD — What changed and why
```

Every change to any file = version increment. No exceptions. This ensures unambiguous identification of deployed vs. GitHub versions during debugging.

---

## Related Projects

- [`crypto_trader_v6`](https://github.com/TX-9AI/crypto_trader_v6) — Live BTC/USD trading bot whose strategy this backtester replays
- `options_trader_v2` — QQQ/SPX 0DTE options bot (separate repo)

---

## Session Notes — June 28, 2026

- v1.0 initial build: data_fetcher, replay engine, simulated broker, backtest logger, config, main, install.sh, html_report
- Strategy bundle (crypto_trader/ adapter) and optimizer to be completed next session
- Walk-forward, Monte Carlo, and PDF report are stubbed — next build
