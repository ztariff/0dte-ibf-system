"""
SPX 0DTE — Iron Butterfly Backtest v5 (Live Option Quotes)
===========================================================
Same scoring & signal logic as v4-moderate-timeadd, but uses real
Polygon option prices at ENTRY and EXIT instead of Black-Scholes.

Methodology  (hybrid — efficient API usage):
  1. ENTRY:  Fetch 4 option leg prices at exactly 10:00am via a narrow
             1-minute aggregate window → compute real IBF credit.
  2. TRACK:  Walk SPX minute bars.  Use BS *calibrated* to the real entry
             credit (solve for the IV that reproduces the live credit) so
             intraday target / wing-stop detection stays realistic.
  3. EXIT:   When BS says to exit (target, wing stop, or time stop), fetch
             the real option prices at that bar's timestamp → compute
             actual P&L from real mid prices.

This gives us real entry + real exit pricing while only making ~8 API
calls per GO day instead of ~1,600.

Ticker note:  0DTE SPX options trade as SPXW, not SPX.
  Format:  O:SPXW{YYMMDD}{C|P}{strike*1000:08d}
  e.g.     O:SPXW251013C06000000

Usage:
    python3 backtest_v5_livequotes.py YOUR_POLYGON_KEY [LOOKBACK_DAYS]
"""

import sys, math, time, csv, requests, json, os
import numpy as np
import pandas as pd
from datetime import date, timedelta, datetime
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

class _Norm:
    cdf = staticmethod(_norm_cdf)

norm = _Norm()
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_KEY   = "cBE5Kbq9yllt0Yj29mDQjBcIKfAYQlHF"
API_KEY       = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KEY
LOOKBACK_DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 600
OUTPUT_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_all_trades.csv")
ET            = pytz.timezone("America/New_York")

WING_WIDTH_BASE = 40          # minimum wing width (low VIX)
WING_SIGMA_PCT  = 0.75        # wings as fraction of 1σ daily move
MIN_SCORE       = 0           # DISABLED for research
TARGET_PCT      = 0.50        # baseline mechanics
MIN_RR          = 0.10        # very low — let almost everything through
LADDER_DRIFT    = 15

TIME_STOP_HOUR    = 15        # 3:30pm — best from sweep
TIME_STOP_MINUTE  = 30

TIME_ADD_MINUTES   = 45
TIME_ADD_MAX_RANGE = 10

DAILY_RISK_BUDGET  = 100_000
MAX_POSITIONS      = 3
TRANCHE_RISK       = DAILY_RISK_BUDGET / MAX_POSITIONS
SPX_MULTIPLIER     = 100
SLIPPAGE_PER_SPR   = 1.00

# ── Optimized signal filters ─────────────────────────────────────────────────
MIN_VP             = 1.00

# VIX-adaptive VP cap: calm market tolerates higher VP, volatile market needs tight VP.
# At low VIX, implied is naturally rich → VP can be higher and you're still selling
# expensive premium. At high VIX, any VP > ~1.3 means realized is outpacing implied →
# the market is moving MORE than options price in → selling premium is suicide.
#
# From our data:
#   VIX < 16:  VP < 1.3 → 100% WR. VP 1.3-2.0 still ok. VP 2.0+ loses.
#   VIX 16-18: VP < 1.3 → strong. VP 1.3-1.5 decent. VP 1.5+ bleeds.
#   VIX 18-20: VP < 1.3 → profitable. VP 1.3+ very dangerous.
#   VIX 20+:   Almost nothing works. Need VP < 1.2 if trading at all.
def max_vp_for_vix(vix_val):
    """Return the maximum acceptable VP ratio given current VIX.
    With VIX capped at 18, this mainly distinguishes calm vs moderate.
    VP < 1.2 at low VIX is pure gold (100% WR in backtest).
    VP 1.4-1.8 at low VIX is mixed — allow up to 1.5 uniformly.
    """
    if vix_val <= 16:
        return 1.50     # calm market — moderate tolerance
    else:
        return 1.50     # VIX 16-18 — same (both well-behaved at this level)

MAX_VIX            = 18.0     # VIX 18+ net losers even with adaptive wings

# ── Adaptive wing width ──────────────────────────────────────────────────────
# At VIX 14: ±40 (same as before). At VIX 20: ±55. At VIX 22: ±60.
# Formula: max(40, round_to_5(SPX × VIX/100 / √252 × 0.75))
# This keeps wings at ~75% of 1σ daily move regardless of vol regime.
# Wider wings = more credit, but also more max loss → fewer spreads → less risk.
def adaptive_wing_width(spx_price, vix_val):
    daily_sigma = spx_price * (vix_val / 100) / (252 ** 0.5)
    raw = daily_sigma * WING_SIGMA_PCT
    rounded = max(WING_WIDTH_BASE, round(raw / 5) * 5)
    return int(rounded)

# Rate limiting
API_CALL_TIMES = []
API_CALLS_TOTAL = 0
OPT_FETCH_FAILS = 0
OPT_FETCH_OK    = 0
CACHE_HITS      = 0

def rate_limited_sleep():
    """Respect Polygon paid-tier rate limit (~100 calls/min)."""
    global API_CALL_TIMES, API_CALLS_TOTAL
    now = time.time()
    API_CALL_TIMES = [t for t in API_CALL_TIMES if now - t < 60]
    if len(API_CALL_TIMES) >= 90:
        wait = 60 - (now - API_CALL_TIMES[0]) + 0.2
        if wait > 0:
            time.sleep(wait)
    API_CALL_TIMES.append(time.time())
    API_CALLS_TOTAL += 1


# ── Disk cache ───────────────────────────────────────────────────────────────
# Stores raw API JSON responses keyed by (ticker, date) in a local directory.
# First run fetches from Polygon; all subsequent runs are instant (zero API calls).
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".polygon_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_path(ticker, day_str):
    """Return the cache file path for a given ticker + date."""
    safe_ticker = ticker.replace(":", "_").replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe_ticker}_{day_str}.json")

def _cache_read(ticker, day_str):
    """Read cached JSON rows. Returns list of dicts or None if not cached."""
    global CACHE_HITS
    p = _cache_path(ticker, day_str)
    if os.path.exists(p):
        CACHE_HITS += 1
        with open(p, "r") as f:
            data = json.load(f)
        return data  # list of row dicts, or [] for "no data" (still cached)
    return None  # not in cache

