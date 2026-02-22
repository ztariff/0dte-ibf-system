"""
ENSEMBLE BACKTEST — Options A, B, C
=====================================
Compare 3 approaches for allocating $100K daily risk across 14,991 parameter sets.

Option A: Cluster into strategy families (group by filter, pick best mechanics per cluster)
Option B: Confluence as position sizing for a single trade
Option C: Tier-based allocation with distinct mechanics buckets

Usage:
    python3 ensemble_backtest.py | tee ensemble_results.txt
"""

import os, json, sys
import pandas as pd
import numpy as np
from collections import defaultdict
from itertools import combinations

_DIR = os.path.dirname(os.path.abspath(__file__))

# ════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ════════════════════════════════════════════════════════════════════════════
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)

with open(os.path.join(_DIR, "signal_catalog.json")) as f:
    catalog = json.load(f)

print(f"  {len(go)} GO trades, {len(catalog)} parameter sets loaded")

# ════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════
DAILY_RISK = 100_000
SPX_MULT = 100
SLIPPAGE_PER_SPREAD = 1.00
TRANCHE_RISK = 25_000

TS_HM = {
    "1030": (10,30), "1100": (11,0), "1130": (11,30),
    "1200": (12,0), "1230": (12,30), "1300": (13,0),
    "1330": (13,30), "1400": (14,0), "1430": (14,30),
    "1500": (15,0), "1530": (15,30), "1545": (15,45),
    "close": (16,0),
}

CHECKPOINTS_60M = [
    ("1100", 60), ("1200", 120), ("1300", 180),
    ("1400", 240), ("1500", 300),
]
CHECKPOINTS_30M = [
    ("1030", 30), ("1100", 60), ("1130", 90),
    ("1200", 120), ("1230", 150), ("1300", 180),
    ("1330", 210), ("1400", 240), ("1430", 270),
    ("1500", 300), ("1530", 330), ("1545", 345),
]

# ════════════════════════════════════════════════════════════════════════════
# REBUILD ATOMIC FILTERS (same logic as build_signal_catalog.py)
# ════════════════════════════════════════════════════════════════════════════
print("Rebuilding filter masks...", flush=True)

atomic_filters = {
    "VIX≤15":      go["vix"] <= 15,
    "VIX≤16":      go["vix"] <= 16,
    "VIX≤17":      go["vix"] <= 17,
    "VIX≤18":      go["vix"] <= 18,
    "VIX≤20":      go["vix"] <= 20,
    "VP≤1.0":      go["vp_ratio"] <= 1.0,
    "VP≤1.2":      go["vp_ratio"] <= 1.2,
    "VP≤1.3":      go["vp_ratio"] <= 1.3,
    "VP≤1.5":      go["vp_ratio"] <= 1.5,
    "VP≤1.7":      go["vp_ratio"] <= 1.7,
    "VP≤2.0":      go["vp_ratio"] <= 2.0,
    "STABLE":      go["rv_slope"] == "STABLE",
    "!RISING":     go["rv_slope"] != "RISING",
    "FLAT_vwap":   go["vwap_slope"] == "FLAT",
    "Rng≤0.3":     go["range_pct"] <= 0.3,
    "Rng≤0.4":     go["range_pct"] <= 0.4,
    "Rng≤0.6":     go["range_pct"] <= 0.6,
}

