#!/usr/bin/env python3
"""
test_stop_variants.py — Test different stop loss configurations per strategy
to find the optimal stop for each surviving strategy.

Variants tested:
  A: wing_stop only (price-based — SPX crosses wing strike)
  B: loss_stop(0.70) only (P&L-based — 70% of max risk)
  C: loss_stop(0.50) only (P&L-based — 50% of max risk, tighter)
  D: wing_stop + loss_stop(0.70) (both — whichever fires first)
  E: wing_stop + loss_stop(0.50) (both — whichever fires first)
  F: no stop at all (target + time/close only)

Runs all 6 surviving legacy strategies + all 10 new strategies.
"""

import os, sys, json, math, csv
import numpy as np
from datetime import datetime
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

from research.data import DataUniverse
from research.structures import iron_butterfly, iron_condor, price_entry
from research.exits import (
    profit_target, time_stop, wing_stop, loss_stop, simulate_trade
)
from research.sweep import ibf_factory, ic_factory


# ─────────────────────────────────────────────────────────────────────────────
# STOP VARIANTS
# ─────────────────────────────────────────────────────────────────────────────

STOP_VARIANTS = {
    "A_wing_only":       lambda: [wing_stop()],
    "B_loss70":          lambda: [loss_stop(0.70)],
    "C_loss50":          lambda: [loss_stop(0.50)],
    "D_wing+loss70":     lambda: [wing_stop(), loss_stop(0.70)],
    "E_wing+loss50":     lambda: [wing_stop(), loss_stop(0.50)],
    "F_no_stop":         lambda: [],
}


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY STRATEGY REGIME LOGIC (from rebacktest_legacy.py)
# ─────────────────────────────────────────────────────────────────────────────

def calc_rv_from_closes(closes):
    if len(closes) < 5: return None
    lr = np.diff(np.log(np.array(closes, dtype=float)))
    return float(np.std(lr) * np.sqrt(252 * 390) * 100) if len(lr) >= 2 else None

def compute_rv_and_slope(universe, date, entry_time):
    bars = universe.spx_bars_range(date, "09:30", entry_time)
    if not bars or len(bars) < 20: return None, "UNKNOWN"
    all_closes = [b["c"] for _, b in bars]
    rv = calc_rv_from_closes(all_closes)
    mid = len(bars) // 2
    rv_w1 = calc_rv_from_closes([b["c"] for _, b in bars[:mid]])
    rv_w2 = calc_rv_from_closes([b["c"] for _, b in bars[mid:]])
    if rv_w1 and rv_w1 > 0 and rv_w2 is not None:
        slope = (rv_w2 - rv_w1) / rv_w1 * 100
        label = "RISING" if slope > 20 else "FALLING" if slope < -20 else "STABLE"
    else:
        label = "UNKNOWN"
    return rv, label

def compute_vp_ratio(vix, rv):
    if rv and rv > 0: return round(vix / rv, 2)
    return round(vix / (vix * 0.75), 2) if vix > 0 else 1.5

def compute_score_vol(vp, rv_slope):
    if vp is None: return 15
    if vp >= 1.5: score = 30
    elif vp >= 1.2: score = 22
    elif vp >= 1.0: score = 15
    elif vp >= 0.85: score = 10
    else: score = 3
    if rv_slope == "RISING": score = max(0, score - 15)
    elif rv_slope == "FALLING": score = min(30, score + 5)
    return score

def phoenix_fire_count(vix, vp, prior_dir, ret5d, rv_1d_change, in_range, rv_slope):
    if vp is None: vp = 99
    if ret5d is None: ret5d = -99
    if rv_1d_change is None: rv_1d_change = -99
    g1 = vix <= 20 and vp <= 1.0 and ret5d > 0
    g2 = vp <= 1.3 and prior_dir == "DOWN" and ret5d > 0
    g3 = vp <= 1.2 and ret5d > 0 and rv_1d_change > 0
    g4 = vp <= 1.5 and not in_range and ret5d > 0
    g5 = vp <= 1.3 and rv_slope != "RISING" and ret5d > 0
    return sum([g1, g2, g3, g4, g5])

PHOENIX_TIER = {0: 0, 1: 25000, 2: 50000, 3: 75000, 4: 100000, 5: 100000}
REGIME_MAX = {'v6': 75000, 'v7': 25000, 'v9': 100000, 'v12': 75000}

