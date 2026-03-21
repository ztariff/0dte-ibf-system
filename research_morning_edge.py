#!/usr/bin/env python3
"""
research_morning_edge.py — Deep dive into morning entry edge.

Questions to answer:
1. WHY do morning entries underperform? Is it the entries or the exits?
2. On which specific days DO mornings work? What's different about those days?
3. Is the morning range / first-hour character predictive?
4. Does a delayed conditional entry (enter only after conditions settle) help?
5. Is there a morning structure (wider wings, OTM condor) that captures edge
   that the standard IBF misses?
6. What if we enter morning but exit early (scalp first 30-60 min of theta)?
7. Same-day comparison: on days where afternoon works, does morning also work?

Writes to: morning_edge_results.txt
"""

import os, sys, json
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research.data import DataUniverse
from research.structures import (
    iron_butterfly, iron_condor, broken_wing_butterfly,
    price_entry,
)
from research.exits import (
    simulate_trade, standard_exits, profit_target, time_stop,
    wing_stop, loss_stop, trailing_stop, TIME_GRID_5MIN,
)
from research.stats import (
    calc_stats, fmt_stats, monthly_breakdown, day_of_week_breakdown,
    bootstrap_ci, half_split,
)
from research.sweep import (
    run_sweep, ibf_factory, ic_factory, bwb_gap_factory,
    build_pre_open_filters, build_intraday_filters,
    test_filter_combos, ResultsWriter,
)

_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(_DIR, "morning_edge_results.txt")

DATE_START = "2023-11-01"
DATE_END   = "2024-12-31"


