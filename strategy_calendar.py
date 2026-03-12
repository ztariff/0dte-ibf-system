"""
STRATEGY CALENDAR — Per-day P&L for every v3-v14 strategy.
Shows DOLLAR P&L assuming $100K daily risk budget with correct sizing:
  - V3 (PHOENIX): Tiered by confluence (1=$25K, 2=$50K, 3=$75K, 4+=$100K)
  - V4-V14: Full $100K risk budget

Usage:  python strategy_calendar.py
Output: strategy_calendar.html
"""

import os, json, math, calendar
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))

DAILY_RISK = 100_000
SLIPPAGE_PER_SPREAD = 100  # $1.00 * 100 multiplier
SPX_MULT = 100

# ═══════════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS  (from cockpit_feed.py)
# ═══════════════════════════════════════════════════════════════════════
STRATEGIES = [
    {"ver":"v3","regime":"PHOENIX","entry":"10:00","mech":"50%/close/1T","filter":None,
     "color":"#f59e0b","vix":None,"pd":None,"rng":None,"gap":None},
    {"ver":"v4","regime":"MID_DN_IN_GFL","entry":"10:00","mech":"40%/close/1T","filter":None,
     "color":"#3b82f6","vix":[15,20],"pd":"DN","rng":"IN","gap":"GFL"},
    {"ver":"v5","regime":"MID_UP_OT_GUP","entry":"10:00","mech":"40%/close/1T","filter":"5dRet>0",
     "color":"#22c55e","vix":[15,20],"pd":"UP","rng":"OT","gap":"GUP"},
    {"ver":"v6","regime":"LOW_DN_IN_GFL","entry":"10:00","mech":"50%/1530/1T","filter":"VP<=1.7",
     "color":"#06b6d4","vix":[0,15],"pd":"DN","rng":"IN","gap":"GFL"},
    {"ver":"v7","regime":"LOW_FL_IN_GUP","entry":"10:00","mech":"40%/close/1T","filter":None,
     "color":"#a855f7","vix":[0,15],"pd":"FL","rng":"IN","gap":"GUP"},
    {"ver":"v8","regime":"ELEV_UP_IN_GDN","entry":"10:30","mech":"40%/1530/1T","filter":None,
     "color":"#ef4444","vix":[20,25],"pd":"UP","rng":"IN","gap":"GDN"},
    {"ver":"v9","regime":"MID_UP_OT_GFL","entry":"10:00","mech":"70%/1545/1T","filter":"!RISING",
     "color":"#eab308","vix":[15,20],"pd":"UP","rng":"OT","gap":"GFL"},
    {"ver":"v10","regime":"MID_DN_OT_GFL","entry":"11:00","mech":"70%/1545/1T","filter":None,
     "color":"#ec4899","vix":[15,20],"pd":"DN","rng":"OT","gap":"GFL"},
    {"ver":"v11","regime":"LOW_UP_OT_GFL","entry":"10:00","mech":"70%/close/1T","filter":"VP<=2.0",
     "color":"#14b8a6","vix":[0,15],"pd":"UP","rng":"OT","gap":"GFL"},
    {"ver":"v12","regime":"LOW_UP_OT_GUP","entry":"10:00","mech":"40%/close/1T","filter":"5dRet>1",
     "color":"#f97316","vix":[0,15],"pd":"UP","rng":"OT","gap":"GUP"},
    {"ver":"v13","regime":"LOW_DN_IN_GUP","entry":"10:30","mech":"40%/close/1T","filter":"Rng<=0.3",
     "color":"#8b5cf6","vix":[0,15],"pd":"DN","rng":"IN","gap":"GUP"},
    {"ver":"v14","regime":"MID_DN_IN_GDN","entry":"10:00","mech":"50%/close/1T","filter":"ScoreVol<18",
     "color":"#64748b","vix":[15,20],"pd":"DN","rng":"IN","gap":"GDN"},
]

