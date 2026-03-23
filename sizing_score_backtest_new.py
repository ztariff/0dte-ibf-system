#!/usr/bin/env python3
"""
Backtest sizing scores for the 10 new strategies.
1. Score every historical trade using sizing_scores.py
2. Compute quartile-based thresholds from score distributions
3. Compare original (flat) vs scored P&L, drawdown, return-to-drawdown
"""
import json, os, math
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load data ──────────────────────────────────────────────────────
with open(os.path.join(_DIR, "calendar_trades.json")) as f:
    cal = json.load(f)

trades = cal["trades"]

# Load CSV regime data for enrichment
import csv
csv_path = os.path.join(_DIR, "research_all_trades.csv")
csv_by_date = {}
with open(csv_path, newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        d = row.get("date", "")[:10]
        csv_by_date[d] = row

# Load VIX9D data
vix9d_path = os.path.join(_DIR, "vix9d_daily.json")
vix9d_data = {}
if os.path.exists(vix9d_path):
    with open(vix9d_path) as f:
        vix9d_data = json.load(f)

# Import scoring functions
from sizing_scores import SCORE_FUNCTIONS, score_to_multiplier, SCORE_THRESHOLDS

# ── Identify new strategies ────────────────────────────────────────
NEW_STRATS = [
    "Phoenix 75 Power Close", "Phoenix 75 Last Hour", "Phoenix 75 Midday",
    "Phoenix 75 Early Afternoon", "Phoenix 75 Afternoon",
    "Firebird 60 Final Bell", "Firebird 60 Last Hour", "Firebird 60 Midday",
    "Ironclad 35 Condor", "Morning Decel Scalp"
]

# ── Build scoring context for each trade ───────────────────────────
def build_context(t, csv_row):
    """Build scoring context from trade + CSV regime data."""
    d = t.get("date", "")[:10]
    ctx = {}

    # From CSV row
    if csv_row:
        ctx["prior_dir"] = csv_row.get("prior_day_direction", "")
        try: ctx["prior_1d"] = float(csv_row["prior_day_return"])
        except: ctx["prior_1d"] = None
        try: ctx["prior_5d"] = float(csv_row["prior_5d_return"])
        except: ctx["prior_5d"] = None
        try: ctx["rv"] = float(csv_row["rv"])
        except: ctx["rv"] = None
        try: ctx["vix"] = float(csv_row["vix"])
        except: ctx["vix"] = None
        ctx["rv_slope"] = csv_row.get("rv_slope", "")
        try: ctx["vp_ratio"] = float(csv_row["vp_ratio"])
        except: ctx["vp_ratio"] = None
        try: ctx["prior_day_range"] = float(csv_row["prior_day_range"])
        except: ctx["prior_day_range"] = None

        iwr = csv_row.get("in_prior_week_range", "")
        if iwr and iwr not in ("", "nan"):
            ctx["in_prior_week_range"] = bool(int(float(iwr)))
        else:
            ctx["in_prior_week_range"] = None

        # Term structure from VIX9D/VIX
        vix = ctx.get("vix")
        vix9d = vix9d_data.get(d)
        if vix9d is not None:
            try: vix9d = float(vix9d)
            except: vix9d = None
        if vix is not None and vix9d is not None and vix > 0:
            ratio = vix9d / vix
            ctx["vix9d_vix_ratio"] = ratio
            if ratio < 0.90:
                ctx["ts_label"] = "INVERTED"
            elif ratio > 1.02:
                ctx["ts_label"] = "CONTANGO"
            else:
                ctx["ts_label"] = "FLAT"
        else:
            ctx["ts_label"] = ""
            ctx["vix9d_vix_ratio"] = None

    # Gap from trade or CSV
    gap = t.get("gap_pct")
    if gap is None and csv_row:
        try: gap = float(csv_row.get("gap_pct", 0))
        except: gap = None
    ctx["gap_pct"] = gap

    # Day of week from date
    from datetime import datetime
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        ctx["dow"] = dt.strftime("%A")
    except:
        ctx["dow"] = ""

    # Credit/wing % — parse from structure if available
    # structure like "IBF_5600_75w" → wing=75
    structure = t.get("structure", "")
    credit = t.get("entry_credit") or t.get("credit")
    if structure and credit:
        try:
            parts = structure.split("_")
            wing = int(parts[-1].replace("w", ""))
            ctx["credit_wing_pct"] = float(credit) / wing if wing > 0 else None
        except:
            ctx["credit_wing_pct"] = None
    else:
        ctx["credit_wing_pct"] = None

    return ctx


# ── Score all trades ───────────────────────────────────────────────
scored_trades = defaultdict(list)  # strat_name -> [(pnl, score)]

for t in trades:
    sname = t.get("strategy", "") or t.get("strategy_name", "")
    if sname not in NEW_STRATS:
        continue

    pnl = t.get("pnl_dollar_sized") or t.get("pnl") or 0
    d = t.get("date", "")[:10]
    csv_row = csv_by_date.get(d)

    if csv_row is None:
        # Can't score without regime data
        scored_trades[sname].append((pnl, None))
        continue

    ctx = build_context(t, csv_row)
    fn = SCORE_FUNCTIONS.get(sname)
    if fn is None:
        scored_trades[sname].append((pnl, None))
        continue

    score = fn(ctx)
    scored_trades[sname].append((pnl, score))


# ── Compute quartile thresholds from score distributions ───────────
print("=" * 80)
print("  SCORE DISTRIBUTIONS & CALIBRATED THRESHOLDS")
print("=" * 80)

calibrated_thresholds = {}

for sname in NEW_STRATS:
    items = scored_trades.get(sname, [])
    scores = [s for (_, s) in items if s is not None]
    if not scores:
        print(f"\n{sname}: No scored trades")
        continue

    scores_sorted = sorted(scores)
    n = len(scores_sorted)
    q25 = scores_sorted[int(n * 0.25)]
    q50 = scores_sorted[int(n * 0.50)]
    q75 = scores_sorted[int(n * 0.75)]

    # Ensure thresholds are distinct and ascending
    if q50 <= q25:
        q50 = q25 + 1
    if q75 <= q50:
        q75 = q50 + 1

    calibrated_thresholds[sname] = (q25, q50, q75)

    print(f"\n{sname}  (n={n})")
    print(f"  Range: [{min(scores)}, {max(scores)}]")
    print(f"  Quartiles: 25%={q25}, 50%={q50}, 75%={q75}")
    print(f"  Thresholds: ({q25}, {q50}, {q75})")

    # Distribution histogram
    from collections import Counter
    hist = Counter(scores)
    for val in sorted(hist.keys()):
        bar = "#" * hist[val]
        print(f"    {val:+3d}: {bar} ({hist[val]})")


# ── Backtest: flat vs scored sizing ────────────────────────────────
print("\n" + "=" * 80)
print("  BACKTEST: FLAT vs SCORED SIZING")
print("=" * 80)

results = {}

for sname in NEW_STRATS:
    items = scored_trades.get(sname, [])
    if not items:
        continue

    # Flat sizing (original)
    flat_pnls = [pnl for (pnl, _) in items]
    flat_total = sum(flat_pnls)
    flat_dd = 0
    flat_peak = 0
    flat_cum = 0
    for p in flat_pnls:
        flat_cum += p
        if flat_cum > flat_peak:
            flat_peak = flat_cum
        dd = flat_peak - flat_cum
        if dd > flat_dd:
            flat_dd = dd

    # Scored sizing
    scored_pnls = []
    for (pnl, score) in items:
        if score is None:
            scored_pnls.append(pnl)  # no scoring data, use flat
            continue
        thresholds = calibrated_thresholds.get(sname)
        if thresholds is None:
            scored_pnls.append(pnl)
            continue
        t25, t50, t75 = thresholds
        if score <= t25:    mult = 0.25
        elif score <= t50:  mult = 0.50
        elif score <= t75:  mult = 0.75
        else:               mult = 1.00
        scored_pnls.append(pnl * mult)

    scored_total = sum(scored_pnls)
    scored_dd = 0
    scored_peak = 0
    scored_cum = 0
    for p in scored_pnls:
        scored_cum += p
        if scored_cum > scored_peak:
            scored_peak = scored_cum
        dd = scored_peak - scored_cum
        if dd > scored_dd:
            scored_dd = dd

    # Capital deployed ratio
    total_scored_mult = sum(
        (0.25 if s is not None and s <= calibrated_thresholds.get(sname, (0,0,0))[0]
         else 0.50 if s is not None and s <= calibrated_thresholds.get(sname, (0,0,0))[1]
         else 0.75 if s is not None and s <= calibrated_thresholds.get(sname, (0,0,0))[2]
         else 1.00)
        for (_, s) in items if s is not None
    )
    n_scored = sum(1 for (_, s) in items if s is not None)
    avg_mult = total_scored_mult / n_scored if n_scored > 0 else 1.0

    # Return-to-drawdown
    flat_r2d = flat_total / flat_dd if flat_dd > 0 else float('inf')
    scored_r2d = scored_total / scored_dd if scored_dd > 0 else float('inf')

    results[sname] = {
        "n": len(items),
        "n_scored": n_scored,
        "flat_total": flat_total,
        "flat_dd": flat_dd,
        "flat_r2d": flat_r2d,
        "scored_total": scored_total,
        "scored_dd": scored_dd,
        "scored_r2d": scored_r2d,
        "avg_mult": avg_mult,
        "pnl_delta_pct": (scored_total - flat_total) / abs(flat_total) * 100 if flat_total != 0 else 0,
        "dd_delta_pct": (scored_dd - flat_dd) / flat_dd * 100 if flat_dd > 0 else 0,
    }

    print(f"\n  {sname}  (n={len(items)}, scored={n_scored})")
    print(f"  {'':30s} {'FLAT':>12s}  {'SCORED':>12s}  {'DELTA':>10s}")
    print(f"  {'Total P&L':30s} ${flat_total:>11,.0f}  ${scored_total:>11,.0f}  {results[sname]['pnl_delta_pct']:+.1f}%")
    print(f"  {'Max Drawdown':30s} ${flat_dd:>11,.0f}  ${scored_dd:>11,.0f}  {results[sname]['dd_delta_pct']:+.1f}%")
    r2d_flat = f"{flat_r2d:.2f}" if flat_r2d != float('inf') else "inf"
    r2d_scored = f"{scored_r2d:.2f}" if scored_r2d != float('inf') else "inf"
    print(f"  {'Return/Drawdown':30s} {r2d_flat:>12s}  {r2d_scored:>12s}")
    print(f"  {'Avg Sizing Mult':30s} {'1.00':>12s}  {avg_mult:>12.2f}")

# ── Grand totals ───────────────────────────────────────────────────
print("\n" + "=" * 80)
print("  GRAND TOTALS (ALL 10 NEW STRATEGIES)")
print("=" * 80)

grand_flat = sum(r["flat_total"] for r in results.values())
grand_scored = sum(r["scored_total"] for r in results.values())
grand_flat_dd = max(r["flat_dd"] for r in results.values())
grand_scored_dd = max(r["scored_dd"] for r in results.values())
avg_mults = [r["avg_mult"] for r in results.values()]
grand_avg_mult = sum(avg_mults) / len(avg_mults) if avg_mults else 1.0

print(f"  {'':30s} {'FLAT':>12s}  {'SCORED':>12s}")
print(f"  {'Total P&L':30s} ${grand_flat:>11,.0f}  ${grand_scored:>11,.0f}  ({(grand_scored-grand_flat)/abs(grand_flat)*100:+.1f}%)")
print(f"  {'Worst Strategy Drawdown':30s} ${grand_flat_dd:>11,.0f}  ${grand_scored_dd:>11,.0f}")
print(f"  {'Avg Sizing Multiplier':30s} {'1.00':>12s}  {grand_avg_mult:>12.2f}")

# ── Output calibrated thresholds for copy into sizing_scores.py ────
print("\n" + "=" * 80)
print("  CALIBRATED THRESHOLDS (copy into sizing_scores.py)")
print("=" * 80)
for sname in NEW_STRATS:
    if sname in calibrated_thresholds:
        t = calibrated_thresholds[sname]
        print(f'    "{sname}": {t},')

# Save results
with open(os.path.join(_DIR, "sizing_score_backtest_new.json"), "w") as f:
    json.dump({"results": results, "thresholds": calibrated_thresholds}, f, indent=2, default=str)
print(f"\nResults saved to sizing_score_backtest_new.json")
