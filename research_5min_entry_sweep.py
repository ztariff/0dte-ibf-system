#!/usr/bin/env python3
"""
research_5min_entry_sweep.py — Entry at every 5-min mark from 09:30 to 15:30.

We have 5-min option data but only tested at 30-min intervals before.
This maps the full edge surface at every possible entry time.

Writes to: entry_sweep_5min_results.txt
"""

import os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research.data import DataUniverse
from research.structures import price_entry, iron_butterfly, iron_condor
from research.exits import (
    simulate_trade, standard_exits, profit_target, time_stop,
    wing_stop, TIME_GRID_5MIN,
)
from research.stats import calc_stats, fmt_stats
from research.sweep import run_sweep, ibf_factory, ic_factory, ResultsWriter

_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_FILE = os.path.join(_DIR, "entry_sweep_5min_results.txt")

DATE_START = "2023-11-01"
DATE_END   = "2024-12-31"


def main():
    universe = DataUniverse()
    universe.load(load_quotes=False)
    all_dates = universe.trading_dates()
    dates = [d for d in all_dates if DATE_START <= d <= DATE_END]

    out = ResultsWriter(RESULTS_FILE)
    out.write("=" * 100)
    out.write("5-MINUTE ENTRY TIME SWEEP — Full resolution edge map")
    out.write(f"Discovery window: {dates[0]} to {dates[-1]} ({len(dates)} days)")
    out.write("=" * 100)

    # Every 5-min entry from 09:30 to 15:30
    entry_times = [t for t in TIME_GRID_5MIN if "09:30" <= t <= "15:30"]
    out.write(f"\nTesting {len(entry_times)} entry times: {entry_times[0]} to {entry_times[-1]}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 1: IBF at various wing widths, standard exits
    # ─────────────────────────────────────────────────────────────────

    for ww in [40, 50, 60, 75]:
        out.write_header(f"IBF_{ww}w — 50% target / 15:30 stop / wing stop")
        sfn = ibf_factory(ww)

        out.write(f"\n{'Entry':>8}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'Total$':>10}  {'PF':>7}  {'DD$':>9}  {'t':>6}  {'p':>8}  {'WS%':>5}")
        out.write("-" * 95)

        for et in entry_times:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
            )
            s = calc_stats(trades, label=f"IBF_{ww}w/{et}")
            if s and s['n'] >= 10:
                ws_pct = s['exit_counts'].get('WING_STOP', 0) / s['n'] * 100
                out.write(f"{et:>8}  {s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                         f"${s['total_dollar']:>9,.0f}  {s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  "
                         f"{s['t_stat']:>6.2f}  {s['p_val']:>8.4f}  {ws_pct:>4.1f}%")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 2: IBF_75w with hold-to-close (no early time stop)
    # ─────────────────────────────────────────────────────────────────

    out.write_header("IBF_75w — 70% target / 16:15 close / wing stop")
    sfn = ibf_factory(75)

    out.write(f"\n{'Entry':>8}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'Total$':>10}  {'PF':>7}  {'DD$':>9}  {'t':>6}  {'p':>8}")
    out.write("-" * 85)

    for et in entry_times:
        trades = run_sweep(
            universe, sfn, [et],
            lambda: [profit_target(0.70), wing_stop(), time_stop('16:15')],
            dates=dates, slippage=1.0,
        )
        s = calc_stats(trades, label=f"IBF_75w/{et}/70pct_close")
        if s and s['n'] >= 10:
            out.write(f"{et:>8}  {s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                     f"${s['total_dollar']:>9,.0f}  {s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  "
                     f"{s['t_stat']:>6.2f}  {s['p_val']:>8.4f}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 3: Morning scalp — enter at 5min marks, exit 60min later
    # ─────────────────────────────────────────────────────────────────

    out.write_header("IBF_75w — Morning scalp: 30% target / exit 60min after entry / wing stop")

    out.write(f"\n{'Entry':>8}  {'Exit@':>6}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'Total$':>10}  {'PF':>7}  {'DD$':>9}  {'t':>6}  {'p':>8}")
    out.write("-" * 95)

    sfn = ibf_factory(75)
    for et in entry_times:
        et_min = int(et[:2]) * 60 + int(et[3:])
        exit_min = et_min + 60
        if exit_min > 16 * 60:
            continue
        exit_time = f"{exit_min // 60:02d}:{exit_min % 60:02d}"

        trades = run_sweep(
            universe, sfn, [et],
            lambda ex=exit_time: [profit_target(0.30), wing_stop(), time_stop(ex)],
            dates=dates, slippage=1.0,
        )
        s = calc_stats(trades, label=f"IBF_75w/{et}/scalp60")
        if s and s['n'] >= 10:
            out.write(f"{et:>8}  {exit_time:>6}  {s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                     f"${s['total_dollar']:>9,.0f}  {s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  "
                     f"{s['t_stat']:>6.2f}  {s['p_val']:>8.4f}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 4: OTM Iron Condors at 5-min resolution
    # ─────────────────────────────────────────────────────────────────

    for short_off, wing_w in [(10, 20), (15, 20), (20, 20)]:
        name = f"IC_{short_off}otm_{wing_w}w"
        out.write_header(f"{name} — 50% target / 15:30 stop / wing stop")
        sfn = ic_factory(short_off, wing_w)

        out.write(f"\n{'Entry':>8}  {'N':>5}  {'WR':>6}  {'Avg$':>8}  {'Total$':>10}  {'PF':>7}  {'DD$':>9}  {'t':>6}  {'p':>8}")
        out.write("-" * 85)

        for et in entry_times:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
            )
            s = calc_stats(trades, label=f"{name}/{et}")
            if s and s['n'] >= 10:
                out.write(f"{et:>8}  {s['n']:>5}  {s['win_rate']:>5.1f}%  ${s['avg_dollar']:>7.0f}  "
                         f"${s['total_dollar']:>9,.0f}  {s['pf']:>7.2f}  ${s['max_dd_dollar']:>8,.0f}  "
                         f"{s['t_stat']:>6.2f}  {s['p_val']:>8.4f}")

    # ─────────────────────────────────────────────────────────────────
    # SECTION 5: Top 20 overall
    # ─────────────────────────────────────────────────────────────────
    out.write_header("TOP 20 ACROSS ALL STRUCTURES AND ENTRY TIMES (by avg $)")

    # Re-run everything and collect
    all_results = []

    for ww in [40, 50, 60, 75]:
        sfn = ibf_factory(ww)
        for et in entry_times:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
            )
            s = calc_stats(trades, label=f"IBF_{ww}w @ {et}")
            if s and s['n'] >= 20:
                all_results.append(s)

    for short_off, wing_w in [(10, 20), (15, 20), (20, 20)]:
        sfn = ic_factory(short_off, wing_w)
        for et in entry_times:
            trades = run_sweep(
                universe, sfn, [et],
                lambda: standard_exits(0.50, '15:30', True),
                dates=dates, slippage=1.0,
            )
            s = calc_stats(trades, label=f"IC_{short_off}otm_{wing_w}w @ {et}")
            if s and s['n'] >= 20:
                all_results.append(s)

    all_results.sort(key=lambda x: x['avg_dollar'], reverse=True)
    out.write_stats_table(all_results[:20])

    out.write(f"\nTotal configs tested: {len(all_results)}")
    sig = sum(1 for r in all_results if r['p_val'] < 0.05)
    out.write(f"Significant at p<0.05: {sig}/{len(all_results)}")
    sig001 = sum(1 for r in all_results if r['p_val'] < 0.001)
    out.write(f"Significant at p<0.001: {sig001}/{len(all_results)}")

    out.save()
    print(f"\nDone. Results in: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
