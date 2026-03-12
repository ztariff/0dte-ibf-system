"""
RE-CENTERED ADD ANALYSIS
=========================
Answers: Would re-centering tranche adds at current ATM be more profitable
than same-strike adds?

Approach:
1. Fetch SPX 30-min bars from Polygon for all GO days
2. Compute SPX drift from entry center at each checkpoint
3. Analyze mean-reversion vs continuation patterns
4. Estimate P&L impact for same-strike vs re-centered adds
5. Per-strategy comparison for V4, V5, V10, V11, V12, V13

Key insight:
- Same-strike add at time C: P&L = (exit_pnl - pnl_at_C) * n_per
  This profits if SPX reverts back toward the original center.
- Re-centered add at time C: Fresh ATM IBF at current SPX.
  This profits from theta regardless of where SPX is, but is hurt
  if SPX moves away from the NEW center.

If SPX mean-reverts -> same-strike wins (original strikes come back into profit)
If SPX trends      -> re-centered wins (fresh ATM captures theta without directional drag)

Usage:
    python3 recentered_analysis.py
"""

import os, json, sys, time as _time
import pandas as pd
import numpy as np
import requests
from math import sqrt, log, exp, pi

_DIR = os.path.dirname(os.path.abspath(__file__))

# ====================================================================
# CONSTANTS
# ====================================================================
DAILY_RISK = 100_000
SPX_MULT = 100
SLIPPAGE_PER_SPREAD = 1.00
TRANCHE_RISK = 25_000
WING_WIDTH = 50  # IBF wing width in SPX points

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

# Strategy definitions (multi-tranche only)
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

# ====================================================================
# LOAD CSV DATA
# ====================================================================
print("Loading research_all_trades.csv...", flush=True)
df = pd.read_csv(os.path.join(_DIR, "research_all_trades.csv"))
go = df[df["recommendation"] == "GO"].copy()
go["date"] = pd.to_datetime(go["date"])
go = go.sort_values("date").reset_index(drop=True)
N = len(go)
print(f"  {N} GO days  |  {go['date'].min().date()} to {go['date'].max().date()}")

# Load gap cache for regime matching
GAP_CACHE = os.path.join(_DIR, "spx_gap_cache.json")
if os.path.exists(GAP_CACHE):
    with open(GAP_CACHE) as f:
        gap_data = json.load(f)
    def classify_gap(date_val):
        d_str = date_val.strftime("%Y-%m-%d")
        gp = gap_data.get(d_str, None)
        if gp is None: return "UNK"
        if gp < -0.25: return "GDN"
        elif gp > 0.25: return "GUP"
        else: return "GFL"
    go["gap"] = go["date"].apply(classify_gap)
else:
    print("  ERROR: Run tranche_comparison.py first to generate gap cache")
    sys.exit(1)

# ====================================================================
# FETCH SPX INTRADAY DATA FROM POLYGON
# ====================================================================
SPX_INTRADAY_CACHE = os.path.join(_DIR, "spx_intraday_cache.json")

