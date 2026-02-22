"""
REVERSE-ENGINEER OPTIMAL FILTERS + MECHANICS
=============================================
Full unfiltered universe. Every day traded. Rich intraday P&L timeline.

Phase 1: What signal variables predict winners?
Phase 2: What exit mechanics (target %, time stop, hold-to-close) maximize edge?
Phase 3: Combined — best signals + best mechanics
Phase 4: Robustness (split-half, rolling window)
"""
import pandas as pd
import numpy as np
from itertools import combinations

import os
_DIR = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go["winner"] = (go["pnl_p1_dollars"] > 0).astype(int)

N = len(go)
print(f"{'='*150}")
print(f"  RESEARCH MODE — {N} UNFILTERED TRADES")
print(f"  {go['date'].min().strftime('%Y-%m-%d')} to {go['date'].max().strftime('%Y-%m-%d')}")
print(f"{'='*150}")


# ════════════════════════════════════════════════════════════════════════════
# PHASE 2: EXIT MECHANICS — test every combo of target % and time stop
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*150}")
print(f"  PHASE 1: EXIT MECHANICS SWEEP")
print(f"  For each trade, simulate different exit rules using intraday P&L snapshots")
print(f"{'='*150}")

def simulate_exit(row, target_pct, time_stop, use_wing_stop=True):
    """
    Simulate exit for a single trade given target % and time stop.
    Returns (pnl_per_spread, outcome).

    Priority: wing_stop (if enabled) > target > time_stop > close
    But we need to figure out which happens FIRST chronologically.
    """
    credit = row.get("pnl_at_close", 0)  # credit is embedded in the sweep data
    n = row["n_spreads_p1"]
    if n == 0 or pd.isna(n):
        return 0, "SKIP"

    # Wing stop time
    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")

    # Target hit time
    tgt_col = f"hit_{target_pct}_pnl"
    tgt_time_col = f"hit_{target_pct}_time"
    tgt_pnl = row.get(tgt_col)
    tgt_time = row.get(tgt_time_col, "")

    # Time stop P&L
    ts_map = {
        "1030": "pnl_at_1030", "1100": "pnl_at_1100", "1130": "pnl_at_1130",
        "1200": "pnl_at_1200", "1230": "pnl_at_1230", "1300": "pnl_at_1300",
        "1330": "pnl_at_1330", "1400": "pnl_at_1400", "1430": "pnl_at_1430",
        "1500": "pnl_at_1500", "1530": "pnl_at_1530", "1545": "pnl_at_1545",
        "close": "pnl_at_close",
    }
    ts_pnl = row.get(ts_map.get(time_stop, "pnl_at_close"))

    # Map time_stop to (hour, minute) for comparison
    ts_hm = {
        "1030": (10,30), "1100": (11,0), "1130": (11,30),
        "1200": (12,0), "1230": (12,30), "1300": (13,0),
        "1330": (13,30), "1400": (14,0), "1430": (14,30),
        "1500": (15,0), "1530": (15,30), "1545": (15,45),
        "close": (16,0),
    }
    ts_hour, ts_min = ts_hm.get(time_stop, (16,0))

    def time_before(t_str, h, m):
        """Check if timestamp string is before h:m"""
        if not t_str or pd.isna(t_str) or t_str == "":
            return False
        try:
            t = pd.Timestamp(t_str)
            return t.hour < h or (t.hour == h and t.minute < m)
        except:
            return False

    # Determine what happens first
    # 1. Wing stop before everything?
    if use_wing_stop and ws_time and pd.notna(ws_pnl):
        ws_before_ts = time_before(ws_time, ts_hour, ts_min)
        ws_before_tgt = True  # assume ws happens before target unless target hit earlier
        if tgt_time and pd.notna(tgt_pnl):
            try:
                ws_t = pd.Timestamp(ws_time)
                tgt_t = pd.Timestamp(tgt_time)
                ws_before_tgt = ws_t <= tgt_t
            except:
                pass

        if ws_before_ts and ws_before_tgt:
            pnl = round(ws_pnl * n * 100 - n * 100, 0)  # pnl_per_spread * n * SPX_MULT - slippage
            return pnl, "WING_STOP"

    # 2. Target hit before time stop?
    if pd.notna(tgt_pnl) and tgt_time:
        tgt_before_ts = time_before(tgt_time, ts_hour, ts_min)
        if tgt_before_ts:
            pnl = round(tgt_pnl * n * 100 - n * 100, 0)
            return pnl, "TARGET"

    # 3. Time stop
    if pd.notna(ts_pnl):
        pnl = round(ts_pnl * n * 100 - n * 100, 0)
        return pnl, "TIME_STOP" if time_stop != "close" else "EXPIRY"

    # 4. Fallback to close
    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl):
        pnl = round(close_pnl * n * 100 - n * 100, 0)
        return pnl, "EXPIRY"

    return 0, "NO_DATA"

