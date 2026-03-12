"""
V3 PHOENIX: Pin Day Analysis
==============================
Do PHOENIX signal days select for pinning / mean reversion?
If yes, V3 should go back to multi-tranche (5T60m) with same-strike adds.

PHOENIX signals (any 1+ firing = trade day):
  1. VIX<=20 + VP<=1.0 + 5dRet>0
  2. VP<=1.3 + PrDayDn + 5dRet>0
  3. VP<=1.2 + 5dRet>0 + RVchg>0
  4. VP<=1.5 + OutWkRng + 5dRet>0
  5. VP<=1.3 + !RISING + 5dRet>0

Also tests: 1T vs 5T60m backtest comparison (already done, but showing
alongside reversion data for context).

Usage:
    python3 reversion_v3_phoenix.py
"""

import os, json
import pandas as pd
import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))

# ====================================================================
# CONSTANTS
# ====================================================================
TS_HM = {
    "1030": (10,30), "1100": (11,0), "1130": (11,30),
    "1200": (12,0), "1230": (12,30), "1300": (13,0),
    "1330": (13,30), "1400": (14,0), "1430": (14,30),
    "1500": (15,0), "1530": (15,30), "1545": (15,45),
    "close": (16,0),
}
DAILY_RISK = 100_000
SPX_MULT = 100
SLIPPAGE_PER_SPREAD = 1.00
TRANCHE_RISK = 25_000
CHECKPOINTS_60M = [("1100",60),("1200",120),("1300",180),("1400",240),("1500",300)]

# ====================================================================
# LOAD DATA
# ====================================================================
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)
N = len(go)

with open(os.path.join(_DIR, "spx_intraday_cache.json")) as f:
    spx_intraday = json.load(f)

print(f"  {N} GO days, {len(spx_intraday)} days with intraday data")

# ====================================================================
# PHOENIX SIGNAL CLASSIFICATION
# ====================================================================
def phoenix_signals(row):
    """Count how many PHOENIX signals fire for a given day. Returns (count, list_of_firing)."""
    signals = []

    vix = row.get("vix", 99)
    vp = row.get("vp_ratio", 99)
    ret5d = row.get("prior_5d_return", -99)
    pd_dir = str(row.get("prior_day_direction", ""))
    rv_slope = str(row.get("rv_slope", ""))
    rv_chg = row.get("rv_1d_change", -99)
    in_wk = row.get("in_prior_week_range", 1)

    # Signal 1: VIX<=20 + VP<=1.0 + 5dRet>0
    if vix <= 20 and vp <= 1.0 and ret5d > 0:
        signals.append("S1: VIX<=20+VP<=1.0+5dRet>0")

    # Signal 2: VP<=1.3 + PrDayDn + 5dRet>0
    if vp <= 1.3 and pd_dir == "DOWN" and ret5d > 0:
        signals.append("S2: VP<=1.3+PrDayDn+5dRet>0")

    # Signal 3: VP<=1.2 + 5dRet>0 + RVchg>0
    if vp <= 1.2 and ret5d > 0 and rv_chg > 0:
        signals.append("S3: VP<=1.2+5dRet>0+RVchg>0")

    # Signal 4: VP<=1.5 + OutWkRng + 5dRet>0
    if vp <= 1.5 and in_wk == 0 and ret5d > 0:
        signals.append("S4: VP<=1.5+OutWkRng+5dRet>0")

    # Signal 5: VP<=1.3 + !RISING + 5dRet>0
    if vp <= 1.3 and rv_slope != "RISING" and ret5d > 0:
        signals.append("S5: VP<=1.3+!RISING+5dRet>0")

    return len(signals), signals

# Classify all GO days
go["phx_count"] = 0
go["phx_signals"] = ""
for idx, row in go.iterrows():
    cnt, sigs = phoenix_signals(row)
    go.at[idx, "phx_count"] = cnt
    go.at[idx, "phx_signals"] = "; ".join(sigs)

# Summary
print(f"\n  PHOENIX signal distribution across {N} GO days:")
for n in range(6):
    ct = (go["phx_count"] == n).sum()
    print(f"    {n} signals: {ct} days ({ct/N*100:.1f}%)")

phx_days = go[go["phx_count"] >= 1]
print(f"\n  V3 trades (1+ signals): {len(phx_days)} days")