def fetch_spx_intraday():
    """Fetch SPX 5-min bars from Polygon, extract prices at checkpoint times."""
    if os.path.exists(SPX_INTRADAY_CACHE):
        print("  Loading cached intraday data...", flush=True)
        with open(SPX_INTRADAY_CACHE) as f:
            cached = json.load(f)
        print(f"  {len(cached)} days in cache")
        return cached

    print("  Fetching SPX 5-min bars from Polygon (may take a moment)...", flush=True)
    config_path = os.path.join(_DIR, "cockpit_config.json")
    with open(config_path) as f:
        api_key = json.load(f)["polygon_api_key"]

    start = (go["date"].min() - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    end = go["date"].max().strftime("%Y-%m-%d")

    # Fetch in monthly chunks to avoid hitting limit
    all_bars = []
    current = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)

    while current <= end_dt:
        chunk_end = min(current + pd.DateOffset(months=2), end_dt)
        url = f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/5/minute/{current.strftime('%Y-%m-%d')}/{chunk_end.strftime('%Y-%m-%d')}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}

        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=60)
                data = r.json()
                if "results" in data:
                    all_bars.extend(data["results"])
                    print(f"    {current.strftime('%Y-%m')} to {chunk_end.strftime('%Y-%m')}: {len(data['results'])} bars", flush=True)
                    break
                elif r.status_code == 429:
                    print(f"    Rate limited, waiting...", flush=True)
                    _time.sleep(15)
                else:
                    print(f"    Warning: no results for {current.date()} to {chunk_end.date()}: {data.get('status')}")
                    break
            except Exception as e:
                print(f"    Error: {e}, retrying...")
                _time.sleep(5)

        current = chunk_end + pd.Timedelta(days=1)
        _time.sleep(0.5)  # Rate limit

    print(f"  Total bars fetched: {len(all_bars)}")

    # Parse into per-day checkpoint prices
    # checkpoint_times we need: 1000, 1030, 1100, 1130, 1200, 1230, 1300, 1330, 1400, 1430, 1500, 1530, 1545, close(1600)
    daily_prices = {}  # date_str -> {time_label: price}

    for bar in all_bars:
        ts = pd.Timestamp(bar["t"], unit="ms", tz="UTC").tz_convert("America/New_York")
        d_str = ts.strftime("%Y-%m-%d")
        hm = ts.strftime("%H%M")

        if d_str not in daily_prices:
            daily_prices[d_str] = {}

        # Store the close price of the 5-min bar that starts at or nearest to each checkpoint
        # For checkpoint "1000", we want the bar at 10:00
        # The bar at 10:00 ET represents 10:00-10:05, so its close is SPX at ~10:05
        # Actually, we want the OPEN of the bar at 10:00 for the checkpoint price
        # But since bars are 5-min, the open of 10:00 bar = SPX at 10:00

        # Map 5-min bar start times to checkpoint labels
        checkpoint_map = {
            "1000": "1000", "1030": "1030", "1100": "1100", "1130": "1130",
            "1200": "1200", "1230": "1230", "1300": "1300", "1330": "1330",
            "1400": "1400", "1430": "1430", "1500": "1500", "1530": "1530",
            "1545": "1545", "1555": "close",
        }

        if hm in checkpoint_map:
            label = checkpoint_map[hm]
            daily_prices[d_str][label] = bar["o"]  # Use open of bar = SPX at that exact time

        # Also capture close of day (last bar before 16:00)
        if hm == "1555":
            daily_prices[d_str]["close"] = bar["c"]  # Close of last bar = SPX at ~16:00

    # Cache
    with open(SPX_INTRADAY_CACHE, "w") as f:
        json.dump(daily_prices, f)
    print(f"  Cached {len(daily_prices)} days of intraday data")
    return daily_prices

print("\nSPX intraday data:", flush=True)
spx_intraday = fetch_spx_intraday()

# ====================================================================
# PART 1: INTRADAY DRIFT ANALYSIS (all GO days)
# ====================================================================
print(f"\n{'='*110}")
print("  PART 1: SPX INTRADAY DRIFT ANALYSIS")
print(f"  How far does SPX drift from the 10am entry center at each checkpoint?")
print(f"  Does it mean-revert or continue trending?")
print(f"{'='*110}")

# Checkpoint labels in order
CP_LABELS = ["1030", "1100", "1130", "1200", "1230", "1300",
             "1330", "1400", "1430", "1500", "1530", "1545", "close"]

drift_data = {cp: [] for cp in CP_LABELS}
reversion_data = []  # (drift_at_checkpoint, drift_at_close) pairs per checkpoint

n_with_data = 0
for _, row in go.iterrows():
    d_str = row["date"].strftime("%Y-%m-%d")
    if d_str not in spx_intraday:
        continue
    day = spx_intraday[d_str]
    if "1000" not in day:
        continue

    entry_spx = day["1000"]
    n_with_data += 1

    close_spx = day.get("close")
    if close_spx is None:
        continue

    close_drift = close_spx - entry_spx

    for cp in CP_LABELS:
        if cp in day:
            cp_drift = day[cp] - entry_spx
            drift_data[cp].append(cp_drift)
            if cp != "close":
                reversion_data.append((cp, cp_drift, close_drift))