def eval_mechanics(go_df, target_pct, time_stop, use_ws=True, label=""):
    if go_df.empty:
        return None
    results = []
    for _, row in go_df.iterrows():
        pnl, oc = simulate_exit(row, target_pct, time_stop, use_ws)
        results.append({"pnl": pnl, "outcome": oc})
    sim = pd.DataFrame(results)
    if sim.empty or "outcome" not in sim.columns:
        return None
    valid = sim[sim["outcome"] != "SKIP"]
    n = len(valid)
    if n < 5:
        return None
    tot = valid["pnl"].sum()
    wr = (valid["pnl"]>0).mean()*100
    w = valid[valid["pnl"]>0]["pnl"].sum()
    l = abs(valid[valid["pnl"]<0]["pnl"].sum())
    pf = w/l if l > 0 else 99
    cum = valid["pnl"].cumsum()
    dd = (cum - cum.cummax()).min()
    avg = tot / n
    tgt_n = len(valid[valid["outcome"]=="TARGET"])
    ws_n = len(valid[valid["outcome"]=="WING_STOP"])
    ts_n = len(valid[valid["outcome"].isin(["TIME_STOP","EXPIRY"])])
    return {"label": label, "n": n, "total": tot, "wr": wr, "pf": pf, "dd": dd, "avg": avg,
            "tgt_n": tgt_n, "ws_n": ws_n, "ts_n": ts_n}

# Full mechanics sweep
targets = [30, 40, 50, 60, 70, 80, 90]
time_stops = ["1300", "1400", "1430", "1500", "1530", "1545", "close"]
ts_labels = {"1300":"1:00pm", "1400":"2:00pm", "1430":"2:30pm", "1500":"3:00pm",
             "1530":"3:30pm", "1545":"3:45pm", "close":"Hold to Close"}

print(f"\n  {'Target':>8} {'TimeStop':>16} {'Trades':>6} {'Total $':>14} {'WR':>7} {'PF':>7} {'MaxDD':>13} {'Avg/Trd':>11}  {'TGT':>4} {'WS':>4} {'TS':>4}")
print(f"  {'─'*130}")

mech_results = []
for tgt in targets:
    for ts in time_stops:
        r = eval_mechanics(go, tgt, ts, True, f"{tgt}% / {ts_labels[ts]}")
        if r:
            mech_results.append(r)
            print(f"  {tgt:>7}% {ts_labels[ts]:>16} {r['n']:>6} ${r['total']:>+13,.0f} {r['wr']:>6.1f}% {r['pf']:>6.2f} ${r['dd']:>+12,.0f} ${r['avg']:>+10,.0f}  {r['tgt_n']:>4} {r['ws_n']:>4} {r['ts_n']:>4}")

# Also test NO target (pure time stop / hold to close)
print(f"\n  No-target (time stop only or hold to close):")
for ts in time_stops:
    r = eval_mechanics(go, 999, ts, True, f"No target / {ts_labels[ts]}")  # 999% = never hits
    if r:
        print(f"  {'none':>8} {ts_labels[ts]:>16} {r['n']:>6} ${r['total']:>+13,.0f} {r['wr']:>6.1f}% {r['pf']:>6.2f} ${r['dd']:>+12,.0f} ${r['avg']:>+10,.0f}  {r['tgt_n']:>4} {r['ws_n']:>4} {r['ts_n']:>4}")

