"""
Entry Time Sensitivity Analysis for PHOENIX V3
================================================
Compares backtest results using 10:00, 10:10, and 10:15 entry pricing
to quantify opening-rotation credit inflation.

Uses cached Polygon option minute bars — zero new API calls for same-strike analysis.

Usage:
    python entry_time_sensitivity.py [POLYGON_API_KEY]
"""

import sys, os, json
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Import backtest_research.py infrastructure ---
_API_KEY = sys.argv[1] if len(sys.argv) > 1 else "cBE5Kbq9yllt0Yj29mDQjBcIKfAYQlHF"
sys.argv = ["entry_time_sensitivity.py", _API_KEY, "600"]
import backtest_research as br

# Point cache to main repo
_MAIN_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_DIR)))
_MAIN_CACHE = os.path.join(_MAIN_REPO, ".polygon_cache")
if os.path.isdir(_MAIN_CACHE):
    br.CACHE_DIR = _MAIN_CACHE
    n_cached = len(os.listdir(_MAIN_CACHE))
    print(f"  Cache: {_MAIN_CACHE} ({n_cached} files)")
else:
    print(f"  WARNING: Main cache not found at {_MAIN_CACHE}")

# --- Constants ---
DAILY_RISK = 100_000
SPX_MULT = 100
SLIPPAGE = 1.00
ET = br.ET
CSV_PATH = os.path.join(_DIR, "research_all_trades.csv")

# Entry times to test: (hour, minute, label)
ENTRY_TIMES = [
    (10,  0, "10:00"),
    (10, 10, "10:10"),
    (10, 15, "10:15"),
]

# Time checkpoint mapping
TS_HM = {
    "1030": (10, 30), "1100": (11, 0), "1130": (11, 30),
    "1200": (12, 0),  "1230": (12, 30), "1300": (13, 0),
    "1330": (13, 30), "1400": (14, 0),  "1430": (14, 30),
    "1500": (15, 0),  "1530": (15, 30), "1545": (15, 45),
}
CHECKPOINT_LABELS = list(TS_HM.keys())


# ==============================================================
# PHOENIX SIGNAL EVALUATION
# ==============================================================
def evaluate_phoenix_signals(row):
    """Evaluate 5 PHOENIX signal groups against a CSV row.
    Returns (fire_count, best_target_pct, best_time_stop)."""
    vix = row.get("vix", 99)
    vp = row.get("vp_ratio", 99)
    pd_dir = row.get("prior_day_direction", "")
    ret5d = row.get("prior_5d_return", -99)
    in_wk = row.get("in_prior_week_range", 1)
    rv_slope = row.get("rv_slope", "UNKNOWN")
    rv_1d = row.get("rv_1d_change", -99)

    groups = [
        {"name": "VIX≤20 + VP≤1.0 + 5dRet>0",
         "fire": vix <= 20 and vp <= 1.0 and ret5d > 0,
         "target": 70, "ts": "close"},
        {"name": "VP≤1.3 + PrDayDn + 5dRet>0",
         "fire": vp <= 1.3 and pd_dir == "DOWN" and ret5d > 0,
         "target": 50, "ts": "close"},
        {"name": "VP≤1.2 + 5dRet>0 + RVchg>0",
         "fire": vp <= 1.2 and ret5d > 0 and rv_1d > 0,
         "target": 50, "ts": "close"},
        {"name": "VP≤1.5 + OutWkRng + 5dRet>0",
         "fire": vp <= 1.5 and in_wk == 0 and ret5d > 0,
         "target": 70, "ts": "close"},
        {"name": "VP≤1.3 + !RISING + 5dRet>0",
         "fire": vp <= 1.3 and rv_slope != "RISING" and ret5d > 0,
         "target": 50, "ts": "close"},
    ]

    fire_count = sum(1 for g in groups if g["fire"])
    best = next((g for g in groups if g["fire"]), None)
    if best:
        return fire_count, best["target"], best["ts"]
    return 0, 50, "close"


