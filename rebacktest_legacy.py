#!/usr/bin/env python3
"""
rebacktest_legacy.py — Re-run ALL legacy strategies (V3-V14, N15) on the full
776-day dataset using the research engine at 5-min option bar resolution.

This replaces the CSV-based compute_stats.py pipeline entirely.
Every trade is:
  - Entered at the strategy's ACTUAL stated entry time
  - Priced from real option chains (data/option_chains/)
  - Centered on the correct ATM strike at entry time
  - Walked forward at 5-min resolution with 1-min SPX wing stop detection
  - Sized using the correct adaptive wing width formula

Output: rebacktest_legacy_results.json + printed comparison table
"""

import os
import sys
import json
import math
import numpy as np
from datetime import datetime
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

from research.data import DataUniverse
from research.structures import iron_butterfly, price_entry
from research.exits import (
    profit_target, time_stop, wing_stop, loss_stop, simulate_trade, standard_exits
)
from research.sweep import run_sweep, ibf_factory


# ─────────────────────────────────────────────────────────────────────────────
# REGIME SIGNAL COMPUTATION (same as refresh_legacy_strategies.py)
# ─────────────────────────────────────────────────────────────────────────────

def calc_rv_from_closes(closes):
    if len(closes) < 5:
        return None
    lr = np.diff(np.log(np.array(closes, dtype=float)))
    if len(lr) < 2:
        return None
    return float(np.std(lr) * np.sqrt(252 * 390) * 100)


def compute_rv_and_slope_at_time(universe, date, entry_time):
    """
    Compute RV and slope using bars up to entry_time (forward-walk safe).
    W1 = first 30 min of session, W2 = 30 min before entry.
    """
    bars = universe.spx_bars_range(date, "09:30", entry_time)
    if not bars or len(bars) < 20:
        return None, "UNKNOWN"

    all_closes = [b["c"] for _, b in bars]
    rv = calc_rv_from_closes(all_closes)

    # Split into two windows
    mid_idx = len(bars) // 2
    w1_closes = [b["c"] for _, b in bars[:mid_idx]]
    w2_closes = [b["c"] for _, b in bars[mid_idx:]]

    rv_w1 = calc_rv_from_closes(w1_closes)
    rv_w2 = calc_rv_from_closes(w2_closes)

    if rv_w1 and rv_w1 > 0 and rv_w2 is not None:
        slope = (rv_w2 - rv_w1) / rv_w1 * 100
        if slope > 20:
            label = "RISING"
        elif slope < -20:
            label = "FALLING"
        else:
            label = "STABLE"
    else:
        label = "UNKNOWN"

    return rv, label


def compute_vp_ratio(vix_val, rv):
    if rv and rv > 0:
        return round(vix_val / rv, 2)
    rv_est = vix_val * 0.75
    return round(vix_val / rv_est, 2) if rv_est > 0 else 1.5


def compute_score_vol(vp, rv_slope_label):
    if vp is None:
        return 15
    if vp >= 1.5:
        score = 30
    elif vp >= 1.2:
        score = 22
    elif vp >= 1.0:
        score = 15
    elif vp >= 0.85:
        score = 10
    else:
        score = 3
    if rv_slope_label == "RISING":
        score = max(0, score - 15)
    elif rv_slope_label == "FALLING":
        score = min(30, score + 5)
    return score


# ─────────────────────────────────────────────────────────────────────────────
# PHOENIX FIRE COUNT
# ─────────────────────────────────────────────────────────────────────────────

def phoenix_fire_count(vix, vp, prior_dir, ret5d, rv_1d_change, in_range, rv_slope):
    if vp is None: vp = 99
    if ret5d is None: ret5d = -99
    if rv_1d_change is None: rv_1d_change = -99
    g1 = vix <= 20 and vp <= 1.0 and ret5d > 0
    g2 = vp <= 1.3 and prior_dir == "DOWN" and ret5d > 0
    g3 = vp <= 1.2 and ret5d > 0 and rv_1d_change > 0
    g4 = vp <= 1.5 and not in_range and ret5d > 0
    g5 = vp <= 1.3 and rv_slope != "RISING" and ret5d > 0
    return sum([g1, g2, g3, g4, g5]), [g1, g2, g3, g4, g5]