if "prior_day_return" in go.columns:
    atomic_filters.update({
        "PrDayDn":      go["prior_day_direction"] == "DOWN",
        "PrDayUp":      go["prior_day_direction"] == "UP",
        "PrRet<-1":     go["prior_day_return"] < -1,
        "PrRet>0":      go["prior_day_return"] > 0,
        "PrRng≤0.8":    go["prior_day_range"] <= 0.8,
        "PrRng≤1.0":    go["prior_day_range"] <= 1.0,
        "InWkRng":      go["in_prior_week_range"] == 1,
        "OutWkRng":     go["in_prior_week_range"] == 0,
        "InMoRng":      go["in_prior_month_range"] == 1,
        "WkTop50":      go["pct_in_weekly_range"] >= 50,
        "5dRet>0":      go["prior_5d_return"] > 0,
        "5dRet>1":      go["prior_5d_return"] > 1,
        "5dRet<0":      go["prior_5d_return"] < 0,
        "PrRV<12":      go["prior_day_rv"] < 12,
        "PrRV<15":      go["prior_day_rv"] < 15,
        "RVchg<0":      go["rv_1d_change"] < 0,
        "RVchg>0":      go["rv_1d_change"] > 0,
    })


def parse_filter_mask(filter_name):
    """Parse a filter combo name like 'VP≤1.3 + PrDayDn + 5dRet>0' into a boolean mask."""
    parts = [p.strip() for p in filter_name.split(" + ")]
    mask = pd.Series(True, index=go.index)
    for p in parts:
        if p in atomic_filters:
            mask = mask & atomic_filters[p]
        else:
            # Unknown filter — skip this set
            return None
    return mask


# ════════════════════════════════════════════════════════════════════════════
# SIMULATE A SINGLE DAY WITH GIVEN MECHANICS
# ════════════════════════════════════════════════════════════════════════════

def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str) or t_str == "":
        return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except:
        return False


def find_exit_pnl(row, target_pct, time_stop):
    """Find exit P&L per spread and outcome."""
    ts_hour, ts_min = TS_HM.get(time_stop, (16, 0))

    tgt_time = row.get(f"hit_{target_pct}_time", "")
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")
    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")

    events = []
    if ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_hour, ts_min):
        try:
            events.append(("WS", pd.Timestamp(ws_time), ws_pnl))
        except: pass
    if pd.notna(tgt_pnl) and tgt_time and time_before(tgt_time, ts_hour, ts_min):
        try:
            events.append(("TGT", pd.Timestamp(tgt_time), tgt_pnl))
        except: pass

    if events:
        events.sort(key=lambda x: x[1])
        return events[0][2], events[0][0]

    ts_col = f"pnl_at_{time_stop}" if time_stop != "close" else "pnl_at_close"
    ts_pnl = row.get(ts_col)
    if pd.notna(ts_pnl):
        return ts_pnl, "TS"

    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl):
        return close_pnl, "EXP"

    return None, "ND"


def simulate_day_dollar(row, target_pct, time_stop, n_tranches, interval_cps, risk_allocated):
    """
    Simulate a single day. Returns $ P&L scaled to risk_allocated.
    risk_allocated = how much of the $100K is assigned to this position.
    """
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND":
        return 0.0, "ND"

    # Determine exit time
    if outcome == "WS":
        exit_time_str = row.get("ws_time", "")
    elif outcome == "TGT":
        exit_time_str = row.get(f"hit_{target_pct}_time", "")
    else:
        exit_time_str = time_stop

    if exit_time_str in TS_HM:
        exit_h, exit_m = TS_HM[exit_time_str]
    else:
        try:
            et = pd.Timestamp(exit_time_str)
            exit_h, exit_m = et.hour, et.minute
        except:
            exit_h, exit_m = 16, 0

    # Size: how many spreads for this risk allocation
    risk_per_spread = row.get("risk_deployed_p1", 0)
    n_sp = row.get("n_spreads_p1", 0)
    if n_sp > 0 and risk_per_spread > 0:
        ml_ps = risk_per_spread / n_sp  # max loss per spread
    else:
        ml_ps = TRANCHE_RISK
    if ml_ps <= 0:
        ml_ps = TRANCHE_RISK

    # Number of spreads for tranche 1 based on allocated risk
    risk_per_tranche = risk_allocated / max(1, n_tranches)
    n_per = max(1, int(risk_per_tranche / ml_ps))

    # Tranche 1 P&L
    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    # Additional tranches
    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_cps):
            break
        cp_lbl, _ = interval_cps[k - 2]
        cp_pnl = row.get(f"pnl_at_{cp_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl):
            continue
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m):
            continue
        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        total += tk_pnl

    return round(total, 2), outcome


