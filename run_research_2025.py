#!/usr/bin/env python3
"""
run_research_2025.py — BLIND validation on 2025 data.

This runs the IDENTICAL analysis framework as run_research_2024.py
on a completely independent time period. No parameters were tuned
for 2025 — the same structures, filters, entry times, and exit
mechanics are tested.

Validation period: Jan 2025 – Dec 2025 (~250 trading days)
Discovery period:  Nov 2023 – Dec 2024 (already analyzed separately)

Edges that appear in BOTH periods independently are the strongest
candidates for real, tradeable strategies.

Writes all output to: research_results_2025.txt
"""

import os
import sys
import json
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research.data import DataUniverse
from research.structures import (
    iron_butterfly, iron_condor, broken_wing_butterfly,
    bear_call_spread, bull_put_spread, price_entry,
)
from research.exits import (
    simulate_trade, standard_exits, aggressive_exits, trailing_exits,
    profit_target, time_stop, wing_stop, loss_stop, trailing_stop,
    time_decay_target, TIME_GRID_5MIN,
)
from research.stats import (
    calc_stats, fmt_stats, monthly_breakdown, yearly_breakdown,
    day_of_week_breakdown, bootstrap_ci, walk_forward_split,
    half_split, holm_bonferroni, daily_pnl_correlation,
    overlap_analysis,
)
from research.sweep import (
    run_sweep, ibf_factory, ic_factory, bwb_gap_factory,
    build_pre_open_filters, build_intraday_filters,
    test_filter_combos, ResultsWriter,
)

_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(_DIR, "research_results_2025.txt")

# Date window: blind validation period
DATE_START = "2025-01-01"
DATE_END   = "2025-12-31"