# ─────────────────────────────────────────────────────────────────────────────
# SIZING
# ─────────────────────────────────────────────────────────────────────────────

PHOENIX_TIER = {0: 0, 1: 25000, 2: 50000, 3: 75000, 4: 100000, 5: 100000}
REGIME_MAX = {
    'v6': 75000, 'v7': 25000, 'v8': 25000, 'v9': 100000,
    'v10': 75000, 'v12': 75000, 'v14': 75000,
}

def regime_budget(ver, vp):
    if vp is None: vp = 1.5
    max_bud = REGIME_MAX.get(ver, 100000)
    if vp <= 1.0: scale = 1.00
    elif vp <= 1.2: scale = 0.75
    elif vp <= 1.5: scale = 0.50
    else: scale = 0.25
    return int(max_bud * scale)


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

# Per-strategy stop rules optimized via test_stop_variants.py on 776-day backtest
# stop_type: "wing+loss70" | "loss50" | "loss70" | "none" | "wing_only"
STRATS = [
    {"ver": "v3", "type": "phoenix", "entry": "10:00",
     "target": 0.50, "time_stop": None, "stop_type": "wing+loss70", "name": "PHOENIX"},
    {"ver": "n15", "type": "phoenix_clear", "entry": "10:00",
     "target": 0.50, "time_stop": None, "stop_type": "loss50", "name": "PHOENIX CLEAR"},
    {"ver": "v6", "vix": [0, 15], "pd": "DN", "rng": "IN", "gap": "GFL",
     "filter": "VP<=1.7", "entry": "10:00", "target": 0.50, "time_stop": "15:30",
     "stop_type": "none", "name": "QUIET REBOUND"},
    {"ver": "v7", "vix": [0, 15], "pd": "FL", "rng": "IN", "gap": "GUP",
     "filter": None, "entry": "10:00", "target": 0.40, "time_stop": None,
     "stop_type": "wing_only", "name": "FLAT-GAP FADE"},
    {"ver": "v9", "vix": [15, 20], "pd": "UP", "rng": "OT", "gap": "GFL",
     "filter": "!RISING", "entry": "10:00", "target": 0.70, "time_stop": "15:45",
     "stop_type": "loss70", "name": "BREAKOUT STALL"},
    {"ver": "v12", "vix": [0, 15], "pd": "UP", "rng": "OT", "gap": "GUP",
     "filter": "5dRet>1", "entry": "10:00", "target": 0.40, "time_stop": None,
     "stop_type": "loss50", "name": "BULL SQUEEZE"},
]


def check_filter(filt, vp, rv_slope, ret5d, score_vol):
    if filt is None: return True
    if filt == "VP<=1.7": return (vp or 99) <= 1.7
    if filt == "VP<=2.0": return (vp or 99) <= 2.0
    if filt == "!RISING": return rv_slope != "RISING"
    if filt == "5dRet>0": return (ret5d or -99) > 0
    if filt == "5dRet>1": return (ret5d or -99) > 1.0
    if filt == "ScoreVol<18": return (score_vol or 99) < 18
    return True


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def load_csv_range_lookup():
    """Load in_prior_week_range from research_all_trades.csv as ground truth.
    The CSV uses a different (original) calculation that disagrees with
    daily_context.json on 36% of dates. compute_stats.py reads from CSV,
    so we must too for a fair comparison."""
    import csv as _csv
    csv_path = os.path.join(_DIR, "research_all_trades.csv")
    lookup = {}
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            for row in _csv.DictReader(f):
                lookup[row['date']] = bool(int(row['in_prior_week_range']))
    return lookup


def compute_in_prior_week_range(universe, date, all_dates):
    """Fallback for dates not in the CSV: compute in_prior_week_range from
    SPX daily bars. Uses prior 5 trading days' high/low vs today's open."""
    idx = all_dates.index(date)
    if idx < 5:
        return True  # not enough history, default to IN
    prior_5 = all_dates[idx-5:idx]
    highs = []
    lows = []
    for d in prior_5:
        bars = universe.spx_bars_range(d, "09:30", "16:00")
        if bars:
            day_h = max(b["h"] for _, b in bars)
            day_l = min(b["l"] for _, b in bars)
            highs.append(day_h)
            lows.append(day_l)
    if not highs:
        return True
    week_high = max(highs)
    week_low = min(lows)
    today_open = universe.spx_at(date, "09:31")
    if today_open is None:
        return True
    return week_low <= today_open <= week_high


