"""
TRANCHE COMPARISON ANALYSIS
============================
Compares 1T (single tranche) vs multi-tranche P&L for the 6 strategies
that currently use multi-tranche configs:

  V4  : MID_DN_IN_GFL  | 40%/close/5T30m
  V5  : MID_UP_OT_GUP  | 40%/close/5T60m  (filter: 5dRet>0)
  V10 : MID_DN_OT_GFL  | 70%/1545/5T60m
  V11 : LOW_UP_OT_GFL  | 70%/close/3T60m  (filter: VP<=2.0)
  V12 : LOW_UP_OT_GUP  | 40%/close/5T60m  (filter: 5dRet>1)
  V13 : LOW_DN_IN_GUP  | 40%/close/5T60m  (filter: Rng<=0.3)

Uses the EXACT same simulation logic as ensemble_v3.py for consistency.
Fetches SPX daily OHLC from Polygon to compute gap classification (GFL/GUP/GDN).

Usage:
    python3 tranche_comparison.py
"""

import os, json, sys
import pandas as pd
import numpy as np
import requests

_DIR = os.path.dirname(os.path.abspath(__file__))

# ════════════════════════════════════════════════════════════════════════════
# CONSTANTS (from ensemble_v3.py)
# ════════════════════════════════════════════════════════════════════════════
DAILY_RISK = 100_000
SPX_MULT = 100
SLIPPAGE_PER_SPREAD = 1.00
TRANCHE_RISK = 25_000

TS_HM = {
    "1030": (10,30), "1100": (11,0), "1130": (11,30),
    "1200": (12,0), "1230": (12,30), "1300": (13,0),
    "1330": (13,30), "1400": (14,0), "1430": (14,30),
    "1500": (15,0), "1530": (15,30), "1545": (15,45),
    "close": (16,0),
}
CHECKPOINTS_60M = [("1100",60),("1200",120),("1300",180),("1400",240),("1500",300)]
CHECKPOINTS_30M = [("1030",30),("1100",60),("1130",90),("1200",120),("1230",150),
                   ("1300",180),("1330",210),("1400",240),("1430",270),("1500",300),
                   ("1530",330),("1545",345)]

# ════════════════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS (multi-tranche only)
# ════════════════════════════════════════════════════════════════════════════
STRATEGIES = [
    {"ver":"v4",  "regime":"MID_DN_IN_GFL",  "mech":"40%/close/5T30m", "filter":None,
     "vix":[15,20], "pd":"DN", "rng":"IN",  "gap":"GFL"},
    {"ver":"v5",  "regime":"MID_UP_OT_GUP",  "mech":"40%/close/5T60m", "filter":"5dRet>0",
     "vix":[15,20], "pd":"UP", "rng":"OT",  "gap":"GUP"},
    {"ver":"v10", "regime":"MID_DN_OT_GFL",  "mech":"70%/1545/5T60m",  "filter":None,
     "vix":[15,20], "pd":"DN", "rng":"OT",  "gap":"GFL"},
    {"ver":"v11", "regime":"LOW_UP_OT_GFL",  "mech":"70%/close/3T60m", "filter":"VP<=2.0",
     "vix":[0,15],  "pd":"UP", "rng":"OT",  "gap":"GFL"},
    {"ver":"v12", "regime":"LOW_UP_OT_GUP",  "mech":"40%/close/5T60m", "filter":"5dRet>1",
     "vix":[0,15],  "pd":"UP", "rng":"OT",  "gap":"GUP"},
    {"ver":"v13", "regime":"LOW_DN_IN_GUP",  "mech":"40%/close/5T60m", "filter":"Rng<=0.3",
     "vix":[0,15],  "pd":"DN", "rng":"IN",  "gap":"GUP"},
]

