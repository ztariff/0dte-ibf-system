"""
RE-CENTERED TRANCHE BACKTEST
==============================
Compares 1T vs re-centered multi-tranche vs same-strike multi-tranche
using LIVE Polygon option prices for the PHOENIX V3 strategy.

The existing ensemble_v3.py assumes all tranche adds use the same ATM strike
as the 10:00 entry. In live trading, you'd re-center to current ATM. This
script fetches real option prices at the re-centered strikes to determine
which approach actually performs better.

Usage:
    python backtest_recentered.py [POLYGON_API_KEY]
"""

import sys, os, json, time
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Import backtest_research.py infrastructure ---
# It reads sys.argv at module level, so set it before import
_API_KEY = sys.argv[1] if len(sys.argv) > 1 else "cBE5Kbq9yllt0Yj29mDQjBcIKfAYQlHF"
sys.argv = ["backtest_recentered.py", _API_KEY, "600"]
import backtest_research as br

# Override cache dir to point to main repo's cache (worktree doesn't have its own)
# _DIR = .../0dte-ibf-system/.claude/worktrees/hungry-feynman → go up 3 levels
_MAIN_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_DIR)))
_MAIN_CACHE = os.path.join(_MAIN_REPO, ".polygon_cache")
if os.path.isdir(_MAIN_CACHE):
    br.CACHE_DIR = _MAIN_CACHE
    print(f"  Cache: {_MAIN_CACHE} ({len(os.listdir(_MAIN_CACHE))} files)")
else:
    print(f"  WARNING: Main cache not found at {_MAIN_CACHE}, using default")

# --- Constants ---
DAILY_RISK = 100_000
SPX_MULT = 100
SLIPPAGE = 1.00  # dollars per spread
ET = br.ET

TS_HM = {
    "1000": (10, 0),
    "1030": (10, 30), "1100": (11, 0), "1130": (11, 30),
    "1200": (12, 0),  "1230": (12, 30), "1300": (13, 0),
    "1330": (13, 30), "1400": (14, 0),  "1430": (14, 30),
    "1500": (15, 0),  "1530": (15, 30), "1545": (15, 45),
    "close": (16, 0),
}

CHECKPOINT_LABELS = ["1030","1100","1130","1200","1230","1300","1330","1400","1430","1500","1530","1545"]

# --- PHOENIX V3 Signal Groups ---
# Built dynamically via Jaccard clustering (replicates ensemble_v3.py exactly)