def main():
    print("Loading DataUniverse...")
    universe = DataUniverse()
    universe.load(load_quotes=False)
    all_dates = universe.trading_dates()
    print(f"Loaded {len(all_dates)} trading days: {all_dates[0]} to {all_dates[-1]}")

    # Load VIX9D for N15
    vix9d_path = os.path.join(_DIR, "vix9d_daily.json")
    vix9d_data = {}
    if os.path.exists(vix9d_path):
        with open(vix9d_path) as f:
            vix9d_data = json.load(f)

    # Load gap cache (used for gap classification alongside daily_context)
    gap_cache_path = os.path.join(_DIR, "spx_gap_cache.json")
    gap_cache = {}
    if os.path.exists(gap_cache_path):
        with open(gap_cache_path) as f:
            gap_cache = json.load(f)

    # Load CSV-sourced in_prior_week_range (ground truth for comparison)
    csv_range_lookup = load_csv_range_lookup()
    print(f"Loaded CSV range lookup: {len(csv_range_lookup)} dates")

    # Track previous day's RV for rv_1d_change (Phoenix G3 signal)
    prev_rv = {}

    # Results storage
    all_results = {}
    all_trades_flat = []

    for strat in STRATS:
        ver = strat["ver"]
        entry_time = strat["entry"]
        print(f"\n{'='*60}")
        print(f"Running {ver} ({strat['name']}) — entry at {entry_time}")
        print(f"{'='*60}")

        trades = []
        skipped_filter = 0
        skipped_price = 0
        skipped_data = 0

        for date in all_dates:
            # ── Get daily context signals ──
            vix = universe.ctx(date, 'vix_prior_close')
            if vix is None:
                skipped_data += 1
                continue

            prior_dir_raw = universe.ctx(date, 'prior_day_direction') or 'FLAT'
            pd_label = 'UP' if prior_dir_raw == 'UP' else 'DN' if prior_dir_raw == 'DOWN' else 'FL'

            # Use CSV range lookup as ground truth; fall back to computed for new dates
            if date in csv_range_lookup:
                in_range = csv_range_lookup[date]
            else:
                in_range = compute_in_prior_week_range(universe, date, all_dates)
            rng = 'IN' if in_range else 'OT'

            gap_dir = universe.ctx(date, 'gap_direction')
            if gap_dir is None:
                gp = gap_cache.get(date, 0)
                if isinstance(gp, dict): gp = gp.get('gap_pct', 0)
                gap_dir = 'GUP' if gp > 0.25 else 'GDN' if gp < -0.25 else 'GFL'

            ret5d = universe.ctx(date, 'prior_5d_return') or 0

            # Compute intraday regime signals at entry time
            rv, rv_slope = compute_rv_and_slope_at_time(universe, date, entry_time)
            vp = compute_vp_ratio(vix, rv)
            score_vol = compute_score_vol(vp, rv_slope)

            # RV 1-day change for Phoenix G3
            rv_1d_change = 0
            if date in prev_rv and rv is not None:
                rv_1d_change = rv - prev_rv[date]
            if rv is not None:
                # Store for next day
                # Find next trading day
                idx = all_dates.index(date)
                if idx + 1 < len(all_dates):
                    prev_rv[all_dates[idx + 1]] = rv

            # ── Strategy filter evaluation ──
            if strat.get("type") in ("phoenix", "phoenix_clear"):
                fc, signals = phoenix_fire_count(
                    vix, vp, prior_dir_raw, ret5d, rv_1d_change, in_range, rv_slope)
                if fc == 0:
                    skipped_filter += 1
                    continue
                if strat["type"] == "phoenix_clear":
                    v9d = vix9d_data.get(date)
                    if v9d is None or vix <= 0:
                        skipped_filter += 1
                        continue
                    try:
                        if float(v9d) / float(vix) >= 1.0:
                            skipped_filter += 1
                            continue
                    except (TypeError, ValueError):
                        skipped_filter += 1
                        continue
                risk_budget = PHOENIX_TIER.get(fc, 100000)
            else:
                # Regime strategy
                vix_range = strat.get("vix")
                if not vix_range:
                    skipped_filter += 1
                    continue
                if not (vix >= vix_range[0] and vix < vix_range[1]):
                    skipped_filter += 1
                    continue
                if pd_label != strat["pd"]:
                    skipped_filter += 1
                    continue
                if rng != strat["rng"]:
                    skipped_filter += 1
                    continue
                if gap_dir != strat["gap"]:
                    skipped_filter += 1
                    continue
                if not check_filter(strat.get("filter"), vp, rv_slope, ret5d, score_vol):
                    skipped_filter += 1
                    continue
                if ver == "v9" and (vp or 0) > 2.0:
                    skipped_filter += 1
                    continue

                risk_budget = regime_budget(ver, vp)
                if ver == "v10" and ret5d <= -1.5:
                    risk_budget = risk_budget // 2

                fc = None
                signals = None

            # ── Build IBF at correct entry time and simulate ──
            # Adaptive wing width
            spx_price = universe.spx_at(date, entry_time)
            if spx_price is None:
                skipped_price += 1
                continue

            daily_sigma = spx_price * (vix / 100) / math.sqrt(252)
            raw_wing = daily_sigma * 0.75
            wing_width = max(40, round(raw_wing / 5) * 5)

            atm = universe.current_atm(date, entry_time)
            if atm is None:
                skipped_price += 1
                continue

            structure = iron_butterfly(atm, wing_width)
            position = price_entry(universe, date, entry_time, structure,
                                   risk_budget=risk_budget)
            if position is None:
                skipped_price += 1
                continue

            # Build exit rules with per-strategy stop configuration
            exit_rules = [profit_target(strat["target"])]
            if strat["time_stop"]:
                exit_rules.append(time_stop(strat["time_stop"]))
            st = strat.get("stop_type", "wing_only")
            if st == "wing+loss70":
                exit_rules.extend([wing_stop(), loss_stop(0.70)])
            elif st == "loss70":
                exit_rules.append(loss_stop(0.70))
            elif st == "loss50":
                exit_rules.append(loss_stop(0.50))
            elif st == "wing_only":
                exit_rules.append(wing_stop())
            # "none" = no stop, just target + time/close

            result = simulate_trade(universe, position, exit_rules, slippage_per_spread=1.0)
            if result is None:
                skipped_price += 1
                continue

            trade = {
                "date": date,
                "ver": ver,
                "entry_time": entry_time,
                "exit_time": result.exit_time,
                "exit_type": result.exit_type,
                "entry_credit": round(result.entry_credit, 2),
                "pnl_per_spread": round(result.pnl_per_spread, 2),
                "pnl_dollar": round(result.pnl_dollar, 0),
                "qty": result.qty,
                "wing_width": wing_width,
                "atm": atm,
                "risk_budget": risk_budget,
                "vix": round(vix, 2),
                "vp": round(vp, 2) if vp else None,
                "fire_count": fc,
                "spx_at_entry": round(spx_price, 2),
            }
            trades.append(trade)
            all_trades_flat.append(trade)

        # ── Compute stats for this strategy ──
        n = len(trades)
        print(f"  Trades: {n} | Filtered: {skipped_filter} | No price: {skipped_price} | No data: {skipped_data}")

        if n == 0:
            all_results[ver] = {
                "ver": ver, "name": strat["name"], "n": 0,
                "total_pnl": 0, "note": "No qualifying trades"
            }
            continue

        pnls = [t["pnl_dollar"] for t in trades]
        wins = [t for t in trades if t["pnl_dollar"] > 0]
        losses = [t for t in trades if t["pnl_dollar"] <= 0]
        gross_w = sum(t["pnl_dollar"] for t in wins)
        gross_l = abs(sum(t["pnl_dollar"] for t in losses))

        # Exit type counts
        exit_counts = defaultdict(int)
        for t in trades:
            ex = t["exit_type"]
            if "TARGET" in ex: exit_counts["TARGET"] += 1
            elif "WING" in ex: exit_counts["WING_STOP"] += 1
            elif "TIME" in ex: exit_counts["TIME"] += 1
            else: exit_counts["CLOSE"] += 1

        # Drawdown
        equity = peak = max_dd = 0
        for t in trades:
            equity += t["pnl_dollar"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        # Yearly breakdown
        yearly = defaultdict(lambda: {"pnl": 0, "n": 0, "wins": 0})
        for t in trades:
            y = t["date"][:4]
            yearly[y]["pnl"] += t["pnl_dollar"]
            yearly[y]["n"] += 1
            if t["pnl_dollar"] > 0:
                yearly[y]["wins"] += 1

        stats = {
            "ver": ver,
            "name": strat["name"],
            "entry_time": entry_time,
            "n": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1),
            "total_pnl": round(sum(pnls)),
            "avg_pnl": round(sum(pnls) / n),
            "max_win": round(max(pnls)),
            "max_loss": round(min(pnls)),
            "gross_wins": round(gross_w),
            "gross_losses": round(gross_l),
            "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else 999,
            "max_drawdown": round(max_dd),
            "exit_counts": dict(exit_counts),
            "yearly": {y: dict(v) for y, v in sorted(yearly.items())},
        }
        all_results[ver] = stats

        wr = stats["win_rate"]
        pf = stats["profit_factor"]
        print(f"  WR: {wr}% | P&L: ${stats['total_pnl']:,} | PF: {pf:.2f} | DD: ${stats['max_drawdown']:,}")
        print(f"  Exits: {dict(exit_counts)}")
        for y, ydata in sorted(yearly.items()):
            ywr = round(ydata["wins"] / ydata["n"] * 100, 1) if ydata["n"] > 0 else 0
            print(f"    {y}: n={ydata['n']:>3}  P&L=${ydata['pnl']:>10,.0f}  WR={ywr}%")

    # ── Load old compute_stats.py results for comparison ──
    old_stats_path = os.path.join(_DIR, "strategy_stats.json")
    old_stats = {}
    if os.path.exists(old_stats_path):
        with open(old_stats_path) as f:
            old_stats = json.load(f)

    # ── Print comparison table ──
    print("\n" + "=" * 100)
    print("COMPARISON: compute_stats.py (CSV) vs Research Engine (5-min)")
    print("=" * 100)
    print(f"{'Strategy':<20} {'':>5} {'OLD N':>6} {'NEW N':>6} {'OLD WR':>7} {'NEW WR':>7} "
          f"{'OLD P&L':>12} {'NEW P&L':>12} {'OLD PF':>7} {'NEW PF':>7} {'DELTA':>12}")
    print("-" * 100)

    total_old = 0
    total_new = 0
    for ver in ['v3', 'n15', 'v6', 'v7', 'v9', 'v12']:
        old = old_stats.get(ver, {})
        new = all_results.get(ver, {})

        on = old.get('total_trades', 0)
        nn = new.get('n', 0)
        owr = old.get('win_rate', 0)
        nwr = new.get('win_rate', 0)
        opnl = old.get('total_pnl', 0)
        npnl = new.get('total_pnl', 0)
        opf = old.get('profit_factor', 0)
        npf = new.get('profit_factor', 0)
        delta = npnl - opnl
        name = new.get('name', ver)

        total_old += opnl
        total_new += npnl

        flag = " !!!" if (opnl > 0 and npnl <= 0) else ""
        print(f"{name:<20} {ver:>5} {on:>6} {nn:>6} {owr:>6.1f}% {nwr:>6.1f}% "
              f"${opnl:>10,} ${npnl:>10,} {opf:>6.2f} {npf:>6.2f} ${delta:>10,}{flag}")

    print("-" * 100)
    print(f"{'TOTAL':<20} {'':>5} {'':>6} {'':>6} {'':>7} {'':>7} "
          f"${total_old:>10,} ${total_new:>10,} {'':>7} {'':>7} ${total_new - total_old:>10,}")

    # ── Save results ──
    output = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": f"{all_dates[0]} to {all_dates[-1]} ({len(all_dates)} days)",
        "engine": "research framework, 5-min option bars, 1-min SPX wing detection",
        "stats": all_results,
        "trades": all_trades_flat,
    }

    outfile = os.path.join(_DIR, "rebacktest_legacy_results.json")
    with open(outfile, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {outfile}")
    print(f"Total trades: {len(all_trades_flat)}")


if __name__ == "__main__":
    main()
