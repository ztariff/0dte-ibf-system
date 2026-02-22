"""
SIGNAL CATALOG + CONFLUENCE SYSTEM
====================================
1. Generate every viable parameter set (filter combo × mechanics × tranches)
2. Label each set (e.g. SET_001, SET_002, ...)
3. Compute per-set stats (PF, WR, total $, DD, Calmar, n_trades)
4. For each historical day, record which sets would have fired
5. Analyze: does confluence (more sets firing) = better results?
6. Build a sizing model: more confluence → bigger position
7. Save everything to JSON + CSV for production use

Usage:
    python3 build_signal_catalog.py | tee signal_catalog_results.txt
"""

import os, json
import pandas as pd
import numpy as np
from itertools import combinations
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.reset_index(drop=True)

N = len(go)
print(f"{'='*160}")
print(f"  SIGNAL CATALOG + CONFLUENCE SYSTEM — {N} TRADES")
print(f"  {go['date'].min().strftime('%Y-%m-%d')} to {go['date'].max().strftime('%Y-%m-%d')}")
print(f"{'='*160}")

# ════════════════════════════════════════════════════════════════════════════
# STEP 1: Define all filter combinations
# ════════════════════════════════════════════════════════════════════════════

# Atomic filters — each is a boolean mask on go
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

# ════════════════════════════════════════════════════════════════════════════
# STEP 2: Generate filter combos (2-filter and 3-filter)
# ════════════════════════════════════════════════════════════════════════════
print(f"\n  Generating filter combinations...", flush=True)

# Key filters to use in combos (avoid redundant pairs like VP≤1.3 + VP≤1.5)
combo_filters_2 = list(atomic_filters.keys())
combo_filters_3 = [
    "VIX≤16", "VIX≤17", "VIX≤18", "VIX≤20",
    "VP≤1.0", "VP≤1.2", "VP≤1.3", "VP≤1.5", "VP≤1.7",
    "STABLE", "!RISING", "FLAT_vwap", "Rng≤0.4", "Rng≤0.6",
]
if "prior_day_return" in go.columns:
    combo_filters_3 += [
        "PrDayDn", "PrDayUp", "PrRng≤1.0",
        "InWkRng", "OutWkRng", "WkTop50",
        "5dRet>0", "5dRet>1", "5dRet<0",
        "PrRV<12", "PrRV<15", "RVchg<0", "RVchg>0",
    ]

filter_combos = {}

# Single filters
for name, mask in atomic_filters.items():
    filter_combos[name] = mask

# 2-filter combos
for n1, n2 in combinations(combo_filters_2, 2):
    # Skip redundant pairs (both VIX or both VP at different thresholds)
    if n1.startswith("VIX") and n2.startswith("VIX"): continue
    if n1.startswith("VP") and n2.startswith("VP"): continue
    if n1.startswith("5dRet") and n2.startswith("5dRet"): continue
    if n1.startswith("PrRV") and n2.startswith("PrRV"): continue
    if n1.startswith("Rng") and n2.startswith("Rng"): continue
    if n1.startswith("PrRng") and n2.startswith("PrRng"): continue
    combined = atomic_filters[n1] & atomic_filters[n2]
    n_trades = combined.sum()
    if n_trades >= 10:
        filter_combos[f"{n1} + {n2}"] = combined

# 3-filter combos
for n1, n2, n3 in combinations(combo_filters_3, 3):
    if n1.startswith("VIX") and n2.startswith("VIX"): continue
    if n1.startswith("VIX") and n3.startswith("VIX"): continue
    if n2.startswith("VIX") and n3.startswith("VIX"): continue
    if n1.startswith("VP") and n2.startswith("VP"): continue
    if n1.startswith("VP") and n3.startswith("VP"): continue
    if n2.startswith("VP") and n3.startswith("VP"): continue
    if n1.startswith("5dRet") and n2.startswith("5dRet"): continue
    if n1.startswith("5dRet") and n3.startswith("5dRet"): continue
    if n2.startswith("5dRet") and n3.startswith("5dRet"): continue
    combined = atomic_filters[n1] & atomic_filters[n2] & atomic_filters[n3]
    n_trades = combined.sum()
    if n_trades >= 8:
        filter_combos[f"{n1} + {n2} + {n3}"] = combined

print(f"  Generated {len(filter_combos)} filter combos")

