"""
V3 PHOENIX Tier 1 Improvements Analysis
========================================
Tests 4 potential improvements against the V3 baseline:
  1. Dynamic profit target by signal count
  2. Adaptive time stop (take-and-run)
  3. Day-of-week filter
  4. Credit quality filter

Uses research_all_trades.csv with intraday P&L snapshots.
P&L columns (pnl_at_XXXX, hit_XX_pnl) are per-spread dollar values.
"""

import pandas as pd
import numpy as np

# --- Load data ---
df = pd.read_csv("research_all_trades.csv")
df["date"] = pd.to_datetime(df["date"])

# Back-calculate credit per spread
df["ml_per_spread"] = df["risk_deployed_p1"] / df["n_spreads_p1"].clip(lower=1)
df["credit"] = df["wing_width"] - df["ml_per_spread"] / 100

# --- Reconstruct PHOENIX signal count per day ---
def count_phoenix_signals(row):
    vix = row.get("vix", 99)
    vp = row.get("vp_ratio", 99)
    ret5d = row.get("prior_5d_return", 0)
    pd_dir = str(row.get("prior_day_direction", "")).upper()
    rv_chg = row.get("rv_1d_change", 0)
    in_range = row.get("in_prior_week_range", True)
    rv_slope = str(row.get("rv_slope", "")).upper()
    count = 0
    if vix <= 20 and vp <= 1.0 and ret5d > 0: count += 1  # S1
    if vp <= 1.3 and pd_dir in ("DN","DOWN") and ret5d > 0: count += 1  # S2
    if vp <= 1.2 and ret5d > 0 and rv_chg > 0: count += 1  # S3
    if vp <= 1.5 and not in_range and ret5d > 0: count += 1  # S4
    if vp <= 1.3 and rv_slope != "RISING" and ret5d > 0: count += 1  # S5
    return count

df["signal_count"] = df.apply(count_phoenix_signals, axis=1)

# V3 trades = days where at least 1 signal fires
v3 = df[df["signal_count"] >= 1].copy()
print(f"V3 PHOENIX trades: {len(v3)} (of {len(df)} total days)")
print(f"Signal count distribution: {v3['signal_count'].value_counts().sort_index().to_dict()}")
print()

# --- Constants ---
RISK = 100000
SPX_MULT = 100

# --- Core simulation: same approach as ensemble_v3.py ---
def find_exit_pnl(row, target_pct, time_stop):
    """Find exit P&L (per-spread) given target and time stop. Returns (pnl, outcome)."""
    ts_map = {"close": (16, 0), "1545": (15, 45), "1530": (15, 30),
              "1500": (15, 0), "1430": (14, 30), "1400": (14, 0),
              "1330": (13, 30), "1300": (13, 0), "1200": (12, 0)}
    ts_h, ts_m = ts_map.get(time_stop, (16, 0))

    events = []

    # Wing stop
    ws_pnl = row.get("ws_pnl")
    ws_time = row.get("ws_time")
    if pd.notna(ws_pnl) and pd.notna(ws_time):
        try:
            wt = pd.Timestamp(ws_time)
            if wt.hour < ts_h or (wt.hour == ts_h and wt.minute < ts_m):
                events.append(("WS", wt, ws_pnl))
        except: pass

    # Target hit
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")
    tgt_time = row.get(f"hit_{target_pct}_time")
    if pd.notna(tgt_pnl) and pd.notna(tgt_time):
        try:
            tt = pd.Timestamp(tgt_time)
            if tt.hour < ts_h or (tt.hour == ts_h and tt.minute < ts_m):
                events.append(("TGT", tt, tgt_pnl))
        except: pass

    # First event wins
    if events:
        events.sort(key=lambda x: x[1])
        return events[0][2], events[0][0]

    # Time stop
    ts_label = time_stop if time_stop != "close" else "close"
    ts_col = f"pnl_at_{ts_label}"
    ts_pnl = row.get(ts_col)
    if pd.notna(ts_pnl): return ts_pnl, "TS"

    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl): return close_pnl, "EXP"
    return None, "ND"


