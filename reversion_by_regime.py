"""
REVERSION ANALYSIS BY REGIME
==============================
Re-runs the mean reversion analysis but ONLY on days matching each
strategy's regime filter -- not all GO days.

The thesis: regime filters are designed to catch "pin" days where SPX
is expected to mean-revert. If true, the reversion rate on regime-matched
days should be significantly higher than the 35% overall average.

If regimes DO select for pinning, same-strike adds make MORE sense
because you're betting on reversion on days specifically chosen for it.

Usage:
    python3 reversion_by_regime.py
"""

import os, json, sys
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
CHECKPOINTS_60M = [("1100",60),("1200",120),("1300",180),("1400",240),("1500",300)]
CHECKPOINTS_30M = [("1030",30),("1100",60),("1130",90),("1200",120),("1230",150),
                   ("1300",180),("1330",210),("1400",240),("1430",270),("1500",300),
                   ("1530",330),("1545",345)]

STRATEGIES = [
    {"ver":"v4",  "regime":"MID_DN_IN_GFL",  "mech":"40%/close/5T30m", "filter":None,
     "vix":[15,20], "pd":"DN", "rng":"IN",  "gap":"GFL"},
    {"ver":"v5",  "regime":"MID_UP_OT_GUP",  "mech":"40%/close/5T60m", "filter":"5dRet>0",
     "vix":[15,20], "pd":"UP", "rng":"OT",  "gap":"GUP"},
    {"ver":"v10", "regime":"MID_DN_OT_GFL",  "mech":"70%/1545/5T60m",  "filter":None,
     "vix":[15,20], "pd":"DN", "rng":"OT",  "gap":"GFL"},
    {"ver":"v11", "regime":"LOW_UP_OT_GFL",  "mech":"70%/close/3T60m", "filter":"VP<=2.0",
     "vix":[0,15],  "pd":"UP", "rng":"OT",  "gap":"GFL"},
    {"ver":"v12", "regime":"LOW_UP_OT_GUP",  "mech":"40%/close/5T60m", "filter":"5dRet>1",
     "vix":[0,15],  "pd":"UP", "rng":"OT",  "gap":"GUP"},
    {"ver":"v13", "regime":"LOW_DN_IN_GUP",  "mech":"40%/close/5T60m", "filter":"Rng<=0.3",
     "vix":[0,15],  "pd":"DN", "rng":"IN",  "gap":"GUP"},
]

# ====================================================================
# LOAD DATA
# ====================================================================
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)

# Gap classification
GAP_CACHE = os.path.join(_DIR, "spx_gap_cache.json")
with open(GAP_CACHE) as f:
    gap_data = json.load(f)
def classify_gap(date_val):
    d_str = date_val.strftime("%Y-%m-%d")
    gp = gap_data.get(d_str, None)
    if gp is None: return "UNK"
    if gp < -0.25: return "GDN"
    elif gp > 0.25: return "GUP"
    else: return "GFL"
go["gap"] = go["date"].apply(classify_gap)

# SPX intraday data
SPX_CACHE = os.path.join(_DIR, "spx_intraday_cache.json")
with open(SPX_CACHE) as f:
    spx_intraday = json.load(f)
print(f"  {len(go)} GO days, {len(spx_intraday)} days with intraday data")

# ====================================================================
# HELPERS
# ====================================================================
def match_regime(row, strat):
    vix = row["vix"]
    vix_lo, vix_hi = strat["vix"]
    if not (vix_lo <= vix < vix_hi): return False
    pd_map = {"UP": "UP", "DOWN": "DN", "FLAT": "FL"}
    pd_val = pd_map.get(str(row.get("prior_day_direction", "")), "")
    if pd_val != strat["pd"]: return False
    rng = "IN" if row.get("in_prior_week_range", 0) == 1 else "OT"
    if rng != strat["rng"]: return False
    if row.get("gap", "UNK") != strat["gap"]: return False
    return True