# ════════════════════════════════════════════════════════════════════════════
# STEP 3: Define mechanics variations
# ════════════════════════════════════════════════════════════════════════════
# Checkpoints for tranche simulation
CHECKPOINTS_30M = [
    ("1030", 30), ("1100", 60), ("1130", 90),
    ("1200", 120), ("1230", 150), ("1300", 180),
    ("1330", 210), ("1400", 240), ("1430", 270),
    ("1500", 300), ("1530", 330), ("1545", 345),
]
CHECKPOINTS_60M = [
    ("1100", 60), ("1200", 120), ("1300", 180),
    ("1400", 240), ("1500", 300),
]

TS_HM = {
    "1030": (10,30), "1100": (11,0), "1130": (11,30),
    "1200": (12,0), "1230": (12,30), "1300": (13,0),
    "1330": (13,30), "1400": (14,0), "1430": (14,30),
    "1500": (15,0), "1530": (15,30), "1545": (15,45),
    "close": (16,0),
}

SPX_MULT = 100
SLIPPAGE_PER_SPREAD = 1.00
TRANCHE_RISK = 25_000

mechanics_list = [
    # (target_pct, time_stop, n_tranches, interval, interval_checkpoints)
    (50, "close", 1, "1T", []),
    (50, "close", 3, "3T/60m", CHECKPOINTS_60M),
    (50, "close", 5, "5T/60m", CHECKPOINTS_60M),
    (50, "close", 5, "5T/30m", CHECKPOINTS_30M),
    (50, "1530", 1, "1T", []),
    (50, "1530", 3, "3T/60m", CHECKPOINTS_60M),
    (50, "1530", 5, "5T/60m", CHECKPOINTS_60M),
    (40, "close", 1, "1T", []),
    (40, "close", 3, "3T/60m", CHECKPOINTS_60M),
    (40, "close", 5, "5T/60m", CHECKPOINTS_60M),
    (70, "close", 1, "1T", []),
    (70, "close", 3, "3T/60m", CHECKPOINTS_60M),
    (70, "close", 5, "5T/60m", CHECKPOINTS_60M),
    (70, "1545", 1, "1T", []),
    (70, "1545", 5, "5T/60m", CHECKPOINTS_60M),
]

print(f"  {len(mechanics_list)} mechanics variations")


def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str) or t_str == "":
        return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except:
        return False


def find_exit_pnl(row, target_pct, time_stop, use_ws=True):
    """Find exit P&L per spread and outcome."""
    ts_hour, ts_min = TS_HM.get(time_stop, (16, 0))

    tgt_time = row.get(f"hit_{target_pct}_time", "")
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")
    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")

    events = []
    if use_ws and ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_hour, ts_min):
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


