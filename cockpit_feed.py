"""
PHOENIX 0DTE IBF COCKPIT — Live Data Feed
==========================================
Polls Polygon.io for SPX, VIX, and 0DTE option chain.
Computes regime classification, signal filters, and writes cockpit_state.json.
The HTML cockpit reads this file on auto-refresh.

Usage:
    python3 cockpit_feed.py

Requires: pip install requests
Config:  cockpit_config.json (set your Polygon API key there)
"""

import os, json, time, sys, math
from datetime import datetime, timedelta
import requests
import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "cockpit_config.json")
STATE_PATH = os.path.join(_DIR, "cockpit_state.json")
TRADE_PATH       = os.path.join(_DIR, "trade_active.json")
LIVE_TRADES_PATH = os.path.join(_DIR, "live_trades.json")

# ─── Load config (file optional — env vars take precedence in cloud) ───
try:
    with open(CONFIG_PATH) as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    CONFIG = {}

# API key: env var first (Railway/cloud), then config file, then placeholder
API_KEY    = os.environ.get("POLYGON_API_KEY") or CONFIG.get("polygon_api_key", "YOUR_KEY_HERE")
DAILY_RISK = int(os.environ.get("DAILY_RISK", CONFIG.get("daily_risk", 100000)))
POLL_SEC   = int(os.environ.get("POLL_INTERVAL_SEC", CONFIG.get("poll_interval_sec", 10)))
BASE = "https://api.polygon.io"

# Adaptive wing width — mirrors backtest_research.py exactly
# Wings = 75% of 1-sigma daily move, min 40pt, rounded to nearest 5
WING_WIDTH_BASE = 40
WING_SIGMA_PCT = 0.75

def adaptive_wing_width(spx_price, vix_val):
    """Compute wing width from SPX and VIX, matching the backtest formula."""
    daily_sigma = spx_price * (vix_val / 100) / (252 ** 0.5)
    raw = daily_sigma * WING_SIGMA_PCT
    rounded = max(WING_WIDTH_BASE, round(raw / 5) * 5)
    return int(rounded)

if API_KEY == "YOUR_KEY_HERE":
    print("ERROR: Set your Polygon API key via POLYGON_API_KEY env var or cockpit_config.json")
    sys.exit(1)

# ─── Strategy definitions ───
STRATEGIES = [
    {"ver":"v3","regime":"PHOENIX","entry":"10:00","mech":"50%/close/1T","filter":None,
     "desc":"PHOENIX v3 — Concentrated Signal Model","color":"#f59e0b",
     "vix":None,"pd":None,"rng":None,"gap":None},
    {"ver":"v6","regime":"LOW_DN_IN_GFL","entry":"10:00","mech":"50%/1530/1T","filter":"VP<=1.7",
     "desc":"VIX<15 | Prior Day DOWN | In Range | Flat Gap","color":"#06b6d4",
     "vix":[0,15],"pd":"DN","rng":"IN","gap":"GFL"},
    {"ver":"v7","regime":"LOW_FL_IN_GUP","entry":"10:00","mech":"40%/close/1T","filter":None,
     "desc":"VIX<15 | Prior Day FLAT | In Range | Gap Up","color":"#a855f7",
     "vix":[0,15],"pd":"FL","rng":"IN","gap":"GUP"},
    {"ver":"v8","regime":"ELEV_UP_IN_GDN","entry":"10:30","mech":"40%/1530/1T","filter":None,
     "desc":"VIX 20-25 | Prior Day UP | In Range | Gap Down","color":"#ef4444",
     "vix":[20,25],"pd":"UP","rng":"IN","gap":"GDN"},
    {"ver":"v9","regime":"MID_UP_OT_GFL","entry":"10:00","mech":"70%/1545/1T","filter":"!RISING",
     "desc":"VIX 15-20 | Prior Day UP | Outside Range | Flat Gap","color":"#eab308",
     "vix":[15,20],"pd":"UP","rng":"OT","gap":"GFL"},
    {"ver":"v10","regime":"MID_DN_OT_GFL","entry":"11:00","mech":"70%/1545/1T","filter":None,
     "desc":"VIX 15-20 | Prior Day DOWN | Outside Range | Flat Gap","color":"#ec4899",
     "vix":[15,20],"pd":"DN","rng":"OT","gap":"GFL"},
    {"ver":"v12","regime":"LOW_UP_OT_GUP","entry":"10:00","mech":"40%/close/1T","filter":"5dRet>1",
     "desc":"VIX<15 | Prior Day UP | Outside Range | Gap Up","color":"#f97316",
     "vix":[0,15],"pd":"UP","rng":"OT","gap":"GUP"},
    {"ver":"v14","regime":"MID_DN_IN_GDN","entry":"10:00","mech":"50%/close/1T","filter":"ScoreVol<18",
     "desc":"VIX 15-20 | Prior Day DOWN | In Range | Gap Down","color":"#64748b",
     "vix":[15,20],"pd":"DN","rng":"IN","gap":"GDN"},
]

TRANCHE_CONFIGS = {
    "1T":    {"n":1, "adds":[]},
    "3T60m": {"n":3, "adds":["11:00","12:00"]},
    "5T30m": {"n":5, "adds":["10:30","11:00","11:30","12:00"]},
    "5T60m": {"n":5, "adds":["11:00","12:00","13:00","14:00"]},
}

# ─── VP-scaled regime budget (matches backtest regime_budget()) ───
REGIME_MAX_BUDGET = {
    "v6":  75000, "v7":  100000, "v8":  50000, "v9":  75000,
    "v10": 100000, "v12": 100000, "v14": 100000,
}

def regime_budget_cockpit(ver, vp):
    """Scale risk budget by VP ratio within per-strategy max cap.
    Mirrors compute_stats.py regime_budget() exactly."""
    try:
        vp = float(vp)
    except (TypeError, ValueError):
        vp = 1.5
    max_bud = REGIME_MAX_BUDGET.get(ver, 100000)
    if   vp <= 1.0: scale = 1.00
    elif vp <= 1.2: scale = 0.75
    elif vp <= 1.5: scale = 0.50
    else:           scale = 0.25
    return int(max_bud * scale)

# ─── Active Trade persistence ───
def load_active_trade():
    if os.path.exists(TRADE_PATH):
        try:
            with open(TRADE_PATH) as f:
                return json.load(f)
        except:
            return None
    return None

def save_active_trade(trade):
    with open(TRADE_PATH, "w") as f:
        json.dump(trade, f, indent=2, default=str)

def clear_active_trade():
    if os.path.exists(TRADE_PATH):
        os.remove(TRADE_PATH)

def append_live_trade(trade):
    """Convert a closed active trade into strategy_trades.json format and append to live_trades.json."""
    try:
        # Weighted avg credit + total qty across all filled tranches
        filled = [t for t in trade.get("tranches", [])
                  if t.get("status") == "filled" and t.get("credit") is not None]
        if filled:
            qtys       = [t.get("qty", trade.get("fill_qty", 0)) for t in filled]
            total_qty  = sum(qtys)
            avg_credit = (sum(t["credit"] * q for t, q in zip(filled, qtys)) / total_qty
                          if total_qty > 0 else trade.get("entry_credit", 0))
        else:
            total_qty  = trade.get("fill_qty", 0)
            avg_credit = trade.get("entry_credit", 0)

        exit_sv   = float(trade.get("exit_sv") or avg_credit)
        pnl_ps    = round(avg_credit - exit_sv, 2)
        pnl       = round(pnl_ps * 100 * total_qty)

        entry_iso = trade.get("entry_time", "")
        exit_iso  = trade.get("exit_time",  "")
        exit_map  = {
            "target": "TARGET", "stop": "STOP", "time": "TIME",
            "manual": "MANUAL", "wing_stop": "WING_STOP", "EOD_AUTO": "CLOSE",
        }
        exit_label = exit_map.get(
            trade.get("exit_type", "manual"),
            str(trade.get("exit_type", "MANUAL")).upper()
        )

        mkt = state.get("market", {})
        record = {
            "date":         entry_iso[:10] if entry_iso else datetime.now().strftime("%Y-%m-%d"),
            "ver":          trade.get("ver", "?"),
            "pnl":          int(pnl),
            "qty":          total_qty,
            "exit":         exit_label,
            "risk_budget":  trade.get("risk_budget", 0),
            "pnl_ps":       pnl_ps,
            "is_win":       pnl >= 0,
            "entry_time":   (entry_iso[11:16] + " ET") if len(entry_iso) >= 16 else "?",
            "exit_time":    (exit_iso[11:16]  + " ET") if len(exit_iso)  >= 16 else "?",
            "wing_width":   trade.get("wing_width", 0),
            "entry_credit": round(float(avg_credit), 2),
            "exit_sv":      exit_sv,
            "vix":          round(float(trade.get("entry_vix") or mkt.get("vix", 0)), 1),
            "vp_ratio":     round(float(trade.get("entry_vp")  or mkt.get("vp_ratio", 0)), 2),
            "atm":          trade.get("atm"),
            "mech":         trade.get("mech", ""),
            "live":         True,   # distinguishes live trades from backtest in calendar
        }
        if trade.get("fire_count") is not None:
            record["fire_count"] = trade["fire_count"]

        try:
            with open(LIVE_TRADES_PATH) as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            existing = []

        # Replace any existing record for same date + ver (avoid duplicates on re-log)
        key = (record["date"], record["ver"])
        existing = [e for e in existing if (e.get("date"), e.get("ver")) != key]
        existing.append(record)
        existing.sort(key=lambda x: x.get("date", ""))

        with open(LIVE_TRADES_PATH, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"  LIVE TRADE LOGGED: {record['date']} {record['ver']} {exit_label} P&L=${int(pnl):+,}")
        # Push to GitHub for persistence across Railway redeploys (non-blocking)
        if os.environ.get("GITHUB_TOKEN"):
            import threading as _t
            _t.Thread(target=_push_live_trades_bg, daemon=True).start()
    except Exception as e:
        print(f"  WARNING: Failed to log live trade: {e}")