# Test WITHOUT wing stops
print(f"\n  Without wing stops (let it ride through breaches):")
for tgt in [40, 50, 70]:
    for ts in ["1500", "1530", "close"]:
        r = eval_mechanics(go, tgt, ts, False, f"{tgt}% / {ts_labels[ts]} / no WS")
        if r:
            print(f"  {tgt:>7}% {ts_labels[ts]:>16} {r['n']:>6} ${r['total']:>+13,.0f} {r['wr']:>6.1f}% {r['pf']:>6.2f} ${r['dd']:>+12,.0f} ${r['avg']:>+10,.0f}  {r['tgt_n']:>4} {r['ws_n']:>4} {r['ts_n']:>4}")

# Best mechanics
mech_results.sort(key=lambda x: -x["pf"])
print(f"\n  ★ BEST BY PF: {mech_results[0]['label']} → PF {mech_results[0]['pf']:.2f}, ${mech_results[0]['total']:+,.0f}")
mech_results.sort(key=lambda x: -x["total"])
print(f"  ★ BEST BY TOTAL $: {mech_results[0]['label']} → ${mech_results[0]['total']:+,.0f}, PF {mech_results[0]['pf']:.2f}")


# ════════════════════════════════════════════════════════════════════════════
# PHASE 2: SIGNAL VARIABLE ANALYSIS
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*150}")
print(f"  PHASE 2: SIGNAL VARIABLES — What predicts winners?")
print(f"  Using the BEST mechanics from Phase 1 to evaluate signals")
print(f"{'='*150}")

# Use the best mechanics for signal analysis
best_mech = mech_results[0]
best_tgt = int(best_mech["label"].split("%")[0])
best_ts_label = best_mech["label"].split("/ ")[1]
# reverse lookup
best_ts = [k for k, v in ts_labels.items() if v == best_ts_label][0]

def eval_filter_with_mechanics(mask, label, tgt=best_tgt, ts=best_ts):
    sub = go[mask]
    r = eval_mechanics(sub, tgt, ts, True, label)
    return r

def print_r(r):
    if r is None: return
    print(f"    {r['label']:<50} {r['n']:>4} trades | ${r['total']:>+12,.0f} | WR={r['wr']:>5.1f}% | PF={r['pf']:>5.2f} | DD=${r['dd']:>+11,.0f} | Avg=${r['avg']:>+9,.0f}")

# Correlations
print(f"\n  Correlations with P&L (using {best_mech['label']}):")
# We need to compute P&L for each trade with best mechanics
sim_pnls = []
for _, row in go.iterrows():
    pnl, _ = simulate_exit(row, best_tgt, best_ts, True)
    sim_pnls.append(pnl)
go["sim_pnl"] = sim_pnls
go["sim_win"] = (go["sim_pnl"] > 0).astype(int)

num_cols = ["vix", "rv", "vp_ratio", "range_pct", "score", "wing_width",
            "score_vol", "score_regime", "score_ts",
            # Enrichment columns (prior-day & range context)
            "prior_day_return", "prior_day_range", "prior_day_body_pct",
            "prior_2d_return", "prior_5d_return",
            "pct_in_weekly_range", "pct_in_monthly_range",
            "prior_day_rv", "rv_1d_change"]
print(f"\n  {'Variable':<25} {'Corr w/ PnL':>12} {'Corr w/ Win':>12}")
for col in num_cols:
    valid = go[go[col].notna()]
    if len(valid) < 10: continue
    corr_pnl = valid[col].corr(valid["sim_pnl"])
    corr_win = valid[col].corr(valid["sim_win"])
    marker = " ★" if abs(corr_pnl) > 0.10 else ""
    print(f"  {col:<25} {corr_pnl:>+11.3f} {corr_win:>+11.3f}{marker}")

# Single variable breakdowns
print(f"\n  VIX buckets:")
for lo, hi in [(0,12),(12,14),(14,15),(15,16),(16,17),(17,18),(18,20),(20,25),(25,35),(35,99)]:
    print_r(eval_filter_with_mechanics((go["vix"]>=lo)&(go["vix"]<hi), f"VIX [{lo}-{hi})"))

