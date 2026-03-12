"""
DRIFT-TRIGGERED ADD ANALYSIS
==============================
Instead of adding tranches on a fixed timer (every 30m/60m),
add when SPX drifts away from the original center strike.

Concept:
  - Entry tranche at 10:00 (always)
  - Additional tranches added when |SPX - center| >= drift threshold
  - "Buys the dip" on the butterfly when it's at a discount

Approaches tested:
  A) TIMER (baseline): current multi-tranche system, adds on schedule
  B) DRIFT (equal split): same $100K budget split among entry + potential
     drift adds. Adds only fire when drift threshold is met. On quiet
     pinning days, fewer tranches deployed = less capital at work.
  C) 1T REFERENCE: full $100K at entry, no adds

Drift thresholds: 5, 8, 10, 15, 20 points from center
Max adds: matches each strategy's current tranche config

Applies to multi-tranche strategies: V4, V5, V10, V11, V12, V13

Data sources:
  - research_all_trades.csv: P&L at 30-min checkpoints
  - spx_intraday_cache.json: SPX prices at 30-min intervals
  - spx_gap_cache.json: gap classification for regime matching

Usage:
    python3 drift_triggered_adds.py
"""

import os, json, sys
import pandas as pd
import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# CONSTANTS (from ensemble_v3.py)
# ============================================================================
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

# All 30-min checkpoints in order (used for drift checking)
ALL_CPS = ["1030","1100","1130","1200","1230","1300","1330","1400","1430","1500","1530","1545"]

CHECKPOINTS_60M = [("1100",60),("1200",120),("1300",180),("1400",240),("1500",300)]
CHECKPOINTS_30M = [("1030",30),("1100",60),("1130",90),("1200",120),("1230",150),
                   ("1300",180),("1330",210),("1400",240),("1430",270),("1500",300),
                   ("1530",330),("1545",345)]

# ============================================================================
# STRATEGY DEFINITIONS (multi-tranche only)
# ============================================================================
STRATEGIES = [
    {"ver":"V4",  "regime":"MID_DN_IN_GFL",  "mech":"40%/close/5T30m", "filter":None,
     "vix":[15,20], "pd":"DN", "rng":"IN",  "gap":"GFL"},
    {"ver":"V5",  "regime":"MID_UP_OT_GUP",  "mech":"40%/close/5T60m", "filter":"5dRet>0",
     "vix":[15,20], "pd":"UP", "rng":"OT",  "gap":"GUP"},
    {"ver":"V10", "regime":"MID_DN_OT_GFL",  "mech":"70%/1545/5T60m",  "filter":None,
     "vix":[15,20], "pd":"DN", "rng":"OT",  "gap":"GFL"},
    {"ver":"V11", "regime":"LOW_UP_OT_GFL",  "mech":"70%/close/3T60m", "filter":"VP<=2.0",
     "vix":[0,15],  "pd":"UP", "rng":"OT",  "gap":"GFL"},
    {"ver":"V12", "regime":"LOW_UP_OT_GUP",  "mech":"40%/close/5T60m", "filter":"5dRet>1",
     "vix":[0,15],  "pd":"UP", "rng":"OT",  "gap":"GUP"},
    {"ver":"V13", "regime":"LOW_DN_IN_GUP",  "mech":"40%/close/5T60m", "filter":"Rng<=0.3",
     "vix":[0,15],  "pd":"DN", "rng":"IN",  "gap":"GUP"},
]

DRIFT_THRESHOLDS = [5, 8, 10, 15, 20]

# ============================================================================
# LOAD DATA
# ============================================================================
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)
N = len(go)
print(f"  {N} GO days  |  {go['date'].min().date()} to {go['date'].max().date()}")

print("Loading SPX intraday cache...", flush=True)
with open(os.path.join(_DIR, "spx_intraday_cache.json")) as f:
    spx_intraday = json.load(f)
print(f"  {len(spx_intraday)} days in cache")

print("Loading gap cache...", flush=True)
with open(os.path.join(_DIR, "spx_gap_cache.json")) as f:
    gap_data = json.load(f)

def classify_gap(date_val):
    d_str = date_val.strftime("%Y-%m-%d")
    gp = gap_data.get(d_str, None)
    if gp is None: return "UNK"
    if gp < -0.25: return "GDN"
    elif gp > 0.25: return "GUP"
    else: return "GFL"