def phoenix_sizing(fire_count):
    """Tiered sizing: 0→$0, 1→$25K, 2→$50K, 3→$75K, 4+→$100K."""
    if fire_count == 0: return 0
    if fire_count == 1: return 25_000
    if fire_count == 2: return 50_000
    if fire_count == 3: return 75_000
    return 100_000


# ==============================================================
# PHASE 1: SAME-STRIKE CREDIT COMPARISON
# ==============================================================
def phase1_credit_comparison(go_df, phoenix_days):
    """For each PHOENIX day, compare entry credit at 10:00, 10:10, 10:15
    using the SAME strikes (10:00 ATM). Isolates opening-rotation inflation."""

    print(f"\n{'=' * 90}")
    print(f"  PHASE 1: SAME-STRIKE CREDIT COMPARISON (opening rotation analysis)")
    print(f"{'=' * 90}")
    print(f"  For each day, pricing the 10:00 strikes at 10:00, 10:10, and 10:15")
    print(f"  This isolates credit inflation from opening rotation vs settled pricing\n")

    results = []
    skipped = 0

    print(f"{'Date':<12} {'ATM':>6} {'WW':>4} {'Cr@10:00':>10} {'Cr@10:10':>10} {'Cr@10:15':>10} {'d10':>8} {'d15':>8} {'%d10':>7} {'%d15':>7}")
    print("-" * 90)

    for day_info in phoenix_days:
        ds = day_info["day_str"]
        row = day_info["row"]
        ww = day_info["wing_width"]
        exp_date_obj = datetime.strptime(ds, "%Y-%m-%d").date()

        br.clear_option_cache()

        # Get SPX bars
        spx_df = br.get_bars("I:SPX", ds)
        if spx_df.empty:
            skipped += 1
            continue

        first_t = spx_df.iloc[0]["t"]

        # Get SPX at 10:00 for ATM strike
        t_1000 = first_t.replace(hour=10, minute=0, second=0, microsecond=0)
        entry_bars = spx_df[spx_df["t"] >= t_1000]
        if entry_bars.empty:
            skipped += 1
            continue
        spx_1000 = entry_bars.iloc[0]["o"]
        atm = br.snap(spx_1000)
        wp = atm - ww
        wc = atm + ww

        # Price at each time using SAME strikes
        credits = {}
        for h, m, lbl in ENTRY_TIMES:
            target_t = first_t.replace(hour=h, minute=m, second=0, microsecond=0)
            cr, _, miss = br.fetch_ibf_prices_at(exp_date_obj, atm, wp, wc, ds, target_t)
            credits[lbl] = cr

        if credits["10:00"] is None:
            skipped += 1
            continue

        c0 = credits["10:00"]
        c10 = credits["10:10"]
        c15 = credits["10:15"]

        if c10 is None or c15 is None:
            skipped += 1
            continue

        d10 = c10 - c0
        d15 = c15 - c0
        pct10 = d10 / c0 * 100 if c0 > 0 else 0
        pct15 = d15 / c0 * 100 if c0 > 0 else 0

        results.append({
            "date": ds, "atm": atm, "ww": ww,
            "credit_1000": c0, "credit_1010": c10, "credit_1015": c15,
            "diff_10": d10, "diff_15": d15,
            "pct_10": pct10, "pct_15": pct15,
            "fire_count": day_info["signal_count"],
            "risk_budget": day_info["risk_budget"],
            "target_pct": day_info["target_pct"],
        })

        flag = " <<<" if abs(pct10) > 10 else ""
        print(f"{ds:<12} {atm:>6.0f} {ww:>4} {c0:>10.2f} {c10:>10.2f} {c15:>10.2f} "
              f"{d10:>+8.2f} {d15:>+8.2f} {pct10:>+6.1f}% {pct15:>+6.1f}%{flag}")

    if not results:
        print("  No results!")
        return results

    # Summary statistics
    df = pd.DataFrame(results)
    print(f"\n  -- Summary ({len(df)} PHOENIX days, {skipped} skipped) --")
    print(f"  {'Metric':<30} {'d10:10':>12} {'d10:15':>12}")
    print(f"  {'Mean credit diff':<30} {df['diff_10'].mean():>+12.2f} {df['diff_15'].mean():>+12.2f}")
    print(f"  {'Median credit diff':<30} {df['diff_10'].median():>+12.2f} {df['diff_15'].median():>+12.2f}")
    print(f"  {'Std dev':<30} {df['diff_10'].std():>12.2f} {df['diff_15'].std():>12.2f}")
    print(f"  {'Mean % change':<30} {df['pct_10'].mean():>+11.1f}% {df['pct_15'].mean():>+11.1f}%")
    print(f"  {'Days with >5% inflation':<30} {(df['pct_10'] < -5).sum():>12} {(df['pct_15'] < -5).sum():>12}")
    print(f"  {'Days with >10% inflation':<30} {(df['pct_10'] < -10).sum():>12} {(df['pct_15'] < -10).sum():>12}")
    print(f"  {'Max inflation (most neg)':<30} {df['pct_10'].min():>+11.1f}% {df['pct_15'].min():>+11.1f}%")
    print(f"  {'Max deflation (most pos)':<30} {df['pct_10'].max():>+11.1f}% {df['pct_15'].max():>+11.1f}%")

    # Breakdown by bucket
    print(f"\n  -- Credit change distribution --")
    for lo, hi, lbl in [(-999, -5, ">5% lower"), (-5, -2, "2-5% lower"), (-2, 2, "Within +/-2%"),
                         (2, 5, "2-5% higher"), (5, 999, ">5% higher")]:
        n10 = ((df["pct_10"] >= lo) & (df["pct_10"] < hi)).sum()
        n15 = ((df["pct_15"] >= lo) & (df["pct_15"] < hi)).sum()
        print(f"  {lbl:<20} 10:10: {n10:>3} ({n10/len(df)*100:>5.1f}%)   10:15: {n15:>3} ({n15/len(df)*100:>5.1f}%)")

    return results