print(f"\n  VP Ratio:")
for lo, hi in [(0,0.8),(0.8,1.0),(1.0,1.2),(1.2,1.3),(1.3,1.5),(1.5,1.7),(1.7,2.0),(2.0,3.0),(3.0,99)]:
    print_r(eval_filter_with_mechanics((go["vp_ratio"]>=lo)&(go["vp_ratio"]<hi), f"VP [{lo:.1f}-{hi:.1f})"))

print(f"\n  RV Slope:")
for slope in go["rv_slope"].dropna().unique():
    print_r(eval_filter_with_mechanics(go["rv_slope"]==slope, f"rv_slope={slope}"))

print(f"\n  VWAP Slope:")
for slope in go["vwap_slope"].dropna().unique():
    print_r(eval_filter_with_mechanics(go["vwap_slope"]==slope, f"vwap={slope}"))

print(f"\n  Day of Week:")
if "dow" in go.columns:
    for dow in ["Monday","Tuesday","Wednesday","Thursday","Friday"]:
        print_r(eval_filter_with_mechanics(go["dow"]==dow, dow))

print(f"\n  Range %:")
for lo, hi in [(0,0.2),(0.2,0.3),(0.3,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.5),(1.5,99)]:
    print_r(eval_filter_with_mechanics((go["range_pct"]>=lo)&(go["range_pct"]<hi), f"Range [{lo:.1f}-{hi:.1f}%)"))

# ── Prior-Day & Range Context Signals ──────────────────────────────────────
if "prior_day_return" in go.columns:
    print(f"\n  Prior Day Return:")
    for lo, hi in [(-99,-2),(-2,-1),(-1,-0.5),(-0.5,0),(0,0.5),(0.5,1),(1,2),(2,99)]:
        mask = (go["prior_day_return"]>=lo)&(go["prior_day_return"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"PriorRet [{lo:+.1f} to {hi:+.1f}%)"))

    print(f"\n  Prior Day Direction:")
    for d in ["UP", "DOWN", "FLAT"]:
        if d in go["prior_day_direction"].values:
            print_r(eval_filter_with_mechanics(go["prior_day_direction"]==d, f"Prior day {d}"))

    print(f"\n  Prior Day Range (high-low %):")
    for lo, hi in [(0,0.5),(0.5,0.8),(0.8,1.0),(1.0,1.5),(1.5,2.0),(2.0,99)]:
        mask = (go["prior_day_range"]>=lo)&(go["prior_day_range"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"PriorRange [{lo:.1f}-{hi:.1f}%)"))

    print(f"\n  Prior 5-Day Return (momentum):")
    for lo, hi in [(-99,-3),(-3,-1),(-1,0),(0,1),(1,3),(3,99)]:
        mask = (go["prior_5d_return"]>=lo)&(go["prior_5d_return"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"5dRet [{lo:+.0f} to {hi:+.0f}%)"))

    print(f"\n  Weekly Range Position:")
    if "in_prior_week_range" in go.columns:
        print_r(eval_filter_with_mechanics(go["in_prior_week_range"]==1, "Inside prior week range"))
        print_r(eval_filter_with_mechanics(go["in_prior_week_range"]==0, "Outside prior week range"))
    for lo, hi in [(0,20),(20,40),(40,60),(60,80),(80,100),(100,999)]:
        mask = (go["pct_in_weekly_range"]>=lo)&(go["pct_in_weekly_range"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"WkRng [{lo}-{hi}%)"))

    print(f"\n  Monthly Range Position:")
    if "in_prior_month_range" in go.columns:
        print_r(eval_filter_with_mechanics(go["in_prior_month_range"]==1, "Inside prior month range"))
        print_r(eval_filter_with_mechanics(go["in_prior_month_range"]==0, "Outside prior month range"))
    for lo, hi in [(0,20),(20,40),(40,60),(60,80),(80,100),(100,999)]:
        mask = (go["pct_in_monthly_range"]>=lo)&(go["pct_in_monthly_range"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"MoRng [{lo}-{hi}%)"))

    print(f"\n  Prior Day Realized Vol:")
    for lo, hi in [(0,8),(8,12),(12,16),(16,20),(20,25),(25,99)]:
        mask = (go["prior_day_rv"]>=lo)&(go["prior_day_rv"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"PriorRV [{lo}-{hi})"))

    print(f"\n  RV 1-Day Change:")
    for lo, hi in [(-99,-30),(-30,-10),(-10,0),(0,10),(10,30),(30,99)]:
        mask = (go["rv_1d_change"]>=lo)&(go["rv_1d_change"]<hi)
        print_r(eval_filter_with_mechanics(mask, f"RV chg [{lo:+.0f} to {hi:+.0f}%)"))


