"""
ENSEMBLE BACKTEST V3 — Concentrated Signal Model
==================================================
Instead of spreading $100K thin across hundreds of groups:
1. Use top S/A-tier Jaccard clusters as go/no-go filters
2. Only trade on days when N+ distinct top signals agree
3. Deploy concentrated capital on ONE position using best mechanics
4. Size by confluence: more signals agreeing = larger position

Tests multiple configurations:
  - Min signals required: 1, 2, 3, 4, 5
  - Sizing models: flat $100K, scaled by count, tiered
  - Mechanics: best overall (50%/close/5T/60m) vs adaptive
  - Signal pool: top 5, 10, 15, 20, 30 groups

Usage:
    python3 ensemble_v3.py | tee ensemble_v3_results.txt
"""

import os, json
import pandas as pd
import numpy as np
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))

# ════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ════════════════════════════════════════════════════════════════════════════
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)
N = len(go)

with open(os.path.join(_DIR, "signal_catalog.json")) as f:
    catalog = json.load(f)

print(f"  {N} GO trades, {len(catalog)} parameter sets")

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
CHECKPOINTS_60M = [("1100",60),("1200",120),("1300",180),("1400",240),("1500",300)]
CHECKPOINTS_30M = [("1030",30),("1100",60),("1130",90),("1200",120),("1230",150),
                   ("1300",180),("1330",210),("1400",240),("1430",270),("1500",300),
                   ("1530",330),("1545",345)]

# ════════════════════════════════════════════════════════════════════════════
# FILTER MASKS
# ════════════════════════════════════════════════════════════════════════════
atomic_filters = {
    "VIX≤15": go["vix"]<=15, "VIX≤16": go["vix"]<=16, "VIX≤17": go["vix"]<=17,
    "VIX≤18": go["vix"]<=18, "VIX≤20": go["vix"]<=20,
    "VP≤1.0": go["vp_ratio"]<=1.0, "VP≤1.2": go["vp_ratio"]<=1.2,
    "VP≤1.3": go["vp_ratio"]<=1.3, "VP≤1.5": go["vp_ratio"]<=1.5,
    "VP≤1.7": go["vp_ratio"]<=1.7, "VP≤2.0": go["vp_ratio"]<=2.0,
    "STABLE": go["rv_slope"]=="STABLE", "!RISING": go["rv_slope"]!="RISING",
    "FLAT_vwap": go["vwap_slope"]=="FLAT",
    "Rng≤0.3": go["range_pct"]<=0.3, "Rng≤0.4": go["range_pct"]<=0.4,
    "Rng≤0.6": go["range_pct"]<=0.6,
}
if "prior_day_return" in go.columns:
    atomic_filters.update({
        "PrDayDn": go["prior_day_direction"]=="DOWN", "PrDayUp": go["prior_day_direction"]=="UP",
        "PrRet<-1": go["prior_day_return"]<-1, "PrRet>0": go["prior_day_return"]>0,
        "PrRng≤0.8": go["prior_day_range"]<=0.8, "PrRng≤1.0": go["prior_day_range"]<=1.0,
        "InWkRng": go["in_prior_week_range"]==1, "OutWkRng": go["in_prior_week_range"]==0,
        "InMoRng": go["in_prior_month_range"]==1, "WkTop50": go["pct_in_weekly_range"]>=50,
        "5dRet>0": go["prior_5d_return"]>0, "5dRet>1": go["prior_5d_return"]>1,
        "5dRet<0": go["prior_5d_return"]<0,
        "PrRV<12": go["prior_day_rv"]<12, "PrRV<15": go["prior_day_rv"]<15,
        "RVchg<0": go["rv_1d_change"]<0, "RVchg>0": go["rv_1d_change"]>0,
    })

def parse_filter_mask(filter_name):
    parts = [p.strip() for p in filter_name.split(" + ")]
    mask = pd.Series(True, index=go.index)
    for p in parts:
        if p in atomic_filters: mask = mask & atomic_filters[p]
        else: return None
    return mask

# ════════════════════════════════════════════════════════════════════════════
# SIMULATION HELPERS
# ════════════════════════════════════════════════════════════════════════════
def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str) or t_str == "": return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except: return False