# By confluence level
for n in range(1, 6):
    ct = (go["phx_count"] == n).sum()
    if ct > 0:
        print(f"    Exactly {n}: {ct} days")

# ====================================================================
# REVERSION ANALYSIS: PHOENIX DAYS vs ALL DAYS
# ====================================================================
CP_LABELS = ["1030", "1100", "1200", "1300", "1400", "1500"]

def compute_reversion(rows, label):
    """Compute reversion stats for a set of rows."""
    results = {}
    ranges = []

    for _, row in rows.iterrows():
        d_str = row["date"].strftime("%Y-%m-%d")
        if d_str not in spx_intraday: continue
        day = spx_intraday[d_str]
        if "1000" not in day or "close" not in day: continue
        entry = day["1000"]
        close = day["close"]
        ranges.append(abs(close - entry))

    range_stats = None
    if ranges:
        rarr = np.array(ranges)
        range_stats = {
            "n": len(rarr), "mean": rarr.mean(), "median": np.median(rarr),
            "pct_lt5": (rarr < 5).mean()*100, "pct_lt10": (rarr < 10).mean()*100,
            "pct_lt15": (rarr < 15).mean()*100, "pct_lt20": (rarr < 20).mean()*100,
        }

    for cp in CP_LABELS:
        reversions = []
        drifts = []

        for _, row in rows.iterrows():
            d_str = row["date"].strftime("%Y-%m-%d")
            if d_str not in spx_intraday: continue
            day = spx_intraday[d_str]
            if "1000" not in day or cp not in day or "close" not in day: continue

            entry = day["1000"]
            cp_drift = day[cp] - entry
            close_drift = day["close"] - entry
            drifts.append(abs(cp_drift))

            if abs(cp_drift) > 2:
                rev = 1.0 - (close_drift / cp_drift)
                reversions.append(rev)

        if not drifts: continue
        darr = np.array(drifts)

        res = {"n": len(darr), "median_abs_drift": np.median(darr),
               "pct_gt10": (darr > 10).mean()*100}

        if reversions:
            rarr = np.array(reversions)
            res.update({
                "n_meaningful": len(rarr),
                "avg_rev": rarr.mean(), "median_rev": np.median(rarr),
                "pct_revert_50": (rarr > 0.5).mean()*100,
                "pct_continue": (rarr < 0).mean()*100,
            })
        results[cp] = res

    return results, range_stats


# ── ALL GO days baseline ──
bl_rev, bl_range = compute_reversion(go, "ALL GO")

# ── PHOENIX 1+ signal days ──
phx_rev, phx_range = compute_reversion(phx_days, "PHOENIX 1+")

# ── By confluence level ──
phx2_days = go[go["phx_count"] >= 2]
phx3_days = go[go["phx_count"] >= 3]
phx2_rev, phx2_range = compute_reversion(phx2_days, "PHOENIX 2+")
phx3_rev, phx3_range = compute_reversion(phx3_days, "PHOENIX 3+")


print(f"\n{'='*120}")
print("  PHOENIX SIGNAL DAYS: PINNING ANALYSIS")
print(f"{'='*120}")

def print_range_comparison(label, rs, bl_rs):
    if rs is None: return
    print(f"\n  {label}  ({rs['n']} days)")
    print(f"    Daily |SPX move| 10am->close:")
    print(f"      Mean: {rs['mean']:.1f} pts  |  Median: {rs['median']:.1f} pts")
    print(f"      <5pts: {rs['pct_lt5']:.0f}%  |  <10pts: {rs['pct_lt10']:.0f}%  |  <15pts: {rs['pct_lt15']:.0f}%  |  <20pts: {rs['pct_lt20']:.0f}%")
    if bl_rs:
        diff = rs['median'] - bl_rs['median']
        pct = diff / bl_rs['median'] * 100
        pin = "MORE pinning" if diff < 0 else "LESS pinning"
        print(f"      vs baseline: {'+'if diff>=0 else ''}{diff:.1f} pts ({pct:+.0f}%) -- {pin}")

print_range_comparison("BASELINE: ALL GO DAYS", bl_range, None)
print_range_comparison("PHOENIX 1+ signals", phx_range, bl_range)
print_range_comparison("PHOENIX 2+ signals", phx2_range, bl_range)
print_range_comparison("PHOENIX 3+ signals", phx3_range, bl_range)