def check_filter(row, filt):
    if filt is None: return True
    if filt == "5dRet>0": return row.get("prior_5d_return", 0) > 0
    if filt == "5dRet>1": return row.get("prior_5d_return", 0) > 1
    if filt == "VP<=2.0": return row.get("vp_ratio", 99) <= 2.0
    if filt == "Rng<=0.3": return row.get("range_pct", 99) <= 0.3
    return True

def parse_mech(mech_str):
    parts = mech_str.split("/")
    tranche_str = parts[2]
    if "60m" in tranche_str: return CHECKPOINTS_60M
    elif "30m" in tranche_str: return CHECKPOINTS_30M
    return []

# ====================================================================
# ANALYSIS: ALL GO DAYS (baseline)
# ====================================================================
print(f"\n{'='*120}")
print("  MEAN REVERSION BY REGIME")
print(f"  Question: Do regime-filtered days show more pinning/reversion than average?")
print(f"{'='*120}")

# First: baseline across all GO days
CP_LABELS = ["1030", "1100", "1200", "1300", "1400", "1500"]

def compute_reversion_stats(matching_rows, label, checkpoints_to_analyze):
    """Compute reversion stats for a set of matching rows at specific checkpoints."""
    results = {}

    for cp in checkpoints_to_analyze:
        drifts = []
        reversions = []
        abs_drifts = []
        close_drifts = []

        for row in matching_rows:
            d_str = row["date"].strftime("%Y-%m-%d")
            if d_str not in spx_intraday: continue
            day = spx_intraday[d_str]
            if "1000" not in day or cp not in day or "close" not in day: continue

            entry_spx = day["1000"]
            cp_spx = day[cp]
            close_spx = day["close"]

            cp_drift = cp_spx - entry_spx
            close_drift = close_spx - entry_spx

            drifts.append(cp_drift)
            abs_drifts.append(abs(cp_drift))
            close_drifts.append(close_drift)

            # Only compute reversion for meaningful drifts (>2pts)
            if abs(cp_drift) > 2:
                rev = 1.0 - (close_drift / cp_drift)
                reversions.append(rev)

        if not drifts:
            continue

        darr = np.array(drifts)
        adarr = np.array(abs_drifts)

        rev_stats = {}
        if reversions:
            rarr = np.array(reversions)
            rev_stats = {
                "n_meaningful": len(rarr),
                "avg_rev": rarr.mean(),
                "median_rev": np.median(rarr),
                "pct_revert_50": (rarr > 0.5).mean() * 100,
                "pct_continue": (rarr < 0).mean() * 100,
                "pct_full_revert": (rarr > 0.9).mean() * 100,
            }

        results[cp] = {
            "n": len(darr),
            "mean_drift": darr.mean(),
            "median_abs_drift": np.median(adarr),
            "pct_gt5": (adarr > 5).mean() * 100,
            "pct_gt10": (adarr > 10).mean() * 100,
            "pct_gt20": (adarr > 20).mean() * 100,
            **rev_stats,
        }

    return results

# Compute close-to-close range (how much SPX moves from 10am to close)
def compute_daily_range_stats(matching_rows):
    """Compute stats on total daily SPX movement from entry to close."""
    ranges = []
    for row in matching_rows:
        d_str = row["date"].strftime("%Y-%m-%d")
        if d_str not in spx_intraday: continue
        day = spx_intraday[d_str]
        if "1000" not in day or "close" not in day: continue
        entry = day["1000"]
        close = day["close"]
        ranges.append(abs(close - entry))
    if not ranges:
        return None
    arr = np.array(ranges)
    return {
        "n": len(arr),
        "mean": arr.mean(),
        "median": np.median(arr),
        "pct_lt5": (arr < 5).mean() * 100,
        "pct_lt10": (arr < 10).mean() * 100,
        "pct_lt15": (arr < 15).mean() * 100,
        "pct_lt20": (arr < 20).mean() * 100,
    }