go["gap"] = go["date"].apply(classify_gap)

# ============================================================================
# SIMULATION HELPERS
# ============================================================================
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

def get_exit_hm(row, outcome, target_pct, time_stop):
    if outcome == "WS": ets = row.get("ws_time", "")
    elif outcome == "TGT": ets = row.get(f"hit_{target_pct}_time", "")
    else: ets = time_stop
    if ets in TS_HM: return TS_HM[ets]
    try:
        et = pd.Timestamp(ets)
        return et.hour, et.minute
    except: return 16, 0

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
    target_pct = int(parts[0].replace("%", ""))
    time_stop = parts[1]
    tranche_str = parts[2]
    n_t = int(tranche_str.split("T")[0])
    if "60m" in tranche_str: cps = CHECKPOINTS_60M
    elif "30m" in tranche_str: cps = CHECKPOINTS_30M
    else: cps = []
    return target_pct, time_stop, n_t, cps

def compute_stats(pnl_list):
    arr = np.array(pnl_list)
    n_traded = int(np.sum(arr != 0))
    if n_traded == 0:
        return {"trades": 0, "total": 0, "avg": 0, "wr": 0, "pf": 0,
                "max_dd": 0, "worst": 0, "best": 0}
    traded = arr[arr != 0]
    total = arr.sum()
    wins = traded[traded > 0]
    losses = traded[traded < 0]
    wr = len(wins) / len(traded) * 100
    w_sum = wins.sum() if len(wins) > 0 else 0
    l_sum = abs(losses.sum()) if len(losses) > 0 else 0.001
    pf = w_sum / l_sum if l_sum > 0 else 99.0
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum).max()
    return {"trades": n_traded, "total": round(total, 2), "avg": round(total/n_traded, 2),
            "wr": round(wr, 1), "pf": round(pf, 2), "max_dd": round(dd, 2),
            "worst": round(arr.min(), 2), "best": round(arr.max(), 2)}

# ============================================================================
# SIMULATION MODE 1: TIMER-BASED (baseline from ensemble_v3.py)
# ============================================================================
def simulate_timer(row, target_pct, time_stop, n_tranches, interval_cps, risk_allocated):
    """Standard timer-based tranche adds. Returns (pnl, outcome, n_adds)."""
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0, "ND", 0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)

    rps = row.get("risk_deployed_p1", 0)
    nsp = row.get("n_spreads_p1", 0)
    ml_ps = (rps / nsp) if (nsp > 0 and rps > 0) else TRANCHE_RISK
    if ml_ps <= 0: ml_ps = TRANCHE_RISK
    rpt = risk_allocated / max(1, n_tranches)
    n_per = max(1, int(rpt / ml_ps))

    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
    n_adds = 0

    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_cps): break
        cp_lbl, _ = interval_cps[k - 2]
        cp_pnl = row.get(f"pnl_at_{cp_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl): continue
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue
        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        total += tk_pnl
        n_adds += 1

    return round(total, 2), outcome, n_adds

# ============================================================================
# SIMULATION MODE 2: 1T REFERENCE (no adds)
# ============================================================================
def simulate_1t(row, target_pct, time_stop, risk_allocated):
    """Single tranche, full capital. Returns (pnl, outcome)."""
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0, "ND"

    rps = row.get("risk_deployed_p1", 0)
    nsp = row.get("n_spreads_p1", 0)
    ml_ps = (rps / nsp) if (nsp > 0 and rps > 0) else TRANCHE_RISK
    if ml_ps <= 0: ml_ps = TRANCHE_RISK
    n_per = max(1, int(risk_allocated / ml_ps))

    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
    return round(total, 2), outcome

# ============================================================================
# SIMULATION MODE 3: DRIFT-TRIGGERED ADDS
# ============================================================================
def simulate_drift(row, target_pct, time_stop, max_adds, drift_threshold,
                   risk_allocated, spx_prices):
    """
    Drift-triggered tranche adds.

    Entry tranche at 10:00 (always). Additional tranches when |SPX - center|
    >= drift_threshold at a checkpoint.

    Capital split equally: risk_allocated / (1 + max_adds) per tranche.
    Adds that don't fire = undeployed capital.

    Returns (pnl, outcome, n_adds_fired, add_checkpoints, max_drift_seen).
    """
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND":
        return 0.0, "ND", 0, [], 0.0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)

    rps = row.get("risk_deployed_p1", 0)
    nsp = row.get("n_spreads_p1", 0)
    ml_ps = (rps / nsp) if (nsp > 0 and rps > 0) else TRANCHE_RISK
    if ml_ps <= 0: ml_ps = TRANCHE_RISK

    total_tranches = 1 + max_adds
    rpt = risk_allocated / total_tranches
    n_per = max(1, int(rpt / ml_ps))

    # Entry tranche P&L
    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    center = spx_prices.get("1000")
    if center is None:
        return round(total, 2), outcome, 0, [], 0.0

    adds_made = 0
    add_cps = []
    max_drift = 0.0

    for cp_lbl in ALL_CPS:
        if adds_made >= max_adds:
            break

        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m):
            break

        spx_now = spx_prices.get(cp_lbl)
        if spx_now is None:
            continue

        drift = abs(spx_now - center)
        max_drift = max(max_drift, drift)

        if drift >= drift_threshold:
            # Drift exceeds threshold -- trigger add at this checkpoint
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl):
                continue
            tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total += tk_pnl
            adds_made += 1
            add_cps.append(cp_lbl)

    return round(total, 2), outcome, adds_made, add_cps, max_drift

