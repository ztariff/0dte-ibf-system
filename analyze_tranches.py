"""
TRANCHE ANALYSIS — How many entries? How often?
================================================
Uses the existing research_all_trades.csv checkpoint data to simulate
different tranche strategies WITHOUT re-running the backtest.

Math:
  We have P&L snapshots at 12 checkpoints (10:30 through 3:45) plus close.
  pnl_at_X = credit_entry - cost_to_close_at_X  (per spread, for the 10am entry)

  If we ADD a new tranche at checkpoint C (same strikes), we sell the IBF at
  whatever the market price is at time C.  That new tranche's P&L at exit =
      pnl_at_exit - pnl_at_C
  (since the new credit = credit_entry - pnl_at_C, and exit cost = credit_entry - pnl_at_exit)

  Tranche 1 (10am entry): P&L = pnl_at_exit - 0 = pnl_at_exit

Tests:
  - 1 to 12 tranches
  - 30-minute intervals (every checkpoint)
  - 60-minute intervals (every other checkpoint)
  - Combined with best filters from Phase 3

Usage:
    python3 analyze_tranches.py | tee tranche_results.txt
"""

import os
import pandas as pd
import numpy as np
from itertools import combinations

_DIR = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])

N = len(go)
print(f"{'='*160}")
print(f"  TRANCHE ANALYSIS — {N} TRADES")
print(f"  {go['date'].min().strftime('%Y-%m-%d')} to {go['date'].max().strftime('%Y-%m-%d')}")
print(f"{'='*160}")

# Checkpoints in order (label, minutes after 10:00 entry)
CHECKPOINTS_30M = [
    ("1030", 30), ("1100", 60), ("1130", 90),
    ("1200", 120), ("1230", 150), ("1300", 180),
    ("1330", 210), ("1400", 240), ("1430", 270),
    ("1500", 300), ("1530", 330), ("1545", 345),
]

CHECKPOINTS_60M = [
    ("1100", 60), ("1200", 120), ("1300", 180),
    ("1400", 240), ("1500", 300),
]

# Exit time checkpoints — map label to column name
EXIT_MAP = {
    "1030": "pnl_at_1030", "1100": "pnl_at_1100", "1130": "pnl_at_1130",
    "1200": "pnl_at_1200", "1230": "pnl_at_1230", "1300": "pnl_at_1300",
    "1330": "pnl_at_1330", "1400": "pnl_at_1400", "1430": "pnl_at_1430",
    "1500": "pnl_at_1500", "1530": "pnl_at_1530", "1545": "pnl_at_1545",
    "close": "pnl_at_close",
}

# Time stop → (hour, minute) for chronological comparison
TS_HM = {
    "1030": (10,30), "1100": (11,0), "1130": (11,30),
    "1200": (12,0), "1230": (12,30), "1300": (13,0),
    "1330": (13,30), "1400": (14,0), "1430": (14,30),
    "1500": (15,0), "1530": (15,30), "1545": (15,45),
    "close": (16,0),
}

SPX_MULT = 100
SLIPPAGE_PER_SPREAD = 1.00  # $1/spread slippage
TRANCHE_RISK = 25_000       # $25K risk per tranche


def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str) or t_str == "":
        return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except:
        return False