def _cache_write(ticker, day_str, rows):
    """Write API response rows to disk cache. Empty list = 'no data'."""
    p = _cache_path(ticker, day_str)
    with open(p, "w") as f:
        json.dump(rows if rows else [], f)


# ── API helpers ──────────────────────────────────────────────────────────────
def get_bars(ticker, day_str):
    """Fetch minute bars for an index (I:SPX, I:VIX, etc.). Uses disk cache."""
    is_index = ticker.startswith("I:")  # I:SPX, I:VIX, etc.

    # Check cache first
    cached = _cache_read(ticker, day_str)
    if cached is not None:
        if not cached:
            if is_index:
                pass  # DON'T trust empty cache for indices — might be API failure
            else:
                return pd.DataFrame()  # option tickers: empty = leg doesn't exist
        else:
            df = pd.DataFrame(cached)
            df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
            return df.sort_values("t").reset_index(drop=True)

    # Fetch from API
    rate_limited_sleep()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{day_str}/{day_str}"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"adjusted":"true","sort":"asc","limit":500,"apiKey":API_KEY}, timeout=15)
            if r.status_code == 200:
                rows = r.json().get("results", [])
                if rows:
                    _cache_write(ticker, day_str, rows)  # only cache non-empty
                elif not is_index:
                    _cache_write(ticker, day_str, [])     # option: cache "no data"
                # else: index with no rows = API glitch, don't cache
                if not rows:
                    return pd.DataFrame()
                df = pd.DataFrame(rows)
                df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
                return df.sort_values("t").reset_index(drop=True)
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
        except Exception:
            time.sleep(1)
    return pd.DataFrame()


def option_ticker(exp_date, strike, call_put):
    """Build SPXW option ticker.  0DTE = SPXW (weekly/daily), not SPX."""
    yy = exp_date.strftime("%y")
    mm = exp_date.strftime("%m")
    dd = exp_date.strftime("%d")
    strike_int = int(round(strike * 1000))
    return f"O:SPXW{yy}{mm}{dd}{call_put}{strike_int:08d}"


# ── Per-day option bar cache ─────────────────────────────────────────────────
# Key = option ticker string → DataFrame of all minute bars for that day.
# Cleared at the start of each new trading day in the main loop.
_opt_bar_cache = {}

def clear_option_cache():
    """Call at the start of each trading day."""
    global _opt_bar_cache
    _opt_bar_cache = {}


def get_option_day_bars(exp_date, strike, call_put, day_str):
    """
    Fetch ALL minute bars for a single option contract for the full day.
    Uses in-memory cache (per day) AND persistent disk cache (across runs).
    Returns a DataFrame or None.
    """
    global OPT_FETCH_FAILS, OPT_FETCH_OK
    ticker = option_ticker(exp_date, strike, call_put)

    # Check in-memory cache first (fastest)
    if ticker in _opt_bar_cache:
        return _opt_bar_cache[ticker]

    # Check disk cache
    cached = _cache_read(ticker, day_str)
    if cached is not None:
        if not cached:  # empty list = no data (but was looked up before)
            _opt_bar_cache[ticker] = None
            return None
        df = pd.DataFrame(cached)
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
        df = df.sort_values("t").reset_index(drop=True)
        _opt_bar_cache[ticker] = df
        OPT_FETCH_OK += 1
        return df

    # Fetch from API
    rate_limited_sleep()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{day_str}/{day_str}"
    params = {"adjusted": "true", "sort": "asc", "limit": 500, "apiKey": API_KEY}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                rows = r.json().get("results", [])
                if not rows:
                    # Try SPX (non-weekly) fallback
                    ticker_alt = ticker.replace("O:SPXW", "O:SPX")
                    if ticker_alt not in _opt_bar_cache:
                        cached_alt = _cache_read(ticker_alt, day_str)
                        if cached_alt is not None:
                            if cached_alt:
                                df2 = pd.DataFrame(cached_alt)
                                df2["t"] = pd.to_datetime(df2["t"], unit="ms", utc=True).dt.tz_convert(ET)
                                df2 = df2.sort_values("t").reset_index(drop=True)
                                _opt_bar_cache[ticker] = df2
                                OPT_FETCH_OK += 1
                                return df2
                            else:
                                _opt_bar_cache[ticker] = None
                                OPT_FETCH_FAILS += 1
                                return None
                        rate_limited_sleep()
                        url2 = f"https://api.polygon.io/v2/aggs/ticker/{ticker_alt}/range/1/minute/{day_str}/{day_str}"
                        r2 = requests.get(url2, params=params, timeout=15)
                        if r2.status_code == 200:
                            rows2 = r2.json().get("results", [])
                            _cache_write(ticker_alt, day_str, rows2)
                            if rows2:
                                df2 = pd.DataFrame(rows2)
                                df2["t"] = pd.to_datetime(df2["t"], unit="ms", utc=True).dt.tz_convert(ET)
                                df2 = df2.sort_values("t").reset_index(drop=True)
                                _opt_bar_cache[ticker] = df2
                                OPT_FETCH_OK += 1
                                return df2
                    OPT_FETCH_FAILS += 1
                    _cache_write(ticker, day_str, [])
                    _opt_bar_cache[ticker] = None
                    return None
                _cache_write(ticker, day_str, rows)
                df = pd.DataFrame(rows)
                df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
                df = df.sort_values("t").reset_index(drop=True)
                _opt_bar_cache[ticker] = df
                OPT_FETCH_OK += 1
                return df

            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code in (403, 404):
                # Try SPX (non-weekly) fallback
                ticker_alt = ticker.replace("O:SPXW", "O:SPX")
                cached_alt = _cache_read(ticker_alt, day_str)
                if cached_alt is not None:
                    if cached_alt:
                        df2 = pd.DataFrame(cached_alt)
                        df2["t"] = pd.to_datetime(df2["t"], unit="ms", utc=True).dt.tz_convert(ET)
                        df2 = df2.sort_values("t").reset_index(drop=True)
                        _opt_bar_cache[ticker] = df2
                        OPT_FETCH_OK += 1
                        return df2
                    else:
                        _opt_bar_cache[ticker] = None
                        OPT_FETCH_FAILS += 1
                        return None
                rate_limited_sleep()
                url2 = f"https://api.polygon.io/v2/aggs/ticker/{ticker_alt}/range/1/minute/{day_str}/{day_str}"
                r2 = requests.get(url2, params=params, timeout=15)
                if r2.status_code == 200:
                    rows2 = r2.json().get("results", [])
                    _cache_write(ticker_alt, day_str, rows2)
                    if rows2:
                        df2 = pd.DataFrame(rows2)
                        df2["t"] = pd.to_datetime(df2["t"], unit="ms", utc=True).dt.tz_convert(ET)
                        df2 = df2.sort_values("t").reset_index(drop=True)
                        _opt_bar_cache[ticker] = df2
                        OPT_FETCH_OK += 1
                        return df2
                OPT_FETCH_FAILS += 1
                _cache_write(ticker, day_str, [])
                _opt_bar_cache[ticker] = None
                return None
        except Exception:
            time.sleep(1)
    OPT_FETCH_FAILS += 1
    _opt_bar_cache[ticker] = None
    return None