print(f"\n  Days with intraday data: {n_with_data}/{N}")

# Drift statistics at each checkpoint
print(f"\n  {'Checkpoint':>12} {'N':>5} {'Mean Drift':>12} {'Median':>10} {'Std Dev':>10} {'|Drift|>5':>10} {'|Drift|>10':>10} {'|Drift|>20':>10}")
print(f"  {'-'*85}")
for cp in CP_LABELS:
    if not drift_data[cp]:
        continue
    arr = np.array(drift_data[cp])
    n = len(arr)
    print(f"  {cp:>12} {n:>5} {arr.mean():>+11.2f} {np.median(arr):>+9.2f} {arr.std():>9.2f} "
          f"{(np.abs(arr)>5).sum():>8}({(np.abs(arr)>5).mean()*100:.0f}%) "
          f"{(np.abs(arr)>10).sum():>4}({(np.abs(arr)>10).mean()*100:.0f}%) "
          f"{(np.abs(arr)>20).sum():>4}({(np.abs(arr)>20).mean()*100:.0f}%)")

# Reversion analysis: For each checkpoint, what fraction of the drift
# is reversed by close?
print(f"\n\n  MEAN REVERSION ANALYSIS")
print(f"  If SPX drifts D points from center by checkpoint C,")
print(f"  what fraction reverts by close?")
print(f"  Reversion ratio = 1 - (close_drift / checkpoint_drift)")
print(f"  >0 = mean-reverting, <0 = continuation, 1.0 = full reversion")
print(f"\n  {'Checkpoint':>12} {'N':>5} {'Avg Reversion':>15} {'Median Rev':>12} {'%Revert>50%':>13} {'%Continue':>11}")
print(f"  {'-'*75}")

for cp in ["1030", "1100", "1130", "1200", "1300", "1400", "1500"]:
    # Filter to meaningful drifts (|drift| > 2 points to avoid noise)
    pairs = [(d, c) for (label, d, c) in reversion_data if label == cp and abs(d) > 2]
    if len(pairs) < 5:
        continue

    revs = []
    for cp_d, cl_d in pairs:
        # Reversion ratio: how much of the drift reversed?
        # If drift was +10 and close drift is +3, reversion = 1 - 3/10 = 0.70 (70% reverted)
        # If drift was +10 and close drift is +15, reversion = 1 - 15/10 = -0.50 (continued 50%)
        if abs(cp_d) > 0.1:
            rev = 1.0 - (cl_d / cp_d)
            revs.append(rev)

    if not revs:
        continue

    rarr = np.array(revs)
    n_rev = len(rarr)
    pct_revert = (rarr > 0.5).mean() * 100  # >50% reversion
    pct_continue = (rarr < 0).mean() * 100   # drift continued past checkpoint
    print(f"  {cp:>12} {n_rev:>5} {rarr.mean():>+14.2f} {np.median(rarr):>+11.2f} {pct_revert:>11.0f}% {pct_continue:>9.0f}%")

# ====================================================================
# PART 2: SIMPLIFIED RE-CENTERED ADD P&L MODEL
# ====================================================================
print(f"\n\n{'='*110}")
print("  PART 2: RE-CENTERED vs SAME-STRIKE ADD P&L COMPARISON")
print(f"  Using simplified IBF payoff model for re-centered adds")
print(f"{'='*110}")
print(f"\n  Model: For a re-centered add at checkpoint C:")
print(f"    - Entry credit estimated from VIX and time remaining (Black-Scholes ATM straddle approx)")
print(f"    - Exit P&L based on SPX movement from new center to close/exit")
print(f"    - Wing width: {WING_WIDTH} pts")

# Black-Scholes helpers for ATM IBF credit estimation
def norm_cdf(x):
    """Approximate normal CDF."""
    from math import erf
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))