def find_exit_pnl_and_time(row, target_pct, time_stop, use_wing_stop=True):
    """
    Determine when this trade exits and the P&L per spread at exit.
    Returns (exit_pnl_per_spread, exit_checkpoint_label, outcome).
    exit_pnl_per_spread is the value from the pnl_at_* column.
    """
    ts_hour, ts_min = TS_HM.get(time_stop, (16, 0))

    # Check target hits — which targets were hit before the time stop?
    tgt_col = f"hit_{target_pct}_time"
    tgt_pnl_col = f"hit_{target_pct}_pnl"
    tgt_time = row.get(tgt_col, "")
    tgt_pnl = row.get(tgt_pnl_col)

    # Check wing stop
    ws_time = row.get("ws_time", "")
    ws_pnl = row.get("ws_pnl")

    # Find earliest event
    events = []

    # Wing stop
    if use_wing_stop and ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_hour, ts_min):
        try:
            ws_t = pd.Timestamp(ws_time)
            events.append(("WING_STOP", ws_t, ws_pnl, ws_time))
        except:
            pass

    # Target hit
    if pd.notna(tgt_pnl) and tgt_time and time_before(tgt_time, ts_hour, ts_min):
        try:
            tgt_t = pd.Timestamp(tgt_time)
            events.append(("TARGET", tgt_t, tgt_pnl, tgt_time))
        except:
            pass

    # Sort by time — earliest event wins
    if events:
        events.sort(key=lambda x: x[1])
        outcome, _, pnl, t_str = events[0]
        return pnl, t_str, outcome

    # Time stop
    ts_pnl_col = EXIT_MAP.get(time_stop, "pnl_at_close")
    ts_pnl = row.get(ts_pnl_col)
    if pd.notna(ts_pnl):
        return ts_pnl, time_stop, "TIME_STOP" if time_stop != "close" else "EXPIRY"

    # Fallback to close
    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl):
        return close_pnl, "close", "EXPIRY"

    return None, None, "NO_DATA"


def simulate_tranches(row, n_tranches, interval_checkpoints, target_pct, time_stop, use_ws=True):
    """
    Simulate N tranches for a single trade.

    Tranche 1 enters at 10am (entry).
    Tranche K enters at interval_checkpoints[K-2] (for K >= 2).

    Each tranche gets n_spreads based on TRANCHE_RISK / max_loss_per_spread.
    All tranches exit at the same point (target, wing stop, or time stop).

    Returns total $ P&L across all tranches.
    """
    # Find exit point
    exit_pnl, exit_time_str, outcome = find_exit_pnl_and_time(row, target_pct, time_stop, use_ws)

    if exit_pnl is None or outcome == "NO_DATA":
        return 0, "NO_DATA", 0

    # Determine exit (hour, minute) for checking if tranches entered before exit
    if exit_time_str in TS_HM:
        exit_h, exit_m = TS_HM[exit_time_str]
    else:
        try:
            exit_t = pd.Timestamp(exit_time_str)
            exit_h, exit_m = exit_t.hour, exit_t.minute
        except:
            exit_h, exit_m = 16, 0

    # Size each tranche
    credit = row.get("pnl_at_close", 0)  # approximate credit from data
    # Better: use the risk column
    risk_per_spread = row.get("risk_deployed_p1", 0)
    n_spreads_p1 = row.get("n_spreads_p1", 0)
    if n_spreads_p1 > 0 and risk_per_spread > 0:
        ml_per_spread = risk_per_spread / n_spreads_p1
    else:
        ml_per_spread = TRANCHE_RISK  # fallback
    if ml_per_spread <= 0:
        ml_per_spread = TRANCHE_RISK

    n_per_tranche = max(1, int(TRANCHE_RISK / ml_per_spread))

    total_pnl = 0
    tranches_entered = 0

    # Tranche 1: enters at 10am, P&L = exit_pnl per spread
    t1_pnl = exit_pnl * n_per_tranche * SPX_MULT - n_per_tranche * SLIPPAGE_PER_SPREAD * SPX_MULT
    total_pnl += t1_pnl
    tranches_entered = 1

    # Subsequent tranches
    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_checkpoints):
            break  # not enough checkpoints for this many tranches

        cp_label, cp_mins = interval_checkpoints[k - 2]
        cp_col = f"pnl_at_{cp_label}"
        cp_pnl = row.get(cp_col)

        if pd.isna(cp_pnl) if cp_pnl is not None else True:
            continue  # no data at this checkpoint

        # Only add tranche if checkpoint is BEFORE exit
        cp_h, cp_m = TS_HM.get(cp_label, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m):
            continue  # checkpoint is at or after exit — skip

        # Tranche K's P&L per spread = exit_pnl - pnl_at_checkpoint
        tk_pnl_per_spread = exit_pnl - cp_pnl
        tk_pnl = tk_pnl_per_spread * n_per_tranche * SPX_MULT - n_per_tranche * SLIPPAGE_PER_SPREAD * SPX_MULT
        total_pnl += tk_pnl
        tranches_entered += 1

    return round(total_pnl, 0), outcome, tranches_entered