def _push_live_trades_bg():
    """Background: push live_trades.json to GitHub repo via REST API."""
    try:
        import urllib.request as _ur, base64 as _b64
        token = os.environ.get("GITHUB_TOKEN", "")
        repo  = os.environ.get("GITHUB_REPO", "")   # e.g. "ztariff/0dte-ibf-system"
        branch = os.environ.get("GITHUB_BRANCH", "main")
        if not token or not repo:
            return
        filename = "live_trades.json"
        api_url  = f"https://api.github.com/repos/{repo}/contents/{filename}"
        hdrs = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "cockpit-feed/1.0",
        }
        # Read current file to get SHA (required for updates)
        sha = ""
        try:
            req = _ur.Request(api_url, headers=hdrs)
            with _ur.urlopen(req, timeout=10) as resp:
                sha = json.loads(resp.read()).get("sha", "")
        except Exception:
            pass   # file may not exist yet
        # Read local file
        with open(LIVE_TRADES_PATH, "rb") as f:
            content_b64 = _b64.b64encode(f.read()).decode()
        payload = {
            "message": f"cockpit: live trade update {datetime.now().strftime('%H:%M:%S')}",
            "content": content_b64,
            "branch":  branch,
        }
        if sha:
            payload["sha"] = sha
        req = _ur.Request(api_url,
                          data=json.dumps(payload).encode(),
                          headers=hdrs, method="PUT")
        with _ur.urlopen(req, timeout=15) as resp:
            resp.read()
        print(f"  GITHUB: live_trades.json pushed to {repo}")
    except Exception as e:
        print(f"  GITHUB PUSH FAILED: {e}")

def _restore_live_trades_from_github():
    """On startup: fetch live_trades.json from GitHub if local copy is missing."""
    if os.path.exists(LIVE_TRADES_PATH):
        return   # already have local copy
    try:
        import urllib.request as _ur, base64 as _b64
        token = os.environ.get("GITHUB_TOKEN", "")
        repo  = os.environ.get("GITHUB_REPO", "")
        if not token or not repo:
            return
        api_url = f"https://api.github.com/repos/{repo}/contents/live_trades.json"
        hdrs = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "cockpit-feed/1.0",
        }
        req = _ur.Request(api_url, headers=hdrs)
        with _ur.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        content = _b64.b64decode(data["content"].replace("\n", ""))
        with open(LIVE_TRADES_PATH, "wb") as f:
            f.write(content)
        print(f"  GITHUB: live_trades.json restored from {repo}")
    except Exception as e:
        print(f"  GITHUB: could not restore live_trades.json: {e}")

def parse_mech(mech):
    """Parse mech string like '50%/close/5T60m' into components."""
    parts = mech.split("/")
    profit_pct = int(parts[0].replace("%", ""))
    ts = parts[1]
    if ts == "close": stop_time = "16:00"
    elif ts == "1545": stop_time = "15:45"
    elif ts == "1530": stop_time = "15:30"
    else: stop_time = ts[:2] + ":" + ts[2:]
    tranche_key = parts[2]
    tc = TRANCHE_CONFIGS.get(tranche_key, {"n": 1, "adds": []})
    return {
        "profit_pct": profit_pct,
        "stop_time": stop_time,
        "tranche_key": tranche_key,
        "n_tranches": tc["n"],
        "add_times": tc["adds"],
    }

# ─── State (persists across polls) ───
state = {
    "last_update": None,
    "market": {},
    "regime": {},
    "signals": [],
    "option_chain": {},
    "errors": [],
    "log": [],
}

# ─── Signal latching ───
# Once a v4-v14 strategy matches at its entry time, latch it as active
# Entry-time regime snapshot: captured once at each strategy's entry time,
# then used for the rest of the day. No intraday re-evaluation.
_entry_regime = {}      # ver -> {"regime": str, "vix": float, "filter_data": dict, "matched": bool, "reason": str}
_entry_regime_date = None  # date string to reset daily

# ─── PHOENIX signal lock ───
# PHOENIX signals are evaluated ONCE at 10:00 AM (matching backtest methodology)
# and locked for the rest of the day. Before 10:00 AM, show live preview.
# This matches ensemble_v3.py which uses frozen 10:00 AM CSV values for all filters.
_phoenix_lock = None        # dict: {"ctx": {}, "result": {}, "lock_time": str}
_phoenix_lock_date = None   # date string to reset daily

# ─── Daily data cache ───
# Prior day, 5d return, rv_1d_change, and weekly range come from COMPLETED daily bars
# and cannot change during the trading day. Cache them after first successful fetch
# to avoid signal flickering caused by Polygon API timeouts.
_daily_cache = {}       # key -> value (e.g., "prior_day" -> {...}, "ret5d" -> float)
_daily_cache_date = None  # date string to reset daily

# ─── Polygon API helpers ───
def poly_get(path, params=None):
    if params is None: params = {}
    params["apiKey"] = API_KEY
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        state["errors"].append(f"{datetime.now().isoformat()}: {str(e)}")
        return None

def get_spx_price():
    """Get current SPX price via snapshot."""
    data = poly_get("/v2/snapshot/locale/us/markets/stocks/tickers/SPY")
    if data and "ticker" in data:
        # SPY as proxy, multiply by ~10 for SPX approximation
        # Better: use I:SPX index
        pass
    # Try the indices endpoint
    data = poly_get("/v3/snapshot", params={"ticker.any_of": "I:SPX"})
    if data and data.get("results"):
        for r in data["results"]:
            if r.get("ticker") == "I:SPX":
                session = r.get("session", {})
                return session.get("price") or session.get("close") or session.get("previous_close")
    # Fallback: try aggs
    today = datetime.now().strftime("%Y-%m-%d")
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/minute/{today}/{today}", params={"limit": 1, "sort": "desc"})
    if data and data.get("results"):
        return data["results"][0].get("c")
    return None

def get_vix_price():
    """Get current VIX level."""
    data = poly_get("/v3/snapshot", params={"ticker.any_of": "I:VIX"})
    if data and data.get("results"):
        for r in data["results"]:
            if r.get("ticker") == "I:VIX":
                session = r.get("session", {})
                return session.get("price") or session.get("close") or session.get("previous_close")
    today = datetime.now().strftime("%Y-%m-%d")
    data = poly_get(f"/v2/aggs/ticker/I:VIX/range/1/minute/{today}/{today}", params={"limit": 1, "sort": "desc"})
    if data and data.get("results"):
        return data["results"][0].get("c")
    return None

def get_prior_day_data():
    """Get prior trading day SPX close, return, direction.
    IMPORTANT: Only uses COMPLETED daily bars (excludes today's developing bar)
    to match the backtest which uses prior_day_direction from yesterday's close."""
    now = datetime.now()
    # Exclude today — only fetch completed bars up through yesterday
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=10)).strftime("%Y-%m-%d")
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/day/{week_ago}/{yesterday}",
                    params={"limit": 10, "sort": "desc", "adjusted": "true"})
    if data and data.get("results") and len(data["results"]) >= 1:
        prior_bar = data["results"][0]  # Most recent COMPLETED trading day
        prior_ret = (prior_bar["c"] - prior_bar["o"]) / prior_bar["o"] * 100
        if prior_ret > 0.15: direction = "UP"
        elif prior_ret < -0.15: direction = "DOWN"
        else: direction = "FLAT"
        return {
            "prior_close": prior_bar["c"],
            "prior_return": round(prior_ret, 4),
            "prior_direction": direction,
            "prior_high": prior_bar["h"],
            "prior_low": prior_bar["l"],
        }
    return None

def get_weekly_range():
    """Get PRIOR week's high/low for SPX — matches backtest 'in_prior_week_range' column.
    The backtest compares SPX to last week's completed range (Mon-Fri),
    NOT the current week's developing range."""
    now = datetime.now()
    monday = now - timedelta(days=now.weekday())

    # Prior week = last Monday through last Friday
    prev_monday = monday - timedelta(days=7)
    prev_friday = monday - timedelta(days=1)
    start = prev_monday.strftime("%Y-%m-%d")
    end = prev_friday.strftime("%Y-%m-%d")
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/day/{start}/{end}", params={"limit": 10, "adjusted": "true"})
    if data and data.get("results"):
        highs = [b["h"] for b in data["results"]]
        lows = [b["l"] for b in data["results"]]
        return {"week_high": max(highs), "week_low": min(lows)}

    # Fallback: try the week before that
    prev2_monday = prev_monday - timedelta(days=7)
    prev2_friday = prev_monday - timedelta(days=1)
    start2 = prev2_monday.strftime("%Y-%m-%d")
    end2 = prev2_friday.strftime("%Y-%m-%d")
    data2 = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/day/{start2}/{end2}", params={"limit": 10, "adjusted": "true"})
    if data2 and data2.get("results"):
        highs = [b["h"] for b in data2["results"]]
        lows = [b["l"] for b in data2["results"]]
        return {"week_high": max(highs), "week_low": min(lows)}
    return None