# ════════════════════════════════════════════════════════════════════════════
# LOAD DATA
# ════════════════════════════════════════════════════════════════════════════
print("Loading research_all_trades.csv...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)
N = len(go)
print(f"  {N} GO days  |  {go['date'].min().date()} to {go['date'].max().date()}")

# ════════════════════════════════════════════════════════════════════════════
# FETCH SPX GAP DATA FROM POLYGON
# ════════════════════════════════════════════════════════════════════════════
GAP_CACHE = os.path.join(_DIR, "spx_gap_cache.json")

def fetch_gap_data():
    """Fetch SPX daily OHLC from Polygon, compute gap_pct per day."""
    if os.path.exists(GAP_CACHE):
        print("  Loading cached gap data...", flush=True)
        with open(GAP_CACHE) as f:
            cached = json.load(f)
        print(f"  {len(cached)} days in cache")
        return cached

    print("  Fetching SPX daily bars from Polygon...", flush=True)
    config_path = os.path.join(_DIR, "cockpit_config.json")
    with open(config_path) as f:
        api_key = json.load(f)["polygon_api_key"]

    # Fetch a range slightly wider than the CSV so we get the prior close for the first day
    start = (go["date"].min() - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    end = go["date"].max().strftime("%Y-%m-%d")

    url = f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/day/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}

    r = requests.get(url, params=params, timeout=30)
    data = r.json()

    if "results" not in data:
        print(f"  ERROR: Polygon API returned no results. Status: {data.get('status')}")
        print(f"  Message: {data.get('message', 'none')}")
        print(f"  Try clearing {GAP_CACHE} and re-running if a cached file exists.")
        sys.exit(1)

    bars = data["results"]
    print(f"  Got {len(bars)} daily bars from Polygon")

    gap_map = {}  # date_str -> gap_pct
    prev_close = None
    for bar in bars:
        dt = pd.Timestamp(bar["t"], unit="ms").strftime("%Y-%m-%d")
        open_px = bar["o"]
        close_px = bar["c"]

        if prev_close is not None:
            gap_pct = (open_px - prev_close) / prev_close * 100
            gap_map[dt] = round(gap_pct, 4)

        prev_close = close_px

    # Cache for future runs
    with open(GAP_CACHE, "w") as f:
        json.dump(gap_map, f)
    print(f"  Cached {len(gap_map)} days of gap data to {GAP_CACHE}")
    return gap_map

print("\nGap data:", flush=True)
gap_data = fetch_gap_data()

# Add gap classification to dataframe
def classify_gap(date_val):
    d_str = date_val.strftime("%Y-%m-%d")
    gp = gap_data.get(d_str, None)
    if gp is None:
        return "UNK"  # unknown -- no data
    if gp < -0.25:
        return "GDN"
    elif gp > 0.25:
        return "GUP"
    else:
        return "GFL"

go["gap"] = go["date"].apply(classify_gap)
gap_dist = go["gap"].value_counts()
print(f"\n  Gap distribution across {N} GO days:")
for g in ["GFL", "GUP", "GDN", "UNK"]:
    ct = gap_dist.get(g, 0)
    print(f"    {g}: {ct} days ({ct/N*100:.1f}%)")

# ════════════════════════════════════════════════════════════════════════════
# SIMULATION FUNCTIONS (from ensemble_v3.py -- exact copy)
# ════════════════════════════════════════════════════════════════════════════
def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str) or t_str == "":
        return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except:
        return False

def find_exit_pnl(row, target_pct, time_stop):
    """Determine exit P&L and outcome: WS, TGT, TS, EXP, or ND."""
    ts_hour, ts_min = TS_HM.get(time_stop, (16, 0))
    tgt_time = row.get(f"hit_{target_pct}_time", "")
    tgt_pnl  = row.get(f"hit_{target_pct}_pnl")
    ws_time  = row.get("ws_time", "")
    ws_pnl   = row.get("ws_pnl")

    events = []
    if ws_time and pd.notna(ws_pnl) and time_before(ws_time, ts_hour, ts_min):
        try: events.append(("WS", pd.Timestamp(ws_time), ws_pnl))
        except: pass
    if pd.notna(tgt_pnl) and tgt_time and time_before(tgt_time, ts_hour, ts_min):
        try: events.append(("TGT", pd.Timestamp(tgt_time), tgt_pnl))
        except: pass

    if events:
        events.sort(key=lambda x: x[1])
        return events[0][2], events[0][0]

    ts_col = f"pnl_at_{time_stop}" if time_stop != "close" else "pnl_at_close"
    ts_pnl = row.get(ts_col)
    if pd.notna(ts_pnl):
        return ts_pnl, "TS"

    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl):
        return close_pnl, "EXP"
    return None, "ND"

