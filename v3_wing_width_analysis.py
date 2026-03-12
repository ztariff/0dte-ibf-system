"""
V3 PHOENIX Wing Width Analysis
================================
The backtest data uses VIX-scaled wing widths (~VIX * 2.8).
The live cockpit uses fixed 50pt wings.

This analysis answers:
  1. How does V3 perform at each wing width?
  2. Is VIX-scaling better than fixed width?
  3. What's the optimal wing width rule?
  4. Credit quality by wing width (credit / wing ratio)
"""

import pandas as pd
import numpy as np

# --- Load data ---
df = pd.read_csv("research_all_trades.csv")
df["date"] = pd.to_datetime(df["date"])
df["ml_per_spread"] = df["risk_deployed_p1"] / df["n_spreads_p1"].clip(lower=1)
df["credit"] = df["wing_width"] - df["ml_per_spread"] / 100

# Reconstruct PHOENIX signal count
def count_phoenix(row):
    vix = row.get("vix", 99)
    vp = row.get("vp_ratio", 99)
    ret5d = row.get("prior_5d_return", 0)
    pd_dir = str(row.get("prior_day_direction", "")).upper()
    rv_chg = row.get("rv_1d_change", 0)
    in_range = row.get("in_prior_week_range", True)
    rv_slope = str(row.get("rv_slope", "")).upper()
    c = 0
    if vix <= 20 and vp <= 1.0 and ret5d > 0: c += 1
    if vp <= 1.3 and pd_dir in ("DN","DOWN") and ret5d > 0: c += 1
    if vp <= 1.2 and ret5d > 0 and rv_chg > 0: c += 1
    if vp <= 1.5 and not in_range and ret5d > 0: c += 1
    if vp <= 1.3 and rv_slope != "RISING" and ret5d > 0: c += 1
    return c

df["signal_count"] = df.apply(count_phoenix, axis=1)
v3 = df[df["signal_count"] >= 1].copy()

# --- Constants ---
RISK = 100000
SPX_MULT = 100

def find_exit_pnl(row, target_pct, time_stop):
    ts_map = {"close": (16, 0), "1545": (15, 45), "1530": (15, 30)}
    ts_h, ts_m = ts_map.get(time_stop, (16, 0))
    events = []
    ws_pnl, ws_time = row.get("ws_pnl"), row.get("ws_time")
    if pd.notna(ws_pnl) and pd.notna(ws_time):
        try:
            wt = pd.Timestamp(ws_time)
            if wt.hour < ts_h or (wt.hour == ts_h and wt.minute < ts_m):
                events.append(("WS", wt, ws_pnl))
        except: pass
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")
    tgt_time = row.get(f"hit_{target_pct}_time")
    if pd.notna(tgt_pnl) and pd.notna(tgt_time):
        try:
            tt = pd.Timestamp(tgt_time)
            if tt.hour < ts_h or (tt.hour == ts_h and tt.minute < ts_m):
                events.append(("TGT", tt, tgt_pnl))
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

def sim_trade(row, target_pct=50, time_stop="close"):
    credit = row["credit"]
    ww = row["wing_width"]
    ml_per = (ww - credit) * SPX_MULT
    if ml_per <= 0: return 0, "ND"
    n = max(1, int(RISK / ml_per))
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None: return 0, "ND"
    return round(exit_pnl * n * SPX_MULT), outcome

def stats(results):
    total = results["pnl"].sum()
    wins = results[results["pnl"] > 0]["pnl"].sum()
    losses = abs(results[results["pnl"] < 0]["pnl"].sum())
    pf = wins / losses if losses > 0 else 99
    wr = (results["pnl"] > 0).mean() * 100
    cum = results["pnl"].cumsum()
    dd = (cum.cummax() - cum).max()
    return total, pf, wr, dd

# --- Simulate baseline ---
results = []
for _, row in v3.iterrows():
    pnl, outcome = sim_trade(row, 50, "close")
    results.append({
        "date": row["date"], "pnl": pnl, "outcome": outcome,
        "ww": row["wing_width"], "vix": row["vix"],
        "credit": row["credit"], "sc": row["signal_count"],
    })
rdf = pd.DataFrame(results)

b_total, b_pf, b_wr, b_dd = stats(rdf)
print("=" * 80)
print("V3 WING WIDTH ANALYSIS")
print("=" * 80)
print(f"Baseline: {len(rdf)} trades, ${b_total:,.0f}, PF {b_pf:.2f}, WR {b_wr:.1f}%")
print()

# ================================================================
# 1. Performance by wing width
# ================================================================
print("=" * 80)
print("1. V3 Performance by Wing Width")
print("=" * 80)
print()

rdf["credit_pct"] = rdf["credit"] / rdf["ww"] * 100  # credit as % of wing
rdf["pnl_per_dollar_risk"] = rdf["pnl"] / RISK  # normalize by risk