# ============================================================================
# MAIN ANALYSIS
# ============================================================================
print(f"\n{'='*120}")
print("  DRIFT-TRIGGERED ADD ANALYSIS")
print(f"  Concept: Add tranches when SPX drifts from center, not on a timer")
print(f"  Capital: $100K risk budget (split equally among entry + max adds)")
print(f"  Drift thresholds tested: {DRIFT_THRESHOLDS} pts")
print(f"{'='*120}")

# Per-strategy results
all_strategy_results = []

for strat in STRATEGIES:
    target_pct, time_stop, n_t, timer_cps = parse_mech(strat["mech"])
    max_adds = n_t - 1  # 5T = 4 adds, 3T = 2 adds

    # Filter matching days
    matching = []
    for _, row in go.iterrows():
        if match_regime(row, strat) and check_filter(row, strat["filter"]):
            d_str = row["date"].strftime("%Y-%m-%d")
            if d_str in spx_intraday:
                matching.append((row, spx_intraday[d_str]))
            else:
                matching.append((row, None))

    n_match = len(matching)
    if n_match == 0:
        print(f"\n  {strat['ver']} ({strat['regime']}) -- 0 matching days, SKIPPING")
        continue

    # ---- Simulate all modes ----
    pnl_timer = []
    pnl_1t = []
    timer_adds_total = 0

    # Drift results keyed by threshold
    pnl_drift = {th: [] for th in DRIFT_THRESHOLDS}
    drift_add_counts = {th: [] for th in DRIFT_THRESHOLDS}
    drift_max_drifts = {th: [] for th in DRIFT_THRESHOLDS}

    for row, spx_prices in matching:
        # Timer baseline
        p_timer, o_timer, n_adds_t = simulate_timer(
            row, target_pct, time_stop, n_t, timer_cps, DAILY_RISK)
        pnl_timer.append(p_timer)
        timer_adds_total += n_adds_t

        # 1T reference
        p_1t, o_1t = simulate_1t(row, target_pct, time_stop, DAILY_RISK)
        pnl_1t.append(p_1t)

        # Drift at each threshold
        for th in DRIFT_THRESHOLDS:
            if spx_prices is not None:
                p_drift, o_drift, n_adds_d, add_cps_d, mx_drift = simulate_drift(
                    row, target_pct, time_stop, max_adds, th,
                    DAILY_RISK, spx_prices)
            else:
                # No intraday data -- fall back to 1T-equivalent (entry only, same split)
                p_drift, o_drift, n_adds_d, mx_drift = 0.0, "ND", 0, 0.0
                ep, eo = find_exit_pnl(row, target_pct, time_stop)
                if ep is not None and eo != "ND":
                    rps = row.get("risk_deployed_p1", 0)
                    nsp = row.get("n_spreads_p1", 0)
                    ml_ps = (rps / nsp) if (nsp > 0 and rps > 0) else TRANCHE_RISK
                    if ml_ps <= 0: ml_ps = TRANCHE_RISK
                    rpt = DAILY_RISK / (1 + max_adds)
                    n_per = max(1, int(rpt / ml_ps))
                    p_drift = round(ep * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT, 2)

            pnl_drift[th].append(p_drift)
            drift_add_counts[th].append(n_adds_d)
            drift_max_drifts[th].append(mx_drift)

    # ---- Compute stats ----
    s_timer = compute_stats(pnl_timer)
    s_1t = compute_stats(pnl_1t)
    s_drift = {th: compute_stats(pnl_drift[th]) for th in DRIFT_THRESHOLDS}

    # ---- Print ----
    print(f"\n{'='*120}")
    print(f"  {strat['ver']} -- {strat['regime']}")
    print(f"  Current: {strat['mech']}  |  Filter: {strat['filter'] or 'none'}  |  {n_match} matching days")
    print(f"  Max adds: {max_adds} (total {n_t}T)  |  Target: {target_pct}%  |  Time stop: {time_stop}")
    print(f"{'='*120}")

    hdr = f"  {'Mode':>22} {'Total P&L':>14} {'WinRate':>8} {'PF':>7} {'MaxDD':>14} {'Avg/Trade':>12} {'Trades':>7}"
    print(hdr)
    print(f"  {'-'*90}")

    def fmt_row(label, s):
        return (f"  {label:>22} ${s['total']:>12,.0f} {s['wr']:>6.1f}% {s['pf']:>6.2f} "
                f"${s['max_dd']:>12,.0f} ${s['avg']:>10,.0f} {s['trades']:>7}")

    print(fmt_row(f"Timer ({n_t}T)", s_timer))
    print(fmt_row("1T (no adds)", s_1t))
    print(f"  {'-'*90}")

    for th in DRIFT_THRESHOLDS:
        sd = s_drift[th]
        adds_arr = np.array(drift_add_counts[th])
        avg_adds = adds_arr.mean() if len(adds_arr) > 0 else 0
        pct_any = (adds_arr > 0).mean() * 100 if len(adds_arr) > 0 else 0
        label = f"Drift {th}pt ({avg_adds:.1f} adds)"
        print(fmt_row(label, sd))

    # ---- Drift frequency & add distribution ----
    print(f"\n  Drift add frequency (how often adds fire):")
    print(f"  {'Threshold':>12} {'Any add':>10} {'1 add':>8} {'2 adds':>8} {'3 adds':>8} {'4 adds':>8} {'Avg adds':>10} {'Avg max drift':>15}")
    print(f"  {'-'*85}")

    for th in DRIFT_THRESHOLDS:
        adds_arr = np.array(drift_add_counts[th])
        drifts_arr = np.array(drift_max_drifts[th])
        pct_any = (adds_arr > 0).mean() * 100
        pct_1 = (adds_arr == 1).mean() * 100
        pct_2 = (adds_arr == 2).mean() * 100
        pct_3 = (adds_arr == 3).mean() * 100
        pct_4 = (adds_arr >= 4).mean() * 100
        avg_a = adds_arr.mean()
        avg_d = drifts_arr.mean()
        print(f"  {th:>10}pt {pct_any:>8.0f}% {pct_1:>6.0f}% {pct_2:>6.0f}% {pct_3:>6.0f}% {pct_4:>6.0f}% {avg_a:>10.2f} {avg_d:>13.1f}pt")

    # ---- Timer vs best drift ----
    best_th = max(DRIFT_THRESHOLDS, key=lambda th: s_drift[th]["pf"])
    best_sd = s_drift[best_th]
    delta_total = best_sd["total"] - s_timer["total"]
    delta_pf = best_sd["pf"] - s_timer["pf"]

    print(f"\n  Best drift threshold: {best_th}pt")
    print(f"  Delta vs Timer:  P&L {'+'if delta_total>=0 else ''}${delta_total:,.0f}  |  PF {'+'if delta_pf>=0 else ''}{delta_pf:.2f}")

    if best_sd["pf"] > s_timer["pf"]:
        print(f"  --> DRIFT {best_th}pt WINS by PF")
    else:
        print(f"  --> TIMER WINS by PF")

    # ---- Reversion vs continuation breakdown ----
    # Classify each day by whether SPX reverted or continued from peak drift
    print(f"\n  P&L breakdown by drift behavior:")
    print(f"  {'Category':>20} {'Days':>6} {'Timer Total':>14} {'Timer PF':>9} | {'Drift {best_th}pt Total':>16} {'Drift PF':>9}")
    print(f"  {'-'*85}")

    # Categorize: small drift (<5pt), drift+revert, drift+continue
    cat_small = {"timer": [], "drift": [], "count": 0}
    cat_revert = {"timer": [], "drift": [], "count": 0}
    cat_continue = {"timer": [], "drift": [], "count": 0}

    for i, (row, spx_prices) in enumerate(matching):
        if spx_prices is None:
            continue
        center = spx_prices.get("1000")
        if center is None:
            continue

        # Find max drift and close drift
        max_d = 0
        for cp in ALL_CPS:
            px = spx_prices.get(cp)
            if px is not None:
                max_d = max(max_d, abs(px - center))

        close_px = spx_prices.get("close")
        if close_px is not None:
            close_d = abs(close_px - center)
        else:
            close_d = max_d

        if max_d < 5:
            cat_small["timer"].append(pnl_timer[i])
            cat_small["drift"].append(pnl_drift[best_th][i])
            cat_small["count"] += 1
        elif close_d < max_d * 0.5:
            # Reverted: close drift < 50% of max drift
            cat_revert["timer"].append(pnl_timer[i])
            cat_revert["drift"].append(pnl_drift[best_th][i])
            cat_revert["count"] += 1
        else:
            cat_continue["timer"].append(pnl_timer[i])
            cat_continue["drift"].append(pnl_drift[best_th][i])
            cat_continue["count"] += 1

    for label, cat in [("Small drift (<5pt)", cat_small),
                        ("Drift + revert", cat_revert),
                        ("Drift + continue", cat_continue)]:
        if cat["count"] == 0:
            continue
        t_arr = np.array(cat["timer"])
        d_arr = np.array(cat["drift"])
        t_tot = t_arr.sum()
        d_tot = d_arr.sum()
        t_w = t_arr[t_arr > 0].sum(); t_l = abs(t_arr[t_arr < 0].sum())
        d_w = d_arr[d_arr > 0].sum(); d_l = abs(d_arr[d_arr < 0].sum())
        t_pf = t_w / t_l if t_l > 0 else 99
        d_pf = d_w / d_l if d_l > 0 else 99
        print(f"  {label:>20} {cat['count']:>6} ${t_tot:>12,.0f} {t_pf:>8.2f} | ${d_tot:>14,.0f} {d_pf:>8.2f}")

    all_strategy_results.append({
        "ver": strat["ver"],
        "regime": strat["regime"],
        "n_days": n_match,
        "n_t": n_t,
        "timer": s_timer,
        "one_t": s_1t,
        "drift": s_drift,
        "best_drift_th": best_th,
    })


