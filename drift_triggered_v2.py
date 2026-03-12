"""
DRIFT-TRIGGERED ADDS v2 -- IMPROVED MODELS
============================================
The v1 analysis showed drift wins 3/6 by PF but has a capital deployment
problem: on quiet pinning days, only entry tranche deploys.

This v2 tests improved approaches:

MODEL A: "Timer baseline" -- current system (same as v1)
MODEL B: "Drift equal split" -- same as v1 (for reference)
MODEL C: "Drift heavy entry" -- asymmetric split: entry gets 60%,
         drift adds split remaining 40%
MODEL D: "Drift undeployed recapture" -- equal split BUT if no drift
         fires by a cutoff time (13:00), deploy remaining capital as
         timer-style adds at remaining checkpoints
MODEL E: "Conditional timer" -- timer adds BUT skip any add when
         |SPX drift| < 5pts (position doesn't need help)
MODEL F: "1T reference" (no adds)

Applies to: V4, V5, V10, V11, V12, V13

Usage:
    python3 drift_triggered_v2.py
"""

import os, json, sys
import pandas as pd
import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# CONSTANTS
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

ALL_CPS = ["1030","1100","1130","1200","1230","1300","1330","1400","1430","1500","1530","1545"]

CHECKPOINTS_60M = [("1100",60),("1200",120),("1300",180),("1400",240),("1500",300)]
CHECKPOINTS_30M = [("1030",30),("1100",60),("1130",90),("1200",120),("1230",150),
                   ("1300",180),("1330",210),("1400",240),("1430",270),("1500",300),
                   ("1530",330),("1545",345)]

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

# Best drift thresholds from v1 analysis
BEST_DRIFT = {"V4": 10, "V5": 8, "V10": 5, "V11": 20, "V12": 8, "V13": 5}

# ============================================================================
# LOAD DATA
# ============================================================================
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)
N = len(go)
print(f"  {N} GO days")

with open(os.path.join(_DIR, "spx_intraday_cache.json")) as f:
    spx_intraday = json.load(f)
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
# HELPERS
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

def get_n_spreads(row, risk_per_tranche):
    """Compute number of spreads for a given risk-per-tranche allocation."""
    rps = row.get("risk_deployed_p1", 0)
    nsp = row.get("n_spreads_p1", 0)
    ml_ps = (rps / nsp) if (nsp > 0 and rps > 0) else TRANCHE_RISK
    if ml_ps <= 0: ml_ps = TRANCHE_RISK
    return max(1, int(risk_per_tranche / ml_ps))

# ============================================================================
# MODEL A: TIMER BASELINE
# ============================================================================
def sim_timer(row, target_pct, time_stop, n_tranches, interval_cps, risk_allocated):
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)
    rpt = risk_allocated / max(1, n_tranches)
    n_per = get_n_spreads(row, rpt)

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

    return round(total, 2)

# ============================================================================
# MODEL B: DRIFT EQUAL SPLIT
# ============================================================================
def sim_drift_equal(row, target_pct, time_stop, max_adds, drift_th,
                    risk_allocated, spx_prices):
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)
    total_tranches = 1 + max_adds
    rpt = risk_allocated / total_tranches
    n_per = get_n_spreads(row, rpt)

    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    center = spx_prices.get("1000") if spx_prices else None
    if center is None: return round(total, 2)

    adds_made = 0
    for cp_lbl in ALL_CPS:
        if adds_made >= max_adds: break
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): break

        spx_now = spx_prices.get(cp_lbl)
        if spx_now is None: continue
        if abs(spx_now - center) >= drift_th:
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total += tk_pnl
            adds_made += 1

    return round(total, 2)

# ============================================================================
# MODEL C: DRIFT HEAVY ENTRY (60% entry, 40% split among adds)
# ============================================================================
def sim_drift_heavy(row, target_pct, time_stop, max_adds, drift_th,
                    risk_allocated, spx_prices, entry_pct=0.60):
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)

    # Entry tranche: entry_pct of capital
    entry_risk = risk_allocated * entry_pct
    add_risk = (risk_allocated * (1 - entry_pct)) / max(1, max_adds) if max_adds > 0 else 0

    n_entry = get_n_spreads(row, entry_risk)
    n_add = get_n_spreads(row, add_risk) if add_risk > 0 else 0

    total = exit_pnl * n_entry * SPX_MULT - n_entry * SLIPPAGE_PER_SPREAD * SPX_MULT

    center = spx_prices.get("1000") if spx_prices else None
    if center is None or n_add == 0: return round(total, 2)

    adds_made = 0
    for cp_lbl in ALL_CPS:
        if adds_made >= max_adds: break
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): break

        spx_now = spx_prices.get(cp_lbl)
        if spx_now is None: continue
        if abs(spx_now - center) >= drift_th:
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            tk_pnl = (exit_pnl - cp_pnl) * n_add * SPX_MULT - n_add * SLIPPAGE_PER_SPREAD * SPX_MULT
            total += tk_pnl
            adds_made += 1

    return round(total, 2)