def sim_trade(row, target_pct, time_stop="close"):
    """Simulate a single 1T trade. Returns (dollar_pnl, outcome)."""
    credit = row["credit"]
    ww = row["wing_width"]
    ml_per = (ww - credit) * SPX_MULT
    if ml_per <= 0: return 0, "ND"
    n = max(1, int(RISK / ml_per))

    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None: return 0, "ND"
    return round(exit_pnl * n * SPX_MULT), outcome


def sim_early_exit(row, normal_target, early_target, cutoff_time, time_stop="close"):
    """Take-and-run: exit at early_target if hit before cutoff_time. Else normal."""
    credit = row["credit"]
    ww = row["wing_width"]
    ml_per = (ww - credit) * SPX_MULT
    if ml_per <= 0: return 0, "ND"
    n = max(1, int(RISK / ml_per))

    # Check wing stop first (overrides everything)
    ws_pnl = row.get("ws_pnl")
    ws_time = row.get("ws_time")
    if pd.notna(ws_pnl) and pd.notna(ws_time):
        try:
            wt = pd.Timestamp(ws_time)
            # Check early target hit time
            et_pnl = row.get(f"hit_{early_target}_pnl")
            et_time = row.get(f"hit_{early_target}_time")
            if pd.notna(et_pnl) and pd.notna(et_time):
                ett = pd.Timestamp(et_time)
                if wt < ett:  # wing stop happened before early target
                    return round(ws_pnl * n * SPX_MULT), "WS"
            else:
                # No early target hit — check if WS is before normal exit
                pass  # fall through to normal logic below
        except: pass

    # Check early target
    et_pnl = row.get(f"hit_{early_target}_pnl")
    et_time = row.get(f"hit_{early_target}_time")
    cutoff_h = int(cutoff_time[:2])
    cutoff_m = int(cutoff_time[2:])
    if pd.notna(et_pnl) and pd.notna(et_time):
        try:
            ett = pd.Timestamp(et_time)
            if ett.hour < cutoff_h or (ett.hour == cutoff_h and ett.minute <= cutoff_m):
                return round(et_pnl * n * SPX_MULT), "EARLY"
        except: pass

    # Fall through to normal target/time stop
    exit_pnl, outcome = find_exit_pnl(row, normal_target, time_stop)
    if exit_pnl is None: return 0, "ND"
    return round(exit_pnl * n * SPX_MULT), outcome


def stats(results_df):
    """Compute summary stats from a results DataFrame with 'pnl' column."""
    total = results_df["pnl"].sum()
    wins = results_df[results_df["pnl"] > 0]["pnl"].sum()
    losses = abs(results_df[results_df["pnl"] < 0]["pnl"].sum())
    pf = wins / losses if losses > 0 else 99
    wr = (results_df["pnl"] > 0).mean() * 100
    cum = results_df["pnl"].cumsum()
    dd = (cum.cummax() - cum).max()
    return total, pf, wr, dd


# ════════════════════════════════════════════════════════════════
# BASELINE
# ════════════════════════════════════════════════════════════════
print("=" * 80)
print("BASELINE: V3 (50% target, hold to close, 1T)")
print("=" * 80)

base_results = []
for _, row in v3.iterrows():
    pnl, outcome = sim_trade(row, 50, "close")
    base_results.append({"date": row["date"], "pnl": pnl, "outcome": outcome,
                         "sc": row["signal_count"], "dow": row["date"].dayofweek,
                         "credit": row["credit"], "vix": row["vix"]})
bdf = pd.DataFrame(base_results)
b_total, b_pf, b_wr, b_dd = stats(bdf)
print(f"  Trades: {len(bdf)}, Total: ${b_total:,.0f}, PF: {b_pf:.2f}, Win: {b_wr:.1f}%, MaxDD: ${b_dd:,.0f}")
outcomes = bdf["outcome"].value_counts().to_dict()
print(f"  Outcomes: {outcomes}")
print()