def regime_budget(ver, vp):
    if vp is None: vp = 1.5
    mx = REGIME_MAX.get(ver, 100000)
    if vp <= 1.0: s = 1.0
    elif vp <= 1.2: s = 0.75
    elif vp <= 1.5: s = 0.50
    else: s = 0.25
    return int(mx * s)

def check_filter(filt, vp, rv_slope, ret5d, score_vol):
    if filt is None: return True
    if filt == "VP<=1.7": return (vp or 99) <= 1.7
    if filt == "!RISING": return rv_slope != "RISING"
    if filt == "5dRet>1": return (ret5d or -99) > 1.0
    return True


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY STRATEGIES (surviving)
# ─────────────────────────────────────────────────────────────────────────────

LEGACY = [
    {"ver": "v3",  "type": "phoenix",       "entry": "10:00", "target": 0.50, "time_stop": None,    "name": "PHOENIX"},
    {"ver": "n15", "type": "phoenix_clear",  "entry": "10:00", "target": 0.50, "time_stop": None,    "name": "PHOENIX CLEAR"},
    {"ver": "v6",  "vix": [0,15], "pd": "DN", "rng": "IN", "gap": "GFL", "filter": "VP<=1.7",
     "entry": "10:00", "target": 0.50, "time_stop": "15:30", "name": "QUIET REBOUND"},
    {"ver": "v7",  "vix": [0,15], "pd": "FL", "rng": "IN", "gap": "GUP", "filter": None,
     "entry": "10:00", "target": 0.40, "time_stop": None,    "name": "FLAT-GAP FADE"},
    {"ver": "v9",  "vix": [15,20], "pd": "UP", "rng": "OT", "gap": "GFL", "filter": "!RISING",
     "entry": "10:00", "target": 0.70, "time_stop": "15:45", "name": "BREAKOUT STALL"},
    {"ver": "v12", "vix": [0,15], "pd": "UP", "rng": "OT", "gap": "GUP", "filter": "5dRet>1",
     "entry": "10:00", "target": 0.40, "time_stop": None,    "name": "BULL SQUEEZE"},
]


# ─────────────────────────────────────────────────────────────────────────────
# NEW STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────

NEW_STRATS = [
    ("Phoenix 75 Power Close",    "15:15", ibf_factory(75),    0.50, "15:30", 150000),
    ("Phoenix 75 Last Hour",      "15:00", ibf_factory(75),    0.50, "15:30", 100000),
    ("Firebird 60 Last Hour",     "15:00", ibf_factory(60),    0.50, "15:30", 100000),
    ("Phoenix 75 Afternoon",      "14:30", ibf_factory(75),    0.50, "15:30",  75000),
    ("Ironclad 35 Condor",        "14:30", ic_factory(35, 35), 0.40, "15:30",  75000),
    ("Firebird 60 Final Bell",    "15:30", ibf_factory(60),    0.50, "15:30",  75000),
    ("Phoenix 75 Early Afternoon", "13:45", ibf_factory(75),   0.50, "15:30",  50000),
    ("Phoenix 75 Midday",         "14:00", ibf_factory(75),    0.50, "15:30",  35000),
    ("Firebird 60 Midday",        "14:00", ibf_factory(60),    0.50, "15:30",  35000),
    ("Morning Decel Scalp",       "10:30", ibf_factory(75),    0.30, "11:30",  20000),
]


