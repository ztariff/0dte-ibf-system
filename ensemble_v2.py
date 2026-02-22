"""
ENSEMBLE BACKTEST V2 — Jaccard-Clustered Signal Groups
========================================================
1. Take 891 filter clusters (PF≥1.2), compute which days each fires on
2. Jaccard-cluster them: merge filters that fire on 70/80/90%+ same days
3. Each distinct cluster gets a quality score (best PF/Calmar within)
4. Each day: count how many distinct clusters fire, allocate $100K
   proportional to quality score, each cluster uses its own best mechanics
5. Confluence bonus: when many distinct clusters agree, weight up

Usage:
    python3 ensemble_v2.py | tee ensemble_v2_results.txt
"""

import os, json, sys
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

print(f"  {N} GO trades, {len(catalog)} parameter sets loaded")

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
# REBUILD FILTER MASKS
# ════════════════════════════════════════════════════════════════════════════
print("Rebuilding filter masks...", flush=True)

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
        if p in atomic_filters:
            mask = mask & atomic_filters[p]
        else:
            return None
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
            et = pd.Timestamp(exit_time_str)
            exit_h, exit_m = et.hour, et.minute
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
# STEP 1: Build filter clusters with day-fire sets
# ════════════════════════════════════════════════════════════════════════════
print("\nStep 1: Building filter clusters...", flush=True)

# Group catalog by filter name, pick best mechanics per filter
filter_groups = defaultdict(list)
for ps in catalog:
    filter_groups[ps["filters"]].append(ps)

# Each filter cluster: name, best set, set of days it fires on
clusters_raw = []
for filt_name, group in filter_groups.items():
    mask = parse_filter_mask(filt_name)
    if mask is None: continue
    fire_days = frozenset(go.index[mask].tolist())
    if len(fire_days) < 5: continue

    # Pick best mechanics: prefer robust S/A tier, then highest calmar
    robust = [ps for ps in group if (ps.get("robust") == True or ps.get("robust") == "True")]
    sa_robust = [ps for ps in robust if ps["tier"] in ("S", "A")]
    sab_robust = [ps for ps in robust if ps["tier"] in ("S", "A", "B")]

    if sa_robust:
        best = max(sa_robust, key=lambda x: x["calmar"])
    elif sab_robust:
        best = max(sab_robust, key=lambda x: x["calmar"])
    elif robust:
        best = max(robust, key=lambda x: x["calmar"])
    else:
        best = max(group, key=lambda x: x["calmar"])

    # Only keep if PF >= 1.2
    if best["profit_factor"] < 1.2: continue

    clusters_raw.append({
        "name": filt_name,
        "fire_days": fire_days,
        "n_fire": len(fire_days),
        "best_set": best,
        "tier": best["tier"],
        "pf": best["profit_factor"],
        "calmar": best["calmar"],
        "target_pct": best["target_pct"],
        "time_stop": best["time_stop"],
        "tranches": best["tranches"],
    })

# Sort by quality: S first, then A, then B, within tier by calmar desc
tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
clusters_raw.sort(key=lambda x: (tier_order.get(x["tier"], 5), -x["calmar"]))

print(f"  {len(clusters_raw)} filter clusters with PF≥1.2")


# ════════════════════════════════════════════════════════════════════════════
# STEP 2: Jaccard clustering at multiple thresholds
# ════════════════════════════════════════════════════════════════════════════
print("\nStep 2: Jaccard clustering...", flush=True)

def jaccard(s1, s2):
    """Jaccard similarity between two sets."""
    if len(s1) == 0 and len(s2) == 0: return 1.0
    inter = len(s1 & s2)
    union = len(s1 | s2)
    return inter / union if union > 0 else 0


def cluster_by_jaccard(clusters_list, threshold):
    """
    Greedy clustering: iterate through clusters (already sorted by quality).
    For each cluster, check if it's >threshold similar to any existing group leader.
    If yes, merge into that group. If no, start a new group.
    """
    groups = []  # list of {"leader": cluster, "members": [clusters], "fire_days": set}

    for cl in clusters_list:
        merged = False
        for grp in groups:
            sim = jaccard(cl["fire_days"], grp["fire_days"])
            if sim >= threshold:
                grp["members"].append(cl)
                # Union the fire days (group fires if any member fires)
                grp["fire_days"] = grp["fire_days"] | cl["fire_days"]
                merged = True
                break
        if not merged:
            groups.append({
                "leader": cl,
                "members": [cl],
                "fire_days": set(cl["fire_days"]),
            })

    return groups


thresholds = [0.70, 0.80, 0.90]
threshold_results = {}