# ════════════════════════════════════════════════════════════════
# TEST 1: Dynamic Profit Target by Signal Count
# ════════════════════════════════════════════════════════════════
print("=" * 80)
print("TEST 1: Dynamic Profit Target by Signal Count")
print("=" * 80)
print()

# First show baseline by signal count
print("  --- Baseline breakdown by signal count ---")
print(f"  {'Signals':>8} {'Trades':>6} {'Total P&L':>12} {'Win%':>6} {'PF':>6} {'Avg P&L':>10}")
print(f"  {'-'*55}")
for sc in sorted(bdf["sc"].unique()):
    s = bdf[bdf["sc"] == sc]
    t, pf, wr, _ = stats(s)
    print(f"  {sc:>8} {len(s):>6} ${t:>11,.0f} {wr:>5.1f}% {pf:>5.2f} ${s['pnl'].mean():>9,.0f}")
print()

# Test different target maps
target_maps = {
    "Conservative (40/50/60/70)": {1: 40, 2: 50, 3: 60, 4: 70, 5: 70},
    "Moderate (40/50/70/80)":     {1: 40, 2: 50, 3: 70, 4: 80, 5: 80},
    "Aggressive (30/50/70/90)":   {1: 30, 2: 50, 3: 70, 4: 90, 5: 90},
    "Quick low (40/40/50/50)":    {1: 40, 2: 40, 3: 50, 4: 50, 5: 50},
    "1sig=40 only":               {1: 40, 2: 50, 3: 50, 4: 50, 5: 50},
    "3+sig=70":                   {1: 50, 2: 50, 3: 70, 4: 70, 5: 70},
}

print(f"  {'Config':<32} {'Total':>12} {'Delta':>10} {'PF':>6} {'Win%':>6}")
print(f"  {'-'*70}")
print(f"  {'BASELINE (flat 50%)':<32} ${b_total:>11,.0f} {'---':>10} {b_pf:>5.2f} {b_wr:>5.1f}%")
for name, tmap in target_maps.items():
    results = []
    for _, row in v3.iterrows():
        tgt = tmap.get(row["signal_count"], 50)
        pnl, outcome = sim_trade(row, tgt, "close")
        results.append({"pnl": pnl})
    rdf = pd.DataFrame(results)
    t, pf, wr, _ = stats(rdf)
    delta = t - b_total
    print(f"  {name:<32} ${t:>11,.0f} {'+' if delta>=0 else ''}{delta:>9,.0f} {pf:>5.2f} {wr:>5.1f}%")
print()


# ════════════════════════════════════════════════════════════════
# TEST 2: Adaptive Time Stop (Take-and-Run)
# ════════════════════════════════════════════════════════════════
print("=" * 80)
print("TEST 2: Adaptive Time Stop (Take-and-Run)")
print("=" * 80)
print("If profit >= early_target by cutoff, exit. Else hold to 50%/close.")
print()

configs = [
    ("30% by 12:00", 30, "1200"),
    ("30% by 13:00", 30, "1300"),
    ("30% by 14:00", 30, "1400"),
    ("40% by 12:00", 40, "1200"),
    ("40% by 13:00", 40, "1300"),
    ("40% by 14:00", 40, "1400"),
]

print(f"  {'Config':<22} {'Total':>12} {'Delta':>10} {'PF':>6} {'Win%':>6} {'Early%':>7}")
print(f"  {'-'*70}")
print(f"  {'BASELINE (50%/close)':<22} ${b_total:>11,.0f} {'---':>10} {b_pf:>5.2f} {b_wr:>5.1f}%")
for name, early_tgt, cutoff in configs:
    results = []
    early_n = 0
    for _, row in v3.iterrows():
        pnl, outcome = sim_early_exit(row, 50, early_tgt, cutoff, "close")
        results.append({"pnl": pnl, "outcome": outcome})
        if outcome == "EARLY": early_n += 1
    rdf = pd.DataFrame(results)
    t, pf, wr, _ = stats(rdf)
    delta = t - b_total
    pct_early = early_n / len(v3) * 100
    print(f"  {name:<22} ${t:>11,.0f} {'+' if delta>=0 else ''}{delta:>9,.0f} {pf:>5.2f} {wr:>5.1f}% {pct_early:>5.1f}%")