# ════════════════════════════════════════════════════════════════════════════
# PHASE 3: COMBINED — Best signals + best mechanics
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*150}")
print(f"  PHASE 3: FILTER OPTIMIZATION (using {best_mech['label']})")
print(f"{'='*150}")

filters = {
    "VIX≤15":    go["vix"] <= 15,
    "VIX≤16":    go["vix"] <= 16,
    "VIX≤17":    go["vix"] <= 17,
    "VIX≤18":    go["vix"] <= 18,
    "VIX≤20":    go["vix"] <= 20,
    "VP≤1.2":    go["vp_ratio"] <= 1.2,
    "VP≤1.3":    go["vp_ratio"] <= 1.3,
    "VP≤1.5":    go["vp_ratio"] <= 1.5,
    "VP≤1.7":    go["vp_ratio"] <= 1.7,
    "VP≤2.0":    go["vp_ratio"] <= 2.0,
    "STABLE":    go["rv_slope"] == "STABLE",
    "!RISING":   go["rv_slope"] != "RISING",
    "FLAT_vwap": go["vwap_slope"] == "FLAT",
    "Rng≤0.4":   go["range_pct"] <= 0.4,
    "Rng≤0.6":   go["range_pct"] <= 0.6,
}

# Add enrichment filters if columns exist
if "prior_day_return" in go.columns:
    filters.update({
        "PrDayDn":      go["prior_day_direction"] == "DOWN",
        "PrDayUp":      go["prior_day_direction"] == "UP",
        "PrRet<-1":     go["prior_day_return"] < -1,
        "PrRet>0":      go["prior_day_return"] > 0,
        "PrRng≤1":      go["prior_day_range"] <= 1.0,
        "PrRng≤0.8":    go["prior_day_range"] <= 0.8,
        "InWkRng":      go["in_prior_week_range"] == 1,
        "InMoRng":      go["in_prior_month_range"] == 1,
        "WkTop50":      go["pct_in_weekly_range"] >= 50,
        "WkBot50":      go["pct_in_weekly_range"] < 50,
        "5dRet>0":      go["prior_5d_return"] > 0,
        "5dRet<0":      go["prior_5d_return"] < 0,
        "PrRV<15":      go["prior_day_rv"] < 15,
        "PrRV<12":      go["prior_day_rv"] < 12,
        "RVchg<0":      go["rv_1d_change"] < 0,
    })

# Two-filter combos
results_2f = []
for (n1, f1), (n2, f2) in combinations(filters.items(), 2):
    r = eval_filter_with_mechanics(f1 & f2, f"{n1} + {n2}")
    if r and r["n"] >= 15:
        results_2f.append(r)

results_2f.sort(key=lambda x: -x["pf"])
print(f"\n  Top 15 two-filter combos (min 15 trades, by PF):")
for r in results_2f[:15]:
    print_r(r)

# Three-filter combos
top_filters = ["VIX≤16","VIX≤17","VIX≤18","VIX≤20",
               "VP≤1.3","VP≤1.5","VP≤1.7","VP≤2.0",
               "STABLE","!RISING","FLAT_vwap","Rng≤0.4","Rng≤0.6"]
# Add enrichment filters to 3-filter combos if available
if "prior_day_return" in go.columns:
    top_filters += ["PrDayDn","PrDayUp","PrRng≤1","PrRng≤0.8",
                    "InWkRng","InMoRng","5dRet>0","5dRet<0",
                    "PrRV<15","PrRV<12","RVchg<0"]