# ============================================================================
# MODEL D: DRIFT WITH FALLBACK TO TIMER AT 13:00
# ============================================================================
def sim_drift_fallback(row, target_pct, time_stop, max_adds, drift_th,
                       risk_allocated, spx_prices, interval_cps):
    """
    Equal split. Try drift adds. If by 13:00 we haven't used all adds,
    switch to timer mode for remaining adds at subsequent checkpoints.
    """
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)
    total_tranches = 1 + max_adds
    rpt = risk_allocated / total_tranches
    n_per = get_n_spreads(row, rpt)

    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    center = spx_prices.get("1000") if spx_prices else None
    if center is None:
        # Fallback: timer-only
        for k in range(2, total_tranches + 1):
            if k - 2 >= len(interval_cps): break
            cp_lbl, _ = interval_cps[k - 2]
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
            if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue
            tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total += tk_pnl
        return round(total, 2)

    adds_made = 0
    fallback_triggered = False

    for cp_lbl in ALL_CPS:
        if adds_made >= max_adds: break
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): break

        # Check if we've passed 13:00 without using all drift adds
        if not fallback_triggered and cp_h >= 13:
            fallback_triggered = True
            # Switch to timer mode for remaining adds
            remaining = max_adds - adds_made
            timer_idx = 0
            for k in range(remaining):
                # Find next timer checkpoint at or after 13:00
                while timer_idx < len(interval_cps):
                    t_lbl, _ = interval_cps[timer_idx]
                    t_h, t_m = TS_HM.get(t_lbl, (16, 0))
                    timer_idx += 1
                    if t_h < 13: continue
                    if t_h > exit_h or (t_h == exit_h and t_m >= exit_m): break
                    t_pnl = row.get(f"pnl_at_{t_lbl}")
                    if t_pnl is None or pd.isna(t_pnl): continue
                    tk_pnl = (exit_pnl - t_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
                    total += tk_pnl
                    adds_made += 1
                    break
            break  # done with all adds

        spx_now = spx_prices.get(cp_lbl)
        if spx_now is None: continue
        if abs(spx_now - center) >= drift_th:
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total += tk_pnl
            adds_made += 1

    return round(total, 2)

# ============================================================================
# MODEL E: CONDITIONAL TIMER (skip adds when drift < 5pt)
# ============================================================================
def sim_cond_timer(row, target_pct, time_stop, n_tranches, interval_cps,
                   risk_allocated, spx_prices, min_drift=5):
    """
    Timer-based adds BUT skip any add at a checkpoint where |SPX drift| < min_drift.
    Deploys FULL capital per tranche (like timer), just skips quiet checkpoints.
    """
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0

    exit_h, exit_m = get_exit_hm(row, outcome, target_pct, time_stop)
    rpt = risk_allocated / max(1, n_tranches)
    n_per = get_n_spreads(row, rpt)

    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    center = spx_prices.get("1000") if spx_prices else None

    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_cps): break
        cp_lbl, _ = interval_cps[k - 2]
        cp_pnl = row.get(f"pnl_at_{cp_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl): continue
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue

        # Check drift condition
        if center is not None:
            spx_now = spx_prices.get(cp_lbl)
            if spx_now is not None and abs(spx_now - center) < min_drift:
                continue  # Skip -- SPX barely moved, butterfly doesn't need help

        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        total += tk_pnl

    return round(total, 2)

# ============================================================================
# MODEL F: 1T (no adds)
# ============================================================================
def sim_1t(row, target_pct, time_stop, risk_allocated):
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND": return 0.0

    n_per = get_n_spreads(row, risk_allocated)
    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
    return round(total, 2)

# ============================================================================
# MAIN ANALYSIS
# ============================================================================
print(f"\n{'='*130}")
print("  DRIFT-TRIGGERED ADDS v2 -- IMPROVED MODELS")
print(f"  Models: A=Timer | B=Drift(equal) | C=Drift(heavy entry 60/40)")
print(f"         D=Drift+fallback@13:00 | E=ConditionalTimer(skip<5pt) | F=1T")
print(f"{'='*130}")

all_results = []

for strat in STRATEGIES:
    target_pct, time_stop, n_t, timer_cps = parse_mech(strat["mech"])
    max_adds = n_t - 1
    drift_th = BEST_DRIFT[strat["ver"]]

    # Filter matching days
    matching = []
    for _, row in go.iterrows():
        if match_regime(row, strat) and check_filter(row, strat["filter"]):
            d_str = row["date"].strftime("%Y-%m-%d")
            spx = spx_intraday.get(d_str)
            matching.append((row, spx))

    n_match = len(matching)
    if n_match == 0:
        print(f"\n  {strat['ver']} -- 0 matching days, SKIPPING")
        continue

    # Run all models
    pnl_a, pnl_b, pnl_c, pnl_d, pnl_e, pnl_f = [], [], [], [], [], []
    # Also test C with different entry percentages
    pnl_c50, pnl_c70, pnl_c80 = [], [], []

    for row, spx in matching:
        # A: Timer
        pnl_a.append(sim_timer(row, target_pct, time_stop, n_t, timer_cps, DAILY_RISK))

        # B: Drift equal split
        pnl_b.append(sim_drift_equal(row, target_pct, time_stop, max_adds, drift_th,
                                      DAILY_RISK, spx))

        # C: Drift heavy entry (60%)
        pnl_c.append(sim_drift_heavy(row, target_pct, time_stop, max_adds, drift_th,
                                      DAILY_RISK, spx, entry_pct=0.60))

        # C variants
        pnl_c50.append(sim_drift_heavy(row, target_pct, time_stop, max_adds, drift_th,
                                        DAILY_RISK, spx, entry_pct=0.50))
        pnl_c70.append(sim_drift_heavy(row, target_pct, time_stop, max_adds, drift_th,
                                        DAILY_RISK, spx, entry_pct=0.70))
        pnl_c80.append(sim_drift_heavy(row, target_pct, time_stop, max_adds, drift_th,
                                        DAILY_RISK, spx, entry_pct=0.80))

        # D: Drift with fallback
        pnl_d.append(sim_drift_fallback(row, target_pct, time_stop, max_adds, drift_th,
                                         DAILY_RISK, spx, timer_cps))

        # E: Conditional timer (skip <5pt)
        pnl_e.append(sim_cond_timer(row, target_pct, time_stop, n_t, timer_cps,
                                     DAILY_RISK, spx, min_drift=5))

        # F: 1T
        pnl_f.append(sim_1t(row, target_pct, time_stop, DAILY_RISK))

    stats = {
        "A: Timer": compute_stats(pnl_a),
        "B: Drift(equal)": compute_stats(pnl_b),
        "C: Drift(60/40)": compute_stats(pnl_c),
        "C: Drift(50/50)": compute_stats(pnl_c50),
        "C: Drift(70/30)": compute_stats(pnl_c70),
        "C: Drift(80/20)": compute_stats(pnl_c80),
        "D: Drift+fallback": compute_stats(pnl_d),
        "E: CondTimer(>5pt)": compute_stats(pnl_e),
        "F: 1T (no adds)": compute_stats(pnl_f),
    }

    print(f"\n{'='*130}")
    print(f"  {strat['ver']} -- {strat['regime']}  |  {strat['mech']}  |  Filter: {strat['filter'] or 'none'}")
    print(f"  {n_match} matching days  |  Best drift threshold: {drift_th}pt  |  Max adds: {max_adds}")
    print(f"{'='*130}")

    hdr = f"  {'Model':>22} {'Total P&L':>14} {'WinRate':>8} {'PF':>7} {'MaxDD':>14} {'Avg/Trade':>12}"
    print(hdr)
    print(f"  {'-'*82}")

    # Find best model by PF
    best_model = max(stats.keys(), key=lambda k: stats[k]["pf"])

    for name, s in stats.items():
        marker = " <-- BEST PF" if name == best_model else ""
        print(f"  {name:>22} ${s['total']:>12,.0f} {s['wr']:>6.1f}% {s['pf']:>6.2f} "
              f"${s['max_dd']:>12,.0f} ${s['avg']:>10,.0f}{marker}")

    all_results.append({
        "ver": strat["ver"],
        "regime": strat["regime"],
        "n_days": n_match,
        "n_t": n_t,
        "drift_th": drift_th,
        "stats": stats,
        "best_model": best_model,
    })

# ============================================================================
# CROSS-STRATEGY SUMMARY
# ============================================================================
print(f"\n\n{'='*130}")
print("  CROSS-STRATEGY SUMMARY")
print(f"{'='*130}")

models_to_compare = ["A: Timer", "B: Drift(equal)", "C: Drift(60/40)",
                     "C: Drift(80/20)", "E: CondTimer(>5pt)", "F: 1T (no adds)"]

print(f"\n  {'Ver':>5}", end="")
for m in models_to_compare:
    print(f"  {m[:16]:>16}", end="")
print()
print(f"  {'-'*(5 + 18*len(models_to_compare))}")

totals = {m: 0 for m in models_to_compare}

for r in all_results:
    print(f"  {r['ver']:>5}", end="")
    for m in models_to_compare:
        s = r["stats"][m]
        totals[m] += s["total"]
        marker = "*" if m == r["best_model"] else " "
        print(f"  {s['pf']:>5.2f} ${s['total']/1000:>7.0f}K{marker}", end="")
    print()

print(f"  {'-'*(5 + 18*len(models_to_compare))}")
print(f"  {'TOTAL':>5}", end="")
for m in models_to_compare:
    print(f"        ${totals[m]/1000:>7.0f}K ", end="")
print()

# ============================================================================
# PF COMPARISON GRID
# ============================================================================
print(f"\n\n{'='*130}")
print("  PROFIT FACTOR COMPARISON")
print(f"{'='*130}")

print(f"\n  {'Ver':>5} {'DriftTh':>8}", end="")
for m in models_to_compare:
    print(f" {m[:15]:>15}", end="")
print(f" {'BEST':>18}")
print(f"  {'-'*(5 + 8 + 16*len(models_to_compare) + 18)}")

for r in all_results:
    print(f"  {r['ver']:>5} {r['drift_th']:>6}pt", end="")
    best_pf = 0
    best_name = ""
    for m in models_to_compare:
        pf = r["stats"][m]["pf"]
        if pf > best_pf:
            best_pf = pf
            best_name = m
        print(f" {pf:>14.2f}", end="")
    print(f"  {best_name}")

# ============================================================================
# VERDICT
# ============================================================================
print(f"\n\n{'='*130}")
print("  VERDICT")
print(f"{'='*130}")

# Count wins by model
win_counts = {}
for r in all_results:
    bm = r["best_model"]
    win_counts[bm] = win_counts.get(bm, 0) + 1

print(f"\n  Wins by model (best PF per strategy):")
for m, cnt in sorted(win_counts.items(), key=lambda x: -x[1]):
    print(f"    {m}: {cnt}/{len(all_results)} strategies")

# Best overall model by total P&L
best_total_model = max(models_to_compare, key=lambda m: totals[m])
print(f"\n  Best by total P&L: {best_total_model} (${totals[best_total_model]:,.0f})")

# Conditional timer vs timer
ct = totals.get("E: CondTimer(>5pt)", 0)
tm = totals.get("A: Timer", 0)
print(f"\n  Conditional Timer vs Timer: {'+'if ct-tm>=0 else ''}${ct-tm:,.0f}")
print(f"  (Conditional timer = timer adds but skip when SPX drift < 5pt)")

# Drift heavy 60/40 vs timer
dh = totals.get("C: Drift(60/40)", 0)
print(f"\n  Drift Heavy(60/40) vs Timer: {'+'if dh-tm>=0 else ''}${dh-tm:,.0f}")
print(f"  (Heavy entry = 60% at entry, 10% per drift add)")

# Drift heavy 80/20 vs timer
d80 = totals.get("C: Drift(80/20)", 0)
print(f"\n  Drift Heavy(80/20) vs Timer: {'+'if d80-tm>=0 else ''}${d80-tm:,.0f}")
print(f"  (Heavy entry = 80% at entry, 5% per drift add)")

print(f"\n{'='*130}")
print("  DONE")
print(f"{'='*130}")