print(f"\n\n  REVERSION AT ADD CHECKPOINTS (60m intervals -- V3's original 5T60m)")
print(f"  {'':>18} {'CP':>6} {'N':>4} {'Med|Drift|':>11} {'%Rev>50':>8} {'%Cont':>7} {'MedRev':>8}  vs Baseline")
print(f"  {'-'*95}")

for cp in ["1100", "1200", "1300", "1400"]:
    # Baseline
    bl = bl_rev.get(cp, {})
    bl_r50 = bl.get("pct_revert_50", 0)

    # Phoenix 1+
    p1 = phx_rev.get(cp, {})
    if "pct_revert_50" in p1:
        d = p1["pct_revert_50"] - bl_r50
        tag = "BETTER" if d > 5 else ("WORSE" if d < -5 else "SIMILAR")
        print(f"  {'PHOENIX 1+':>18} {cp:>6} {p1['n']:>4} {p1['median_abs_drift']:>9.1f}pt {p1['pct_revert_50']:>6.0f}% {p1['pct_continue']:>5.0f}% {p1['median_rev']:>+7.2f}  Rev50 {'+'if d>=0 else ''}{d:.0f}pp {tag}")

    # Phoenix 2+
    p2 = phx2_rev.get(cp, {})
    if "pct_revert_50" in p2:
        d = p2["pct_revert_50"] - bl_r50
        tag = "BETTER" if d > 5 else ("WORSE" if d < -5 else "SIMILAR")
        print(f"  {'PHOENIX 2+':>18} {cp:>6} {p2['n']:>4} {p2['median_abs_drift']:>9.1f}pt {p2['pct_revert_50']:>6.0f}% {p2['pct_continue']:>5.0f}% {p2['median_rev']:>+7.2f}  Rev50 {'+'if d>=0 else ''}{d:.0f}pp {tag}")

    # Phoenix 3+
    p3 = phx3_rev.get(cp, {})
    if "pct_revert_50" in p3:
        d = p3["pct_revert_50"] - bl_r50
        tag = "BETTER" if d > 5 else ("WORSE" if d < -5 else "SIMILAR")
        print(f"  {'PHOENIX 3+':>18} {cp:>6} {p3['n']:>4} {p3['median_abs_drift']:>9.1f}pt {p3['pct_revert_50']:>6.0f}% {p3['pct_continue']:>5.0f}% {p3['median_rev']:>+7.2f}  Rev50 {'+'if d>=0 else ''}{d:.0f}pp {tag}")

    print()

# ====================================================================
# ADD-TRANCHE P&L: REVERSION vs CONTINUATION on PHOENIX days
# ====================================================================
print(f"\n{'='*120}")
print("  ADD-TRANCHE P&L ON PHOENIX DAYS: REVERSION vs CONTINUATION")
print(f"  Using 50%/close/5T60m mechanics (V3's original best config)")
print(f"{'='*120}")

target_pct = 50
time_stop = "close"
cps = CHECKPOINTS_60M
n_t = 5

def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str): return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except: return False

def find_exit(row):
    ts_h, ts_m = 16, 0  # close
    tgt_time = row.get(f"hit_{target_pct}_time", "")
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")
    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")
    events = []
    if ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_h, ts_m):
        try: events.append(("WS", pd.Timestamp(ws_time), ws_pnl))
        except: pass
    if pd.notna(tgt_pnl) and tgt_time and time_before(tgt_time, ts_h, ts_m):
        try: events.append(("TGT", pd.Timestamp(tgt_time), tgt_pnl))
        except: pass
    if events:
        events.sort(key=lambda x: x[1])
        return events[0][2], events[0][0], events[0][1].hour, events[0][1].minute
    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl): return close_pnl, "TS", 16, 0
    return None, "ND", 16, 0