print(f"  {'WW':>4} {'N':>4} {'Total P&L':>12} {'Avg P&L':>10} {'Win%':>6} {'PF':>6} {'Credit%':>8} {'VIX Avg':>8}")
print(f"  {'-'*65}")
for ww in sorted(rdf["ww"].unique()):
    s = rdf[rdf["ww"] == ww]
    t, pf, wr, _ = stats(s)
    avg_credit_pct = s["credit_pct"].mean()
    avg_vix = s["vix"].mean()
    print(f"  {ww:>4} {len(s):>4} ${t:>11,.0f} ${s['pnl'].mean():>9,.0f} {wr:>5.1f}% {pf:>5.2f} {avg_credit_pct:>6.1f}% {avg_vix:>7.1f}")
print()

# ================================================================
# 2. Group into buckets for statistical significance
# ================================================================
print("=" * 80)
print("2. Wing Width Buckets (grouped for sample size)")
print("=" * 80)
print()

def ww_bucket(ww):
    if ww <= 40: return "<=40 (tight)"
    elif ww <= 50: return "45-50 (medium)"
    elif ww <= 60: return "55-60 (wide)"
    else: return "65+ (very wide)"

rdf["ww_bucket"] = rdf["ww"].apply(ww_bucket)

print(f"  {'Bucket':<20} {'N':>4} {'Total P&L':>12} {'Avg P&L':>10} {'Win%':>6} {'PF':>6} {'VIX':>6}")
print(f"  {'-'*68}")
for bucket in ["<=40 (tight)", "45-50 (medium)", "55-60 (wide)", "65+ (very wide)"]:
    s = rdf[rdf["ww_bucket"] == bucket]
    if len(s) == 0: continue
    t, pf, wr, _ = stats(s)
    print(f"  {bucket:<20} {len(s):>4} ${t:>11,.0f} ${s['pnl'].mean():>9,.0f} {wr:>5.1f}% {pf:>5.2f} {s['vix'].mean():>5.1f}")
print()

# ================================================================
# 3. Credit as % of wing width (quality metric)
# ================================================================
print("=" * 80)
print("3. Credit Quality: Credit as % of Wing Width")
print("=" * 80)
print("Higher credit% = more premium collected relative to risk = better value.")
print()

# Quartile by credit%
rdf["cq"] = pd.qcut(rdf["credit_pct"], 4, labels=["Q1 (low%)", "Q2", "Q3", "Q4 (high%)"], duplicates="drop")
print(f"  {'Quartile':<15} {'Credit% Range':>15} {'N':>4} {'Total P&L':>12} {'Win%':>6} {'PF':>6} {'Avg WW':>7}")
print(f"  {'-'*68}")
for q in rdf["cq"].cat.categories:
    s = rdf[rdf["cq"] == q]
    t, pf, wr, _ = stats(s)
    print(f"  {q:<15} {s['credit_pct'].min():>5.1f}%-{s['credit_pct'].max():<5.1f}% {len(s):>4} ${t:>11,.0f} {wr:>5.1f}% {pf:>5.2f} {s['ww'].mean():>6.1f}")
print()

# ================================================================
# 4. VIX-to-Wing ratio analysis
# ================================================================
print("=" * 80)
print("4. VIX-to-Wing Ratio (how aggressive is the scaling?)")
print("=" * 80)
print()

rdf["vix_ww_ratio"] = rdf["vix"] / rdf["ww"]
print(f"  VIX/WW ratio stats: mean={rdf['vix_ww_ratio'].mean():.3f}, "
      f"min={rdf['vix_ww_ratio'].min():.3f}, max={rdf['vix_ww_ratio'].max():.3f}")
print(f"  Implied: WW = VIX / {rdf['vix_ww_ratio'].mean():.3f} = VIX * {1/rdf['vix_ww_ratio'].mean():.2f}")
print()

# What if wing width was fixed at different values?
# We can't re-simulate with different WW (data only has one WW per day)
# But we CAN check: on days where WW=40, would we have had better or worse
# results if the data had been collected with WW=50 or WW=60?
# Answer: we can't know from this data.
#
# What we CAN do: compare return per unit risk across WW buckets

print("  --- Return per $1 risked by WW bucket ---")
print(f"  {'Bucket':<20} {'Avg Ret/$Risk':>14} {'Med Ret/$Risk':>14}")
print(f"  {'-'*50}")
for bucket in ["<=40 (tight)", "45-50 (medium)", "55-60 (wide)", "65+ (very wide)"]:
    s = rdf[rdf["ww_bucket"] == bucket]
    if len(s) == 0: continue
    avg_ret = s["pnl_per_dollar_risk"].mean()
    med_ret = s["pnl_per_dollar_risk"].median()
    print(f"  {bucket:<20} {avg_ret:>13.3f}x {med_ret:>13.3f}x")
print()

# ================================================================
# 5. Outcome distribution by wing width
# ================================================================
print("=" * 80)
print("5. Exit Outcome Distribution by Wing Width")
print("=" * 80)
print("TGT=profit target hit, TS=time stop, WS=wing stop (max loss)")
print()

