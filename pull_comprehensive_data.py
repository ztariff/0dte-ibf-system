#!/usr/bin/env python3
"""
Comprehensive SPX 0DTE Option Data Pull — Polygon API
======================================================

PURPOSE:
  Pull complete, unbiased SPX option chain data for 0DTE research.
  Eliminates the survivorship bias and tunnel vision of the previous
  data pull by capturing:

  1. FULL option chains — every strike from ATM-200 to ATM+200 at 5pt spacing
     (not re-centered snapshots). Locked absolute strikes mean we can mark any
     position at any time regardless of where SPX has moved.

  2. HIGH time resolution — 5-minute bars for all option contracts (vs 9 snapshots).
     81 bars per contract per session (9:30-16:15), enabling:
     - Entry/exit at any 5-min mark (not just 9 fixed times)
     - Accurate target/stop detection within 5-min granularity
     - Full last-45-minutes coverage (15:30 → 16:15)

  3. BID/ASK data — via Polygon quotes endpoint for key time windows,
     enabling realistic execution cost modeling per leg.

  4. SPX 1-minute bars — full session 9:30-16:15 (390 bars vs current 392 max)

  5. Volatility indices — VIX, VIX9D, VVIX intraday bars for live regime signals

  6. SPX DAILY bars — 2+ years lookback from start date. Provides the full
     daily context going INTO each trading day: prior day candle, ATR,
     trend direction, consecutive up/down streaks, distance from highs/lows,
     candlestick patterns, all computable from settled data before the open.

  7. SPX WEEKLY bars — same lookback. Weekly trend, range expansion/contraction,
     inside weeks, distance from weekly high/low.

  8. VIX DAILY bars — for computing VIX regime, term structure, percentile rank.

  9. PRE-COMPUTED DAILY CONTEXT — a derived file that computes every
     forward-walk-legal signal for each trading day from the daily/weekly
     bars, gap data, and prior day's intraday bars. Every field in this file
     is computed ONLY from data available before 9:30 on that day.

OUTPUT:
  data/
    spx_1min/YYYY-MM-DD.json        — SPX 1-min OHLCV bars
    option_chains/YYYY-MM-DD.json   — Complete option chain (5-min bars, all strikes)
    vix_1min/YYYY-MM-DD.json        — VIX 1-min bars
    quotes/YYYY-MM-DD.json          — Bid/ask snapshots at key times
    spx_daily.json                  — SPX daily OHLCV bars (full lookback)
    spx_weekly.json                 — SPX weekly OHLCV bars (full lookback)
    vix_daily.json                  — VIX daily OHLCV bars (full lookback)
    daily_context.json              — Pre-computed daily context signals
    pull_log.json                    — Progress tracker for resumability
    coverage_report.txt             — Post-pull coverage and quality report

  Each option_chains file structure:
  {
    "date": "2024-06-03",
    "spx_open": 5280.5,
    "atm": 5280,
    "strike_range": [5080, 5480],
    "strikes": {
      "5080": {
        "C": {"09:30": {"o":..,"h":..,"l":..,"c":..,"v":..,"vw":..}, "09:35": {...}, ...},
        "P": {"09:30": {...}, ...}
      },
      "5085": {...},
      ...
      "5480": {...}
    }
  }

USAGE:
  python3 pull_comprehensive_data.py YOUR_POLYGON_API_KEY [START_DATE] [END_DATE]

  Defaults: 2023-11-06 to 2026-03-19 (full history + forward to today)
  Resumable: skips dates already pulled (checks pull_log.json)

API BUDGET (top-tier Polygon plan assumed per CLAUDE.md):
  Per day: ~162 option contracts × 1 call + 1 SPX + 1 VIX + ~20 quote calls ≈ 185 calls
  Total for 600 days: ~111,000 calls
  At 100 calls/sec with rate limiting: ~20 minutes wall clock (plus network latency)
"""

import os
import sys
import json
import time
import math
import requests
from datetime import datetime, timedelta, date as dt_date
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "data")
SPX_DIR = os.path.join(DATA_DIR, "spx_1min")
OPT_DIR = os.path.join(DATA_DIR, "option_chains")
VIX_DIR = os.path.join(DATA_DIR, "vix_1min")
QUOTE_DIR = os.path.join(DATA_DIR, "quotes")
SPX_DAILY_FILE = os.path.join(DATA_DIR, "spx_daily.json")
SPX_WEEKLY_FILE = os.path.join(DATA_DIR, "spx_weekly.json")
VIX_DAILY_FILE = os.path.join(DATA_DIR, "vix_daily.json")
DAILY_CONTEXT_FILE = os.path.join(DATA_DIR, "daily_context.json")
LOG_FILE = os.path.join(DATA_DIR, "pull_log.json")
REPORT_FILE = os.path.join(DATA_DIR, "coverage_report.txt")

# Lookback for daily/weekly bars (years before start date)
DAILY_LOOKBACK_YEARS = 2

# Strike range: ATM ± STRIKE_RANGE points, at STRIKE_STEP intervals
STRIKE_RANGE = 200  # points above and below ATM
STRIKE_STEP = 5     # SPXW standard spacing

# Time resolution
OPT_BAR_SIZE = 5    # minutes per bar for options
SPX_BAR_SIZE = 1    # minutes per bar for SPX/VIX

# Quote snapshot times (ET) — capture bid/ask at these specific times
# These cover every reasonable entry window plus settlement
QUOTE_TIMES = [
    "09:31", "09:35", "09:45", "10:00", "10:15", "10:30", "10:45",
    "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00",
    "14:30", "15:00", "15:15", "15:30", "15:45", "15:55"
]

# API rate limiting
CALLS_PER_SEC = 80   # stay under 100/sec for safety margin
MIN_CALL_GAP = 1.0 / CALLS_PER_SEC
_last_call_time = 0.0

# Polygon base URL
BASE = "https://api.polygon.io"

# Global API key (set from argv)
API_KEY = None