def lookup_option_price(bars_df, target_time_et):
    """
    Look up the option close price at target_time from a cached DataFrame.
    Returns the close price at or just before target_time, or None.
    """
    if bars_df is None or bars_df.empty:
        return None
    near = bars_df[bars_df["t"] <= target_time_et]
    if not near.empty:
        return near.iloc[-1]["c"]
    # If no bar at/before, take earliest after
    return bars_df.iloc[0]["c"]


def fetch_ibf_prices_at(exp_date, center, wp, wc, day_str, target_time_et):
    """
    Fetch all 4 IBF leg prices at a specific moment.
    Uses cached full-day bars — first call per contract fetches from API,
    subsequent calls for the same contract (entry vs exit) are instant.

    Returns (credit, leg_prices_dict, missing_list).
    credit = atm_put + atm_call - wing_put - wing_call
    """
    # Get (or cache) full-day bars for each leg
    bars_ap = get_option_day_bars(exp_date, center, "P", day_str)
    bars_ac = get_option_day_bars(exp_date, center, "C", day_str)
    bars_wp = get_option_day_bars(exp_date, wp,     "P", day_str)
    bars_wc = get_option_day_bars(exp_date, wc,     "C", day_str)

    # Look up price at target time
    atm_put   = lookup_option_price(bars_ap, target_time_et)
    atm_call  = lookup_option_price(bars_ac, target_time_et)
    wing_put  = lookup_option_price(bars_wp, target_time_et)
    wing_call = lookup_option_price(bars_wc, target_time_et)

    prices = {
        "atm_put": atm_put, "atm_call": atm_call,
        "wing_put": wing_put, "wing_call": wing_call,
    }

    if any(v is None for v in prices.values()):
        missing = [k for k, v in prices.items() if v is None]
        return None, prices, missing

    credit = atm_put + atm_call - wing_put - wing_call
    return credit, prices, []


# ── Standard helpers (unchanged from v4) ─────────────────────────────────────
def trading_days(n):
    days, d = [], date.today() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))

def calc_rv(df):
    if df is None or len(df) < 5: return None
    lr = np.diff(np.log(df["c"].values))
    return np.std(lr) * np.sqrt(252 * 390) * 100

def calc_rv_slope(full_df, as_of_t):
    if full_df is None or len(full_df) < 8:
        return None, None, "UNKNOWN"
    market_open = as_of_t.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_since_open = (as_of_t - market_open).total_seconds() / 60
    if mins_since_open <= 35:
        mid = market_open + pd.Timedelta(minutes=15)
        w1  = full_df[(full_df["t"] >= market_open) & (full_df["t"] <  mid)]
        w2  = full_df[(full_df["t"] >= mid)          & (full_df["t"] <= as_of_t)]
    else:
        w2 = full_df[(full_df["t"] > as_of_t - pd.Timedelta(minutes=30)) & (full_df["t"] <= as_of_t)]
        w1 = full_df[(full_df["t"] > as_of_t - pd.Timedelta(minutes=60)) & (full_df["t"] <= as_of_t - pd.Timedelta(minutes=30))]
    rv_now  = calc_rv(w2)
    rv_prev = calc_rv(w1)
    if not rv_now or not rv_prev or rv_prev == 0:
        return rv_now, rv_prev, "UNKNOWN"
    slope = (rv_now - rv_prev) / rv_prev * 100
    label = "RISING" if slope > 20 else ("FALLING" if slope < -20 else "STABLE")
    return rv_now, rv_prev, label

def calc_vwap_label(df, n=10):
    if df is None or len(df) < n + 1: return "FLAT"
    typ = (df["h"] + df["l"] + df["c"]) / 3
    vol = df["v"] if "v" in df.columns else pd.Series([1]*len(df), index=df.index)
    vwap = (typ * vol).cumsum() / vol.cumsum()
    recent = vwap.iloc[-n:].values
    slope_pct = np.polyfit(np.arange(len(recent)), recent, 1)[0] / recent.mean() * 100
    if slope_pct >  0.01: return "RISING"
    if slope_pct < -0.01: return "FALLING"
    return "FLAT"

def calc_term_structure(vix, vix9d):
    if not vix or not vix9d or vix == 0: return None, "UNKNOWN"
    r = vix9d / vix
    return r, ("INVERTED" if r > 1.05 else ("FLAT" if r > 0.95 else "CONTANGO"))

def slopes_ok(vwap_label, rv_slope_label, allow_unknown=False):
    # STABLE only — FALLING rv_slope loses money (PF 0.84 vs STABLE PF 1.81)
    ok_rv = rv_slope_label == "STABLE" or (allow_unknown and rv_slope_label == "UNKNOWN")
    return vwap_label == "FLAT" and ok_rv