for min_sig in [1, 2, 3]:
    subset = go[go["phx_count"] >= min_sig]
    if len(subset) < 5: continue

    revert_add = []
    continue_add = []
    revert_total = []
    continue_total = []

    for _, row in subset.iterrows():
        d_str = row["date"].strftime("%Y-%m-%d")
        if d_str not in spx_intraday: continue
        day = spx_intraday[d_str]
        if "1000" not in day or "close" not in day: continue

        entry_spx = day["1000"]
        close_spx = day["close"]

        exit_pnl, outcome, exit_h, exit_m = find_exit(row)
        if exit_pnl is None: continue

        # First add checkpoint for reversion classification
        first_cp = "1100"
        if first_cp not in day: continue
        cp_drift = day[first_cp] - entry_spx
        close_drift = close_spx - entry_spx

        if abs(cp_drift) < 2: continue
        rev_ratio = 1.0 - (close_drift / cp_drift)
        is_revert = rev_ratio > 0.5

        # Position sizing
        rps = row.get("risk_deployed_p1", 0)
        nsp = row.get("n_spreads_p1", 0)
        ml = (rps / nsp) if (nsp > 0 and rps > 0) else TRANCHE_RISK
        if ml <= 0: ml = TRANCHE_RISK
        rpt = DAILY_RISK / n_t
        n_per = max(1, int(rpt / ml))
        n_per_full = max(1, int(DAILY_RISK / ml))

        # Full 5T P&L
        total_5t = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        add_only = 0
        for k in range(2, n_t + 1):
            if k-2 >= len(cps): break
            cl, _ = cps[k-2]
            cp_pnl = row.get(f"pnl_at_{cl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            ch, cm = TS_HM.get(cl, (16,0))
            if ch > exit_h or (ch == exit_h and cm >= exit_m): continue
            tk = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total_5t += tk
            add_only += tk

        # 1T P&L
        total_1t = exit_pnl * n_per_full * SPX_MULT - n_per_full * SLIPPAGE_PER_SPREAD * SPX_MULT

        if is_revert:
            revert_add.append(add_only)
            revert_total.append((total_5t, total_1t))
        else:
            continue_add.append(add_only)
            continue_total.append((total_5t, total_1t))

    nr = len(revert_add)
    nc = len(continue_add)

    print(f"\n  PHOENIX {min_sig}+ signals  ({nr + nc} classifiable days)")
    if nr > 0:
        rev_arr = np.array(revert_add)
        rev_5t = np.array([t[0] for t in revert_total])
        rev_1t = np.array([t[1] for t in revert_total])
        print(f"    REVERSION days ({nr:>3}):  Add-only avg: ${rev_arr.mean():>+10,.0f}  |  5T total: ${rev_5t.sum():>+12,.0f}  |  1T total: ${rev_1t.sum():>+12,.0f}  |  5T-1T: ${rev_5t.sum()-rev_1t.sum():>+10,.0f}")

    if nc > 0:
        cont_arr = np.array(continue_add)
        cont_5t = np.array([t[0] for t in continue_total])
        cont_1t = np.array([t[1] for t in continue_total])
        print(f"    CONTINUATION days ({nc:>3}):  Add-only avg: ${cont_arr.mean():>+10,.0f}  |  5T total: ${cont_5t.sum():>+12,.0f}  |  1T total: ${cont_1t.sum():>+12,.0f}  |  5T-1T: ${cont_5t.sum()-cont_1t.sum():>+10,.0f}")

    if nr > 0 and nc > 0:
        rev_add_avg = np.mean(revert_add)
        cont_add_avg = np.mean(continue_add)
        print(f"    Same-strike adds better on: {'REVERSION' if rev_add_avg > cont_add_avg else 'CONTINUATION'} days")
        total_add = sum(revert_add) + sum(continue_add)
        print(f"    Net add-tranche value: ${total_add:>+12,.0f} ({'ADDS HELP' if total_add > 0 else 'ADDS HURT'})")

# ====================================================================
# VERDICT
# ====================================================================
print(f"\n\n{'='*120}")
print("  VERDICT: Should V3 go back to 5T60m?")
print(f"{'='*120}")
print(f"\n  Current config: 1T (full size at entry)")
print(f"  Question: Do PHOENIX signals select for pinning days where")
print(f"  same-strike adds would capture reversion?")
print(f"\n  If PHOENIX days show elevated reversion + add tranches are profitable:")
print(f"    -> Switch V3 back to 5T60m")
print(f"  If PHOENIX days show normal/low reversion:")
print(f"    -> Keep V3 at 1T")
print(f"\n{'='*120}")