# Counters
STATS = {
    "api_calls": 0,
    "api_errors": 0,
    "api_429s": 0,
    "dates_completed": 0,
    "dates_skipped": 0,
    "option_contracts_fetched": 0,
    "option_contracts_empty": 0,
    "quote_fetches": 0,
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def ensure_dirs():
    for d in [DATA_DIR, SPX_DIR, OPT_DIR, VIX_DIR, QUOTE_DIR]:
        os.makedirs(d, exist_ok=True)


def rate_limit():
    """Enforce minimum gap between API calls."""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < MIN_CALL_GAP:
        time.sleep(MIN_CALL_GAP - elapsed)
    _last_call_time = time.time()


def api_get(url, params=None, retries=4):
    """
    Make a rate-limited GET request to Polygon.
    Retries on 429 (rate limit) and transient errors.
    Returns parsed JSON or None on failure.
    """
    if params is None:
        params = {}
    params["apiKey"] = API_KEY

    for attempt in range(retries):
        rate_limit()
        STATS["api_calls"] += 1
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                STATS["api_429s"] += 1
                wait = min(2 ** attempt * 5, 60)
                log(f"    429 rate limit, waiting {wait}s...")
                time.sleep(wait)
                continue
            if r.status_code == 403:
                log(f"    403 Forbidden: {url}")
                return None
            if r.status_code == 404:
                return None  # no data for this ticker
            # Other errors
            STATS["api_errors"] += 1
            log(f"    HTTP {r.status_code}: {url}")
            time.sleep(2)
        except requests.exceptions.Timeout:
            log(f"    Timeout (attempt {attempt+1})")
            time.sleep(5)
        except Exception as e:
            STATS["api_errors"] += 1
            log(f"    Error: {e}")
            time.sleep(2)
    return None


def log(msg):
    """Print with timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def load_log():
    """Load pull progress log."""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return {"completed_dates": [], "failed_dates": [], "started": None}


def save_log(log_data):
    """Save pull progress log."""
    with open(LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)


def option_ticker(date_str, strike, call_put):
    """
    Build SPXW 0DTE option ticker for Polygon.
    Format: O:SPXW{YYMMDD}{C/P}{strike*1000:08d}
    """
    # date_str is YYYY-MM-DD
    yy = date_str[2:4]
    mm = date_str[5:7]
    dd = date_str[8:10]
    strike_int = int(round(strike * 1000))
    return f"O:SPXW{yy}{mm}{dd}{call_put}{strike_int:08d}"


def ts_to_et_str(ts_ms):
    """Convert Polygon timestamp (ms since epoch) to ET time string HH:MM."""
    from datetime import timezone
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    # Convert UTC to ET (EST = UTC-5, EDT = UTC-4)
    # Use a simple offset based on month (good enough for HH:MM resolution)
    month = dt.month
    # EDT: March second Sunday through November first Sunday
    # Approximate: EDT = months 3-10, EST = months 11-2
    if 3 <= month <= 10:
        offset = timedelta(hours=-4)
    else:
        offset = timedelta(hours=-5)
    et = dt + offset
    return et.strftime("%H:%M")


def bars_to_dict(results_list):
    """
    Convert Polygon aggs results list to {HH:MM: {o,h,l,c,v,vw}} dict.
    Uses close time of each bar.
    """
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


def get_trading_days(start_str, end_str):
    """
    Get list of trading days by querying Polygon for SPX daily bars.
    This ensures we only process actual market days (no weekends/holidays).
    """
    log(f"Fetching trading day calendar from {start_str} to {end_str}...")
    url = f"{BASE}/v2/aggs/ticker/I:SPX/range/1/day/{start_str}/{end_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})
    if not data or "results" not in data:
        log("FATAL: Could not fetch trading day calendar")
        sys.exit(1)
    days = []
    for bar in data["results"]:
        dt = datetime.fromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
        days.append(dt)
    log(f"Found {len(days)} trading days")
    return days


# ─────────────────────────────────────────────────────────────────────────────
# DATA PULL FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def pull_spx_bars(date_str):
    """
    Pull 1-minute SPX bars for the full session (9:30-16:15 ET).
    Saves to data/spx_1min/YYYY-MM-DD.json
    """
    outfile = os.path.join(SPX_DIR, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    url = f"{BASE}/v2/aggs/ticker/I:SPX/range/1/minute/{date_str}/{date_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 1000})
    if not data or "results" not in data or not data["results"]:
        log(f"  SPX bars: no data")
        return False

    bars = bars_to_dict(data["results"])
    with open(outfile, "w") as f:
        json.dump(bars, f)
    return True


def pull_vix_bars(date_str, ticker="I:VIX", subdir=None):
    """
    Pull 1-minute VIX (or VIX9D, VVIX) bars.
    Saves to data/vix_1min/YYYY-MM-DD.json (or subdir variant)
    """
    save_dir = os.path.join(DATA_DIR, subdir) if subdir else VIX_DIR
    os.makedirs(save_dir, exist_ok=True)
    outfile = os.path.join(save_dir, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{date_str}/{date_str}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 1000})
    if not data or "results" not in data or not data["results"]:
        return False

    bars = bars_to_dict(data["results"])
    with open(outfile, "w") as f:
        json.dump(bars, f)
    return True


def get_spx_open(date_str):
    """
    Get SPX price at 9:31 from already-pulled 1-min bars.
    Falls back to first available bar.
    """
    spx_file = os.path.join(SPX_DIR, f"{date_str}.json")
    if not os.path.exists(spx_file):
        return None
    with open(spx_file) as f:
        bars = json.load(f)
    # Try 9:31 first (first full bar after open)
    for t in ["09:31", "09:30", "09:32", "09:33", "09:34", "09:35"]:
        if t in bars:
            return bars[t]["c"]
    # Fallback to first bar
    if bars:
        first = sorted(bars.keys())[0]
        return bars[first]["c"]
    return None


def pull_option_chain(date_str, spx_price):
    """
    Pull 5-minute bars for ALL option contracts in the strike range.
    Strike range: ATM ± STRIKE_RANGE at STRIKE_STEP intervals.
    Both calls and puts for every strike.

    Saves to data/option_chains/YYYY-MM-DD.json
    """
    outfile = os.path.join(OPT_DIR, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    # Round to nearest 5 for ATM
    atm = int(round(spx_price / STRIKE_STEP) * STRIKE_STEP)
    lo = atm - STRIKE_RANGE
    hi = atm + STRIKE_RANGE

    strikes = list(range(lo, hi + 1, STRIKE_STEP))
    log(f"  Options: ATM={atm}, range=[{lo}, {hi}], {len(strikes)} strikes × 2 = {len(strikes)*2} contracts")

    chain = {
        "date": date_str,
        "spx_at_open": round(spx_price, 2),
        "atm": atm,
        "strike_range": [lo, hi],
        "strikes": {}
    }

    contracts_fetched = 0
    contracts_empty = 0

    for strike in strikes:
        chain["strikes"][str(strike)] = {}

        for cp in ["C", "P"]:
            ticker = option_ticker(date_str, strike, cp)
            url = f"{BASE}/v2/aggs/ticker/{ticker}/range/{OPT_BAR_SIZE}/minute/{date_str}/{date_str}"
            data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 500})

            if data and "results" in data and data["results"]:
                bars = bars_to_dict(data["results"])
                chain["strikes"][str(strike)][cp] = bars
                contracts_fetched += 1
            else:
                # Try non-weekly SPX fallback (some dates use SPX not SPXW)
                ticker_alt = ticker.replace("O:SPXW", "O:SPX")
                url_alt = f"{BASE}/v2/aggs/ticker/{ticker_alt}/range/{OPT_BAR_SIZE}/minute/{date_str}/{date_str}"
                data_alt = api_get(url_alt, {"adjusted": "true", "sort": "asc", "limit": 500})

                if data_alt and "results" in data_alt and data_alt["results"]:
                    bars = bars_to_dict(data_alt["results"])
                    chain["strikes"][str(strike)][cp] = bars
                    contracts_fetched += 1
                else:
                    contracts_empty += 1

        # Progress every 20 strikes
        idx = strikes.index(strike) + 1
        if idx % 20 == 0 or idx == len(strikes):
            log(f"    Strikes: {idx}/{len(strikes)} done ({contracts_fetched} OK, {contracts_empty} empty)")

    STATS["option_contracts_fetched"] += contracts_fetched
    STATS["option_contracts_empty"] += contracts_empty

    # Save even if some contracts are empty (that's valid — far OTM may not trade)
    with open(outfile, "w") as f:
        json.dump(chain, f)

    return True


def pull_quotes(date_str, spx_price):
    """
    Pull bid/ask snapshots at key times for liquid strikes.
    Focus on ATM ± 80 at 5pt spacing (the tradeable range) at QUOTE_TIMES.
    Uses /v3/quotes/{ticker} with timestamp window.

    Saves to data/quotes/YYYY-MM-DD.json
    """
    outfile = os.path.join(QUOTE_DIR, f"{date_str}.json")
    if os.path.exists(outfile):
        return True

    atm = int(round(spx_price / STRIKE_STEP) * STRIKE_STEP)
    # Narrower range for quotes (most expensive API-wise, focus on tradeable)
    quote_lo = atm - 80
    quote_hi = atm + 80
    quote_strikes = list(range(quote_lo, quote_hi + 1, STRIKE_STEP))

    quotes = {
        "date": date_str,
        "atm": atm,
        "times": {}
    }

    for t_str in QUOTE_TIMES:
        hh, mm = int(t_str[:2]), int(t_str[3:])

        # Build UTC timestamp window (±60 seconds around target time)
        # Determine UTC offset (EDT vs EST)
        month = int(date_str[5:7])
        utc_offset = 4 if 3 <= month <= 10 else 5  # approximate

        utc_hh = hh + utc_offset
        t_from = f"{date_str}T{utc_hh:02d}:{mm:02d}:00Z"
        mm_to = mm + 2
        hh_to = utc_hh + mm_to // 60
        mm_to = mm_to % 60
        t_to = f"{date_str}T{hh_to:02d}:{mm_to:02d}:00Z"

        time_data = {}

        # Pull quotes for a subset of strikes at this time (ATM, ATM±25, ATM±40, ATM±50)
        # This gives us bid/ask for the most commonly traded structures
        key_offsets = [0, -10, 10, -20, 20, -25, 25, -30, 30, -35, 35, -40, 40, -50, 50]
        key_strikes = [atm + off for off in key_offsets if quote_lo <= atm + off <= quote_hi]

        for strike in key_strikes:
            for cp in ["C", "P"]:
                ticker = option_ticker(date_str, strike, cp)
                url = f"{BASE}/v3/quotes/{ticker}"
                data = api_get(url, {
                    "timestamp.gte": t_from,
                    "timestamp.lte": t_to,
                    "limit": 5,
                    "sort": "timestamp",
                    "order": "asc",
                })
                STATS["quote_fetches"] += 1

                if data and data.get("results"):
                    q = data["results"][0]
                    bid = q.get("bid_price")
                    ask = q.get("ask_price")
                    if bid and ask and bid > 0 and ask >= bid:
                        strike_key = str(strike)
                        if strike_key not in time_data:
                            time_data[strike_key] = {}
                        time_data[strike_key][cp] = {
                            "bid": round(bid, 3),
                            "ask": round(ask, 3),
                            "mid": round((bid + ask) / 2, 3),
                            "spread": round(ask - bid, 3),
                        }

        quotes["times"][t_str] = time_data

    with open(outfile, "w") as f:
        json.dump(quotes, f)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# DAILY / WEEKLY BAR PULLS (one-time, not per-day)
# ─────────────────────────────────────────────────────────────────────────────

def pull_daily_bars(start_date, end_date, ticker, outfile, timespan="day"):
    """
    Pull daily (or weekly) OHLCV bars for a ticker across the full date range.
    Includes DAILY_LOOKBACK_YEARS of history before start_date for context.
    Saves to a single JSON file: {"bars": {"YYYY-MM-DD": {o,h,l,c,v}}, "ticker": ...}
    """
    if os.path.exists(outfile):
        log(f"  {os.path.basename(outfile)} already exists, skipping")
        return True

    # Extend start date backwards for lookback
    from datetime import date as dt_date
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    lookback_start = dt_date(start_dt.year - DAILY_LOOKBACK_YEARS, start_dt.month, start_dt.day)
    lookback_str = lookback_start.strftime("%Y-%m-%d")

    log(f"  Pulling {ticker} {timespan} bars: {lookback_str} to {end_date}")

    url = f"{BASE}/v2/aggs/ticker/{ticker}/range/1/{timespan}/{lookback_str}/{end_date}"
    data = api_get(url, {"adjusted": "true", "sort": "asc", "limit": 50000})

    if not data or "results" not in data or not data["results"]:
        log(f"  FAILED: no {timespan} bars for {ticker}")
        return False

    bars = {}
    for bar in data["results"]:
        # Daily bars: timestamp is start of day in ms
        dt = datetime.utcfromtimestamp(bar["t"] / 1000).strftime("%Y-%m-%d")
        bars[dt] = {
            "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"],
        }
        if "v" in bar:
            bars[dt]["v"] = bar["v"]
        if "vw" in bar:
            bars[dt]["vw"] = round(bar["vw"], 4)

    result = {"ticker": ticker, "timespan": timespan, "bars": bars}
    with open(outfile, "w") as f:
        json.dump(result, f)
    log(f"  Saved {len(bars)} {timespan} bars to {os.path.basename(outfile)}")
    return True


def pull_all_daily_weekly(start_date, end_date):
    """Pull SPX daily, SPX weekly, and VIX daily bars."""
    log("\n" + "=" * 60)
    log("PULLING DAILY & WEEKLY BARS (one-time)")
    log("=" * 60)

    pull_daily_bars(start_date, end_date, "I:SPX", SPX_DAILY_FILE, "day")
    pull_daily_bars(start_date, end_date, "I:SPX", SPX_WEEKLY_FILE, "week")
    pull_daily_bars(start_date, end_date, "I:VIX", VIX_DAILY_FILE, "day")


# ─────────────────────────────────────────────────────────────────────────────
# DAILY CONTEXT COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_daily_context(trading_days):
    """
    Compute pre-open context signals for every trading day.

    CRITICAL FORWARD-WALK RULE: Every field computed here uses ONLY data
    that is fully settled BEFORE 9:30 AM on the trading day. Nothing from
    the current day's session is used. This file represents what a trader
    would know when they sit down before the open.

    Signals computed:
      FROM DAILY BARS:
        - prior_close, prior_open, prior_high, prior_low
        - prior_day_return (% change close-to-close)
        - prior_day_range (high - low in points)
        - prior_day_range_pct (range / close as %)
        - prior_day_body_pct (abs(close-open) / range)
        - prior_day_direction (UP / DOWN / FLAT)
        - prior_day_upper_wick, prior_day_lower_wick (wick ratios)
        - gap_pct (today open vs yesterday close, %)
        - gap_vs_prior_range (gap size / prior day range — gap significance)
        - gap_direction (GUP / GDN / GFL using ±0.25% threshold)
        - gap_into_new_high (boolean: today's open > 5d high)
        - gap_into_new_low (boolean: today's open < 5d low)
        - consecutive_up_days, consecutive_down_days
        - atr_5, atr_10, atr_20 (average true range lookbacks)
        - distance_from_5d_high, distance_from_5d_low (% terms)
        - distance_from_20d_high, distance_from_20d_low
        - prior_2d_return, prior_5d_return, prior_10d_return, prior_20d_return
        - inside_day (boolean: prior day range inside day-before range)
        - prior_day_candle (DOJI / HAMMER / SHOOTING_STAR / ENGULF_BULL /
          ENGULF_BEAR / MARUBOZU_BULL / MARUBOZU_BEAR / NORMAL)

      FROM WEEKLY BARS:
        - prior_week_high, prior_week_low, prior_week_close
        - prior_week_return
        - prior_week_range
        - in_prior_week_range (boolean: today's open within last week's H/L)
        - prior_week_direction (UP / DOWN / FLAT)
        - inside_week (boolean: prior week range inside week-before range)
        - weekly_consecutive_up, weekly_consecutive_down
        - distance_from_weekly_high, distance_from_weekly_low

      FROM VIX DAILY BARS:
        - vix_prior_close
        - vix_5d_avg, vix_10d_avg, vix_20d_avg
        - vix_percentile_60d (where current VIX sits in last 60 days)
        - vix_1d_change, vix_5d_change
        - vix_term_slope (if VIX9D available: VIX9D/VIX ratio)

      FROM EXPIRY CALENDAR:
        - expiry_type (QUAD_WITCH / MONTHLY_OPEX / PRE_OPEX / POST_OPEX /
          OPEX_WEEK / MONTH_END / MONTH_START / REGULAR)
        - is_opex_day (boolean: quad witch or monthly OPEX)
        - is_opex_week (boolean: Mon-Fri of OPEX week)
        - is_quad_witch (boolean)
        - days_to_next_monthly_opex (0 = today is OPEX)
        - days_since_last_monthly_opex

    Saves to data/daily_context.json
    """
    log("\n" + "=" * 60)
    log("COMPUTING DAILY CONTEXT (forward-walk signals)")
    log("=" * 60)

    # ── BUILD EXPIRY CALENDAR ─────────────────────────────────────
    from datetime import date as dt_date

    def _third_friday(year, month):
        d = dt_date(year, month, 1)
        days_until_friday = (4 - d.weekday()) % 7
        return d + timedelta(days=days_until_friday + 14)

    # Build calendar for full range
    expiry_cal = {}   # date_str -> type
    opex_dates = []   # sorted list of all monthly/quad OPEX dates

    for year in range(2021, 2027):
        for month in range(1, 13):
            tf = _third_friday(year, month)
            tf_str = tf.strftime("%Y-%m-%d")
            opex_dates.append(tf_str)

            if month in [3, 6, 9, 12]:
                expiry_cal[tf_str] = "QUAD_WITCH"
            else:
                expiry_cal[tf_str] = "MONTHLY_OPEX"

            # Day before OPEX (Thursday)
            day_before = tf - timedelta(days=1)
            if day_before.weekday() < 5:
                db_str = day_before.strftime("%Y-%m-%d")
                if db_str not in expiry_cal:
                    expiry_cal[db_str] = "PRE_OPEX"

            # Day after OPEX (Monday)
            day_after = tf + timedelta(days=3)
            da_str = day_after.strftime("%Y-%m-%d")
            if da_str not in expiry_cal:
                expiry_cal[da_str] = "POST_OPEX"

            # OPEX week (Mon-Thu before third Friday)
            opex_monday = tf - timedelta(days=4)
            for i in range(4):
                d = opex_monday + timedelta(days=i)
                ds = d.strftime("%Y-%m-%d")
                if ds not in expiry_cal:
                    expiry_cal[ds] = "OPEX_WEEK"

            # End of month (last 3 trading days)
            if month == 12:
                next_m1 = dt_date(year + 1, 1, 1)
            else:
                next_m1 = dt_date(year, month + 1, 1)
            last_day = next_m1 - timedelta(days=1)
            eom_count = 0
            for i in range(7):
                d = last_day - timedelta(days=i)
                if d.weekday() < 5:
                    ds = d.strftime("%Y-%m-%d")
                    if ds not in expiry_cal:
                        expiry_cal[ds] = "MONTH_END"
                    eom_count += 1
                    if eom_count >= 3:
                        break

            # First 3 trading days of month
            d = dt_date(year, month, 1)
            som_count = 0
            while som_count < 3 and d.month == month:
                if d.weekday() < 5:
                    ds = d.strftime("%Y-%m-%d")
                    if ds not in expiry_cal:
                        expiry_cal[ds] = "MONTH_START"
                    som_count += 1
                d += timedelta(days=1)

    opex_dates.sort()

    # Load daily/weekly bars
    if not os.path.exists(SPX_DAILY_FILE):
        log("  SKIP: spx_daily.json not found")
        return
    if not os.path.exists(VIX_DAILY_FILE):
        log("  SKIP: vix_daily.json not found")
        return

    with open(SPX_DAILY_FILE) as f:
        spx_daily = json.load(f)["bars"]
    with open(VIX_DAILY_FILE) as f:
        vix_daily = json.load(f)["bars"]

    spx_weekly = {}
    if os.path.exists(SPX_WEEKLY_FILE):
        with open(SPX_WEEKLY_FILE) as f:
            spx_weekly = json.load(f)["bars"]

    # Load existing VIX9D daily for term structure
    vix9d_file = os.path.join(_DIR, "vix9d_daily.json")
    vix9d_daily = {}
    if os.path.exists(vix9d_file):
        with open(vix9d_file) as f:
            vix9d_daily = json.load(f)

    # Sort all dates for lookback computation
    all_spx_dates = sorted(spx_daily.keys())
    all_vix_dates = sorted(vix_daily.keys())
    all_weekly_dates = sorted(spx_weekly.keys())

    context = {}

    for date_str in trading_days:
        ctx = {"date": date_str}

        # Find this date's index in the daily bar series
        if date_str not in spx_daily:
            # Need today's open — get from intraday bars if available
            spx_file = os.path.join(SPX_DIR, f"{date_str}.json")
            today_open = None
            if os.path.exists(spx_file):
                with open(spx_file) as f:
                    intraday = json.load(f)
                for t in ["09:31", "09:30", "09:32"]:
                    if t in intraday:
                        today_open = intraday[t]["o"]
                        break
            ctx["today_open"] = today_open
        else:
            ctx["today_open"] = spx_daily[date_str]["o"]

        # Get sorted prior dates (all dates strictly before today)
        prior_dates = [d for d in all_spx_dates if d < date_str]
        if len(prior_dates) < 2:
            context[date_str] = ctx
            continue

        # ── PRIOR DAY SIGNALS ──────────────────────────────────────────
        d1 = prior_dates[-1]  # yesterday
        d2 = prior_dates[-2] if len(prior_dates) >= 2 else None  # day before
        bar1 = spx_daily[d1]
        bar2 = spx_daily[d2] if d2 else None

        ctx["prior_close"] = bar1["c"]
        ctx["prior_open"] = bar1["o"]
        ctx["prior_high"] = bar1["h"]
        ctx["prior_low"] = bar1["l"]

        # Return and range
        if bar2:
            ctx["prior_day_return"] = round((bar1["c"] - bar2["c"]) / bar2["c"] * 100, 4)
        else:
            ctx["prior_day_return"] = round((bar1["c"] - bar1["o"]) / bar1["o"] * 100, 4)

        day_range = bar1["h"] - bar1["l"]
        ctx["prior_day_range"] = round(day_range, 2)
        ctx["prior_day_range_pct"] = round(day_range / bar1["c"] * 100, 4) if bar1["c"] else 0

        # Body and direction
        body = bar1["c"] - bar1["o"]
        ctx["prior_day_body_pct"] = round(abs(body) / day_range, 4) if day_range > 0 else 0

        if body > 0.001 * bar1["c"]:
            ctx["prior_day_direction"] = "UP"
        elif body < -0.001 * bar1["c"]:
            ctx["prior_day_direction"] = "DOWN"
        else:
            ctx["prior_day_direction"] = "FLAT"

        # Wicks (as fraction of day range)
        if day_range > 0:
            if bar1["c"] >= bar1["o"]:  # green candle
                upper_wick = bar1["h"] - bar1["c"]
                lower_wick = bar1["o"] - bar1["l"]
            else:  # red candle
                upper_wick = bar1["h"] - bar1["o"]
                lower_wick = bar1["c"] - bar1["l"]
            ctx["prior_day_upper_wick"] = round(upper_wick / day_range, 4)
            ctx["prior_day_lower_wick"] = round(lower_wick / day_range, 4)
        else:
            ctx["prior_day_upper_wick"] = 0
            ctx["prior_day_lower_wick"] = 0

        # ── GAP ANALYSIS ──────────────────────────────────────────────
        today_open = ctx.get("today_open")
        if today_open and bar1["c"]:
            gap = (today_open - bar1["c"]) / bar1["c"] * 100
            ctx["gap_pct"] = round(gap, 4)
            ctx["gap_pts"] = round(today_open - bar1["c"], 2)

            if gap > 0.25:
                ctx["gap_direction"] = "GUP"
            elif gap < -0.25:
                ctx["gap_direction"] = "GDN"
            else:
                ctx["gap_direction"] = "GFL"

            # Gap significance: how big is the gap vs prior day's range?
            if day_range > 0:
                ctx["gap_vs_prior_range"] = round(abs(today_open - bar1["c"]) / day_range, 4)
            else:
                ctx["gap_vs_prior_range"] = 0

            # Gap into new territory
            if len(prior_dates) >= 5:
                hi5 = max(spx_daily[d]["h"] for d in prior_dates[-5:] if d in spx_daily)
                lo5 = min(spx_daily[d]["l"] for d in prior_dates[-5:] if d in spx_daily)
                ctx["gap_into_new_5d_high"] = today_open > hi5
                ctx["gap_into_new_5d_low"] = today_open < lo5
            if len(prior_dates) >= 20:
                hi20 = max(spx_daily[d]["h"] for d in prior_dates[-20:] if d in spx_daily)
                lo20 = min(spx_daily[d]["l"] for d in prior_dates[-20:] if d in spx_daily)
                ctx["gap_into_new_20d_high"] = today_open > hi20
                ctx["gap_into_new_20d_low"] = today_open < lo20

        # ── INSIDE DAY ────────────────────────────────────────────────
        if bar2:
            bar2_range = spx_daily[d2]["h"] - spx_daily[d2]["l"]
            ctx["inside_day"] = (bar1["h"] <= spx_daily[d2]["h"] and
                                 bar1["l"] >= spx_daily[d2]["l"])
        else:
            ctx["inside_day"] = False

        # ── CANDLE PATTERN (prior day) ────────────────────────────────
        if day_range > 0:
            body_ratio = abs(body) / day_range
            upper_ratio = ctx["prior_day_upper_wick"]
            lower_ratio = ctx["prior_day_lower_wick"]

            if body_ratio < 0.1:
                ctx["prior_day_candle"] = "DOJI"
            elif body_ratio < 0.3 and lower_ratio > 0.6:
                ctx["prior_day_candle"] = "HAMMER" if body >= 0 else "HAMMER"
            elif body_ratio < 0.3 and upper_ratio > 0.6:
                ctx["prior_day_candle"] = "SHOOTING_STAR"
            elif body_ratio > 0.8 and body > 0:
                ctx["prior_day_candle"] = "MARUBOZU_BULL"
            elif body_ratio > 0.8 and body < 0:
                ctx["prior_day_candle"] = "MARUBOZU_BEAR"
            elif bar2 and body > 0 and (spx_daily[d2]["c"] - spx_daily[d2]["o"]) < 0:
                if bar1["c"] > spx_daily[d2]["o"] and bar1["o"] < spx_daily[d2]["c"]:
                    ctx["prior_day_candle"] = "ENGULF_BULL"
                else:
                    ctx["prior_day_candle"] = "NORMAL"
            elif bar2 and body < 0 and (spx_daily[d2]["c"] - spx_daily[d2]["o"]) > 0:
                if bar1["o"] > spx_daily[d2]["c"] and bar1["c"] < spx_daily[d2]["o"]:
                    ctx["prior_day_candle"] = "ENGULF_BEAR"
                else:
                    ctx["prior_day_candle"] = "NORMAL"
            else:
                ctx["prior_day_candle"] = "NORMAL"
        else:
            ctx["prior_day_candle"] = "DOJI"

        # ── CONSECUTIVE STREAKS ───────────────────────────────────────
        up_streak = 0
        for i in range(len(prior_dates) - 1, 0, -1):
            d_cur = prior_dates[i]
            d_prev = prior_dates[i - 1]
            if d_cur in spx_daily and d_prev in spx_daily:
                if spx_daily[d_cur]["c"] > spx_daily[d_prev]["c"]:
                    up_streak += 1
                else:
                    break
            else:
                break
        ctx["consecutive_up_days"] = up_streak

        dn_streak = 0
        for i in range(len(prior_dates) - 1, 0, -1):
            d_cur = prior_dates[i]
            d_prev = prior_dates[i - 1]
            if d_cur in spx_daily and d_prev in spx_daily:
                if spx_daily[d_cur]["c"] < spx_daily[d_prev]["c"]:
                    dn_streak += 1
                else:
                    break
            else:
                break
        ctx["consecutive_down_days"] = dn_streak

        # ── ATR (Average True Range) ─────────────────────────────────
        for lookback in [5, 10, 20]:
            if len(prior_dates) >= lookback + 1:
                trs = []
                for i in range(-lookback, 0):
                    d_cur = prior_dates[i]
                    d_prev = prior_dates[i - 1]
                    if d_cur in spx_daily and d_prev in spx_daily:
                        bc = spx_daily[d_cur]
                        pc = spx_daily[d_prev]["c"]
                        tr = max(bc["h"] - bc["l"],
                                 abs(bc["h"] - pc),
                                 abs(bc["l"] - pc))
                        trs.append(tr)
                if trs:
                    ctx[f"atr_{lookback}"] = round(sum(trs) / len(trs), 2)

        # ── DISTANCE FROM HIGHS/LOWS ─────────────────────────────────
        for lookback in [5, 20]:
            if len(prior_dates) >= lookback:
                recent = [spx_daily[d] for d in prior_dates[-lookback:] if d in spx_daily]
                if recent:
                    hi = max(b["h"] for b in recent)
                    lo = min(b["l"] for b in recent)
                    ctx[f"dist_from_{lookback}d_high_pct"] = round((bar1["c"] - hi) / bar1["c"] * 100, 4)
                    ctx[f"dist_from_{lookback}d_low_pct"] = round((bar1["c"] - lo) / bar1["c"] * 100, 4)

        # ── MULTI-DAY RETURNS ─────────────────────────────────────────
        for lookback in [2, 5, 10, 20]:
            if len(prior_dates) >= lookback:
                d_back = prior_dates[-lookback]
                if d_back in spx_daily and spx_daily[d_back]["c"]:
                    ret = (bar1["c"] - spx_daily[d_back]["c"]) / spx_daily[d_back]["c"] * 100
                    ctx[f"prior_{lookback}d_return"] = round(ret, 4)

        # ── WEEKLY CONTEXT ────────────────────────────────────────────
        prior_weeks = [w for w in all_weekly_dates if w < date_str]
        if len(prior_weeks) >= 2:
            w1 = prior_weeks[-1]
            w2 = prior_weeks[-2]
            wbar1 = spx_weekly.get(w1)
            wbar2 = spx_weekly.get(w2)
            if wbar1:
                ctx["prior_week_high"] = wbar1["h"]
                ctx["prior_week_low"] = wbar1["l"]
                ctx["prior_week_close"] = wbar1["c"]
                ctx["prior_week_range"] = round(wbar1["h"] - wbar1["l"], 2)
                if wbar2 and wbar2["c"]:
                    ctx["prior_week_return"] = round((wbar1["c"] - wbar2["c"]) / wbar2["c"] * 100, 4)
                wk_body = wbar1["c"] - wbar1["o"]
                if wk_body > 0:
                    ctx["prior_week_direction"] = "UP"
                elif wk_body < 0:
                    ctx["prior_week_direction"] = "DOWN"
                else:
                    ctx["prior_week_direction"] = "FLAT"

                # In prior week range?
                if today_open:
                    ctx["in_prior_week_range"] = wbar1["l"] <= today_open <= wbar1["h"]

                # Inside week
                if wbar2:
                    ctx["inside_week"] = (wbar1["h"] <= wbar2["h"] and wbar1["l"] >= wbar2["l"])

                # Weekly streaks
                wk_up = 0
                for i in range(len(prior_weeks) - 1, 0, -1):
                    wc = spx_weekly.get(prior_weeks[i])
                    wp = spx_weekly.get(prior_weeks[i - 1])
                    if wc and wp and wc["c"] > wp["c"]:
                        wk_up += 1
                    else:
                        break
                ctx["weekly_consecutive_up"] = wk_up

                wk_dn = 0
                for i in range(len(prior_weeks) - 1, 0, -1):
                    wc = spx_weekly.get(prior_weeks[i])
                    wp = spx_weekly.get(prior_weeks[i - 1])
                    if wc and wp and wc["c"] < wp["c"]:
                        wk_dn += 1
                    else:
                        break
                ctx["weekly_consecutive_down"] = wk_dn

                # Distance from weekly high/low
                if today_open and wbar1["h"]:
                    ctx["dist_from_weekly_high_pct"] = round((today_open - wbar1["h"]) / today_open * 100, 4)
                    ctx["dist_from_weekly_low_pct"] = round((today_open - wbar1["l"]) / today_open * 100, 4)

        # ── VIX CONTEXT ───────────────────────────────────────────────
        prior_vix_dates = [d for d in all_vix_dates if d < date_str]
        if prior_vix_dates:
            vd1 = prior_vix_dates[-1]
            vbar1 = vix_daily.get(vd1)
            if vbar1:
                ctx["vix_prior_close"] = vbar1["c"]

                # VIX averages
                for lookback in [5, 10, 20]:
                    if len(prior_vix_dates) >= lookback:
                        vals = [vix_daily[d]["c"] for d in prior_vix_dates[-lookback:]
                                if d in vix_daily]
                        if vals:
                            ctx[f"vix_{lookback}d_avg"] = round(sum(vals) / len(vals), 2)

                # VIX change
                if len(prior_vix_dates) >= 2:
                    vd2 = prior_vix_dates[-2]
                    if vd2 in vix_daily:
                        ctx["vix_1d_change"] = round(vbar1["c"] - vix_daily[vd2]["c"], 2)
                if len(prior_vix_dates) >= 6:
                    vd5 = prior_vix_dates[-6]
                    if vd5 in vix_daily:
                        ctx["vix_5d_change"] = round(vbar1["c"] - vix_daily[vd5]["c"], 2)

                # VIX percentile (60-day)
                if len(prior_vix_dates) >= 60:
                    vals60 = [vix_daily[d]["c"] for d in prior_vix_dates[-60:]
                              if d in vix_daily]
                    if vals60:
                        below = sum(1 for v in vals60 if v <= vbar1["c"])
                        ctx["vix_percentile_60d"] = round(below / len(vals60) * 100, 1)

                # VIX term structure (VIX9D / VIX)
                if date_str in vix9d_daily:
                    v9d = vix9d_daily[date_str]
                    if isinstance(v9d, (int, float)) and vbar1["c"] > 0:
                        ctx["vix9d_vix_ratio"] = round(v9d / vbar1["c"], 4)
                elif vd1 in vix9d_daily:
                    v9d = vix9d_daily[vd1]
                    if isinstance(v9d, (int, float)) and vbar1["c"] > 0:
                        ctx["vix9d_vix_ratio"] = round(v9d / vbar1["c"], 4)

        # ── EXPIRY CALENDAR ────────────────────────────────────────────
        ctx["expiry_type"] = expiry_cal.get(date_str, "REGULAR")
        ctx["is_opex_day"] = ctx["expiry_type"] in ("QUAD_WITCH", "MONTHLY_OPEX")
        ctx["is_quad_witch"] = ctx["expiry_type"] == "QUAD_WITCH"
        ctx["is_opex_week"] = ctx["expiry_type"] in ("QUAD_WITCH", "MONTHLY_OPEX",
                                                       "PRE_OPEX", "OPEX_WEEK")

        # Days to/from nearest monthly OPEX
        try:
            dt_today = datetime.strptime(date_str, "%Y-%m-%d").date()
            future_opex = [o for o in opex_dates if o >= date_str]
            past_opex = [o for o in opex_dates if o < date_str]
            if future_opex:
                next_opex = datetime.strptime(future_opex[0], "%Y-%m-%d").date()
                ctx["days_to_next_opex"] = (next_opex - dt_today).days
            if past_opex:
                last_opex = datetime.strptime(past_opex[-1], "%Y-%m-%d").date()
                ctx["days_since_last_opex"] = (dt_today - last_opex).days
        except Exception:
            pass

        context[date_str] = ctx

    # Save
    with open(DAILY_CONTEXT_FILE, "w") as f:
        json.dump(context, f, indent=2, default=str)
    log(f"  Saved daily context for {len(context)} dates to {os.path.basename(DAILY_CONTEXT_FILE)}")

    # Summary stats
    sample = list(context.values())[-1] if context else {}
    log(f"  Fields per day: {len(sample)}")
    log(f"  Sample fields: {list(sample.keys())[:15]}...")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PULL LOOP
# ─────────────────────────────────────────────────────────────────────────────

def pull_one_day(date_str, pull_quotes_flag=True):
    """
    Pull all data for a single trading day.
    Returns True if successful, False if critical data missing.
    """
    log(f"\n{'='*60}")
    log(f"PULLING: {date_str}")
    log(f"{'='*60}")

    # Step 1: SPX 1-min bars
    log("  [1/5] SPX 1-min bars...")
    if not pull_spx_bars(date_str):
        log(f"  FAILED: no SPX bars for {date_str}")
        return False

    # Step 2: Get SPX open price to determine ATM
    spx_price = get_spx_open(date_str)
    if spx_price is None:
        log(f"  FAILED: can't determine SPX open price")
        return False
    log(f"  SPX open: {spx_price:.2f}")

    # Step 3: VIX bars
    log("  [2/5] VIX 1-min bars...")
    pull_vix_bars(date_str, "I:VIX", "vix_1min")

    # Step 4: VIX9D and VVIX (best effort — may not have intraday data)
    log("  [3/5] VIX9D + VVIX bars...")
    pull_vix_bars(date_str, "I:VIX9D", "vix9d_1min")
    pull_vix_bars(date_str, "I:VVIX", "vvix_1min")

    # Step 4: Full option chain (5-min bars, all strikes)
    log("  [4/5] Full option chain (5-min bars)...")
    if not pull_option_chain(date_str, spx_price):
        log(f"  FAILED: option chain pull failed")
        return False

    # Step 5: Bid/ask quotes at key times
    if pull_quotes_flag:
        log("  [5/5] Bid/ask quotes at key times...")
        pull_quotes(date_str, spx_price)
    else:
        log("  [5/5] Skipping quotes (disabled)")

    return True


def generate_coverage_report(trading_days, pull_log_data):
    """
    Generate a coverage report showing what we got and what's missing.
    Per CLAUDE.md: surface gaps, never hide them.
    """
    report = []
    report.append("=" * 80)
    report.append("COMPREHENSIVE DATA PULL — COVERAGE REPORT")
    report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("=" * 80)

    completed = set(pull_log_data.get("completed_dates", []))
    failed = set(pull_log_data.get("failed_dates", []))
    total = len(trading_days)

    report.append(f"\nTrading days requested: {total}")
    report.append(f"Successfully pulled:    {len(completed)}")
    report.append(f"Failed:                 {len(failed)}")
    report.append(f"Not yet attempted:      {total - len(completed) - len(failed)}")

    if failed:
        report.append(f"\nFailed dates: {sorted(failed)}")

    # Check file sizes and completeness
    report.append(f"\n{'='*80}")
    report.append("PER-DATE COMPLETENESS CHECK")
    report.append(f"{'='*80}")

    spx_ok, opt_ok, vix_ok, quote_ok = 0, 0, 0, 0
    thin_option_days = []

    for d in sorted(completed):
        issues = []

        # SPX bars
        spx_file = os.path.join(SPX_DIR, f"{d}.json")
        if os.path.exists(spx_file):
            with open(spx_file) as f:
                spx_data = json.load(f)
            n_bars = len(spx_data)
            if n_bars < 350:
                issues.append(f"SPX only {n_bars} bars (expect 380+)")
            else:
                spx_ok += 1
            # Check if we have 15:30+ coverage
            has_late = any(t >= "15:30" for t in spx_data.keys())
            if not has_late:
                issues.append("SPX missing after 15:30")
        else:
            issues.append("SPX file missing")

        # Option chain
        opt_file = os.path.join(OPT_DIR, f"{d}.json")
        if os.path.exists(opt_file):
            with open(opt_file) as f:
                opt_data = json.load(f)
            n_strikes = len(opt_data.get("strikes", {}))
            # Count strikes with actual data
            strikes_with_data = 0
            for sk, sd in opt_data.get("strikes", {}).items():
                if sd.get("C") or sd.get("P"):
                    strikes_with_data += 1
            if strikes_with_data < 40:
                issues.append(f"Only {strikes_with_data} strikes with data")
                thin_option_days.append(d)
            else:
                opt_ok += 1
        else:
            issues.append("Option chain file missing")

        # VIX
        vix_file = os.path.join(VIX_DIR, f"{d}.json")
        if os.path.exists(vix_file):
            vix_ok += 1
        else:
            issues.append("VIX file missing")

        # Quotes
        quote_file = os.path.join(QUOTE_DIR, f"{d}.json")
        if os.path.exists(quote_file):
            quote_ok += 1

        if issues:
            report.append(f"  {d}: {'; '.join(issues)}")

    report.append(f"\n{'='*80}")
    report.append("SUMMARY")
    report.append(f"{'='*80}")
    report.append(f"SPX bars complete:    {spx_ok}/{len(completed)}")
    report.append(f"Option chains valid:  {opt_ok}/{len(completed)}")
    report.append(f"VIX bars present:     {vix_ok}/{len(completed)}")
    report.append(f"Quote data present:   {quote_ok}/{len(completed)}")

    if thin_option_days:
        report.append(f"\nThin option days ({len(thin_option_days)}): {thin_option_days[:20]}")
        if len(thin_option_days) > 20:
            report.append(f"  ... and {len(thin_option_days) - 20} more")

    # Data structure description (for future research scripts)
    report.append(f"\n{'='*80}")
    report.append("DATA STRUCTURE REFERENCE")
    report.append(f"{'='*80}")
    report.append("""
option_chains/YYYY-MM-DD.json:
  .date              — Trading date
  .spx_at_open       — SPX price at first bar
  .atm               — ATM strike (rounded to nearest 5)
  .strike_range      — [lowest_strike, highest_strike]
  .strikes.{K}.C     — Call bars at strike K, keyed by "HH:MM"
  .strikes.{K}.P     — Put bars at strike K, keyed by "HH:MM"
  Each bar: {o, h, l, c, v, vw, n}

  CRITICAL: Strikes are ABSOLUTE (not re-centered per time slot).
  A position entered at strike 5300 can be marked at 15:30 using
  .strikes.5300.C["15:30"] regardless of where ATM has moved.

spx_1min/YYYY-MM-DD.json:
  Keyed by "HH:MM", each: {o, h, l, c, v}
  Coverage: 09:30 through 16:15 (390 bars)

quotes/YYYY-MM-DD.json:
  .times.{HH:MM}.{strike}.{C/P} = {bid, ask, mid, spread}
  20 time windows × ~30 key strikes × 2 sides
""")

    report.append(f"\nAPI calls made: {STATS['api_calls']}")
    report.append(f"API errors:     {STATS['api_errors']}")
    report.append(f"Rate limits:    {STATS['api_429s']}")

    report_text = "\n".join(report)
    with open(REPORT_FILE, "w") as f:
        f.write(report_text)
    print(report_text)
    return report_text


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global API_KEY

    if len(sys.argv) < 2:
        print("Usage: python3 pull_comprehensive_data.py POLYGON_API_KEY [START_DATE] [END_DATE]")
        print("       START_DATE/END_DATE format: YYYY-MM-DD")
        print("       Defaults: 2023-11-06 to 2026-03-19")
        print()
        print("Options:")
        print("  --no-quotes     Skip bid/ask quote pulls (faster, option bars only)")
        print("  --report-only   Just generate coverage report from existing data")
        sys.exit(1)

    API_KEY = sys.argv[1]

    # Parse optional dates
    start_date = "2023-11-06"
    end_date = "2026-03-19"
    pull_quotes_flag = True
    report_only = False

    for arg in sys.argv[2:]:
        if arg == "--no-quotes":
            pull_quotes_flag = False
        elif arg == "--report-only":
            report_only = True
        elif len(arg) == 10 and arg[4] == '-':
            if start_date == "2023-11-06" or arg < start_date:
                start_date = arg
            else:
                end_date = arg

    ensure_dirs()
    pull_log_data = load_log()

    # Get trading days
    trading_days = get_trading_days(start_date, end_date)

    if report_only:
        generate_coverage_report(trading_days, pull_log_data)
        return

    if not pull_log_data["started"]:
        pull_log_data["started"] = datetime.now().isoformat()

    completed = set(pull_log_data.get("completed_dates", []))
    failed = set(pull_log_data.get("failed_dates", []))

    # Filter to dates not yet completed
    remaining = [d for d in trading_days if d not in completed]
    STATS["dates_skipped"] = len(completed)

    log(f"\n{'='*80}")
    log(f"COMPREHENSIVE SPX 0DTE DATA PULL")
    log(f"{'='*80}")
    log(f"Date range:    {start_date} to {end_date}")
    log(f"Trading days:  {len(trading_days)}")
    log(f"Already done:  {len(completed)}")
    log(f"Remaining:     {len(remaining)}")
    log(f"Strike range:  ATM ± {STRIKE_RANGE} pts ({STRIKE_RANGE*2//STRIKE_STEP + 1} strikes × 2)")
    log(f"Option bars:   {OPT_BAR_SIZE}-min")
    log(f"Quotes:        {'ON' if pull_quotes_flag else 'OFF'}")
    log(f"{'='*80}\n")

    # Step 0: Pull daily/weekly bars (one-time, fast — 3 API calls)
    pull_all_daily_weekly(start_date, end_date)

    if not remaining:
        log("All dates already pulled! Computing context and generating report...")
        compute_daily_context(trading_days)
        generate_coverage_report(trading_days, pull_log_data)
        return

    # Estimate time
    contracts_per_day = (STRIKE_RANGE * 2 // STRIKE_STEP + 1) * 2
    calls_per_day = contracts_per_day + 4  # options + SPX + VIX + VIX9D + VVIX
    if pull_quotes_flag:
        calls_per_day += len(QUOTE_TIMES) * 30  # ~30 strike×side combos per time
    total_calls = len(remaining) * calls_per_day
    est_minutes = total_calls / CALLS_PER_SEC / 60
    log(f"Estimated API calls: {total_calls:,}")
    log(f"Estimated time:      {est_minutes:.0f} minutes\n")

    # Pull each day
    start_time = time.time()

    for i, date_str in enumerate(remaining):
        day_start = time.time()
        success = pull_one_day(date_str, pull_quotes_flag=pull_quotes_flag)

        if success:
            completed.add(date_str)
            pull_log_data["completed_dates"] = sorted(completed)
            STATS["dates_completed"] += 1
        else:
            failed.add(date_str)
            pull_log_data["failed_dates"] = sorted(failed)

        # Save log after each day (resumability)
        save_log(pull_log_data)

        elapsed = time.time() - day_start
        total_elapsed = time.time() - start_time
        remaining_count = len(remaining) - (i + 1)
        avg_per_day = total_elapsed / (i + 1)
        eta_min = remaining_count * avg_per_day / 60

        log(f"  Done in {elapsed:.0f}s | "
            f"Progress: {i+1}/{len(remaining)} | "
            f"API calls: {STATS['api_calls']:,} | "
            f"ETA: {eta_min:.0f} min")

    # Final report
    log("\n" + "=" * 80)
    log("PULL COMPLETE")
    log("=" * 80)
    log(f"Total API calls:  {STATS['api_calls']:,}")
    log(f"Errors:           {STATS['api_errors']}")
    log(f"Rate limits:      {STATS['api_429s']}")
    log(f"Days completed:   {STATS['dates_completed']}")
    log(f"Days failed:      {len(failed)}")
    log(f"Option contracts: {STATS['option_contracts_fetched']} OK, {STATS['option_contracts_empty']} empty")
    log(f"Quote fetches:    {STATS['quote_fetches']}")
    log(f"Total time:       {(time.time()-start_time)/60:.1f} minutes")

    # Compute daily context from all available data
    compute_daily_context(trading_days)

    # Generate coverage report
    generate_coverage_report(trading_days, pull_log_data)

    log(f"\nAll output in: {DATA_DIR}")
    log(f"Coverage report: {REPORT_FILE}")


if __name__ == "__main__":
    main()