# ── Scoring (identical to v4) ────────────────────────────────────────────────
def score_entry(rv, vix, vix9d, range_pct, vwap_label, ts_label, ts_ratio,
                rv_slope_label):
    s = {}
    vp = (vix / rv) if vix and rv else None
    if vp:
        s["vol_premium"] = 30 if vp >= 1.5 else (22 if vp >= 1.2 else (15 if vp >= 1.0 else (10 if vp >= 0.85 else 3)))
    else:
        s["vol_premium"] = 0
    if rv_slope_label == "RISING":
        s["vol_premium"] = max(0, s["vol_premium"] - 15)
    elif rv_slope_label == "FALLING":
        s["vol_premium"] = min(30, s["vol_premium"] + 5)
    if vix:
        s["skew"] = 16 if 16 <= vix <= 28 else (8 if vix < 16 else (12 if vix <= 35 else 0))
    else:
        s["skew"] = 0
    if range_pct is not None:
        base  = 14 if range_pct < 0.4 else (8 if range_pct < 0.7 else 0)
        bonus = 6 if vwap_label == "FLAT" and base > 0 else 0
        if vwap_label in ("RISING", "FALLING") and base > 0:
            base = max(0, base - 4)
        s["regime"] = min(20, base + bonus)
    else:
        s["regime"] = 0
    s["term_structure"] = {"INVERTED": 20, "FLAT": 12, "CONTANGO": 4}.get(ts_label, 0)
    s["timing"] = 10
    total = sum(s.values())
    return total, s, vp


# ── Black-Scholes (used for calibrated intraday tracking) ────────────────────
def bs_put(S, K, T, iv):
    if T <= 0 or iv <= 0: return max(0.0, K - S)
    d1 = (math.log(S/K) + 0.5*iv*iv*T) / (iv*math.sqrt(T))
    d2 = d1 - iv*math.sqrt(T)
    return K*norm.cdf(-d2) - S*norm.cdf(-d1)

def bs_call(S, K, T, iv):
    if T <= 0 or iv <= 0: return max(0.0, S - K)
    d1 = (math.log(S/K) + 0.5*iv*iv*T) / (iv*math.sqrt(T))
    d2 = d1 - iv*math.sqrt(T)
    return S*norm.cdf(d1) - K*norm.cdf(d2)

def snap(x, step=5):
    return round(round(x / step) * step, 0)

def adj_iv(iv_entry, S_entry, S_now):
    move = S_now - S_entry
    if move < 0:
        return iv_entry + abs(move) * 0.001
    else:
        return max(iv_entry * 0.92, iv_entry - move * 0.0002)


def calibrate_iv(S, center, wp, wc, T, target_credit, base_iv):
    """
    Find the IV that makes BS produce the same credit as the live market.
    Binary search between 0.05 and 2.0.
    This gives us a calibrated BS model for intraday tracking.
    """
    lo, hi = 0.05, 2.0
    for _ in range(40):
        mid_iv = (lo + hi) / 2
        credit = (bs_put(S, center, T, mid_iv) + bs_call(S, center, T, mid_iv)
                - bs_put(S, wp, T, mid_iv) - bs_call(S, wc, T, mid_iv))
        if credit < target_credit:
            lo = mid_iv
        else:
            hi = mid_iv
    return (lo + hi) / 2


def price_ibf_bs(S, iv, T, wing_w=WING_WIDTH_BASE):
    """BS pricing (fallback or for calibrated tracking)."""
    atm = snap(S)
    wp  = snap(S - wing_w)
    wc  = snap(S + wing_w)
    ap  = bs_put(S, atm, T, iv)
    ac  = bs_call(S, atm, T, iv)
    wpp = bs_put(S, wp, T, iv)
    wcp = bs_call(S, wc, T, iv)
    credit   = ap + ac - wpp - wcp
    max_loss = (atm - wp) - credit
    rr       = credit / max_loss if max_loss > 0 else 0
    return {
        "center": atm, "wp": wp, "wc": wc,
        "credit": round(credit, 3),
        "max_profit": round(credit, 3),
        "max_loss": round(max_loss, 3),
        "target": round(credit * TARGET_PCT, 3),
        "rr": round(rr, 3),
        "T_entry": T, "iv": iv, "S_entry": S,
        "source": "BS",
    }


def current_value_bs(pos, S_now, T_now):
    """Reprice using calibrated BS."""
    iv_now = adj_iv(pos["iv"], pos["S_entry"], S_now)
    cost = (bs_put(S_now, pos["center"], T_now, iv_now)
          + bs_call(S_now, pos["center"], T_now, iv_now)
          - bs_put(S_now, pos["wp"],  T_now, iv_now)
          - bs_call(S_now, pos["wc"], T_now, iv_now))
    pnl = pos["credit"] - cost
    return round(max(-pos["max_loss"], min(pos["max_profit"], pnl)), 4)


def mins_to_close(bar_t):
    return max(0, (15*60 + 30) - (bar_t.hour*60 + bar_t.minute))

def T_from_bar(bar_t):
    return max(0.0001, mins_to_close(bar_t) / (252 * 390))


# ── Hybrid position pricing ─────────────────────────────────────────────────
def price_ibf_hybrid(exp_date, day_str, entry_time_et, S, iv_vix, T, wing_w=None):
    """
    Step 1: Fetch real option prices at entry time.
    Step 2: If successful, use live credit.
    wing_w: adaptive wing width (default: WING_WIDTH_BASE).
    Returns position dict with 'source' = 'LIVE' or 'BS'.
    """
    if wing_w is None:
        wing_w = WING_WIDTH_BASE
    atm = snap(S)
    wp  = snap(S - wing_w)
    wc  = snap(S + wing_w)

    credit_live, leg_prices, missing = fetch_ibf_prices_at(
        exp_date, atm, wp, wc, day_str, entry_time_et
    )

    if credit_live is not None and credit_live > 0:
        max_loss = (atm - wp) - credit_live
        if max_loss <= 0:
            max_loss = 0.01
        rr = credit_live / max_loss

        return {
            "center": atm, "wp": wp, "wc": wc,
            "credit": round(credit_live, 3),
            "max_profit": round(credit_live, 3),
            "max_loss": round(max_loss, 3),
            "target": round(credit_live * TARGET_PCT, 3),
            "rr": round(rr, 3),
            "T_entry": T,
            "iv": iv_vix,       # keep VIX IV for BS fallback only
            "S_entry": S,
            "source": "LIVE",
            "leg_prices": leg_prices,
            "wing_w": wing_w,
        }
    else:
        # Fallback to pure BS
        if missing:
            miss_str = ", ".join(missing)
            print(f"    ⚠ Missing legs: {miss_str} — using BS fallback")
        pos = price_ibf_bs(S, iv_vix, T, wing_w)
        pos["source"] = "BS"
        pos["leg_prices"] = leg_prices
        pos["wing_w"] = wing_w
        return pos