# ============================================================================
# CROSS-STRATEGY SUMMARY
# ============================================================================
print(f"\n\n{'='*120}")
print("  CROSS-STRATEGY SUMMARY: Timer vs Best Drift vs 1T")
print(f"{'='*120}")

print(f"\n  {'Ver':>5} {'Days':>5} {'nT':>3} | {'Timer PnL':>12} {'Timer PF':>9} | {'BestDrift':>10} {'Drift PnL':>12} {'Drift PF':>9} | {'1T PnL':>12} {'1T PF':>7} | {'Winner':>8}")
print(f"  {'-'*115}")

total_timer = 0
total_drift = 0
total_1t = 0

for r in all_strategy_results:
    bt = r["best_drift_th"]
    sd = r["drift"][bt]
    st = r["timer"]
    s1 = r["one_t"]

    total_timer += st["total"]
    total_drift += sd["total"]
    total_1t += s1["total"]

    candidates = [("Timer", st["pf"]), (f"D{bt}pt", sd["pf"]), ("1T", s1["pf"])]
    winner = max(candidates, key=lambda x: x[1])[0]

    print(f"  {r['ver']:>5} {r['n_days']:>5} {r['n_t']:>3} | "
          f"${st['total']:>10,.0f} {st['pf']:>8.2f} | "
          f"{bt:>8}pt ${sd['total']:>10,.0f} {sd['pf']:>8.2f} | "
          f"${s1['total']:>10,.0f} {s1['pf']:>6.2f} | "
          f"{winner:>8}")