def run_legacy_variants(universe, csv_range, gap_cache, vix9d_data):
    """Run all legacy strategies through all stop variants."""
    all_dates = universe.trading_dates()
    results = {}
    prev_rv = {}

    for strat in LEGACY:
        ver = strat["ver"]
        entry_time = strat["entry"]
        print(f"\n  {ver} ({strat['name']})")

        # First pass: collect qualifying dates and their context
        qualifying = []
        for date in all_dates:
            vix = universe.ctx(date, 'vix_prior_close')
            if vix is None: continue

            prior_dir_raw = universe.ctx(date, 'prior_day_direction') or 'FLAT'
            pd_label = 'UP' if prior_dir_raw == 'UP' else 'DN' if prior_dir_raw == 'DOWN' else 'FL'

            if date in csv_range:
                in_range = csv_range[date]
            else:
                in_range = True  # default for dates beyond CSV
            rng = 'IN' if in_range else 'OT'

            gap_dir = universe.ctx(date, 'gap_direction')
            if gap_dir is None:
                gp = gap_cache.get(date, 0)
                if isinstance(gp, dict): gp = gp.get('gap_pct', 0)
                gap_dir = 'GUP' if gp > 0.25 else 'GDN' if gp < -0.25 else 'GFL'

            ret5d = universe.ctx(date, 'prior_5d_return') or 0
            rv, rv_slope = compute_rv_and_slope(universe, date, entry_time)
            vp = compute_vp_ratio(vix, rv)
            score_vol = compute_score_vol(vp, rv_slope)

            rv_1d_change = 0
            if date in prev_rv and rv is not None:
                rv_1d_change = rv - prev_rv[date]
            if rv is not None:
                idx = all_dates.index(date)
                if idx + 1 < len(all_dates):
                    prev_rv[all_dates[idx + 1]] = rv

            # Check filters
            if strat.get("type") in ("phoenix", "phoenix_clear"):
                fc = phoenix_fire_count(vix, vp, prior_dir_raw, ret5d, rv_1d_change, in_range, rv_slope)
                if fc == 0: continue
                if strat["type"] == "phoenix_clear":
                    v9d = vix9d_data.get(date)
                    if v9d is None or vix <= 0: continue
                    try:
                        if float(v9d) / float(vix) >= 1.0: continue
                    except: continue
                risk_budget = PHOENIX_TIER.get(fc, 100000)
            else:
                vr = strat.get("vix")
                if not vr or not (vix >= vr[0] and vix < vr[1]): continue
                if pd_label != strat["pd"]: continue
                if rng != strat["rng"]: continue
                if gap_dir != strat["gap"]: continue
                if not check_filter(strat.get("filter"), vp, rv_slope, ret5d, score_vol): continue
                if ver == "v9" and (vp or 0) > 2.0: continue
                risk_budget = regime_budget(ver, vp)

            spx_price = universe.spx_at(date, entry_time)
            if spx_price is None: continue
            daily_sigma = spx_price * (vix / 100) / math.sqrt(252)
            wing_width = max(40, round(daily_sigma * 0.75 / 5) * 5)
            atm = universe.current_atm(date, entry_time)
            if atm is None: continue

            qualifying.append({
                "date": date, "atm": atm, "wing_width": wing_width,
                "risk_budget": risk_budget, "vix": vix, "spx": spx_price
            })

        print(f"    {len(qualifying)} qualifying dates")
        if not qualifying:
            results[ver] = {v: {"n": 0} for v in STOP_VARIANTS}
            continue

        # Now run each stop variant
        for vname, stop_fn in STOP_VARIANTS.items():
            trades_pnl = []
            exits = defaultdict(int)

            for q in qualifying:
                structure = iron_butterfly(q["atm"], q["wing_width"])
                position = price_entry(universe, q["date"], entry_time, structure,
                                       risk_budget=q["risk_budget"])
                if position is None: continue

                exit_rules = [profit_target(strat["target"])]
                if strat["time_stop"]:
                    exit_rules.append(time_stop(strat["time_stop"]))
                exit_rules.extend(stop_fn())

                result = simulate_trade(universe, position, exit_rules, slippage_per_spread=1.0)
                if result is None: continue

                trades_pnl.append(result.pnl_dollar)
                ex = result.exit_type
                if "TARGET" in ex: exits["TARGET"] += 1
                elif "WING" in ex: exits["WING_STOP"] += 1
                elif "LOSS" in ex: exits["LOSS_STOP"] += 1
                elif "TIME" in ex: exits["TIME"] += 1
                else: exits["CLOSE"] += 1

            n = len(trades_pnl)
            if n == 0:
                results.setdefault(ver, {})[vname] = {"n": 0}
                continue

            wins = sum(1 for p in trades_pnl if p > 0)
            total_pnl = sum(trades_pnl)
            gross_w = sum(p for p in trades_pnl if p > 0)
            gross_l = abs(sum(p for p in trades_pnl if p <= 0))
            pf = round(gross_w / gross_l, 2) if gross_l > 0 else 999

            # Max drawdown
            eq = pk = dd = 0
            for p in trades_pnl:
                eq += p; pk = max(pk, eq); dd = max(dd, pk - eq)

            results.setdefault(ver, {})[vname] = {
                "n": n, "wins": wins, "wr": round(wins/n*100, 1),
                "total_pnl": round(total_pnl), "pf": pf,
                "max_dd": round(dd), "avg_pnl": round(total_pnl/n),
                "exits": dict(exits),
                "max_loss": round(min(trades_pnl)),
            }

    return results


