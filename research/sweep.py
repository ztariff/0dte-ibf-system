"""
research/sweep.py — Sweep harness for systematic edge discovery.

Orchestrates:
  - Structure × entry time × exit mechanic sweeps
  - Regime filter testing (single, 2-combo, 3-combo)
  - Walk-forward validation of top strategies
  - Gap/daily/weekly context analysis
  - Results writing to file (per CLAUDE.md: scripts must write output)
"""

import os
import json
from typing import List, Dict, Callable, Optional, Tuple
from collections import defaultdict
from datetime import datetime
from itertools import combinations

from research.data import DataUniverse
from research.structures import (
    Structure, PricedPosition, iron_butterfly, iron_condor,
    broken_wing_butterfly, bear_call_spread, bull_put_spread,
    price_entry,
)
from research.exits import (
    TradeResult, ExitRule, simulate_trade, standard_exits,
    aggressive_exits, trailing_exits, profit_target, time_stop,
    wing_stop, loss_stop, trailing_stop, time_decay_target,
    TIME_GRID_5MIN,
)
from research.stats import (
    calc_stats, fmt_stats, monthly_breakdown, yearly_breakdown,
    day_of_week_breakdown, bootstrap_ci, walk_forward_split,
    half_split, bonferroni_threshold, holm_bonferroni,
    daily_pnl_correlation, overlap_analysis,
)


# ─────────────────────────────────────────────────────────────────────────────
# FILTER DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