def eval_tranche_strategy(go_df, n_tranches, interval_checkpoints, interval_label,
                          target_pct, time_stop, use_ws=True, filter_label="ALL"):
    """Evaluate a tranche strategy across all trades in go_df."""
    if go_df.empty:
        return None
    results = []
    total_tranches = 0
    for _, row in go_df.iterrows():
        pnl, outcome, n_entered = simulate_tranches(
            row, n_tranches, interval_checkpoints, target_pct, time_stop, use_ws
        )
        results.append({"pnl": pnl, "outcome": outcome, "tranches": n_entered})
        total_tranches += n_entered

    rdf = pd.DataFrame(results)
    valid = rdf[rdf["outcome"] != "NO_DATA"]
    n = len(valid)
    if n < 5:
        return None

    tot = valid["pnl"].sum()
    wr = (valid["pnl"] > 0).mean() * 100
    w = valid[valid["pnl"] > 0]["pnl"].sum()
    l = abs(valid[valid["pnl"] < 0]["pnl"].sum())
    pf = w / l if l > 0 else 99
    cum = valid["pnl"].cumsum()
    dd = (cum - cum.cummax()).min()
    avg = tot / n
    avg_tranches = total_tranches / n

    return {
        "filter": filter_label, "n_tranches": n_tranches, "interval": interval_label,
        "target": target_pct, "time_stop": time_stop,
        "n": n, "total": tot, "wr": wr, "pf": pf, "dd": dd, "avg": avg,
        "avg_tranches_entered": round(avg_tranches, 1),
    }


def print_result(r):
    if r is None:
        return
    print(f"    {r['n_tranches']:>2}T {r['interval']:>4} | "
          f"tgt={r['target']:>2}% ts={r['time_stop']:>5} | "
          f"{r['n']:>4} trades | "
          f"${r['total']:>+13,.0f} | "
          f"WR={r['wr']:>5.1f}% | PF={r['pf']:>5.2f} | "
          f"DD=${r['dd']:>+12,.0f} | "
          f"Avg=${r['avg']:>+9,.0f} | "
          f"AvgTr={r['avg_tranches_entered']:.1f}")


# ════════════════════════════════════════════════════════════════════════════
# SECTION 1: Unfiltered universe — tranche sweep
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  SECTION 1: TRANCHE SWEEP — ALL {N} TRADES (unfiltered)")
print(f"  Testing 1-12 tranches × 30m/60m intervals × multiple mechanics")
print(f"{'='*160}")

# Test across a few good mechanics combos from Phase 1 analysis
mechanics = [
    (50, "1530", "50%/3:30"),
    (50, "close", "50%/close"),
    (70, "1545", "70%/3:45"),
    (70, "close", "70%/close"),
    (40, "1530", "40%/3:30"),
    (40, "close", "40%/close"),
]

for tgt, ts, mech_label in mechanics:
    print(f"\n  ── Mechanics: {mech_label} ──")
    print(f"    {'Config':<20} {'Trades':>6} {'Total $':>14} {'WR':>7} {'PF':>7} "
          f"{'MaxDD':>13} {'Avg/Trd':>11} {'AvgTranches':>12}")
    print(f"    {'─'*110}")

    for interval_label, checkpoints in [("30m", CHECKPOINTS_30M), ("60m", CHECKPOINTS_60M)]:
        for n_t in [1, 2, 3, 4, 5, 6, 8, 10, 12]:
            r = eval_tranche_strategy(go, n_t, checkpoints, interval_label, tgt, ts)
            if r:
                print_result(r)
        print()  # blank line between intervals


# ════════════════════════════════════════════════════════════════════════════
# SECTION 2: Best filters + tranche sweep
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  SECTION 2: TRANCHE SWEEP WITH BEST FILTERS")
print(f"{'='*160}")

# Define the top filter combos from Phase 3
filter_sets = {}