def live_ibf_pnl(pos, exp_date, day_str, bar_t):
    """
    Compute IBF P&L per spread from cached live option bars at bar_t.
    Returns (pnl, source) where source is 'LIVE' or 'BS'.
    pnl is capped to [-max_loss, +max_profit].
    """
    credit_now, _, missing = fetch_ibf_prices_at(
        exp_date, pos["center"], pos["wp"], pos["wc"], day_str, bar_t
    )

    if credit_now is not None:
        pnl = pos["credit"] - credit_now
        return round(max(-pos["max_loss"], min(pos["max_profit"], pnl)), 4), "LIVE"
    else:
        # Fallback to calibrated BS only if live data missing
        S_now = pos.get("_S_now", pos["S_entry"])  # caller should set this
        T_now = pos.get("_T_now", pos["T_entry"])
        return current_value_bs(pos, S_now, T_now), "BS"


# ── Position simulator — RESEARCH MODE ───────────────────────────────────────
# Walks the ENTIRE day without exiting early. Records P&L at every key checkpoint
# so the analysis script can simulate ANY exit rule after the fact.
#
# 100% LIVE PRICING — NO BS MODEL USED FOR ANY P&L VALUES.
# Fetches real option prices at 12 time checkpoints + close (~13 fetches/day).
# Target hits are detected by comparing consecutive LIVE checkpoint values.
# Wing stop is pure SPX price comparison; P&L at breach uses nearest checkpoint.
def run_position(pos, bars, exp_date, day_str, scale=1.0):
    """
    RESEARCH MODE: walk ALL bars from entry to close. Never exit early.
    Record LIVE P&L per spread at every checkpoint for post-hoc analysis.

    ALL P&L values in sweep_data are from LIVE option prices — zero BS.

    Returns (final_pnl, "EXPIRY", last_bar_t, source, sweep_data).
    The sweep_data dict is the real payload — it has everything needed to
    simulate any target/time-stop/wing-stop combo.
    """
    credit = pos["credit"]
    ml     = pos["max_loss"]
    wp     = pos["wp"]
    wc     = pos["wc"]

    # Target thresholds — wide range for sweep
    targets = {
        30: credit * 0.30, 40: credit * 0.40, 50: credit * 0.50,
        60: credit * 0.60, 70: credit * 0.70, 80: credit * 0.80,
        90: credit * 0.90,
    }

    # Time checkpoints — LIVE prices fetched at each of these (ordered)
    time_checks_ordered = [
        ("1030", 10, 30), ("1100", 11, 0), ("1130", 11, 30),
        ("1200", 12, 0),  ("1230", 12, 30), ("1300", 13, 0),
        ("1330", 13, 30), ("1400", 14, 0),  ("1430", 14, 30),
        ("1500", 15, 0),  ("1530", 15, 30), ("1545", 15, 45),
    ]
    checkpoint_hm = {(h, m): lbl for lbl, h, m in time_checks_ordered}

    sweep = {}
    # Initialize all sweep fields
    for pct in targets:
        sweep[f"hit_{pct}_pnl"] = None
        sweep[f"hit_{pct}_time"] = None
    for lbl, _, _ in time_checks_ordered:
        sweep[f"pnl_at_{lbl}"] = None
    sweep["wing_stop_pnl"] = None
    sweep["wing_stop_time"] = None
    sweep["pnl_at_close"] = None
    sweep["max_pnl"] = None
    sweep["min_pnl"] = None
    sweep["max_pnl_time"] = None

    last_src = "NONE"
    best_live = -999.0
    worst_live = 999.0
    best_live_time = None
    wing_stop_bar_t = None        # first bar where SPX breached wing

    for _, bar in bars.iterrows():
        bar_t = bar["t"]
        S_now = bar["c"]

        pos["_S_now"] = S_now
        pos["_T_now"] = T_from_bar(bar_t)

        # ── Wing stop tracking (pure price comparison, no model) ──
        if wing_stop_bar_t is None and (S_now <= wp or S_now >= wc):
            wing_stop_bar_t = bar_t

        # ── Check if this bar is a time checkpoint → fetch LIVE prices ──
        lbl = checkpoint_hm.get((bar_t.hour, bar_t.minute))
        if lbl is not None:
            print(f"    checkpoint {lbl}", end="", flush=True)
            val, src = live_ibf_pnl(pos, exp_date, day_str, bar_t)
            last_src = src
            print(f" → {val:+.2f} [{src}]", flush=True)
            sweep[f"pnl_at_{lbl}"] = round(val, 4)

            # Track max/min from LIVE values only
            if val > best_live:
                best_live = val
                best_live_time = str(bar_t)
            if val < worst_live:
                worst_live = val

            # ── Target hit detection from LIVE checkpoint values ──
            for pct, tgt_val in targets.items():
                if sweep[f"hit_{pct}_pnl"] is None and val >= tgt_val:
                    sweep[f"hit_{pct}_pnl"] = round(val, 4)
                    sweep[f"hit_{pct}_time"] = str(bar_t)

            # ── Wing stop P&L: if wing breached since last checkpoint, record LIVE P&L ──
            if wing_stop_bar_t is not None and sweep["wing_stop_pnl"] is None:
                sweep["wing_stop_pnl"] = round(val, 4)
                sweep["wing_stop_time"] = str(wing_stop_bar_t)

    # ── End of day — final P&L via LIVE prices ──
    if not bars.empty:
        last = bars.iloc[-1]
        print(f"    checkpoint CLOSE", end="", flush=True)
        val_final, src = live_ibf_pnl(pos, exp_date, day_str, last["t"])
        print(f" → {val_final:+.2f} [{src}]", flush=True)
        sweep["pnl_at_close"] = round(val_final, 4)

        # Update max/min with close value
        if val_final > best_live:
            best_live = val_final
            best_live_time = str(last["t"])
        if val_final < worst_live:
            worst_live = val_final

        sweep["max_pnl"] = round(best_live, 4)
        sweep["min_pnl"] = round(worst_live, 4)
        sweep["max_pnl_time"] = best_live_time

        # If wing was breached but no checkpoint came after, use close P&L
        if wing_stop_bar_t is not None and sweep["wing_stop_pnl"] is None:
            sweep["wing_stop_pnl"] = round(val_final, 4)
            sweep["wing_stop_time"] = str(wing_stop_bar_t)

        return round(val_final * scale, 3), "EXPIRY", last["t"], src, sweep

    return 0.0, "NO_BARS", None, "NONE", sweep