def get_5d_return():
    """Get 5-day SPX return (yesterday's close vs 6 trading days ago close).
    IMPORTANT: Excludes today's developing bar — uses only completed daily bars
    to match the backtest which uses prior_5d_return from yesterday's close."""
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (now - timedelta(days=12)).strftime("%Y-%m-%d")
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/day/{start}/{yesterday}",
                    params={"limit": 10, "sort": "asc", "adjusted": "true"})
    if data and data.get("results") and len(data["results"]) >= 6:
        bars = data["results"]
        close_yesterday = bars[-1]["c"]
        close_6d_ago = bars[-6]["c"]
        return round((close_yesterday - close_6d_ago) / close_6d_ago * 100, 4)
    elif data and data.get("results") and len(data["results"]) >= 2:
        bars = data["results"]
        return round((bars[-1]["c"] - bars[0]["c"]) / bars[0]["c"] * 100, 4)
    return 0

def _fetch_minute_bars_for_date(date_str):
    """Fetch all SPX 1-minute bars for a specific completed trading day.
    Used for computing proper intraday realized vol (matching backtest methodology)."""
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/minute/{date_str}/{date_str}",
                    params={"limit": 1000, "sort": "asc", "adjusted": "true"})
    if data and data.get("results"):
        return data["results"]
    return []

def get_rv_1d_change():
    """Get 1-day change in realized vol — matches backtest (enrich_research.py).

    BACKTEST FORMULA (ground truth):
        1. Compute annualized RV from 1-min bars: std(log_returns) * sqrt(252*390) * 100
        2. rv_1d_change = (rv_yesterday - rv_2days_ago) / rv_2days_ago * 100

    PREVIOUS BUG: Used daily-bar (H-L)/O range % with absolute subtraction.
    That's a fundamentally different metric — wrong scale and missing % change division.

    IMPORTANT: Excludes today's developing bar — uses only completed trading days."""
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (now - timedelta(days=10)).strftime("%Y-%m-%d")

    # Step 1: Get recent daily bars to find the last 2 TRADING days (handles weekends/holidays)
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/day/{start}/{yesterday}",
                    params={"limit": 10, "sort": "asc", "adjusted": "true"})
    if not data or not data.get("results") or len(data["results"]) < 2:
        return 0

    daily_bars = data["results"]
    # Extract actual trading dates from timestamps (last 2 completed days)
    yday_date = datetime.utcfromtimestamp(daily_bars[-1]["t"] / 1000).strftime("%Y-%m-%d")
    day2_date = datetime.utcfromtimestamp(daily_bars[-2]["t"] / 1000).strftime("%Y-%m-%d")

    # Step 2: Fetch 1-minute bars for both trading days
    yday_bars = _fetch_minute_bars_for_date(yday_date)
    day2_bars = _fetch_minute_bars_for_date(day2_date)

    # Step 3: Compute RV using log-return std dev (reuses calc_rv_from_bars, same formula as backtest)
    rv_yday = calc_rv_from_bars(yday_bars) if len(yday_bars) >= 10 else None
    rv_day2 = calc_rv_from_bars(day2_bars) if len(day2_bars) >= 10 else None

    if rv_yday is not None and rv_day2 is not None and rv_day2 > 0:
        result = round((rv_yday - rv_day2) / rv_day2 * 100, 2)
        print(f"    [rv_1d_change] {yday_date}: RV={rv_yday:.2f}  {day2_date}: RV={rv_day2:.2f}  chg={result:+.2f}%")
        return result

    print(f"    [rv_1d_change] Could not compute — yday_bars={len(yday_bars)} day2_bars={len(day2_bars)}")
    return 0

def get_0dte_options(spx_price, vix_val=None, ww_override=None):
    """Get 0DTE SPX option chain for IBF structure.
    Wing width priority: ww_override > adaptive(SPX,VIX) > WING_WIDTH_BASE.
    Use ww_override for active trade repricing (locked wing width)."""
    today = datetime.now().strftime("%Y-%m-%d")
    atm = round(spx_price / 5) * 5
    if ww_override is not None:
        ww = ww_override
    elif vix_val is not None:
        ww = adaptive_wing_width(spx_price, vix_val)
    else:
        ww = WING_WIDTH_BASE

    strikes_needed = [atm - ww, atm, atm + ww]
    chain = {"atm": atm, "wing_width": ww, "puts": {}, "calls": {}, "credit": 0, "spread_val": 0}

    # Try the chain snapshot endpoint first (more reliable than building tickers)
    for cp in ["call", "put"]:
        data = poly_get(f"/v3/snapshot/options/I:SPX", params={
            "expiration_date": today,
            "contract_type": cp,
            "strike_price.gte": atm - ww,
            "strike_price.lte": atm + ww,
            "limit": 50,
        })
        if data and data.get("results"):
            for r in data["results"]:
                details = r.get("details", {})
                strike = details.get("strike_price")
                if strike is None:
                    continue
                strike = float(strike)
                if strike not in strikes_needed:
                    continue
                greeks = r.get("greeks", {})
                day = r.get("day", {})
                last_quote = r.get("last_quote", {})

                mid = 0
                if last_quote.get("bid") and last_quote.get("ask"):
                    mid = (last_quote["bid"] + last_quote["ask"]) / 2
                elif day.get("close"):
                    mid = day["close"]

                chain[f"{cp}s"][str(int(strike))] = {
                    "strike": strike,
                    "mid": round(mid, 2),
                    "bid": last_quote.get("bid", 0),
                    "ask": last_quote.get("ask", 0),
                    "iv": greeks.get("implied_volatility", 0),
                    "delta": greeks.get("delta", 0),
                    "ticker": details.get("ticker", ""),
                }

    # Fallback: try individual ticker construction if chain endpoint returned nothing
    if not chain["puts"] and not chain["calls"]:
        ymd = datetime.now().strftime("%y%m%d")
        for strike in strikes_needed:
            for cp in ["call", "put"]:
                cp_char = "C" if cp == "call" else "P"
                strike_str = f"{int(strike * 1000):08d}"
                # Try both SPX and SPXW tickers (SPXW = weekly/0DTE)
                for root in ["SPX", "SPXW"]:
                    ticker = f"O:{root}{ymd}{cp_char}{strike_str}"
                    data = poly_get(f"/v3/snapshot/options/I:SPX/{ticker}")
                    if data and data.get("results"):
                        r = data["results"]
                        greeks = r.get("greeks", {})
                        day = r.get("day", {})
                        last_quote = r.get("last_quote", {})

                        mid = 0
                        if last_quote.get("bid") and last_quote.get("ask"):
                            mid = (last_quote["bid"] + last_quote["ask"]) / 2
                        elif day.get("close"):
                            mid = day["close"]

                        chain[f"{cp}s"][str(int(strike))] = {
                            "strike": strike,
                            "mid": round(mid, 2),
                            "bid": last_quote.get("bid", 0),
                            "ask": last_quote.get("ask", 0),
                            "iv": greeks.get("implied_volatility", 0),
                            "delta": greeks.get("delta", 0),
                            "ticker": ticker,
                        }
                        break  # found it, don't try the other root

    # Calculate IBF credit and spread value
    atm_put = chain["puts"].get(str(atm), {})
    atm_call = chain["calls"].get(str(atm), {})
    wing_put = chain["puts"].get(str(atm - ww), {})
    wing_call = chain["calls"].get(str(atm + ww), {})

    credit = (atm_put.get("mid", 0) + atm_call.get("mid", 0)
              - wing_put.get("mid", 0) - wing_call.get("mid", 0))
    chain["credit"] = round(credit, 2)
    chain["spread_val"] = round(credit, 2)  # at entry, spread_val == credit

    chain["atm_put_price"] = atm_put.get("mid", 0)
    chain["atm_call_price"] = atm_call.get("mid", 0)
    chain["wing_put_price"] = wing_put.get("mid", 0)
    chain["wing_call_price"] = wing_call.get("mid", 0)

    return chain

def compute_current_spread_val(chain):
    """Recompute spread value from current option prices."""
    atm = chain["atm"]
    ww = chain["wing_width"]
    atm_put = chain["puts"].get(str(atm), {})
    atm_call = chain["calls"].get(str(atm), {})
    wing_put = chain["puts"].get(str(atm - ww), {})
    wing_call = chain["calls"].get(str(atm + ww), {})
    sv = (atm_put.get("mid", 0) + atm_call.get("mid", 0)
          - wing_put.get("mid", 0) - wing_call.get("mid", 0))
    return round(sv, 2)

