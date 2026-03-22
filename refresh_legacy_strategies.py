#!/usr/bin/env python3
"""
refresh_legacy_strategies.py — Incremental refresh for the 11 legacy strategies (V3-V14, N15).

Replaces the CSV-dependent compute_stats.py pipeline for NEW dates.
Pulls Polygon data, computes regime signals (VP ratio, RV slope, score_vol),
evaluates strategy filters, simulates trades via the research engine,
and merges results into strategy_trades.json + strategy_stats.json.

N17/N18 are handled separately by pull_real_fills.py (real broker fills).

Flow:
  1. Read strategy_trades.json → find max trade date for legacy strategies
  2. Query Polygon for trading days after that date through yesterday
  3. For each new day: pull SPX 1-min bars, option chains, VIX bars (reuses refresh_new_strategies.py logic)
  4. Compute regime signals: VP ratio, RV slope, score_vol from intraday bars
  5. Evaluate legacy strategy filters on each new day
  6. Run qualifying trades through the research engine (DataUniverse + simulate_trade)
  7. Append new trades to strategy_trades.json and regenerate strategy_stats.json

Usage:
  python3 refresh_legacy_strategies.py              # uses POLYGON_API_KEY env or cockpit_config.json
  python3 refresh_legacy_strategies.py YOUR_API_KEY  # explicit key
"""

import os
import sys
import json
import time
import math
import numpy as np
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
from sizing_scores import compute_sizing

DATA_DIR = os.path.join(_DIR, "data")
SPX_DIR = os.path.join(DATA_DIR, "spx_1min")
OPT_DIR = os.path.join(DATA_DIR, "option_chains")
VIX_DIR = os.path.join(DATA_DIR, "vix_1min")
TRADES_FILE = os.path.join(_DIR, "strategy_trades.json")
STATS_FILE = os.path.join(_DIR, "strategy_stats.json")


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# REGIME SIGNAL COMPUTATION (from intraday bars)
# ─────────────────────────────────────────────────────────────────────────────

def calc_rv_from_closes(closes):
    """Annualized realized vol from 1-min close prices."""
    if len(closes) < 5:
        return None
    lr = np.diff(np.log(np.array(closes, dtype=float)))
    if len(lr) < 2:
        return None
    return float(np.std(lr) * np.sqrt(252 * 390) * 100)


def compute_rv_and_slope(spx_bars):
    """
    Compute realized vol and RV slope from SPX 1-min bars.
    Uses first 30 min as W1, next 30 min as W2 (9:30-10:00 vs 10:00-10:30).
    This matches the pre-open signal timing for 10:00 entry strategies.
    Returns: (rv, rv_slope_label)
    """
    if not spx_bars:
        return None, "UNKNOWN"

    # Sort bars by time
    times = sorted(spx_bars.keys())
    if len(times) < 20:
        return None, "UNKNOWN"

    # Get all closes
    all_closes = [spx_bars[t]["c"] for t in times if "09:30" <= t <= "10:30"]
    rv = calc_rv_from_closes(all_closes) if len(all_closes) >= 10 else None

    # W1 = 9:30-10:00, W2 = 10:00-10:30
    w1_closes = [spx_bars[t]["c"] for t in times if "09:30" <= t < "10:00"]
    w2_closes = [spx_bars[t]["c"] for t in times if "10:00" <= t <= "10:30"]

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
    """VP ratio: VIX / realized vol."""
    if rv and rv > 0:
        return round(vix_val / rv, 2)
    # Fallback estimate
    rv_est = vix_val * 0.75
    return round(vix_val / rv_est, 2) if rv_est > 0 else 1.5


def compute_score_vol(vp, rv_slope_label):
    """Score_vol matching backtest_research.py logic. Range 0-30."""
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


def compute_morning_range_pct(spx_bars):
    """Morning range (9:30-10:00) as % of opening price."""
    morning = {t: b for t, b in spx_bars.items() if "09:30" <= t <= "10:00"}
    if not morning:
        return 0.5
    hi = max(b["h"] for b in morning.values())
    lo = min(b["l"] for b in morning.values())
    op_bar = spx_bars.get("09:31") or spx_bars.get("09:30")
    if not op_bar:
        return 0.5
    op = op_bar["o"]
    if op <= 0:
        return 0.5
    return round((hi - lo) / op * 100, 4)