print(f"  {'-'*115}")
print(f"  {'TOTAL':>5} {'':>5} {'':>3} | ${total_timer:>10,.0f} {'':>8} | {'':>8}   ${total_drift:>10,.0f} {'':>8} | ${total_1t:>10,.0f}")

# ============================================================================
# DEEP DIVE: Per-day add timing analysis
# ============================================================================
print(f"\n\n{'='*120}")
print("  DEEP DIVE: When do drift adds fire? (aggregate across all strategies)")
print(f"{'='*120}")

# Count how often each checkpoint triggers a drift add at each threshold
cp_add_counts = {th: {cp: 0 for cp in ALL_CPS} for th in DRIFT_THRESHOLDS}
cp_total_days = 0

for strat in STRATEGIES:
    target_pct, time_stop, n_t, timer_cps = parse_mech(strat["mech"])
    max_adds = n_t - 1

    for _, row in go.iterrows():
        if not match_regime(row, strat) or not check_filter(row, strat["filter"]):
            continue
        d_str = row["date"].strftime("%Y-%m-%d")
        if d_str not in spx_intraday:
            continue

        spx_prices = spx_intraday[d_str]
        center = spx_prices.get("1000")
        if center is None:
            continue

        cp_total_days += 1

        for th in DRIFT_THRESHOLDS:
            for cp in ALL_CPS:
                px = spx_prices.get(cp)
                if px is not None and abs(px - center) >= th:
                    cp_add_counts[th][cp] += 1

