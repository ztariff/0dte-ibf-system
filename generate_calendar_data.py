#!/usr/bin/env python3
"""
Generate trade data JSON for the interactive calendar.
Runs all 10 strategies with VIX tiered sizing across the full dataset.
Outputs: calendar_trades.json
"""

import sys, os, json, math, csv
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research.data import DataUniverse
from research.exits import profit_target, time_stop, wing_stop, loss_stop, standard_exits
from research.sweep import run_sweep, ibf_factory, ic_factory
from sizing_scores import SCORE_FUNCTIONS, score_to_multiplier, compute_sizing

universe = DataUniverse()
universe.load(load_quotes=False)
all_dates = universe.trading_dates()

# Load CSV regime data for scoring context
_DIR = os.path.dirname(os.path.abspath(__file__))
csv_by_date = {}
csv_path = os.path.join(_DIR, "research_all_trades.csv")
if os.path.exists(csv_path):
    with open(csv_path, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("date", "")[:10]
            csv_by_date[d] = row

# Load VIX9D data for term structure
vix9d_data = {}
vix9d_path = os.path.join(_DIR, "vix9d_daily.json")
if os.path.exists(vix9d_path):
    with open(vix9d_path) as f:
        vix9d_data = json.load(f)

def _build_scoring_context(date, vix, entry_credit=None, structure_name=None):
    """Build scoring context dict from CSV regime data + trade info."""
    csv_row = csv_by_date.get(date, {})
    ctx = {}
    # From CSV
    ctx["prior_dir"] = csv_row.get("prior_day_direction", "")
    try: ctx["prior_1d"] = float(csv_row["prior_day_return"])
    except: ctx["prior_1d"] = None
    try: ctx["prior_5d"] = float(csv_row["prior_5d_return"])
    except: ctx["prior_5d"] = None
    try: ctx["rv"] = float(csv_row["rv"])
    except: ctx["rv"] = None
    ctx["vix"] = vix
    ctx["rv_slope"] = csv_row.get("rv_slope", "")
    try: ctx["vp_ratio"] = float(csv_row["vp_ratio"])
    except: ctx["vp_ratio"] = None
    try: ctx["prior_day_range"] = float(csv_row["prior_day_range"])
    except: ctx["prior_day_range"] = None
    iwr = csv_row.get("in_prior_week_range", "")
    if iwr and iwr not in ("", "nan"):
        try: ctx["in_prior_week_range"] = bool(int(float(iwr)))
        except: ctx["in_prior_week_range"] = None
    else:
        ctx["in_prior_week_range"] = None
    # Gap
    try: ctx["gap_pct"] = float(csv_row.get("gap_pct", 0))
    except: ctx["gap_pct"] = None
    # DOW
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        ctx["dow"] = dt.strftime("%A")
    except: ctx["dow"] = ""
    # Term structure from VIX9D/VIX
    v9d = vix9d_data.get(date)
    if v9d is not None and vix is not None and vix > 0:
        try:
            ratio = float(v9d) / vix
            ctx["vix9d_vix_ratio"] = ratio
            ctx["ts_label"] = "INVERTED" if ratio < 0.90 else ("CONTANGO" if ratio > 1.02 else "FLAT")
        except:
            ctx["ts_label"] = ""
            ctx["vix9d_vix_ratio"] = None
    else:
        ctx["ts_label"] = ""
        ctx["vix9d_vix_ratio"] = None
    # Credit/wing % for Morning Decel
    if entry_credit is not None and structure_name:
        try:
            parts = structure_name.split("_")
            wing = int(parts[-1].replace("w", ""))
            ctx["credit_wing_pct"] = float(entry_credit) / wing if wing > 0 else None
        except:
            ctx["credit_wing_pct"] = None
    else:
        ctx["credit_wing_pct"] = None
    return ctx

def vix_size(date):
    vix = universe.ctx(date, 'vix_prior_close')
    if vix is None: return 1.0
    if vix < 20: return 1.0
    if vix < 25: return 0.5
    return 0.25

# Risk budgets from grading model (score/100 → risk allocation)
# Grade S=$150K, A=$100K, B+=$75K, B=$50K, C+=$35K, C=$20K
# VIX tiered sizing applied ON TOP of this budget

strategies = [
    # (name, short, color, structure_fn, entry_times, exit_fn, pre_filter, intra_filter, risk_budget, grade)
    # Per-strategy stop rules optimized via test_stop_variants.py on 776-day backtest
    ("Phoenix 75 Power Close", "PHX-PC", "#8b5cf6", ibf_factory(75), ["15:15"],
     lambda: [profit_target(0.50), loss_stop(0.70), time_stop('15:30')], None, None, 150000, "S"),
    ("Phoenix 75 Last Hour", "PHX-LH", "#6366f1", ibf_factory(75), ["15:00"],
     lambda: [profit_target(0.50), loss_stop(0.50), time_stop('15:30')], None, None, 100000, "A"),
    ("Firebird 60 Last Hour", "FBD-LH", "#14b8a6", ibf_factory(60), ["15:00"],
     lambda: [profit_target(0.50), loss_stop(0.50), time_stop('15:30')], None, None, 100000, "A"),
    ("Phoenix 75 Afternoon", "PHX-AFT", "#a855f7", ibf_factory(75), ["14:30"],
     lambda: [profit_target(0.50), loss_stop(0.50), time_stop('15:30')], None, None, 75000, "B+"),
    ("Ironclad 35 Condor", "IC-35", "#10b981", ic_factory(35, 35), ["14:30"],
     lambda: [profit_target(0.40), wing_stop(), time_stop('15:30')], None, None, 75000, "B+"),
    ("Firebird 60 Final Bell", "FBD-FB", "#0ea5e9", ibf_factory(60), ["15:30"],
     lambda: [profit_target(0.50), wing_stop(), time_stop('15:30')], None, None, 75000, "B+"),
    ("Phoenix 75 Early Afternoon", "PHX-EA", "#f59e0b", ibf_factory(75), ["13:45"],
     lambda: [profit_target(0.50), loss_stop(0.50), time_stop('15:30')], None, None, 50000, "B"),
    ("Phoenix 75 Midday", "PHX-MD", "#ec4899", ibf_factory(75), ["14:00"],
     lambda: [profit_target(0.50), loss_stop(0.50), time_stop('15:30')], None, None, 35000, "C+"),
    ("Firebird 60 Midday", "FBD-MD", "#f97316", ibf_factory(60), ["14:00"],
     lambda: [profit_target(0.50), loss_stop(0.70), time_stop('15:30')], None, None, 35000, "C+"),
    ("Morning Decel Scalp", "AM-DEC", "#64748b", ibf_factory(75), ["10:30"],
     lambda: [profit_target(0.30), time_stop('11:30')], None,
     lambda d, t: (universe.spx_acceleration(d, t, 10) or 0) < -0.05, 20000, "C"),
]

print("Generating trade data for calendar...")

all_trades = []

for sname, short, color, sfn, et, exit_fn, pre_fn, intra_fn, risk_budget, grade in strategies:
    print(f"  {sname} (Grade {grade}, ${risk_budget:,} risk)...")
    trades = run_sweep(universe, sfn, et, exit_fn, dates=all_dates, slippage=1.0,
                      pre_filter=pre_fn, intra_filter=intra_fn)

    for t in trades:
        vix = universe.ctx(t.date, 'vix_prior_close')
        mult = vix_size(t.date)
        spx_open = universe.spx_at(t.date, "09:31")

        # Position sizing: contracts = (risk_budget * vix_mult) / (max_risk_per_spread * 100)
        max_risk_per_spread = t.max_risk
        sized_budget = risk_budget * mult
        contracts = max(1, int(sized_budget / (max_risk_per_spread * 100))) if max_risk_per_spread > 0 else 1

        # Get SPX bars for chart
        spx_bars = universe.spx_bars_range(t.date, "09:30", "16:00")

        # Get entry/exit SPX prices
        entry_spx = universe.spx_at(t.date, t.entry_time)
        exit_spx = universe.spx_at(t.date, t.exit_time)

        # ATM and wing strikes from the structure
        atm = universe.current_atm(t.date, t.entry_time)

        # Build the intraday P&L timeline if available
        pnl_timeline = {}
        for time_str, pnl_val in t.pnl_timeline.items():
            pnl_timeline[time_str] = round(pnl_val * 100, 2)  # per-spread in dollars

        hold_min = 0
        try:
            eh, em = int(t.entry_time[:2]), int(t.entry_time[3:])
            xh, xm = int(t.exit_time[:2]), int(t.exit_time[3:])
            hold_min = (xh * 60 + xm) - (eh * 60 + em)
        except:
            pass

        # Compute sizing score
        scoring_ctx = _build_scoring_context(t.date, vix, t.entry_credit, t.structure_name)
        score_fn = SCORE_FUNCTIONS.get(sname)
        if score_fn is not None:
            sizing_score = score_fn(scoring_ctx)
            sizing_mult = score_to_multiplier(sizing_score, sname)
        else:
            sizing_score = 0
            sizing_mult = 1.0

        # Apply sizing score to budget
        scored_budget = sized_budget * sizing_mult
        scored_contracts = max(1, int(scored_budget / (max_risk_per_spread * 100))) if max_risk_per_spread > 0 else 1

        # Dollar P&L with full sizing: per-spread P&L * 100 * contracts
        pnl_dollar_sized = round(t.pnl_per_spread * 100 * scored_contracts, 0)
        risk_deployed = round(max_risk_per_spread * 100 * scored_contracts, 0)

        trade_record = {
            "date": t.date,
            "strategy": sname,
            "short_name": short,
            "color": color,
            "grade": grade,
            "structure": t.structure_name,
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "exit_type": t.exit_type,
            "entry_credit": round(t.entry_credit, 2),
            "pnl_per_spread": round(t.pnl_per_spread, 2),
            "contracts": scored_contracts,
            "risk_budget": risk_budget,
            "risk_deployed": risk_deployed,
            "pnl_dollar_sized": pnl_dollar_sized,
            "vix_sizing": mult,
            "sizing_score": sizing_score,
            "sizing_mult": sizing_mult,
            "vix": round(vix, 1) if vix else None,
            "hold_minutes": hold_min,
            "peak_pnl": round(t.peak_pnl * 100, 2),
            "trough_pnl": round(t.trough_pnl * 100, 2),
            "entry_spx": round(entry_spx, 2) if entry_spx else None,
            "exit_spx": round(exit_spx, 2) if exit_spx else None,
            "atm": atm,
            "pnl_timeline": pnl_timeline,
        }
        all_trades.append(trade_record)

# Build SPX daily bar data — aggregate 1-min bars into proper 5-min candles
spx_chart_data = {}
for date in all_dates:
    bars = universe.spx_bars_range(date, "09:30", "16:01")
    if not bars:
        continue
    # Aggregate into 5-min candles
    candles_5m = []
    i = 0
    while i < len(bars):
        # Take up to 5 bars for one candle
        bucket = bars[i:i+5]
        if not bucket:
            break
        candle = {
            "t": bucket[0][0],  # time of first bar in bucket
            "o": round(bucket[0][1]["o"], 2),
            "h": round(max(b["h"] for _, b in bucket), 2),
            "l": round(min(b["l"] for _, b in bucket), 2),
            "c": round(bucket[-1][1]["c"], 2),
        }
        candles_5m.append(candle)
        i += 5
    spx_chart_data[date] = candles_5m

# Sort trades by date
all_trades.sort(key=lambda x: (x["date"], x["entry_time"]))

# Strategy metadata
strategy_meta = []
for sname, short, color, *_ in strategies:
    strategy_meta.append({"name": sname, "short": short, "color": color})

output = {
    "generated": "2026-03-22",
    "strategies": strategy_meta,
    "trades": all_trades,
    "spx_bars": spx_chart_data,
}

outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_trades.json")
with open(outpath, "w") as f:
    json.dump(output, f)

print(f"\nDone: {len(all_trades)} trades across {len(set(t['date'] for t in all_trades))} unique dates")
print(f"Saved to {outpath} ({os.path.getsize(outpath) / 1024 / 1024:.1f} MB)")