def bs_call(S, K, T, sigma, r=0.05):
    """Black-Scholes call price."""
    if T <= 0 or sigma <= 0:
        return max(0, S - K)
    d1 = (log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    return S * norm_cdf(d1) - K * exp(-r*T) * norm_cdf(d2)

def bs_put(S, K, T, sigma, r=0.05):
    """Black-Scholes put price."""
    if T <= 0 or sigma <= 0:
        return max(0, K - S)
    d1 = (log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    return K * exp(-r*T) * norm_cdf(-d2) - S * norm_cdf(-d1)

def ibf_credit(S, W, T_years, iv):
    """
    Estimate Iron Butterfly credit per spread.
    Short ATM call + Short ATM put - Long call at S+W - Long put at S-W
    Returns credit per $1 of SPX (divide by SPX_MULT for per-point).
    """
    K = S  # ATM center
    short_call = bs_call(S, K, T_years, iv)
    short_put = bs_put(S, K, T_years, iv)
    long_call = bs_call(S, K + W, T_years, iv)
    long_put = bs_put(S, K - W, T_years, iv)
    credit = (short_call + short_put) - (long_call + long_put)
    return credit / S  # normalize per dollar of SPX

def ibf_pnl_at_exit(credit_per_pt, spx_at_exit, center, wing_width):
    """
    Compute IBF P&L per spread at expiry (or near-expiry approximation).
    Returns P&L per point of SPX (multiply by SPX_MULT for dollar P&L).
    """
    drift = abs(spx_at_exit - center)
    if drift <= wing_width:
        # P&L = credit - intrinsic loss
        return credit_per_pt - drift
    else:
        # Max loss = wing_width - credit
        return -(wing_width - credit_per_pt)


# Checkpoint times as fraction of trading day remaining
# Market: 9:30 to 16:00 = 6.5 hours = 390 minutes
def time_remaining_years(checkpoint_hhmm):
    """Fraction of year remaining from checkpoint to close."""
    h, m = TS_HM.get(checkpoint_hhmm, (16, 0))
    minutes_to_close = (16 * 60) - (h * 60 + m)
    return max(0.0001, minutes_to_close / (252 * 390))  # fraction of trading year


# ====================================================================
# HELPER FUNCTIONS (from tranche_comparison.py)
# ====================================================================
def time_before(t_str, h, m):
    if not t_str or pd.isna(t_str) or t_str == "":
        return False
    try:
        t = pd.Timestamp(t_str)
        return t.hour < h or (t.hour == h and t.minute < m)
    except:
        return False

def find_exit_pnl(row, target_pct, time_stop):
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
        return events[0][2], events[0][0], events[0][1]
    ts_col = f"pnl_at_{time_stop}" if time_stop != "close" else "pnl_at_close"
    ts_pnl = row.get(ts_col)
    if pd.notna(ts_pnl): return ts_pnl, "TS", None
    close_pnl = row.get("pnl_at_close")
    if pd.notna(close_pnl): return close_pnl, "EXP", None
    return None, "ND", None

def find_exit_time_hm(row, target_pct, time_stop, outcome, exit_ts):
    """Get exit hour,minute for determining which adds happened before exit."""
    if outcome == "WS":
        t_str = row.get("ws_time", "")
    elif outcome == "TGT":
        t_str = row.get(f"hit_{target_pct}_time", "")
    else:
        t_str = time_stop
    if t_str in TS_HM:
        return TS_HM[t_str]
    try:
        et = pd.Timestamp(t_str)
        return et.hour, et.minute
    except:
        return 16, 0

def parse_mech(mech_str):
    parts = mech_str.split("/")
    target_pct = int(parts[0].replace("%", ""))
    time_stop = parts[1]
    tranche_str = parts[2]
    n_t = int(tranche_str.split("T")[0])
    if "60m" in tranche_str: cps = CHECKPOINTS_60M
    elif "30m" in tranche_str: cps = CHECKPOINTS_30M
    else: cps = []
    return target_pct, time_stop, n_t, cps

def match_regime(row, strat):
    vix = row["vix"]
    vix_lo, vix_hi = strat["vix"]
    if not (vix_lo <= vix < vix_hi): return False
    pd_map = {"UP": "UP", "DOWN": "DN", "FLAT": "FL"}
    pd_val = pd_map.get(str(row.get("prior_day_direction", "")), "")
    if pd_val != strat["pd"]: return False
    rng = "IN" if row.get("in_prior_week_range", 0) == 1 else "OT"
    if rng != strat["rng"]: return False
    if row.get("gap", "UNK") != strat["gap"]: return False
    return True

def check_filter(row, filt):
    if filt is None: return True
    if filt == "5dRet>0": return row.get("prior_5d_return", 0) > 0
    if filt == "5dRet>1": return row.get("prior_5d_return", 0) > 1
    if filt == "VP<=2.0": return row.get("vp_ratio", 99) <= 2.0
    if filt == "Rng<=0.3": return row.get("range_pct", 99) <= 0.3
    return True

def compute_stats(pnl_list):
    arr = np.array(pnl_list)
    n_traded = int(np.sum(arr != 0))
    if n_traded == 0:
        return {"n_days": len(arr), "total": 0, "avg": 0, "wr": 0, "pf": 0, "max_dd": 0}
    traded = arr[arr != 0]
    total = arr.sum()
    wins = traded[traded > 0]
    losses = traded[traded < 0]
    wr = len(wins) / len(traded) * 100 if len(traded) > 0 else 0
    w_sum = wins.sum() if len(wins) > 0 else 0
    l_sum = abs(losses.sum()) if len(losses) > 0 else 0.001
    pf = w_sum / l_sum if l_sum > 0 else float('inf')
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = dd.max()
    return {"n_days": len(arr), "total": round(total, 2), "avg": round(total/n_traded, 2),
            "wr": round(wr, 1), "pf": round(pf, 2), "max_dd": round(max_dd, 2)}


# ====================================================================
# SIMULATE SAME-STRIKE vs RE-CENTERED for each strategy
# ====================================================================
def simulate_strategy(strat, go_df, spx_data):
    """
    For a given strategy, simulate three modes:
    1. Same-strike multi-tranche (original backtest)
    2. Re-centered multi-tranche (new adds at current ATM)
    3. Single tranche (1T baseline)
    """
    target_pct, time_stop, n_t, cps = parse_mech(strat["mech"])

    matching = []
    for _, row in go_df.iterrows():
        if match_regime(row, strat) and check_filter(row, strat["filter"]):
            matching.append(row)

    if not matching:
        return None

    pnl_same = []     # same-strike multi-tranche
    pnl_recenter = [] # re-centered multi-tranche
    pnl_1t = []       # single tranche
    debug_days = []

    for row in matching:
        d_str = row["date"].strftime("%Y-%m-%d")
        vix = row["vix"]
        iv = vix / 100.0  # VIX as annualized IV

        # Get exit info
        exit_pnl, outcome, exit_ts = find_exit_pnl(row, target_pct, time_stop)
        if exit_pnl is None or outcome == "ND":
            pnl_same.append(0)
            pnl_recenter.append(0)
            pnl_1t.append(0)
            continue

        exit_h, exit_m = find_exit_time_hm(row, target_pct, time_stop, outcome, exit_ts)

        # Position sizing
        risk_per_spread = row.get("risk_deployed_p1", 0)
        n_sp = row.get("n_spreads_p1", 0)
        ml_ps = (risk_per_spread / n_sp) if (n_sp > 0 and risk_per_spread > 0) else TRANCHE_RISK
        if ml_ps <= 0: ml_ps = TRANCHE_RISK
        risk_per_tranche = DAILY_RISK / max(1, n_t)
        n_per = max(1, int(risk_per_tranche / ml_ps))
        n_per_full = max(1, int(DAILY_RISK / ml_ps))

        # ---- 1T: Single tranche ----
        total_1t = exit_pnl * n_per_full * SPX_MULT - n_per_full * SLIPPAGE_PER_SPREAD * SPX_MULT
        pnl_1t.append(round(total_1t, 2))

        # ---- SAME-STRIKE: Original multi-tranche ----
        total_same = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
        for k in range(2, n_t + 1):
            if k - 2 >= len(cps): break
            cp_lbl, _ = cps[k - 2]
            cp_pnl = row.get(f"pnl_at_{cp_lbl}")
            if cp_pnl is None or pd.isna(cp_pnl): continue
            cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
            if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue
            tk_pnl = (exit_pnl - cp_pnl) * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total_same += tk_pnl
        pnl_same.append(round(total_same, 2))

        # ---- RE-CENTERED: New IBF at current SPX for each add ----
        # Get SPX data for this day
        day_spx = spx_data.get(d_str, {})
        entry_spx = day_spx.get("1000")

        if entry_spx is None:
            # No intraday data, fall back to same as same-strike
            pnl_recenter.append(round(total_same, 2))
            continue

        # Determine SPX at exit time
        # For TGT/WS: use the closest checkpoint SPX as proxy
        # For TS/EXP at close: use close SPX
        if outcome in ("TS", "EXP"):
            exit_spx_key = "close" if time_stop == "close" else time_stop
            spx_at_exit = day_spx.get(exit_spx_key, day_spx.get("close"))
        else:
            # TGT or WS: find closest checkpoint to exit time
            best_key = "close"
            best_diff = 9999
            for k in day_spx:
                if k == "1000" or k not in TS_HM: continue
                kh, km = TS_HM[k]
                diff = abs((kh * 60 + km) - (exit_h * 60 + exit_m))
                if diff < best_diff:
                    best_diff = diff
                    best_key = k
            spx_at_exit = day_spx.get(best_key, day_spx.get("close"))

        if spx_at_exit is None:
            pnl_recenter.append(round(total_same, 2))
            continue

        # Tranche 1 is same for both methods (entered at original ATM at 10am)
        total_rc = exit_pnl * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT

        # Additional tranches: re-centered at current SPX
        day_debug = {"date": d_str, "entry_spx": entry_spx, "tranches": []}
        for k in range(2, n_t + 1):
            if k - 2 >= len(cps): break
            cp_lbl, _ = cps[k - 2]
            cp_h, cp_m = TS_HM.get(cp_lbl, (16, 0))
            if cp_h > exit_h or (cp_h == exit_h and cp_m >= exit_m): continue

            # SPX at this checkpoint
            spx_at_cp = day_spx.get(cp_lbl)
            if spx_at_cp is None: continue

            # Estimate credit for fresh ATM IBF at checkpoint time
            T_rem = time_remaining_years(cp_lbl)
            credit_normalized = ibf_credit(spx_at_cp, WING_WIDTH, T_rem, iv)
            credit_pts = credit_normalized * spx_at_cp  # credit in SPX points

            # P&L of re-centered add from checkpoint to exit
            # SPX moved from spx_at_cp (new center) to spx_at_exit
            rc_pnl_per_spread = ibf_pnl_at_exit(credit_pts, spx_at_exit, spx_at_cp, WING_WIDTH)

            # Dollar P&L for this tranche
            tk_pnl = rc_pnl_per_spread * n_per * SPX_MULT - n_per * SLIPPAGE_PER_SPREAD * SPX_MULT
            total_rc += tk_pnl

            day_debug["tranches"].append({
                "cp": cp_lbl, "spx_cp": round(spx_at_cp, 1),
                "drift": round(spx_at_cp - entry_spx, 1),
                "credit": round(credit_pts, 2),
                "rc_pnl": round(rc_pnl_per_spread, 2),
                "same_pnl": round((exit_pnl - (row.get(f"pnl_at_{cp_lbl}") or 0)), 2),
            })

        pnl_recenter.append(round(total_rc, 2))
        debug_days.append(day_debug)

    return {
        "pnl_same": pnl_same,
        "pnl_recenter": pnl_recenter,
        "pnl_1t": pnl_1t,
        "n_days": len(matching),
        "debug": debug_days,
    }


# Run for each strategy
summary = []
for strat in STRATEGIES:
    result = simulate_strategy(strat, go, spx_intraday)
    if result is None:
        print(f"\n  {strat['ver'].upper()} ({strat['regime']}) -- 0 matching days, SKIPPING")
        continue

    s_same = compute_stats(result["pnl_same"])
    s_rc = compute_stats(result["pnl_recenter"])
    s_1t = compute_stats(result["pnl_1t"])
    n_t = int(strat["mech"].split("/")[2].split("T")[0])

    print(f"\n{'='*110}")
    print(f"  {strat['ver'].upper()} -- {strat['regime']}  |  {result['n_days']} days  |  Mech: {strat['mech']}")
    print(f"{'='*110}")
    print(f"  {'Mode':>22} {'Total P&L':>14} {'WinRate':>9} {'PF':>7} {'MaxDD':>14} {'Avg/Day':>11}")
    print(f"  {'-'*80}")
    def fmt(label, s):
        return f"  {label:>22} ${s['total']:>12,.0f} {s['wr']:>7.1f}% {s['pf']:>6.2f} ${s['max_dd']:>12,.0f} ${s['avg']:>9,.0f}"
    print(fmt(f"Same-Strike ({n_t}T)", s_same))
    print(fmt(f"Re-Centered ({n_t}T)", s_rc))
    print(fmt("Single (1T)", s_1t))

    d_pnl = s_rc['total'] - s_same['total']
    d_pf = s_rc['pf'] - s_same['pf']
    winner = "Re-Centered" if s_rc['pf'] > s_same['pf'] else "Same-Strike"
    print(f"\n  Re-Centered vs Same-Strike:")
    print(f"    P&L delta: {'+'if d_pnl>=0 else ''}${d_pnl:,.0f}  |  PF delta: {'+'if d_pf>=0 else ''}{d_pf:.2f}")
    print(f"    --> Winner: {winner}")

    summary.append({
        "ver": strat["ver"].upper(), "regime": strat["regime"],
        "n_days": result["n_days"], "n_t": n_t,
        "same_total": s_same["total"], "same_pf": s_same["pf"], "same_dd": s_same["max_dd"],
        "rc_total": s_rc["total"], "rc_pf": s_rc["pf"], "rc_dd": s_rc["max_dd"],
        "one_total": s_1t["total"], "one_pf": s_1t["pf"], "one_dd": s_1t["max_dd"],
    })

# ====================================================================
# FINAL SUMMARY
# ====================================================================
print(f"\n\n{'='*120}")
print("  FINAL SUMMARY: Same-Strike vs Re-Centered vs 1T")
print(f"{'='*120}")
print(f"  {'Ver':>5} {'Days':>5} {'Same P&L':>12} {'Same PF':>8} {'RC P&L':>12} {'RC PF':>8} {'1T P&L':>12} {'1T PF':>8}  {'Best Mode':>14}")
print(f"  {'-'*100}")

t_same = 0
t_rc = 0
t_1t = 0
for r in summary:
    t_same += r["same_total"]
    t_rc += r["rc_total"]
    t_1t += r["one_total"]
    modes = [("Same-Strike", r["same_pf"]), ("Re-Centered", r["rc_pf"]), ("1T", r["one_pf"])]
    best = max(modes, key=lambda x: x[1])
    print(f"  {r['ver']:>5} {r['n_days']:>5} ${r['same_total']:>10,.0f} {r['same_pf']:>7.2f} ${r['rc_total']:>10,.0f} {r['rc_pf']:>7.2f} ${r['one_total']:>10,.0f} {r['one_pf']:>7.2f}  {best[0]:>14}")

print(f"  {'-'*100}")
print(f"  {'TOTAL':>5} {'':>5} ${t_same:>10,.0f} {'':>7} ${t_rc:>10,.0f} {'':>7} ${t_1t:>10,.0f}")

print(f"\n  NOTES:")
print(f"  - Same-Strike uses backtest P&L data directly (reliable)")
print(f"  - Re-Centered uses Black-Scholes IBF pricing model (approximate)")
print(f"  - 0DTE options have extreme gamma near expiry; B-S underestimates tail risk")
print(f"  - Re-centered model assumes expiry-like payoff (conservative for early exits)")
print(f"  - For strategies where SPX mean-reverts, same-strike naturally outperforms")
print(f"\n{'='*120}")
