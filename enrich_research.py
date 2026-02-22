"""
ENRICH research_all_trades.csv with prior-day & range context signals.
=====================================================================
Pulls SPX daily bars from Polygon (uses disk cache), computes:

  Prior-day signals:
    prior_day_return      – prior day close-to-close return %
    prior_day_range       – prior day (high-low)/open %
    prior_day_direction   – UP / DOWN / FLAT
    prior_day_body_pct    – |close-open|/open % (how trendy vs choppy)
    prior_2d_return       – 2-day cumulative return %
    prior_5d_return       – 5-day cumulative return %

  Range context:
    in_prior_week_range   – 1 if today's open is within prior week's high-low
    pct_in_weekly_range   – where in the range (0=low, 100=high)
    in_prior_month_range  – 1 if today's open is within prior 20-day high-low
    pct_in_monthly_range  – where in the range (0=low, 100=high)

  Volatility context:
    prior_day_rv          – prior day's intraday realized vol (from minute bars)
    rv_1d_change          – change in RV from 2 days ago to prior day

Usage:
    python3 enrich_research.py [POLYGON_KEY]

Reads:  research_all_trades.csv
Writes: research_all_trades.csv (overwrites with new columns added)
"""

import sys, os, json, time, requests
import pandas as pd
import numpy as np
from datetime import date, timedelta

DEFAULT_KEY = "cBE5Kbq9yllt0Yj29mDQjBcIKfAYQlHF"
API_KEY = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KEY