def get_interval_cps(tranches_label):
    if "60m" in tranches_label:
        return CHECKPOINTS_60M
    elif "30m" in tranches_label:
        return CHECKPOINTS_30M
    return []


def get_n_tranches(tranches_label):
    try:
        return int(tranches_label.split("T")[0])
    except:
        return 1


# ════════════════════════════════════════════════════════════════════════════
# PRECOMPUTE: For each day, which sets fire? (by set_id)
# ════════════════════════════════════════════════════════════════════════════
print("Precomputing which sets fire per day...", flush=True)

# Build a lookup: filter_name → boolean mask over go
filter_mask_cache = {}
for ps in catalog:
    fn = ps["filters"]
    if fn not in filter_mask_cache:
        mask = parse_filter_mask(fn)
        filter_mask_cache[fn] = mask

# For each day, store which sets fire + their tier info
# day_sets[day_idx] = list of (set_id, tier, filters, target_pct, time_stop, tranches_label)
day_sets = defaultdict(list)
set_info = {}  # set_id → full info dict

for ps in catalog:
    sid = ps["set_id"]
    set_info[sid] = ps
    mask = filter_mask_cache.get(ps["filters"])
    if mask is None:
        continue
    for idx in go.index[mask]:
        day_sets[idx].append(sid)

# Count tiers per day for quick access
day_tier_counts = {}
for idx in range(len(go)):
    sids = day_sets.get(idx, [])
    counts = {"S": 0, "A": 0, "B": 0, "C": 0, "D": 0, "total": len(sids)}
    for sid in sids:
        tier = set_info[sid]["tier"]
        counts[tier] = counts.get(tier, 0) + 1
    day_tier_counts[idx] = counts

print(f"  Precompute done. Avg sets/day: {np.mean([len(day_sets.get(i, [])) for i in range(len(go))]):.0f}", flush=True)

# ════════════════════════════════════════════════════════════════════════════
# OPTION A: Cluster into Strategy Families
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  OPTION A: Cluster into Strategy Families")
print(f"{'='*120}", flush=True)

# Group all sets by their filter combination
# Within each filter group, pick the best set (highest Calmar among S/A/B tier, or highest PF)
filter_groups = defaultdict(list)
for ps in catalog:
    filter_groups[ps["filters"]].append(ps)

# Pick the representative set for each filter cluster
clusters = []
for filt_name, group in filter_groups.items():
    # Prefer robust sets, then highest calmar
    robust = [ps for ps in group if ps.get("robust") == True or ps.get("robust") == "True"]
    if robust:
        best = max(robust, key=lambda x: x["calmar"])
    else:
        best = max(group, key=lambda x: x["calmar"])
    # Only keep clusters where the best set has PF >= 1.2
    if best["profit_factor"] >= 1.2:
        clusters.append({
            "cluster_name": filt_name,
            "representative": best,
            "n_mechanics": len(group),
            "best_pf": best["profit_factor"],
            "best_calmar": best["calmar"],
            "tier": best["tier"],
        })

# Sort clusters by tier then calmar
tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
clusters.sort(key=lambda x: (tier_order.get(x["tier"], 5), -x["best_calmar"]))

print(f"  {len(clusters)} filter clusters with PF≥1.2")
print(f"  Tier breakdown: S={sum(1 for c in clusters if c['tier']=='S')}, "
      f"A={sum(1 for c in clusters if c['tier']=='A')}, "
      f"B={sum(1 for c in clusters if c['tier']=='B')}, "
      f"C={sum(1 for c in clusters if c['tier']=='C')}, "
      f"D={sum(1 for c in clusters if c['tier']=='D')}")