results_3f = []
for n1, n2, n3 in combinations(top_filters, 3):
    mask = filters[n1] & filters[n2] & filters[n3]
    r = eval_filter_with_mechanics(mask, f"{n1} + {n2} + {n3}")
    if r and r["n"] >= 10:
        results_3f.append(r)

results_3f.sort(key=lambda x: -x["pf"])
print(f"\n  Top 15 three-filter combos (min 10 trades, by PF):")
for r in results_3f[:15]:
    print_r(r)

results_3f.sort(key=lambda x: -x["total"])
print(f"\n  Top 10 three-filter combos (by total $):")
for r in results_3f[:10]:
    print_r(r)


# ════════════════════════════════════════════════════════════════════════════
# PHASE 4: ROBUSTNESS — Does the edge hold across time?
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*150}")
print(f"  PHASE 4: ROBUSTNESS CHECK")
print(f"{'='*150}")

mid = go["date"].median()
h1 = go[go["date"] <= mid]
h2 = go[go["date"] > mid]
print(f"\n  H1: {h1['date'].min().strftime('%Y-%m-%d')} to {h1['date'].max().strftime('%Y-%m-%d')} ({len(h1)} trades)")
print(f"  H2: {h2['date'].min().strftime('%Y-%m-%d')} to {h2['date'].max().strftime('%Y-%m-%d')} ({len(h2)} trades)")

# Test top combos on each half
results_3f.sort(key=lambda x: -x["pf"])
print(f"\n  Top combos tested on each half:")
for r in results_3f[:15]:
    label = r["label"]
    parts = label.split(" + ")
    mask_h1 = pd.Series(True, index=h1.index)
    mask_h2 = pd.Series(True, index=h2.index)
    for p in parts:
        if p in filters:
            mask_h1 &= filters[p].reindex(h1.index, fill_value=False)
            mask_h2 &= filters[p].reindex(h2.index, fill_value=False)

    r1 = eval_mechanics(h1[mask_h1], best_tgt, best_ts, True, "")
    r2 = eval_mechanics(h2[mask_h2], best_tgt, best_ts, True, "")

    def qs(r):
        if r is None or r["n"] < 3: return " <3 trades  "
        return f"{r['n']:>3}t ${r['total']:>+10,.0f} PF={r['pf']:.2f}"

    robust = "✓" if (r1 and r2 and r1["n"]>=3 and r2["n"]>=3 and r1["pf"]>1.0 and r2["pf"]>1.0) else "✗"
    print(f"    {robust} {label:<50} H1: {qs(r1)}  |  H2: {qs(r2)}")


# ════════════════════════════════════════════════════════════════════════════
# PHASE 5: WINNER PROFILE
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*150}")
print(f"  PHASE 5: WINNER vs LOSER PROFILE")
print(f"{'='*150}")

winners = go[go["sim_pnl"] > 0]
losers = go[go["sim_pnl"] <= 0]

print(f"\n  {'Variable':<20} {'Winners (n={})'.format(len(winners)):>25} {'Losers (n={})'.format(len(losers)):>25} {'Delta':>10}")
profile_cols = ["vix", "rv", "vp_ratio", "range_pct", "score", "wing_width"]
if "prior_day_return" in go.columns:
    profile_cols += ["prior_day_return", "prior_day_range", "prior_day_body_pct",
                     "prior_5d_return", "pct_in_weekly_range", "pct_in_monthly_range",
                     "prior_day_rv", "rv_1d_change"]
for col in profile_cols:
    w_mean = winners[col].mean()
    l_mean = losers[col].mean()
    delta = w_mean - l_mean
    print(f"  {col:<20} {w_mean:>24.2f} {l_mean:>24.2f} {delta:>+9.2f}")

cat_cols = ["rv_slope", "vwap_slope", "dow"]
if "prior_day_direction" in go.columns:
    cat_cols += ["prior_day_direction", "in_prior_week_range", "in_prior_month_range"]
for col in cat_cols:
    if col in go.columns:
        print(f"\n  {col} — Winners: {winners[col].value_counts().to_dict()}")
        print(f"  {col} — Losers:  {losers[col].value_counts().to_dict()}")

print(f"\n{'='*150}")
print(f"  DONE — Use these findings to build the optimal filtered backtest")
print(f"{'='*150}")