# ── Baseline: ALL GO days ──
all_rows = [row for _, row in go.iterrows()]
baseline_rev = compute_reversion_stats(all_rows, "ALL GO DAYS", CP_LABELS)
baseline_range = compute_daily_range_stats(all_rows)

print(f"\n  BASELINE: ALL {len(all_rows)} GO DAYS")
print(f"  Daily |SPX move| from 10am to close:")
if baseline_range:
    print(f"    Mean: {baseline_range['mean']:.1f} pts  |  Median: {baseline_range['median']:.1f} pts")
    print(f"    <5pts: {baseline_range['pct_lt5']:.0f}%  |  <10pts: {baseline_range['pct_lt10']:.0f}%  |  <15pts: {baseline_range['pct_lt15']:.0f}%  |  <20pts: {baseline_range['pct_lt20']:.0f}%")

print(f"\n  {'CP':>6} {'N':>4} {'Med|Drift|':>11} {'|D|>10':>8} {'Meaningful':>11} {'AvgRev':>8} {'MedRev':>8} {'%Rev>50':>8} {'%Cont':>7} {'%FullRev':>9}")
print(f"  {'-'*95}")
for cp in CP_LABELS:
    if cp not in baseline_rev: continue
    s = baseline_rev[cp]
    nr = s.get("n_meaningful", 0)
    if nr > 0:
        print(f"  {cp:>6} {s['n']:>4} {s['median_abs_drift']:>9.1f}pt {s['pct_gt10']:>6.0f}% {nr:>9}d {s['avg_rev']:>+7.2f} {s['median_rev']:>+7.2f} {s['pct_revert_50']:>6.0f}% {s['pct_continue']:>5.0f}% {s['pct_full_revert']:>7.0f}%")

# ── Per-strategy regime analysis ──
for strat in STRATEGIES:
    cps = parse_mech(strat["mech"])
    cp_labels_for_strat = [c[0] for c in cps[:4]]  # first 4 add checkpoints

    # Also always include key times
    for t in ["1100", "1200", "1300", "1400"]:
        if t not in cp_labels_for_strat:
            cp_labels_for_strat.append(t)
    cp_labels_for_strat = sorted(set(cp_labels_for_strat))

    # Filter matching days
    matching = []
    for _, row in go.iterrows():
        if match_regime(row, strat) and check_filter(row, strat["filter"]):
            matching.append(row)

    if len(matching) < 3:
        print(f"\n  {strat['ver'].upper()} ({strat['regime']}) -- {len(matching)} days, too few to analyze")
        continue

    regime_rev = compute_reversion_stats(matching, strat["regime"], cp_labels_for_strat)
    regime_range = compute_daily_range_stats(matching)

    print(f"\n\n  {'='*110}")
    print(f"  {strat['ver'].upper()} -- {strat['regime']}  |  {len(matching)} matching days  |  Filter: {strat['filter'] or 'none'}")
    print(f"  {'='*110}")

    if regime_range:
        print(f"  Daily |SPX move| from 10am to close:")
        print(f"    Mean: {regime_range['mean']:.1f} pts  |  Median: {regime_range['median']:.1f} pts")
        print(f"    <5pts: {regime_range['pct_lt5']:.0f}%  |  <10pts: {regime_range['pct_lt10']:.0f}%  |  <15pts: {regime_range['pct_lt15']:.0f}%  |  <20pts: {regime_range['pct_lt20']:.0f}%")

        # Compare to baseline
        if baseline_range:
            diff = regime_range['median'] - baseline_range['median']
            pct_diff = diff / baseline_range['median'] * 100
            pin_label = "MORE pinning" if diff < 0 else "LESS pinning"
            print(f"    vs baseline median: {'+'if diff>=0 else ''}{diff:.1f} pts ({pct_diff:+.0f}%) -- {pin_label} than average")

    print(f"\n  Reversion at add checkpoints:")
    print(f"  {'CP':>6} {'N':>4} {'Med|Drift|':>11} {'|D|>10':>8} {'Meaningful':>11} {'AvgRev':>8} {'MedRev':>8} {'%Rev>50':>8} {'%Cont':>7}  vs Baseline")
    print(f"  {'-'*105}")

    for cp in cp_labels_for_strat:
        if cp not in regime_rev: continue
        s = regime_rev[cp]
        nr = s.get("n_meaningful", 0)

        # Compare to baseline
        bl = baseline_rev.get(cp, {})
        bl_rev50 = bl.get("pct_revert_50", 0)
        bl_cont = bl.get("pct_continue", 0)

        if nr > 0:
            rev50_diff = s["pct_revert_50"] - bl_rev50
            cont_diff = s["pct_continue"] - bl_cont
            better = "BETTER" if rev50_diff > 5 else ("WORSE" if rev50_diff < -5 else "SIMILAR")
            print(f"  {cp:>6} {s['n']:>4} {s['median_abs_drift']:>9.1f}pt {s['pct_gt10']:>6.0f}% {nr:>9}d {s['avg_rev']:>+7.2f} {s['median_rev']:>+7.2f} {s['pct_revert_50']:>6.0f}% {s['pct_continue']:>5.0f}%  Rev50 {'+'if rev50_diff>=0 else ''}{rev50_diff:.0f}pp  {better}")
        else:
            print(f"  {cp:>6} {s['n']:>4} {s['median_abs_drift']:>9.1f}pt {s['pct_gt10']:>6.0f}% {'(too few meaningful drifts)':>50}")