# Precompute filter masks for clusters
cluster_masks = {}
for cl in clusters:
    mask = parse_filter_mask(cl["cluster_name"])
    if mask is not None:
        cluster_masks[cl["cluster_name"]] = mask

# Simulate Option A: each day, find which clusters fire, allocate $100K equally
print("  Simulating Option A...", flush=True)

opt_a_daily_pnl = []
opt_a_daily_trades = []

for idx in range(len(go)):
    row = go.iloc[idx]
    firing_clusters = []
    for cl in clusters:
        mask = cluster_masks.get(cl["cluster_name"])
        if mask is not None and mask.iloc[idx]:
            firing_clusters.append(cl)

    if len(firing_clusters) == 0:
        opt_a_daily_pnl.append(0.0)
        opt_a_daily_trades.append(0)
        continue

    # Allocate $100K equally across firing clusters
    risk_each = DAILY_RISK / len(firing_clusters)
    day_pnl = 0.0

    for cl in firing_clusters:
        rep = cl["representative"]
        n_t = get_n_tranches(rep["tranches"])
        cps = get_interval_cps(rep["tranches"])
        pnl, oc = simulate_day_dollar(row, rep["target_pct"], rep["time_stop"], n_t, cps, risk_each)
        day_pnl += pnl

    opt_a_daily_pnl.append(day_pnl)
    opt_a_daily_trades.append(len(firing_clusters))

opt_a_arr = np.array(opt_a_daily_pnl)

# ════════════════════════════════════════════════════════════════════════════
# OPTION B: Confluence as Position Sizing (Single Trade)
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  OPTION B: Confluence Position Sizing (Single Trade)")
print(f"{'='*120}", flush=True)

# Use the best overall mechanics: 50%/close/5T/60m (SET_8913's mechanics)
# Size based on confluence: S+A tier count determines how much of $100K to deploy
# 0 SAB sets = skip, 1-10 SAB = 25%, 10-50 SAB = 50%, 50-200 SAB = 75%, 200+ = 100%

print("  Simulating Option B...", flush=True)

opt_b_daily_pnl = []
opt_b_daily_trades = []

for idx in range(len(go)):
    row = go.iloc[idx]
    tc = day_tier_counts[idx]
    sab = tc["S"] + tc["A"] + tc["B"]

    if sab == 0:
        opt_b_daily_pnl.append(0.0)
        opt_b_daily_trades.append(0)
        continue

    # Scale by confluence
    if sab < 10:
        scale = 0.25
    elif sab < 50:
        scale = 0.50
    elif sab < 200:
        scale = 0.75
    else:
        scale = 1.0

    risk_alloc = DAILY_RISK * scale
    pnl, oc = simulate_day_dollar(row, 50, "close", 5, CHECKPOINTS_60M, risk_alloc)
    opt_b_daily_pnl.append(pnl)
    opt_b_daily_trades.append(1)

opt_b_arr = np.array(opt_b_daily_pnl)

# ════════════════════════════════════════════════════════════════════════════
# OPTION C: Tier-Based Allocation with Distinct Mechanics Buckets
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  OPTION C: Tier-Based Multi-Mechanics Buckets")
print(f"{'='*120}", flush=True)

# Define 5 mechanics buckets (genuinely different exit profiles)
mech_buckets = [
    {"name": "Conservative",  "target_pct": 40, "time_stop": "close", "tranches": "1T",     "cps": [],              "n_t": 1},
    {"name": "Base",          "target_pct": 50, "time_stop": "close", "tranches": "5T/60m",  "cps": CHECKPOINTS_60M, "n_t": 5},
    {"name": "Aggressive",    "target_pct": 70, "time_stop": "close", "tranches": "5T/60m",  "cps": CHECKPOINTS_60M, "n_t": 5},
    {"name": "EarlyExit",     "target_pct": 50, "time_stop": "1530",  "tranches": "3T/60m",  "cps": CHECKPOINTS_60M, "n_t": 3},
    {"name": "Swing",         "target_pct": 70, "time_stop": "1545",  "tranches": "5T/60m",  "cps": CHECKPOINTS_60M, "n_t": 5},
]