def build_jaccard_groups(go_df, n_top=5, threshold=0.70):
    """Replicate ensemble_v3.py Jaccard clustering to identify PHOENIX signal groups.

    Returns top N groups, each with merged fire_days (frozenset of GO row indices),
    leader mechanics (target_pct, time_stop, tranches), and rank.
    """
    # Load signal catalog
    catalog_path = os.path.join(_DIR, "signal_catalog.json")
    with open(catalog_path) as f:
        catalog = json.load(f)
    print(f"  Signal catalog: {len(catalog)} parameter sets")

    # Build atomic filter masks (same as ensemble_v3.py lines 65-87)
    atomic_filters = {
        "VIX\u226415": go_df["vix"]<=15, "VIX\u226416": go_df["vix"]<=16,
        "VIX\u226417": go_df["vix"]<=17, "VIX\u226418": go_df["vix"]<=18,
        "VIX\u226420": go_df["vix"]<=20,
        "VP\u22641.0": go_df["vp_ratio"]<=1.0, "VP\u22641.2": go_df["vp_ratio"]<=1.2,
        "VP\u22641.3": go_df["vp_ratio"]<=1.3, "VP\u22641.5": go_df["vp_ratio"]<=1.5,
        "VP\u22641.7": go_df["vp_ratio"]<=1.7, "VP\u22642.0": go_df["vp_ratio"]<=2.0,
        "STABLE": go_df["rv_slope"]=="STABLE", "!RISING": go_df["rv_slope"]!="RISING",
        "FLAT_vwap": go_df["vwap_slope"]=="FLAT",
        "Rng\u22640.3": go_df["range_pct"]<=0.3, "Rng\u22640.4": go_df["range_pct"]<=0.4,
        "Rng\u22640.6": go_df["range_pct"]<=0.6,
    }
    if "prior_day_return" in go_df.columns:
        atomic_filters.update({
            "PrDayDn": go_df["prior_day_direction"]=="DOWN",
            "PrDayUp": go_df["prior_day_direction"]=="UP",
            "PrRet<-1": go_df["prior_day_return"]<-1,
            "PrRet>0": go_df["prior_day_return"]>0,
            "PrRng\u22640.8": go_df["prior_day_range"]<=0.8,
            "PrRng\u22641.0": go_df["prior_day_range"]<=1.0,
            "InWkRng": go_df["in_prior_week_range"]==1,
            "OutWkRng": go_df["in_prior_week_range"]==0,
            "InMoRng": go_df["in_prior_month_range"]==1,
            "WkTop50": go_df["pct_in_weekly_range"]>=50,
            "5dRet>0": go_df["prior_5d_return"]>0,
            "5dRet>1": go_df["prior_5d_return"]>1,
            "5dRet<0": go_df["prior_5d_return"]<0,
            "PrRV<12": go_df["prior_day_rv"]<12,
            "PrRV<15": go_df["prior_day_rv"]<15,
            "RVchg<0": go_df["rv_1d_change"]<0,
            "RVchg>0": go_df["rv_1d_change"]>0,
        })

    def parse_filter_mask(filter_name):
        parts = [p.strip() for p in filter_name.split(" + ")]
        mask = pd.Series(True, index=go_df.index)
        for p in parts:
            if p in atomic_filters:
                mask = mask & atomic_filters[p]
            else:
                return None
        return mask

    # Group catalog by filter name, build fire_days (same as ensemble_v3.py lines 175-201)
    filter_groups = defaultdict(list)
    for ps in catalog:
        filter_groups[ps["filters"]].append(ps)

    clusters_raw = []
    for filt_name, group in filter_groups.items():
        mask = parse_filter_mask(filt_name)
        if mask is None:
            continue
        fire_days = frozenset(go_df.index[mask].tolist())
        if len(fire_days) < 5:
            continue

        # Pick best mechanics (same logic as ensemble_v3)
        robust = [ps for ps in group if ps.get("robust") == True or ps.get("robust") == "True"]
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
        if best["profit_factor"] < 1.2:
            continue

        clusters_raw.append({
            "name": filt_name, "fire_days": fire_days, "n_fire": len(fire_days),
            "best_set": best, "tier": best["tier"], "pf": best["profit_factor"],
            "calmar": best["calmar"], "target_pct": best["target_pct"],
            "time_stop": best["time_stop"], "tranches": best["tranches"],
        })

    tier_order = {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    clusters_raw.sort(key=lambda x: (tier_order.get(x["tier"], 5), -x["calmar"]))

    # Jaccard clustering (same as ensemble_v3.py lines 206-222)
    def jaccard(s1, s2):
        if len(s1) == 0 and len(s2) == 0:
            return 1.0
        return len(s1 & s2) / len(s1 | s2)

    groups = []
    for cl in clusters_raw:
        merged = False
        for grp in groups:
            if jaccard(cl["fire_days"], grp["fire_days"]) >= threshold:
                grp["members"].append(cl)
                grp["fire_days"] = grp["fire_days"] | cl["fire_days"]  # UNION
                merged = True
                break
        if not merged:
            groups.append({"leader": cl, "members": [cl], "fire_days": set(cl["fire_days"])})

    # Rank by leader quality (same as ensemble_v3.py lines 228-232)
    for grp in groups:
        l = grp["leader"]
        grp["rank_score"] = tier_order.get(l["tier"], 5) * -100 + l["calmar"]
    groups.sort(key=lambda x: -x["rank_score"])

    top = groups[:n_top]
    print(f"  Jaccard clustering: {len(clusters_raw)} raw -> {len(groups)} groups -> top {n_top}")
    for i, grp in enumerate(top):
        l = grp["leader"]
        print(f"    #{i+1} {l['name']:<40} tier={l['tier']} pf={l['pf']:.2f} "
              f"cal={l['calmar']:.2f} fires={len(grp['fire_days'])}d "
              f"mech={l['target_pct']}%/{l['time_stop']}")

    return top


# --- Configuration definitions ---
CONFIGS = {
    "1T":       {"n_tranches": 1, "add_times": [],                             "recenter": False},
    "RC_3T60m": {"n_tranches": 3, "add_times": ["1100", "1200"],               "recenter": True},
    "RC_5T60m": {"n_tranches": 5, "add_times": ["1100","1200","1300","1400"],   "recenter": True},
    "SS_5T60m": {"n_tranches": 5, "add_times": ["1100","1200","1300","1400"],   "recenter": False},
}

TARGET_PCTS = [50, 70]


# ============================================================
# PHOENIX DAY IDENTIFICATION
# ============================================================
def identify_phoenix_days(go_df):
    """Identify PHOENIX V3 trading days using Jaccard-clustered signal groups.

    Replicates the exact logic from ensemble_v3.py:
    - Build top 5 Jaccard clusters from signal_catalog.json
    - For each GO day, count how many of the top 5 groups fire (using merged fire_days)
    - Apply tiered sizing based on signal count
    - Use adaptive mechanics from highest-ranked firing group
    """
    # Build Jaccard groups (replicates ensemble_v3.py)
    top5_groups = build_jaccard_groups(go_df, n_top=5, threshold=0.70)

    phoenix_days = []
    for idx in range(len(go_df)):
        row = go_df.iloc[idx]

        # Count how many top-5 groups fire on this day (using merged fire_days)
        n_fire = 0
        best_group_leader = None
        for grp in top5_groups:
            if idx in grp["fire_days"]:
                n_fire += 1
                if best_group_leader is None:
                    best_group_leader = grp["leader"]

        if n_fire == 0:
            continue

        # Tiered sizing
        if n_fire == 1:   risk = 25_000
        elif n_fire == 2: risk = 50_000
        elif n_fire == 3: risk = 75_000
        else:             risk = 100_000

        ds = row["date"]
        if isinstance(ds, str):
            ds = pd.Timestamp(ds)

        phoenix_days.append({
            "idx": idx,
            "date": ds,
            "day_str": ds.strftime("%Y-%m-%d"),
            "signal_count": n_fire,
            "risk_budget": risk,
            "target_pct": best_group_leader["target_pct"],
            "time_stop": best_group_leader["time_stop"],
            "wing_width": int(row.get("wing_width", 40)),
            "row": row,
        })

    return phoenix_days


# ============================================================
# EXIT LOGIC HELPERS (reused from ensemble_v3.py)
# ============================================================
def time_before(t_str, h, m):
    """Check if timestamp string is before h:m."""
    if not t_str or pd.isna(t_str) or t_str == "":
        return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except:
        return False

def find_exit_pnl(row, target_pct, time_stop):
    """Find exit P&L per spread using CSV sweep data. Priority: WS > TGT > TS > EXP."""
    ts_hour, ts_min = TS_HM.get(time_stop, (16, 0))

    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")
    tgt_time = row.get(f"hit_{target_pct}_time", "")
    tgt_pnl = row.get(f"hit_{target_pct}_pnl")

    events = []
    if ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_hour, ts_min):
        try:
            events.append(("WS", pd.Timestamp(ws_time), ws_pnl))
        except:
            pass
    if pd.notna(tgt_pnl) and tgt_time and time_before(tgt_time, ts_hour, ts_min):
        try:
            events.append(("TGT", pd.Timestamp(tgt_time), tgt_pnl))
        except:
            pass

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