def build_pre_open_filters(universe: DataUniverse) -> Dict[str, Callable]:
    """
    Build a library of pre-open filters.
    Each filter takes a date string and returns True/False.
    All use only data available before 9:30 (daily_context).
    """
    def _ctx(date, field, default=None):
        return universe.ctx(date, field, default)

    return {
        # VIX regime
        'VIX<13':        lambda d: (_ctx(d, 'vix_prior_close') or 99) < 13,
        'VIX<15':        lambda d: (_ctx(d, 'vix_prior_close') or 99) < 15,
        'VIX<18':        lambda d: (_ctx(d, 'vix_prior_close') or 99) < 18,
        'VIX<20':        lambda d: (_ctx(d, 'vix_prior_close') or 99) < 20,
        'VIX 15-20':     lambda d: 15 <= (_ctx(d, 'vix_prior_close') or 0) < 20,
        'VIX 20-25':     lambda d: 20 <= (_ctx(d, 'vix_prior_close') or 0) < 25,
        'VIX>25':        lambda d: (_ctx(d, 'vix_prior_close') or 0) >= 25,
        'VIX_pct<25':    lambda d: (_ctx(d, 'vix_percentile_60d') or 99) < 25,
        'VIX_pct>75':    lambda d: (_ctx(d, 'vix_percentile_60d') or 0) > 75,

        # Prior day
        'PriorUP':       lambda d: _ctx(d, 'prior_day_direction') == 'UP',
        'PriorDOWN':     lambda d: _ctx(d, 'prior_day_direction') == 'DOWN',
        'PriorFLAT':     lambda d: _ctx(d, 'prior_day_direction') == 'FLAT',
        'InsideDay':     lambda d: _ctx(d, 'inside_day') is True,
        'BigPriorRange': lambda d: (_ctx(d, 'prior_day_range_pct') or 0) > 1.0,
        'SmallPriorRng': lambda d: (_ctx(d, 'prior_day_range_pct') or 99) < 0.5,
        'DOJI':          lambda d: _ctx(d, 'prior_day_candle') == 'DOJI',
        'HAMMER':        lambda d: _ctx(d, 'prior_day_candle') == 'HAMMER',
        'MARUBOZU':      lambda d: _ctx(d, 'prior_day_candle') in ('MARUBOZU_BULL', 'MARUBOZU_BEAR'),
        'ENGULF':        lambda d: _ctx(d, 'prior_day_candle') in ('ENGULF_BULL', 'ENGULF_BEAR'),

        # Multi-day momentum
        '5dRet>0':       lambda d: (_ctx(d, 'prior_5d_return') or -99) > 0,
        '5dRet>1':       lambda d: (_ctx(d, 'prior_5d_return') or -99) > 1,
        '5dRet<0':       lambda d: (_ctx(d, 'prior_5d_return') or 99) < 0,
        '5dRet<-1':      lambda d: (_ctx(d, 'prior_5d_return') or 99) < -1,
        '10dRet>0':      lambda d: (_ctx(d, 'prior_10d_return') or -99) > 0,
        '20dRet>0':      lambda d: (_ctx(d, 'prior_20d_return') or -99) > 0,
        'ConsecUp>=3':   lambda d: (_ctx(d, 'consecutive_up_days') or 0) >= 3,
        'ConsecDn>=3':   lambda d: (_ctx(d, 'consecutive_down_days') or 0) >= 3,

        # Distance from extremes
        'Near5dHi':      lambda d: abs(_ctx(d, 'dist_from_5d_high_pct') or -99) < 0.3,
        'Near20dHi':     lambda d: abs(_ctx(d, 'dist_from_20d_high_pct') or -99) < 0.5,
        'Near5dLo':      lambda d: abs(_ctx(d, 'dist_from_5d_low_pct') or 99) < 0.3,

        # Gap
        'GapUP':         lambda d: _ctx(d, 'gap_direction') == 'GUP',
        'GapDN':         lambda d: _ctx(d, 'gap_direction') == 'GDN',
        'GapFL':         lambda d: _ctx(d, 'gap_direction') == 'GFL',
        'BigGap':        lambda d: abs(_ctx(d, 'gap_pct') or 0) > 0.5,
        'SmallGap':      lambda d: abs(_ctx(d, 'gap_pct') or 99) < 0.15,
        'GapNewHi':      lambda d: _ctx(d, 'gap_into_new_5d_high') is True,
        'GapNewLo':      lambda d: _ctx(d, 'gap_into_new_5d_low') is True,
        'GapBig_vs_rng': lambda d: (_ctx(d, 'gap_vs_prior_range') or 0) > 0.5,

        # Weekly
        'InWeekRange':   lambda d: _ctx(d, 'in_prior_week_range') is True,
        'OutWeekRange':  lambda d: _ctx(d, 'in_prior_week_range') is False,
        'WeekUP':        lambda d: _ctx(d, 'prior_week_direction') == 'UP',
        'WeekDOWN':      lambda d: _ctx(d, 'prior_week_direction') == 'DOWN',
        'InsideWeek':    lambda d: _ctx(d, 'inside_week') is True,
        'WkConsecUp>=2': lambda d: (_ctx(d, 'weekly_consecutive_up') or 0) >= 2,
        'WkConsecDn>=2': lambda d: (_ctx(d, 'weekly_consecutive_down') or 0) >= 2,

        # Volatility context
        'VIX_rising':    lambda d: (_ctx(d, 'vix_1d_change') or 0) > 1,
        'VIX_falling':   lambda d: (_ctx(d, 'vix_1d_change') or 0) < -1,
        'VIX_below_avg': lambda d: (_ctx(d, 'vix_prior_close') or 99) < (_ctx(d, 'vix_20d_avg') or 99),
        'LowATR':        lambda d: (_ctx(d, 'atr_5') or 999) < (_ctx(d, 'atr_20') or 1) * 0.8,
        'HighATR':       lambda d: (_ctx(d, 'atr_5') or 0) > (_ctx(d, 'atr_20') or 999) * 1.2,
        'Contango':      lambda d: (_ctx(d, 'vix9d_vix_ratio') or 99) < 1.0,
        'Backwardation': lambda d: (_ctx(d, 'vix9d_vix_ratio') or 0) >= 1.0,

        # Expiry calendar
        'QuadWitch':     lambda d: _ctx(d, 'is_quad_witch') is True,
        'OPEXday':       lambda d: _ctx(d, 'is_opex_day') is True,
        'NotOPEXday':    lambda d: _ctx(d, 'is_opex_day') is not True,
        'OPEXweek':      lambda d: _ctx(d, 'is_opex_week') is True,
        'NotOPEXweek':   lambda d: _ctx(d, 'is_opex_week') is not True,
        'PreOPEX':       lambda d: _ctx(d, 'expiry_type') == 'PRE_OPEX',
        'PostOPEX':      lambda d: _ctx(d, 'expiry_type') == 'POST_OPEX',
        'MonthEnd':      lambda d: _ctx(d, 'expiry_type') == 'MONTH_END',
        'MonthStart':    lambda d: _ctx(d, 'expiry_type') == 'MONTH_START',
        'OPEX_near3':    lambda d: (_ctx(d, 'days_to_next_opex') or 99) <= 3,
        'OPEX_far':      lambda d: (_ctx(d, 'days_to_next_opex') or 0) >= 10,
    }