# For each bucket, find the best filter combos (S/A tier sets matching that mechanic)
bucket_filters = {}
for bkt in mech_buckets:
    # Find all S/A/B sets that match this bucket's mechanics
    matching = [ps for ps in catalog
                if ps["target_pct"] == bkt["target_pct"]
                and ps["time_stop"] == bkt["time_stop"]
                and ps["tranches"] == bkt["tranches"]
                and ps["tier"] in ("S", "A", "B")
                and (ps.get("robust") == True or ps.get("robust") == "True")]
    # Group by filter, pick best by calmar
    best_filters = {}
    for ps in matching:
        fn = ps["filters"]
        if fn not in best_filters or ps["calmar"] > best_filters[fn]["calmar"]:
            best_filters[fn] = ps
    bucket_filters[bkt["name"]] = list(best_filters.keys())
    print(f"  Bucket '{bkt['name']}' ({bkt['target_pct']}%/{bkt['time_stop']}/{bkt['tranches']}): "
          f"{len(best_filters)} filter combos")

# Precompute bucket filter masks
bucket_masks = {}
for bname, filters_list in bucket_filters.items():
    masks = {}
    for fn in filters_list:
        m = parse_filter_mask(fn)
        if m is not None:
            masks[fn] = m
    bucket_masks[bname] = masks

# Simulate Option C: each day, check which buckets have signals firing,
# allocate $100K across active buckets weighted by their S/A signal count
print("  Simulating Option C...", flush=True)

opt_c_daily_pnl = []
opt_c_daily_trades = []

for idx in range(len(go)):
    row = go.iloc[idx]

    # For each bucket, count how many of its filters fire today
    bucket_scores = {}
    for bkt in mech_buckets:
        bname = bkt["name"]
        masks = bucket_masks.get(bname, {})
        n_firing = sum(1 for fn, m in masks.items() if m.iloc[idx])
        if n_firing > 0:
            bucket_scores[bname] = n_firing

    if len(bucket_scores) == 0:
        opt_c_daily_pnl.append(0.0)
        opt_c_daily_trades.append(0)
        continue

    # Weight allocation by number of filters firing per bucket
    total_score = sum(bucket_scores.values())
    day_pnl = 0.0
    n_active = 0

    for bkt in mech_buckets:
        bname = bkt["name"]
        if bname not in bucket_scores:
            continue
        weight = bucket_scores[bname] / total_score
        risk_alloc = DAILY_RISK * weight
        pnl, oc = simulate_day_dollar(row, bkt["target_pct"], bkt["time_stop"],
                                       bkt["n_t"], bkt["cps"], risk_alloc)
        day_pnl += pnl
        n_active += 1

    opt_c_daily_pnl.append(day_pnl)
    opt_c_daily_trades.append(n_active)

opt_c_arr = np.array(opt_c_daily_pnl)


# ════════════════════════════════════════════════════════════════════════════
# COMPARISON
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  RESULTS COMPARISON")
print(f"{'='*120}\n")