for thresh in thresholds:
    groups = cluster_by_jaccard(clusters_raw, thresh)
    print(f"\n  Threshold {thresh:.0%}: {len(groups)} distinct signal groups")

    # Show top groups
    for i, grp in enumerate(groups[:10]):
        leader = grp["leader"]
        print(f"    G{i+1:02d}: {leader['name']:<45} "
              f"T={leader['tier']} PF={leader['pf']:.2f} Cal={leader['calmar']:.2f} "
              f"fires={len(grp['fire_days']):>3}d  members={len(grp['members'])}")

    threshold_results[thresh] = groups


# ════════════════════════════════════════════════════════════════════════════
# STEP 3: Compute quality scores for each group
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("Step 3: Quality scoring + backtest at each threshold")
print(f"{'='*120}", flush=True)

def quality_score(grp):
    """
    Score a signal group by its leader's stats.
    Higher = more capital allocated.
    """
    leader = grp["leader"]
    tier_scores = {"S": 4.0, "A": 3.0, "B": 2.0, "C": 1.0, "D": 0.5}
    base = tier_scores.get(leader["tier"], 0.5)

    # Bonus for high PF and Calmar
    pf_bonus = min(leader["pf"] / 2.0, 3.0)  # cap at 3
    cal_bonus = min(leader["calmar"] / 5.0, 2.0)  # cap at 2

    return base + pf_bonus + cal_bonus


# ════════════════════════════════════════════════════════════════════════════
# STEP 4: Backtest each threshold
# ════════════════════════════════════════════════════════════════════════════

def run_backtest(groups, label, confluence_bonus=True):
    """
    Run backtest for a set of signal groups.

    Each day:
    - Find which groups fire
    - Compute quality-weighted allocation of $100K
    - If confluence_bonus: when more groups fire, scale total risk up
      (capped at $100K — we never exceed daily budget)
    - Simulate each group's trade with its leader's mechanics
    """
    # Precompute: for each group, which day indices it fires on
    group_fire_sets = []
    group_scores = []
    for grp in groups:
        group_fire_sets.append(grp["fire_days"])
        group_scores.append(quality_score(grp))

    n_groups = len(groups)
    daily_pnl = []
    daily_n_groups = []
    daily_risk_used = []

    for idx in range(N):
        row = go.iloc[idx]

        # Which groups fire today?
        firing = []
        firing_scores = []
        for g_idx in range(n_groups):
            if idx in group_fire_sets[g_idx]:
                firing.append(g_idx)
                firing_scores.append(group_scores[g_idx])

        if len(firing) == 0:
            daily_pnl.append(0.0)
            daily_n_groups.append(0)
            daily_risk_used.append(0.0)
            continue

        # Confluence bonus: more groups = higher conviction
        # But never exceed $100K total
        n_firing = len(firing)

        if confluence_bonus:
            # Base allocation: $100K * utilization factor
            # 1 group = 40%, 2 = 60%, 3 = 75%, 4 = 85%, 5+ = 100%
            if n_firing == 1:
                total_risk = DAILY_RISK * 0.40
            elif n_firing == 2:
                total_risk = DAILY_RISK * 0.60
            elif n_firing == 3:
                total_risk = DAILY_RISK * 0.75
            elif n_firing == 4:
                total_risk = DAILY_RISK * 0.85
            else:
                total_risk = DAILY_RISK * 1.00
        else:
            total_risk = DAILY_RISK

        # Allocate proportional to quality score
        total_score = sum(firing_scores)
        day_pnl = 0.0

        for i, g_idx in enumerate(firing):
            grp = groups[g_idx]
            leader = grp["leader"]
            weight = firing_scores[i] / total_score
            risk_alloc = total_risk * weight

            # Minimum risk per group: $2,000 (otherwise not worth trading)
            if risk_alloc < 2000:
                continue

            n_t = get_n_tranches(leader["tranches"])
            cps = get_interval_cps(leader["tranches"])
            pnl, oc = simulate_day_dollar(row, leader["target_pct"],
                                           leader["time_stop"], n_t, cps, risk_alloc)
            day_pnl += pnl

        daily_pnl.append(day_pnl)
        daily_n_groups.append(n_firing)
        daily_risk_used.append(total_risk)

    return np.array(daily_pnl), daily_n_groups, daily_risk_used