def get_exit_hm(row, target_pct, time_stop, outcome):
    """Get the hour/minute of exit for tranche validity checks."""
    if outcome == "WS":
        ws_time = row.get("ws_time", "")
        try:
            t = pd.Timestamp(ws_time)
            return t.hour, t.minute
        except:
            return 16, 0
    elif outcome == "TGT":
        tgt_time = row.get(f"hit_{target_pct}_time", "")
        try:
            t = pd.Timestamp(tgt_time)
            return t.hour, t.minute
        except:
            return 16, 0
    return 16, 0


# ============================================================
# SIMULATE: 1T (single tranche, from CSV data)
# ============================================================
def simulate_1t(day_info, target_pct):
    """Single tranche: full budget at 10:00, use CSV sweep data."""
    row = day_info["row"]
    risk = day_info["risk_budget"]
    ww = day_info["wing_width"]

    exit_pnl, outcome = find_exit_pnl(row, target_pct, "close")
    if exit_pnl is None:
        return 0.0, "ND", {}

    # Size: full budget on one tranche
    risk_p1 = row.get("risk_deployed_p1", 0)
    n_sp = row.get("n_spreads_p1", 0)
    ml_ps = (risk_p1 / n_sp) if (n_sp > 0 and risk_p1 > 0) else 5000
    if ml_ps <= 0:
        ml_ps = 5000

    n_spreads = max(1, int(risk / ml_ps))
    dollar_pnl = exit_pnl * n_spreads * SPX_MULT - n_spreads * SLIPPAGE * SPX_MULT

    return round(dollar_pnl), outcome, {"n_spreads": n_spreads}