def compute_stats(pnls, name, dates):
    """Compute comprehensive stats for a P&L series."""
    arr = np.array(pnls)
    n_days = len(arr)
    n_traded = np.sum(arr != 0)
    tot = arr.sum()
    avg = tot / n_traded if n_traded > 0 else 0
    traded_pnls = arr[arr != 0]
    wr = (traded_pnls > 0).mean() * 100 if len(traded_pnls) > 0 else 0
    w = traded_pnls[traded_pnls > 0].sum() if len(traded_pnls) > 0 else 0
    l = abs(traded_pnls[traded_pnls < 0].sum()) if len(traded_pnls) > 0 else 0
    pf = w / l if l > 0 else 99

    # Equity curve + drawdown
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd_curve = cum - peak
    max_dd = dd_curve.min()
    calmar = tot / abs(max_dd) if max_dd < 0 else 0

    # Sharpe (annualized, 252 trading days)
    if arr.std() > 0:
        sharpe = (arr.mean() / arr.std()) * np.sqrt(252)
    else:
        sharpe = 0

    # Monthly breakdown
    date_arr = dates.values
    monthly = defaultdict(float)
    for i in range(len(arr)):
        m = pd.Timestamp(date_arr[i]).strftime("%Y-%m")
        monthly[m] += arr[i]
    monthly_pnls = list(monthly.values())
    pct_profitable_months = (np.array(monthly_pnls) > 0).mean() * 100 if len(monthly_pnls) > 0 else 0

    # Split-half robustness
    mid = n_days // 2
    h1 = arr[:mid]
    h2 = arr[mid:]
    h1_w = h1[h1>0].sum()
    h1_l = abs(h1[h1<0].sum())
    h2_w = h2[h2>0].sum()
    h2_l = abs(h2[h2<0].sum())
    h1_pf = h1_w / h1_l if h1_l > 0 else 99
    h2_pf = h2_w / h2_l if h2_l > 0 else 99

    # Worst day, best day
    worst_day = arr.min()
    best_day = arr.max()

    # Consecutive losses
    is_loss = arr < 0
    max_consec_loss = 0
    curr = 0
    for x in is_loss:
        if x:
            curr += 1
            max_consec_loss = max(max_consec_loss, curr)
        else:
            curr = 0

    return {
        "name": name,
        "n_days": n_days,
        "n_traded": int(n_traded),
        "total_pnl": tot,
        "avg_pnl_traded": avg,
        "win_rate": wr,
        "profit_factor": pf,
        "max_dd": max_dd,
        "calmar": calmar,
        "sharpe": sharpe,
        "pct_profitable_months": pct_profitable_months,
        "n_months": len(monthly_pnls),
        "h1_pf": h1_pf,
        "h2_pf": h2_pf,
        "robust": h1_pf > 1.0 and h2_pf > 1.0,
        "worst_day": worst_day,
        "best_day": best_day,
        "max_consec_losses": max_consec_loss,
    }


stats_a = compute_stats(opt_a_daily_pnl, "Option A: Strategy Families", go["date"])
stats_b = compute_stats(opt_b_daily_pnl, "Option B: Confluence Sizing", go["date"])
stats_c = compute_stats(opt_c_daily_pnl, "Option C: Multi-Mechanics Buckets", go["date"])

# Print comparison table
header = f"  {'Metric':<30} {'Option A':>18} {'Option B':>18} {'Option C':>18}"
sep = f"  {'─'*84}"

print(header)
print(sep)