# ==============================================================
# PHASE 2: FULL P&L RE-COMPUTATION
# ==============================================================
def phase2_pnl_recompute(go_df, phoenix_days, phase1_results):
    """Re-compute full PHOENIX V3 backtest P&L at each entry time.

    Uses the offset method: since spread values at checkpoints don't change,
    new_pnl = old_pnl + (new_credit - old_credit) for same-strike analysis.
    Re-derives target hits with the adjusted P&L values.
    """

    print(f"\n{'=' * 90}")
    print(f"  PHASE 2: FULL P&L RE-COMPUTATION (PHOENIX tiered sizing + mechanics)")
    print(f"{'=' * 90}\n")

    # Index phase1 results by date
    p1_by_date = {r["date"]: r for r in phase1_results}

    # Build results for each entry time
    all_results = {}
    for _, _, lbl in ENTRY_TIMES:
        all_results[lbl] = []

    for day_info in phoenix_days:
        ds = day_info["day_str"]
        row = day_info["row"]
        fire_count = day_info["signal_count"]
        risk = day_info["risk_budget"]
        target_pct = day_info["target_pct"]
        ww = day_info["wing_width"]

        p1 = p1_by_date.get(ds)
        if p1 is None:
            continue

        # Original entry data from CSV
        orig_credit = p1["credit_1000"]
        if orig_credit is None or orig_credit <= 0:
            continue

        orig_ml_ps = (ww - orig_credit)  # max loss per spread (points)
        if orig_ml_ps <= 0:
            continue
        orig_n_spreads = max(1, int(risk / (orig_ml_ps * SPX_MULT)))

        # Get checkpoint spread values (constant regardless of entry time)
        # spread_val_at_cp = orig_credit - pnl_at_cp
        checkpoint_svs = {}
        for cp in CHECKPOINT_LABELS:
            pnl = row.get(f"pnl_at_{cp}")
            if pd.notna(pnl):
                checkpoint_svs[cp] = orig_credit - pnl
        close_pnl = row.get("pnl_at_close")
        if pd.notna(close_pnl):
            checkpoint_svs["close"] = orig_credit - close_pnl

        # Wing stop data (doesn't depend on entry credit)
        ws_time_str = row.get("ws_time", "")
        ws_pnl_orig = row.get("ws_pnl")

        for _, _, time_lbl in ENTRY_TIMES:
            cr_key = f"credit_{time_lbl.replace(':', '')}"
            new_credit = p1.get(cr_key)
            if new_credit is None or new_credit <= 0:
                all_results[time_lbl].append(0)
                continue

            # New sizing
            new_ml_ps = ww - new_credit
            if new_ml_ps <= 0:
                new_ml_ps = 0.01
            new_n_spreads = max(1, int(risk / (new_ml_ps * SPX_MULT)))

            # New target threshold (per spread)
            new_target_val = new_credit * (target_pct / 100.0)

            # Re-derive checkpoint P&L and find exit
            # new_pnl_at_cp = new_credit - spread_val = new_credit - (orig_credit - old_pnl)
            credit_diff = new_credit - orig_credit

            # Check wing stop (same timing, but P&L is adjusted)
            ws_active = False
            ws_adj_pnl = None
            if ws_time_str and pd.notna(ws_pnl_orig):
                ws_adj_pnl = ws_pnl_orig + credit_diff  # adjusted per-spread P&L

            # Check target hit (re-scan checkpoints)
            tgt_hit_pnl = None
            tgt_hit_cp = None
            for cp in CHECKPOINT_LABELS:
                if cp not in checkpoint_svs:
                    continue
                new_pnl = new_credit - checkpoint_svs[cp]
                if new_pnl >= new_target_val:
                    tgt_hit_pnl = new_pnl
                    tgt_hit_cp = cp
                    break

            # Determine exit (priority: WS > TGT > TS > EXP)
            # Wing stop time check
            ws_h, ws_m = 99, 99
            if ws_time_str:
                try:
                    ws_t = pd.Timestamp(ws_time_str)
                    ws_h, ws_m = ws_t.hour, ws_t.minute
                except:
                    pass

            tgt_h, tgt_m = 99, 99
            if tgt_hit_cp:
                tgt_h, tgt_m = TS_HM[tgt_hit_cp]

            # Time stop = close for V3
            exit_pnl_ps = None
            exit_type = "ND"

            # WS before TGT?
            ws_before_tgt = (ws_h < tgt_h or (ws_h == tgt_h and ws_m <= tgt_m))

            if ws_adj_pnl is not None and (tgt_hit_pnl is None or ws_before_tgt):
                # Wing stop fires first
                exit_pnl_ps = ws_adj_pnl
                exit_type = "WS"
            elif tgt_hit_pnl is not None:
                exit_pnl_ps = tgt_hit_pnl
                exit_type = "TGT"
            else:
                # Time stop / expiry
                if "close" in checkpoint_svs:
                    exit_pnl_ps = new_credit - checkpoint_svs["close"]
                    exit_type = "EXP"
                elif pd.notna(close_pnl):
                    exit_pnl_ps = close_pnl + credit_diff
                    exit_type = "EXP"

            if exit_pnl_ps is None:
                all_results[time_lbl].append(0)
                continue

            # Cap P&L
            exit_pnl_ps = max(-new_ml_ps, min(new_credit, exit_pnl_ps))

            # Dollar P&L
            dollar_pnl = exit_pnl_ps * new_n_spreads * SPX_MULT - new_n_spreads * SLIPPAGE * SPX_MULT
            all_results[time_lbl].append(round(dollar_pnl))

    # Print comparison
    print(f"{'Entry Time':<12} {'Trades':>7} {'Total P&L':>14} {'Avg P&L':>10} {'WR%':>6} {'PF':>6} {'MaxDD':>12} {'Calmar':>8}")
    print("-" * 80)

    for _, _, lbl in ENTRY_TIMES:
        pnls = np.array(all_results[lbl])
        n = len(pnls)
        if n == 0:
            continue
        wins = (pnls > 0).sum()
        losses = (pnls < 0).sum()
        wr = wins / n * 100
        tot = pnls.sum()
        avg = pnls.mean()
        avg_w = pnls[pnls > 0].mean() if wins > 0 else 0
        avg_l = pnls[pnls < 0].mean() if losses > 0 else 0
        pf = abs(avg_w * wins / (avg_l * losses)) if losses > 0 and avg_l != 0 else float("inf")

        # Max drawdown
        cum = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        dd = cum - peak
        max_dd = dd.min()
        calmar = tot / abs(max_dd) if max_dd < 0 else float("inf")

        print(f"{lbl:<12} {n:>7} ${tot:>+13,.0f} ${avg:>+9,.0f} {wr:>5.1f}% {pf:>5.2f} ${max_dd:>+11,.0f} {calmar:>7.2f}")

    # Per-day comparison
    print(f"\n  -- Per-Day P&L Differences (10:10 vs 10:00) --")
    pnl_1000 = np.array(all_results["10:00"])
    pnl_1010 = np.array(all_results["10:10"])
    pnl_1015 = np.array(all_results["10:15"])
    diff_10 = pnl_1010 - pnl_1000
    diff_15 = pnl_1015 - pnl_1000
    print(f"  Mean daily P&L diff (10:10): ${diff_10.mean():>+,.0f}")
    print(f"  Mean daily P&L diff (10:15): ${diff_15.mean():>+,.0f}")
    print(f"  Total P&L diff (10:10):      ${diff_10.sum():>+,.0f}")
    print(f"  Total P&L diff (10:15):      ${diff_15.sum():>+,.0f}")
    print(f"  Days where 10:10 was better: {(diff_10 > 0).sum()}")
    print(f"  Days where 10:10 was worse:  {(diff_10 < 0).sum()}")
    print(f"  Days where 10:15 was better: {(diff_15 > 0).sum()}")
    print(f"  Days where 10:15 was worse:  {(diff_15 < 0).sum()}")

    return all_results