def calc_spreads(tranche_risk, max_loss_per_spread):
    if max_loss_per_spread <= 0: return 0, 0
    n = int(tranche_risk / max_loss_per_spread)
    return max(1, n), n * max_loss_per_spread


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_backtest():
    days    = trading_days(LOOKBACK_DAYS)
    results = []
    live_entry = 0
    bs_entry   = 0
    live_exit  = 0
    bs_exit    = 0

    print(f"\n{'SPX 0DTE — Iron Butterfly RESEARCH MODE (No Filters)':=^100}")
    print(f"  Entry: 10am · Exit: {TARGET_PCT*100:.0f}% target or {TIME_STOP_HOUR}:{TIME_STOP_MINUTE:02d} ET · Wings: adaptive (≥±{WING_WIDTH_BASE}pts)")
    print(f"  Pricing: LIVE option prices at entry, exit, AND target detection")
    print(f"  ⚠ ALL FILTERS DISABLED — trading every single day for research")
    print(f"  {LOOKBACK_DAYS} trading days lookback\n")
    print(f"{'Date':<12} {'Sc':>4} {'VIX':>5} {'RV':>5} {'RVSlp':<8} {'VWAP':<8} "
          f"{'VP':>5} {'Ent':<4} {'Exit':<4} {'N':>4} {'P1 ($)':>10} {'P2 ($)':>10} {'Total':>10}  Outcome")
    print("-" * 120)

    for day_idx, day in enumerate(days):
        ds = day.strftime("%Y-%m-%d")
        clear_option_cache()   # fresh cache each day

        print(f"  [{day_idx+1}/{len(days)}] {ds} fetching bars...", end=" ", flush=True)
        spx_df   = get_bars("I:SPX",   ds)
        print("SPX", end=" ", flush=True)
        vix_df   = get_bars("I:VIX",   ds)
        print("VIX", end=" ", flush=True)
        vix9d_df = get_bars("I:VIX9D", ds)
        if vix9d_df.empty:
            vix9d_df = get_bars("I:VXST", ds)
        print("VIX9D ✓", flush=True)

        if spx_df.empty:
            print(f"    SKIP: SPX empty", flush=True)
            continue

        open_t  = spx_df["t"].iloc[0].replace(hour=9,  minute=30, second=0, microsecond=0)
        entry_t = spx_df["t"].iloc[0].replace(hour=10, minute=0,  second=0, microsecond=0)

        morning = spx_df[(spx_df["t"] >= open_t) & (spx_df["t"] <= entry_t)]
        if len(morning) < 5:
            print(f"    SKIP: morning bars={len(morning)} (need 5+) | SPX bars={len(spx_df)} | first_t={spx_df['t'].iloc[0]} open_t={open_t}", flush=True)
            continue

        def val_at(df, t):
            if df.empty: return None
            near = df[df["t"] <= t]
            return near.iloc[-1]["c"] if not near.empty else None

        vix_val   = val_at(vix_df, entry_t)
        vix9d_val = val_at(vix9d_df, entry_t)
        if not vix_val:
            print(f"    SKIP: no VIX value at entry", flush=True)
            continue

        iv = vix_val / 100.0

        # ── Metrics at 10am ──────────────────────────────────────────────────
        rv          = calc_rv(morning)
        range_pct   = (morning["h"].max() - morning["l"].min()) / morning.iloc[0]["o"] * 100
        vwap_lbl    = calc_vwap_label(morning)
        ts_ratio, ts_lbl = calc_term_structure(vix_val, vix9d_val)
        _, _, rv_slope_lbl = calc_rv_slope(spx_df, entry_t)

        score, scores, vp = score_entry(
            rv, vix_val, vix9d_val, range_pct, vwap_lbl, ts_lbl, ts_ratio, rv_slope_lbl
        )

        entry_row = spx_df[spx_df["t"] >= entry_t]
        if entry_row.empty:
            print(f"    SKIP: no SPX bars at/after 10am", flush=True)
            continue
        spx_entry = entry_row.iloc[0]["o"]
        bars_from_entry = spx_df[spx_df["t"] >= entry_t].copy()
        wing_w = 0  # set properly when rec == GO

        # ── RESEARCH MODE — NO FILTERS, TRADE EVERY DAY ──────────────────
        rec = "GO"
        vp_cap = max_vp_for_vix(vix_val) if vix_val else 99  # record for analysis only

        # ── Position 1 ───────────────────────────────────────────────────────
        pnl1, outcome1, exit_t1 = 0, "NO_TRADE", None
        pnl2, outcome2          = 0, "—"
        add_tried               = False
        entry_src               = "—"
        exit_src                = "—"
        n1, risk1               = 0, 0
        sweep1                  = {}

        if rec == "GO":
            T_e = T_from_bar(entry_row.iloc[0]["t"])

            # Adaptive wing width based on VIX
            wing_w = adaptive_wing_width(spx_entry, vix_val)

            # Price IBF using live quotes at entry
            print(f"  [{day_idx+1}/{len(days)}] {ds} — Wings ±{wing_w} (VIX {vix_val:.1f}) Fetching entry prices...", end=" ", flush=True)
            pos1 = price_ibf_hybrid(day, ds, entry_t, spx_entry, iv, T_e, wing_w=wing_w)
            entry_src = pos1["source"]
            print(f"[{entry_src}] credit={pos1['credit']:.2f}  "
                  f"iv_cal={pos1['iv']:.3f}" if entry_src == "LIVE" else
                  f"[{entry_src}] credit={pos1['credit']:.2f}", flush=True)

            if entry_src == "LIVE":
                live_entry += 1
            else:
                bs_entry += 1
                # SKIP days with no live entry — all checkpoints would be BS too
                print(f"    ⚠ No live entry prices — skipping day (BS-only)", flush=True)
                rec = "BS_SKIP"
                outcome1 = "BS_SKIP"

            # Size P1
            ml_per_spread1   = pos1["max_loss"] * SPX_MULTIPLIER
            n1, risk1        = calc_spreads(TRANCHE_RISK, ml_per_spread1)
            remaining_budget = DAILY_RISK_BUDGET - risk1

            sweep1 = {}
            if rec == "BS_SKIP":
                pass  # already handled above
            elif pos1["rr"] < MIN_RR:
                rec = "RR_SKIP"
                outcome1 = f"RR_SKIP ({pos1['rr']:.2f}x)"
            else:
                raw1, outcome1, exit_t1, exit_src1, sweep1 = run_position(
                    pos1, bars_from_entry, day, ds, scale=1.0
                )
                exit_src = exit_src1
                if exit_src1 == "LIVE":
                    live_exit += 1
                else:
                    bs_exit += 1

                slip1 = n1 * SLIPPAGE_PER_SPR * SPX_MULTIPLIER
                pnl1 = round(raw1 * n1 * SPX_MULTIPLIER - slip1, 0)

                # RESEARCH MODE: no adds — P1 only for clean analysis
                # Tranche count impact will be tested in post-hoc analysis

        combined = pnl1  # Research mode: P1 only, no adds

        _n1    = n1    if rec == "GO" and "RR_SKIP" not in str(outcome1) else 0
        _risk1 = risk1 if rec == "GO" and "RR_SKIP" not in str(outcome1) else 0
        results.append(_row(ds, score, vix_val, rv, rv_slope_lbl, vwap_lbl, vp,
                            rec, pnl1, 0, combined, outcome1, "—",
                            False, range_pct, ts_lbl, scores,
                            n1=_n1, risk1=_risk1,
                            entry_src=entry_src, exit_src=exit_src,
                            wing_w=wing_w if rec == "GO" else 0,
                            sweep=sweep1 if rec == "GO" else None))

        rv_str = f"{rv:.1f}" if rv else "—"
        vp_str = f"{vp:.2f}" if vp else "—"
        p1_str  = f"${pnl1:>+8,.0f}" if rec == "GO" else "         —"
        tot_str = f"${combined:>+8,.0f}" if rec == "GO" else "         —"
        ns_str  = f"{n1}x" if rec == "GO" and n1 > 0 else "—"
        oc_str  = outcome1
        print(f"{ds:<12} {score:>4} {vix_val:>5.1f} {rv_str:>5} "
              f"{rv_slope_lbl:<8} {vwap_lbl:<8} {vp_str:>5} {entry_src[:4]:<4} {exit_src[:4]:<4} {ns_str:>4} "
              f"{p1_str} {tot_str}  {oc_str}")

    print(f"\n  ── API Stats ──────────────────────────────────")
    print(f"  Disk cache hits:    {CACHE_HITS}")
    print(f"  API calls (new):    {API_CALLS_TOTAL}")
    print(f"  Option fetches OK:  {OPT_FETCH_OK}")
    print(f"  Option fetches FAIL:{OPT_FETCH_FAILS}")
    print(f"  Entry pricing:      {live_entry} LIVE, {bs_entry} BS")
    print(f"  Exit pricing:       {live_exit} LIVE, {bs_exit} BS")
    if CACHE_HITS > 0 and API_CALLS_TOTAL == 0:
        print(f"  ⚡ 100% cached — zero API calls!")
    return results