def run_new_variants(universe):
    """Run all new strategies through all stop variants."""
    all_dates = universe.trading_dates()
    results = {}

    for sname, entry_time, structure_fn, target_pct, stop_time, risk_budget in NEW_STRATS:
        print(f"\n  {sname}")

        # Collect qualifying dates (all dates qualify for new strategies, except Morning Decel)
        qualifying = []
        for date in all_dates:
            vix = universe.ctx(date, 'vix_prior_close')
            # VIX sizing
            if vix is not None:
                if vix < 20: vix_mult = 1.0
                elif vix < 25: vix_mult = 0.5
                else: vix_mult = 0.25
            else:
                vix_mult = 1.0

            spx_price = universe.spx_at(date, entry_time)
            if spx_price is None: continue
            atm = universe.current_atm(date, entry_time)
            if atm is None: continue

            # Morning Decel intra-filter
            if sname == "Morning Decel Scalp":
                accel = universe.spx_acceleration(date, entry_time, 10) if hasattr(universe, 'spx_acceleration') else None
                if accel is None or accel >= -0.05:
                    continue

            adj_budget = int(risk_budget * vix_mult)
            qualifying.append({"date": date, "atm": atm, "adj_budget": adj_budget})

        print(f"    {len(qualifying)} qualifying dates")
        if not qualifying:
            results[sname] = {v: {"n": 0} for v in STOP_VARIANTS}
            continue

        for vname, stop_fn in STOP_VARIANTS.items():
            trades_pnl = []
            exits = defaultdict(int)

            for q in qualifying:
                structure = structure_fn(q["date"], entry_time, universe)
                if structure is None: continue
                position = price_entry(universe, q["date"], entry_time, structure,
                                       risk_budget=q["adj_budget"])
                if position is None: continue

                exit_rules = [profit_target(target_pct), time_stop(stop_time)]
                exit_rules.extend(stop_fn())

                result = simulate_trade(universe, position, exit_rules, slippage_per_spread=1.0)
                if result is None: continue

                trades_pnl.append(result.pnl_dollar)
                ex = result.exit_type
                if "TARGET" in ex: exits["TARGET"] += 1
                elif "WING" in ex: exits["WING_STOP"] += 1
                elif "LOSS" in ex: exits["LOSS_STOP"] += 1
                elif "TIME" in ex: exits["TIME"] += 1
                else: exits["CLOSE"] += 1

            n = len(trades_pnl)
            if n == 0:
                results.setdefault(sname, {})[vname] = {"n": 0}
                continue

            wins = sum(1 for p in trades_pnl if p > 0)
            total_pnl = sum(trades_pnl)
            gross_w = sum(p for p in trades_pnl if p > 0)
            gross_l = abs(sum(p for p in trades_pnl if p <= 0))
            pf = round(gross_w / gross_l, 2) if gross_l > 0 else 999

            eq = pk = dd = 0
            for p in trades_pnl:
                eq += p; pk = max(pk, eq); dd = max(dd, pk - eq)

            results.setdefault(sname, {})[vname] = {
                "n": n, "wins": wins, "wr": round(wins/n*100, 1),
                "total_pnl": round(total_pnl), "pf": pf,
                "max_dd": round(dd), "avg_pnl": round(total_pnl/n),
                "exits": dict(exits),
                "max_loss": round(min(trades_pnl)),
            }

    return results