# ─── Regime classification ───
def classify_regime(vix, prior_dir, spx, week_hi, week_lo, prior_close):
    # VIX
    if vix < 15: vr = "LOW"
    elif vix < 20: vr = "MID"
    elif vix < 25: vr = "ELEV"
    else: vr = "HIGH"

    # Prior day
    if prior_dir == "UP": pd = "UP"
    elif prior_dir == "DOWN": pd = "DN"
    else: pd = "FL"

    # In range
    in_range = week_lo <= spx <= week_hi if (week_hi and week_lo) else True
    rng = "IN" if in_range else "OT"

    # Gap
    gap_pct = ((spx - prior_close) / prior_close * 100) if prior_close else 0
    if gap_pct < -0.25: gap = "GDN"
    elif gap_pct > 0.25: gap = "GUP"
    else: gap = "GFL"

    return f"{vr}_{pd}_{rng}_{gap}", gap_pct, in_range

def check_filter(filt, filter_data):
    """Check if a signal filter passes."""
    if filt is None: return True
    vp = filter_data.get("vp", 1.5)
    rv_slope_label = filter_data.get("rv_slope_label", "UNKNOWN")
    ret5d = filter_data.get("ret5d", 0)
    range_pct = filter_data.get("range_pct", 0.5)
    score_vol = filter_data.get("score_vol", 15)

    if filt == "5dRet>0": return ret5d > 0
    if filt == "5dRet>1": return ret5d > 1.0
    if filt == "VP<=1.7": return vp <= 1.7
    if filt == "VP<=2.0": return vp <= 2.0
    if filt == "!RISING": return rv_slope_label != "RISING"
    if filt == "ScoreVol<18": return score_vol < 18
    if filt == "Rng<=0.3": return range_pct <= 0.3
    return True

# ─── Intraday 1-minute bar cache (refreshed each poll) ───
_minute_bars = []

def get_spx_minute_bars():
    """Fetch today's SPX 1-minute bars for RV and range calculations."""
    today = datetime.now().strftime("%Y-%m-%d")
    data = poly_get(f"/v2/aggs/ticker/I:SPX/range/1/minute/{today}/{today}",
                    params={"limit": 500, "sort": "asc", "adjusted": "true"})
    if data and data.get("results"):
        return data["results"]  # list of {o, h, l, c, v, t, ...}
    return []

def calc_rv_from_bars(bars):
    """Compute annualized realized vol from 1-min close prices (log returns)."""
    if len(bars) < 5:
        return None
    closes = np.array([b["c"] for b in bars], dtype=float)
    lr = np.diff(np.log(closes))
    if len(lr) < 2:
        return None
    return float(np.std(lr) * np.sqrt(252 * 390) * 100)

def compute_rv_slope(bars):
    """
    Compare RV of two windows to detect rising/falling vol.
    Window logic matches backtest_research.py:
    - If <35 mins since open: split at 15 mins
    - Else: W1 = 30-60 min ago, W2 = last 30 min
    Returns: (rv_now, rv_prev, label, slope_pct)
    """
    if not bars or len(bars) < 10:
        return None, None, "UNKNOWN", 0

    # Get market open time from first bar
    first_t = bars[0]["t"] / 1000  # ms -> s
    first_dt = datetime.fromtimestamp(first_t)
    market_open = first_dt.replace(hour=9, minute=30, second=0, microsecond=0)
    now = datetime.now()
    mins_since_open = (now - market_open).total_seconds() / 60

    if mins_since_open <= 35:
        mid_t = (market_open + timedelta(minutes=15)).timestamp() * 1000
        w1 = [b for b in bars if b["t"] < mid_t]
        w2 = [b for b in bars if b["t"] >= mid_t]
    else:
        t_30_ago = (now - timedelta(minutes=30)).timestamp() * 1000
        t_60_ago = (now - timedelta(minutes=60)).timestamp() * 1000
        w1 = [b for b in bars if t_60_ago <= b["t"] < t_30_ago]
        w2 = [b for b in bars if b["t"] >= t_30_ago]

    rv_prev = calc_rv_from_bars(w1)
    rv_now = calc_rv_from_bars(w2)

    if rv_prev and rv_prev > 0 and rv_now is not None:
        slope = (rv_now - rv_prev) / rv_prev * 100
        if slope > 20:
            label = "RISING"
        elif slope < -20:
            label = "FALLING"
        else:
            label = "STABLE"
        return rv_now, rv_prev, label, round(slope, 1)

    return rv_now, rv_prev, "UNKNOWN", 0

def compute_morning_range_pct(bars):
    """
    Compute morning range (9:30-10:00) as % of opening price.
    Used for Rng<=0.3 filter (v13).
    """
    if not bars:
        return 0.5  # fallback

    first_t = bars[0]["t"] / 1000
    first_dt = datetime.fromtimestamp(first_t)
    cutoff = first_dt.replace(hour=10, minute=0, second=0, microsecond=0)
    cutoff_ms = cutoff.timestamp() * 1000

    morning = [b for b in bars if b["t"] <= cutoff_ms]
    if not morning:
        return 0.5

    hi = max(b["h"] for b in morning)
    lo = min(b["l"] for b in morning)
    op = morning[0]["o"]
    if op <= 0:
        return 0.5

    return round((hi - lo) / op * 100, 4)

def compute_vp_ratio(vix_val, rv):
    """Actual VP ratio: VIX / realized vol. Falls back to estimate if RV unavailable."""
    if rv and rv > 0:
        return round(vix_val / rv, 2)
    # Fallback: rough estimate
    rv_est = vix_val * 0.75
    return round(vix_val / rv_est, 2) if rv_est > 0 else 1.5

def compute_score_vol(vp, rv_slope_label):
    """
    Compute score_vol (vol premium score) matching backtest_research.py logic:
    Base score from VP ratio, adjusted by RV slope direction.
    Range: 0-30 (clamped).
    """
    if vp is None:
        return 15  # neutral default

    # Base score from VP ratio
    if vp >= 1.5:
        score = 30
    elif vp >= 1.2:
        score = 22
    elif vp >= 1.0:
        score = 15
    elif vp >= 0.85:
        score = 10
    else:
        score = 3

    # Adjust for RV slope
    if rv_slope_label == "RISING":
        score = max(0, score - 15)
    elif rv_slope_label == "FALLING":
        score = min(30, score + 5)

    return score

# ─── PHOENIX v3 Signal Confluence ───
PHOENIX_SIGNALS = [
    {
        "rank": 1,
        "name": "VIX≤20 + VP≤1.0 + 5dRet>0",
        "filters": [
            {"id": "VIX≤20",  "test": lambda ctx: ctx["vix"] <= 20},
            {"id": "VP≤1.0",  "test": lambda ctx: ctx["vp"] <= 1.0},
            {"id": "5dRet>0", "test": lambda ctx: ctx["ret5d"] > 0},
        ],
        "mech": {"target": 70, "time_stop": "close", "tranches": "1T", "interval": "60m"},
    },
    {
        "rank": 2,
        "name": "VP≤1.3 + PrDayDn + 5dRet>0",
        "filters": [
            {"id": "VP≤1.3",   "test": lambda ctx: ctx["vp"] <= 1.3},
            {"id": "PrDayDn",  "test": lambda ctx: ctx["prior_direction"] == "DOWN"},
            {"id": "5dRet>0",  "test": lambda ctx: ctx["ret5d"] > 0},
        ],
        "mech": {"target": 50, "time_stop": "close", "tranches": "1T", "interval": "60m"},
    },
    {
        "rank": 3,
        "name": "VP≤1.2 + 5dRet>0 + RVchg>0",
        "filters": [
            {"id": "VP≤1.2",  "test": lambda ctx: ctx["vp"] <= 1.2},
            {"id": "5dRet>0",  "test": lambda ctx: ctx["ret5d"] > 0},
            {"id": "RVchg>0", "test": lambda ctx: ctx["rv_1d_change"] > 0},
        ],
        "mech": {"target": 50, "time_stop": "close", "tranches": "1T", "interval": "60m"},
    },
    {
        "rank": 4,
        "name": "VP≤1.5 + OutWkRng + 5dRet>0",
        "filters": [
            {"id": "VP≤1.5",    "test": lambda ctx: ctx["vp"] <= 1.5},
            {"id": "OutWkRng",  "test": lambda ctx: not ctx["in_range"]},
            {"id": "5dRet>0",   "test": lambda ctx: ctx["ret5d"] > 0},
        ],
        "mech": {"target": 70, "time_stop": "close", "tranches": "1T", "interval": "60m"},
    },
    {
        "rank": 5,
        "name": "VP≤1.3 + !RISING + 5dRet>0",
        "filters": [
            {"id": "VP≤1.3",    "test": lambda ctx: ctx["vp"] <= 1.3},
            {"id": "!RISING",   "test": lambda ctx: ctx["rv_slope_label"] != "RISING"},
            {"id": "5dRet>0",   "test": lambda ctx: ctx["ret5d"] > 0},
        ],
        "mech": {"target": 50, "time_stop": "close", "tranches": "1T", "interval": "60m"},
    },
]