# ==============================================================
# MAIN
# ==============================================================
def main():
    print(f"\n{'PHOENIX V3 - Entry Time Sensitivity Analysis':=^90}")
    print(f"  Testing entry times: 10:00, 10:10, 10:15")
    print(f"  Using cached Polygon option bars\n")

    # Load CSV
    if not os.path.exists(CSV_PATH):
        print(f"  ERROR: CSV not found at {CSV_PATH}")
        return
    go_df = pd.read_csv(CSV_PATH)
    go_df = go_df[go_df["recommendation"] == "GO"].reset_index(drop=True)
    print(f"  Loaded {len(go_df)} GO days from {CSV_PATH}")

    # Identify PHOENIX days
    phoenix_days = []
    for idx in range(len(go_df)):
        row = go_df.iloc[idx]
        fire_count, target_pct, time_stop = evaluate_phoenix_signals(row)
        if fire_count == 0:
            continue
        risk = phoenix_sizing(fire_count)
        ds = row["date"]
        ww = int(row.get("wing_width", 40))
        phoenix_days.append({
            "idx": idx,
            "date": ds if isinstance(ds, str) else ds.strftime("%Y-%m-%d"),
            "day_str": ds if isinstance(ds, str) else ds.strftime("%Y-%m-%d"),
            "signal_count": fire_count,
            "risk_budget": risk,
            "target_pct": target_pct,
            "time_stop": time_stop,
            "wing_width": ww,
            "row": row,
        })

    print(f"  Identified {len(phoenix_days)} PHOENIX trading days")

    # Fire count distribution
    from collections import Counter
    fc = Counter(d["signal_count"] for d in phoenix_days)
    for k in sorted(fc.keys()):
        print(f"    {k} signals: {fc[k]} days")

    # Phase 1: Credit comparison
    p1_results = phase1_credit_comparison(go_df, phoenix_days)

    # Phase 2: Full P&L re-computation
    if p1_results:
        phase2_pnl_recompute(go_df, phoenix_days, p1_results)

    print(f"\n{'=' * 90}")
    print(f"  Done.")
    print(f"{'=' * 90}\n")


if __name__ == "__main__":
    main()
