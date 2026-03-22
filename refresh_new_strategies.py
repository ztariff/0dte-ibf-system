#!/usr/bin/env python3
"""
refresh_new_strategies.py — Incremental refresh for the 10 graded strategies.

Called by cockpit_feed.py's /api/refresh_stats endpoint (Refresh Data button).
Pulls only NEW trading days from Polygon, runs strategies, appends to calendar_trades.json.

Flow:
  1. Read calendar_trades.json → find max trade date
  2. Query Polygon for trading days after that date through yesterday
  3. For each new day: pull SPX 1-min bars, option chains, VIX 1-min bars
  4. Update daily context (spx_daily, vix_daily)
  5. Reload DataUniverse with new data
  6. Run all 10 strategies on new dates only
  7. Append new trades + SPX bars to calendar_trades.json
  8. Print summary to stdout (captured by cockpit_feed.py)

Usage:
  python3 refresh_new_strategies.py              # uses POLYGON_API_KEY env or cockpit_config.json
  python3 refresh_new_strategies.py YOUR_API_KEY  # explicit key
"""

import os
import sys
import json
import time
import math
import requests
from datetime import datetime, timedelta, date as dt_date

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

DATA_DIR = os.path.join(_DIR, "data")
SPX_DIR = os.path.join(DATA_DIR, "spx_1min")
OPT_DIR = os.path.join(DATA_DIR, "option_chains")
VIX_DIR = os.path.join(DATA_DIR, "vix_1min")
CALENDAR_FILE = os.path.join(_DIR, "calendar_trades.json")

BASE = "https://api.polygon.io"
STRIKE_RANGE = 200
STRIKE_STEP = 5

# Rate limiting
CALLS_PER_SEC = 80
MIN_CALL_GAP = 1.0 / CALLS_PER_SEC
_last_call_time = 0.0

API_KEY = None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS (reused from pull_comprehensive_data.py patterns)
# ─────────────────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def rate_limit():
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < MIN_CALL_GAP:
        time.sleep(MIN_CALL_GAP - elapsed)
    _last_call_time = time.time()


def api_get(url, params=None, retries=4):
    if params is None:
        params = {}
    params["apiKey"] = API_KEY

    for attempt in range(retries):
        rate_limit()
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = min(2 ** attempt * 5, 60)
                log(f"  429 rate limit, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code in (403, 404):
                return None
            log(f"  HTTP {r.status_code}: {url}")
            time.sleep(2)
        except requests.exceptions.Timeout:
            log(f"  Timeout (attempt {attempt+1})")
            time.sleep(5)
        except Exception as e:
            log(f"  Error: {e}")
            time.sleep(2)
    return None


def ts_to_et_str(ts_ms):
    from datetime import timezone
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    month = dt.month
    if 3 <= month <= 10:
        offset = timedelta(hours=-4)
    else:
        offset = timedelta(hours=-5)
    et = dt + offset
    return et.strftime("%H:%M")


def bars_to_dict(results_list):
    out = {}
    if not results_list:
        return out
    for bar in results_list:
        t_str = ts_to_et_str(bar["t"])
        entry = {"o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"]}
        if "v" in bar:
            entry["v"] = bar["v"]
        if "vw" in bar:
            entry["vw"] = round(bar["vw"], 4)
        if "n" in bar:
            entry["n"] = bar["n"]
        out[t_str] = entry
    return out


def option_ticker(date_str, strike, call_put):
    yy = date_str[2:4]
    mm = date_str[5:7]
    dd = date_str[8:10]
    strike_int = int(round(strike * 1000))
    return f"O:SPXW{yy}{mm}{dd}{call_put}{strike_int:08d}"


# ─────────────────────────────────────────────────────────────────────────────
# DATA PULL FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def get_trading_days(start_str, end_str):
    """Get trading days from Polygon using SPY daily bars."""
    for ticker in ["SPY", "I:SPX"]:
        url = f"{BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start_str}/{end_str}"
        data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
        if data and "results" in data and data["results"]:
            days = []
            for bar in data["results"]:
                dt = datetime.fromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
                days.append(dt)
            return days
    return []