rows = [
    ("Days Traded",          f"{stats_a['n_traded']:,}", f"{stats_b['n_traded']:,}", f"{stats_c['n_traded']:,}"),
    ("Total P&L",            f"${stats_a['total_pnl']:>+14,.0f}", f"${stats_b['total_pnl']:>+14,.0f}", f"${stats_c['total_pnl']:>+14,.0f}"),
    ("Avg P&L (traded days)",f"${stats_a['avg_pnl_traded']:>+14,.0f}", f"${stats_b['avg_pnl_traded']:>+14,.0f}", f"${stats_c['avg_pnl_traded']:>+14,.0f}"),
    ("Win Rate",             f"{stats_a['win_rate']:.1f}%", f"{stats_b['win_rate']:.1f}%", f"{stats_c['win_rate']:.1f}%"),
    ("Profit Factor",        f"{stats_a['profit_factor']:.2f}", f"{stats_b['profit_factor']:.2f}", f"{stats_c['profit_factor']:.2f}"),
    ("Max Drawdown",         f"${stats_a['max_dd']:>+14,.0f}", f"${stats_b['max_dd']:>+14,.0f}", f"${stats_c['max_dd']:>+14,.0f}"),
    ("Calmar Ratio",         f"{stats_a['calmar']:.2f}", f"{stats_b['calmar']:.2f}", f"{stats_c['calmar']:.2f}"),
    ("Sharpe Ratio",         f"{stats_a['sharpe']:.2f}", f"{stats_b['sharpe']:.2f}", f"{stats_c['sharpe']:.2f}"),
    ("Profitable Months",    f"{stats_a['pct_profitable_months']:.0f}% ({stats_a['n_months']}mo)",
                             f"{stats_b['pct_profitable_months']:.0f}% ({stats_b['n_months']}mo)",
                             f"{stats_c['pct_profitable_months']:.0f}% ({stats_c['n_months']}mo)"),
    ("H1 PF / H2 PF",       f"{stats_a['h1_pf']:.2f} / {stats_a['h2_pf']:.2f}",
                             f"{stats_b['h1_pf']:.2f} / {stats_b['h2_pf']:.2f}",
                             f"{stats_c['h1_pf']:.2f} / {stats_c['h2_pf']:.2f}"),
    ("Robust (both halves)", f"{'✓' if stats_a['robust'] else '✗'}", f"{'✓' if stats_b['robust'] else '✗'}", f"{'✓' if stats_c['robust'] else '✗'}"),
    ("Best Day",             f"${stats_a['best_day']:>+14,.0f}", f"${stats_b['best_day']:>+14,.0f}", f"${stats_c['best_day']:>+14,.0f}"),
    ("Worst Day",            f"${stats_a['worst_day']:>+14,.0f}", f"${stats_b['worst_day']:>+14,.0f}", f"${stats_c['worst_day']:>+14,.0f}"),
    ("Max Consec Losses",    f"{stats_a['max_consec_losses']}", f"{stats_b['max_consec_losses']}", f"{stats_c['max_consec_losses']}"),
]

for label, a, b, c in rows:
    print(f"  {label:<30} {a:>18} {b:>18} {c:>18}")


# ════════════════════════════════════════════════════════════════════════════
# MONTHLY P&L BREAKDOWN
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  MONTHLY P&L BREAKDOWN")
print(f"{'='*120}\n")

monthly_a = defaultdict(float)
monthly_b = defaultdict(float)
monthly_c = defaultdict(float)
for i in range(len(go)):
    m = go.iloc[i]["date"].strftime("%Y-%m")
    monthly_a[m] += opt_a_daily_pnl[i]
    monthly_b[m] += opt_b_daily_pnl[i]
    monthly_c[m] += opt_c_daily_pnl[i]

all_months = sorted(set(list(monthly_a.keys()) + list(monthly_b.keys()) + list(monthly_c.keys())))

print(f"  {'Month':<10} {'Option A':>14} {'Option B':>14} {'Option C':>14} {'Best':>10}")
print(f"  {'─'*65}")
for m in all_months:
    a = monthly_a.get(m, 0)
    b = monthly_b.get(m, 0)
    c = monthly_c.get(m, 0)
    best_val = max(a, b, c)
    best = "A" if a == best_val else ("B" if b == best_val else "C")
    print(f"  {m:<10} ${a:>+13,.0f} ${b:>+13,.0f} ${c:>+13,.0f} {'← '+best:>10}")


# ════════════════════════════════════════════════════════════════════════════
# EQUITY CURVE DATA (for later plotting)
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  EQUITY CURVES")
print(f"{'='*120}\n")

cum_a = np.cumsum(opt_a_arr)
cum_b = np.cumsum(opt_b_arr)
cum_c = np.cumsum(opt_c_arr)

