#!/usr/bin/env python3
"""
Out-of-sample backtest: run all 10 newer strategies on 2023 data only.
2023 was NOT used in the original discovery/validation (which used 2024-2025).
This is a true out-of-sample test.

Output: backtest_2023_results.txt
"""

import sys, os, json, math
from datetime import datetime
import math

def ttest_1samp(data, popmean=0):
    """Simple one-sample t-test without scipy."""
    n = len(data)
    if n < 3:
        return 0.0, 1.0
    mean = sum(data) / n
    var = sum((x - mean) ** 2 for x in data) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.001
    t_stat = (mean - popmean) / se
    # Approximate p-value using normal for large n, conservative for small n
    # Using the t-distribution approximation
    df = n - 1
    x = abs(t_stat)
    # Abramowitz & Stegun approx for two-tailed p
    a1 = 0.254829592; a2 = -0.284496736; a3 = 1.421413741
    a4 = -1.453152027; a5 = 1.061405429; p_const = 0.3275911
    # Convert t to approximate normal z for large df
    z = x * (1 - 1/(4*df)) if df > 30 else x * math.sqrt(df/(df-2)) if df > 2 else x
    t_var = 1.0 / (1.0 + p_const * z)
    p_one = (a1*t_var + a2*t_var**2 + a3*t_var**3 + a4*t_var**4 + a5*t_var**5) * math.exp(-z*z/2)
    p_val = 2 * max(p_one, 0.0001)
    return t_stat, min(p_val, 1.0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research.data import DataUniverse
from research.exits import (
    profit_target, time_stop, wing_stop, standard_exits,
    simulate_trade, TradeResult
)
from research.sweep import run_sweep, ibf_factory, ic_factory
from research.stats import calc_stats

# Load only 2023 dates
universe = DataUniverse()
universe.load(load_quotes=False)

all_dates = universe.trading_dates()
dates_2023 = [d for d in all_dates if d.startswith("2023")]
print(f"\n2023 trading days: {len(dates_2023)}")
print(f"  Range: {dates_2023[0]} to {dates_2023[-1]}")

# Also grab 2024 and 2025 for comparison
dates_2024 = [d for d in all_dates if d.startswith("2024")]
dates_2025 = [d for d in all_dates if d.startswith("2025")]
dates_2026 = [d for d in all_dates if d.startswith("2026")]

# Strategy definitions — exactly matching generate_calendar_data.py
strategies = [
    {
        "name": "Phoenix 75 Power Close",
        "short": "PHX-PC",
        "grade": "S",
        "structure_fn": ibf_factory(75),
        "entry_times": ["15:15"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 150000,
    },
    {
        "name": "Phoenix 75 Last Hour",
        "short": "PHX-LH",
        "grade": "A",
        "structure_fn": ibf_factory(75),
        "entry_times": ["15:00"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 100000,
    },
    {
        "name": "Firebird 60 Last Hour",
        "short": "FBD-LH",
        "grade": "A",
        "structure_fn": ibf_factory(60),
        "entry_times": ["15:00"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 100000,
    },
    {
        "name": "Phoenix 75 Afternoon",
        "short": "PHX-AFT",
        "grade": "B+",
        "structure_fn": ibf_factory(75),
        "entry_times": ["14:30"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 75000,
    },
    {
        "name": "Ironclad 35 Condor",
        "short": "IC-35",
        "grade": "B+",
        "structure_fn": ic_factory(35, 35),
        "entry_times": ["14:30"],
        "exit_fn": lambda: [profit_target(0.40), wing_stop(), time_stop('15:30')],
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 75000,
    },
    {
        "name": "Firebird 60 Final Bell",
        "short": "FBD-FB",
        "grade": "B+",
        "structure_fn": ibf_factory(60),
        "entry_times": ["15:30"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 75000,
    },
    {
        "name": "Phoenix 75 Early Afternoon",
        "short": "PHX-EA",
        "grade": "B",
        "structure_fn": ibf_factory(75),
        "entry_times": ["13:45"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 50000,
    },
    {
        "name": "Phoenix 75 Midday",
        "short": "PHX-MD",
        "grade": "C+",
        "structure_fn": ibf_factory(75),
        "entry_times": ["14:00"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 35000,
    },
    {
        "name": "Firebird 60 Midday",
        "short": "FBD-MD",
        "grade": "C+",
        "structure_fn": ibf_factory(60),
        "entry_times": ["14:00"],
        "exit_fn": lambda: standard_exits(0.50, '15:30', True),
        "pre_filter": None,
        "intra_filter": None,
        "risk_budget": 35000,
    },
    {
        "name": "Morning Decel Scalp",
        "short": "AM-DEC",
        "grade": "C",
        "structure_fn": ibf_factory(75),
        "entry_times": ["10:30"],
        "exit_fn": lambda: [profit_target(0.30), wing_stop(), time_stop('11:30')],
        "pre_filter": None,
        "intra_filter": lambda d, t: (universe.spx_acceleration(d, t, 10) or 0) < -0.05,
        "risk_budget": 20000,
    },
]

# Reference results from STRATEGIES_TO_REPLICATE.md (2024-2025 combined, per-spread)
reference = {
    "Phoenix 75 Power Close":     {"n_24": 98,  "n_25": 173, "avg_24": 211, "avg_25": 220, "wr_24": 76.5, "wr_25": 80.3, "pf_24": 3.67, "pf_25": 4.95},
    "Phoenix 75 Last Hour":       {"n_24": 106, "n_25": 193, "avg_24": 259, "avg_25": 196, "wr_24": 78.3, "wr_25": 75.6, "pf_24": 3.44, "pf_25": 3.05},
    "Firebird 60 Last Hour":      {"n_24": 219, "n_25": 236, "avg_24": 136, "avg_25": 118, "wr_24": 73.5, "wr_25": 74.6, "pf_24": 2.49, "pf_25": 2.40},
    "Phoenix 75 Afternoon":       {"n_24": 132, "n_25": 216, "avg_24": 179, "avg_25": 229, "wr_24": 72.7, "wr_25": 71.8, "pf_24": 1.95, "pf_25": 2.49},
    "Ironclad 35 Condor":         {"n_24": 155, "n_25": 217, "avg_24": 42,  "avg_25": 24,  "wr_24": 97.4, "wr_25": 91.7, "pf_24": 11.24,"pf_25": 1.93},
    "Firebird 60 Final Bell":     {"n_24": 155, "n_25": 211, "avg_24": 169, "avg_25": 65,  "wr_24": 78.1, "wr_25": 69.7, "pf_24": 4.32, "pf_25": 2.42},
    "Phoenix 75 Early Afternoon": {"n_24": 150, "n_25": 225, "avg_24": 357, "avg_25": 211, "wr_24": 77.3, "wr_25": 71.1, "pf_24": 2.64, "pf_25": 1.72},
    "Phoenix 75 Midday":          {"n_24": 159, "n_25": 221, "avg_24": 277, "avg_25": 207, "wr_24": 76.7, "wr_25": 73.3, "pf_24": 2.17, "pf_25": 1.84},
    "Firebird 60 Midday":         {"n_24": 253, "n_25": 242, "avg_24": 218, "avg_25": 101, "wr_24": 74.3, "wr_25": 71.5, "pf_24": 2.16, "pf_25": 1.44},
    "Morning Decel Scalp":        {"n_24": 118, "n_25": 131, "avg_24": 218, "avg_25": 11,  "wr_24": 77.1, "wr_25": 64.1, "pf_24": 3.61, "pf_25": 1.09},
}


def run_for_year(strat, dates, year_label):
    """Run a single strategy on a set of dates, return summary dict."""
    trades = run_sweep(
        universe,
        strat["structure_fn"],
        strat["entry_times"],
        strat["exit_fn"],
        dates=dates,
        slippage=1.0,
        pre_filter=strat["pre_filter"],
        intra_filter=strat["intra_filter"],
    )

    if not trades:
        return {"year": year_label, "n": 0, "avg": 0, "wr": 0, "pf": 0, "total": 0,
                "max_dd": 0, "wins": 0, "losses": 0, "max_win": 0, "max_loss": 0,
                "target_pct": 0, "wing_pct": 0, "time_pct": 0, "close_pct": 0,
                "monthly": {}, "trades": []}

    # Per-spread P&L (to compare with reference which is per-spread)
    pnls = [t.pnl_per_spread * 100 for t in trades]  # in dollars per spread
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total = sum(pnls)
    avg = total / len(pnls) if pnls else 0
    wr = len(wins) / len(pnls) * 100 if pnls else 0
    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    pf = gross_win / gross_loss if gross_loss > 0 else 99.99

    # Max drawdown (per-spread cumulative)
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    # Exit type breakdown
    exit_counts = {}
    for t in trades:
        etype = t.exit_type.split("_")[0] if "_" in t.exit_type else t.exit_type
        # Normalize
        if "TARGET" in t.exit_type:
            etype = "TARGET"
        elif "WING" in t.exit_type:
            etype = "WING_STOP"
        elif "TIME" in t.exit_type:
            etype = "TIME"
        elif "CLOSE" in t.exit_type:
            etype = "CLOSE"
        exit_counts[etype] = exit_counts.get(etype, 0) + 1

    n = len(trades)
    target_pct = exit_counts.get("TARGET", 0) / n * 100
    wing_pct = exit_counts.get("WING_STOP", 0) / n * 100
    time_pct = exit_counts.get("TIME", 0) / n * 100
    close_pct = exit_counts.get("CLOSE", 0) / n * 100

    # Monthly breakdown
    monthly = {}
    for t, p in zip(trades, pnls):
        m = t.date[:7]
        monthly[m] = monthly.get(m, 0) + p

    # t-test
    t_stat = 0
    p_val = 1.0
    if len(pnls) >= 3:
        t_stat, p_val = ttest_1samp(pnls, 0)

    return {
        "year": year_label,
        "n": n,
        "avg": round(avg, 1),
        "wr": round(wr, 1),
        "pf": round(pf, 2),
        "total": round(total, 0),
        "max_dd": round(max_dd, 0),
        "wins": len(wins),
        "losses": len(losses),
        "max_win": round(max(pnls), 0) if pnls else 0,
        "max_loss": round(min(pnls), 0) if pnls else 0,
        "target_pct": round(target_pct, 1),
        "wing_pct": round(wing_pct, 1),
        "time_pct": round(time_pct, 1),
        "close_pct": round(close_pct, 1),
        "monthly": monthly,
        "t_stat": round(t_stat, 2),
        "p_val": round(p_val, 4),
        "trades": trades,
    }


# ─────────────────────────────────────────────────────────────────────
# RUN ALL STRATEGIES
# ─────────────────────────────────────────────────────────────────────

results = []
out_lines = []

def out(s=""):
    out_lines.append(s)
    print(s)

out("=" * 120)
out("2023 OUT-OF-SAMPLE BACKTEST — ALL 10 NEWER STRATEGIES")
out(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
out(f"2023 trading days: {len(dates_2023)} ({dates_2023[0]} to {dates_2023[-1]})")
out(f"2024 trading days: {len(dates_2024)}, 2025: {len(dates_2025)}, 2026: {len(dates_2026)}")
out("=" * 120)

for strat in strategies:
    name = strat["name"]
    grade = strat["grade"]
    out(f"\n{'─' * 120}")
    out(f"  {name} [Grade {grade}]  |  Budget: ${strat['risk_budget']:,}  |  Entry: {strat['entry_times']}")
    out(f"{'─' * 120}")

    # Run on 2023
    r23 = run_for_year(strat, dates_2023, "2023")
    # Run on 2024 for fresh comparison
    r24 = run_for_year(strat, dates_2024, "2024")
    # Run on 2025
    r25 = run_for_year(strat, dates_2025, "2025")

    results.append({
        "name": name,
        "grade": grade,
        "y2023": r23,
        "y2024": r24,
        "y2025": r25,
    })

    # Print comparison table
    out(f"\n  {'Year':<8} {'N':>5} {'WR%':>7} {'Avg$/sp':>10} {'Total$/sp':>12} {'PF':>7} {'MaxDD':>10} {'t-stat':>8} {'p-val':>8} {'TGT%':>7} {'WING%':>7} {'TIME%':>7}")
    out(f"  {'─'*8} {'─'*5} {'─'*7} {'─'*10} {'─'*12} {'─'*7} {'─'*10} {'─'*8} {'─'*8} {'─'*7} {'─'*7} {'─'*7}")

    for r, label in [(r23, "2023 OOS"), (r24, "2024"), (r25, "2025")]:
        if r["n"] > 0:
            out(f"  {label:<8} {r['n']:>5} {r['wr']:>6.1f}% ${r['avg']:>8.0f} ${r['total']:>10,.0f} {r['pf']:>6.2f} ${r['max_dd']:>8,.0f} {r['t_stat']:>7.2f} {r['p_val']:>7.4f} {r['target_pct']:>6.1f}% {r['wing_pct']:>6.1f}% {r['time_pct']:>6.1f}%")
        else:
            out(f"  {label:<8}     0    N/A        N/A          N/A     N/A        N/A      N/A      N/A     N/A     N/A     N/A")

    # Edge retention
    if r23["n"] > 0 and r24["n"] > 0 and r24["avg"] != 0:
        retention_vs_24 = r23["avg"] / r24["avg"] * 100 if r24["avg"] != 0 else 0
        out(f"\n  2023 vs 2024 edge retention: {retention_vs_24:.0f}%")

    # Monthly P&L for 2023
    if r23["monthly"]:
        out(f"\n  2023 Monthly P&L (per spread):")
        months_pos = 0
        months_neg = 0
        for m in sorted(r23["monthly"].keys()):
            pnl = r23["monthly"][m]
            bar = "█" * max(1, int(abs(pnl) / 200))
            sign = "+" if pnl >= 0 else ""
            if pnl >= 0:
                months_pos += 1
            else:
                months_neg += 1
            out(f"    {m}: {sign}${pnl:>8,.0f}  {'▓' if pnl >= 0 else '░'}{bar}")
        out(f"    Profitable months: {months_pos}/{months_pos+months_neg}")


# ─────────────────────────────────────────────────────────────────────
# SUMMARY RANKING
# ─────────────────────────────────────────────────────────────────────

out(f"\n\n{'=' * 120}")
out("SUMMARY: 2023 OUT-OF-SAMPLE RANKING (sorted by avg $/spread)")
out(f"{'=' * 120}")
out(f"\n  {'#':<3} {'Strategy':<30} {'Grade':<6} {'N':>5} {'WR%':>7} {'Avg$':>8} {'Total$':>12} {'PF':>7} {'p-val':>8} {'vs2024':>8} {'vs2025':>8}")
out(f"  {'─'*3} {'─'*30} {'─'*6} {'─'*5} {'─'*7} {'─'*8} {'─'*12} {'─'*7} {'─'*8} {'─'*8} {'─'*8}")

# Sort by 2023 avg P&L
ranked = sorted(results, key=lambda r: r["y2023"]["avg"], reverse=True)

for i, r in enumerate(ranked, 1):
    r23 = r["y2023"]
    r24 = r["y2024"]
    r25 = r["y2025"]

    vs24 = f"{r23['avg']/r24['avg']*100:.0f}%" if r24["avg"] != 0 and r23["n"] > 0 else "N/A"
    vs25 = f"{r23['avg']/r25['avg']*100:.0f}%" if r25["avg"] != 0 and r23["n"] > 0 else "N/A"

    if r23["n"] > 0:
        out(f"  {i:<3} {r['name']:<30} {r['grade']:<6} {r23['n']:>5} {r23['wr']:>6.1f}% ${r23['avg']:>6.0f} ${r23['total']:>10,.0f} {r23['pf']:>6.2f} {r23['p_val']:>7.4f} {vs24:>8} {vs25:>8}")
    else:
        out(f"  {i:<3} {r['name']:<30} {r['grade']:<6}     0    N/A      N/A          N/A     N/A      N/A      N/A      N/A")


# ─────────────────────────────────────────────────────────────────────
# CROSS-YEAR STABILITY MATRIX
# ─────────────────────────────────────────────────────────────────────

out(f"\n\n{'=' * 120}")
out("CROSS-YEAR STABILITY MATRIX — Avg $/spread by Year")
out(f"{'=' * 120}")
out(f"\n  {'Strategy':<30} {'2023 OOS':>10} {'2024':>10} {'2025':>10} {'All 3yr':>10} {'Stable?':>10}")
out(f"  {'─'*30} {'─'*10} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")

for r in ranked:
    r23, r24, r25 = r["y2023"], r["y2024"], r["y2025"]
    all_positive = r23["avg"] > 0 and r24["avg"] > 0 and r25["avg"] > 0
    stable = "✓ YES" if all_positive else "✗ NO"

    total_n = r23["n"] + r24["n"] + r25["n"]
    total_pnl = r23["total"] + r24["total"] + r25["total"]
    avg_all = total_pnl / total_n if total_n > 0 else 0

    a23 = f"${r23['avg']:>7.0f}" if r23["n"] > 0 else "    N/A"
    a24 = f"${r24['avg']:>7.0f}" if r24["n"] > 0 else "    N/A"
    a25 = f"${r25['avg']:>7.0f}" if r25["n"] > 0 else "    N/A"
    aall = f"${avg_all:>7.0f}" if total_n > 0 else "    N/A"

    out(f"  {r['name']:<30} {a23:>10} {a24:>10} {a25:>10} {aall:>10} {stable:>10}")


# Save
outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_2023_results.txt")
with open(outpath, "w") as f:
    f.write("\n".join(out_lines))
print(f"\n\nResults saved to {outpath}")