def main():
    print("Loading DataUniverse...")
    universe = DataUniverse()
    universe.load(load_quotes=False)
    print(f"Loaded {len(universe.trading_dates())} trading days")

    # Load CSV range lookup
    csv_range = {}
    csv_path = os.path.join(_DIR, "research_all_trades.csv")
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                csv_range[row['date']] = bool(int(row['in_prior_week_range']))

    gap_cache = {}
    gp = os.path.join(_DIR, "spx_gap_cache.json")
    if os.path.exists(gp):
        with open(gp) as f: gap_cache = json.load(f)

    vix9d_data = {}
    v9p = os.path.join(_DIR, "vix9d_daily.json")
    if os.path.exists(v9p):
        with open(v9p) as f: vix9d_data = json.load(f)

    # ── LEGACY ──
    print("\n" + "=" * 80)
    print("LEGACY STRATEGIES — STOP VARIANT COMPARISON")
    print("=" * 80)
    legacy_results = run_legacy_variants(universe, csv_range, gap_cache, vix9d_data)

    # ── NEW ──
    print("\n" + "=" * 80)
    print("NEW STRATEGIES — STOP VARIANT COMPARISON")
    print("=" * 80)
    new_results = run_new_variants(universe)

    # ── PRINT RESULTS ──
    all_results = {}
    all_results.update(legacy_results)
    all_results.update(new_results)

    print("\n\n" + "=" * 130)
    print("RESULTS COMPARISON TABLE")
    print("=" * 130)

    for strat_name, variants in all_results.items():
        print(f"\n{'─'*130}")
        print(f"  {strat_name}")
        print(f"  {'Variant':<20} {'N':>5} {'WR':>7} {'Total P&L':>12} {'Avg P&L':>10} {'PF':>7} "
              f"{'MaxDD':>10} {'MaxLoss':>10} {'Exits':>35}")
        print(f"  {'─'*125}")

        # Sort by total P&L descending
        sorted_variants = sorted(variants.items(),
                                 key=lambda x: x[1].get('total_pnl', -999999), reverse=True)

        best_pnl = sorted_variants[0][1].get('total_pnl', 0) if sorted_variants else 0

        for vname, stats in sorted_variants:
            n = stats.get('n', 0)
            if n == 0:
                print(f"  {vname:<20} {'n/a':>5}")
                continue
            wr = stats['wr']
            pnl = stats['total_pnl']
            avg = stats['avg_pnl']
            pf = stats['pf']
            dd = stats['max_dd']
            ml = stats['max_loss']
            ex = stats.get('exits', {})
            ex_str = " ".join(f"{k}={v}" for k, v in sorted(ex.items()))
            flag = " <<<" if pnl == best_pnl else ""
            print(f"  {vname:<20} {n:>5} {wr:>6.1f}% ${pnl:>10,} ${avg:>8,} {pf:>6.2f} "
                  f"${dd:>8,} ${ml:>8,}  {ex_str}{flag}")

    # ── RECOMMENDATION ──
    print("\n\n" + "=" * 130)
    print("RECOMMENDED STOP PER STRATEGY")
    print("=" * 130)
    recommendations = {}
    for strat_name, variants in all_results.items():
        # Pick best by: highest P&L among variants with PF > 1.0
        candidates = [(v, s) for v, s in variants.items()
                       if s.get('n', 0) > 0 and s.get('pf', 0) > 1.0]
        if not candidates:
            candidates = [(v, s) for v, s in variants.items() if s.get('n', 0) > 0]
        if not candidates:
            print(f"  {strat_name:<35}: NO TRADES")
            continue

        # Rank by: PF * sqrt(n) as quality-adjusted metric, tiebreak on total P&L
        def score(item):
            s = item[1]
            # Sharpe-like: avg_pnl / stdev proxy, but simpler: PF * log(n+1) * sign(pnl)
            pf = s.get('pf', 0)
            pnl = s.get('total_pnl', 0)
            dd = s.get('max_dd', 1)
            # Use return/drawdown ratio as primary, PF as secondary
            rd = pnl / dd if dd > 0 else pnl
            return (rd, pf, pnl)

        best_v, best_s = max(candidates, key=score)
        recommendations[strat_name] = best_v
        print(f"  {strat_name:<35}: {best_v:<20} P&L=${best_s['total_pnl']:>10,}  "
              f"PF={best_s['pf']:.2f}  DD=${best_s['max_dd']:>8,}  WR={best_s['wr']}%")

    # Save full results
    output_path = os.path.join(_DIR, "stop_variant_results.json")
    with open(output_path, "w") as f:
        json.dump({"results": all_results, "recommendations": recommendations,
                   "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
    print(f"\nFull results saved to {output_path}")


if __name__ == "__main__":
    main()