if "prior_5d_return" in go.columns:
    filter_sets["VP≤1.3 + !RISING + 5dRet>0"] = (
        (go["vp_ratio"] <= 1.3) & (go["rv_slope"] != "RISING") & (go["prior_5d_return"] > 0)
    )
    filter_sets["VP≤1.3 + PrDayDn + 5dRet>0"] = (
        (go["vp_ratio"] <= 1.3) & (go["prior_day_direction"] == "DOWN") & (go["prior_5d_return"] > 0)
    )
    filter_sets["VP≤1.5 + !RISING + 5dRet>0"] = (
        (go["vp_ratio"] <= 1.5) & (go["rv_slope"] != "RISING") & (go["prior_5d_return"] > 0)
    )
    filter_sets["VP≤1.7 + !RISING + 5dRet>0"] = (
        (go["vp_ratio"] <= 1.7) & (go["rv_slope"] != "RISING") & (go["prior_5d_return"] > 0)
    )
    filter_sets["VP≤1.3 + STABLE + 5dRet>0"] = (
        (go["vp_ratio"] <= 1.3) & (go["rv_slope"] == "STABLE") & (go["prior_5d_return"] > 0)
    )

# Also test with no filter for comparison
filter_sets["ALL (no filter)"] = pd.Series(True, index=go.index)

for filt_name, mask in filter_sets.items():
    sub = go[mask]
    print(f"\n  ── Filter: {filt_name} ({len(sub)} trades) ──")

    # Test the most promising mechanics from Section 1 + tranche combos
    for tgt, ts, mech_label in [(70, "close", "70%/close"), (50, "close", "50%/close"),
                                  (50, "1530", "50%/3:30"), (70, "1545", "70%/3:45")]:
        print(f"\n    Mechanics: {mech_label}")
        print(f"    {'Config':<20} {'Trades':>6} {'Total $':>14} {'WR':>7} {'PF':>7} "
              f"{'MaxDD':>13} {'Avg/Trd':>11} {'AvgTranches':>12}")
        print(f"    {'─'*110}")

        for interval_label, checkpoints in [("30m", CHECKPOINTS_30M), ("60m", CHECKPOINTS_60M)]:
            for n_t in [1, 2, 3, 4, 5, 6, 8, 10]:
                r = eval_tranche_strategy(sub, n_t, checkpoints, interval_label,
                                          tgt, ts, filter_label=filt_name)
                if r:
                    print_result(r)
            print()


# ════════════════════════════════════════════════════════════════════════════
# SECTION 3: Find the absolute best combo
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  SECTION 3: TOP OVERALL COMBOS (filter + mechanics + tranches)")
print(f"{'='*160}")

all_results = []
for filt_name, mask in filter_sets.items():
    sub = go[mask]
    if len(sub) < 5:
        continue
    for tgt, ts, _ in mechanics:
        for interval_label, checkpoints in [("30m", CHECKPOINTS_30M), ("60m", CHECKPOINTS_60M)]:
            for n_t in [1, 2, 3, 4, 5, 6, 8, 10]:
                r = eval_tranche_strategy(sub, n_t, checkpoints, interval_label,
                                          tgt, ts, filter_label=filt_name)
                if r:
                    all_results.append(r)

# Top by PF (min 15 trades)
top_pf = [r for r in all_results if r["n"] >= 15]
top_pf.sort(key=lambda x: -x["pf"])
print(f"\n  Top 20 by Profit Factor (min 15 trades):")
print(f"    {'Filter':<40} {'Config':<20} {'Trades':>6} {'Total $':>14} {'WR':>7} {'PF':>7} "
      f"{'MaxDD':>13} {'Avg/Trd':>11} {'AvgTr':>6}")
print(f"    {'─'*140}")
for r in top_pf[:20]:
    print(f"    {r['filter']:<40} {r['n_tranches']}T/{r['interval']} tgt={r['target']}%/ts={r['time_stop']:<5} "
          f"{r['n']:>6} ${r['total']:>+13,.0f} {r['wr']:>6.1f}% {r['pf']:>6.2f} "
          f"${r['dd']:>+12,.0f} ${r['avg']:>+10,.0f} {r['avg_tranches_entered']:>5.1f}")

# Top by total $
top_tot = [r for r in all_results if r["n"] >= 15]
top_tot.sort(key=lambda x: -x["total"])
print(f"\n  Top 20 by Total $ (min 15 trades):")
print(f"    {'Filter':<40} {'Config':<20} {'Trades':>6} {'Total $':>14} {'WR':>7} {'PF':>7} "
      f"{'MaxDD':>13} {'Avg/Trd':>11} {'AvgTr':>6}")