print(f"\n  % of regime-matched days where |SPX - center| >= threshold at each checkpoint:")
print(f"  (Total days across all strategies: {cp_total_days})")
print(f"\n  {'Checkpoint':>12}", end="")
for th in DRIFT_THRESHOLDS:
    print(f" {th:>6}pt", end="")
print()
print(f"  {'-'*(12 + 8*len(DRIFT_THRESHOLDS))}")

for cp in ALL_CPS:
    print(f"  {cp:>12}", end="")
    for th in DRIFT_THRESHOLDS:
        pct = cp_add_counts[th][cp] / cp_total_days * 100 if cp_total_days > 0 else 0
        print(f" {pct:>5.0f}%", end="")
    print()

# ============================================================================
# ANALYSIS: Does undeployed capital hurt drift strategy?
# ============================================================================
print(f"\n\n{'='*120}")
print("  CAPITAL DEPLOYMENT ANALYSIS")
print(f"  Timer deploys all tranches every day. Drift only deploys on drift days.")
print(f"  How much capital goes undeployed?")
print(f"{'='*120}")

for r in all_strategy_results:
    print(f"\n  {r['ver']}:")
    n_t = r["n_t"]
    max_adds = n_t - 1
    rpt_pct = 100.0 / n_t  # % per tranche

    for th in DRIFT_THRESHOLDS:
        sd = r["drift"][th]
        # Average adds per day
        # We need to recompute... use the stored data
        # Actually let's just show the implication
        pass

    print(f"    Timer: always deploys {n_t} tranches = 100% of ${DAILY_RISK:,}")
    print(f"    Drift: deploys 1 entry tranche ({rpt_pct:.0f}%) + adds only on drift days")
    print(f"    On quiet pinning days (best days!), drift only uses {rpt_pct:.0f}% of capital")


print(f"\n\n{'='*120}")
print("  VERDICT")
print(f"{'='*120}")
print(f"""
  The drift-triggered add model has a fundamental TRADEOFF:

  ADVANTAGE:
    - Buys the butterfly at a discount when SPX drifts
    - Better timing of adds (targets moments of maximum discount)
    - Fewer adds on quiet days = less slippage cost

  DISADVANTAGE:
    - Capital is RESERVED for adds that may never fire
    - On quiet pinning days (butterfly's BEST days), only entry tranche
      is deployed = less capital capturing the profit
    - Timer-based adds deploy ALL capital every day regardless

  The regime filters select for PINNING days. On a perfect pin day,
  SPX barely moves, the butterfly wins big. Timer deploys 100% of
  capital on those days. Drift deploys only {100//5:.0f}% (for 5T) or {100//3:.0f}% (for 3T).

  This is why the drift model may UNDERPERFORM on the exact days
  the strategy is designed to capture.
""")

print(f"{'='*120}")
print("  DONE")
print(f"{'='*120}")