# ====================================================================
# FINAL VERDICT
# ====================================================================
print(f"\n\n{'='*120}")
print("  VERDICT: Do regime filters select for pinning days?")
print(f"{'='*120}")
print(f"\n  Compare each regime's reversion rate to the baseline ({baseline_rev.get('1100',{}).get('pct_revert_50',0):.0f}% revert >50% at 11:00 across all GO days)")
print(f"\n  If a regime shows significantly HIGHER reversion rates than baseline,")
print(f"  same-strike adds are justified -- the regime IS catching pin days.")
print(f"\n  If reversion rates are similar to baseline,")
print(f"  the regime isn't specifically selecting for pinning, and re-centered adds are safer.")

# Also: analyze the P&L of JUST the add tranches on reversion vs continuation days
print(f"\n\n{'='*120}")
print("  ADD-TRANCHE P&L: REVERSION vs CONTINUATION DAYS")
print(f"  How do the adds perform on days where SPX reverts vs continues?")
print(f"{'='*120}")

for strat in STRATEGIES:
    cps_raw = parse_mech(strat["mech"])
    target_pct = int(strat["mech"].split("/")[0].replace("%", ""))
    time_stop = strat["mech"].split("/")[1]
    n_t = int(strat["mech"].split("/")[2].split("T")[0])

    matching = []
    for _, row in go.iterrows():
        if match_regime(row, strat) and check_filter(row, strat["filter"]):
            matching.append(row)

    if len(matching) < 5:
        continue

    # For each day, classify as reversion or continuation based on
    # first add checkpoint, then compute the add tranches' P&L
    revert_add_pnl = []
    continue_add_pnl = []

    for row in matching:
        d_str = row["date"].strftime("%Y-%m-%d")
        if d_str not in spx_intraday: continue
        day = spx_intraday[d_str]
        if "1000" not in day or "close" not in day: continue

        entry_spx = day["1000"]
        close_spx = day["close"]

        # Get the first add checkpoint
        if len(cps_raw) < 1: continue
        first_cp = cps_raw[0][0]
        if first_cp not in day: continue

        first_cp_spx = day[first_cp]
        cp_drift = first_cp_spx - entry_spx
        close_drift = close_spx - entry_spx

        if abs(cp_drift) < 2: continue  # not enough drift to classify

        # Classify: reversion or continuation
        rev_ratio = 1.0 - (close_drift / cp_drift)
        is_reversion = rev_ratio > 0.5

        # Compute add-only P&L (tranches 2-N, NOT tranche 1)
        # Using existing backtest columns
        exit_pnl_val = row.get("pnl_at_close")  # simplified: use close as exit

        # Try to find actual exit
        ts_h, ts_m = TS_HM.get(time_stop, (16,0))
        tgt_time = row.get(f"hit_{target_pct}_time", "")
        tgt_pnl = row.get(f"hit_{target_pct}_pnl")
        ws_time = row.get("ws_time", "")
        ws_pnl = row.get("ws_pnl")

        events = []
        if ws_time and pd.notna(ws_pnl):
            try:
                wt = pd.Timestamp(ws_time)
                if wt.hour < ts_h or (wt.hour == ts_h and wt.minute < ts_m):
                    events.append(("WS", wt, ws_pnl))
            except: pass
        if pd.notna(tgt_pnl) and tgt_time:
            try:
                tt = pd.Timestamp(tgt_time)
                if tt.hour < ts_h or (tt.hour == ts_h and tt.minute < ts_m):
                    events.append(("TGT", tt, tgt_pnl))
            except: pass

        if events:
            events.sort(key=lambda x: x[1])
            exit_pnl_val = events[0][2]
            exit_h, exit_m = events[0][1].hour, events[0][1].minute
        else:
            ts_col = f"pnl_at_{time_stop}" if time_stop != "close" else "pnl_at_close"
            exit_pnl_val = row.get(ts_col, row.get("pnl_at_close"))
            exit_h, exit_m = ts_h, ts_m

        if exit_pnl_val is None or pd.isna(exit_pnl_val):
            continue

        # Sum P&L for add tranches only (k=2 to n_t)
        add_total = 0
        n_adds = 0
        for k in range(2, n_t + 1):
            if k - 2 >= len(cps_raw): break
            cp_lbl, _ = cps_raw[k - 2]
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            cp_h, cp_m = TS_HM.get(cp_lbl, (16,0))
            if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue
            add_pnl = exit_pnl_val - cp_pnl  # per-spread add P&L
            add_total += add_pnl
            n_adds += 1

        if n_adds > 0:
            if is_reversion:
                revert_add_pnl.append(add_total)
            else:
                continue_add_pnl.append(add_total)

    n_rev = len(revert_add_pnl)
    n_cont = len(continue_add_pnl)

    if n_rev < 2 and n_cont < 2:
        continue

    print(f"\n  {strat['ver'].upper()} -- {strat['regime']}  ({n_rev + n_cont} days with classifiable drift)")

    if n_rev > 0:
        rev_arr = np.array(revert_add_pnl)
        rev_avg = rev_arr.mean()
        rev_wr = (rev_arr > 0).mean() * 100
        rev_total = rev_arr.sum()
        print(f"    REVERSION days ({n_rev:>3}):  Avg add P&L: {rev_avg:>+7.2f}/spread  |  WR: {rev_wr:.0f}%  |  Total: {rev_total:>+8.2f}")

    if n_cont > 0:
        cont_arr = np.array(continue_add_pnl)
        cont_avg = cont_arr.mean()
        cont_wr = (cont_arr > 0).mean() * 100
        cont_total = cont_arr.sum()
        print(f"    CONTINUATION days ({n_cont:>3}):  Avg add P&L: {cont_avg:>+7.2f}/spread  |  WR: {cont_wr:.0f}%  |  Total: {cont_total:>+8.2f}")

    if n_rev > 0 and n_cont > 0:
        better = "REVERSION" if np.mean(revert_add_pnl) > np.mean(continue_add_pnl) else "CONTINUATION"
        print(f"    Same-strike adds do better on: {better} days")

print(f"\n{'='*120}")