# ─────────────────────────────────────────────────────────────────────────────
# PHOENIX FIRE COUNT
# ─────────────────────────────────────────────────────────────────────────────

def phoenix_fire_count(vix, vp, prior_dir, ret5d, rv_1d_change, in_range, rv_slope):
    """Compute Phoenix 5-signal confluence. Returns (fire_count, [g1..g5])."""
    if vp is None:
        vp = 99
    if ret5d is None:
        ret5d = -99
    if rv_1d_change is None:
        rv_1d_change = -99

    g1 = vix <= 20 and vp <= 1.0 and ret5d > 0
    g2 = vp <= 1.3 and prior_dir == "DOWN" and ret5d > 0
    g3 = vp <= 1.2 and ret5d > 0 and rv_1d_change > 0
    g4 = vp <= 1.5 and not in_range and ret5d > 0
    g5 = vp <= 1.3 and rv_slope != "RISING" and ret5d > 0
    return sum([g1, g2, g3, g4, g5]), [g1, g2, g3, g4, g5]


# ─────────────────────────────────────────────────────────────────────────────
# SIZING
# ─────────────────────────────────────────────────────────────────────────────

PHOENIX_TIER_MAP = {0: 0, 1: 25000, 2: 50000, 3: 75000, 4: 100000, 5: 100000}

REGIME_MAX_BUDGET = {
    'v6': 75000, 'v7': 25000, 'v9': 100000, 'v12': 75000,
}

def regime_budget(ver, vp):
    if vp is None:
        vp = 1.5
    max_bud = REGIME_MAX_BUDGET.get(ver, 100000)
    if vp <= 1.0:
        scale = 1.00
    elif vp <= 1.2:
        scale = 0.75
    elif vp <= 1.5:
        scale = 0.50
    else:
        scale = 0.25
    return int(max_bud * scale)


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY DEFINITIONS (from CLAUDE.md / compute_stats.py)
# ─────────────────────────────────────────────────────────────────────────────

# Per-strategy stop rules optimized via test_stop_variants.py on 776-day backtest
# stop_type: "wing+loss70" | "loss50" | "loss70" | "none" | "wing_only"
LEGACY_STRATS = [
    {"ver": "v3", "type": "phoenix", "mech": "50%/close/1T", "entry": "10:00",
     "stop_type": "wing+loss70", "name": "PHOENIX", "color": "#f59e0b"},
    {"ver": "n15", "type": "phoenix_clear", "mech": "50%/close/1T", "entry": "10:00",
     "stop_type": "loss50", "name": "PHOENIX CLEAR", "color": "#22c55e"},
    {"ver": "v6", "vix": [0, 15], "pd": "DN", "rng": "IN", "gap": "GFL",
     "filter": "VP<=1.7", "mech": "50%/1530/1T", "entry": "10:00",
     "stop_type": "none", "name": "QUIET REBOUND", "color": "#06b6d4"},
    {"ver": "v7", "vix": [0, 15], "pd": "FL", "rng": "IN", "gap": "GUP",
     "filter": None, "mech": "40%/close/1T", "entry": "10:00",
     "stop_type": "wing_only", "name": "FLAT-GAP FADE", "color": "#a855f7"},
    {"ver": "v9", "vix": [15, 20], "pd": "UP", "rng": "OT", "gap": "GFL",
     "filter": "!RISING", "mech": "70%/1545/1T", "entry": "10:00",
     "stop_type": "loss70", "name": "BREAKOUT STALL", "color": "#eab308"},
    {"ver": "v12", "vix": [0, 15], "pd": "UP", "rng": "OT", "gap": "GUP",
     "filter": "5dRet>1", "mech": "40%/close/1T", "entry": "10:00",
     "stop_type": "loss50", "name": "BULL SQUEEZE", "color": "#f97316"},
]