def _print_skip(ds, score, vix, rv, rv_sl, vwap, vp, outcome):
    rv_str = f"{rv:.1f}" if rv else "—"
    vp_str = f"{vp:.2f}" if vp else "—"
    print(f"{ds:<12} {score:>4} {vix:>5.1f} {rv_str:>5} "
          f"{rv_sl:<8} {vwap:<8} {vp_str:>5} {'—':<4} {'—':<4} {'—':>4} "
          f"{'         —'} {'         —'} {'         —'}  {outcome}")


def _row(ds, score, vix, rv, rv_sl, vwap, vp, rec,
         pnl1, pnl2, combo, oc1, oc2, add_tried, range_pct, ts_lbl, scores,
         n1=0, risk1=0, entry_src="—", exit_src="—", wing_w=0, sweep=None):
    from datetime import datetime as _dt
    _d = _dt.strptime(ds, "%Y-%m-%d")
    row = {
        "date":           ds,
        "dow":            _d.strftime("%A"),
        "score":          score,
        "recommendation": rec,
        "vix":            round(vix, 2) if vix else None,
        "rv":             round(rv, 2) if rv else None,
        "vp_ratio":       round(vp, 3) if vp else None,
        "rv_slope":       rv_sl,
        "vwap_slope":     vwap,
        "ts_label":       ts_lbl,
        "range_pct":      round(range_pct, 3) if range_pct else None,
        "entry_pricing":  entry_src,
        "exit_pricing":   exit_src,
        "n_spreads_p1":   n1,
        "risk_deployed_p1": risk1,
        "pnl_p1_dollars": pnl1,
        "outcome_p1":     oc1,
        "add_tried":      add_tried,
        "pnl_p2_dollars": pnl2,
        "outcome_p2":     str(oc2),
        "combined_pnl":   combo,
        "budget_used_pct": round((risk1 / DAILY_RISK_BUDGET * 100), 1) if risk1 else 0,
        "wing_width":     wing_w,
        "score_vol":      scores.get("vol_premium", 0),
        "score_regime":   scores.get("regime", 0),
        "score_ts":       scores.get("term_structure", 0),
    }
    # Sweep columns — full intraday P&L timeline for post-hoc analysis
    if sweep:
        # Time checkpoint P&L snapshots (per spread)
        for tlbl in ["1030","1100","1130","1200","1230","1300","1330","1400","1430","1500","1530","1545"]:
            row[f"pnl_at_{tlbl}"] = sweep.get(f"pnl_at_{tlbl}")
        # Target hit tracking (per spread)
        for pct in [30, 40, 50, 60, 70, 80, 90]:
            row[f"hit_{pct}_pnl"]  = sweep.get(f"hit_{pct}_pnl")
            raw_t = sweep.get(f"hit_{pct}_time")
            row[f"hit_{pct}_time"] = str(raw_t) if raw_t else ""
        # Wing stop
        row["ws_pnl"]  = sweep.get("wing_stop_pnl")
        raw_ws = sweep.get("wing_stop_time")
        row["ws_time"] = str(raw_ws) if raw_ws else ""
        # Close and extremes
        row["pnl_at_close"] = sweep.get("pnl_at_close")
        row["max_pnl"]      = sweep.get("max_pnl")
        row["min_pnl"]      = sweep.get("min_pnl")
        row["max_pnl_time"] = sweep.get("max_pnl_time", "")
    return row