# ═══════════════════════════════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════════════════════════════
print("Loading data...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
df["date"] = pd.to_datetime(df["date"])
print(f"  {len(df)} rows loaded")

with open(os.path.join(_DIR, "spx_gap_cache.json")) as f:
    gap_data = json.load(f)

# Derive entry credit and max_loss_per_spread from the backtest data
df["ml_per_spread"] = df["risk_deployed_p1"] / df["n_spreads_p1"]
df["entry_credit"] = df["wing_width"] - df["ml_per_spread"] / SPX_MULT

# ═══════════════════════════════════════════════════════════════════════
# REGIME + FILTER CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════
def classify_gap(date_val):
    d_str = date_val.strftime("%Y-%m-%d") if hasattr(date_val, "strftime") else str(date_val)[:10]
    gp = gap_data.get(d_str, None)
    if gp is None: return "UNK"
    if gp < -0.25: return "GDN"
    elif gp > 0.25: return "GUP"
    else: return "GFL"

def classify_direction(d):
    d = str(d).strip().upper()
    if d == "UP": return "UP"
    elif d == "DOWN": return "DN"
    else: return "FL"

def check_filter(row, filt):
    if filt is None: return True
    if filt == "5dRet>0": return row.get("prior_5d_return", 0) > 0
    if filt == "5dRet>1": return row.get("prior_5d_return", 0) > 1.0
    if filt == "VP<=1.7": return row.get("vp_ratio", 99) <= 1.7
    if filt == "VP<=2.0": return row.get("vp_ratio", 99) <= 2.0
    if filt == "!RISING": return str(row.get("rv_slope", "")) != "RISING"
    if filt == "ScoreVol<18": return row.get("score_vol", 99) < 18
    if filt == "Rng<=0.3": return row.get("range_pct", 99) <= 0.3
    return True

def phoenix_fire_count(row):
    vix = row.get("vix", 99)
    vp = row.get("vp_ratio", 99)
    ret5d = row.get("prior_5d_return", -99)
    pd_dir = str(row.get("prior_day_direction", ""))
    rv_slope = str(row.get("rv_slope", ""))
    rv_chg = row.get("rv_1d_change", -99) if pd.notna(row.get("rv_1d_change")) else -99
    in_wk = row.get("in_prior_week_range", 1)

    count = 0
    if vix <= 20 and vp <= 1.0 and ret5d > 0: count += 1
    if vp <= 1.3 and pd_dir == "DOWN" and ret5d > 0: count += 1
    if vp <= 1.2 and ret5d > 0 and rv_chg > 0: count += 1
    if vp <= 1.5 and in_wk == 0 and ret5d > 0: count += 1
    if vp <= 1.3 and rv_slope != "RISING" and ret5d > 0: count += 1
    return count

def phoenix_sizing(fire_count):
    if fire_count == 0: return 0
    if fire_count == 1: return 25_000
    if fire_count == 2: return 50_000
    if fire_count == 3: return 75_000
    return 100_000

def match_strategy(row, strat):
    if strat["ver"] == "v3":
        return phoenix_fire_count(row) >= 1
    if strat["vix"]:
        vix = row.get("vix", 0)
        if vix < strat["vix"][0] or vix >= strat["vix"][1]: return False
    if strat["pd"]:
        if classify_direction(row.get("prior_day_direction", "")) != strat["pd"]: return False
    if strat["rng"]:
        rng = "IN" if row.get("in_prior_week_range", 1) == 1 else "OT"
        if rng != strat["rng"]: return False
    if strat["gap"]:
        if classify_gap(row["date"]) != strat["gap"]: return False
    if not check_filter(row, strat["filter"]): return False
    return True

# ═══════════════════════════════════════════════════════════════════════
# P&L COMPUTATION per strategy mechanics
# ═══════════════════════════════════════════════════════════════════════
def compute_strategy_pnl(row, strat):
    """
    Compute per-spread P&L for a strategy on a given day.
    Returns (pnl_per_spread, exit_type, exit_time).
    """
    parts = strat["mech"].split("/")
    target_pct = int(parts[0].replace("%", ""))
    time_stop = parts[1]
    entry_time = strat["entry"].replace(":", "")

    # Target hit columns
    target_col = f"hit_{target_pct}_pnl"
    target_time_col = f"hit_{target_pct}_time"

    # Wing stop
    ws_pnl = row.get("ws_pnl")
    ws_time = row.get("ws_time")
    has_ws = pd.notna(ws_pnl) and ws_pnl != "" and ws_pnl != 0

    # Target hit
    target_hit_pnl = row.get(target_col)
    target_hit_time = row.get(target_time_col)
    has_target = pd.notna(target_hit_pnl) and target_hit_pnl != ""

    # Time stop column
    ts_map = {"close": ("pnl_at_close", "1600"), "1545": ("pnl_at_1545", "1545"), "1530": ("pnl_at_1530", "1530")}
    ts_col, ts_hhmm = ts_map.get(time_stop, ("pnl_at_close", "1600"))

    def time_to_hhmm(t):
        if pd.isna(t) or t == "": return None
        try:
            ts = pd.Timestamp(t)
            return f"{ts.hour:02d}{ts.minute:02d}"
        except:
            return None

    target_hhmm = time_to_hhmm(target_hit_time)
    ws_hhmm = time_to_hhmm(ws_time)

    target_valid = has_target and target_hhmm and target_hhmm >= entry_time and target_hhmm <= ts_hhmm
    ws_valid = has_ws and ws_hhmm and ws_hhmm >= entry_time and ws_hhmm <= ts_hhmm

    if ws_valid and target_valid:
        if ws_hhmm <= target_hhmm:
            return float(ws_pnl), "WS", ws_hhmm
        else:
            return float(target_hit_pnl), "TGT", target_hhmm
    elif ws_valid:
        return float(ws_pnl), "WS", ws_hhmm
    elif target_valid:
        return float(target_hit_pnl), "TGT", target_hhmm
    else:
        ts_pnl = row.get(ts_col)
        if pd.notna(ts_pnl):
            return float(ts_pnl), "TS", ts_hhmm
        close_pnl = row.get("pnl_at_close", 0)
        return float(close_pnl) if pd.notna(close_pnl) else 0, "EXP", "1600"

# ═══════════════════════════════════════════════════════════════════════
# BUILD CALENDAR DATA
# ═══════════════════════════════════════════════════════════════════════
print("Computing per-strategy dollar P&L (100K risk budget)...", flush=True)

calendar_data = {}
strategy_stats = {s["ver"]: {"wins": 0, "losses": 0, "total_pnl": 0, "days": 0, "cum_pnl": []} for s in STRATEGIES}
combined_daily = {}  # date -> total dollar PnL across all strategies

for _, row in df.iterrows():
    d_str = row["date"].strftime("%Y-%m-%d")
    day_data = {}
    ml_per_spread = row["ml_per_spread"]  # max loss per spread in dollars

    if pd.isna(ml_per_spread) or ml_per_spread <= 0:
        continue

    for strat in STRATEGIES:
        if not match_strategy(row, strat):
            continue

        # Determine risk budget for this strategy
        if strat["ver"] == "v3":
            fc = phoenix_fire_count(row)
            risk_budget = phoenix_sizing(fc)
            if risk_budget == 0:
                continue
        else:
            risk_budget = DAILY_RISK

        # Number of contracts
        qty = int(risk_budget // ml_per_spread)
        if qty <= 0:
            continue

        # Per-spread P&L (in option points)
        pnl_ps, exit_type, exit_time = compute_strategy_pnl(row, strat)

        # Dollar P&L = qty * (pnl_per_spread * 100 - slippage)
        dollar_pnl = qty * (pnl_ps * SPX_MULT - SLIPPAGE_PER_SPREAD)

        day_data[strat["ver"]] = {
            "pnl": round(dollar_pnl),
            "qty": qty,
            "exit": exit_type,
            "exit_time": exit_time,
            "pnl_ps": round(pnl_ps, 2),
            "fire_count": phoenix_fire_count(row) if strat["ver"] == "v3" else None,
            "risk_budget": risk_budget,
        }
        st = strategy_stats[strat["ver"]]
        st["days"] += 1
        st["total_pnl"] += dollar_pnl
        if dollar_pnl >= 0:
            st["wins"] += 1
        else:
            st["losses"] += 1

    if day_data:
        calendar_data[d_str] = day_data
        combined_daily[d_str] = sum(t["pnl"] for t in day_data.values())

# Compute cumulative P&L per strategy
cum_by_strat = {s["ver"]: 0 for s in STRATEGIES}
for d_str in sorted(calendar_data.keys()):
    for ver, trade in calendar_data[d_str].items():
        cum_by_strat[ver] += trade["pnl"]
        trade["cum_pnl"] = cum_by_strat[ver]

print(f"  {len(calendar_data)} days with at least one strategy trading\n")
print(f"  {'Strat':<6} {'Days':>5} {'WR':>5} {'Total $':>12} {'Avg $':>10} {'Avg Qty':>8}")
print(f"  {'-----':<6} {'----':>5} {'--':>5} {'-------':>12} {'-----':>10} {'-------':>8}")
for s in STRATEGIES:
    st = strategy_stats[s["ver"]]
    if st["days"] == 0: continue
    wr = st["wins"] / st["days"] * 100
    avg = st["total_pnl"] / st["days"]
    # Compute avg qty
    strat_days = [calendar_data[d][s["ver"]] for d in sorted(calendar_data.keys()) if s["ver"] in calendar_data[d]]
    avg_qty = sum(t["qty"] for t in strat_days) / len(strat_days)
    print(f"  {s['ver'].upper():<6} {st['days']:>5} {wr:>4.0f}% ${st['total_pnl']:>11,} ${avg:>9,.0f} {avg_qty:>7.1f}")

total_all = sum(st["total_pnl"] for st in strategy_stats.values())
print(f"\n  COMBINED TOTAL: ${total_all:,.0f}")

# ═══════════════════════════════════════════════════════════════════════
# GENERATE HTML CALENDAR
# ═══════════════════════════════════════════════════════════════════════
print("\nGenerating HTML calendar...", flush=True)

all_dates = sorted(calendar_data.keys())
first_date = pd.Timestamp(all_dates[0])
last_date = pd.Timestamp(all_dates[-1])

months = []
current = first_date.replace(day=1)
while current <= last_date:
    months.append((current.year, current.month))
    if current.month == 12:
        current = current.replace(year=current.year + 1, month=1)
    else:
        current = current.replace(month=current.month + 1)

color_map = {s["ver"]: s["color"] for s in STRATEGIES}
ver_list = [s["ver"] for s in STRATEGIES]

html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Strategy Calendar -- v3-v14 Dollar P&L ($100K Risk)</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0f; color: #e0e0e0; font-family: 'Consolas', 'Monaco', monospace; padding: 20px; }
h1 { color: #f59e0b; font-size: 20px; margin-bottom: 4px; }
.subtitle { color: #888; font-size: 11px; margin-bottom: 16px; }
.legend { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; padding: 10px; background: #111118; border-radius: 6px; border: 1px solid #222; }
.legend-item { display: flex; align-items: center; gap: 4px; font-size: 10px; }
.legend-dot { width: 10px; height: 10px; border-radius: 2px; }
.stats-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 11px; }
.stats-table th { text-align: left; padding: 6px 10px; border-bottom: 2px solid #333; color: #888; font-weight: normal; text-transform: uppercase; font-size: 9px; letter-spacing: 1px; }
.stats-table td { padding: 5px 10px; border-bottom: 1px solid #1a1a22; }
.stats-table tr:hover { background: #111118; }
.month-section { margin-bottom: 28px; }
.month-title { font-size: 14px; color: #ccc; margin-bottom: 8px; padding: 4px 10px; background: #111118; border-radius: 4px; display: inline-block; }
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
.cal-hdr { font-size: 9px; color: #666; text-align: center; padding: 4px; }
.cal-day { background: #0f0f16; border: 1px solid #1a1a22; border-radius: 4px; min-height: 68px; padding: 3px; font-size: 9px; }
.cal-day.empty { background: transparent; border-color: transparent; min-height: 20px; }
.cal-day.has-trades { border-color: #333; }
.cal-day .day-num { color: #666; font-size: 10px; margin-bottom: 2px; }
.cal-day .day-num.weekend { color: #333; }
.strat-tag { display: inline-block; padding: 1px 4px; border-radius: 2px; font-size: 8px; font-weight: bold; margin: 1px; line-height: 1.4; white-space: nowrap; }
.strat-tag.win { opacity: 1; }
.strat-tag.loss { opacity: 0.7; }
.pnl-pos { color: #22c55e; }
.pnl-neg { color: #ef4444; }
.day-total { font-size: 9px; font-weight: bold; margin-top: 2px; padding-top: 2px; border-top: 1px solid #222; }
.sizing-note { font-size: 8px; color: #666; }
</style>
</head>
<body>
<h1>STRATEGY CALENDAR -- v3-v14 Dollar P&L</h1>
<div class="subtitle">$100K daily risk budget | V3 tiered by confluence (1=$25K, 2=$50K, 3=$75K, 4+=$100K) | $1/spread slippage</div>

<div class="legend">
"""

for s in STRATEGIES:
    html += f'<div class="legend-item"><div class="legend-dot" style="background:{s["color"]}"></div>{s["ver"].upper()}</div>\n'

html += '</div>\n'

# Stats summary table
html += '<table class="stats-table">\n'
html += '<tr><th>Strategy</th><th>Regime</th><th>Mech</th><th>Filter</th><th>Days</th><th>W/L</th><th>Win Rate</th><th>Total P&L</th><th>Avg P&L</th></tr>\n'
for s in STRATEGIES:
    st = strategy_stats[s["ver"]]
    if st["days"] == 0: continue
    wr = st["wins"] / st["days"] * 100
    avg = st["total_pnl"] / st["days"]
    pnl_class = "pnl-pos" if st["total_pnl"] >= 0 else "pnl-neg"
    html += f'<tr><td style="color:{s["color"]};font-weight:bold">{s["ver"].upper()}</td>'
    html += f'<td>{s["regime"]}</td><td>{s["mech"]}</td><td>{s["filter"] or "--"}</td>'
    html += f'<td>{st["days"]}</td><td>{st["wins"]}/{st["losses"]}</td>'
    html += f'<td>{wr:.0f}%</td>'
    html += f'<td class="{pnl_class}">${st["total_pnl"]:,.0f}</td>'
    html += f'<td class="{pnl_class}">${avg:,.0f}</td></tr>\n'

# Total row
html += f'<tr style="border-top:2px solid #444;font-weight:bold"><td colspan="7" style="color:#f59e0b">COMBINED</td>'
html += f'<td class="{"pnl-pos" if total_all >= 0 else "pnl-neg"}">${total_all:,.0f}</td><td></td></tr>\n'
html += '</table>\n'

# Month calendars
DOW_NAMES = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

def fmt_pnl(v):
    """Format dollar P&L compactly: +1.2K, -500, +23K"""
    if abs(v) >= 1000:
        return f"{'+' if v >= 0 else ''}{v/1000:.1f}K"
    else:
        return f"{'+' if v >= 0 else ''}{v:.0f}"

for year, month in months:
    month_name = calendar.month_name[month]

    # Month total
    month_total = 0
    for d_str, total in combined_daily.items():
        d = pd.Timestamp(d_str)
        if d.year == year and d.month == month:
            month_total += total

    pnl_cls = "pnl-pos" if month_total >= 0 else "pnl-neg"
    html += f'<div class="month-section">\n'
    html += f'<div class="month-title">{month_name} {year} <span class="{pnl_cls}" style="margin-left:12px">${month_total:,.0f}</span></div>\n'
    html += '<div class="cal-grid">\n'

    for d in DOW_NAMES:
        html += f'<div class="cal-hdr">{d}</div>\n'

    first_dow = datetime(year, month, 1).weekday()
    days_in_month = calendar.monthrange(year, month)[1]

    for _ in range(first_dow):
        html += '<div class="cal-day empty"></div>\n'

    for day in range(1, days_in_month + 1):
        d_str = f"{year}-{month:02d}-{day:02d}"
        dow = datetime(year, month, day).weekday()
        is_weekend = dow >= 5

        day_trades = calendar_data.get(d_str, {})
        has_trades = len(day_trades) > 0

        cls = "cal-day"
        if has_trades: cls += " has-trades"

        html += f'<div class="{cls}">\n'
        html += f'<div class="day-num{"" if not is_weekend else " weekend"}">{day}</div>\n'

        if has_trades:
            total_pnl = 0
            for ver in ver_list:
                if ver in day_trades:
                    t = day_trades[ver]
                    pnl = t["pnl"]
                    total_pnl += pnl
                    win_cls = "win" if pnl >= 0 else "loss"
                    sign_cls = "pnl-pos" if pnl >= 0 else "pnl-neg"
                    qty_str = f"{t['qty']}x"
                    fc_str = f" ({t['fire_count']}sig)" if t.get("fire_count") else ""
                    tooltip = f"{ver.upper()} {t['exit']}@{t['exit_time']} | {qty_str}{fc_str} | ${pnl:+,} (${t['risk_budget']/1000:.0f}K risk)"
                    html += f'<span class="strat-tag {win_cls}" style="background:{color_map[ver]}22;color:{color_map[ver]};border:1px solid {color_map[ver]}44" '
                    html += f'title="{tooltip}">'
                    html += f'{ver[1:]} <span class="{sign_cls}">{fmt_pnl(pnl)}</span>'
                    html += '</span>\n'

            sign_cls = "pnl-pos" if total_pnl >= 0 else "pnl-neg"
            html += f'<div class="day-total {sign_cls}">{fmt_pnl(total_pnl)}</div>\n'

        html += '</div>\n'

    remaining = (7 - (first_dow + days_in_month) % 7) % 7
    for _ in range(remaining):
        html += '<div class="cal-day empty"></div>\n'

    html += '</div></div>\n'

html += '</body></html>'

out_path = os.path.join(_DIR, "strategy_calendar.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"\nCalendar saved to: {out_path}")