def find_exit_pnl(row, target_pct, time_stop):
    ts_hour, ts_min = TS_HM.get(time_stop, (16,0))
    tgt_time = row.get(f"hit_{target_pct}_time", "")
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")
    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")
    events = []
    if ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_hour, ts_min):
        try: events.append(("WS", pd.Timestamp(ws_time), ws_pnl))
        except: pass
    if pd.notna(tgt_pnl) and tgt_time and time_before(tgt_time, ts_hour, ts_min):
        try: events.append(("TGT", pd.Timestamp(tgt_time), tgt_pnl))
        except: pass
    if events:
        events.sort(key=lambda x: x[1])
        return events[0][2], events[0][0]
    ts_col = f"pnl_at_{time_stop}" if time_stop != "close" else "pnl_at_close"
    ts_pnl = row.get(ts_col)
    if pd.notna(ts_pnl): return ts_pnl, "TS"
    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl): return close_pnl, "EXP"
    return None, "ND"

def simulate_day_dollar(row, target_pct, time_stop, n_tranches, interval_cps, risk_allocated):
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0, "ND"
    if outcome == "WS": exit_time_str = row.get("ws_time", "")
    elif outcome == "TGT": exit_time_str = row.get(f"hit_{target_pct}_time", "")
    else: exit_time_str = time_stop
    if exit_time_str in TS_HM: exit_h, exit_m = TS_HM[exit_time_str]
    else:
        try:
            et = pd.Timestamp(exit_time_str); exit_h, exit_m = et.hour, et.minute
        except: exit_h, exit_m = 16, 0

    risk_per_spread = row.get("risk_deployed_p1", 0)
    n_sp = row.get("n_spreads_p1", 0)
    ml_ps = (risk_per_spread / n_sp) if (n_sp > 0 and risk_per_spread > 0) else TRANCHE_RISK
    if ml_ps <= 0: ml_ps = TRANCHE_RISK
    risk_per_tranche = risk_allocated / max(1, n_tranches)
    n_per = max(1, int(risk_per_tranche / ml_ps))

    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_cps): break
        cp_lbl, _ = interval_cps[k - 2]
        cp_pnl = row.get(f"pnl_at_{cp_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl): continue
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue
        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        total += tk_pnl
    return round(total, 2), outcome

def get_interval_cps(label):
    if "60m" in label: return CHECKPOINTS_60M
    elif "30m" in label: return CHECKPOINTS_30M
    return []

def get_n_tranches(label):
    try: return int(label.split("T")[0])
    except: return 1

# ════════════════════════════════════════════════════════════════════════════
# STEP 1: Build signal groups via Jaccard clustering (70% threshold)
# ════════════════════════════════════════════════════════════════════════════
print("\nStep 1: Building Jaccard-clustered signal groups...", flush=True)

filter_groups = defaultdict(list)
for ps in catalog:
    filter_groups[ps["filters"]].append(ps)

clusters_raw = []
for filt_name, group in filter_groups.items():
    mask = parse_filter_mask(filt_name)
    if mask is None: continue
    fire_days = frozenset(go.index[mask].tolist())
    if len(fire_days) < 5: continue

    # Pick best mechanics
    robust = [ps for ps in group if ps.get("robust") == True or ps.get("robust") == "True"]
    sa_robust = [ps for ps in robust if ps["tier"] in ("S", "A")]
    sab_robust = [ps for ps in robust if ps["tier"] in ("S", "A", "B")]
    if sa_robust: best = max(sa_robust, key=lambda x: x["calmar"])
    elif sab_robust: best = max(sab_robust, key=lambda x: x["calmar"])
    elif robust: best = max(robust, key=lambda x: x["calmar"])
    else: best = max(group, key=lambda x: x["calmar"])
    if best["profit_factor"] < 1.2: continue

    clusters_raw.append({
        "name": filt_name, "fire_days": fire_days, "n_fire": len(fire_days),
        "best_set": best, "tier": best["tier"], "pf": best["profit_factor"],
        "calmar": best["calmar"], "target_pct": best["target_pct"],
        "time_stop": best["time_stop"], "tranches": best["tranches"],
    })

tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
clusters_raw.sort(key=lambda x: (tier_order.get(x["tier"], 5), -x["calmar"]))

def jaccard(s1, s2):
    if len(s1) == 0 and len(s2) == 0: return 1.0
    return len(s1 & s2) / len(s1 | s2)

def cluster_by_jaccard(clusters_list, threshold):
    groups = []
    for cl in clusters_list:
        merged = False
        for grp in groups:
            if jaccard(cl["fire_days"], grp["fire_days"]) >= threshold:
                grp["members"].append(cl)
                grp["fire_days"] = grp["fire_days"] | cl["fire_days"]
                merged = True
                break
        if not merged:
            groups.append({"leader": cl, "members": [cl], "fire_days": set(cl["fire_days"])})
    return groups

groups_70 = cluster_by_jaccard(clusters_raw, 0.70)
print(f"  {len(groups_70)} distinct groups at J=70%")

# Rank groups by leader quality
for grp in groups_70:
    l = grp["leader"]
    grp["rank_score"] = tier_order.get(l["tier"], 5) * -100 + l["calmar"]

groups_70.sort(key=lambda x: -x["rank_score"])

print(f"\n  Top 30 signal groups:")
print(f"  {'#':>3} {'Filter':>45} {'Tier':>5} {'PF':>6} {'Cal':>7} {'Fires':>6} {'Mechanics':>20}")
print(f"  {'─'*100}")
for i, grp in enumerate(groups_70[:30]):
    l = grp["leader"]
    mech = f"{l['target_pct']}%/{l['time_stop']}/{l['tranches']}"
    print(f"  {i+1:>3} {l['name']:>45} {l['tier']:>5} {l['pf']:>5.2f} {l['calmar']:>6.2f} {len(grp['fire_days']):>5}d {mech:>20}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 2: For each day, count how many top-N groups fire
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("Step 2: Precomputing daily signal counts for various pool sizes...")
print(f"{'='*120}", flush=True)

pool_sizes = [5, 10, 15, 20, 30]

# For each pool size, precompute per-day firing count
# day_signals[pool_size][day_idx] = number of groups in top-N that fire
day_signals = {}
for ps in pool_sizes:
    top_groups = groups_70[:ps]
    counts = []
    for idx in range(N):
        n_fire = sum(1 for grp in top_groups if idx in grp["fire_days"])
        counts.append(n_fire)
    day_signals[ps] = counts
    avg_fire = np.mean([c for c in counts if c > 0])
    pct_any = sum(1 for c in counts if c > 0) / N * 100
    print(f"  Pool {ps:>2}: avg {avg_fire:.1f} signals/day when active, {pct_any:.0f}% of days have ≥1")


# ════════════════════════════════════════════════════════════════════════════
# STEP 3: Define mechanics options
# ════════════════════════════════════════════════════════════════════════════
# We'll test: fixed best mechanics, and adaptive (use the mechanic of the
# highest-ranked group that fires that day)

MECH_CONFIGS = {
    "50%/close/5T60m": (50, "close", 5, CHECKPOINTS_60M),     # Our best overall
    "50%/close/1T":    (50, "close", 1, []),                   # Simplest
    "70%/close/5T60m": (70, "close", 5, CHECKPOINTS_60M),     # Aggressive target
    "40%/close/5T60m": (40, "close", 5, CHECKPOINTS_60M),     # Conservative target
    "adaptive":        None,  # Use highest-ranked firing group's mechanics
}


# ════════════════════════════════════════════════════════════════════════════
# STEP 4: Run all configurations
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("Step 3: Running all configurations...")
print(f"{'='*120}", flush=True)

def compute_stats(arr):
    n_traded = int(np.sum(arr != 0))
    if n_traded == 0:
        return {"n_traded": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0,
                "profit_factor": 0, "max_dd": 0, "calmar": 0, "sharpe": 0,
                "h1_pf": 0, "h2_pf": 0, "robust": False, "pct_mo": 0,
                "worst_day": 0, "best_day": 0, "max_cl": 0}
    traded = arr[arr != 0]
    tot = arr.sum()
    wr = (traded > 0).mean() * 100
    w = traded[traded > 0].sum()
    l = abs(traded[traded < 0].sum())
    pf = w / l if l > 0 else 99
    cum = np.cumsum(arr)
    dd = (cum - np.maximum.accumulate(cum)).min()
    calmar = tot / abs(dd) if dd < 0 else 0
    sharpe = (arr.mean() / arr.std()) * np.sqrt(252) if arr.std() > 0 else 0
    mid = len(arr) // 2
    h1, h2 = arr[:mid], arr[mid:]
    h1w, h1l = h1[h1>0].sum(), abs(h1[h1<0].sum())
    h2w, h2l = h2[h2>0].sum(), abs(h2[h2<0].sum())
    h1_pf = h1w/h1l if h1l > 0 else 99
    h2_pf = h2w/h2l if h2l > 0 else 99
    monthly = defaultdict(float)
    for i in range(len(arr)):
        monthly[go.iloc[i]["date"].strftime("%Y-%m")] += arr[i]
    mo_arr = np.array(list(monthly.values()))
    pct_mo = (mo_arr > 0).mean() * 100
    is_loss = arr < 0
    max_cl = curr = 0
    for x in is_loss:
        if x: curr += 1; max_cl = max(max_cl, curr)
        else: curr = 0
    return {"n_traded": n_traded, "total_pnl": tot, "avg_pnl": tot/n_traded,
            "win_rate": wr, "profit_factor": pf, "max_dd": dd, "calmar": calmar,
            "sharpe": sharpe, "h1_pf": h1_pf, "h2_pf": h2_pf,
            "robust": h1_pf > 1.0 and h2_pf > 1.0, "pct_mo": pct_mo,
            "worst_day": arr.min(), "best_day": arr.max(), "max_cl": max_cl}


# Sizing models
def sizing_flat(n_signals, pool_size):
    """Always $100K when any signal fires."""
    return DAILY_RISK if n_signals > 0 else 0

def sizing_linear(n_signals, pool_size):
    """Scale linearly: each signal adds 1/pool_size of budget."""
    if n_signals == 0: return 0
    return min(DAILY_RISK, DAILY_RISK * (n_signals / pool_size))

def sizing_tiered(n_signals, pool_size):
    """Tiered: 1 signal = 25%, 2 = 50%, 3 = 75%, 4+ = 100%."""
    if n_signals == 0: return 0
    if n_signals == 1: return DAILY_RISK * 0.25
    if n_signals == 2: return DAILY_RISK * 0.50
    if n_signals == 3: return DAILY_RISK * 0.75
    return DAILY_RISK

def sizing_aggressive(n_signals, pool_size):
    """1 signal = 50%, 2 = 75%, 3+ = 100%."""
    if n_signals == 0: return 0
    if n_signals == 1: return DAILY_RISK * 0.50
    if n_signals == 2: return DAILY_RISK * 0.75
    return DAILY_RISK

def sizing_binary(n_signals, pool_size):
    """Only trade when 2+ signals agree. Full size."""
    return DAILY_RISK if n_signals >= 2 else 0

SIZING_MODELS = {
    "flat_100k":    sizing_flat,
    "linear":       sizing_linear,
    "tiered":       sizing_tiered,
    "aggressive":   sizing_aggressive,
    "binary_2+":    sizing_binary,
}

# Run all combos
results = []
total_configs = len(pool_sizes) * len(SIZING_MODELS) * len(MECH_CONFIGS)
config_num = 0

for pool_size in pool_sizes:
    top_groups = groups_70[:pool_size]

    for sizing_name, sizing_fn in SIZING_MODELS.items():
        for mech_name, mech_params in MECH_CONFIGS.items():
            config_num += 1

            daily_pnl = np.zeros(N)
            for idx in range(N):
                row = go.iloc[idx]
                n_fire = day_signals[pool_size][idx]

                risk = sizing_fn(n_fire, pool_size)
                if risk <= 0: continue

                # Determine mechanics
                if mech_name == "adaptive":
                    # Use the highest-ranked group that fires today
                    best_firing = None
                    for grp in top_groups:
                        if idx in grp["fire_days"]:
                            best_firing = grp["leader"]
                            break
                    if best_firing is None: continue
                    tgt = best_firing["target_pct"]
                    ts = best_firing["time_stop"]
                    n_t = get_n_tranches(best_firing["tranches"])
                    cps = get_interval_cps(best_firing["tranches"])
                else:
                    tgt, ts, n_t, cps = mech_params

                pnl, oc = simulate_day_dollar(row, tgt, ts, n_t, cps, risk)
                daily_pnl[idx] = pnl

            stats = compute_stats(daily_pnl)
            stats["pool"] = pool_size
            stats["sizing"] = sizing_name
            stats["mech"] = mech_name
            stats["label"] = f"P{pool_size}/{sizing_name}/{mech_name}"
            stats["daily_pnl"] = daily_pnl
            results.append(stats)

print(f"  {len(results)} configurations tested", flush=True)


# ════════════════════════════════════════════════════════════════════════════
# STEP 5: Results — sort by Calmar among robust configs
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  TOP 30 CONFIGURATIONS (sorted by Calmar, robust first)")
print(f"{'='*120}\n")

# Separate robust and non-robust
robust_results = [r for r in results if r["robust"] and r["n_traded"] >= 20]
non_robust = [r for r in results if not r["robust"] and r["n_traded"] >= 20]

robust_results.sort(key=lambda x: -x["calmar"])
non_robust.sort(key=lambda x: -x["calmar"])

ranked = robust_results + non_robust

print(f"  {'#':>3} {'Pool':>4} {'Sizing':>12} {'Mechanics':>16} {'Trades':>6} {'Total $':>14} {'Avg':>9} {'WR':>6} {'PF':>6} {'DD':>12} {'Cal':>6} {'Shp':>6} {'H1':>5} {'H2':>5} {'Rob':>4} {'Mo%':>4}")
print(f"  {'─'*140}")

for i, r in enumerate(ranked[:30]):
    rob = "✓" if r["robust"] else "✗"
    print(f"  {i+1:>3} {r['pool']:>4} {r['sizing']:>12} {r['mech']:>16} "
          f"{r['n_traded']:>6} ${r['total_pnl']:>+13,.0f} ${r['avg_pnl']:>+8,.0f} "
          f"{r['win_rate']:>5.1f}% {r['profit_factor']:>5.2f} ${r['max_dd']:>+11,.0f} "
          f"{r['calmar']:>5.2f} {r['sharpe']:>5.2f} {r['h1_pf']:>4.1f} {r['h2_pf']:>4.1f} {rob:>4} {r['pct_mo']:>3.0f}%")


# ════════════════════════════════════════════════════════════════════════════
# STEP 6: Deep dive on top 5
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  DEEP DIVE — Top 5 Configurations")
print(f"{'='*120}")

for i, r in enumerate(ranked[:5]):
    print(f"\n  ── #{i+1}: Pool={r['pool']} / {r['sizing']} / {r['mech']} ──")
    print(f"  Trades: {r['n_traded']}  |  Total: ${r['total_pnl']:+,.0f}  |  Avg: ${r['avg_pnl']:+,.0f}")
    print(f"  WR: {r['win_rate']:.1f}%  |  PF: {r['profit_factor']:.2f}  |  Calmar: {r['calmar']:.2f}  |  Sharpe: {r['sharpe']:.2f}")
    print(f"  Max DD: ${r['max_dd']:+,.0f}  |  Best: ${r['best_day']:+,.0f}  |  Worst: ${r['worst_day']:+,.0f}")
    print(f"  H1 PF: {r['h1_pf']:.2f}  |  H2 PF: {r['h2_pf']:.2f}  |  Robust: {'✓' if r['robust'] else '✗'}")
    print(f"  Max consec losses: {r['max_cl']}  |  Profitable months: {r['pct_mo']:.0f}%")

    # Monthly breakdown
    monthly = defaultdict(float)
    for idx in range(N):
        m = go.iloc[idx]["date"].strftime("%Y-%m")
        monthly[m] += r["daily_pnl"][idx]

    print(f"\n  {'Month':<10} {'P&L':>14} {'Cum':>14}")
    print(f"  {'─'*40}")
    cum = 0
    for m in sorted(monthly.keys()):
        cum += monthly[m]
        marker = " ◄" if monthly[m] < -20000 else ""
        print(f"  {m:<10} ${monthly[m]:>+13,.0f} ${cum:>+13,.0f}{marker}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 7: Confluence depth for the best config
# ════════════════════════════════════════════════════════════════════════════
best = ranked[0]
best_pool = best["pool"]
print(f"\n{'='*120}")
print(f"  CONFLUENCE DEPTH — Best config (Pool={best_pool}, {best['sizing']}, {best['mech']})")
print(f"{'='*120}\n")

print(f"  {'Signals':>8} {'Days':>6} {'WR':>7} {'Avg PnL':>12} {'Total':>14} {'PF':>6}")
print(f"  {'─'*60}")

for n_sig in range(0, best_pool + 1):
    indices = [i for i in range(N) if day_signals[best_pool][i] == n_sig]
    if len(indices) < 3: continue
    sub = best["daily_pnl"][indices]
    traded = sub[sub != 0]
    if len(traded) == 0: continue
    w = traded[traded>0].sum()
    l = abs(traded[traded<0].sum())
    pf = w/l if l > 0 else 99
    print(f"  {n_sig:>8} {len(indices):>6} {(traded>0).mean()*100:>6.1f}% ${traded.mean():>+11,.0f} ${sub.sum():>+13,.0f} {pf:>5.2f}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 8: Equity curve milestones
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  EQUITY CURVES — Top 5")
print(f"{'='*120}\n")

milestones = [50, 100, 200, 300, 400, 500, N-1]
top5 = ranked[:5]

header = f"  {'Day':>6} {'Date':<12}"
for r in top5:
    header += f"  {r['label'][:22]:>22}"
print(header)
print(f"  {'─'*(18 + 24*len(top5))}")

cum_curves = [np.cumsum(r["daily_pnl"]) for r in top5]
for d in milestones:
    if d >= N: continue
    dt = go.iloc[d]["date"].strftime("%Y-%m-%d")
    line = f"  {d:>6} {dt:<12}"
    for cc in cum_curves:
        line += f"  ${cc[d]:>+21,.0f}"
    print(line)


# ════════════════════════════════════════════════════════════════════════════
# SAVE
# ════════════════════════════════════════════════════════════════════════════
# Save top 5 equity curves
eq_data = {"date": go["date"].values}
for i, r in enumerate(top5):
    safe = r["label"].replace("/", "_").replace("%", "").replace("+", "p")
    eq_data[f"top{i+1}_daily"] = r["daily_pnl"]
    eq_data[f"top{i+1}_cum"] = cum_curves[i]

eq_df = pd.DataFrame(eq_data)
eq_path = os.path.join(_DIR, "ensemble_v3_equity.csv")
eq_df.to_csv(eq_path, index=False)
print(f"\n  ✓ Equity curves → {eq_path}")

# Save full results table (without daily_pnl arrays)
summary_rows = []
for r in ranked[:50]:
    row = {k: v for k, v in r.items() if k != "daily_pnl"}
    summary_rows.append(row)
pd.DataFrame(summary_rows).to_csv(os.path.join(_DIR, "ensemble_v3_summary.csv"), index=False)
print(f"  ✓ Summary → ensemble_v3_summary.csv")


# ════════════════════════════════════════════════════════════════════════════
# VERDICT
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  VERDICT")
print(f"{'='*120}\n")

b = ranked[0]
print(f"  🏆 BEST: Pool={b['pool']} / {b['sizing']} / {b['mech']}")
print(f"     {b['n_traded']} trades  |  Total: ${b['total_pnl']:+,.0f}  |  PF: {b['profit_factor']:.2f}  |  "
      f"Calmar: {b['calmar']:.2f}  |  Sharpe: {b['sharpe']:.2f}  |  DD: ${b['max_dd']:+,.0f}")
print(f"     Robust: {'✓' if b['robust'] else '✗'}  |  Win rate: {b['win_rate']:.1f}%  |  "
      f"Profitable months: {b['pct_mo']:.0f}%")

if len(ranked) > 1:
    r2 = ranked[1]
    print(f"\n  🥈 2nd: Pool={r2['pool']} / {r2['sizing']} / {r2['mech']}")
    print(f"     {r2['n_traded']} trades  |  Total: ${r2['total_pnl']:+,.0f}  |  PF: {r2['profit_factor']:.2f}  |  "
          f"Calmar: {r2['calmar']:.2f}  |  DD: ${r2['max_dd']:+,.0f}")

print(f"\n{'='*120}")
print("  DONE")
print(f"{'='*120}")