def evaluate_phoenix(ctx):
    """Evaluate all 5 PHOENIX signal groups. Returns dict with firing status and sizing."""
    results = []
    fire_count = 0
    highest_mech = None

    for sig in PHOENIX_SIGNALS:
        filter_results = []
        for f in sig["filters"]:
            try:
                passed = f["test"](ctx)
            except:
                passed = False
            filter_results.append({"id": f["id"], "pass": passed})
        all_pass = all(fr["pass"] for fr in filter_results)
        if all_pass:
            fire_count += 1
            if highest_mech is None:
                highest_mech = sig["mech"]
        results.append({
            "rank": sig["rank"],
            "name": sig["name"],
            "firing": all_pass,
            "filters": filter_results,
            "mech": sig["mech"],
        })

    # Tiered sizing
    if fire_count == 0:   sizing = {"dollars": 0, "pct": 0}
    elif fire_count == 1: sizing = {"dollars": 25000, "pct": 25}
    elif fire_count == 2: sizing = {"dollars": 50000, "pct": 50}
    elif fire_count == 3: sizing = {"dollars": 75000, "pct": 75}
    else:                 sizing = {"dollars": 100000, "pct": 100}

    return {
        "signals": results,
        "fire_count": fire_count,
        "sizing": sizing,
        "adaptive_mech": highest_mech,
    }