print(f"    {'─'*140}")
for r in top_tot[:20]:
    print(f"    {r['filter']:<40} {r['n_tranches']}T/{r['interval']} tgt={r['target']}%/ts={r['time_stop']:<5} "
          f"{r['n']:>6} ${r['total']:>+13,.0f} {r['wr']:>6.1f}% {r['pf']:>6.2f} "
          f"${r['dd']:>+12,.0f} ${r['avg']:>+10,.0f} {r['avg_tranches_entered']:>5.1f}")

# Top by Calmar (total / abs(dd))
top_calmar = [r for r in all_results if r["n"] >= 15 and r["dd"] < -1000]
for r in top_calmar:
    r["calmar"] = r["total"] / abs(r["dd"]) if r["dd"] < 0 else 0
top_calmar.sort(key=lambda x: -x["calmar"])
print(f"\n  Top 20 by Calmar ratio (total$/maxDD, min 15 trades):")
print(f"    {'Filter':<40} {'Config':<20} {'Trades':>6} {'Total $':>14} {'Calmar':>8} {'PF':>7} "
      f"{'MaxDD':>13} {'AvgTr':>6}")
print(f"    {'─'*140}")
for r in top_calmar[:20]:
    print(f"    {r['filter']:<40} {r['n_tranches']}T/{r['interval']} tgt={r['target']}%/ts={r['time_stop']:<5} "
          f"{r['n']:>6} ${r['total']:>+13,.0f} {r['calmar']:>7.2f} {r['pf']:>6.2f} "
          f"${r['dd']:>+12,.0f} {r['avg_tranches_entered']:>5.1f}")


# ════════════════════════════════════════════════════════════════════════════
# SECTION 4: Robustness check on top combos
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*160}")
print(f"  SECTION 4: ROBUSTNESS — Split-half test on top 15 combos")
print(f"{'='*160}")

mid = go["date"].median()
h1 = go[go["date"] <= mid]
h2 = go[go["date"] > mid]
print(f"  H1: {h1['date'].min().strftime('%Y-%m-%d')} to {h1['date'].max().strftime('%Y-%m-%d')} ({len(h1)} trades)")
print(f"  H2: {h2['date'].min().strftime('%Y-%m-%d')} to {h2['date'].max().strftime('%Y-%m-%d')} ({len(h2)} trades)")

# Take top 15 by PF with enough trades
robust_candidates = top_pf[:15]
print(f"\n  {'Filter':<40} {'Config':<18} {'H1':>30} {'H2':>30} {'Robust':>7}")
print(f"  {'─'*140}")

for r in robust_candidates:
    filt_name = r["filter"]
    mask = filter_sets.get(filt_name, pd.Series(True, index=go.index))

    mask_h1 = mask.reindex(h1.index, fill_value=False)
    mask_h2 = mask.reindex(h2.index, fill_value=False)

    interval_cps = CHECKPOINTS_30M if r["interval"] == "30m" else CHECKPOINTS_60M

    r1 = eval_tranche_strategy(h1[mask_h1], r["n_tranches"], interval_cps, r["interval"],
                                r["target"], r["time_stop"])
    r2 = eval_tranche_strategy(h2[mask_h2], r["n_tranches"], interval_cps, r["interval"],
                                r["target"], r["time_stop"])

    def qs(rr):
        if rr is None or rr["n"] < 3:
            return "  <3 trades       "
        return f"{rr['n']:>3}t ${rr['total']:>+10,.0f} PF={rr['pf']:.2f}"

    robust = "✓" if (r1 and r2 and r1["n"] >= 3 and r2["n"] >= 3
                     and r1["pf"] > 1.0 and r2["pf"] > 1.0) else "✗"
    config = f"{r['n_tranches']}T/{r['interval']} {r['target']}%/{r['time_stop']}"
    print(f"  {filt_name:<40} {config:<18} {qs(r1):>30} | {qs(r2):>30}  {robust}")


print(f"\n{'='*160}")
print(f"  DONE")
print(f"{'='*160}")