print()


# ════════════════════════════════════════════════════════════════
# TEST 3: Day-of-Week Filter
# ════════════════════════════════════════════════════════════════
print("=" * 80)
print("TEST 3: Day-of-Week Analysis")
print("=" * 80)
print()

dow_names = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}

print(f"  {'Day':<12} {'Trades':>6} {'Total P&L':>12} {'Win%':>6} {'PF':>6} {'Avg P&L':>10}")
print(f"  {'-'*55}")
for d in range(5):
    s = bdf[bdf["dow"] == d]
    if len(s) == 0: continue
    t, pf, wr, _ = stats(s)
    print(f"  {dow_names[d]:<12} {len(s):>6} ${t:>11,.0f} {wr:>5.1f}% {pf:>5.2f} ${s['pnl'].mean():>9,.0f}")

print()
print("  --- Impact of skipping each day ---")
print(f"  {'Skip':<15} {'Trades':>6} {'Total':>12} {'Delta':>10} {'PF':>6}")
print(f"  {'-'*52}")
for d in range(5):
    s = bdf[bdf["dow"] != d]
    t, pf, wr, _ = stats(s)
    delta = t - b_total
    print(f"  {dow_names[d]:<15} {len(s):>6} ${t:>11,.0f} {'+' if delta>=0 else ''}{delta:>9,.0f} {pf:>5.2f}")
print()


# ════════════════════════════════════════════════════════════════
# TEST 4: Credit Quality Filter
# ════════════════════════════════════════════════════════════════
print("=" * 80)
print("TEST 4: Credit Quality Filter")
print("=" * 80)
print()

credits = bdf["credit"]
print(f"  Credit stats: min=${credits.min():.2f}, p25=${credits.quantile(0.25):.2f}, "
      f"median=${credits.median():.2f}, p75=${credits.quantile(0.75):.2f}, max=${credits.max():.2f}")
print()

# Quartile analysis
bdf["credit_q"] = pd.qcut(bdf["credit"], 4, labels=["Q1 (thin)", "Q2", "Q3", "Q4 (fat)"], duplicates="drop")
print("  --- Performance by credit quartile ---")
print(f"  {'Quartile':<15} {'Range':>15} {'Trades':>6} {'Total P&L':>12} {'Win%':>6} {'PF':>6}")
print(f"  {'-'*65}")
for q in bdf["credit_q"].cat.categories:
    s = bdf[bdf["credit_q"] == q]
    t, pf, wr, _ = stats(s)
    cmin, cmax = s["credit"].min(), s["credit"].max()
    print(f"  {q:<15} ${cmin:>5.2f}-${cmax:<5.2f} {len(s):>6} ${t:>11,.0f} {wr:>5.1f}% {pf:>5.2f}")
print()

# Threshold filter tests
print("  --- Minimum credit filter ---")
print(f"  {'Threshold':>10} {'Trades':>6} {'Skip':>5} {'Total':>12} {'Delta':>10} {'PF':>6} {'Win%':>6}")
print(f"  {'-'*60}")
for thresh in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
    s = bdf[bdf["credit"] >= thresh]
    if len(s) == 0:
        print(f"  >=${thresh:<5.1f}     0  (all filtered)")
        continue
    t, pf, wr, _ = stats(s)
    delta = t - b_total
    skip = len(bdf) - len(s)
    print(f"  >=${thresh:<5.1f} {len(s):>6} {skip:>5} ${t:>11,.0f} {'+' if delta>=0 else ''}{delta:>9,.0f} {pf:>5.2f} {wr:>5.1f}%")

print()
print()
print("=" * 80)
print("DONE")
print("=" * 80)