def simulate_day(row, target_pct, time_stop, n_tranches, interval_cps):
    """Simulate a single day with tranches. Returns total $ P&L."""
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None:
        return 0, "ND"

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

    # Size
    risk_per_spread = row.get("risk_deployed_p1", 0)
    n_sp = row.get("n_spreads_p1", 0)
    ml_ps = (risk_per_spread / n_sp) if (n_sp > 0 and risk_per_spread > 0) else TRANCHE_RISK
    if ml_ps <= 0: ml_ps = TRANCHE_RISK
    n_per = max(1, int(TRANCHE_RISK / ml_ps))

    # Tranche 1
    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    # Additional tranches
    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_cps): break
        cp_lbl, _ = interval_cps[k - 2]
        cp_pnl = row.get(f"pnl_at_{cp_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl): continue
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue
        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        total += tk_pnl

    return round(total, 0), outcome


# ════════════════════════════════════════════════════════════════════════════
# STEP 4: Evaluate all parameter sets
# ════════════════════════════════════════════════════════════════════════════
print(f"\n  Evaluating all parameter sets...", flush=True)

param_sets = []       # list of dicts with stats
set_day_map = {}      # set_id → set of date indices where it fires
day_set_map = defaultdict(list)  # date_idx → list of set_ids that fire

set_id = 0
total_combos = len(filter_combos) * len(mechanics_list)
progress_interval = max(1, total_combos // 20)

combo_count = 0
for filt_name, mask in filter_combos.items():
    active_indices = go.index[mask].tolist()
    if len(active_indices) < 5:
        combo_count += len(mechanics_list)
        continue

    for tgt, ts, n_t, interval_label, interval_cps in mechanics_list:
        combo_count += 1
        if combo_count % progress_interval == 0:
            print(f"    {combo_count}/{total_combos} ({combo_count*100//total_combos}%)", flush=True)

        pnls = []
        trade_dates = []
        for idx in active_indices:
            row = go.iloc[idx]
            pnl, oc = simulate_day(row, tgt, ts, n_t, interval_cps)
            if oc != "ND":
                pnls.append(pnl)
                trade_dates.append(idx)

        n = len(pnls)
        if n < 5:
            continue

        pnl_arr = np.array(pnls)
        tot = pnl_arr.sum()
        wr = (pnl_arr > 0).mean() * 100
        w = pnl_arr[pnl_arr > 0].sum()
        l = abs(pnl_arr[pnl_arr < 0].sum())
        pf = w / l if l > 0 else 99
        cum = np.cumsum(pnl_arr)
        dd = (cum - np.maximum.accumulate(cum)).min()
        avg = tot / n
        calmar = tot / abs(dd) if dd < 0 else 0

        # Only keep sets with PF > 1.0 (they actually make money)
        if pf < 1.0:
            continue

        sid = f"SET_{set_id:04d}"
        param_sets.append({
            "set_id": sid,
            "filters": filt_name,
            "target_pct": tgt,
            "time_stop": ts,
            "tranches": interval_label,
            "n_trades": n,
            "total_pnl": round(tot, 0),
            "win_rate": round(wr, 1),
            "profit_factor": round(pf, 2),
            "max_dd": round(dd, 0),
            "avg_pnl": round(avg, 0),
            "calmar": round(calmar, 2),
        })

        set_day_map[sid] = set(trade_dates)
        for idx in trade_dates:
            day_set_map[idx].append(sid)

        set_id += 1

print(f"\n  ✓ {len(param_sets)} profitable parameter sets (PF > 1.0)")

# ════════════════════════════════════════════════════════════════════════════
# STEP 5: Tier the parameter sets
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  PARAMETER SET TIERS")
print(f"{'='*160}")

# Tier by robustness: split-half test
mid = go["date"].median()
h1_idx = set(go[go["date"] <= mid].index.tolist())
h2_idx = set(go[go["date"] > mid].index.tolist())

for ps in param_sets:
    sid = ps["set_id"]
    days = set_day_map[sid]
    h1_days = days & h1_idx
    h2_days = days & h2_idx

    h1_pnls = [simulate_day(go.iloc[i], ps["target_pct"], ps["time_stop"],
                int(ps["tranches"].split("T")[0]),
                CHECKPOINTS_60M if "60m" in ps["tranches"] else CHECKPOINTS_30M if "30m" in ps["tranches"] else [])[0]
               for i in h1_days]
    h2_pnls = [simulate_day(go.iloc[i], ps["target_pct"], ps["time_stop"],
                int(ps["tranches"].split("T")[0]),
                CHECKPOINTS_60M if "60m" in ps["tranches"] else CHECKPOINTS_30M if "30m" in ps["tranches"] else [])[0]
               for i in h2_days]

    h1_pf = sum(p for p in h1_pnls if p > 0) / abs(sum(p for p in h1_pnls if p < 0)) if any(p < 0 for p in h1_pnls) else 99
    h2_pf = sum(p for p in h2_pnls if p > 0) / abs(sum(p for p in h2_pnls if p < 0)) if any(p < 0 for p in h2_pnls) else 99

    ps["h1_n"] = len(h1_pnls)
    ps["h2_n"] = len(h2_pnls)
    ps["h1_pf"] = round(h1_pf, 2)
    ps["h2_pf"] = round(h2_pf, 2)
    ps["robust"] = (h1_pf > 1.0 and h2_pf > 1.0 and len(h1_pnls) >= 3 and len(h2_pnls) >= 3)

    # Assign tier
    if ps["robust"] and ps["profit_factor"] >= 2.0 and ps["n_trades"] >= 15:
        ps["tier"] = "S"
    elif ps["robust"] and ps["profit_factor"] >= 1.5:
        ps["tier"] = "A"
    elif ps["robust"] and ps["profit_factor"] >= 1.2:
        ps["tier"] = "B"
    elif ps["profit_factor"] >= 1.2:
        ps["tier"] = "C"  # profitable but not robust
    else:
        ps["tier"] = "D"

tier_counts = defaultdict(int)
for ps in param_sets:
    tier_counts[ps["tier"]] += 1

for t in ["S", "A", "B", "C", "D"]:
    print(f"  Tier {t}: {tier_counts[t]} sets")

# Print top sets per tier
for tier in ["S", "A", "B"]:
    tier_sets = [ps for ps in param_sets if ps["tier"] == tier]
    tier_sets.sort(key=lambda x: -x["calmar"])
    print(f"\n  ── Top 10 Tier {tier} sets (by Calmar) ──")
    print(f"  {'ID':<10} {'Filters':<45} {'Mech':<18} {'N':>4} {'Total $':>14} {'PF':>6} {'WR':>6} {'DD':>12} {'Calmar':>7} {'H1 PF':>6} {'H2 PF':>6}")
    print(f"  {'─'*145}")
    for ps in tier_sets[:10]:
        mech = f"{ps['target_pct']}%/{ps['time_stop']}/{ps['tranches']}"
        print(f"  {ps['set_id']:<10} {ps['filters']:<45} {mech:<18} {ps['n_trades']:>4} ${ps['total_pnl']:>+13,.0f} {ps['profit_factor']:>5.2f} {ps['win_rate']:>5.1f}% ${ps['max_dd']:>+11,.0f} {ps['calmar']:>6.2f} {ps['h1_pf']:>5.2f} {ps['h2_pf']:>5.2f}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 6: Confluence analysis
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  CONFLUENCE ANALYSIS — Do more signals = better results?")
print(f"{'='*160}")

# For each day, count how many sets fire (by tier)
confluence_data = []
for idx in range(len(go)):
    row = go.iloc[idx]
    sets_firing = day_set_map.get(idx, [])

    # Count by tier
    tier_s = sum(1 for sid in sets_firing if next((ps for ps in param_sets if ps["set_id"] == sid), {}).get("tier") == "S")
    tier_a = sum(1 for sid in sets_firing if next((ps for ps in param_sets if ps["set_id"] == sid), {}).get("tier") == "A")
    tier_b = sum(1 for sid in sets_firing if next((ps for ps in param_sets if ps["set_id"] == sid), {}).get("tier") == "B")
    total_firing = len(sets_firing)

    confluence_data.append({
        "date": row["date"],
        "n_sets_total": total_firing,
        "n_tier_S": tier_s,
        "n_tier_A": tier_a,
        "n_tier_B": tier_b,
        "n_tier_SAB": tier_s + tier_a + tier_b,
    })

cdf = pd.DataFrame(confluence_data)

# Use a simple 1T/50%/close as the "base" P&L for each day
base_pnls = []
for idx in range(len(go)):
    row = go.iloc[idx]
    pnl, _ = simulate_day(row, 50, "close", 1, [])
    base_pnls.append(pnl)
cdf["base_pnl"] = base_pnls

# Analyze by confluence level
print(f"\n  Base 1T/50%/close P&L by total sets firing:")
print(f"  {'Sets Firing':>12} {'Days':>6} {'WR':>7} {'Avg PnL':>11} {'Total':>14} {'PF':>6}")
print(f"  {'─'*65}")
for lo, hi in [(0,1),(1,10),(10,50),(50,100),(100,200),(200,500),(500,9999)]:
    mask = (cdf["n_sets_total"] >= lo) & (cdf["n_sets_total"] < hi)
    sub = cdf[mask]
    if len(sub) < 3: continue
    w = sub[sub["base_pnl"]>0]["base_pnl"].sum()
    l = abs(sub[sub["base_pnl"]<0]["base_pnl"].sum())
    pf = w/l if l > 0 else 99
    print(f"  {f'[{lo}-{hi})':>12} {len(sub):>6} {(sub['base_pnl']>0).mean()*100:>6.1f}% ${sub['base_pnl'].mean():>+10,.0f} ${sub['base_pnl'].sum():>+13,.0f} {pf:>5.2f}")

print(f"\n  Base P&L by Tier S sets firing:")
print(f"  {'S Sets':>12} {'Days':>6} {'WR':>7} {'Avg PnL':>11} {'Total':>14} {'PF':>6}")
print(f"  {'─'*65}")
for n_s in range(0, min(20, cdf["n_tier_S"].max()+1)):
    sub = cdf[cdf["n_tier_S"] == n_s]
    if len(sub) < 3: continue
    w = sub[sub["base_pnl"]>0]["base_pnl"].sum()
    l = abs(sub[sub["base_pnl"]<0]["base_pnl"].sum())
    pf = w/l if l > 0 else 99
    print(f"  {n_s:>12} {len(sub):>6} {(sub['base_pnl']>0).mean()*100:>6.1f}% ${sub['base_pnl'].mean():>+10,.0f} ${sub['base_pnl'].sum():>+13,.0f} {pf:>5.2f}")

# Bucketized Tier S+A+B
print(f"\n  Base P&L by Tier S+A+B sets firing:")
print(f"  {'SAB Sets':>12} {'Days':>6} {'WR':>7} {'Avg PnL':>11} {'Total':>14} {'PF':>6}")
print(f"  {'─'*65}")
for lo, hi in [(0,1),(1,5),(5,20),(20,50),(50,100),(100,300),(300,9999)]:
    mask = (cdf["n_tier_SAB"] >= lo) & (cdf["n_tier_SAB"] < hi)
    sub = cdf[mask]
    if len(sub) < 3: continue
    w = sub[sub["base_pnl"]>0]["base_pnl"].sum()
    l = abs(sub[sub["base_pnl"]<0]["base_pnl"].sum())
    pf = w/l if l > 0 else 99
    print(f"  {f'[{lo}-{hi})':>12} {len(sub):>6} {(sub['base_pnl']>0).mean()*100:>6.1f}% ${sub['base_pnl'].mean():>+10,.0f} ${sub['base_pnl'].sum():>+13,.0f} {pf:>5.2f}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 7: Sizing model simulation
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  SIZING MODEL — Scale position by confluence")
print(f"{'='*160}")

# Model: base size = 1x for any signal, +0.5x per tier-S set, +0.25x per tier-A set, etc.
# Then simulate P&L with dynamic sizing

sizing_models = [
    ("Flat 1x (no scaling)", lambda s, a, b: 1.0),
    ("1x base + 0.5x per S", lambda s, a, b: 1.0 + 0.5 * s),
    ("1x base + 0.5x per S + 0.25x per A", lambda s, a, b: 1.0 + 0.5 * s + 0.25 * a),
    ("S-only: 1x if S>0 else skip", lambda s, a, b: 1.0 if s > 0 else 0),
    ("Tiered: 0.5x if SAB<10, 1x if 10-50, 2x if 50+", lambda s, a, b: 0.5 if (s+a+b) < 10 else (1.0 if (s+a+b) < 50 else 2.0)),
    ("Confidence: skip if SAB<5, 1x if 5-20, 1.5x if 20-100, 2x if 100+",
     lambda s, a, b: 0 if (s+a+b) < 5 else (1.0 if (s+a+b) < 20 else (1.5 if (s+a+b) < 100 else 2.0))),
]

print(f"\n  {'Model':<60} {'Trades':>6} {'Total $':>14} {'WR':>7} {'PF':>7} {'MaxDD':>13} {'Avg':>11}")
print(f"  {'─'*130}")

for model_name, size_fn in sizing_models:
    scaled_pnls = []
    for idx in range(len(go)):
        cd = confluence_data[idx]
        scale = size_fn(cd["n_tier_S"], cd["n_tier_A"], cd["n_tier_B"])
        if scale <= 0:
            continue
        pnl = base_pnls[idx] * scale
        scaled_pnls.append(pnl)

    if len(scaled_pnls) < 5:
        print(f"  {model_name:<60} {'<5 trades':>6}")
        continue

    arr = np.array(scaled_pnls)
    n = len(arr)
    tot = arr.sum()
    wr = (arr > 0).mean() * 100
    w = arr[arr > 0].sum()
    l = abs(arr[arr < 0].sum())
    pf = w / l if l > 0 else 99
    cum = np.cumsum(arr)
    dd = (cum - np.maximum.accumulate(cum)).min()

    print(f"  {model_name:<60} {n:>6} ${tot:>+13,.0f} {wr:>6.1f}% {pf:>6.2f} ${dd:>+12,.0f} ${tot/n:>+10,.0f}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 8: Save everything
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  SAVING CATALOG")
print(f"{'='*160}")

# Save parameter sets to JSON
catalog_path = os.path.join(_DIR, "signal_catalog.json")
with open(catalog_path, "w") as f:
    json.dump(param_sets, f, indent=2, default=str)
print(f"  ✓ {len(param_sets)} parameter sets → {catalog_path}")

# Save parameter sets to CSV for easy viewing
catalog_csv = os.path.join(_DIR, "signal_catalog.csv")
pd.DataFrame(param_sets).to_csv(catalog_csv, index=False)
print(f"  ✓ CSV → {catalog_csv}")

# Save confluence per day
conf_csv = os.path.join(_DIR, "confluence_by_day.csv")
cdf.to_csv(conf_csv, index=False)
print(f"  ✓ Daily confluence → {conf_csv}")

# Summary
robust_sets = [ps for ps in param_sets if ps["robust"]]
print(f"\n  SUMMARY:")
print(f"    Total profitable sets (PF>1):  {len(param_sets)}")
print(f"    Robust sets (PF>1 both halves): {len(robust_sets)}")
print(f"    Tier S: {tier_counts['S']}  |  Tier A: {tier_counts['A']}  |  Tier B: {tier_counts['B']}  |  Tier C: {tier_counts['C']}  |  Tier D: {tier_counts['D']}")

print(f"\n{'='*160}")
print(f"  DONE")
print(f"{'='*160}")