# ─── Main poll loop ───
def poll():
    now = datetime.now()
    ts = now.strftime("%H:%M:%S")
    print(f"\n[{ts}] Polling Polygon...", flush=True)

    # 1. Get SPX
    spx = get_spx_price()
    if not spx:
        state["errors"].append(f"{ts}: Failed to get SPX price")
        print(f"  ERROR: No SPX data")
        write_state()
        return
    print(f"  SPX: {spx:.2f}")

    # 2. Get VIX
    vix = get_vix_price()
    if not vix:
        state["errors"].append(f"{ts}: Failed to get VIX")
        vix = 18.0  # fallback
    print(f"  VIX: {vix:.2f}")

    # ─── Daily data (cached — these come from completed bars, can't change intraday) ───
    global _daily_cache, _daily_cache_date
    today_str = now.strftime("%Y-%m-%d")
    if _daily_cache_date != today_str:
        _daily_cache.clear()
        _daily_cache_date = today_str

    # 3. Prior day data (cached after first successful fetch)
    if "prior" not in _daily_cache:
        prior = get_prior_day_data()
        if prior:
            _daily_cache["prior"] = prior
            print(f"  Prior: {prior['prior_direction']} ({prior['prior_return']:.2f}%), close={prior['prior_close']:.2f} [CACHED]")
        else:
            state["errors"].append(f"{ts}: Failed to get prior day data (will retry next poll)")
            prior = {"prior_close": spx, "prior_return": 0, "prior_direction": "FLAT", "prior_high": spx, "prior_low": spx}
            print(f"  Prior: FAILED — using fallback (will retry)")
    else:
        prior = _daily_cache["prior"]
        print(f"  Prior: {prior['prior_direction']} ({prior['prior_return']:.2f}%), close={prior['prior_close']:.2f} [cached]")

    # 4. Weekly range (cached after first successful fetch)
    if "weekly" not in _daily_cache:
        weekly = get_weekly_range()
        if weekly:
            _daily_cache["weekly"] = weekly
            print(f"  Prior week range: {weekly['week_low']:.2f} - {weekly['week_high']:.2f} [CACHED]")
        else:
            weekly = {"week_high": spx + 50, "week_low": spx - 50}
            print(f"  Prior week range: FAILED — using fallback (will retry)")
    else:
        weekly = _daily_cache["weekly"]
        print(f"  Prior week range: {weekly['week_low']:.2f} - {weekly['week_high']:.2f} [cached]")

    # 5. 5d return (cached after first successful fetch)
    if "ret5d" not in _daily_cache:
        ret5d_result = get_5d_return()
        # Only cache if the API call actually succeeded (non-zero or confirmed zero)
        # We check by also verifying prior was cached (means API is working)
        if "prior" in _daily_cache:
            _daily_cache["ret5d"] = ret5d_result
            print(f"  5d return: {ret5d_result:.4f}% [CACHED]")
        else:
            print(f"  5d return: {ret5d_result:.4f}% [not cached — API may be down]")
        ret5d = ret5d_result
    else:
        ret5d = _daily_cache["ret5d"]
        print(f"  5d return: {ret5d:.4f}% [cached]")

    # 5b. RV 1-day change (cached after first successful fetch)
    # Now uses minute-bar RV with % change formula (matching backtest/enrich_research.py)
    if "rv_1d_chg" not in _daily_cache:
        rv_1d_result = get_rv_1d_change()
        if "prior" in _daily_cache:
            _daily_cache["rv_1d_chg"] = rv_1d_result
            print(f"  RV 1d change: {rv_1d_result:+.2f}% [CACHED — minute-bar RV]")
        else:
            print(f"  RV 1d change: {rv_1d_result:+.2f}% [not cached — API may be down]")
        rv_1d_chg = rv_1d_result
    else:
        rv_1d_chg = _daily_cache["rv_1d_chg"]
        print(f"  RV 1d change: {rv_1d_chg:+.2f}% [cached — minute-bar RV]")

    # 6. Compute regime
    regime_label, gap_pct, in_range = classify_regime(
        vix, prior["prior_direction"], spx,
        weekly["week_high"], weekly["week_low"], prior["prior_close"]
    )
    print(f"  Regime: {regime_label} | Gap: {gap_pct:.2f}%")

    # 7. Intraday 1-minute bars for RV, slope, and morning range
    global _minute_bars
    _minute_bars = get_spx_minute_bars()
    print(f"  Minute bars: {len(_minute_bars)} loaded")

    # 8. Realized vol + RV slope
    rv_now, rv_prev, rv_slope_label, rv_slope_pct = compute_rv_slope(_minute_bars)
    print(f"  RV: now={rv_now if rv_now is not None else 'N/A'}, prev={rv_prev if rv_prev is not None else 'N/A'}, slope={rv_slope_label} ({rv_slope_pct:+.1f}%)")

    # 9. VP ratio (actual VIX / RV)
    # Use full-session RV for VP, not windowed
    full_rv = calc_rv_from_bars(_minute_bars)
    vp = compute_vp_ratio(vix, full_rv)
    print(f"  VP ratio: {vp:.2f} (VIX={vix:.1f}, RV={full_rv if full_rv is not None else 'N/A'})")

    # 10. Morning range %
    range_pct = compute_morning_range_pct(_minute_bars)
    print(f"  Morning range: {range_pct:.3f}%")

    # 11. Score vol (vol premium score)
    score_vol = compute_score_vol(vp, rv_slope_label)
    print(f"  Score vol: {score_vol}")

    # 12. Option chain (adaptive wing width from SPX + VIX, matching backtest)
    chain = get_0dte_options(spx, vix)
    n_puts = len(chain.get("puts", {}))
    n_calls = len(chain.get("calls", {}))
    print(f"  Options: ATM={chain['atm']}, credit={chain['credit']:.2f}, WW={chain['wing_width']}, puts={n_puts}, calls={n_calls}")

    # 13. Compute spread value (recompute from live prices)
    spread_val = compute_current_spread_val(chain)
    chain["spread_val"] = spread_val

    # ─── Filter data (all computed from live market data) ───
    filter_data = {
        "vp": vp,
        "rv_slope": rv_slope_pct,
        "rv_slope_label": rv_slope_label,
        "ret5d": ret5d,
        "range_pct": range_pct,
        "score_vol": score_vol,
    }

    # ─── Check if market is open (9:30-16:00 ET) ───
    try:
        from zoneinfo import ZoneInfo
        et_now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        et_now = now  # fallback
    et_h, et_m = et_now.hour, et_now.minute
    market_open = (et_h > 9 or (et_h == 9 and et_m >= 30)) and et_h < 16

    # ─── Match strategies (with signal latching) ───
    signals = []
    et_hm_str = f"{et_h:02d}:{et_m:02d}"

    for strat in STRATEGIES:
        if strat["ver"] == "v3":
            # PHOENIX — only show as active during market hours
            signals.append({
                **strat,
                "matched": market_open,
                "match_reason": "PHOENIX (manual signal)" if market_open else "Pre-market — waiting for open",
                "regime_match": True,
                "filter_pass": True,
            })
            continue

        # Outside market hours, no regime strategies can fire
        if not market_open:
            signals.append({
                **strat,
                "matched": False,
                "match_reason": "Pre-market — waiting for open",
                "regime_match": False,
                "filter_pass": False,
                "half_size": False,
                "half_size_reason": None,
                "risk_budget": regime_budget_cockpit(strat["ver"], filter_data.get("vp", 1.5)),
            })
            continue

        ver = strat["ver"]
        entry_time_str = strat["entry"]  # e.g. "10:00" or "10:30"

        # ── Reset entry regime snapshot daily ──
        global _entry_regime_date
        today_str = now.strftime("%Y-%m-%d")
        if _entry_regime_date != today_str:
            _entry_regime.clear()
            _entry_regime_date = today_str

        # ── Entry-time regime check: only evaluate ONCE at the strategy's entry time ──
        et_str = now.strftime("%H:%M")
        if ver not in _entry_regime:
            # Haven't checked this strategy yet today
            if et_str < entry_time_str:
                # Before entry time — too early to evaluate
                signals.append({
                    **strat,
                    "matched": False,
                    "match_reason": f"Waiting for entry time ({entry_time_str} ET)",
                    "regime_match": False,
                    "filter_pass": False,
                    "half_size": False,
                    "half_size_reason": None,
                    "risk_budget": regime_budget_cockpit(ver, filter_data.get("vp", 1.5)),
                })
                continue
            else:
                # At or past entry time — evaluate regime NOW and lock it for the day
                vix_range = strat.get("vix")
                regime_parts = regime_label.split("_")
                no_match_reason = None
                if not vix_range:
                    no_match_reason = "No VIX range defined"
                elif vix < vix_range[0] or vix >= vix_range[1]:
                    no_match_reason = f"VIX {vix:.1f} outside [{vix_range[0]},{vix_range[1]}) at entry"
                elif strat["pd"] != regime_parts[1]:
                    no_match_reason = f"Prior day {regime_parts[1]} != {strat['pd']}"
                elif strat["rng"] != regime_parts[2]:
                    no_match_reason = f"Range {regime_parts[2]} != {strat['rng']}"
                elif strat["gap"] != regime_parts[3]:
                    no_match_reason = f"Gap {regime_parts[3]} != {strat['gap']}"

                if no_match_reason:
                    _entry_regime[ver] = {
                        "matched": False,
                        "reason": f"No match at {entry_time_str} — {no_match_reason}",
                        "regime": regime_label,
                        "vix": vix,
                        "half_size": False,
                        "half_size_reason": None,
                        "risk_budget": 0,
                    }
                    print(f"  {ver}: NO MATCH at entry — {no_match_reason}")
                else:
                    # Regime matched — check filter
                    filt_pass = check_filter(strat["filter"], filter_data)
                    # V9 VP cap: skip when VP > 2.0 (backtest shows all 5 such days are losers)
                    vp_cap_blocked = (ver == "v9" and filter_data.get("vp", 0) > 2.0)
                    # V10 half-size flag: when 5d return <= -1.5% use 50% budget
                    v10_half_size = (ver == "v10" and ret5d <= -1.5)
                    if filt_pass and not vp_cap_blocked:
                        risk_budget = regime_budget_cockpit(ver, filter_data.get("vp", 1.5))
                        if v10_half_size:
                            risk_budget = risk_budget // 2
                        _entry_regime[ver] = {
                            "matched": True,
                            "reason": f"Matched at {entry_time_str} — {regime_label} (VIX {vix:.1f})",
                            "regime": regime_label,
                            "vix": vix,
                            "half_size": v10_half_size,
                            "half_size_reason": f"5d return {ret5d:.2f}% ≤ -1.5% — HALF SIZE" if v10_half_size else None,
                            "risk_budget": risk_budget,
                        }
                        size_note = " [HALF SIZE]" if v10_half_size else ""
                        print(f"  SIGNAL: {ver} — matched at entry ({regime_label}, VIX {vix:.1f}){size_note} risk_budget=${risk_budget:,}")
                    elif vp_cap_blocked:
                        _entry_regime[ver] = {
                            "matched": False,
                            "reason": f"V9 SKIP: VP {filter_data.get('vp', 0):.2f} > 2.0 — extreme IV stress (backtest: all losers)",
                            "regime": regime_label,
                            "vix": vix,
                            "half_size": False,
                            "half_size_reason": None,
                            "risk_budget": 0,
                        }
                        print(f"  {ver}: VP CAP blocked — VP {filter_data.get('vp', 0):.2f} > 2.0")
                    else:
                        _entry_regime[ver] = {
                            "matched": False,
                            "reason": f"Regime matched at {entry_time_str} but filter FAIL ({strat['filter']})",
                            "regime": regime_label,
                            "vix": vix,
                            "half_size": False,
                            "half_size_reason": None,
                            "risk_budget": 0,
                        }
                        print(f"  {ver}: filter fail at entry ({strat['filter']})")

        # ── Use the locked entry-time result for the rest of the day ──
        entry_result = _entry_regime[ver]
        signals.append({
            **strat,
            "matched": entry_result["matched"],
            "match_reason": entry_result["reason"],
            "regime_match": entry_result["matched"],
            "filter_pass": entry_result["matched"],
            "half_size": entry_result.get("half_size", False),
            "half_size_reason": entry_result.get("half_size_reason"),
            "risk_budget": entry_result.get("risk_budget", 0),
        })
        if entry_result["matched"]:
            print(f"  {ver}: ACTIVE (locked at entry — {entry_result['reason']})")

    matched_count = sum(1 for s in signals if s["matched"])
    print(f"  {matched_count} active signals")

    # ─── Update state ───
    state["last_update"] = datetime.utcnow().isoformat() + "Z"  # UTC with Z so browsers parse correctly
    state["market"] = {
        "spx": round(spx, 2),
        "vix": round(vix, 2),
        "prior_close": prior["prior_close"],
        "prior_direction": prior["prior_direction"],
        "prior_return": prior["prior_return"],
        "gap_pct": round(gap_pct, 3),
        "week_high": weekly["week_high"],
        "week_low": weekly["week_low"],
        "in_range": in_range,
        "ret5d": ret5d,
        "vp_ratio": vp,
        "rv": round(full_rv, 1) if full_rv else None,
        "rv_slope": rv_slope_label,
        "rv_slope_pct": rv_slope_pct,
        "range_pct": range_pct,
        "score_vol": score_vol,
        "rv_1d_change": rv_1d_chg,
        "daily_risk": DAILY_RISK,
    }
    state["regime"] = {
        "label": regime_label,
        "vix_bucket": regime_label.split("_")[0],
        "prior_day": regime_label.split("_")[1],
        "range": regime_label.split("_")[2],
        "gap": regime_label.split("_")[3],
    }
    state["signals"] = signals
    state["option_chain"] = chain

    # ─── PHOENIX v3 Signal Confluence (locked at 10:00 AM to match backtest) ───
    # The backtest evaluates all PHOENIX filters using 10:00 AM data frozen in the CSV.
    # VIX, VP, RV slope, and in_range are all snapshots at 10:00 AM — they never change
    # during the trading day. The cockpit must do the same: evaluate once at 10:00 AM,
    # lock the result, and display it unchanged for the rest of the session.
    global _phoenix_lock, _phoenix_lock_date
    if _phoenix_lock_date != today_str:
        _phoenix_lock = None
        _phoenix_lock_date = today_str

    # ─── Clear stale active trade from a previous day ───
    # 0DTE trades expire at close — if we still have an active trade from
    # yesterday, it means the cockpit didn't cleanly close it (crash, API
    # timeout, etc.).  Auto-close it so today starts fresh.
    stale_trade = load_active_trade()
    if stale_trade and stale_trade.get("status") == "active":
        entry_date = stale_trade.get("entry_time", "")[:10]  # "2026-02-24" from ISO
        if entry_date and entry_date != today_str:
            print(f"  TRADE: Auto-closing stale {stale_trade.get('ver','')} trade from {entry_date} (today is {today_str})")
            stale_trade["status"] = "closed"
            stale_trade["exit_type"] = "EOD_AUTO"
            stale_trade["exit_time"] = f"{entry_date}T16:00:00"
            stale_trade["exit_sv"]   = stale_trade.get("current_sv", stale_trade.get("entry_credit", 0))
            save_active_trade(stale_trade)
            append_live_trade(stale_trade)
            # Clear it so today can start fresh
            clear_active_trade()

    # Restore lock from active trade file on restart (if feed restarted mid-day)
    # IMPORTANT: Only restore if the saved lock is from TODAY — otherwise we'd
    # display yesterday's stale signal on the next morning.
    if _phoenix_lock is None:
        active_trade_check = load_active_trade()
        if active_trade_check and active_trade_check.get("phoenix_locked"):
            saved = active_trade_check["phoenix_locked"]
            lock_date = saved.get("lock_time", "")[:10]  # e.g. "2026-02-24" from "2026-02-24 10:00"
            if lock_date == today_str:
                _phoenix_lock = {
                    "ctx": saved.get("ctx", {}),
                    "result": {
                        "fire_count": saved["fire_count"],
                        "sizing": saved["sizing"],
                        "signals": saved.get("signals", []),
                        "adaptive_mech": None,  # Will be re-derived from signals
                    },
                    "lock_time": saved["lock_time"],
                }
                # Re-derive adaptive_mech from locked signals
                for sig_info in saved.get("signals", []):
                    if sig_info["firing"]:
                        for sig_def in PHOENIX_SIGNALS:
                            if sig_def["rank"] == sig_info["rank"]:
                                _phoenix_lock["result"]["adaptive_mech"] = sig_def["mech"]
                                break
                        break
                print(f"  PHOENIX: Restored lock from trade file (locked at {saved['lock_time']})")
            else:
                # Stale lock from a previous day — clear it from the trade file
                del active_trade_check["phoenix_locked"]
                save_active_trade(active_trade_check)
                print(f"  PHOENIX: Cleared stale lock from {lock_date} (today is {today_str})")

    can_lock = now.hour > 10 or (now.hour == 10 and now.minute >= 0)

    if _phoenix_lock is not None:
        # Already locked for today — use frozen values
        phoenix = _phoenix_lock["result"]
        state["phoenix"] = phoenix
        state["phoenix_locked"] = True
        state["phoenix_lock_time"] = _phoenix_lock["lock_time"]
        state["phoenix_lock_ctx"] = _phoenix_lock["ctx"]
        firing_names = [s["name"] for s in phoenix["signals"] if s["firing"]]
        print(f"  PHOENIX: {phoenix['fire_count']} signals firing [LOCKED at {_phoenix_lock['lock_time']}]")
        if firing_names:
            for fn in firing_names:
                print(f"    + {fn}")
    else:
        if can_lock:
            # LOCK TIME: compute VP from morning bars only (9:30-10:00) to match backtest
            # Backtest uses: RV = calc_rv(morning) where morning = spx_df 9:30-10:00 AM
            if _minute_bars:
                first_t = _minute_bars[0]["t"] / 1000
                first_dt = datetime.fromtimestamp(first_t)
                cutoff = first_dt.replace(hour=10, minute=0, second=0, microsecond=0)
                cutoff_ms = cutoff.timestamp() * 1000
                morning_bars = [b for b in _minute_bars if b["t"] <= cutoff_ms]
                morning_rv = calc_rv_from_bars(morning_bars) if len(morning_bars) >= 5 else full_rv
                # Backtest in_range uses today's open price, not current SPX
                lock_spx_open = _minute_bars[0]["o"]
            else:
                morning_rv = full_rv
                lock_spx_open = spx

            lock_vp = compute_vp_ratio(vix, morning_rv)
            lock_in_range = weekly["week_low"] <= lock_spx_open <= weekly["week_high"] if (weekly.get("week_high") and weekly.get("week_low")) else True

            phoenix_ctx = {
                "vix": vix,
                "vp": lock_vp,
                "ret5d": ret5d,
                "rv_1d_change": rv_1d_chg,
                "prior_direction": prior["prior_direction"],
                "in_range": lock_in_range,
                "rv_slope_label": rv_slope_label,
            }
            phoenix = evaluate_phoenix(phoenix_ctx)

            _phoenix_lock = {
                "ctx": phoenix_ctx,
                "result": phoenix,
                "lock_time": now.strftime("%H:%M:%S"),
            }
            state["phoenix"] = phoenix
            state["phoenix_locked"] = True
            state["phoenix_lock_time"] = _phoenix_lock["lock_time"]
            state["phoenix_lock_ctx"] = _phoenix_lock["ctx"]
            firing_names = [s["name"] for s in phoenix["signals"] if s["firing"]]
            print(f"  PHOENIX: {phoenix['fire_count']} signals firing [LOCKED NOW at {_phoenix_lock['lock_time']}]")
            if firing_names:
                for fn in firing_names:
                    print(f"    + {fn}")
            print(f"    Lock ctx: VIX={vix:.2f}, VP={lock_vp:.2f} (morning RV={morning_rv}), in_range={lock_in_range}, rv_slope={rv_slope_label}")
        else:
            # Pre-10am: show live preview (informational only, not used for trading decisions)
            phoenix_ctx = {
                "vix": vix,
                "vp": vp,
                "ret5d": ret5d,
                "rv_1d_change": rv_1d_chg,
                "prior_direction": prior["prior_direction"],
                "in_range": in_range,
                "rv_slope_label": rv_slope_label,
            }
            phoenix = evaluate_phoenix(phoenix_ctx)
            state["phoenix"] = phoenix
            state["phoenix_locked"] = False
            state["phoenix_lock_time"] = None
            firing_names = [s["name"] for s in phoenix["signals"] if s["firing"]]
            print(f"  PHOENIX: {phoenix['fire_count']} signals firing [PREVIEW - locks at 10:00]")
            if firing_names:
                for fn in firing_names:
                    print(f"    + {fn}")

    if phoenix.get("adaptive_mech"):
        m = phoenix["adaptive_mech"]
        print(f"    Adaptive mech: {m['target']}% / {m['time_stop']} / {m['tranches']}")

    # ─── Active trade management (locked strike + tranche tracking) ───
    active_trade = load_active_trade()
    if active_trade and active_trade.get("status") == "active":
        locked_atm = active_trade["atm"]
        locked_ww = active_trade["wing_width"]

        # Fetch option prices at LOCKED strike + LOCKED wing width (not current ATM/VIX)
        locked_chain = get_0dte_options(locked_atm, ww_override=locked_ww)
        current_sv = compute_current_spread_val(locked_chain)

        # T1 default qty (used as fallback for tranches without per-tranche qty)
        fill_qty = active_trade.get("fill_qty", 10)

        # Weighted average credit of filled tranches (weighted by qty)
        filled = [t for t in active_trade["tranches"] if t["status"] == "filled" and t.get("credit")]
        total_weighted_credit = sum(t["credit"] * t.get("qty", fill_qty) for t in filled)
        total_filled_qty = sum(t.get("qty", fill_qty) for t in filled)
        avg_credit = total_weighted_credit / total_filled_qty if total_filled_qty > 0 else 0

        # P&L calculation + per-tranche max loss tracking
        total_pnl = 0
        total_max_loss = 0       # cumulative max loss across all filled tranches
        total_contracts = 0      # total contracts across all filled tranches
        tranche_risk_detail = []  # per-tranche risk breakdown
        for t in filled:
            t_credit = t["credit"]
            t_qty = t.get("qty", fill_qty)  # per-tranche qty (dynamic sizing)
            t_pnl = (t_credit - current_sv) * t_qty * 100
            t_max_loss = (locked_ww - t_credit) * t_qty * 100  # worst case per tranche
            total_pnl += t_pnl
            total_max_loss += t_max_loss
            total_contracts += t_qty
            tranche_risk_detail.append({
                "idx": t["idx"],
                "credit": round(t_credit, 2),
                "qty": t_qty,
                "max_loss": round(t_max_loss),
                "current_pnl": round(t_pnl),
            })
        total_pnl = round(total_pnl)
        profit_pct = round((1 - current_sv / avg_credit) * 100, 1) if avg_credit > 0 else 0

        # Dynamic per-tranche sizing: each tranche gets sized to stay within
        # the remaining daily risk budget based on CURRENT credit, not T1 credit
        remaining_budget = DAILY_RISK - total_max_loss
        pending_tranches = [t for t in active_trade["tranches"] if t["status"] in ("pending", "ready")]
        n_remaining = len(pending_tranches)
        # Budget per remaining tranche
        budget_per_remaining = remaining_budget / n_remaining if n_remaining > 0 else 0
        # Max loss per spread at current credit
        ml_per_spread_now = (locked_ww - current_sv) * 100 if current_sv < locked_ww else 1
        # Recommended qty for next tranche
        recommended_qty = max(1, int(budget_per_remaining / ml_per_spread_now)) if ml_per_spread_now > 0 else 0
        next_tranche_max_loss = round(ml_per_spread_now * recommended_qty) if pending_tranches else 0
        projected_total_max_loss = round(total_max_loss + next_tranche_max_loss)

        # Check tranche schedule and validity
        try:
            from zoneinfo import ZoneInfo
            et_now_dt = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            et_now_dt = now
        et_hm = et_now_dt.strftime("%H:%M")

        # Only mark the NEXT pending tranche as "ready" (sequential, not all at once).
        # This ensures sizing recalculates after each fill.
        next_promoted = False
        for t in active_trade["tranches"]:
            if t["status"] == "ready":
                # Already one ready — don't promote another until this is filled/skipped
                next_promoted = True
            if t["status"] == "pending" and et_hm >= t["scheduled"]:
                if not next_promoted:
                    # Promote this one to ready
                    t["status"] = "ready"
                    t["valid"] = True
                    t["validity_reason"] = "Enter now (matches backtest)"
                    t["current_credit"] = round(current_sv, 2)
                    t["recommended_qty"] = recommended_qty
                    next_promoted = True
                else:
                    # Past due but waiting for the previous tranche to be filled first
                    t["validity_reason"] = "Waiting — fill previous tranche first"
                    t["current_credit"] = round(current_sv, 2)

        # Update trade with live data
        active_trade["current_sv"] = round(current_sv, 2)
        active_trade["avg_credit"] = round(avg_credit, 2)
        active_trade["total_pnl"] = total_pnl
        active_trade["profit_pct"] = profit_pct
        active_trade["spx_distance"] = round(spx - locked_atm, 2)
        active_trade["total_max_loss"] = round(total_max_loss)
        active_trade["remaining_budget"] = round(remaining_budget)
        active_trade["recommended_qty"] = recommended_qty
        active_trade["ml_per_spread_now"] = round(ml_per_spread_now)
        active_trade["next_tranche_max_loss"] = next_tranche_max_loss
        active_trade["projected_total_max_loss"] = projected_total_max_loss
        active_trade["tranche_risk_detail"] = tranche_risk_detail
        active_trade["daily_risk_budget"] = DAILY_RISK
        active_trade["risk_utilization_pct"] = round(total_max_loss / DAILY_RISK * 100, 1) if DAILY_RISK > 0 else 0

        # Wing breach detection: SPX at or beyond wing strike (physical)
        spx_dist_abs = abs(spx - locked_atm)
        active_trade["wing_breach"] = spx_dist_abs >= locked_ww
        active_trade["wing_proximity_pct"] = round(spx_dist_abs / locked_ww * 100, 1) if locked_ww > 0 else 0

        # P&L-based wing stop (70% of max loss) — matches backtest exit rule
        # Fires when spread value > avg_credit + 70% of (max_loss per spread)
        max_loss_per_spread = locked_ww - avg_credit
        if max_loss_per_spread > 0 and avg_credit > 0:
            wing_stop_level = round(avg_credit + 0.70 * max_loss_per_spread, 2)
            wing_stop_loss_pct = round((current_sv - avg_credit) / max_loss_per_spread * 100, 1) if current_sv > avg_credit else 0
        else:
            wing_stop_level = locked_ww
            wing_stop_loss_pct = 0
        active_trade["wing_stop_level"] = wing_stop_level    # spread value that triggers stop
        active_trade["wing_stop_triggered"] = current_sv >= wing_stop_level and avg_credit > 0
        active_trade["wing_stop_loss_pct"] = max(0, wing_stop_loss_pct)  # % of max loss realized

        active_trade["locked_chain"] = {
            "atm_put_price": locked_chain.get("atm_put_price", 0),
            "atm_call_price": locked_chain.get("atm_call_price", 0),
            "wing_put_price": locked_chain.get("wing_put_price", 0),
            "wing_call_price": locked_chain.get("wing_call_price", 0),
            "credit": locked_chain.get("credit", 0),
            "spread_val": current_sv,
        }

        # Store locked PHOENIX signal state in trade file for persistence across restarts
        if _phoenix_lock is not None:
            active_trade["phoenix_locked"] = {
                "fire_count": _phoenix_lock["result"]["fire_count"],
                "sizing": _phoenix_lock["result"]["sizing"],
                "lock_time": _phoenix_lock["lock_time"],
                "signals": [
                    {"rank": s["rank"], "name": s["name"], "firing": s["firing"],
                     "filters": s.get("filters", [])}
                    for s in _phoenix_lock["result"]["signals"]
                ],
                "ctx": _phoenix_lock["ctx"],
            }

        save_active_trade(active_trade)
        state["active_trade"] = active_trade

        n_filled = len(filled)
        n_ready = sum(1 for t in active_trade["tranches"] if t["status"] == "ready")
        wing_warn = " *** WING BREACH ***" if active_trade["wing_breach"] else f" | Wing: {active_trade['wing_proximity_pct']:.0f}%"
        print(f"  Trade: {active_trade['ver']} ATM={locked_atm} | {n_filled} filled, {n_ready} ready | P&L ${total_pnl:+,} ({profit_pct:+.1f}%){wing_warn}")
    else:
        state["active_trade"] = None

    write_state()

