# reports/html_report.py — backtester_engine_v1
# v1.0 — 2026-06-28 — Self-contained HTML backtest report with equity curve,
#                      drawdown, parameter snapshot, and full trade breakdown
# v1.1 — 2026-06-28 — Import bt_config instead of config to avoid shadowing crypto_trader/config.py

"""
Generates a self-contained HTML report for a completed backtest run.

Usage:
    python reports/html_report.py --run-id 3
    python reports/html_report.py --run-id 3 --out reports/output/my_report.html
    python reports/html_report.py --latest
    python reports/html_report.py --compare 1,2,3

Output: dark-themed HTML file matching the live bot's report.py aesthetic.
Reads from: data/backtest_results.db
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from backtest.backtest_logger import BacktestLogger
import bt_config as cfg

# ─── COLOUR / FORMAT HELPERS (matching report.py style) ──────────────────────

def pct_color(val):
    if val is None: return "#888"
    return "#00c97a" if val >= 0 else "#ff4d6d"

def r_badge(r):
    if r is None: return "—"
    color = "#00c97a" if r >= 0 else "#ff4d6d"
    return f'<span style="color:{color};font-weight:600">{r:+.2f}R</span>'

def pnl_cell(val):
    if val is None: return "—"
    color = "#00c97a" if val >= 0 else "#ff4d6d"
    sign  = "+" if val >= 0 else ""
    return f'<span style="color:{color}">{sign}${val:,.2f}</span>'

def grade_badge(g):
    colors = {"A": "#f5a623", "B": "#4a90e2", "C": "#888"}
    c = colors.get(str(g).upper(), "#555")
    return f'<span style="background:{c};color:#fff;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">{g}</span>'

def stat_box(label, value, sub=""):
    sub_html = f"<div class='stat-sub'>{sub}</div>" if sub else ""
    return f'''<div class="stat-box">
        <div class="stat-label">{label}</div>
        <div class="stat-value">{value}</div>
        {sub_html}
    </div>'''

def regime_color(regime):
    colors = {
        "TRENDING_BULL":  "#00c97a",
        "TRENDING_BEAR":  "#ff4d6d",
        "COMPRESSION":    "#f5a623",
        "SWEEP_REVERSAL": "#a78bfa",
        "RANGING":        "#888",
    }
    return colors.get(str(regime).upper(), "#888")

def exit_color(reason):
    colors = {
        "target":          "#00c97a",
        "stop_loss":       "#ff4d6d",
        "trail":           "#4a90e2",
        "stagnant_timeout":"#f5a623",
        "partial":         "#a78bfa",
        "end_of_data":     "#888",
    }
    return colors.get(str(reason).lower(), "#888")

# ─── ANALYTICS ────────────────────────────────────────────────────────────────

def compute_report_stats(trades_df: pd.DataFrame, equity_df: pd.DataFrame, run_meta: dict) -> dict:
    if trades_df.empty:
        return {}

    df = trades_df.copy()
    df["net_pnl"]    = pd.to_numeric(df["net_pnl"],    errors="coerce").fillna(0)
    df["r_achieved"] = pd.to_numeric(df["r_achieved"], errors="coerce").fillna(0)
    df["fees"]       = pd.to_numeric(df["fees"],       errors="coerce").fillna(0)

    wins   = df[df["net_pnl"] > 0]
    losses = df[df["net_pnl"] <= 0]
    total  = len(df)

    win_rate      = len(wins) / total if total else 0
    avg_r         = df["r_achieved"].mean()
    gross_profit  = wins["net_pnl"].sum()
    gross_loss    = abs(losses["net_pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    total_fees    = df["fees"].sum()
    net_pnl       = df["net_pnl"].sum()

    # Duration
    def parse_ts(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    df["_entry_dt"] = df["entry_time"].apply(parse_ts)
    df["_exit_dt"]  = df["exit_time"].apply(parse_ts)
    df["_duration"] = (df["_exit_dt"] - df["_entry_dt"]).dt.total_seconds() / 60
    avg_dur = df["_duration"].mean()

    # Streaks
    streak_cur = 0; streak_max_w = 0; streak_max_l = 0; streak_type = None
    for p in df["net_pnl"]:
        win = p > 0
        if streak_type is None:
            streak_type = win; streak_cur = 1
        elif win == streak_type:
            streak_cur += 1
        else:
            if streak_type: streak_max_w = max(streak_max_w, streak_cur)
            else:           streak_max_l = max(streak_max_l, streak_cur)
            streak_type = win; streak_cur = 1
    if streak_type is True:  streak_max_w = max(streak_max_w, streak_cur)
    if streak_type is False: streak_max_l = max(streak_max_l, streak_cur)

    # Best/worst
    best_idx  = df["net_pnl"].idxmax()
    worst_idx = df["net_pnl"].idxmin()

    # Equity curve from trades (cumulative, cash-based)
    start_bal = float(run_meta.get("start_balance") or cfg.DEFAULT_STARTING_BALANCE)
    df["_cumulative"] = start_bal + df["net_pnl"].cumsum()

    # Max drawdown from equity curve table if available, else from trades
    max_dd = 0.0
    max_dd_pct = 0.0
    if not equity_df.empty:
        eq = pd.to_numeric(equity_df["equity"], errors="coerce").dropna()
        if len(eq) > 1:
            rolling_max = eq.cummax()
            dd_abs = (eq - rolling_max)
            dd_pct = dd_abs / rolling_max
            max_dd     = dd_abs.min()
            max_dd_pct = dd_pct.min()
    else:
        eq = df["_cumulative"]
        rolling_max = eq.cummax()
        dd_abs = eq - rolling_max
        max_dd     = dd_abs.min()
        max_dd_pct = (dd_abs / rolling_max).min()

    # Breakdowns
    def breakdown(col):
        result = {}
        for val, group in df.groupby(col):
            if pd.isna(val): val = "Unknown"
            result[str(val)] = {
                "trades": len(group),
                "wins":   len(group[group["net_pnl"] > 0]),
                "pnl":    group["net_pnl"].sum(),
                "r":      group["r_achieved"].tolist(),
                "fees":   group["fees"].sum(),
            }
        return result

    by_strategy  = breakdown("strategy")
    by_regime    = breakdown("regime")
    by_grade     = breakdown("grade")
    by_exit      = breakdown("exit_reason")
    by_direction = breakdown("direction")

    # Equity chart — use equity table if rich enough, else trade-level cumulative
    if not equity_df.empty and len(equity_df) > 10:
        # Downsample for chart (max 500 points)
        step = max(1, len(equity_df) // 500)
        chart_eq = equity_df.iloc[::step].copy()
        eq_labels = chart_eq["timestamp"].astype(str).tolist()
        eq_values = pd.to_numeric(chart_eq["equity"], errors="coerce").tolist()
    else:
        eq_labels = df["exit_time"].astype(str).tolist()
        eq_values = df["_cumulative"].tolist()

    # Drawdown chart — only from equity table
    dd_labels = []
    dd_values = []
    if not equity_df.empty and len(equity_df) > 10:
        step = max(1, len(equity_df) // 500)
        chart_eq = equity_df.iloc[::step].copy()
        eq_s  = pd.to_numeric(chart_eq["equity"], errors="coerce")
        rm    = eq_s.cummax()
        dd_pct_series = ((eq_s - rm) / rm * 100).fillna(0)
        dd_labels = chart_eq["timestamp"].astype(str).tolist()
        dd_values = [round(v, 3) for v in dd_pct_series.tolist()]

    return dict(
        total=total, wins=len(wins), losses=len(losses),
        win_rate=win_rate, avg_r=avg_r, profit_factor=profit_factor,
        net_pnl=net_pnl, total_fees=total_fees,
        avg_win=wins["net_pnl"].mean() if len(wins) else 0,
        avg_loss=losses["net_pnl"].mean() if len(losses) else 0,
        max_dd=max_dd, max_dd_pct=max_dd_pct,
        avg_dur=avg_dur,
        streak_max_w=streak_max_w, streak_max_l=streak_max_l,
        best=df.loc[best_idx].to_dict(),
        worst=df.loc[worst_idx].to_dict(),
        by_strategy=by_strategy, by_regime=by_regime,
        by_grade=by_grade, by_exit=by_exit, by_direction=by_direction,
        eq_labels=eq_labels, eq_values=eq_values,
        dd_labels=dd_labels, dd_values=dd_values,
        start_balance=start_bal,
        end_balance=start_bal + net_pnl,
        df=df,
    )


# ─── HTML BUILDER ─────────────────────────────────────────────────────────────

def build_html(run_meta: dict, stats: dict, trades_df: pd.DataFrame) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    quarter = run_meta.get("quarter") or "—"
    run_id  = run_meta.get("run_id") or "—"

    if not stats:
        body = '<div style="text-align:center;padding:80px;color:#888;font-size:18px">No trades in this run.</div>'
        param_html = ""
    else:
        # ── Parameter snapshot ────────────────────────────────────────────────
        param_set = {}
        try:
            param_set = json.loads(run_meta.get("param_set") or "{}")
        except Exception:
            pass

        param_rows = ""
        param_display = {
            "starting_balance_usd":     "Starting Balance",
            "leverage":                 "Leverage",
            "grade_a_notional_pct":     "Grade A Size",
            "grade_b_notional_pct":     "Grade B Size",
            "min_rrr":                  "Min R:R",
            "min_fee_adjusted_r":       "Min Fee-Adj R",
            "atr_stop_multiplier":      "ATR Stop Mult",
            "adx_trend_threshold":      "ADX Trend Thresh",
            "adx_range_threshold":      "ADX Range Thresh",
            "bb_width_compression_pct": "BB Compression %",
            "trail_activation_r":       "Trail Activation R",
            "partial_exit_pct":         "Partial Exit %",
            "partial_minimum_r":        "Partial Min R",
            "stagnant_trade_minutes":   "Stagnant Timeout",
            "entry_cooldown_minutes":   "Entry Cooldown",
            "vwap_filter_active":       "VWAP Gate",
            "sweep_rejection_candles":  "Sweep Confirm Bars",
            "min_daily_range_pct":      "Min Daily Range",
            "adx_mult_low":             "ADX Mult <25",
            "adx_mult_mid":             "ADX Mult 25–40",
            "adx_mult_high":            "ADX Mult 40–60",
            "adx_mult_extreme":         "ADX Mult >60",
        }
        for key, label in param_display.items():
            val = param_set.get(key, "—")
            if isinstance(val, float) and key.endswith("_pct"):
                val = f"{val*100:.0f}%"
            elif isinstance(val, float):
                val = f"{val:.3f}".rstrip("0").rstrip(".")
            param_rows += f'<tr><td>{label}</td><td style="text-align:right;color:#e0e0e0">{val}</td></tr>'

        param_html = f'''<div class="card">
            <h2>⚙️ Parameter Snapshot</h2>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
            <table style="font-size:12px"><tbody>{param_rows}</tbody></table>
            <div style="color:#888;font-size:12px;padding-top:4px">
                <div>Run ID: <span style="color:#e0e0e0">#{run_id}</span></div>
                <div>Quarter: <span style="color:#e0e0e0">{quarter}</span></div>
                <div>Start balance: <span style="color:#e0e0e0">${stats['start_balance']:,.2f}</span></div>
                <div>End balance: <span style="color:#00c97a;font-weight:700">${stats['end_balance']:,.2f}</span></div>
                <div style="margin-top:10px">Notes: <span style="color:#e0e0e0">{run_meta.get("notes") or "—"}</span></div>
            </div>
            </div></div>'''

        # ── Summary stats grid ────────────────────────────────────────────────
        pf_str = f"{stats['profit_factor']:.2f}" if stats['profit_factor'] != float("inf") else "∞"
        summary = f'''<div class="stats-grid">
            {stat_box("Net P&L", f'<span style="color:{pct_color(stats["net_pnl"])}">${stats["net_pnl"]:+,.2f}</span>',
                      f'Start: ${stats["start_balance"]:,.0f} → End: ${stats["end_balance"]:,.0f}')}
            {stat_box("Win Rate", f'{stats["win_rate"]*100:.1f}%',
                      f'{stats["wins"]}W / {stats["losses"]}L / {stats["total"]} trades')}
            {stat_box("Avg R/Trade", f'<span style="color:{pct_color(stats["avg_r"])}">{stats["avg_r"]:+.3f}R</span>')}
            {stat_box("Profit Factor", pf_str)}
            {stat_box("Avg Win", f'<span style="color:#00c97a">${stats["avg_win"]:,.2f}</span>')}
            {stat_box("Avg Loss", f'<span style="color:#ff4d6d">${abs(stats["avg_loss"]):,.2f}</span>')}
            {stat_box("Max Drawdown", f'<span style="color:#ff4d6d">{stats["max_dd_pct"]*100:.1f}%</span>',
                      f'${abs(stats["max_dd"]):,.2f} absolute')}
            {stat_box("Total Fees", f'<span style="color:#f5a623">${stats["total_fees"]:,.2f}</span>')}
            {stat_box("Avg Duration", f'{stats["avg_dur"]:.0f} min')}
            {stat_box("Best Trade", f'<span style="color:#00c97a">${stats["best"]["net_pnl"]:+,.2f}</span>',
                      str(stats["best"].get("exit_reason","")))}
            {stat_box("Worst Trade", f'<span style="color:#ff4d6d">${stats["worst"]["net_pnl"]:+,.2f}</span>',
                      str(stats["worst"].get("exit_reason","")))}
            {stat_box("Max Win Streak", str(stats["streak_max_w"]))}
            {stat_box("Max Loss Streak", str(stats["streak_max_l"]))}
        </div>'''

        # ── Equity curve chart ────────────────────────────────────────────────
        eq_labels  = stats["eq_labels"]
        eq_values  = stats["eq_values"]
        eq_colors  = ["#00c97a" if v >= stats["start_balance"] else "#ff4d6d" for v in eq_values]

        equity_chart = f'''<div class="card">
            <h2>📈 Equity Curve</h2>
            <canvas id="equityChart" height="80"></canvas>
        </div>
        <script>
        new Chart(document.getElementById('equityChart'), {{
            type: 'line',
            data: {{
                labels: {json.dumps(eq_labels)},
                datasets: [{{
                    label: 'Equity',
                    data: {json.dumps(eq_values)},
                    borderColor: '#00c97a',
                    backgroundColor: 'rgba(0,201,122,0.07)',
                    borderWidth: 1.5,
                    pointRadius: 0,
                    fill: true,
                    tension: 0.2
                }}, {{
                    label: 'Start Balance',
                    data: Array({len(eq_values)}).fill({stats["start_balance"]}),
                    borderColor: '#444',
                    borderWidth: 1,
                    borderDash: [4,4],
                    pointRadius: 0,
                    fill: false,
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }},
                    tooltip: {{ callbacks: {{ label: ctx => '$' + ctx.parsed.y.toFixed(2) }} }} }},
                scales: {{
                    x: {{ ticks: {{ color:'#888', maxTicksLimit:8 }}, grid:{{ color:'#1e1e2e' }} }},
                    y: {{ ticks: {{ color:'#888', callback: v => '$'+v.toLocaleString() }}, grid:{{ color:'#1e1e2e' }} }}
                }}
            }}
        }});
        </script>'''

        # ── Drawdown chart ────────────────────────────────────────────────────
        drawdown_chart = ""
        if stats["dd_labels"]:
            drawdown_chart = f'''<div class="card">
                <h2>📉 Drawdown (%)</h2>
                <canvas id="ddChart" height="50"></canvas>
            </div>
            <script>
            new Chart(document.getElementById('ddChart'), {{
                type: 'line',
                data: {{
                    labels: {json.dumps(stats["dd_labels"])},
                    datasets: [{{
                        label: 'Drawdown %',
                        data: {json.dumps(stats["dd_values"])},
                        borderColor: '#ff4d6d',
                        backgroundColor: 'rgba(255,77,109,0.10)',
                        borderWidth: 1,
                        pointRadius: 0,
                        fill: true,
                        tension: 0.1
                    }}]
                }},
                options: {{
                    responsive: true,
                    plugins: {{ legend: {{ display: false }} }},
                    scales: {{
                        x: {{ ticks: {{ color:'#888', maxTicksLimit:8 }}, grid:{{ color:'#1e1e2e' }} }},
                        y: {{ ticks: {{ color:'#888', callback: v => v+'%' }},
                              grid:{{ color:'#1e1e2e' }}, reverse: false }}
                    }}
                }}
            }});
            </script>'''

        # ── R distribution chart ──────────────────────────────────────────────
        r_vals  = stats["df"]["r_achieved"].dropna().tolist()
        r_bins  = list(range(-5, 6))
        r_counts = [0] * len(r_bins)
        for r in r_vals:
            idx = min(max(int(r) + 5, 0), len(r_bins) - 1)
            r_counts[idx] += 1
        r_bin_labels = [f"{b:+d}R" for b in r_bins]
        r_bin_colors = ["#00c97a" if b >= 0 else "#ff4d6d" for b in r_bins]

        r_chart = f'''<div class="card">
            <h2>📊 R Distribution</h2>
            <canvas id="rChart" height="60"></canvas>
        </div>
        <script>
        new Chart(document.getElementById('rChart'), {{
            type: 'bar',
            data: {{
                labels: {json.dumps(r_bin_labels)},
                datasets: [{{
                    label: 'Trades',
                    data: {json.dumps(r_counts)},
                    backgroundColor: {json.dumps(r_bin_colors)},
                    borderRadius: 4,
                }}]
            }},
            options: {{
                responsive: true,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{
                    x: {{ ticks: {{ color:'#888' }}, grid:{{ color:'#1e1e2e' }} }},
                    y: {{ ticks: {{ color:'#888' }}, grid:{{ color:'#1e1e2e' }} }}
                }}
            }}
        }});
        </script>'''

        # ── Breakdown tables ──────────────────────────────────────────────────
        def breakdown_table(title, data, color_fn=None):
            if not data: return ""
            rows_html = ""
            for k, v in sorted(data.items(), key=lambda x: -abs(x[1].get("pnl", 0))):
                t  = v["trades"]
                w  = v["wins"]
                wr = w / t * 100 if t else 0
                p  = v["pnl"]
                f  = v.get("fees", 0)
                rs = v.get("r", [])
                ar = sum(rs) / len(rs) if rs else None
                wr_bar = f'<div style="background:#2a2a3a;border-radius:3px;height:6px;width:80px;display:inline-block;vertical-align:middle;margin-left:8px"><div style="background:#00c97a;width:{min(100,wr):.0f}%;height:6px;border-radius:3px"></div></div>'
                ar_str = f'{ar:+.2f}R' if ar is not None else '—'
                ar_col = pct_color(ar) if ar is not None else '#888'
                k_display = k
                if color_fn:
                    col = color_fn(k)
                    k_display = f'<span style="color:{col}">{k}</span>'
                rows_html += f'''<tr>
                    <td>{k_display}</td><td>{t}</td>
                    <td>{wr:.0f}% {wr_bar}</td>
                    <td>{pnl_cell(p)}</td>
                    <td><span style="color:{ar_col}">{ar_str}</span></td>
                    <td><span style="color:#f5a623">${f:,.2f}</span></td>
                </tr>'''
            return f'''<div class="card"><h2>{title}</h2>
            <table><thead><tr>
                <th>Name</th><th>Trades</th><th>Win Rate</th>
                <th>Net P&L</th><th>Avg R</th><th>Fees</th>
            </tr></thead><tbody>{rows_html}</tbody></table></div>'''

        breakdowns = (
            breakdown_table("🎯 Performance by Strategy",   stats["by_strategy"]) +
            breakdown_table("📊 Performance by Regime",     stats["by_regime"], color_fn=regime_color) +
            breakdown_table("🏅 Performance by Grade",      stats["by_grade"]) +
            breakdown_table("🚪 Performance by Exit Reason",stats["by_exit"],   color_fn=exit_color) +
            breakdown_table("↕️  Performance by Direction",  stats["by_direction"])
        )

        # ── Trade log ─────────────────────────────────────────────────────────
        df_display = stats["df"].copy()
        trade_rows = ""
        for _, t in df_display.iterrows():
            dur = f'{t["_duration"]:.0f}m' if pd.notna(t.get("_duration")) else "—"
            entry_str = str(t.get("entry_time",""))[:16]
            exit_str  = str(t.get("exit_time",""))[:16]
            regime_col = regime_color(t.get("regime",""))
            exit_col   = exit_color(t.get("exit_reason",""))
            partial_icon = "½" if t.get("partial_taken") else ""
            trail_icon   = "▲" if t.get("trail_active")  else ""
            trade_rows += f'''<tr>
                <td style="font-family:monospace;font-size:10px">{entry_str}</td>
                <td>{"🟢 LONG" if t.get("direction")=="long" else "🔴 SHORT"}</td>
                <td>{grade_badge(t.get("grade","?"))}</td>
                <td style="font-size:11px">{t.get("strategy","—")}</td>
                <td style="color:{regime_col};font-size:11px">{t.get("regime","—")}</td>
                <td>${float(t.get("entry_price",0)):,.2f}</td>
                <td>${float(t.get("exit_price",0)):,.2f}</td>
                <td>{pnl_cell(float(t.get("net_pnl",0)))}</td>
                <td>{r_badge(float(t.get("r_achieved",0)))}</td>
                <td style="color:#f5a623;font-size:11px">${float(t.get("fees",0)):,.3f}</td>
                <td>{dur}</td>
                <td style="color:{exit_col};font-size:11px">{t.get("exit_reason","—")}</td>
                <td style="font-size:11px;color:#888">{partial_icon}{trail_icon}</td>
            </tr>'''

        trade_log = f'''<div class="card">
            <h2>📋 Trade Log ({stats["total"]} trades)</h2>
            <div style="overflow-x:auto">
            <table><thead><tr>
                <th>Entry</th><th>Dir</th><th>Grade</th><th>Strategy</th>
                <th>Regime</th><th>Entry $</th><th>Exit $</th>
                <th>Net P&L</th><th>R</th><th>Fees</th>
                <th>Duration</th><th>Exit Reason</th><th>Flags</th>
            </tr></thead><tbody>{trade_rows}</tbody></table>
            </div></div>'''

        body = summary + equity_chart + drawdown_chart + r_chart + param_html + breakdowns + trade_log

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vertigo Capital — Backtest Report #{run_id} — {quarter}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d0d1a; color: #e0e0e0;
           font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           font-size: 14px; line-height: 1.5; }}
    .header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
               padding: 32px 40px; border-bottom: 1px solid #2a2a3a; }}
    .header h1 {{ font-size: 26px; font-weight: 700; color: #00c97a; letter-spacing: -0.5px; }}
    .header .meta {{ color: #888; font-size: 13px; margin-top: 6px; }}
    .badge {{ display:inline-block; padding:3px 10px; border-radius:20px;
              font-size:12px; font-weight:700; margin-left:10px;
              background:#4a90e2; color:#fff; }}
    .container {{ max-width: 1400px; margin: 0 auto; padding: 24px 20px; }}
    .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
                   gap: 12px; margin-bottom: 24px; }}
    .stat-box {{ background: #1a1a2e; border: 1px solid #2a2a3a;
                 border-radius: 10px; padding: 16px; }}
    .stat-label {{ font-size: 11px; color: #888; text-transform: uppercase;
                   letter-spacing: 0.5px; margin-bottom: 6px; }}
    .stat-value {{ font-size: 20px; font-weight: 700; }}
    .stat-sub {{ font-size: 11px; color: #888; margin-top: 4px; }}
    .card {{ background: #1a1a2e; border: 1px solid #2a2a3a;
             border-radius: 12px; padding: 24px; margin-bottom: 20px; }}
    .card h2 {{ font-size: 15px; font-weight: 600; margin-bottom: 18px; color: #ccc; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    thead tr {{ border-bottom: 1px solid #2a2a3a; }}
    th {{ text-align: left; padding: 8px 12px; color: #888; font-weight: 500;
          font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
          white-space: nowrap; }}
    td {{ padding: 9px 12px; border-bottom: 1px solid #1e1e2e; white-space: nowrap; }}
    tr:hover td {{ background: #1e1e2e; }}
    tr:last-child td {{ border-bottom: none; }}
    @media (max-width:600px) {{ .container {{ padding:12px; }}
        .stats-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
</head>
<body>
<div class="header">
    <h1>Vertigo Capital <span class="badge">BACKTEST</span></h1>
    <div class="meta">BTC/USD · Run #{run_id} · {quarter} · Generated {now}</div>
</div>
<div class="container">
{body}
</div>
</body>
</html>'''


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate backtest HTML report")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id",  type=int,           help="Specific run ID")
    g.add_argument("--latest",  action="store_true", help="Most recent run")
    g.add_argument("--compare", type=str,            help="Comma-separated run IDs")
    parser.add_argument("--out", type=str, default="", help="Output file path")
    args = parser.parse_args()

    bt_logger = BacktestLogger(cfg.RESULTS_DB)

    if args.compare:
        run_ids = [int(x.strip()) for x in args.compare.split(",")]
        df_runs = bt_logger.compare_runs(run_ids)
        print(df_runs.to_string())
        return

    if args.latest:
        runs = bt_logger.get_runs()
        if runs.empty:
            print("No runs found in database.")
            return
        run_id = int(runs.iloc[0]["run_id"])
    else:
        run_id = args.run_id

    run_meta = bt_logger.get_run_summary(run_id)
    if not run_meta:
        print(f"Run #{run_id} not found.")
        return

    trades_df = bt_logger.get_trades(run_id)
    equity_df = bt_logger.get_equity(run_id)

    print(f"Generating report for run #{run_id} | quarter={run_meta.get('quarter')} | "
          f"{len(trades_df)} trades")

    stats = compute_report_stats(trades_df, equity_df, run_meta)
    html  = build_html(run_meta, stats, trades_df)

    quarter_slug = str(run_meta.get("quarter") or "unknown").replace("-", "_")
    out_path = args.out or str(
        Path(cfg.REPORTS_DIR) / f"backtest_run{run_id}_{quarter_slug}.html"
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)

    print(f"\n✅ Report saved: {out_path}")
    print(f"   Open in browser: file://{os.path.abspath(out_path)}")

    if stats:
        print(f"\n   Net P&L:       ${stats['net_pnl']:+,.2f}")
        print(f"   Win rate:      {stats['win_rate']*100:.1f}%")
        print(f"   Profit factor: {stats['profit_factor']:.2f}")
        print(f"   Max drawdown:  {stats['max_dd_pct']*100:.1f}%")
        print(f"   Total fees:    ${stats['total_fees']:,.2f}")


if __name__ == "__main__":
    main()