_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(_DIR, "research_all_trades.csv")
CACHE_DIR = os.path.join(_DIR, ".polygon_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Disk cache (shared with backtest) ──────────────────────────────────────
def _cache_path(ticker, day_str):
    safe = ticker.replace(":", "_").replace("/", "_")
    return os.path.join(CACHE_DIR, f"{safe}_{day_str}.json")

def _cache_read(ticker, day_str):
    p = _cache_path(ticker, day_str)
    if os.path.exists(p):
        with open(p, "r") as f:
            return json.load(f)
    return None

def _cache_write(ticker, day_str, rows):
    p = _cache_path(ticker, day_str)
    with open(p, "w") as f:
        json.dump(rows if rows else [], f)

# ── Rate limiting ──────────────────────────────────────────────────────────
API_CALL_TIMES = []
def rate_limited_sleep():
    global API_CALL_TIMES
    now = time.time()
    API_CALL_TIMES = [t for t in API_CALL_TIMES if now - t < 60]
    if len(API_CALL_TIMES) >= 90:
        wait = 60 - (now - API_CALL_TIMES[0]) + 0.2
        if wait > 0:
            time.sleep(wait)
    API_CALL_TIMES.append(time.time())

# ── Fetch daily bars (OHLCV) for a date range ─────────────────────────────
def get_daily_bars(ticker, start_str, end_str):
    """Fetch daily OHLCV bars. Uses a single API call for the range."""
    cache_key = f"{ticker}_daily_{start_str}_{end_str}"
    cached = _cache_read(cache_key, "range")
    if cached is not None and cached:
        return pd.DataFrame(cached)

    rate_limited_sleep()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_str}/{end_str}"
    params = {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": API_KEY}

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                rows = r.json().get("results", [])
                if rows:
                    _cache_write(cache_key, "range", rows)
                    return pd.DataFrame(rows)
                return pd.DataFrame()
            if r.status_code == 429:
                time.sleep(2 ** attempt + 1)
                continue
        except Exception:
            time.sleep(1)
    return pd.DataFrame()


def get_minute_bars_cached(ticker, day_str):
    """Get minute bars from disk cache (populated by backtest). No API call."""
    cached = _cache_read(ticker, day_str)
    if cached is not None and cached:
        return pd.DataFrame(cached)
    return None


def calc_intraday_rv(minute_df):
    """Realized vol from minute bars."""
    if minute_df is None or len(minute_df) < 10:
        return None
    closes = minute_df["c"].values
    lr = np.diff(np.log(closes))
    return np.std(lr) * np.sqrt(252 * 390) * 100


def main():
    print("=" * 70)
    print("  ENRICHING research_all_trades.csv with prior-day & range signals")
    print("=" * 70)

    df = pd.read_csv(CSV_PATH)
    df["date"] = pd.to_datetime(df["date"])
    print(f"  Loaded {len(df)} rows, dates {df['date'].min().date()} to {df['date'].max().date()}")

    # ── Pull SPX daily bars for the full range + 30 day buffer ─────────────
    start = (df["date"].min() - timedelta(days=45)).strftime("%Y-%m-%d")
    end = df["date"].max().strftime("%Y-%m-%d")
    print(f"  Fetching SPX daily bars {start} to {end}...", flush=True)
    daily = get_daily_bars("I:SPX", start, end)

    if daily.empty:
        print("  ERROR: Could not fetch SPX daily bars. Aborting.")
        return

    daily["date"] = pd.to_datetime(daily["t"], unit="ms").dt.normalize()
    daily = daily.sort_values("date").reset_index(drop=True)
    print(f"  Got {len(daily)} daily bars")

    # ── Pre-compute prior-day metrics ──────────────────────────────────────
    daily["return_pct"] = daily["c"].pct_change() * 100
    daily["range_pct"] = (daily["h"] - daily["l"]) / daily["o"] * 100
    daily["body_pct"] = abs(daily["c"] - daily["o"]) / daily["o"] * 100
    daily["direction"] = np.where(daily["c"] > daily["o"] * 1.001, "UP",
                         np.where(daily["c"] < daily["o"] * 0.999, "DOWN", "FLAT"))
    daily["return_2d"] = daily["c"].pct_change(2) * 100
    daily["return_5d"] = daily["c"].pct_change(5) * 100

    # Rolling weekly high/low (prior 5 trading days, not including today)
    daily["week_high"] = daily["h"].shift(1).rolling(5).max()
    daily["week_low"]  = daily["l"].shift(1).rolling(5).min()

    # Rolling monthly high/low (prior 20 trading days, not including today)
    daily["month_high"] = daily["h"].shift(1).rolling(20).max()
    daily["month_low"]  = daily["l"].shift(1).rolling(20).min()

    # Build lookup dict: date → row
    daily_lookup = {}
    for _, row in daily.iterrows():
        daily_lookup[row["date"].date()] = row

    # ── Also compute prior-day intraday RV from cached minute bars ─────────
    print("  Computing prior-day RV from cached minute bars...", flush=True)
    rv_by_date = {}
    unique_dates = sorted(df["date"].dt.date.unique())
    # Also need the day before the earliest date
    all_dates_needed = set()
    for d in unique_dates:
        all_dates_needed.add(d)
        # Find prior trading day from daily bars
    for d in unique_dates:
        d_dt = pd.Timestamp(d)
        prior_rows = daily[daily["date"] < d_dt]
        if not prior_rows.empty:
            prior_date = prior_rows.iloc[-1]["date"].date()
            all_dates_needed.add(prior_date)

    for d in sorted(all_dates_needed):
        ds = d.strftime("%Y-%m-%d")
        minute_df = get_minute_bars_cached("I:SPX", ds)
        if minute_df is not None:
            rv_by_date[d] = calc_intraday_rv(minute_df)

    # ── Enrich each row ────────────────────────────────────────────────────
    print("  Enriching rows...", flush=True)
    new_cols = {
        "prior_day_return": [], "prior_day_range": [], "prior_day_direction": [],
        "prior_day_body_pct": [], "prior_2d_return": [], "prior_5d_return": [],
        "in_prior_week_range": [], "pct_in_weekly_range": [],
        "in_prior_month_range": [], "pct_in_monthly_range": [],
        "prior_day_rv": [], "rv_1d_change": [],
    }

    for _, row in df.iterrows():
        d = row["date"].date()
        d_dt = pd.Timestamp(d)

        # Find prior trading day in daily bars
        prior_rows = daily[daily["date"] < d_dt]
        if len(prior_rows) < 2:
            for col in new_cols:
                new_cols[col].append(None)
            continue

        prior = prior_rows.iloc[-1]
        prior2 = prior_rows.iloc[-2]

        # Prior day signals
        new_cols["prior_day_return"].append(round(prior["return_pct"], 3) if pd.notna(prior["return_pct"]) else None)
        new_cols["prior_day_range"].append(round(prior["range_pct"], 3) if pd.notna(prior["range_pct"]) else None)
        new_cols["prior_day_direction"].append(prior["direction"])
        new_cols["prior_day_body_pct"].append(round(prior["body_pct"], 3) if pd.notna(prior["body_pct"]) else None)
        new_cols["prior_2d_return"].append(round(prior["return_2d"], 3) if pd.notna(prior["return_2d"]) else None)
        new_cols["prior_5d_return"].append(round(prior["return_5d"], 3) if pd.notna(prior["return_5d"]) else None)

        # Range context — use today's open (approximated by prior close for daily data)
        today_open = prior["c"]  # best proxy from daily bars
        # Actually we have today's data in the research CSV — use SPX entry from daily
        today_row = daily_lookup.get(d)
        if today_row is not None:
            today_open = today_row["o"]

        # Weekly range
        wk_hi = prior["week_high"] if pd.notna(prior.get("week_high")) else None
        wk_lo = prior["week_low"] if pd.notna(prior.get("week_low")) else None
        if wk_hi and wk_lo and wk_hi > wk_lo:
            in_wk = 1 if wk_lo <= today_open <= wk_hi else 0
            pct_wk = round((today_open - wk_lo) / (wk_hi - wk_lo) * 100, 1)
        else:
            in_wk, pct_wk = None, None
        new_cols["in_prior_week_range"].append(in_wk)
        new_cols["pct_in_weekly_range"].append(pct_wk)

        # Monthly range
        mo_hi = prior["month_high"] if pd.notna(prior.get("month_high")) else None
        mo_lo = prior["month_low"] if pd.notna(prior.get("month_low")) else None
        if mo_hi and mo_lo and mo_hi > mo_lo:
            in_mo = 1 if mo_lo <= today_open <= mo_hi else 0
            pct_mo = round((today_open - mo_lo) / (mo_hi - mo_lo) * 100, 1)
        else:
            in_mo, pct_mo = None, None
        new_cols["in_prior_month_range"].append(in_mo)
        new_cols["pct_in_monthly_range"].append(pct_mo)

        # Prior day RV
        prior_d = prior["date"].date()
        prior2_d = prior2["date"].date()
        rv_prior = rv_by_date.get(prior_d)
        rv_prior2 = rv_by_date.get(prior2_d)
        new_cols["prior_day_rv"].append(round(rv_prior, 2) if rv_prior else None)
        if rv_prior and rv_prior2 and rv_prior2 > 0:
            new_cols["rv_1d_change"].append(round((rv_prior - rv_prior2) / rv_prior2 * 100, 2))
        else:
            new_cols["rv_1d_change"].append(None)

    # ── Merge into DataFrame and save ──────────────────────────────────────
    for col, vals in new_cols.items():
        df[col] = vals

    # Save back
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df.to_csv(CSV_PATH, index=False)

    print(f"\n  ✓ Enriched CSV saved → {CSV_PATH}")
    print(f"  New columns added: {list(new_cols.keys())}")

    # Quick stats
    go = df[df["recommendation"] == "GO"]
    print(f"\n  Quick stats on GO trades ({len(go)}):")
    for col in new_cols:
        vals = pd.to_numeric(go[col], errors="coerce").dropna()
        if len(vals) > 0:
            print(f"    {col:25s}  mean={vals.mean():>8.2f}  min={vals.min():>8.2f}  max={vals.max():>8.2f}")
        else:
            cats = go[col].dropna()
            if len(cats) > 0:
                print(f"    {col:25s}  {dict(cats.value_counts())}")


if __name__ == "__main__":
    main()