print(f"  {'Bucket':<20} {'N':>4} {'TGT':>5} {'TS':>5} {'WS':>5} {'TGT%':>6} {'WS%':>6}")
print(f"  {'-'*55}")
for bucket in ["<=40 (tight)", "45-50 (medium)", "55-60 (wide)", "65+ (very wide)"]:
    s = rdf[rdf["ww_bucket"] == bucket]
    if len(s) == 0: continue
    tgt = (s["outcome"] == "TGT").sum()
    ts = (s["outcome"] == "TS").sum()
    ws = (s["outcome"] == "WS").sum()
    print(f"  {bucket:<20} {len(s):>4} {tgt:>5} {ts:>5} {ws:>5} {tgt/len(s)*100:>5.1f}% {ws/len(s)*100:>5.1f}%")
print()

# ================================================================
# 6. Win/Loss magnitude by wing width
# ================================================================
print("=" * 80)
print("6. Win/Loss Magnitude by Wing Width")
print("=" * 80)
print()

print(f"  {'Bucket':<20} {'Avg Win':>10} {'Avg Loss':>10} {'Win/Loss':>10} {'Worst Day':>12}")
print(f"  {'-'*65}")
for bucket in ["<=40 (tight)", "45-50 (medium)", "55-60 (wide)", "65+ (very wide)"]:
    s = rdf[rdf["ww_bucket"] == bucket]
    if len(s) == 0: continue
    avg_win = s[s["pnl"] > 0]["pnl"].mean() if (s["pnl"] > 0).any() else 0
    avg_loss = s[s["pnl"] < 0]["pnl"].mean() if (s["pnl"] < 0).any() else 0
    ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 99
    worst = s["pnl"].min()
    print(f"  {bucket:<20} ${avg_win:>9,.0f} ${avg_loss:>9,.0f} {ratio:>9.2f}x ${worst:>11,.0f}")
print()

# ================================================================
# 7. What the cockpit should use
# ================================================================
print("=" * 80)
print("7. Recommendation: Fixed vs VIX-Scaled Wing Width")
print("=" * 80)
print()

# The data already uses VIX-scaled wings. Let's see the implied formula.
# Fit a simple linear: WW = a * VIX + b
from numpy.polynomial import polynomial as P
coeffs = np.polyfit(rdf["vix"], rdf["ww"], 1)
print(f"  Current scaling formula (from data): WW = {coeffs[0]:.2f} * VIX + {coeffs[1]:.2f}")
print(f"    At VIX=14: WW = {coeffs[0]*14 + coeffs[1]:.0f}")
print(f"    At VIX=18: WW = {coeffs[0]*18 + coeffs[1]:.0f}")
print(f"    At VIX=22: WW = {coeffs[0]*22 + coeffs[1]:.0f}")
print(f"    At VIX=30: WW = {coeffs[0]*30 + coeffs[1]:.0f}")
print()

# Compare: if cockpit used fixed 50pt for all trades...
# Only days where WW is close to 50 are directly comparable
near_50 = rdf[(rdf["ww"] >= 45) & (rdf["ww"] <= 55)]
not_50 = rdf[(rdf["ww"] < 45) | (rdf["ww"] > 55)]
print(f"  Trades near WW=50 (45-55): {len(near_50)} trades")
t, pf, wr, _ = stats(near_50)
print(f"    Total: ${t:,.0f}, PF: {pf:.2f}, WR: {wr:.1f}%")
print(f"  Trades far from WW=50 (<45 or >55): {len(not_50)} trades")
t2, pf2, wr2, _ = stats(not_50)
print(f"    Total: ${t2:,.0f}, PF: {pf2:.2f}, WR: {wr2:.1f}%")
print()

# Round WW to nearest standard value
print("  --- Simplified WW tiers (for live use) ---")
tiers = [
    ("VIX < 15", rdf[rdf["vix"] < 15]),
    ("VIX 15-18", rdf[(rdf["vix"] >= 15) & (rdf["vix"] < 18)]),
    ("VIX 18-22", rdf[(rdf["vix"] >= 18) & (rdf["vix"] < 22)]),
    ("VIX >= 22", rdf[rdf["vix"] >= 22]),
]
print(f"  {'VIX Range':<12} {'N':>4} {'Avg WW':>7} {'Total P&L':>12} {'PF':>6} {'WS%':>6}")
print(f"  {'-'*50}")
for name, s in tiers:
    if len(s) == 0: continue
    t, pf, wr, _ = stats(s)
    ws_pct = (s["outcome"] == "WS").mean() * 100
    print(f"  {name:<12} {len(s):>4} {s['ww'].mean():>6.0f} ${t:>11,.0f} {pf:>5.2f} {ws_pct:>5.1f}%")
print()

print("=" * 80)
print("DONE")
print("=" * 80)