def simulate_day(row, target_pct, time_stop, n_tranches, interval_cps, risk_allocated):
    """
    Simulate a single day's P&L.
    Exact replica of ensemble_v3.py simulate_day_dollar().
    """
    exit_pnl, outcome = find_exit_pnl(row, target_pct, time_stop)
    if exit_pnl is None or outcome == "ND":
        return 0.0, "ND"

    # Determine exit time for tranche cutoff
    if outcome == "WS":
        exit_time_str = row.get("ws_time", "")
    elif outcome == "TGT":
        exit_time_str = row.get(f"hit_{target_pct}_time", "")
    else:
        exit_time_str = time_stop

    if exit_time_str in TS_HM:
        exit_h, exit_m = TS_HM[exit_time_str]
    else:
        try:
            et = pd.Timestamp(exit_time_str)
            exit_h, exit_m = et.hour, et.minute
        except:
            exit_h, exit_m = 16, 0

    # Position sizing
    risk_per_spread = row.get("risk_deployed_p1", 0)
    n_sp = row.get("n_spreads_p1", 0)
    ml_ps = (risk_per_spread / n_sp) if (n_sp > 0 and risk_per_spread > 0) else TRANCHE_RISK
    if ml_ps <= 0:
        ml_ps = TRANCHE_RISK
    risk_per_tranche = risk_allocated / max(1, n_tranches)
    n_per = max(1, int(risk_per_tranche / ml_ps))

    # Tranche 1 P&L (entry at market open)
    total = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

    # Additional tranches
    for k in range(2, n_tranches + 1):
        if k - 2 >= len(interval_cps):
            break
        cp_lbl, _ = interval_cps[k - 2]
        cp_pnl = row.get(f"pnl_at_{cp_lbl}")
        if cp_pnl is None or pd.isna(cp_pnl):
            continue
        cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
        if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m):
            continue
        tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        total += tk_pnl

    return round(total, 2), outcome

# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════
def parse_mech(mech_str):
    """Parse '40%/close/5T30m' -> (target_pct, time_stop, n_tranches, checkpoints)"""
    parts = mech_str.split("/")
    target_pct = int(parts[0].replace("%", ""))
    time_stop = parts[1]
    tranche_str = parts[2]

    n_t = int(tranche_str.split("T")[0])
    if "60m" in tranche_str:
        cps = CHECKPOINTS_60M
    elif "30m" in tranche_str:
        cps = CHECKPOINTS_30M
    else:
        cps = []

    return target_pct, time_stop, n_t, cps

def match_regime(row, strat):
    """Check if a row matches a strategy's VIX/PD/range/gap regime."""
    vix = row["vix"]
    vix_lo, vix_hi = strat["vix"]
    if not (vix_lo <= vix < vix_hi):
        return False

    pd_map = {"UP": "UP", "DOWN": "DN", "FLAT": "FL"}
    pd_val = pd_map.get(str(row.get("prior_day_direction", "")), "")
    if pd_val != strat["pd"]:
        return False

    rng = "IN" if row.get("in_prior_week_range", 0) == 1 else "OT"
    if rng != strat["rng"]:
        return False

    if row.get("gap", "UNK") != strat["gap"]:
        return False

    return True

def check_filter(row, filt):
    """Check strategy-specific additional filter."""
    if filt is None:
        return True
    if filt == "5dRet>0":
        return row.get("prior_5d_return", 0) > 0
    if filt == "5dRet>1":
        return row.get("prior_5d_return", 0) > 1
    if filt == "VP<=2.0":
        return row.get("vp_ratio", 99) <= 2.0
    if filt == "Rng<=0.3":
        return row.get("range_pct", 99) <= 0.3
    if filt == "ScoreVol<18":
        return row.get("score_vol", 99) < 18
    if filt == "VP<=1.7":
        return row.get("vp_ratio", 99) <= 1.7
    if filt == "!RISING":
        return row.get("rv_slope", "") != "RISING"
    return True

def compute_stats(pnl_list):
    """Compute trading stats from a list of daily P&L values."""
    arr = np.array(pnl_list)
    n_traded = int(np.sum(arr != 0))
    if n_traded == 0:
        return {"n_days": len(arr), "total": 0, "avg": 0, "wr": 0,
                "pf": 0, "max_dd": 0, "worst_day": 0, "best_day": 0}

    traded = arr[arr != 0]
    total = arr.sum()
    wins = traded[traded > 0]
    losses = traded[traded < 0]
    wr = len(wins) / len(traded) * 100 if len(traded) > 0 else 0
    w_sum = wins.sum() if len(wins) > 0 else 0
    l_sum = abs(losses.sum()) if len(losses) > 0 else 0.001
    pf = w_sum / l_sum if l_sum > 0 else float('inf')

    # Max drawdown
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = dd.max()

    return {
        "n_days": len(arr),
        "total": round(total, 2),
        "avg": round(total / n_traded, 2),
        "wr": round(wr, 1),
        "pf": round(pf, 2),
        "max_dd": round(max_dd, 2),
        "worst_day": round(arr.min(), 2),
        "best_day": round(arr.max(), 2),
    }