def build_intraday_filters(universe: DataUniverse) -> Dict[str, Callable]:
    """
    Build filters that use intraday data up to entry time.
    Each filter takes (date, as_of_time) and returns True/False.
    Forward-walk safe: only uses data at or before as_of_time.
    """
    return {
        # Morning range
        'MornRng<10':    lambda d, t: (universe.morning_range(d, t) or 99) < 10,
        'MornRng<15':    lambda d, t: (universe.morning_range(d, t) or 99) < 15,
        'MornRng<20':    lambda d, t: (universe.morning_range(d, t) or 99) < 20,
        'MornRng>20':    lambda d, t: (universe.morning_range(d, t) or 0) > 20,

        # Morning direction
        'MornUp':        lambda d, t: (universe.morning_direction(d, t) or 0) > 5,
        'MornDn':        lambda d, t: (universe.morning_direction(d, t) or 0) < -5,
        'MornFlat':      lambda d, t: abs(universe.morning_direction(d, t) or 999) < 5,

        # VIX intraday
        'VIX_intra_dn':  lambda d, t: (universe.vix_change_since_open(d, t) or 0) < -0.5,
        'VIX_intra_up':  lambda d, t: (universe.vix_change_since_open(d, t) or 0) > 0.5,

        # Gap fill
        'GapFilled':     lambda d, t: universe.gap_filled(d, t) is True,
        'GapNotFilled':  lambda d, t: universe.gap_filled(d, t) is False,

        # Velocity — how fast is SPX moving right now?
        'SlowVelocity':  lambda d, t: (universe.spx_abs_velocity(d, t, 15) or 99) < 0.3,
        'FastVelocity':  lambda d, t: (universe.spx_abs_velocity(d, t, 15) or 0) > 0.8,
        'VelUp':         lambda d, t: (universe.spx_velocity(d, t, 15) or 0) > 0.3,
        'VelDn':         lambda d, t: (universe.spx_velocity(d, t, 15) or 0) < -0.3,
        'VelFlat':       lambda d, t: abs(universe.spx_velocity(d, t, 15) or 999) < 0.15,

        # Acceleration — is the move speeding up or slowing down?
        'Decelerating':  lambda d, t: (universe.spx_acceleration(d, t, 10) or 0) < -0.05,
        'Accelerating':  lambda d, t: (universe.spx_acceleration(d, t, 10) or 0) > 0.05,

        # Range expansion — is the day's range still growing?
        'RngStable':     lambda d, t: (universe.spx_range_velocity(d, t, 30) or 1) < 0.4,
        'RngExpanding':  lambda d, t: (universe.spx_range_velocity(d, t, 30) or 0) > 0.6,

        # Trend vs consolidation
        'Trending':      lambda d, t: universe.is_trending(d, t, 30, 0.7) is True,
        'Consolidating': lambda d, t: universe.is_consolidating(d, t, 30, 0.3) is True,

        # Intraday realized vol (bar range as proxy)
        'LowIntraVol':   lambda d, t: (universe.spx_bar_range_avg(d, t, 15) or 99) < 1.5,
        'HighIntraVol':  lambda d, t: (universe.spx_bar_range_avg(d, t, 15) or 0) > 3.0,

        # Center pinning — SPX stuck near ATM (from other AI's research)
        'Pinned_10_60':  lambda d, t: universe.is_center_pinned(d, t, 30, 10, 0.6) is True,
        'Pinned_10_70':  lambda d, t: universe.is_center_pinned(d, t, 30, 10, 0.7) is True,
        'Pinned_10_80':  lambda d, t: universe.is_center_pinned(d, t, 30, 10, 0.8) is True,
        'Pinned_15_60':  lambda d, t: universe.is_center_pinned(d, t, 30, 15, 0.6) is True,
        'Pinned_15_70':  lambda d, t: universe.is_center_pinned(d, t, 30, 15, 0.7) is True,
        'Pinned_20_60':  lambda d, t: universe.is_center_pinned(d, t, 30, 20, 0.6) is True,
        'NotPinned_10':  lambda d, t: universe.is_center_pinned(d, t, 30, 10, 0.6) is False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SWEEP ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def run_sweep(universe: DataUniverse,
              structure_fn: Callable,      # (date, time, universe) -> Structure or None
              entry_times: List[str],
              exit_rules_fn: Callable,     # () -> List[ExitRule]
              dates: List[str] = None,
              pre_filter: Callable = None, # (date) -> bool
              intra_filter: Callable = None,  # (date, time) -> bool
              risk_budget: float = None,
              slippage: float = 1.0,
              label: str = "") -> List[TradeResult]:
    """
    Run a sweep across all dates and entry times.

    Args:
        structure_fn: function that builds a Structure for each date/time/universe
        entry_times: list of "HH:MM" entry times to test
        exit_rules_fn: function returning exit rules
        dates: dates to process (default: all)
        pre_filter: pre-open filter (date -> bool)
        intra_filter: intraday filter (date, time -> bool)
        risk_budget: position sizing budget
        slippage: per-spread slippage in dollars

    Returns:
        List of TradeResult for all successful trades
    """
    if dates is None:
        dates = universe.trading_dates()

    results = []
    skipped_filter = 0
    skipped_price = 0
    skipped_sim = 0

    for date in dates:
        # Pre-open filter (forward-walk safe: uses only pre-open data)
        if pre_filter and not pre_filter(date):
            skipped_filter += 1
            continue

        for entry_time in entry_times:
            # Intraday filter (forward-walk safe: uses data up to entry_time)
            if intra_filter and not intra_filter(date, entry_time):
                skipped_filter += 1
                continue

            # Build structure at this date/time
            structure = structure_fn(date, entry_time, universe)
            if structure is None:
                skipped_price += 1
                continue

            # Price entry
            position = price_entry(universe, date, entry_time, structure,
                                   risk_budget=risk_budget)
            if position is None:
                skipped_price += 1
                continue

            # Simulate trade
            exit_rules = exit_rules_fn()
            result = simulate_trade(universe, position, exit_rules,
                                    slippage_per_spread=slippage)
            if result is None:
                skipped_sim += 1
                continue

            results.append(result)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURE FACTORY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def ibf_factory(wing_width: int):
    """Returns a structure_fn that builds an ATM IBF with given wing width."""
    def build(date, time, universe):
        atm = universe.current_atm(date, time)
        if atm is None:
            return None
        return iron_butterfly(atm, wing_width)
    return build


def ic_factory(short_offset: int, wing_width: int):
    """Returns a structure_fn for an OTM iron condor."""
    def build(date, time, universe):
        atm = universe.current_atm(date, time)
        if atm is None:
            return None
        return iron_condor(atm, short_offset, wing_width)
    return build


def bwb_gap_factory(base_wing: int, wide_extra: int):
    """
    Broken-wing butterfly that widens against the gap direction.
    Gap up → widen put wing (protect downside retracement)
    Gap down → widen call wing (protect upside bounce)
    Flat → symmetric
    """
    def build(date, time, universe):
        atm = universe.current_atm(date, time)
        if atm is None:
            return None
        gap_dir = universe.ctx(date, 'gap_direction')
        if gap_dir == 'GUP':
            return broken_wing_butterfly(atm, base_wing, base_wing + wide_extra)
        elif gap_dir == 'GDN':
            return broken_wing_butterfly(atm, base_wing + wide_extra, base_wing)
        else:
            return iron_butterfly(atm, base_wing)
    return build


# ─────────────────────────────────────────────────────────────────────────────
# FILTER COMBO TESTING
# ─────────────────────────────────────────────────────────────────────────────

def test_filter_combos(trades: List[TradeResult],
                       filters: Dict[str, Callable],
                       max_combo_size: int = 3,
                       min_n: int = 10) -> List[Dict]:
    """
    Test all 1, 2, and 3-filter combinations against a set of trades.
    Returns ranked results sorted by average P&L.
    """
    # Build date-indexed trade lookup
    trade_by_date = {t.date: t for t in trades}
    all_dates = list(trade_by_date.keys())

    results = []

    # Single filters
    for fname, ffunc in filters.items():
        matching = [trade_by_date[d] for d in all_dates if ffunc(d)]
        if len(matching) >= min_n:
            s = calc_stats(matching, label=fname)
            if s:
                results.append(s)

    # 2-combos
    if max_combo_size >= 2:
        fnames = list(filters.keys())
        for f1, f2 in combinations(fnames, 2):
            matching = [trade_by_date[d] for d in all_dates
                        if filters[f1](d) and filters[f2](d)]
            if len(matching) >= min_n:
                s = calc_stats(matching, label=f"{f1} + {f2}")
                if s:
                    results.append(s)

    # 3-combos
    if max_combo_size >= 3:
        for f1, f2, f3 in combinations(fnames, 3):
            matching = [trade_by_date[d] for d in all_dates
                        if filters[f1](d) and filters[f2](d) and filters[f3](d)]
            if len(matching) >= min_n:
                s = calc_stats(matching, label=f"{f1} + {f2} + {f3}")
                if s:
                    results.append(s)

    # Sort by average dollar P&L
    results.sort(key=lambda x: x['avg_dollar'], reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

class ResultsWriter:
    """
    Write results to a file incrementally.
    Per CLAUDE.md: scripts must write output to a shared file.
    """

    def __init__(self, filepath):
        self.filepath = filepath
        self.lines = []

    def write(self, s=""):
        self.lines.append(s)
        print(s)

    def write_header(self, title):
        self.write("\n" + "=" * 100)
        self.write(title)
        self.write("=" * 100)

    def write_stats_table(self, stats_list: List[Dict], headers=None):
        if not stats_list:
            self.write("  (no results)")
            return
        if headers is None:
            headers = ['Label', 'N', 'WR%', 'Avg$', 'Total$', 'PF', 'DD$', 't', 'p']
        widths = [40, 6, 7, 10, 12, 7, 10, 7, 8]
        header = "".join(h.ljust(w) for h, w in zip(headers, widths))
        self.write(header)
        self.write("-" * len(header))
        for s in stats_list:
            row = [
                s.get('label', '')[:38],
                str(s['n']),
                f"{s['win_rate']}%",
                f"${s['avg_dollar']:.0f}",
                f"${s['total_dollar']:,.0f}",
                f"{s['pf']:.2f}",
                f"${s['max_dd_dollar']:,.0f}",
                f"{s['t_stat']:.2f}",
                f"{s['p_val']:.4f}",
            ]
            self.write("".join(str(r).ljust(w) for r, w in zip(row, widths)))

    def save(self):
        with open(self.filepath, 'w') as f:
            f.write("\n".join(self.lines))
        print(f"\nResults saved to {self.filepath} ({len(self.lines)} lines)")