def main():
    # ─── LOAD DATA ──────────────────────────────────────────────────────
    universe = DataUniverse()
    universe.load(load_quotes=True)
    all_dates = universe.trading_dates()

    # Filter to discovery period
    dates = [d for d in all_dates if DATE_START <= d <= DATE_END]

    if not dates:
        print("ERROR: No trading dates in the 2023-11 to 2024-12 window.")
        print(f"Available dates: {len(all_dates)} ({all_dates[0] if all_dates else 'none'} to {all_dates[-1] if all_dates else 'none'})")
        print("Has pull_comprehensive_data.py reached Dec 2024 yet? Check data/pull_log.json.")
        sys.exit(1)

    out = ResultsWriter(RESULTS_FILE)
    out.write("=" * 100)
    out.write(f"SPX 0DTE VOL-SELLING EDGE RESEARCH — 2025 BLIND VALIDATION")
    out.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out.write(f"Validation window: {DATE_START} to {DATE_END}")
    out.write(f"Dates with complete data: {len(dates)} ({dates[0]} to {dates[-1]})")
    out.write(f"Total dates in universe: {len(all_dates)}")
    out.write(f"NOTE: Identical analysis framework as 2024 discovery run. No parameters tuned for 2025.")
    out.write("=" * 100)

    if len(dates) < 200:
        out.write(f"\nWARNING: Only {len(dates)} dates available (expected ~250).")
        out.write(f"Data pull may still be in progress. Results are preliminary.")
        out.write(f"Last available date: {dates[-1]}\n")

    # Track ALL p-values for final multiple comparison correction
    all_pvalues = []

    # ─── PHASE 1: STRUCTURE UNIVERSE ────────────────────────────────────
    out.write_header("PHASE 1: STRUCTURE UNIVERSE — Every structure × entry time baseline")

    # Entry times to test (every 30 min from 9:35 to 15:30)
    entry_times_coarse = ['09:35', '10:00', '10:30', '11:00', '11:30',
                          '12:00', '12:30', '13:00', '13:30', '14:00',
                          '14:30', '15:00', '15:15', '15:30']

    structures_to_test = {
        # ATM Iron Butterflies
        'IBF_25w': ibf_factory(25),
        'IBF_30w': ibf_factory(30),
        'IBF_35w': ibf_factory(35),
        'IBF_40w': ibf_factory(40),
        'IBF_50w': ibf_factory(50),
        'IBF_60w': ibf_factory(60),
        'IBF_75w': ibf_factory(75),

        # OTM Iron Condors (10pt OTM, various wing widths)
        'IC_10otm_15w': ic_factory(10, 15),
        'IC_10otm_20w': ic_factory(10, 20),
        'IC_10otm_30w': ic_factory(10, 30),
        'IC_15otm_15w': ic_factory(15, 15),
        'IC_15otm_20w': ic_factory(15, 20),
        'IC_20otm_15w': ic_factory(20, 15),
        'IC_20otm_20w': ic_factory(20, 20),
        'IC_25otm_15w': ic_factory(25, 15),

        # Broken-wing butterflies (gap-aware)
        'BWB_gap_30_10': bwb_gap_factory(30, 10),
        'BWB_gap_40_15': bwb_gap_factory(40, 15),
    }

    exit_configs = {
        '50%/15:30/ws': lambda: standard_exits(0.50, '15:30', True),
        '30%/15:30/ws': lambda: standard_exits(0.30, '15:30', True),
        '50%/close/ws': lambda: standard_exits(0.50, '16:15', True),
        '30%/close/ws': lambda: standard_exits(0.30, '16:15', True),
        'trail30/15/ws': lambda: trailing_exits(0.30, 0.15, '15:30'),
        'td50_30/14:00': lambda: [time_decay_target(0.50, 0.30, '14:00'),
                                    wing_stop(), time_stop('15:30')],
    }

    # Phase 1A: Coarse sweep — every structure × entry time with standard exits
    out.write("\n--- 1A: Structure × Entry Time (50% target, 15:30 stop, wing stop) ---\n")

    phase1_results = []
    for sname, sfn in structures_to_test.items():
        for et in entry_times_coarse:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
                label=f"{sname}/{et}"
            )
            s = calc_stats(trades, label=f"{sname} @ {et}")
            if s and s['n'] >= 20:
                phase1_results.append(s)
                all_pvalues.append((s['label'], s['p_val']))

    phase1_results.sort(key=lambda x: x['avg_dollar'], reverse=True)
    out.write("Top 30 by avg dollar P&L:")
    out.write_stats_table(phase1_results[:30])

    # ─── PHASE 2: ENTRY TIMING SURFACE ──────────────────────────────────
    out.write_header("PHASE 2: ENTRY TIMING — Fine-grained 5-min resolution")

    # Use the top 3 structures from Phase 1 and test every 5-min entry
    top_structures = phase1_results[:3] if phase1_results else []
    fine_entry_times = [t for t in TIME_GRID_5MIN if '09:30' <= t <= '15:30']

    # Theta surface: entry at T, hold to various exits
    out.write("\n--- 2A: Theta decay surface (hold-to-time, no target/stop) ---\n")
    for sname in ['IBF_40w', 'IBF_30w']:  # standard baselines
        sfn = structures_to_test.get(sname)
        if not sfn:
            continue
        out.write(f"\n{sname}:")
        exit_times = ['10:00', '11:00', '12:00', '13:00', '14:00', '15:00', '15:30', '16:00']
        for et in ['09:35', '10:00', '11:00', '12:00', '13:00', '14:00']:
            line = f"  Entry {et}:"
            for ex in exit_times:
                if ex <= et:
                    line += f"  {'---':>7}"
                    continue
                trades = run_sweep(
                    universe, sfn, [et],
                    lambda ex=ex: [time_stop(ex)],
                    dates=dates, slippage=1.0,
                )
                if trades:
                    avg = sum(t.pnl_dollar for t in trades) / len(trades)
                    line += f"  ${avg:>6.0f}"
                else:
                    line += f"  {'n/a':>7}"
            out.write(line)

    # ─── PHASE 2B: HOLD PERIOD SWEEP ──────────────────────────────────
    out.write_header("PHASE 2B: HOLD PERIOD SWEEP — Optimal hold duration by entry time")
    out.write("\nFor each entry time, test every possible hold duration (in 5-min increments)")
    out.write("with 50% profit target + wing stop.  Shows how edge changes with hold time.\n")

    hold_durations_min = [15, 30, 45, 60, 90, 120, 150, 180, 210, 240, 300, 360]

    for sname in ['IBF_40w', 'IC_10otm_20w']:
        sfn = structures_to_test.get(sname)
        if not sfn:
            continue
        out.write(f"\n--- {sname}: Hold Duration Analysis ---")
        out.write(f"\n{'Entry':>8}  {'Hold':>6}  {'Exit@':>6}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'PF':>6}  {'DD$':>9}  {'t':>6}  {'p':>8}")
        out.write("-" * 85)

        for et in ['09:35', '10:00', '11:00', '12:00', '13:00', '14:00', '15:00']:
            et_h, et_m = int(et[:2]), int(et[3:])
            et_total_min = et_h * 60 + et_m

            for hold_min in hold_durations_min:
                exit_total_min = et_total_min + hold_min
                if exit_total_min > 16 * 60 + 15:  # past 16:15
                    continue
                exit_h = exit_total_min // 60
                exit_m = exit_total_min % 60
                exit_time = f"{exit_h:02d}:{exit_m:02d}"

                trades = run_sweep(
                    universe, sfn, [et],
                    lambda ex=exit_time: [profit_target(0.50), wing_stop(), time_stop(ex)],
                    dates=dates, slippage=1.0,
                )
                s = calc_stats(trades, label=f"{sname}/{et}/hold{hold_min}m")
                if s and s['n'] >= 20:
                    out.write(f"{et:>8}  {hold_min:>4}m  {exit_time:>6}  "
                             f"{s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                             f"{s['pf']:>6.2f}  ${s['max_dd_dollar']:>8,.0f}  "
                             f"{s['t_stat']:>6.2f}  {s['p_val']:>8.4f}")
                    all_pvalues.append((s['label'], s['p_val']))

    # ─── PHASE 3: EXIT MECHANICS ────────────────────────────────────────
    out.write_header("PHASE 3: EXIT MECHANICS — Comparing exit rule sets across hold times")

    # Test all exit configs on top structures, including varied time stops
    out.write("\n--- 3A: Exit config × time stop comparison ---\n")
    phase3_results = []

    time_stops_to_test = ['14:00', '14:30', '15:00', '15:30', '16:00', '16:15']

    for sname in ['IBF_40w', 'IBF_30w', 'IC_10otm_20w']:
        sfn = structures_to_test.get(sname)
        if not sfn:
            continue
        for et in ['10:00', '13:00', '14:00']:
            # Standard exits with varying time stops
            for ts in time_stops_to_test:
                for tgt in [0.30, 0.40, 0.50, 0.60, 0.70]:
                    trades = run_sweep(
                        universe, sfn, [et],
                        lambda tgt=tgt, ts=ts: standard_exits(tgt, ts, True),
                        dates=dates, slippage=1.0,
                    )
                    s = calc_stats(trades, label=f"{sname}/{et}/{int(tgt*100)}%tgt/{ts}stop")
                    if s and s['n'] >= 20:
                        phase3_results.append(s)
                        all_pvalues.append((s['label'], s['p_val']))

            # Trailing stop variants
            for act in [0.20, 0.30, 0.40]:
                for trail in [0.10, 0.15, 0.20]:
                    trades = run_sweep(
                        universe, sfn, [et],
                        lambda a=act, tr=trail: trailing_exits(a, tr, '15:30'),
                        dates=dates, slippage=1.0,
                    )
                    s = calc_stats(trades, label=f"{sname}/{et}/trail_{int(act*100)}_{int(trail*100)}")
                    if s and s['n'] >= 20:
                        phase3_results.append(s)
                        all_pvalues.append((s['label'], s['p_val']))

    phase3_results.sort(key=lambda x: x['avg_dollar'], reverse=True)
    out.write("Top 30 by avg dollar P&L:")
    out.write_stats_table(phase3_results[:30])

    # Exit type breakdown for top configs
    out.write("\n--- 3B: How do the best configs exit? ---\n")
    for s in phase3_results[:10]:
        out.write(f"\n  {s['label']}:")
        for exit_type, count in sorted(s['exit_counts'].items()):
            pct = count / s['n'] * 100
            out.write(f"    {exit_type:>15}: {count:>4} ({pct:.0f}%)")

    # ─── PHASE 4: PRE-OPEN REGIME FILTERS ──────────────────────────────
    out.write_header("PHASE 4: PRE-OPEN REGIME FILTERS — Daily, weekly, gap, VIX")

    pre_filters = build_pre_open_filters(universe)

    # Pick the top 3 unfiltered configs and test all filters
    top3_configs = []
    for s in (phase1_results or [])[:3]:
        # Parse label to recover structure/entry
        parts = s['label'].split(' @ ')
        if len(parts) == 2:
            top3_configs.append((parts[0], parts[1]))

    for sname, et in top3_configs:
        sfn = structures_to_test.get(sname)
        if not sfn:
            continue

        out.write(f"\n--- {sname} @ {et} ---")

        # Unfiltered baseline
        base_trades = run_sweep(
            universe, sfn, [et],
            lambda: standard_exits(0.50, '15:30', True),
            dates=dates, slippage=1.0,
        )
        base_stats = calc_stats(base_trades, label="UNFILTERED")
        out.write(f"\n  UNFILTERED: {fmt_stats(base_stats)}")

        # Test filter combos
        filter_results = test_filter_combos(
            base_trades, pre_filters,
            max_combo_size=3, min_n=10
        )
        for fr in filter_results:
            all_pvalues.append((fr['label'], fr['p_val']))

        out.write("\n  Top 15 single filters:")
        singles = [r for r in filter_results if ' + ' not in r['label']][:15]
        out.write_stats_table(singles)

        out.write("\n  Top 15 two-filter combos:")
        doubles = [r for r in filter_results if r['label'].count(' + ') == 1][:15]
        out.write_stats_table(doubles)

        out.write("\n  Top 10 three-filter combos:")
        triples = [r for r in filter_results if r['label'].count(' + ') == 2][:10]
        out.write_stats_table(triples)

    # ─── PHASE 5: INTRADAY TRIGGERS ────────────────────────────────────
    out.write_header("PHASE 5: INTRADAY TRIGGERS — Morning character, gap fill, VIX trajectory")

    intra_filters = build_intraday_filters(universe)

    for sname in ['IBF_40w', 'IC_10otm_20w']:
        sfn = structures_to_test.get(sname)
        if not sfn:
            continue
        for et in ['10:00', '13:00']:
            out.write(f"\n--- {sname} @ {et} with intraday filters ---")
            for ifname, iffn in intra_filters.items():
                trades = run_sweep(
                    universe, sfn, [et],
                    lambda: standard_exits(0.50, '15:30', True),
                    dates=dates, slippage=1.0,
                    intra_filter=iffn,
                )
                s = calc_stats(trades, label=f"{ifname}")
                if s and s['n'] >= 15:
                    out.write(f"  {ifname:>20}: {fmt_stats(s)}")
                    all_pvalues.append((f"{sname}/{et}/{ifname}", s['p_val']))

    # ─── PHASE 6: BID-ASK REALITY CHECK ────────────────────────────────
    out.write_header("PHASE 6: BID-ASK SPREADS — True execution costs")

    # For the top configs, compare midpoint P&L vs bid-ask-adjusted P&L
    # This uses the quotes data
    out.write("\n(Analysis requires quotes data; will show spread statistics)")

    for sname in ['IBF_40w', 'IC_10otm_20w']:
        sfn = structures_to_test.get(sname)
        if not sfn:
            continue
        out.write(f"\n--- {sname}: Average bid-ask spread at entry ---")
        for et in ['10:00', '13:00', '15:00']:
            spreads = []
            for date in dates[:100]:  # sample
                atm = universe.current_atm(date, et)
                if not atm:
                    continue
                # Check spread for ATM call and put
                for cp in ['C', 'P']:
                    q = universe.quote(date, et, atm, cp)
                    if q:
                        spreads.append(q['spread'])
            if spreads:
                import statistics as pystats
                out.write(f"  {et}: avg spread=${pystats.mean(spreads):.2f}  "
                         f"median=${pystats.median(spreads):.2f}  "
                         f"max=${max(spreads):.2f}  n={len(spreads)}")

    # ─── PHASE 7: WALK-FORWARD VALIDATION ──────────────────────────────
    out.write_header("PHASE 7: WALK-FORWARD VALIDATION — Rolling OOS on top strategies")

    # Collect top strategies for validation
    top_for_validation = (phase1_results or [])[:5] + (phase3_results or [])[:5]

    for s in top_for_validation:
        parts = s['label'].split(' @ ')
        if len(parts) != 2:
            parts = s['label'].split('/')
        if len(parts) < 2:
            continue
        sname = parts[0]
        et = parts[1] if len(parts) == 2 else parts[1]

        sfn = structures_to_test.get(sname)
        if not sfn:
            continue

        trades = run_sweep(
            universe, sfn, [et.split('/')[0] if '/' in et else et],
            lambda: standard_exits(0.50, '15:30', True),
            dates=dates, slippage=1.0,
        )
        if len(trades) < 30:
            continue

        # Half split
        h1, h2 = half_split(trades)
        s1 = calc_stats(h1, label="H1")
        s2 = calc_stats(h2, label="H2")
        stable = "YES" if s1 and s2 and s1['avg_dollar'] > 0 and s2['avg_dollar'] > 0 else "NO"

        out.write(f"\n  {s['label']}: H1={fmt_stats(s1)} | H2={fmt_stats(s2)} | Stable={stable}")

        # Walk-forward
        splits = walk_forward_split(trades, train_months=6, test_months=2)
        if splits:
            oos_pnls = []
            for train, test in splits:
                ts = calc_stats(test)
                if ts:
                    oos_pnls.append(ts['avg_dollar'])
            if oos_pnls:
                pos = sum(1 for p in oos_pnls if p > 0)
                out.write(f"    Walk-forward: {len(splits)} splits, "
                         f"{pos}/{len(splits)} profitable OOS, "
                         f"avg OOS=${sum(oos_pnls)/len(oos_pnls):.0f}")

    # ─── PHASE 8: MULTIPLE COMPARISON CORRECTION ───────────────────────
    out.write_header("PHASE 8: MULTIPLE COMPARISON CORRECTION — Holm-Bonferroni")

    out.write(f"\nTotal tests performed: {len(all_pvalues)}")
    if all_pvalues:
        holm = holm_bonferroni(all_pvalues)
        sig = [h for h in holm if h[2]]
        out.write(f"Significant after Holm-Bonferroni: {len(sig)}/{len(holm)}")
        out.write(f"\nStrategies surviving correction:")
        for label, p, is_sig in sig[:30]:
            out.write(f"  {label:60s}  p={p:.6f}  {'***' if p < 0.001 else '**' if p < 0.01 else '*'}")

    # ─── PHASE 9: SUMMARY ──────────────────────────────────────────────
    out.write_header("PHASE 9: SUMMARY & CAVEATS")

    out.write("""
HONEST CAVEATS (per CLAUDE.md — surface problems, don't hide them):

1. BLIND VALIDATION: This run uses the IDENTICAL framework as 2024.
   No parameters, filters, structures, or entry times were changed.
   Edges that appear in BOTH 2024 and 2025 are independently discovered
   and are the strongest candidates for real, tradeable strategies.

2. DIFFERENT REGIME: 2025 includes the tariff selloff, VIX spikes to 40+,
   and a genuine bear phase — conditions absent from the 2024 discovery
   period. Strategies that survive HERE despite the regime change are
   more robust than those that only worked in the low-VIX bull of 2024.

3. MULTIPLE COMPARISONS: We tested hundreds of combinations. Even after
   Holm-Bonferroni correction, some apparent edges may be spurious.
   The more strategies that "survive" correction, the more confident
   we can be — but no single strategy should be trusted on p-value alone.

4. SLIPPAGE MODEL: We applied $1/spread flat slippage. Real slippage
   varies by time of day, VIX level, and structure type. The bid-ask
   analysis in Phase 6 gives empirical guidance but doesn't capture
   market impact.

5. REGIME DEPENDENCE: An edge that works in VIX 13-18 may disappear or
   reverse in VIX 25+. Always check which VIX regime drives the result.

6. SAMPLE SIZE: Strategies with n < 30 are directional only. Never
   present small-n results as statistically significant.

7. CORRELATION RISK: Running multiple vol-selling strategies
   simultaneously on the same underlier concentrates risk on large
   SPX moves. Check the correlation matrix before sizing a portfolio.

NEXT STEPS:
  - Compare these results against research_results_2024.txt
  - Edges appearing in BOTH periods = high-confidence tradeable strategies
  - Edges that appeared in 2024 but NOT here = regime-dependent, use with caution
  - Edges that appear HERE but not in 2024 = new regime opportunities, need more data
""")

    # ─── SAVE ───────────────────────────────────────────────────────────
    out.save()
    print(f"\nDone. Results in: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