# ════════════════════════════════════════════════════════════════════════════
# MAIN COMPARISON
# ════════════════════════════════════════════════════════════════════════════
print(f"\n{'='*110}")
print("  TRANCHE COMPARISON: 1T vs Multi-Tranche")
print(f"  Dataset: {N} GO days  |  {go['date'].min().date()} to {go['date'].max().date()}")
print(f"  Risk: ${DAILY_RISK:,}  |  Slippage: ${SLIPPAGE_PER_SPREAD}/spread  |  SPX mult: {SPX_MULT}")
print(f"{'='*110}")

summary_rows = []

for strat in STRATEGIES:
    target_pct, time_stop, n_t, cps = parse_mech(strat["mech"])

    # ── Filter matching days ──
    matching = []
    for _, row in go.iterrows():
        if match_regime(row, strat) and check_filter(row, strat["filter"]):
            matching.append(row)

    n_match = len(matching)
    if n_match == 0:
        print(f"\n  {strat['ver'].upper()} ({strat['regime']}) -- 0 matching days, SKIPPING")
        continue

    # ── Simulate both modes ──
    pnl_mt = []   # multi-tranche (original)
    pnl_1t = []   # single tranche
    outcomes_mt = {"TGT": 0, "WS": 0, "TS": 0, "EXP": 0, "ND": 0}
    outcomes_1t = {"TGT": 0, "WS": 0, "TS": 0, "EXP": 0, "ND": 0}
    dates = []

    for row in matching:
        dates.append(row["date"])

        # Multi-tranche
        p_mt, o_mt = simulate_day(row, target_pct, time_stop, n_t, cps, DAILY_RISK)
        pnl_mt.append(p_mt)
        outcomes_mt[o_mt] = outcomes_mt.get(o_mt, 0) + 1

        # Single tranche (1T) -- same target/time_stop, just 1 tranche
        p_1t, o_1t = simulate_day(row, target_pct, time_stop, 1, [], DAILY_RISK)
        pnl_1t.append(p_1t)
        outcomes_1t[o_1t] = outcomes_1t.get(o_1t, 0) + 1

    stats_mt = compute_stats(pnl_mt)
    stats_1t = compute_stats(pnl_1t)

    # ── Per-year breakdown ──
    yearly_mt = {}
    yearly_1t = {}
    for i, dt in enumerate(dates):
        yr = dt.year
        if yr not in yearly_mt:
            yearly_mt[yr] = []
            yearly_1t[yr] = []
        yearly_mt[yr].append(pnl_mt[i])
        yearly_1t[yr].append(pnl_1t[i])

    # ── Print results ──
    print(f"\n{'='*110}")
    print(f"  {strat['ver'].upper()} -- {strat['regime']}")
    print(f"  Mech: {strat['mech']}  |  Filter: {strat['filter'] or 'none'}  |  {n_match} matching days")
    print(f"{'='*110}")

    hdr = f"  {'Mode':>18} {'Total P&L':>14} {'WinRate':>9} {'PF':>7} {'MaxDD':>14} {'Avg/Day':>11} {'Best':>12} {'Worst':>12}"
    print(hdr)
    print(f"  {'-'*106}")

    def row_str(label, s):
        return f"  {label:>18} ${s['total']:>12,.0f} {s['wr']:>7.1f}% {s['pf']:>6.2f} ${s['max_dd']:>12,.0f} ${s['avg']:>9,.0f} ${s['best_day']:>10,.0f} ${s['worst_day']:>10,.0f}"

    print(row_str(f"Multi ({n_t}T)", stats_mt))
    print(row_str("Single (1T)", stats_1t))

    # Delta
    d_total = stats_1t['total'] - stats_mt['total']
    d_pf = stats_1t['pf'] - stats_mt['pf']
    d_dd = stats_1t['max_dd'] - stats_mt['max_dd']
    winner = "1T" if stats_1t['pf'] >= stats_mt['pf'] else f"{n_t}T"
    print(f"\n  Delta (1T - {n_t}T):  P&L {'+'if d_total>=0 else ''}${d_total:,.0f}  |  PF {'+'if d_pf>=0 else ''}{d_pf:.2f}  |  DD {'+'if d_dd>=0 else ''}${d_dd:,.0f}")
    print(f"  --> WINNER: {winner}")

    # Outcomes breakdown
    print(f"\n  Outcomes ({n_t}T): TGT={outcomes_mt['TGT']}  WS={outcomes_mt['WS']}  TS={outcomes_mt.get('TS',0)}  EXP={outcomes_mt.get('EXP',0)}")

    # Per-year
    if len(yearly_mt) > 1:
        print(f"\n  {'Year':>8} {'Days':>6} {'MT Total':>12} {'1T Total':>12} {'MT PF':>8} {'1T PF':>8} {'Winner':>8}")
        print(f"  {'-'*62}")
        for yr in sorted(yearly_mt.keys()):
            ys_mt = compute_stats(yearly_mt[yr])
            ys_1t = compute_stats(yearly_1t[yr])
            yr_win = "1T" if ys_1t['pf'] >= ys_mt['pf'] else f"{n_t}T"
            print(f"  {yr:>8} {len(yearly_mt[yr]):>6} ${ys_mt['total']:>10,.0f} ${ys_1t['total']:>10,.0f} {ys_mt['pf']:>7.2f} {ys_1t['pf']:>7.2f} {yr_win:>8}")

    summary_rows.append({
        "ver": strat["ver"].upper(),
        "regime": strat["regime"],
        "n_days": n_match,
        "mt_label": f"{n_t}T",
        "mt_total": stats_mt["total"],
        "mt_pf": stats_mt["pf"],
        "mt_dd": stats_mt["max_dd"],
        "mt_wr": stats_mt["wr"],
        "one_total": stats_1t["total"],
        "one_pf": stats_1t["pf"],
        "one_dd": stats_1t["max_dd"],
        "one_wr": stats_1t["wr"],
        "winner": winner,
    })

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY TABLE
# ════════════════════════════════════════════════════════════════════════════
print(f"\n\n{'='*110}")
print("  SUMMARY -- 1T vs Multi-Tranche")
print(f"{'='*110}")
print(f"  {'Ver':>5} {'Regime':>18} {'Days':>6} {'MT':>4} {'MT P&L':>12} {'MT PF':>7} {'MT DD':>12} | {'1T P&L':>12} {'1T PF':>7} {'1T DD':>12} {'Winner':>8}")
print(f"  {'-'*120}")