def compute_stats(arr, label):
    n_traded = np.sum(arr != 0)
    if n_traded == 0:
        return {"name": label, "n_traded": 0, "total_pnl": 0}

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

    # Split half
    mid = len(arr) // 2
    h1, h2 = arr[:mid], arr[mid:]
    h1w, h1l = h1[h1>0].sum(), abs(h1[h1<0].sum())
    h2w, h2l = h2[h2>0].sum(), abs(h2[h2<0].sum())
    h1_pf = h1w/h1l if h1l > 0 else 99
    h2_pf = h2w/h2l if h2l > 0 else 99

    # Monthly
    monthly = defaultdict(float)
    for i in range(len(arr)):
        m = go.iloc[i]["date"].strftime("%Y-%m")
        monthly[m] += arr[i]
    mo_arr = np.array(list(monthly.values()))
    pct_mo = (mo_arr > 0).mean() * 100

    # Max consec losses
    is_loss = arr < 0
    max_cl = 0
    curr = 0
    for x in is_loss:
        if x: curr += 1; max_cl = max(max_cl, curr)
        else: curr = 0

    return {
        "name": label,
        "n_traded": int(n_traded),
        "total_pnl": tot,
        "avg_pnl": tot / n_traded,
        "win_rate": wr,
        "profit_factor": pf,
        "max_dd": dd,
        "calmar": calmar,
        "sharpe": sharpe,
        "h1_pf": h1_pf,
        "h2_pf": h2_pf,
        "robust": h1_pf > 1.0 and h2_pf > 1.0,
        "pct_profitable_months": pct_mo,
        "n_months": len(monthly),
        "best_day": arr.max(),
        "worst_day": arr.min(),
        "max_consec_losses": max_cl,
    }


# Run backtests
all_results = {}

for thresh in thresholds:
    groups = threshold_results[thresh]
    label = f"J={thresh:.0%} ({len(groups)} groups)"

    # With confluence bonus
    pnl_arr, n_grps, risk_used = run_backtest(groups, label, confluence_bonus=True)
    stats = compute_stats(pnl_arr, label)
    stats["avg_groups_per_day"] = np.mean([x for x in n_grps if x > 0]) if any(x > 0 for x in n_grps) else 0
    stats["avg_risk_used"] = np.mean([x for x in risk_used if x > 0]) if any(x > 0 for x in risk_used) else 0
    all_results[f"conf_{thresh}"] = stats
    all_results[f"conf_{thresh}_pnl"] = pnl_arr
    all_results[f"conf_{thresh}_ngrps"] = n_grps

    # Without confluence bonus (flat $100K always)
    pnl_arr2, n_grps2, risk_used2 = run_backtest(groups, f"J={thresh:.0%} FLAT", confluence_bonus=False)
    stats2 = compute_stats(pnl_arr2, f"J={thresh:.0%} FLAT ({len(groups)} groups)")
    stats2["avg_groups_per_day"] = np.mean([x for x in n_grps2 if x > 0]) if any(x > 0 for x in n_grps2) else 0
    stats2["avg_risk_used"] = np.mean([x for x in risk_used2 if x > 0]) if any(x > 0 for x in risk_used2) else 0
    all_results[f"flat_{thresh}"] = stats2
    all_results[f"flat_{thresh}_pnl"] = pnl_arr2

print(f"\n  All backtests complete.", flush=True)


# ════════════════════════════════════════════════════════════════════════════
# RESULTS COMPARISON
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  RESULTS COMPARISON — ALL CONFIGURATIONS")
print(f"{'='*120}\n")

configs = []
for thresh in thresholds:
    configs.append(f"conf_{thresh}")
    configs.append(f"flat_{thresh}")

header_labels = [all_results[c]["name"] for c in configs]
# Too many columns — print as rows instead
for c in configs:
    s = all_results[c]
    print(f"  ┌─ {s['name']}")
    print(f"  │  Traded: {s['n_traded']}d  |  Avg groups/day: {s.get('avg_groups_per_day',0):.1f}  |  Avg risk: ${s.get('avg_risk_used',0):,.0f}")
    print(f"  │  Total P&L: ${s['total_pnl']:+,.0f}  |  Avg: ${s['avg_pnl']:+,.0f}/day")
    print(f"  │  WR: {s['win_rate']:.1f}%  |  PF: {s['profit_factor']:.2f}  |  Calmar: {s['calmar']:.2f}  |  Sharpe: {s['sharpe']:.2f}")
    print(f"  │  Max DD: ${s['max_dd']:+,.0f}  |  Best day: ${s['best_day']:+,.0f}  |  Worst day: ${s['worst_day']:+,.0f}")
    print(f"  │  H1 PF: {s['h1_pf']:.2f}  |  H2 PF: {s['h2_pf']:.2f}  |  Robust: {'✓' if s['robust'] else '✗'}  |  Consec losses: {s['max_consec_losses']}")
    print(f"  │  Profitable months: {s['pct_profitable_months']:.0f}% ({s['n_months']}mo)")
    print(f"  └{'─'*80}")
    print()


# ════════════════════════════════════════════════════════════════════════════
# CONFLUENCE DEPTH ANALYSIS — Does more groups firing = better P&L?
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  CONFLUENCE DEPTH ANALYSIS — More groups = better results?")
print(f"{'='*120}\n")