def main():
    universe = DataUniverse()
    universe.load(load_quotes=False)
    all_dates = universe.trading_dates()
    dates = [d for d in all_dates if DATE_START <= d <= DATE_END]

    out = ResultsWriter(RESULTS_FILE)
    out.write("=" * 100)
    out.write("MORNING ENTRY DEEP DIVE — Where is the edge before noon?")
    out.write(f"Discovery window: {dates[0]} to {dates[-1]} ({len(dates)} days)")
    out.write("=" * 100)

    pre_filters = build_pre_open_filters(universe)
    intra_filters = build_intraday_filters(universe)

    # ─────────────────────────────────────────────────────────────────
    # SECTION 1: DIAGNOSE THE PROBLEM — Morning P&L decomposition
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 1: WHY DO MORNINGS UNDERPERFORM?")
    out.write("\nCompare identical structures at morning vs afternoon entry times.")
    out.write("Same wing width, same exit rules — only the entry time changes.\n")

    for ww in [40, 50, 60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w: Morning vs Afternoon (50% target, 15:30 stop, wing stop) ---")
        out.write(f"\n{'Entry':>8}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'PF':>7}  {'DD$':>9}  {'t':>6}  {'WS%':>6}  {'Tgt%':>6}  {'Time%':>6}")
        out.write("-" * 90)

        for et in ['09:35', '10:00', '10:30', '11:00', '12:00', '13:00', '13:30', '14:00', '15:00']:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
            )
            s = calc_stats(trades, label=f"IBF_{ww}w/{et}")
            if s and s['n'] >= 20:
                ws_pct = s['exit_counts'].get('WING_STOP', 0) / s['n'] * 100
                tgt_pct = s['exit_counts'].get('TARGET_50%', 0) / s['n'] * 100
                time_pct = s['exit_counts'].get('TIME_15:30', 0) / s['n'] * 100
                out.write(f"{et:>8}  {s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                         f"{s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  {s['t_stat']:>6.2f}  "
                         f"{ws_pct:>5.1f}%  {tgt_pct:>5.1f}%  {time_pct:>5.1f}%")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 2: MORNING SCALPS — Short hold periods
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 2: MORNING THETA SCALPS — Enter early, exit early")
    out.write("\nInstead of holding through the dangerous midday period,")
    out.write("what if we scalp the first 30-90 min of theta and get out?\n")

    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w: Morning scalps ---")
        out.write(f"\n{'Entry':>8}  {'Exit@':>6}  {'Hold':>6}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'PF':>7}  {'DD$':>9}  {'t':>6}  {'p':>8}")
        out.write("-" * 95)

        for et in ['09:35', '10:00', '10:30']:
            for exit_time in ['10:00', '10:15', '10:30', '10:45', '11:00', '11:30', '12:00']:
                if exit_time <= et:
                    continue
                # Compute hold duration
                et_min = int(et[:2]) * 60 + int(et[3:])
                ex_min = int(exit_time[:2]) * 60 + int(exit_time[3:])
                hold = ex_min - et_min

                trades = run_sweep(
                    universe, sfn, [et],
                    lambda ex=exit_time: [profit_target(0.30), wing_stop(), time_stop(ex)],
                    dates=dates, slippage=1.0,
                )
                s = calc_stats(trades, label=f"IBF_{ww}w/{et}/exit{exit_time}")
                if s and s['n'] >= 20:
                    out.write(f"{et:>8}  {exit_time:>6}  {hold:>4}m  {s['n']:>5}  {s['win_rate']:>5.1f}%  "
                             f"${s['avg_dollar']:>7.0f}  {s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  "
                             f"{s['t_stat']:>6.2f}  {s['p_val']:>8.4f}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 3: MORNING P&L BY REGIME — Which days have morning edge?
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 3: MORNING REGIME ANALYSIS — When do mornings work?")

    # Run morning trades (10:00 entry, 40w/60w/75w, standard exits)
    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w @ 10:00 with pre-open filters ---\n")

        base_trades = run_sweep(
            universe, sfn, ['10:00'],
            lambda: standard_exits(0.50, '15:30', True),
            dates=dates, slippage=1.0,
        )
        base_s = calc_stats(base_trades, label="UNFILTERED")
        out.write(f"  UNFILTERED: {fmt_stats(base_s)}\n")

        # Test all pre-open filters
        filter_results = test_filter_combos(base_trades, pre_filters, max_combo_size=2, min_n=10)

        out.write("  Top 20 single filters:")
        singles = [r for r in filter_results if ' + ' not in r['label']][:20]
        out.write_stats_table(singles)

        out.write("\n  Top 20 two-filter combos:")
        doubles = [r for r in filter_results if r['label'].count(' + ') == 1][:20]
        out.write_stats_table(doubles)

    # ─────────────────────────────────────────────────────────────────
    # SECTION 4: VIX REGIME × MORNING — Does VIX level matter more?
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 4: VIX REGIME × MORNING ENTRY")
    out.write("\nMornings have more exposure to VIX spikes. Does entering only in")
    out.write("low-VIX or falling-VIX regimes fix the morning problem?\n")

    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w @ 10:00 by VIX bucket ---")

        for vix_filter_name, vix_filter in [
            ("VIX<13", lambda d: (universe.ctx(d, 'vix_prior_close') or 99) < 13),
            ("VIX 13-15", lambda d: 13 <= (universe.ctx(d, 'vix_prior_close') or 0) < 15),
            ("VIX 15-18", lambda d: 15 <= (universe.ctx(d, 'vix_prior_close') or 0) < 18),
            ("VIX 18-22", lambda d: 18 <= (universe.ctx(d, 'vix_prior_close') or 0) < 22),
            ("VIX>22", lambda d: (universe.ctx(d, 'vix_prior_close') or 0) >= 22),
        ]:
            trades = run_sweep(
                universe, sfn, ['10:00'],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
                pre_filter=vix_filter,
            )
            s = calc_stats(trades, label=vix_filter_name)
            if s:
                out.write(f"  {vix_filter_name:>12}: {fmt_stats(s)}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 5: MORNING GAP ANALYSIS — Gap type × morning P&L
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 5: GAP TYPE × MORNING ENTRY")
    out.write("\nDoes the overnight gap predict morning vol-selling success?\n")

    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w @ 10:00 by gap type ---")

        for gap_name, gap_filter in [
            ("BigGapUp (>0.5%)", lambda d: (universe.ctx(d, 'gap_pct') or 0) > 0.5),
            ("SmallGapUp", lambda d: 0.15 < (universe.ctx(d, 'gap_pct') or 0) <= 0.5),
            ("Flat gap", lambda d: abs(universe.ctx(d, 'gap_pct') or 99) <= 0.15),
            ("SmallGapDn", lambda d: -0.5 <= (universe.ctx(d, 'gap_pct') or 0) < -0.15),
            ("BigGapDn (<-0.5%)", lambda d: (universe.ctx(d, 'gap_pct') or 0) < -0.5),
            ("GapIntoNew5dHi", lambda d: universe.ctx(d, 'gap_into_new_5d_high') is True),
            ("GapIntoNew5dLo", lambda d: universe.ctx(d, 'gap_into_new_5d_low') is True),
        ]:
            trades = run_sweep(
                universe, sfn, ['10:00'],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
                pre_filter=gap_filter,
            )
            s = calc_stats(trades, label=gap_name)
            if s and s['n'] >= 5:
                out.write(f"  {gap_name:>22}: {fmt_stats(s)}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 6: DAY OF WEEK × MORNING
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 6: DAY OF WEEK × MORNING ENTRY")

    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        trades = run_sweep(
            universe, sfn, ['10:00'],
            lambda: standard_exits(0.50, '15:30', True),
            dates=dates, slippage=1.0,
        )
        out.write(f"\n--- IBF_{ww}w @ 10:00 by day of week ---")
        dow = day_of_week_breakdown(trades)
        for d in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']:
            if d in dow and dow[d]:
                out.write(f"  {d}: {fmt_stats(dow[d])}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 7: PRIOR DAY CHARACTER × MORNING
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 7: PRIOR DAY CHARACTER × MORNING ENTRY")
    out.write("\nWhat happened yesterday matters for morning mean-reversion.\n")

    for ww in [60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w @ 10:00 by prior day character ---")

        for name, filt in [
            ("Prior DOJI", lambda d: universe.ctx(d, 'prior_day_candle') == 'DOJI'),
            ("Prior HAMMER", lambda d: universe.ctx(d, 'prior_day_candle') == 'HAMMER'),
            ("Prior MARUBOZU_BULL", lambda d: universe.ctx(d, 'prior_day_candle') == 'MARUBOZU_BULL'),
            ("Prior MARUBOZU_BEAR", lambda d: universe.ctx(d, 'prior_day_candle') == 'MARUBOZU_BEAR'),
            ("Prior ENGULF_BULL", lambda d: universe.ctx(d, 'prior_day_candle') == 'ENGULF_BULL'),
            ("Prior ENGULF_BEAR", lambda d: universe.ctx(d, 'prior_day_candle') == 'ENGULF_BEAR'),
            ("Prior BigRange+UP", lambda d: (universe.ctx(d, 'prior_day_range_pct') or 0) > 1.0 and universe.ctx(d, 'prior_day_direction') == 'UP'),
            ("Prior BigRange+DN", lambda d: (universe.ctx(d, 'prior_day_range_pct') or 0) > 1.0 and universe.ctx(d, 'prior_day_direction') == 'DOWN'),
            ("Prior SmallRange", lambda d: (universe.ctx(d, 'prior_day_range_pct') or 99) < 0.5),
            ("InsideDay", lambda d: universe.ctx(d, 'inside_day') is True),
            ("3+ ConsecUp", lambda d: (universe.ctx(d, 'consecutive_up_days') or 0) >= 3),
            ("3+ ConsecDn", lambda d: (universe.ctx(d, 'consecutive_down_days') or 0) >= 3),
        ]:
            trades = run_sweep(
                universe, sfn, ['10:00'],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
                pre_filter=filt,
            )
            s = calc_stats(trades, label=name)
            if s and s['n'] >= 5:
                out.write(f"  {name:>22}: {fmt_stats(s)}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 8: CONDITIONAL MORNING ENTRY — Wait for conditions
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 8: CONDITIONAL MORNING ENTRY")
    out.write("\nInstead of fixed 10:00 entry, enter at 10:30 only if")
    out.write("certain intraday conditions are met by then.\n")

    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w @ 10:30 with intraday conditions ---")

        # Baseline at 10:30 unfiltered
        base = run_sweep(universe, sfn, ['10:30'],
                        lambda: standard_exits(0.50, '15:30', True),
                        dates=dates, slippage=1.0)
        bs = calc_stats(base, label="Unfiltered 10:30")
        out.write(f"  {'Unfiltered':>25}: {fmt_stats(bs)}")

        for ifname, iffn in [
            ("MornRng<10", lambda d, t: (universe.morning_range(d, t) or 99) < 10),
            ("MornRng<15", lambda d, t: (universe.morning_range(d, t) or 99) < 15),
            ("MornFlat (±5pts)", lambda d, t: abs(universe.morning_direction(d, t) or 999) < 5),
            ("SlowVelocity", lambda d, t: (universe.spx_abs_velocity(d, t, 15) or 99) < 0.3),
            ("Consolidating", lambda d, t: universe.is_consolidating(d, t, 30, 0.3) is True),
            ("Decelerating", lambda d, t: (universe.spx_acceleration(d, t, 10) or 0) < -0.05),
            ("VIX_intra_dn", lambda d, t: (universe.vix_change_since_open(d, t) or 0) < -0.5),
            ("GapFilled", lambda d, t: universe.gap_filled(d, t) is True),
            ("GapNotFilled", lambda d, t: universe.gap_filled(d, t) is False),
            ("LowIntraVol", lambda d, t: (universe.spx_bar_range_avg(d, t, 15) or 99) < 1.5),
        ]:
            trades = run_sweep(
                universe, sfn, ['10:30'],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
                intra_filter=iffn,
            )
            s = calc_stats(trades, label=ifname)
            if s and s['n'] >= 10:
                out.write(f"  {ifname:>25}: {fmt_stats(s)}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 9: OTM CONDORS IN THE MORNING
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 9: OTM IRON CONDORS — MORNING ENTRIES")
    out.write("\nThe ATM butterfly is maximally exposed to the first SPX move.")
    out.write("OTM condors give the market room to move. Do they work better in AM?\n")

    for short_off, wing_w in [(10, 20), (15, 20), (20, 20), (10, 30), (15, 30), (20, 30)]:
        sfn = ic_factory(short_off, wing_w)
        name = f"IC_{short_off}otm_{wing_w}w"
        out.write(f"\n--- {name} ---")
        out.write(f"\n{'Entry':>8}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'PF':>7}  {'DD$':>9}  {'t':>6}")
        out.write("-" * 65)

        for et in ['09:35', '10:00', '10:30', '11:00', '13:00', '14:00']:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
            )
            s = calc_stats(trades, label=f"{name}/{et}")
            if s and s['n'] >= 20:
                out.write(f"{et:>8}  {s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                         f"{s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  {s['t_stat']:>6.2f}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 10: SAME-DAY COMPARISON — Morning vs Afternoon
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 10: SAME-DAY HEAD-TO-HEAD — Morning vs Afternoon")
    out.write("\nOn each day, compare the 10:00 IBF vs the 14:00 IBF.")
    out.write("Do they win/lose on the same days, or different?\n")

    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)

        am_trades = run_sweep(universe, sfn, ['10:00'],
                             lambda: standard_exits(0.50, '15:30', True),
                             dates=dates, slippage=1.0)
        pm_trades = run_sweep(universe, sfn, ['14:00'],
                             lambda: standard_exits(0.50, '15:30', True),
                             dates=dates, slippage=1.0)

        am_by_date = {t.date: t for t in am_trades}
        pm_by_date = {t.date: t for t in pm_trades}
        common = sorted(set(am_by_date.keys()) & set(pm_by_date.keys()))

        both_win = 0
        both_lose = 0
        am_win_pm_lose = 0
        am_lose_pm_win = 0

        am_pnls_common = []
        pm_pnls_common = []

        for d in common:
            am_w = am_by_date[d].pnl_dollar > 0
            pm_w = pm_by_date[d].pnl_dollar > 0
            if am_w and pm_w: both_win += 1
            elif not am_w and not pm_w: both_lose += 1
            elif am_w and not pm_w: am_win_pm_lose += 1
            else: am_lose_pm_win += 1
            am_pnls_common.append(am_by_date[d].pnl_dollar)
            pm_pnls_common.append(pm_by_date[d].pnl_dollar)

        out.write(f"\n--- IBF_{ww}w: 10:00 vs 14:00 ({len(common)} common days) ---")
        out.write(f"  Both win:         {both_win:>4} ({both_win/len(common)*100:.1f}%)")
        out.write(f"  Both lose:        {both_lose:>4} ({both_lose/len(common)*100:.1f}%)")
        out.write(f"  AM wins, PM loses:{am_win_pm_lose:>4} ({am_win_pm_lose/len(common)*100:.1f}%)")
        out.write(f"  AM loses, PM wins:{am_lose_pm_win:>4} ({am_lose_pm_win/len(common)*100:.1f}%)")

        # Correlation
        if len(am_pnls_common) >= 10:
            import math
            n = len(am_pnls_common)
            mx = sum(am_pnls_common) / n
            my = sum(pm_pnls_common) / n
            num = sum((x - mx) * (y - my) for x, y in zip(am_pnls_common, pm_pnls_common))
            dx = math.sqrt(sum((x - mx) ** 2 for x in am_pnls_common))
            dy = math.sqrt(sum((y - my) ** 2 for y in pm_pnls_common))
            corr = num / (dx * dy) if dx > 0 and dy > 0 else 0
            out.write(f"  P&L correlation:  {corr:.3f}")
            out.write(f"  AM avg on common: ${sum(am_pnls_common)/n:.0f}")
            out.write(f"  PM avg on common: ${sum(pm_pnls_common)/n:.0f}")

        # Days where AM wins but PM loses — what's special?
        am_only_days = [d for d in common if am_by_date[d].pnl_dollar > 0 and pm_by_date[d].pnl_dollar <= 0]
        if am_only_days:
            out.write(f"\n  Days AM wins / PM loses ({len(am_only_days)} days):")
            # Look at characteristics
            vix_vals = [universe.ctx(d, 'vix_prior_close') for d in am_only_days if universe.ctx(d, 'vix_prior_close')]
            gap_vals = [universe.ctx(d, 'gap_pct') for d in am_only_days if universe.ctx(d, 'gap_pct') is not None]
            dirs = [universe.ctx(d, 'prior_day_direction') for d in am_only_days]
            from collections import Counter
            if vix_vals:
                out.write(f"    Avg VIX: {sum(vix_vals)/len(vix_vals):.1f}")
            if gap_vals:
                out.write(f"    Avg gap: {sum(gap_vals)/len(gap_vals):.3f}%")
            out.write(f"    Prior day: {dict(Counter(dirs))}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 11: WEEKLY CONTEXT × MORNING
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 11: WEEKLY CONTEXT × MORNING")

    for ww in [60, 75]:
        sfn = ibf_factory(ww)
        out.write(f"\n--- IBF_{ww}w @ 10:00 by weekly context ---")

        for name, filt in [
            ("InWeekRange", lambda d: universe.ctx(d, 'in_prior_week_range') is True),
            ("OutWeekRange", lambda d: universe.ctx(d, 'in_prior_week_range') is False),
            ("WeekUP", lambda d: universe.ctx(d, 'prior_week_direction') == 'UP'),
            ("WeekDOWN", lambda d: universe.ctx(d, 'prior_week_direction') == 'DOWN'),
            ("InsideWeek", lambda d: universe.ctx(d, 'inside_week') is True),
            ("OPEXweek", lambda d: universe.ctx(d, 'is_opex_week') is True),
            ("NotOPEXweek", lambda d: universe.ctx(d, 'is_opex_week') is not True),
            ("OPEXday", lambda d: universe.ctx(d, 'is_opex_day') is True),
            ("OPEX_near3", lambda d: (universe.ctx(d, 'days_to_next_opex') or 99) <= 3),
            ("MonthEnd", lambda d: universe.ctx(d, 'expiry_type') == 'MONTH_END'),
            ("MonthStart", lambda d: universe.ctx(d, 'expiry_type') == 'MONTH_START'),
        ]:
            trades = run_sweep(
                universe, sfn, ['10:00'],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
                pre_filter=filt,
            )
            s = calc_stats(trades, label=name)
            if s and s['n'] >= 5:
                out.write(f"  {name:>18}: {fmt_stats(s)}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 12: WALK-FORWARD ON BEST MORNING CONFIGS
    # ─────────────────────────────────────────────────────────────────
    out.write_header("SECTION 12: WALK-FORWARD — Best morning configs")

    # Gather the best morning configs and validate
    morning_configs = []
    for ww in [40, 60, 75]:
        sfn = ibf_factory(ww)
        for et in ['09:35', '10:00', '10:30']:
            # Test with various pre-open filters
            for fname, ffunc in [
                ("HighATR", pre_filters.get('HighATR')),
                ("BigPriorRange", pre_filters.get('BigPriorRange')),
                ("5dRet<-1", pre_filters.get('5dRet<-1')),
                ("PriorDOWN", pre_filters.get('PriorDOWN')),
                ("VIX<15", pre_filters.get('VIX<15')),
                ("InsideDay", pre_filters.get('InsideDay')),
            ]:
                if not ffunc:
                    continue
                trades = run_sweep(
                    universe, sfn, [et],
                    lambda: standard_exits(0.50, '15:30', True),
                    dates=dates, slippage=1.0,
                    pre_filter=ffunc,
                )
                s = calc_stats(trades, label=f"IBF_{ww}w/{et}/{fname}")
                if s and s['n'] >= 15 and s['avg_dollar'] > 50 and s['p_val'] < 0.05:
                    morning_configs.append((s, trades))

    morning_configs.sort(key=lambda x: x[0]['avg_dollar'], reverse=True)

    out.write(f"\nTop morning configs with p<0.05 and avg>$50 ({len(morning_configs)} found):\n")
    for s, trades in morning_configs[:15]:
        h1, h2 = half_split(trades)
        s1 = calc_stats(h1)
        s2 = calc_stats(h2)
        stable = "YES" if s1 and s2 and s1['avg_dollar'] > 0 and s2['avg_dollar'] > 0 else "NO"
        out.write(f"  {s['label']:50s}  {fmt_stats(s)}  H1/H2={stable}")

    # ─── SAVE ───────────────────────────────────────────────────────
    out.save()
    print(f"\nDone. Results in: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