def pull_spx_bars(date_str):
    """Pull 1-minute SPX bars. Skips if file already exists."""
    outfile = os.path.join(SPX_DIR, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    url = f"{BASE}/v2/aggs/ticker/I:SPX/range/1/minute/{date_str}/{date_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 1000})
    if data and "results" in data and data["results"]:
        bars = bars_to_dict(data["results"])
        with open(outfile, "w") as f:
            json.dump(bars, f)
        return True

    # Fallback: SPY * 10
    url_spy = f"{BASE}/v2/aggs/ticker/SPY/range/1/minute/{date_str}/{date_str}"
    data_spy = api_get(url_spy, {"adjusted": "true", "sort": "asc", "limit": 1000})
    if data_spy and "results" in data_spy and data_spy["results"]:
        for bar in data_spy["results"]:
            bar["o"] = round(bar["o"] * 10, 2)
            bar["h"] = round(bar["h"] * 10, 2)
            bar["l"] = round(bar["l"] * 10, 2)
            bar["c"] = round(bar["c"] * 10, 2)
        bars = bars_to_dict(data_spy["results"])
        with open(outfile, "w") as f:
            json.dump(bars, f)
        return True
    return False


def pull_vix_bars(date_str):
    """Pull 1-minute VIX bars."""
    outfile = os.path.join(VIX_DIR, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    url = f"{BASE}/v2/aggs/ticker/I:VIX/range/1/minute/{date_str}/{date_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 1000})
    if not data or "results" not in data or not data["results"]:
        return False

    bars = bars_to_dict(data["results"])
    with open(outfile, "w") as f:
        json.dump(bars, f)
    return True


def get_spx_open(date_str):
    """Get SPX open price from already-pulled 1-min bars."""
    spx_file = os.path.join(SPX_DIR, f"{date_str}.json")
    if not os.path.exists(spx_file):
        return None
    with open(spx_file) as f:
        bars = json.load(f)
    for t in ["09:31", "09:30", "09:32", "09:33"]:
        if t in bars:
            return bars[t]["o"]
    return None


def pull_option_chain(date_str, spx_price):
    """Pull 5-minute option bars for all strikes ATM ± STRIKE_RANGE."""
    outfile = os.path.join(OPT_DIR, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    atm = int(round(spx_price / STRIKE_STEP) * STRIKE_STEP)
    lo = atm - STRIKE_RANGE
    hi = atm + STRIKE_RANGE
    strikes = list(range(lo, hi + 1, STRIKE_STEP))

    chain = {
        "date": date_str,
        "spx_at_open": spx_price,
        "atm": atm,
        "strike_range": [lo, hi],
        "strikes": {},
    }

    contracts_fetched = 0
    contracts_empty = 0

    for strike in strikes:
        strike_data = {}
        for cp in ["C", "P"]:
            ticker = option_ticker(date_str, strike, cp)
            url = f"{BASE}/v2/aggs/ticker/{ticker}/range/5/minute/{date_str}/{date_str}"
            data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 500})
            if data and "results" in data and data["results"]:
                strike_data[cp] = bars_to_dict(data["results"])
                contracts_fetched += 1
            else:
                contracts_empty += 1

        if strike_data:
            chain["strikes"][str(strike)] = strike_data

    with open(outfile, "w") as f:
        json.dump(chain, f)

    log(f"  Option chain: {contracts_fetched} contracts, {contracts_empty} empty")
    return contracts_fetched > 0


def update_daily_bars(new_dates):
    """
    Update spx_daily.json and vix_daily.json with any new trading days.
    These files contain daily OHLCV bars needed by DataUniverse for daily context.
    """
    if not new_dates:
        return

    start = new_dates[0]
    end = new_dates[-1]

    # Update SPX daily
    spx_daily_file = os.path.join(DATA_DIR, "spx_daily.json")
    if os.path.exists(spx_daily_file):
        with open(spx_daily_file) as f:
            spx_daily = json.load(f)
    else:
        spx_daily = {"ticker": "I:SPX", "timespan": "day", "bars": {}}

    # Pull new daily bars
    url = f"{BASE}/v2/aggs/ticker/I:SPX/range/1/day/{start}/{end}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 500})
    if data and "results" in data:
        added = 0
        for bar in data["results"]:
            dt = datetime.utcfromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
            if dt not in spx_daily["bars"]:
                spx_daily["bars"][dt] = {
                    "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
                }
                if "v" in bar:
                    spx_daily["bars"][dt]["v"] = bar["v"]
                if "vw" in bar:
                    spx_daily["bars"][dt]["vw"] = round(bar["vw"], 4)
                added += 1
        if added:
            with open(spx_daily_file, "w") as f:
                json.dump(spx_daily, f)
            log(f"  Updated spx_daily.json: +{added} days")

    # Update VIX daily
    vix_daily_file = os.path.join(DATA_DIR, "vix_daily.json")
    if os.path.exists(vix_daily_file):
        with open(vix_daily_file) as f:
            vix_daily = json.load(f)
    else:
        vix_daily = {"ticker": "I:VIX", "timespan": "day", "bars": {}}

    url = f"{BASE}/v2/aggs/ticker/I:VIX/range/1/day/{start}/{end}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 500})
    if data and "results" in data:
        added = 0
        for bar in data["results"]:
            dt = datetime.utcfromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
            if dt not in vix_daily["bars"]:
                vix_daily["bars"][dt] = {
                    "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
                }
                added += 1
        if added:
            with open(vix_daily_file, "w") as f:
                json.dump(vix_daily, f)
            log(f"  Updated vix_daily.json: +{added} days")

    # Update SPX weekly
    spx_weekly_file = os.path.join(DATA_DIR, "spx_weekly.json")
    if os.path.exists(spx_weekly_file):
        with open(spx_weekly_file) as f:
            spx_weekly = json.load(f)
    else:
        spx_weekly = {"ticker": "I:SPX", "timespan": "week", "bars": {}}

    url = f"{BASE}/v2/aggs/ticker/I:SPX/range/1/week/{start}/{end}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 500})
    if data and "results" in data:
        added = 0
        for bar in data["results"]:
            dt = datetime.utcfromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
            if dt not in spx_weekly["bars"]:
                spx_weekly["bars"][dt] = {
                    "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
                }
                added += 1
        if added:
            with open(spx_weekly_file, "w") as f:
                json.dump(spx_weekly, f)
            log(f"  Updated spx_weekly.json: +{added} days")


def pull_one_day(date_str):
    """Pull all required data for a single new trading day."""
    log(f"  Pulling data for {date_str}...")

    # SPX 1-min bars
    if not pull_spx_bars(date_str):
        log(f"    FAILED: no SPX bars for {date_str}")
        return False

    # SPX open for ATM determination
    spx_price = get_spx_open(date_str)
    if spx_price is None:
        log(f"    FAILED: can't get SPX open price")
        return False

    # VIX 1-min bars
    pull_vix_bars(date_str)

    # Option chain (5-min bars)
    if not pull_option_chain(date_str, spx_price):
        log(f"    FAILED: option chain pull failed")
        return False

    log(f"    OK (SPX open: {spx_price:.2f})")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY EXECUTION (mirrors generate_calendar_data.py exactly)
# ─────────────────────────────────────────────────────────────────────────────

def run_strategies_on_dates(new_dates):
    """
    Load the DataUniverse, run all 10 strategies on new_dates, return trades list.
    This mirrors generate_calendar_data.py's logic exactly.
    """
    from research.data import DataUniverse
    from research.exits import profit_target, time_stop, wing_stop, loss_stop, standard_exits
    from research.sweep import run_sweep, ibf_factory, ic_factory

    log("Loading DataUniverse...")
    universe = DataUniverse()
    universe.load(load_quotes=False)
    log(f"  Loaded {len(universe.trading_dates())} trading days")

    def vix_size(date):
        vix = universe.ctx(date, 'vix_prior_close')
        if vix is None:
            return 1.0
        if vix < 20:
            return 1.0
        if vix < 25:
            return 0.5
        return 0.25

    # Strategy definitions — MUST match generate_calendar_data.py exactly
    # Per-strategy stop rules optimized via test_stop_variants.py on 776-day backtest
    strategies = [
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

    all_new_trades = []

    for sname, short, color, sfn, et, exit_fn, pre_fn, intra_fn, risk_budget, grade in strategies:
        log(f"  Running {sname} on {len(new_dates)} new dates...")
        trades = run_sweep(universe, sfn, et, exit_fn, dates=new_dates, slippage=1.0,
                          pre_filter=pre_fn, intra_filter=intra_fn)

        for t in trades:
            vix = universe.ctx(t.date, 'vix_prior_close')
            mult = vix_size(t.date)

            max_risk_per_spread = t.max_risk
            sized_budget = risk_budget * mult
            contracts = max(1, int(sized_budget / (max_risk_per_spread * 100))) if max_risk_per_spread > 0 else 1

            entry_spx = universe.spx_at(t.date, t.entry_time)
            exit_spx = universe.spx_at(t.date, t.exit_time)
            atm = universe.current_atm(t.date, t.entry_time)

            pnl_timeline = {}
            for time_str, pnl_val in t.pnl_timeline.items():
                pnl_timeline[time_str] = round(pnl_val * 100, 2)

            hold_min = 0
            try:
                eh, em = int(t.entry_time[:2]), int(t.entry_time[3:])
                xh, xm = int(t.exit_time[:2]), int(t.exit_time[3:])
                hold_min = (xh * 60 + xm) - (eh * 60 + em)
            except:
                pass

            pnl_dollar_sized = round(t.pnl_per_spread * 100 * contracts, 0)
            risk_deployed = round(max_risk_per_spread * 100 * contracts, 0)

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
                "contracts": contracts,
                "risk_budget": risk_budget,
                "risk_deployed": risk_deployed,
                "pnl_dollar_sized": pnl_dollar_sized,
                "vix_sizing": mult,
                "vix": round(vix, 1) if vix else None,
                "hold_minutes": hold_min,
                "peak_pnl": round(t.peak_pnl * 100, 2),
                "trough_pnl": round(t.trough_pnl * 100, 2),
                "entry_spx": round(entry_spx, 2) if entry_spx else None,
                "exit_spx": round(exit_spx, 2) if exit_spx else None,
                "atm": atm,
                "pnl_timeline": pnl_timeline,
            }
            all_new_trades.append(trade_record)

        log(f"    → {len(trades)} trades")

    # Build SPX bar data for new dates (5-min candles for chart)
    new_spx_bars = {}
    for date in new_dates:
        bars = universe.spx_bars_range(date, "09:30", "16:01")
        if not bars:
            continue
        candles_5m = []
        i = 0
        while i < len(bars):
            bucket = bars[i:i+5]
            if not bucket:
                break
            candle = {
                "t": bucket[0][0],
                "o": round(bucket[0][1]["o"], 2),
                "h": round(max(b["h"] for _, b in bucket), 2),
                "l": round(min(b["l"] for _, b in bucket), 2),
                "c": round(bucket[-1][1]["c"], 2),
            }
            candles_5m.append(candle)
            i += 5
        new_spx_bars[date] = candles_5m

    return all_new_trades, new_spx_bars


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global API_KEY

    # Get API key
    if len(sys.argv) > 1:
        API_KEY = sys.argv[1]
    else:
        API_KEY = os.environ.get("POLYGON_API_KEY")
        if not API_KEY:
            config_path = os.path.join(_DIR, "cockpit_config.json")
            if os.path.exists(config_path):
                with open(config_path) as f:
                    config = json.load(f)
                API_KEY = config.get("polygon_api_key")

    if not API_KEY or API_KEY == "YOUR_KEY_HERE":
        log("ERROR: No Polygon API key. Set POLYGON_API_KEY env var or cockpit_config.json")
        sys.exit(1)

    # Ensure data dirs exist
    for d in [DATA_DIR, SPX_DIR, OPT_DIR, VIX_DIR]:
        os.makedirs(d, exist_ok=True)

    # Step 1: Find last date in calendar_trades.json
    if os.path.exists(CALENDAR_FILE):
        with open(CALENDAR_FILE) as f:
            calendar_data = json.load(f)
        existing_trades = calendar_data.get("trades", [])
        existing_spx = calendar_data.get("spx_bars", {})
        existing_dates = set(t["date"] for t in existing_trades)

        if existing_trades:
            last_date = max(t["date"] for t in existing_trades)
        else:
            last_date = "2023-02-01"
        log(f"Existing calendar: {len(existing_trades)} trades, last date: {last_date}")
    else:
        calendar_data = {"generated": "", "strategies": [], "trades": [], "spx_bars": {}}
        existing_trades = []
        existing_spx = {}
        existing_dates = set()
        last_date = "2023-02-01"
        log("No existing calendar_trades.json — starting fresh")

    # Step 2: Determine new trading days
    # Start from the day AFTER the last trade date
    start_dt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    start_str = start_dt.strftime("%Y-%m-%d")

    # End at yesterday (today's data may be incomplete if market is still open)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if start_str > yesterday:
        log("Calendar is already up to date — nothing to refresh")
        print(json.dumps({"ok": True, "new_trades": 0, "message": "Already up to date"}))
        return

    log(f"Checking for new trading days: {start_str} to {yesterday}...")
    new_trading_days = get_trading_days(start_str, yesterday)

    if not new_trading_days:
        log("No new trading days found")
        print(json.dumps({"ok": True, "new_trades": 0, "message": "No new trading days"}))
        return

    # Filter out any dates we already have trades for (safety check)
    new_trading_days = [d for d in new_trading_days if d not in existing_dates]

    if not new_trading_days:
        log("All trading days already processed")
        print(json.dumps({"ok": True, "new_trades": 0, "message": "All days already processed"}))
        return

    log(f"Found {len(new_trading_days)} new trading days: {new_trading_days[0]} to {new_trading_days[-1]}")

    # Step 3: Pull data for each new day
    log("\n=== PULLING MARKET DATA ===")
    successful_days = []
    for date_str in new_trading_days:
        if pull_one_day(date_str):
            successful_days.append(date_str)
        else:
            log(f"  SKIPPING {date_str} — data pull failed")

    if not successful_days:
        log("No days had successful data pulls")
        print(json.dumps({"ok": False, "error": "All data pulls failed"}))
        return

    log(f"\nSuccessfully pulled data for {len(successful_days)}/{len(new_trading_days)} days")

    # Step 4: Update daily/weekly bars (needed for daily context)
    log("\n=== UPDATING DAILY BARS ===")
    update_daily_bars(successful_days)

    # Step 5: Update daily context
    log("\n=== UPDATING DAILY CONTEXT ===")
    try:
        # Import and call the context computation from pull_comprehensive_data
        # Rather than importing the whole module (which sets globals), we'll
        # just let DataUniverse reload with the new files
        # DataUniverse.load() reads daily_context.json, but the newer strategies
        # don't use regime filters — they're all unfiltered except AM-DEC which
        # uses intraday acceleration. So we can skip full context recomputation
        # and just let DataUniverse load what it has.
        log("  Daily context will be used from existing data + new intraday bars")
    except Exception as e:
        log(f"  Warning: {e}")

    # Step 6: Run strategies on new dates
    log("\n=== RUNNING STRATEGIES ===")
    new_trades, new_spx_bars = run_strategies_on_dates(successful_days)

    log(f"\nGenerated {len(new_trades)} new trades across {len(successful_days)} days")

    # Step 7: Merge into calendar_trades.json
    log("\n=== UPDATING CALENDAR ===")

    # Append new trades
    all_trades = existing_trades + new_trades
    all_trades.sort(key=lambda x: (x["date"], x["entry_time"]))

    # Merge SPX bars
    merged_spx = existing_spx.copy()
    merged_spx.update(new_spx_bars)

    # Rebuild strategy metadata
    strategy_meta = [
        {"name": "Phoenix 75 Power Close", "short": "PHX-PC", "color": "#8b5cf6"},
        {"name": "Phoenix 75 Last Hour", "short": "PHX-LH", "color": "#6366f1"},
        {"name": "Firebird 60 Last Hour", "short": "FBD-LH", "color": "#14b8a6"},
        {"name": "Phoenix 75 Afternoon", "short": "PHX-AFT", "color": "#a855f7"},
        {"name": "Ironclad 35 Condor", "short": "IC-35", "color": "#10b981"},
        {"name": "Firebird 60 Final Bell", "short": "FBD-FB", "color": "#0ea5e9"},
        {"name": "Phoenix 75 Early Afternoon", "short": "PHX-EA", "color": "#f59e0b"},
        {"name": "Phoenix 75 Midday", "short": "PHX-MD", "color": "#ec4899"},
        {"name": "Firebird 60 Midday", "short": "FBD-MD", "color": "#f97316"},
        {"name": "Morning Decel Scalp", "short": "AM-DEC", "color": "#64748b"},
    ]

    output = {
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "strategies": strategy_meta,
        "trades": all_trades,
        "spx_bars": merged_spx,
    }

    with open(CALENDAR_FILE, "w") as f:
        json.dump(output, f)

    file_size_mb = os.path.getsize(CALENDAR_FILE) / 1024 / 1024
    unique_dates = len(set(t["date"] for t in all_trades))

    log(f"\nDone: {len(all_trades)} total trades across {unique_dates} dates")
    log(f"New: {len(new_trades)} trades on {len(successful_days)} new days")
    log(f"Saved to calendar_trades.json ({file_size_mb:.1f} MB)")

    # Print JSON summary for cockpit_feed.py to parse
    print(json.dumps({
        "ok": True,
        "new_trades": len(new_trades),
        "new_days": len(successful_days),
        "total_trades": len(all_trades),
        "total_days": unique_dates,
        "last_date": successful_days[-1] if successful_days else last_date,
    }))


if __name__ == "__main__":
    main()