# ============================================================
# SIMULATE: Same-Strike Multi-Tranche (from CSV data, sanity check)
# ============================================================
def simulate_same_strike(day_info, target_pct, config):
    """Same-strike multi-tranche: uses CSV data with exit_pnl - pnl_at_checkpoint."""
    row = day_info["row"]
    risk = day_info["risk_budget"]
    n_t = config["n_tranches"]
    add_times = config["add_times"]

    exit_pnl, outcome = find_exit_pnl(row, target_pct, "close")
    if exit_pnl is None:
        return 0.0, "ND", {}

    exit_h, exit_m = get_exit_hm(row, target_pct, "close", outcome)

    # Size per tranche
    risk_p1 = row.get("risk_deployed_p1", 0)
    n_sp = row.get("n_spreads_p1", 0)
    ml_ps = (risk_p1 / n_sp) if (n_sp > 0 and risk_p1 > 0) else 5000
    if ml_ps <= 0:
        ml_ps = 5000

    risk_per_t = risk / n_t
    n_per = max(1, int(risk_per_t / ml_ps))

    # P1
    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE * SPX_MULT
    n_active = 1

    # Adds (same-strike formula from ensemble_v3)
    for add_lbl in add_times:
        add_h, add_m = TS_HM[add_lbl]
        if add_h > exit_h or (add_h == exit_h and add_m >= exit_m):
            continue
        cp_pnl = row.get(f"pnl_at_{add_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl):
            continue
        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE * SPX_MULT
        total += tk_pnl
        n_active += 1

    return round(total), outcome, {"n_per_tranche": n_per, "n_tranches_active": n_active}


# ============================================================
# SIMULATE: Re-Centered Multi-Tranche (LIVE option prices)
# ============================================================
def simulate_recentered(day_info, target_pct, config):
    """Re-centered multi-tranche: fetch real option prices at new ATM for each add."""
    row = day_info["row"]
    day_str = day_info["day_str"]
    risk = day_info["risk_budget"]
    n_t = config["n_tranches"]
    add_times = config["add_times"]
    ww = day_info["wing_width"]
    exp_date_obj = datetime.strptime(day_str, "%Y-%m-%d").date()

    # Clear per-day in-memory option cache
    br.clear_option_cache()

    # Get SPX minute bars
    spx_df = br.get_bars("I:SPX", day_str)
    if spx_df.empty:
        return 0.0, "NO_DATA", {}

    # Entry time reference
    first_t = spx_df.iloc[0]["t"]
    entry_t = first_t.replace(hour=10, minute=0, second=0, microsecond=0)

    # Get SPX at entry
    entry_bars = spx_df[spx_df["t"] >= entry_t]
    if entry_bars.empty:
        return 0.0, "NO_ENTRY", {}
    spx_entry = entry_bars.iloc[0]["o"]

    # --- Build P1 ---
    p1_atm = br.snap(spx_entry)
    p1_wp = p1_atm - ww
    p1_wc = p1_atm + ww

    p1_credit, _, p1_miss = br.fetch_ibf_prices_at(
        exp_date_obj, p1_atm, p1_wp, p1_wc, day_str, entry_t
    )
    if p1_credit is None or p1_credit <= 0:
        return 0.0, "NO_P1", {}

    p1_ml = (p1_atm - p1_wp) - p1_credit
    if p1_ml <= 0:
        p1_ml = 0.01

    risk_per_t = risk / n_t
    n_sp_p1 = max(1, int(risk_per_t / (p1_ml * SPX_MULT)))

    tranches = [{
        "id": 1, "atm": p1_atm, "wp": p1_wp, "wc": p1_wc,
        "credit": p1_credit, "max_loss": p1_ml,
        "n_spreads": n_sp_p1, "entry_lbl": "1000",
        "drift": 0,
    }]

    # --- Build add tranches ---
    for i, add_lbl in enumerate(add_times):
        add_h, add_m = TS_HM[add_lbl]
        add_t = first_t.replace(hour=add_h, minute=add_m, second=0, microsecond=0)

        # SPX at add time
        add_bars = spx_df[spx_df["t"] <= add_t]
        if add_bars.empty:
            continue
        spx_now = add_bars.iloc[-1]["c"]
        new_atm = br.snap(spx_now)
        new_wp = new_atm - ww
        new_wc = new_atm + ww

        # Fetch LIVE 4-leg prices at re-centered strikes
        add_credit, _, add_miss = br.fetch_ibf_prices_at(
            exp_date_obj, new_atm, new_wp, new_wc, day_str, add_t
        )
        if add_credit is None or add_credit <= 0:
            continue  # skip this tranche

        add_ml = (new_atm - new_wp) - add_credit
        if add_ml <= 0:
            add_ml = 0.01

        n_sp_add = max(1, int(risk_per_t / (add_ml * SPX_MULT)))

        tranches.append({
            "id": i + 2, "atm": new_atm, "wp": new_wp, "wc": new_wc,
            "credit": add_credit, "max_loss": add_ml,
            "n_spreads": n_sp_add, "entry_lbl": add_lbl,
            "drift": new_atm - p1_atm,
        })

    # --- Determine composite exit ---
    # Wing stop: tightest wings across all tranches
    tightest_wp = max(t["wp"] for t in tranches)  # highest put wing = tightest on downside
    tightest_wc = min(t["wc"] for t in tranches)  # lowest call wing = tightest on upside

    # Walk SPX bars to find wing stop (after last add enters)
    last_add_lbl = tranches[-1]["entry_lbl"]
    last_add_h, last_add_m = TS_HM[last_add_lbl]
    walk_start_t = first_t.replace(hour=last_add_h, minute=last_add_m, second=0, microsecond=0)

    ws_time = None
    ws_bar_t = None
    for _, bar in spx_df[spx_df["t"] >= walk_start_t].iterrows():
        if bar["c"] <= tightest_wp or bar["c"] >= tightest_wc:
            ws_bar_t = bar["t"]
            break

    # Time stop
    time_stop_t = first_t.replace(hour=16, minute=0, second=0, microsecond=0)

    # --- Check target at checkpoints (via LIVE prices for each tranche) ---
    total_entry_credit_dollar = sum(t["credit"] * t["n_spreads"] * SPX_MULT for t in tranches)
    total_slippage = sum(t["n_spreads"] for t in tranches) * SLIPPAGE * SPX_MULT
    target_dollar = total_entry_credit_dollar * (target_pct / 100.0) - total_slippage

    target_hit_t = None
    target_hit_pnl = None

    for cp_lbl in CHECKPOINT_LABELS:
        cp_h, cp_m = TS_HM[cp_lbl]
        cp_t = first_t.replace(hour=cp_h, minute=cp_m, second=0, microsecond=0)

        # Only check after all tranches are entered
        if cp_h < last_add_h or (cp_h == last_add_h and cp_m < last_add_m):
            continue

        # If wing stop already happened before this checkpoint, skip
        if ws_bar_t and cp_t > ws_bar_t:
            break

        # Compute composite P&L at this checkpoint
        composite_pnl = 0
        all_priced = True
        for tranche in tranches:
            val_now, _, _ = br.fetch_ibf_prices_at(
                exp_date_obj, tranche["atm"], tranche["wp"], tranche["wc"],
                day_str, cp_t
            )
            if val_now is not None:
                pnl_ps = tranche["credit"] - val_now
                pnl_ps = max(-tranche["max_loss"], min(tranche["credit"], pnl_ps))
                composite_pnl += pnl_ps * tranche["n_spreads"] * SPX_MULT
            else:
                all_priced = False

        composite_pnl -= total_slippage

        if all_priced and composite_pnl >= target_dollar:
            target_hit_t = cp_t
            target_hit_pnl = composite_pnl
            break

    # --- Determine exit: WS > TGT > TS ---
    exit_time = time_stop_t
    exit_trigger = "TS"

    if ws_bar_t and ws_bar_t < time_stop_t:
        exit_time = ws_bar_t
        exit_trigger = "WS"

    if target_hit_t:
        if exit_trigger == "WS":
            if target_hit_t < exit_time:
                exit_time = target_hit_t
                exit_trigger = "TGT"
        else:
            exit_time = target_hit_t
            exit_trigger = "TGT"

    # --- Compute final P&L at exit time ---
    # For TGT exits, we already have the composite P&L
    if exit_trigger == "TGT" and target_hit_pnl is not None:
        total_dollar_pnl = target_hit_pnl
    else:
        # Fetch live prices at exit time for each tranche
        total_dollar_pnl = 0
        for tranche in tranches:
            exit_val, _, _ = br.fetch_ibf_prices_at(
                exp_date_obj, tranche["atm"], tranche["wp"], tranche["wc"],
                day_str, exit_time
            )
            if exit_val is not None:
                pnl_ps = tranche["credit"] - exit_val
                pnl_ps = max(-tranche["max_loss"], min(tranche["credit"], pnl_ps))
            else:
                pnl_ps = -tranche["max_loss"]  # worst case
            total_dollar_pnl += pnl_ps * tranche["n_spreads"] * SPX_MULT
        total_dollar_pnl -= total_slippage

    details = {
        "n_tranches_active": len(tranches),
        "exit_trigger": exit_trigger,
        "drifts": [t["drift"] for t in tranches],
        "credits": [round(t["credit"], 2) for t in tranches],
        "per_tranche_spreads": [t["n_spreads"] for t in tranches],
    }
    return round(total_dollar_pnl), exit_trigger, details


# ============================================================
# STATISTICS
# ============================================================
def compute_stats(pnl_list):
    """Compute summary stats from daily P&L list."""
    arr = np.array(pnl_list, dtype=float)
    traded = arr[arr != 0]
    if len(traded) == 0:
        return {"n": 0, "total": 0, "avg": 0, "wr": 0, "pf": 0,
                "max_dd": 0, "calmar": 0, "sharpe": 0, "best": 0, "worst": 0}

    total = arr.sum()
    wins = traded[traded > 0]
    losses = traded[traded < 0]
    wr = len(wins) / len(traded) * 100
    w_sum = wins.sum() if len(wins) > 0 else 0
    l_sum = abs(losses.sum()) if len(losses) > 0 else 0
    pf = w_sum / l_sum if l_sum > 0 else 99

    cum = np.cumsum(arr)
    dd = (cum - np.maximum.accumulate(cum)).min()
    calmar = total / abs(dd) if dd < 0 else 0
    sharpe = (arr.mean() / arr.std()) * np.sqrt(252) if arr.std() > 0 else 0

    return {
        "n": len(traded), "total": total, "avg": total / len(traded),
        "wr": wr, "pf": pf, "max_dd": dd, "calmar": calmar, "sharpe": sharpe,
        "best": arr.max(), "worst": arr.min(),
    }


# ============================================================
# MAIN
# ============================================================
def run():
    print("=" * 90)
    print("  RE-CENTERED TRANCHE BACKTEST -- PHOENIX V3")
    print("=" * 90)

    # Load data
    csv_path = os.path.join(_DIR, "research_all_trades.csv")
    df = pd.read_csv(csv_path)
    go = df[df["recommendation"] == "GO"].copy()
    go["date"] = pd.to_datetime(go["date"])
    go = go.sort_values("date").reset_index(drop=True)
    print(f"  {len(go)} GO trades loaded")

    # Identify PHOENIX days
    phoenix_days = identify_phoenix_days(go)
    print(f"  {len(phoenix_days)} PHOENIX V3 days (signal count >= 1)")
    if not phoenix_days:
        print("  ERROR: No PHOENIX days found")
        return

    # Signal count distribution
    sc_dist = defaultdict(int)
    for d in phoenix_days:
        sc_dist[d["signal_count"]] += 1
    print(f"  Signal counts: {dict(sorted(sc_dist.items()))}")
    print()

    # Storage for all results
    all_results = {}

    # Run each configuration
    for config_label, config in CONFIGS.items():
        for target_pct in TARGET_PCTS:
            key = f"{config_label}/{target_pct}%"
            print(f"{'='*90}")
            print(f"  Running: {key}")
            print(f"{'='*90}")

            daily_pnls = []
            outcomes = defaultdict(int)
            drift_data = defaultdict(list)  # add_lbl -> [drift values]

            for i, day_info in enumerate(phoenix_days):
                if config_label == "1T":
                    pnl, outcome, details = simulate_1t(day_info, target_pct)
                elif config["recenter"]:
                    pnl, outcome, details = simulate_recentered(day_info, target_pct, config)
                    # Track drift stats
                    if "drifts" in details:
                        for j, d in enumerate(details["drifts"]):
                            if j == 0:
                                continue  # P1 has 0 drift
                            add_lbl = config["add_times"][j - 1] if j - 1 < len(config["add_times"]) else "?"
                            drift_data[add_lbl].append(d)
                else:
                    pnl, outcome, details = simulate_same_strike(day_info, target_pct, config)

                daily_pnls.append(pnl)
                outcomes[outcome] += 1

                # Progress (less verbose for non-recentered)
                if config["recenter"] or (i % 20 == 0) or (i == len(phoenix_days) - 1):
                    n_t_str = f" ({details.get('n_tranches_active', 1)}T)" if config.get("recenter") else ""
                    print(f"    [{i+1:>3}/{len(phoenix_days)}] {day_info['day_str']} "
                          f"${pnl:>+9,.0f} [{outcome}]{n_t_str}", flush=True)

            stats = compute_stats(daily_pnls)
            all_results[key] = {
                "daily_pnls": daily_pnls,
                "stats": stats,
                "outcomes": dict(outcomes),
                "drift_data": dict(drift_data),
            }
            print(f"  --> Total: ${stats['total']:+,.0f} | PF: {stats['pf']:.2f} | "
                  f"WR: {stats['wr']:.1f}% | DD: ${stats['max_dd']:+,.0f}")
            print()

    # ============================================================
    # COMPARISON TABLE
    # ============================================================
    print()
    print("=" * 110)
    print("  COMPARISON TABLE")
    print("=" * 110)
    print()
    print(f"  {'Config':<22} {'N':>4} {'Total P&L':>14} {'Avg':>10} {'WR%':>6} "
          f"{'PF':>6} {'Max DD':>12} {'Calmar':>7} {'Sharpe':>7}")
    print(f"  {'-'*95}")

    for key in sorted(all_results.keys()):
        s = all_results[key]["stats"]
        print(f"  {key:<22} {s['n']:>4} ${s['total']:>+13,.0f} ${s['avg']:>+9,.0f} "
              f"{s['wr']:>5.1f}% {s['pf']:>5.2f} ${s['max_dd']:>+11,.0f} "
              f"{s['calmar']:>6.2f} {s['sharpe']:>6.2f}")

    # ============================================================
    # OUTCOME BREAKDOWN
    # ============================================================
    print()
    print("=" * 110)
    print("  OUTCOME BREAKDOWN")
    print("=" * 110)
    for key in sorted(all_results.keys()):
        oc = all_results[key]["outcomes"]
        parts = [f"{k}={v}" for k, v in sorted(oc.items())]
        print(f"  {key:<22} {', '.join(parts)}")

    # ============================================================
    # DRIFT ANALYSIS (re-centered configs only)
    # ============================================================
    has_drift = any(all_results[k]["drift_data"] for k in all_results)
    if has_drift:
        print()
        print("=" * 110)
        print("  DRIFT ANALYSIS (re-centered adds)")
        print("=" * 110)
        print("  How far SPX drifted from original ATM at each add time")
        print()
        for key in sorted(all_results.keys()):
            dd = all_results[key]["drift_data"]
            if not dd:
                continue
            print(f"  {key}:")
            print(f"    {'Add Time':<10} {'N':>4} {'Avg Drift':>10} {'Med Drift':>10} "
                  f"{'Avg |Drift|':>12} {'Max |Drift|':>12} {'Same ATM%':>10}")
            print(f"    {'-'*70}")
            for add_lbl in sorted(dd.keys()):
                drifts = np.array(dd[add_lbl])
                n = len(drifts)
                avg = drifts.mean()
                med = np.median(drifts)
                avg_abs = np.abs(drifts).mean()
                max_abs = np.abs(drifts).max()
                same_pct = (np.abs(drifts) < 3).mean() * 100  # within rounding
                print(f"    {add_lbl:<10} {n:>4} {avg:>+9.1f}pt {med:>+9.1f}pt "
                      f"{avg_abs:>10.1f}pt {max_abs:>10.1f}pt {same_pct:>9.0f}%")
            print()

    # ============================================================
    # SAVE CSV
    # ============================================================
    rows = []
    for i, day_info in enumerate(phoenix_days):
        row = {
            "date": day_info["day_str"],
            "signals": day_info["signal_count"],
            "risk": day_info["risk_budget"],
        }
        for key in sorted(all_results.keys()):
            safe = key.replace("/", "_").replace("%", "pct")
            row[f"pnl_{safe}"] = all_results[key]["daily_pnls"][i]
        rows.append(row)
    out_path = os.path.join(_DIR, "recentered_backtest_results.csv")
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f"\n  Results saved to: {out_path}")

    print()
    print("=" * 110)
    print("  DONE")
    print("=" * 110)


if __name__ == "__main__":
    run()