total_mt = 0
total_1t = 0
for r in summary_rows:
    total_mt += r["mt_total"]
    total_1t += r["one_total"]
    print(f"  {r['ver']:>5} {r['regime']:>18} {r['n_days']:>6} {r['mt_label']:>4} ${r['mt_total']:>10,.0f} {r['mt_pf']:>6.2f} ${r['mt_dd']:>10,.0f} | ${r['one_total']:>10,.0f} {r['one_pf']:>6.2f} ${r['one_dd']:>10,.0f} {r['winner']:>8}")

print(f"  {'-'*120}")
print(f"  {'TOTAL':>5} {'':>18} {'':>6} {'':>4} ${total_mt:>10,.0f} {'':>6} {'':>11} | ${total_1t:>10,.0f}")

# Which mode wins overall?
mt_wins = sum(1 for r in summary_rows if "T" in r["winner"] and r["winner"] != "1T")
one_wins = sum(1 for r in summary_rows if r["winner"] == "1T")
print(f"\n  Multi-tranche wins: {mt_wins}/{len(summary_rows)} strategies")
print(f"  Single tranche wins: {one_wins}/{len(summary_rows)} strategies")
print(f"\n  Combined P&L delta (1T - MT): {'+'if (total_1t-total_mt)>=0 else ''}${total_1t - total_mt:,.0f}")

print(f"\n  NOTE: Multi-tranche backtest uses SAME ATM strike for all adds.")
print(f"  In live trading, SPX drift means adds would be at different strikes.")
print(f"  1T results are trustworthy. Multi-tranche results are optimistic for")
print(f"  mean-reverting days and pessimistic for trending days.")
print(f"\n{'='*110}")