def check_filter(filt, filter_data):
    """Check if a signal filter passes."""
    if filt is None:
        return True
    vp = filter_data.get("vp", 1.5)
    rv_slope_label = filter_data.get("rv_slope_label", "UNKNOWN")
    ret5d = filter_data.get("ret5d", 0)
    score_vol = filter_data.get("score_vol", 15)
    if filt == "5dRet>0":
        return ret5d > 0
    if filt == "5dRet>1":
        return ret5d > 1.0
    if filt == "VP<=1.7":
        return vp <= 1.7
    if filt == "VP<=2.0":
        return vp <= 2.0
    if filt == "!RISING":
        return rv_slope_label != "RISING"
    if filt == "ScoreVol<18":
        return score_vol < 18
    return True


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def run_legacy_on_dates(new_dates):
    """
    For each new date, compute regime signals and run qualifying legacy strategies.
    Uses the research engine (DataUniverse + simulate_trade) for actual P&L computation.
    """
    from research.data import DataUniverse
    from research.exits import profit_target, time_stop, wing_stop, loss_stop, simulate_trade
    from research.structures import iron_butterfly, price_entry

    log("Loading DataUniverse for legacy strategies...")
    universe = DataUniverse()
    universe.load(load_quotes=False)
    log(f"  Loaded {len(universe.trading_dates())} trading days")

    # Load VIX9D data for N15
    vix9d_path = os.path.join(_DIR, "vix9d_daily.json")
    vix9d_data = {}
    if os.path.exists(vix9d_path):
        with open(vix9d_path) as f:
            vix9d_data = json.load(f)

    # Load gap cache for gap classification
    gap_cache_path = os.path.join(_DIR, "spx_gap_cache.json")
    gap_cache = {}
    if os.path.exists(gap_cache_path):
        with open(gap_cache_path) as f:
            gap_cache = json.load(f)

    new_trades = []

    for date in new_dates:
        log(f"  Processing {date}...")

        # Get daily context signals
        ctx = {}
        for field in ['vix_prior_close', 'prior_day_direction', 'in_prior_week_range',
                       'gap_direction', 'prior_5d_return', 'gap_pct']:
            ctx[field] = universe.ctx(date, field)

        vix = ctx.get('vix_prior_close')
        if vix is None:
            log(f"    Skipping {date}: no VIX data")
            continue

        prior_dir_raw = ctx.get('prior_day_direction', 'FLAT')
        pd_label = 'UP' if prior_dir_raw == 'UP' else 'DN' if prior_dir_raw == 'DOWN' else 'FL'

        # Use CSV range lookup as ground truth for historical dates,
        # compute from SPX daily bars for new dates (daily_context.json disagrees
        # with CSV on 36% of dates for in_prior_week_range)
        csv_range_lookup = getattr(run_legacy_on_dates, '_csv_range', None)
        if csv_range_lookup is None:
            import csv as _csv
            csv_path = os.path.join(_DIR, "research_all_trades.csv")
            csv_range_lookup = {}
            if os.path.exists(csv_path):
                with open(csv_path) as f:
                    for row in _csv.DictReader(f):
                        csv_range_lookup[row['date']] = bool(int(row['in_prior_week_range']))
            run_legacy_on_dates._csv_range = csv_range_lookup

        if date in csv_range_lookup:
            in_range = csv_range_lookup[date]
        else:
            # For new dates: compute from prior 5 trading days' high/low vs today's open
            all_td = universe.trading_dates()
            if date in all_td:
                idx = all_td.index(date)
                if idx >= 5:
                    prior_5 = all_td[idx-5:idx]
                    highs, lows = [], []
                    for pd in prior_5:
                        pb = universe.spx_bars_range(pd, "09:30", "16:00")
                        if pb:
                            highs.append(max(b["h"] for _, b in pb))
                            lows.append(min(b["l"] for _, b in pb))
                    today_open = universe.spx_at(date, "09:31")
                    if highs and today_open:
                        in_range = min(lows) <= today_open <= max(highs)
                    else:
                        in_range = ctx.get('in_prior_week_range', True)
                else:
                    in_range = True
            else:
                in_range = ctx.get('in_prior_week_range', True)
        rng = 'IN' if in_range else 'OT'

        # Gap classification — try daily_context first, then gap_cache
        gap_dir = ctx.get('gap_direction')
        if gap_dir is None:
            gap_pct = gap_cache.get(date, 0)
            if isinstance(gap_pct, dict):
                gap_pct = gap_pct.get('gap_pct', 0)
            gap_dir = 'GUP' if gap_pct > 0.25 else 'GDN' if gap_pct < -0.25 else 'GFL'

        ret5d = ctx.get('prior_5d_return', 0) or 0

        # Compute intraday regime signals from SPX 1-min bars
        spx_file = os.path.join(SPX_DIR, f"{date}.json")
        if os.path.exists(spx_file):
            with open(spx_file) as f:
                spx_bars = json.load(f)
        else:
            spx_bars = {}

        rv, rv_slope_label = compute_rv_and_slope(spx_bars)
        vp = compute_vp_ratio(vix, rv)
        score_vol = compute_score_vol(vp, rv_slope_label)

        # rv_1d_change: would need yesterday's rv. Approximate from daily context.
        # For simplicity, use 0 (neutral) — this only affects Phoenix G3 signal.
        rv_1d_change = 0

        filter_data = {
            "vp": vp, "rv_slope_label": rv_slope_label,
            "ret5d": ret5d, "score_vol": score_vol,
        }

        # Evaluate each strategy
        for strat in LEGACY_STRATS:
            ver = strat["ver"]

            # Build exit rules from mech string
            mech = strat["mech"]
            parts = mech.split("/")
            target_pct = int(parts[0].replace("%", "")) / 100
            time_stop_str = parts[1]
            entry_time = strat["entry"]

            if strat.get("type") in ("phoenix", "phoenix_clear"):
                fc, signals = phoenix_fire_count(
                    vix, vp, prior_dir_raw, ret5d, rv_1d_change, in_range, rv_slope_label)
                if fc == 0:
                    continue

                # N15: additional VIX9D/VIX < 1.0 filter
                if strat["type"] == "phoenix_clear":
                    v9d = vix9d_data.get(date)
                    if v9d is None or vix <= 0:
                        continue
                    try:
                        if float(v9d) / float(vix) >= 1.0:
                            continue
                    except (TypeError, ValueError):
                        continue

                risk_budget = PHOENIX_TIER_MAP.get(fc, 100000)
            else:
                # Regime strategy filter check
                vix_range = strat.get("vix")
                if not vix_range:
                    continue
                if not (vix >= vix_range[0] and vix < vix_range[1]):
                    continue
                if pd_label != strat["pd"]:
                    continue
                if rng != strat["rng"]:
                    continue
                if gap_dir != strat["gap"]:
                    continue
                if not check_filter(strat.get("filter"), filter_data):
                    continue

                # V9: VP cap
                if ver == "v9" and vp > 2.0:
                    continue

                risk_budget = regime_budget(ver, vp)

                fc = None
                signals = None

            # ── Sizing score ──
            gap_pct_val = gap_cache.get(date, 0)
            if isinstance(gap_pct_val, dict):
                gap_pct_val = gap_pct_val.get('gap_pct', 0)
            # Get day-of-week and prior-day return from universe context
            _dow_names = {0:'Monday',1:'Tuesday',2:'Wednesday',3:'Thursday',4:'Friday'}
            try:
                _dow = _dow_names.get(datetime.strptime(date, "%Y-%m-%d").weekday(), '')
            except:
                _dow = ''
            prior_1d_val = universe.ctx(date, 'prior_day_return') or 0
            prior_day_range_val = universe.ctx(date, 'prior_day_range')
            # Compute term structure label from VIX9D/VIX
            _v9d_val = vix9d_data.get(date)
            _ts_label = ''
            if _v9d_val and vix and vix > 0:
                _ratio = float(_v9d_val) / float(vix)
                if _ratio < 0.9:   _ts_label = 'INVERTED'
                elif _ratio > 1.1: _ts_label = 'CONTANGO'
                else:              _ts_label = 'FLAT'
            sizing_ctx = {
                'prior_dir': prior_dir_raw,
                'prior_1d': float(prior_1d_val) if prior_1d_val else None,
                'prior_5d': float(ret5d) if ret5d else None,
                'fire_count': fc if fc else 0,
                'rv': float(rv) if rv else None,
                'dow': _dow,
                'rv_slope': rv_slope_label or '',
                'ts_label': _ts_label,
                'vp_ratio': float(vp) if vp else None,
                'gap_pct': gap_pct_val,
                'in_prior_week_range': in_range,
                'prior_day_range': float(prior_day_range_val) if prior_day_range_val else None,
            }
            sizing_mult, sizing_score = compute_sizing(ver, sizing_ctx)
            risk_budget = int(risk_budget * sizing_mult)

            # ── Build the IBF and simulate trade ──
            # Adaptive wing width (matches CLAUDE.md formula)
            spx_price = universe.spx_at(date, entry_time)
            if spx_price is None:
                continue
            daily_sigma = spx_price * (vix / 100) / math.sqrt(252)
            raw_wing = daily_sigma * 0.75
            wing_width = max(40, round(raw_wing / 5) * 5)

            atm = universe.current_atm(date, entry_time)
            if atm is None:
                continue

            structure = iron_butterfly(atm, wing_width)
            position = price_entry(universe, date, entry_time, structure,
                                   risk_budget=risk_budget)
            if position is None:
                continue

            # Build exit rules with per-strategy stop configuration
            exit_rules = [profit_target(target_pct)]
            if time_stop_str == "1530":
                exit_rules.append(time_stop("15:30"))
            elif time_stop_str == "1545":
                exit_rules.append(time_stop("15:45"))
            # "close" = hold to settlement, no time stop needed

            # Per-strategy stop type (optimized via test_stop_variants.py)
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
                continue

            # Build intraday P&L dict
            intraday = {}
            for t_str, pnl_val in result.pnl_timeline.items():
                intraday[t_str] = round(pnl_val * 100, 2)

            # Format exit time
            exit_time_str = result.exit_time
            if exit_time_str:
                exit_time_str = exit_time_str + " ET"

            trade_record = {
                "date": date,
                "ver": ver,
                "pnl": round(result.pnl_dollar),
                "qty": result.qty,
                "exit": result.exit_type.split("_")[0] if "_" in result.exit_type else result.exit_type,
                "fire_count": fc,
                "risk_budget": risk_budget,
                "pnl_ps": round(result.pnl_per_spread, 2),
                "is_win": result.pnl_per_spread > 0,
                "entry_time": entry_time,
                "exit_time": exit_time_str,
                "wing_width": int(wing_width),
                "entry_credit": round(result.entry_credit, 2),
                "vix": round(vix, 1),
                "vp_ratio": round(vp, 3) if vp else None,
                "prior_5d": round(ret5d, 3) if ret5d else None,
                "prior_dir": prior_dir_raw,
                "max_ps": round(result.peak_pnl * 100, 2),
                "min_ps": round(result.trough_pnl * 100, 2),
                "intraday": intraday,
                "fire_signals": signals,
                "sizing_score": sizing_score,
                "sizing_mult": sizing_mult,
            }
            new_trades.append(trade_record)
            log(f"    {ver}/{strat['name']}: {'WIN' if trade_record['is_win'] else 'LOSS'} ${trade_record['pnl']:,.0f}")

    return new_trades