# Use the best threshold's data for this analysis
for thresh in thresholds:
    pnl_arr = all_results[f"conf_{thresh}_pnl"]
    n_grps = all_results[f"conf_{thresh}_ngrps"]
    n_groups_total = len(threshold_results[thresh])

    print(f"  ── Threshold {thresh:.0%} ({n_groups_total} groups) ──")
    print(f"  {'Groups Firing':>14} {'Days':>6} {'WR':>7} {'Avg PnL':>12} {'Total':>14} {'PF':>6}")
    print(f"  {'─'*65}")

    # Bucket by number of groups firing
    max_g = max(n_grps) if n_grps else 0
    if max_g <= 10:
        buckets = [(i, i+1) for i in range(max_g+1)]
    else:
        buckets = [(0,1),(1,2),(2,3),(3,4),(4,6),(6,10),(10,15),(15,20),(20,30),(30,max_g+1)]

    for lo, hi in buckets:
        indices = [i for i in range(N) if n_grps[i] >= lo and n_grps[i] < hi]
        if len(indices) < 3: continue
        sub = pnl_arr[indices]
        traded = sub[sub != 0]
        if len(traded) == 0: continue
        w = traded[traded>0].sum()
        l = abs(traded[traded<0].sum())
        pf = w/l if l > 0 else 99
        label = f"{lo}" if hi == lo+1 else f"{lo}-{hi-1}"
        print(f"  {label:>14} {len(indices):>6} {(traded>0).mean()*100:>6.1f}% ${traded.mean():>+11,.0f} ${sub.sum():>+13,.0f} {pf:>5.2f}")
    print()


# ════════════════════════════════════════════════════════════════════════════
# MONTHLY BREAKDOWN FOR BEST CONFIG
# ════════════════════════════════════════════════════════════════════════════

# Find best config
best_config = max(configs, key=lambda c: all_results[c]["calmar"] if all_results[c]["robust"] else -999)
best_s = all_results[best_config]
best_pnl = all_results[f"{best_config}_pnl"]

print(f"\n{'='*120}")
print(f"  MONTHLY BREAKDOWN — Best Config: {best_s['name']}")
print(f"{'='*120}\n")

monthly = defaultdict(float)
for i in range(N):
    m = go.iloc[i]["date"].strftime("%Y-%m")
    monthly[m] += best_pnl[i]

print(f"  {'Month':<10} {'P&L':>14} {'Cumulative':>14}")
print(f"  {'─'*40}")
cum = 0
for m in sorted(monthly.keys()):
    cum += monthly[m]
    print(f"  {m:<10} ${monthly[m]:>+13,.0f} ${cum:>+13,.0f}")


# ════════════════════════════════════════════════════════════════════════════
# EQUITY CURVES
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  EQUITY CURVE MILESTONES")
print(f"{'='*120}\n")

milestones = [50, 100, 200, 300, 400, 500, N-1]
header = f"  {'Day':>6} {'Date':<12}"
for c in configs:
    header += f" {all_results[c]['name'][:20]:>20}"
print(header)
print(f"  {'─'*(18 + 20*len(configs))}")

cum_curves = {}
for c in configs:
    cum_curves[c] = np.cumsum(all_results[f"{c}_pnl"])

for d in milestones:
    if d >= N: continue
    dt = go.iloc[d]["date"].strftime("%Y-%m-%d")
    line = f"  {d:>6} {dt:<12}"
    for c in configs:
        line += f" ${cum_curves[c][d]:>+18,.0f}"
    print(line)


# Save equity curves
eq_data = {"date": go["date"].values}
for c in configs:
    safe_name = c.replace(".", "")
    eq_data[f"{safe_name}_daily"] = all_results[f"{c}_pnl"]
    eq_data[f"{safe_name}_cum"] = cum_curves[c]

eq_df = pd.DataFrame(eq_data)
eq_path = os.path.join(_DIR, "ensemble_v2_equity.csv")
eq_df.to_csv(eq_path, index=False)
print(f"\n  ✓ Equity curves saved → {eq_path}")


# ════════════════════════════════════════════════════════════════════════════
# VERDICT
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*120}")
print("  VERDICT")
print(f"{'='*120}\n")

# Rank all configs
ranked = sorted(configs, key=lambda c: (
    1 if all_results[c]["robust"] else 0,
    all_results[c]["calmar"],
    all_results[c]["sharpe"],
), reverse=True)

for i, c in enumerate(ranked):
    s = all_results[c]
    marker = " 🏆" if i == 0 else ""
    print(f"  #{i+1}: {s['name']}{marker}")
    print(f"      Total: ${s['total_pnl']:+,.0f}  |  PF: {s['profit_factor']:.2f}  |  "
          f"Calmar: {s['calmar']:.2f}  |  Sharpe: {s['sharpe']:.2f}  |  "
          f"DD: ${s['max_dd']:+,.0f}  |  Robust: {'✓' if s['robust'] else '✗'}")

print(f"\n{'='*120}")
print("  DONE")
print(f"{'='*120}")