# Print key milestones
milestones = [50, 100, 200, 300, 400, 500, len(go)-1]
print(f"  {'Day':>6} {'Date':<12} {'Option A':>14} {'Option B':>14} {'Option C':>14}")
print(f"  {'─'*60}")
for d in milestones:
    if d < len(go):
        dt = go.iloc[d]["date"].strftime("%Y-%m-%d")
        print(f"  {d:>6} {dt:<12} ${cum_a[d]:>+13,.0f} ${cum_b[d]:>+13,.0f} ${cum_c[d]:>+13,.0f}")

# Save equity curve CSV
eq_df = pd.DataFrame({
    "date": go["date"].values,
    "opt_a_daily": opt_a_daily_pnl,
    "opt_b_daily": opt_b_daily_pnl,
    "opt_c_daily": opt_c_daily_pnl,
    "opt_a_cumulative": cum_a,
    "opt_b_cumulative": cum_b,
    "opt_c_cumulative": cum_c,
})
eq_path = os.path.join(_DIR, "ensemble_equity_curves.csv")
eq_df.to_csv(eq_path, index=False)
print(f"\n  ✓ Equity curves saved → {eq_path}")


# ════════════════════════════════════════════════════════════════════════════
# WINNER DETERMINATION
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  VERDICT")
print(f"{'='*120}\n")

# Score each option
scores = {
    "A": 0, "B": 0, "C": 0
}

# Total P&L (most important)
rank_pnl = sorted([(stats_a["total_pnl"], "A"), (stats_b["total_pnl"], "B"), (stats_c["total_pnl"], "C")], reverse=True)
scores[rank_pnl[0][1]] += 3
scores[rank_pnl[1][1]] += 2
scores[rank_pnl[2][1]] += 1

# Calmar
rank_cal = sorted([(stats_a["calmar"], "A"), (stats_b["calmar"], "B"), (stats_c["calmar"], "C")], reverse=True)
scores[rank_cal[0][1]] += 3
scores[rank_cal[1][1]] += 2
scores[rank_cal[2][1]] += 1

# Sharpe
rank_sh = sorted([(stats_a["sharpe"], "A"), (stats_b["sharpe"], "B"), (stats_c["sharpe"], "C")], reverse=True)
scores[rank_sh[0][1]] += 3
scores[rank_sh[1][1]] += 2
scores[rank_sh[2][1]] += 1

# Max DD (less negative = better)
rank_dd = sorted([(stats_a["max_dd"], "A"), (stats_b["max_dd"], "B"), (stats_c["max_dd"], "C")], reverse=True)
scores[rank_dd[0][1]] += 3
scores[rank_dd[1][1]] += 2
scores[rank_dd[2][1]] += 1

# Robustness
for s, lbl in [(stats_a, "A"), (stats_b, "B"), (stats_c, "C")]:
    if s["robust"]:
        scores[lbl] += 2

# Profit Factor
rank_pf = sorted([(stats_a["profit_factor"], "A"), (stats_b["profit_factor"], "B"), (stats_c["profit_factor"], "C")], reverse=True)
scores[rank_pf[0][1]] += 2
scores[rank_pf[1][1]] += 1

print(f"  Scoring (Total P&L, Calmar, Sharpe, Max DD, Robustness, PF):")
print(f"    Option A: {scores['A']} pts")
print(f"    Option B: {scores['B']} pts")
print(f"    Option C: {scores['C']} pts")

winner = max(scores, key=scores.get)
print(f"\n  🏆 WINNER: Option {winner}")

names = {"A": "Strategy Families", "B": "Confluence Sizing", "C": "Multi-Mechanics Buckets"}
stats_map = {"A": stats_a, "B": stats_b, "C": stats_c}
ws = stats_map[winner]
print(f"     {names[winner]}")
print(f"     Total: ${ws['total_pnl']:+,.0f}  |  PF: {ws['profit_factor']:.2f}  |  "
      f"Calmar: {ws['calmar']:.2f}  |  Sharpe: {ws['sharpe']:.2f}  |  DD: ${ws['max_dd']:+,.0f}")

print(f"\n{'='*120}")
print("  DONE")
print(f"{'='*120}")