def update_gap_cache(new_dates, universe):
    """
    Append gap % for new dates to spx_gap_cache.json.
    This ensures compute_stats.py can still work for legacy recalculation.
    """
    gap_path = os.path.join(_DIR, "spx_gap_cache.json")
    if os.path.exists(gap_path):
        with open(gap_path) as f:
            gaps = json.load(f)
    else:
        gaps = {}

    added = 0
    for date in new_dates:
        if date not in gaps:
            gap_pct = universe.ctx(date, 'gap_pct')
            if gap_pct is not None:
                gaps[date] = gap_pct
                added += 1

    if added:
        with open(gap_path, "w") as f:
            json.dump(gaps, f, indent=2)
        log(f"  Updated spx_gap_cache.json: +{added} dates")


def merge_and_write(new_trades):
    """
    Load existing strategy_trades.json, append new trades, rewrite.
    Also regenerate strategy_stats.json from the merged set.
    """
    # Load existing trades
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            existing = json.load(f)
    else:
        existing = []

    # Get existing date set per ver to avoid duplicates
    existing_keys = set()
    for t in existing:
        existing_keys.add((t.get("date"), t.get("ver")))

    added = 0
    for t in new_trades:
        key = (t["date"], t["ver"])
        if key not in existing_keys:
            existing.append(t)
            existing_keys.add(key)
            added += 1

    # Sort by date
    existing.sort(key=lambda x: (x.get("date", ""), x.get("ver", "")))

    # Write merged trades
    with open(TRADES_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    log(f"  strategy_trades.json: {len(existing)} total trades (+{added} new)")

    # Regenerate strategy_stats.json from merged trades
    regenerate_stats(existing)

    return added


def regenerate_stats(all_trades):
    """Regenerate strategy_stats.json from the full set of trades."""
    # Load existing stats to preserve N17/N18 data
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            all_stats = json.load(f)
    else:
        all_stats = {}

    # Group by ver
    by_ver = {}
    for t in all_trades:
        ver = t.get("ver")
        if ver not in by_ver:
            by_ver[ver] = []
        by_ver[ver].append(t)

    ver_names = {
        'v3': 'PHOENIX', 'n15': 'PHOENIX CLEAR', 'v6': 'QUIET REBOUND',
        'v7': 'FLAT-GAP FADE', 'v9': 'BREAKOUT STALL',
        'v12': 'BULL SQUEEZE',
    }

    for ver, trades in by_ver.items():
        if ver in ('n17', 'n18'):
            continue  # preserve existing real-fill stats

        pnls = [t.get("pnl", 0) for t in trades]
        wins = [t for t in trades if t.get("is_win")]
        losses = [t for t in trades if not t.get("is_win")]
        gross_wins = sum(t["pnl"] for t in wins) if wins else 0
        gross_losses = abs(sum(t["pnl"] for t in losses)) if losses else 0

        # Monthly P&L
        monthly = {}
        for t in trades:
            ym = t["date"][:7]
            monthly[ym] = monthly.get(ym, 0) + t.get("pnl", 0)

        # Streaks
        streak_w = streak_l = max_sw = max_sl = 0
        for t in trades:
            if t.get("is_win"):
                streak_w += 1
                streak_l = 0
                max_sw = max(max_sw, streak_w)
            else:
                streak_l += 1
                streak_w = 0
                max_sl = max(max_sl, streak_l)

        # Drawdown
        equity = peak = max_dd = 0
        for t in trades:
            equity += t.get("pnl", 0)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        # Exit counts
        exit_counts = {}
        exit_pnl = {}
        for t in trades:
            et = t.get("exit", "UNKNOWN")
            exit_counts[et] = exit_counts.get(et, 0) + 1
            exit_pnl[et] = exit_pnl.get(et, 0) + t.get("pnl", 0)

        # Fire count distribution (V3/N15)
        fc_dist = {}
        if ver in ('v3', 'n15'):
            for t in trades:
                fc = t.get("fire_count", 0) or 0
                if fc not in fc_dist:
                    fc_dist[fc] = {"count": 0, "pnl": 0, "wins": 0}
                fc_dist[fc]["count"] += 1
                fc_dist[fc]["pnl"] += t.get("pnl", 0)
                if t.get("is_win"):
                    fc_dist[fc]["wins"] += 1

        stats = {
            "ver": ver,
            "name": ver_names.get(ver, ver.upper()),
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(sum(pnls)),
            "avg_pnl": round(sum(pnls) / len(trades)) if trades else 0,
            "max_win": round(max(pnls)) if pnls else 0,
            "max_loss": round(min(pnls)) if pnls else 0,
            "median_pnl": round(float(np.median(pnls))) if pnls else 0,
            "gross_wins": round(gross_wins),
            "gross_losses": round(gross_losses),
            "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999,
            "avg_win": round(gross_wins / len(wins)) if wins else 0,
            "avg_loss": round(-gross_losses / len(losses)) if losses else 0,
            "exit_counts": exit_counts,
            "exit_pnl": {k: round(v) for k, v in exit_pnl.items()},
            "monthly_pnl": {k: round(v) for k, v in sorted(monthly.items())},
            "max_win_streak": max_sw,
            "max_loss_streak": max_sl,
            "max_drawdown": round(max_dd),
            "final_equity": round(equity),
        }
        if ver in ('v3', 'n15'):
            stats["fire_count_dist"] = {str(k): v for k, v in sorted(fc_dist.items())}

        all_stats[ver] = stats

    with open(STATS_FILE, "w") as f:
        json.dump(all_stats, f, indent=2)
    log(f"  strategy_stats.json: updated {len(all_stats)} strategy entries")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Import data pull functions from refresh_new_strategies.py
    import refresh_new_strategies as rns

    # Get API key
    if len(sys.argv) > 1:
        rns.API_KEY = sys.argv[1]
    else:
        rns.API_KEY = os.environ.get("POLYGON_API_KEY")
        if not rns.API_KEY:
            config_path = os.path.join(_DIR, "cockpit_config.json")
            if os.path.exists(config_path):
                with open(config_path) as f:
                    config = json.load(f)
                rns.API_KEY = config.get("polygon_api_key")

    if not rns.API_KEY or rns.API_KEY == "YOUR_KEY_HERE":
        log("ERROR: No Polygon API key")
        sys.exit(1)

    # Ensure data dirs exist
    for d in [DATA_DIR, SPX_DIR, OPT_DIR, VIX_DIR]:
        os.makedirs(d, exist_ok=True)

    # Step 1: Find last legacy trade date from strategy_trades.json
    legacy_vers = {'v3', 'n15', 'v6', 'v7', 'v9', 'v12'}
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            existing_trades = json.load(f)
        legacy_dates = set(t["date"] for t in existing_trades if t.get("ver") in legacy_vers)
        if legacy_dates:
            last_date = max(legacy_dates)
        else:
            last_date = "2023-02-01"
        log(f"Existing legacy trades: {len([t for t in existing_trades if t.get('ver') in legacy_vers])}, "
            f"last date: {last_date}")
    else:
        existing_trades = []
        last_date = "2023-02-01"
        log("No existing strategy_trades.json")

    # Step 2: Find new trading days
    start_dt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    start_str = start_dt.strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if start_str > yesterday:
        log("Legacy strategies already up to date")
        print(json.dumps({"ok": True, "new_trades": 0, "message": "Already up to date"}))
        return

    log(f"Checking for new trading days: {start_str} to {yesterday}...")
    new_trading_days = rns.get_trading_days(start_str, yesterday)

    if not new_trading_days:
        log("No new trading days found")
        print(json.dumps({"ok": True, "new_trades": 0, "message": "No new trading days"}))
        return

    # Filter out days already processed
    new_trading_days = [d for d in new_trading_days if d not in legacy_dates if d not in set()]
    if not new_trading_days:
        log("All trading days already processed")
        print(json.dumps({"ok": True, "new_trades": 0, "message": "All days processed"}))
        return

    log(f"Found {len(new_trading_days)} new trading days: "
        f"{new_trading_days[0]} to {new_trading_days[-1]}")

    # Step 3: Pull data for new days (reuses refresh_new_strategies.py data pull)
    # Data may already exist if refresh_new_strategies.py ran first — pull_one_day skips existing files
    log("\n=== PULLING MARKET DATA (legacy) ===")
    successful_days = []
    for date_str in new_trading_days:
        if rns.pull_one_day(date_str):
            successful_days.append(date_str)
        else:
            log(f"  SKIPPING {date_str} — data pull failed")

    if not successful_days:
        log("No days had successful data pulls")
        print(json.dumps({"ok": False, "error": "All data pulls failed"}))
        return

    log(f"\nSuccessfully pulled data for {len(successful_days)}/{len(new_trading_days)} days")

    # Step 4: Update daily/weekly bars
    log("\n=== UPDATING DAILY BARS ===")
    rns.update_daily_bars(successful_days)

    # Step 5: Run legacy strategies on new dates
    log("\n=== RUNNING LEGACY STRATEGIES ===")
    new_trades = run_legacy_on_dates(successful_days)

    log(f"\nGenerated {len(new_trades)} new legacy trades across {len(successful_days)} days")

    # Step 6: Update gap cache for future compute_stats.py compatibility
    from research.data import DataUniverse
    universe = DataUniverse()
    universe.load(load_quotes=False)
    update_gap_cache(successful_days, universe)

    # Step 7: Merge into strategy_trades.json + regenerate stats
    log("\n=== UPDATING STRATEGY FILES ===")
    added = merge_and_write(new_trades)

    log(f"\nDone: +{added} new legacy trades")
    print(json.dumps({
        "ok": True,
        "new_trades": added,
        "new_days": len(successful_days),
        "message": f"Added {added} legacy trades for {len(successful_days)} new days",
    }))


if __name__ == "__main__":
    main()