def write_state():
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ─── Built-in HTTP server (serves cockpit HTML + state JSON) ───
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

HTTP_PORT = int(os.environ.get("PORT", 8811))

class QuietHandler(SimpleHTTPRequestHandler):
    """Serve files from _DIR, suppress request logs, handle trade API."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=_DIR, **kwargs)
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        """Inject CORS header on every response (GET, POST, etc.)."""
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_GET(self):
        """Redirect / to /trading_cockpit.html instead of showing directory listing."""
        if self.path == "/" or self.path == "":
            self.send_response(302)
            self.send_header("Location", "/trading_cockpit.html")
            self.end_headers()
            return
        super().do_GET()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def send_json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj, default=str).encode())

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        if self.path == "/api/confirm_entry":
            try:
                data = json.loads(body)
                ver = data.get("ver", "v3")
                strat = next((s for s in STRATEGIES if s["ver"] == ver), None)
                if not strat:
                    self.send_json(400, {"error": "Unknown strategy"})
                    return
                mech_info = parse_mech(strat["mech"])
                # Build tranche schedule
                tranches = [{
                    "idx": 1, "scheduled": strat["entry"],
                    "status": "filled", "credit": data.get("credit", 0),
                    "fill_time": datetime.now().strftime("%H:%M"),
                    "valid": True, "validity_reason": "Entered",
                }]
                for i, add_time in enumerate(mech_info["add_times"]):
                    tranches.append({
                        "idx": i + 2, "scheduled": add_time,
                        "status": "pending", "credit": None, "fill_time": None,
                        "valid": None, "validity_reason": "Waiting for scheduled time",
                        "current_credit": None,
                    })
                # Grab market context at entry for live trade logging
                sig_entry = next((s for s in state.get("signals", []) if s.get("ver") == ver), {})
                mkt_entry = state.get("market", {})
                trade = {
                    "ver": ver,
                    "atm": data.get("atm"),
                    "wing_width": data.get("wing_width", WING_WIDTH_BASE),
                    "entry_credit": data.get("credit", 0),
                    "fill_qty": data.get("qty", 10),
                    "entry_time": datetime.now().isoformat(),
                    "mech": strat["mech"],
                    "profit_target_pct": mech_info["profit_pct"],
                    "stop_time": mech_info["stop_time"],
                    "tranches": tranches,
                    "status": "active",
                    # Market context snapshot (used by append_live_trade on exit)
                    "risk_budget": sig_entry.get("risk_budget", 0),
                    "entry_vix":   round(float(mkt_entry.get("vix", 0)), 1),
                    "entry_vp":    round(float(mkt_entry.get("vp_ratio", 0)), 2),
                }
                if ver == "v3":
                    trade["fire_count"] = state.get("phoenix", {}).get("fire_count")
                save_active_trade(trade)
                print(f"  TRADE CONFIRMED: {ver} ATM={trade['atm']} credit={trade['entry_credit']} qty={trade['fill_qty']}")
                self.send_json(200, {"ok": True, "trade": trade})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        elif self.path == "/api/fill_tranche":
            try:
                data = json.loads(body)
                trade = load_active_trade()
                if not trade:
                    self.send_json(400, {"error": "No active trade"})
                    return
                idx = data.get("idx")
                for t in trade["tranches"]:
                    if t["idx"] == idx and t["status"] in ("ready", "pending"):
                        t["status"] = "filled"
                        t["credit"] = data.get("credit", 0)
                        t["qty"] = data.get("qty", trade.get("fill_qty", 10))
                        t["fill_time"] = datetime.now().strftime("%H:%M")
                        break
                save_active_trade(trade)
                print(f"  TRANCHE FILLED: T{idx} credit={data.get('credit', 0)}")
                self.send_json(200, {"ok": True, "trade": trade})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        elif self.path == "/api/skip_tranche":
            try:
                data = json.loads(body)
                trade = load_active_trade()
                if not trade:
                    self.send_json(400, {"error": "No active trade"})
                    return
                idx = data.get("idx")
                for t in trade["tranches"]:
                    if t["idx"] == idx:
                        t["status"] = "skipped"
                        break
                save_active_trade(trade)
                print(f"  TRANCHE SKIPPED: T{idx}")
                self.send_json(200, {"ok": True, "trade": trade})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        elif self.path == "/api/exit_trade":
            try:
                data = json.loads(body)
                trade = load_active_trade()
                if not trade:
                    self.send_json(400, {"error": "No active trade"})
                    return
                trade["status"] = "closed"
                trade["exit_type"] = data.get("exit_type", "manual")
                trade["exit_time"] = datetime.now().isoformat()
                trade["exit_sv"] = data.get("exit_sv", 0)
                save_active_trade(trade)
                append_live_trade(trade)
                print(f"  TRADE CLOSED: {trade['ver']} type={trade['exit_type']}")
                self.send_json(200, {"ok": True, "trade": trade})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        elif self.path == "/api/cancel_trade":
            clear_active_trade()
            print(f"  TRADE CANCELLED")
            self.send_json(200, {"ok": True})
            return

        elif self.path == "/api/refresh_stats":
            # Re-run compute_stats.py to regenerate strategy_trades.json and
            # strategy_stats.json from the current research_all_trades.csv.
            # Fast (~1-2s). The calendar HTML fetches these JSON files directly.
            import subprocess
            try:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                r1 = subprocess.run(
                    [sys.executable, os.path.join(_DIR, "compute_stats.py")],
                    capture_output=True, text=True, env=env, timeout=30
                )
                if r1.returncode == 0:
                    summary = [l for l in r1.stdout.splitlines() if l.strip()]
                    print(f"  REFRESH: Stats regenerated OK")
                    self.send_json(200, {"ok": True, "summary": summary[-3:]})
                else:
                    err = r1.stderr
                    print(f"  REFRESH ERROR: {err[:200]}")
                    self.send_json(500, {"error": err[:500]})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})

def start_http_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), QuietHandler)
    server.serve_forever()

# ─── Run ───
if __name__ == "__main__":
    # Restore live_trades.json from GitHub if running in cloud with no local copy
    _restore_live_trades_from_github()

    # Start HTTP server in background thread
    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    cloud = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PORT"))
    print("=" * 60)
    print("PHOENIX 0DTE IBF COCKPIT — Live Feed")
    print(f"Mode:     {'☁ CLOUD (Railway)' if cloud else '🖥  LOCAL'}")
    print(f"Polling every {POLL_SEC}s")
    print(f"State file: {STATE_PATH}")
    print(f"Cockpit URL: http://localhost:{HTTP_PORT}/trading_cockpit.html")
    if cloud:
        print(f"Public URL: (set by Railway — see your Railway dashboard)")
    print("=" * 60)

    while True:
        try:
            poll()
        except Exception as e:
            print(f"  POLL ERROR: {e}")
            state["errors"].append(f"{datetime.now().isoformat()}: {str(e)}")
            write_state()
        time.sleep(POLL_SEC)