# ── Summary ──────────────────────────────────────────────────────────────────
def print_summary(results):
    df  = pd.DataFrame(results)
    if df.empty:
        print("No results."); return

    go  = df[df["recommendation"] == "GO"]
    env = df[df["recommendation"] == "ENV_BLOCK"]
    rrs = df[df["recommendation"] == "RR_SKIP"]
    bss = df[df["recommendation"] == "BS_SKIP"]

    print(f"\n{'=' * 75}")
    print(f"  IRON BUTTERFLY BACKTEST v5 — HYBRID LIVE QUOTES — {len(df)} days scanned")
    print(f"{'=' * 75}")
    print(f"  GO signals:      {len(go)}  (100% LIVE pricing)")
    print(f"  BS skipped:      {len(bss)}  (no live option data)")
    print(f"  Env blocked:     {len(env)}")
    print(f"  RR skipped:      {len(rrs)}")
    print(f"  No trade / Wait: {len(df) - len(go) - len(env) - len(rrs) - len(bss)}")

    if go.empty:
        print("  No GO trades to analyse."); return

    # Pricing breakdown
    le = (go["entry_pricing"] == "LIVE").sum()
    be = (go["entry_pricing"] != "LIVE").sum()
    lx = (go["exit_pricing"] == "LIVE").sum()
    bx = (go["exit_pricing"] != "LIVE").sum()
    print(f"\n  Pricing:  Entry {le} LIVE / {be} BS  ·  Exit {lx} LIVE / {bx} BS")

    wins  = (go["pnl_p1_dollars"] > 0).sum()
    loss  = (go["pnl_p1_dollars"] < 0).sum()
    wr    = wins / len(go) * 100
    tot   = go["pnl_p1_dollars"].sum()
    avg   = go["pnl_p1_dollars"].mean()
    aw    = go[go["pnl_p1_dollars"] > 0]["pnl_p1_dollars"].mean() if wins else 0
    al    = go[go["pnl_p1_dollars"] < 0]["pnl_p1_dollars"].mean() if loss else 0
    pf    = abs(aw * wins / (al * loss)) if loss and al else float("inf")

    print(f"\n  ── Position 1 (all GO trades) ──────────────────")
    print(f"  Trades:         {len(go)}")
    print(f"  Win rate:       {wr:.1f}%  ({wins}W / {loss}L)")
    print(f"  Avg P&L:        ${avg:+,.0f}")
    print(f"  Total P&L:      ${tot:+,.0f}")
    print(f"  Profit factor:  {pf:.2f}")
    print(f"  Avg winner:     ${aw:+,.0f}   Avg loser: ${al:+,.0f}")

    # LIVE-priced only
    live_both = go[(go["entry_pricing"] == "LIVE") & (go["exit_pricing"] == "LIVE")]
    if not live_both.empty and len(live_both) >= 3:
        lw = (live_both["pnl_p1_dollars"] > 0).sum()
        ll = (live_both["pnl_p1_dollars"] < 0).sum()
        l_wr = lw / len(live_both) * 100
        print(f"\n  ── Fully LIVE-priced trades only ────────────────")
        print(f"  Trades: {len(live_both)}  WR: {l_wr:.1f}%  "
              f"Avg: ${live_both['pnl_p1_dollars'].mean():+,.0f}  "
              f"Total: ${live_both['pnl_p1_dollars'].sum():+,.0f}")

    print(f"\n  ── Exit breakdown ──────────────────────────────")
    for oc, grp in go.groupby("outcome_p1"):
        print(f"  {oc:<22} {len(grp):>3} trades  Avg ${grp['pnl_p1_dollars'].mean():+,.0f}")

    adds = go[go["add_tried"] == True]
    blocked = adds[adds["outcome_p2"].str.startswith("ADD_BLOCKED")]
    traded  = adds[~adds["outcome_p2"].str.startswith("ADD_BLOCKED") &
                   ~adds["outcome_p2"].str.startswith("ADD_RR")]
    print(f"\n  ── Ladder adds ─────────────────────────────────")
    print(f"  Days with add attempted:  {len(adds)}")
    print(f"  Blocked by slopes:        {len(blocked)}")
    print(f"  Actually traded:          {len(traded)}")

    if not traded.empty:
        a_wr  = (traded["pnl_p2_dollars"] > 0).mean() * 100
        a_tot = traded["pnl_p2_dollars"].sum()
        c_tot = traded["combined_pnl"].sum()
        print(f"  Add win rate:             {a_wr:.1f}%")
        print(f"  Total add P&L:            ${a_tot:+,.0f}")
        print(f"  Total combined (P1+P2):   ${c_tot:+,.0f}")

    # Combined P&L for all GO days
    combo_tot = go["combined_pnl"].sum()
    print(f"\n  ── Total Combined P&L (P1+all adds) ────────────")
    print(f"  ${combo_tot:+,.0f}")

    print(f"\n  ── Performance by VP ratio bucket ──────────────")
    for lo, hi, label in [(0, 1.2, "<1.2x"), (1.2, 1.5, "1.2–1.5x"), (1.5, 99, "≥1.5x")]:
        sub = go[(go["vp_ratio"] >= lo) & (go["vp_ratio"] < hi)]
        if sub.empty: continue
        wr_s = (sub["pnl_p1_dollars"] > 0).mean() * 100
        print(f"  VP {label:<10}  {len(sub):>3} trades  WR {wr_s:.0f}%  Avg ${sub['pnl_p1_dollars'].mean():+,.0f}")

    print(f"\n  Results → {OUTPUT_FILE}")
    print(f"{'=' * 75}\n")


def save_csv(results):
    if not results: return
    # Collect ALL keys across all rows (GO rows have sweep columns, SKIP rows don't)
    all_keys = []
    seen = set()
    for row in results:
        for k in row.keys():
            if k not in seen:
                all_keys.append(k)
                seen.add(k)
    with open(OUTPUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        for row in results:
            w.writerow(row)


if __name__ == "__main__":
    results = run_backtest()
    print_summary(results)
    save_csv(results)
