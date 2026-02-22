"""
SPX 0DTE — Iron Butterfly Backtest
====================================
Rules:
  - Structure:   Short Iron Butterfly only (ATM short straddle + OTM wings)
  - Entry:       10:00am ET, only if signal scores >= 70
  - Entry gate:  VWAP must be FLAT, RV slope must be STABLE or FALLING
  - Add rule:    If SPX drifts 15+ pts from any existing strike center AND
                 VWAP still FLAT AND RV slope still STABLE/FALLING →
                 open a second IBF centered at new SPX. Max 2 positions/day.
  - Exit:        50% of max credit collected, or 3:30pm hard stop — whichever first
  - No other management exits (keeps it clean and testable)

Usage:
    python3 backtest_v3.py YOUR_POLYGON_KEY [LOOKBACK_DAYS]
"""

import sys, math, time, csv, requests
import numpy as np
import pandas as pd
from datetime import date, timedelta
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

class _Norm:
    cdf = staticmethod(_norm_cdf)

norm = _Norm()
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY       = sys.argv[1] if len(sys.argv) > 1 else input("Polygon API key: ").strip()
LOOKBACK_DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 180
OUTPUT_FILE   = "backtest_v4_moderate_results.csv"
ET            = pytz.timezone("America/New_York")

WING_WIDTH     = 40     # pts each side for IBF wings
MIN_SCORE      = 55     # GO threshold (aggressive — was 70)
TARGET_PCT     = 0.50   # exit at 50% of max credit
MIN_RR         = 0.25   # min credit/max_loss ratio
LADDER_DRIFT   = 15     # SPX pts drift before considering a second IBF
ADD_SIZE_SCALE = 0.60   # second position is 60% the size of the first

# Risk budget — mirrors live system
DAILY_RISK_BUDGET  = 100_000   # max dollars to risk in a day
MAX_POSITIONS      = 3         # initial + up to 2 adds
# Budget is split into equal tranches so we always have room to add.
# Tranche 1 (initial entry): 1/3 of budget
# Tranche 2 (first add):     1/3 of budget
# Tranche 3 (second add):    1/3 of budget
TRANCHE_RISK       = DAILY_RISK_BUDGET / MAX_POSITIONS   # $33,333 per tranche
SPX_MULTIPLIER     = 100       # SPX options: $100 per point
SLIPPAGE_PER_SPR   = 1.00       # $1.00/spread round-trip (4 legs × ~$0.25/leg bid-ask)
MIN_VP             = 1.00       # minimum VIX/RV ratio (moderate — was 1.40, aggressive was 0.90)

# ── API helper ────────────────────────────────────────────────────────────────
def get_bars(ticker, day_str):
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{day_str}/{day_str}"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"adjusted":"true","sort":"asc","limit":500,"apiKey":API_KEY}, timeout=15)
            if r.status_code == 200:
                rows = r.json().get("results", [])
                if not rows:
                    return pd.DataFrame()
                df = pd.DataFrame(rows)
                df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
                return df.sort_values("t").reset_index(drop=True)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
        except Exception:
            time.sleep(1)
    return pd.DataFrame()


def trading_days(n):
    days, d = [], date.today() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


# ── Metric calculations ───────────────────────────────────────────────────────
def calc_rv(df):
    if df is None or len(df) < 5:
        return None
    lr = np.diff(np.log(df["c"].values))
    return np.std(lr) * np.sqrt(252 * 390) * 100


def calc_rv_slope(full_df, as_of_t):
    """
    At 10am entry there is no prior 30-min window (market opened at 9:30).
    Solution: split the 9:30-10:00 morning session into two 15-min halves.
      w1 = 9:30-9:45  (first half — the "prior")
      w2 = 9:45-10:00 (second half — "now")
    For intraday adds (after 10am), use the standard 30m vs prior 30m comparison.
    Returns (rv_now, rv_prev, label).
    """
    if full_df is None or len(full_df) < 8:
        return None, None, "UNKNOWN"

    market_open = as_of_t.replace(hour=9, minute=30, second=0, microsecond=0)
    mins_since_open = (as_of_t - market_open).total_seconds() / 60

    if mins_since_open <= 35:
        # At or near 10am — split morning into two 15-min halves
        mid = market_open + pd.Timedelta(minutes=15)
        w1  = full_df[(full_df["t"] >= market_open) & (full_df["t"] <  mid)]
        w2  = full_df[(full_df["t"] >= mid)          & (full_df["t"] <= as_of_t)]
    else:
        # Intraday — standard 30m vs prior 30m
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
    if df is None or len(df) < n + 1:
        return "FLAT"
    typ = (df["h"] + df["l"] + df["c"]) / 3
    vol = df["v"] if "v" in df.columns else pd.Series([1]*len(df), index=df.index)
    vwap = (typ * vol).cumsum() / vol.cumsum()
    recent = vwap.iloc[-n:].values
    slope_pct = np.polyfit(np.arange(len(recent)), recent, 1)[0] / recent.mean() * 100
    if slope_pct >  0.01: return "RISING"
    if slope_pct < -0.01: return "FALLING"
    return "FLAT"


def calc_term_structure(vix, vix9d):
    if not vix or not vix9d or vix == 0:
        return None, "UNKNOWN"
    r = vix9d / vix
    return r, ("INVERTED" if r > 1.05 else ("FLAT" if r > 0.95 else "CONTANGO"))


def slopes_ok(vwap_label, rv_slope_label, allow_unknown=False):
    """
    Both slopes must be benign to allow entry or add.
    UNKNOWN = insufficient data to compute slope.
    allow_unknown=False (default): treat UNKNOWN as blocking — conservative.
    allow_unknown=True: pass through when data is missing (used for intraday adds
                        early in the session before enough bars exist).
    """
    ok_rv = rv_slope_label in ("STABLE", "FALLING") or (allow_unknown and rv_slope_label == "UNKNOWN")
    return vwap_label == "FLAT" and ok_rv


# ── Scoring ───────────────────────────────────────────────────────────────────
def score_entry(rv, vix, vix9d, range_pct, vwap_label, ts_label, ts_ratio,
                rv_slope_label):
    s = {}

    # Vol premium — IV (VIX proxy) / RV  [aggressive: softer curve]
    vp = (vix / rv) if vix and rv else None
    if vp:
        s["vol_premium"] = 30 if vp >= 1.5 else (22 if vp >= 1.2 else (15 if vp >= 1.0 else (10 if vp >= 0.85 else 3)))
    else:
        s["vol_premium"] = 0

    # RV slope adjustment
    if rv_slope_label == "RISING":
        s["vol_premium"] = max(0, s["vol_premium"] - 15)
    elif rv_slope_label == "FALLING":
        s["vol_premium"] = min(30, s["vol_premium"] + 5)

    # Skew proxy (VIX level)
    if vix:
        s["skew"] = 16 if 16 <= vix <= 28 else (8 if vix < 16 else (12 if vix <= 35 else 0))
    else:
        s["skew"] = 0

    # Market regime + VWAP
    if range_pct is not None:
        base  = 14 if range_pct < 0.4 else (8 if range_pct < 0.7 else 0)
        bonus = 6 if vwap_label == "FLAT" and base > 0 else 0
        if vwap_label in ("RISING", "FALLING") and base > 0:
            base = max(0, base - 4)
        s["regime"] = min(20, base + bonus)
    else:
        s["regime"] = 0

    # VIX term structure
    s["term_structure"] = {"INVERTED": 20, "FLAT": 12, "CONTANGO": 4}.get(ts_label, 0)

    # Entry timing (10am always gets full score)
    s["timing"] = 10

    total = sum(s.values())
    return total, s, vp


# ── Black-Scholes ─────────────────────────────────────────────────────────────
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


def price_ibf(S, iv, T):
    """Price an iron butterfly centered at S. Returns dict with all fields."""
    atm = snap(S)
    wp  = snap(S - WING_WIDTH)
    wc  = snap(S + WING_WIDTH)
    ap  = bs_put(S, atm, T, iv)
    ac  = bs_call(S, atm, T, iv)
    wpp = bs_put(S, wp, T, iv)
    wcp = bs_call(S, wc, T, iv)
    credit     = ap + ac - wpp - wcp
    max_profit = credit
    max_loss   = (atm - wp) - credit
    rr         = credit / max_loss if max_loss > 0 else 0
    return {
        "center":     atm,
        "wp":         wp,
        "wc":         wc,
        "credit":     round(credit, 3),
        "max_profit": round(max_profit, 3),
        "max_loss":   round(max_loss, 3),
        "target":     round(credit * TARGET_PCT, 3),
        "rr":         round(rr, 3),
        "T_entry":    T,
        "iv":         iv,
        "S_entry":    S,
    }


def current_value(pos, S_now, T_now):
    """
    Reprice the IBF using Black-Scholes at current SPX and time remaining.
    Uses dynamic IV: vol expands on adverse (down) moves, compresses slightly on up moves.
    """
    iv_entry = pos["iv"]
    S_entry  = pos["S_entry"]
    atm  = pos["center"]
    wp   = pos["wp"]
    wc   = pos["wc"]
    credit_entry = pos["credit"]
    mp   = pos["max_profit"]
    ml   = pos["max_loss"]

    iv_now = adj_iv(iv_entry, S_entry, S_now)

    cost_to_close = (bs_put(S_now, atm, T_now, iv_now)
                   + bs_call(S_now, atm, T_now, iv_now)
                   - bs_put(S_now, wp,  T_now, iv_now)
                   - bs_call(S_now, wc, T_now, iv_now))

    pnl = credit_entry - cost_to_close
    return round(max(-ml, min(mp, pnl)), 4)


def adj_iv(iv_entry, S_entry, S_now):
    """
    Approximate intraday vol expansion/compression based on SPX move.
    Down moves: IV rises ~1 vol pt per 10 SPX pts (~3 pts at 30pt move).
      Calibrated to real 0DTE behavior: a 30pt SPX decline intraday
      typically drives 0DTE ATM IV up 2-3 vol points.
    Up moves: IV falls slightly — capped at 92% of entry IV.
    """
    move = S_now - S_entry
    if move < 0:
        return iv_entry + abs(move) * 0.001
    else:
        return max(iv_entry * 0.92, iv_entry - move * 0.0002)


def mins_to_close(bar_t):
    return max(0, (15*60 + 30) - (bar_t.hour*60 + bar_t.minute))


def T_from_bar(bar_t):
    return max(0.0001, mins_to_close(bar_t) / (252 * 390))


# ── Single position simulator ─────────────────────────────────────────────────
def run_position(pos, bars, scale=1.0):
    """
    Walk bars from entry until 50% target or 3:30pm.
    Returns (pnl, outcome, exit_bar_t).
    pnl is per-spread at given scale.
    """
    target = pos["target"]
    ml     = pos["max_loss"]

    wp = pos["wp"]
    wc = pos["wc"]

    for _, bar in bars.iterrows():
        bar_t = bar["t"]
        S_now = bar["c"]
        T_now = T_from_bar(bar_t)
        val   = current_value(pos, S_now, T_now)

        # 3:30pm time stop
        if bar_t.hour > 15 or (bar_t.hour == 15 and bar_t.minute >= 30):
            return round(val * scale, 3), "TIME_STOP", bar_t

        # 50% profit target
        if val >= target:
            return round(val * scale, 3), "TARGET", bar_t

        # Wing breach stop: SPX has crossed a wing strike — exit immediately.
        # At this point the position is short gamma with the strike in-the-money;
        # continuing to hold only risks further loss with diminishing recovery odds.
        if S_now <= wp or S_now >= wc:
            return round(val * scale, 3), "WING_STOP", bar_t

    # End of day without hitting either — use last bar value
    if not bars.empty:
        last = bars.iloc[-1]
        val  = current_value(pos, last["c"], T_from_bar(last["t"]))
        return round(val * scale, 3), "EXPIRY", last["t"]

    return 0.0, "NO_BARS", None


def calc_spreads(tranche_risk, max_loss_per_spread):
    """
    How many spreads fit within a tranche risk budget.
    max_loss_per_spread is in dollars (already multiplied by $100).
    """
    if max_loss_per_spread <= 0:
        return 0, 0
    n = int(tranche_risk / max_loss_per_spread)
    return max(1, n), n * max_loss_per_spread


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_backtest():
    days    = trading_days(LOOKBACK_DAYS)
    results = []

    print(f"\n{'SPX 0DTE — Iron Butterfly Backtest':=^95}")
    print(f"  Entry: 10am · Exit: 50% target or 3:30pm · Wings: ±{WING_WIDTH}pts · "
          f"Add if drift >{LADDER_DRIFT}pts + slopes OK")
    print(f"  {LOOKBACK_DAYS} trading days lookback\n")
    print(f"{'Date':<12} {'Sc':>4} {'VIX':>5} {'RV':>5} {'RVSlp':<8} {'VWAP':<8} "
          f"{'VP':>5} {'N':>4} {'P1 ($)':>10} {'P2 ($)':>10} {'Total':>10}  Outcome")
    print("-" * 105)

    for day in days:
        ds = day.strftime("%Y-%m-%d")

        spx_df   = get_bars("I:SPX",   ds)
        vix_df   = get_bars("I:VIX",   ds)
        vix9d_df = get_bars("I:VIX9D", ds)
        if vix9d_df.empty:
            vix9d_df = get_bars("I:VXST", ds)

        if spx_df.empty:
            continue

        open_t  = spx_df["t"].iloc[0].replace(hour=9,  minute=30, second=0, microsecond=0)
        entry_t = spx_df["t"].iloc[0].replace(hour=10, minute=0,  second=0, microsecond=0)

        # 9:30–10:00 morning window
        morning = spx_df[(spx_df["t"] >= open_t) & (spx_df["t"] <= entry_t)]
        if len(morning) < 5:
            continue

        def val_at(df, t):
            if df.empty: return None
            near = df[df["t"] <= t]
            return near.iloc[-1]["c"] if not near.empty else None

        vix_val   = val_at(vix_df, entry_t)
        vix9d_val = val_at(vix9d_df, entry_t)
        if not vix_val:
            continue

        iv = vix_val / 100.0

        # ── Metrics at 10am ───────────────────────────────────────────────────
        rv          = calc_rv(morning)
        range_pct   = (morning["h"].max() - morning["l"].min()) / morning.iloc[0]["o"] * 100
        drift_pct   = (morning.iloc[-1]["c"] - morning.iloc[0]["o"]) / morning.iloc[0]["o"] * 100
        vwap_lbl    = calc_vwap_label(morning)
        ts_ratio, ts_lbl = calc_term_structure(vix_val, vix9d_val)
        _, _, rv_slope_lbl = calc_rv_slope(spx_df, entry_t)

        score, scores, vp = score_entry(
            rv, vix_val, vix9d_val, range_pct, vwap_lbl, ts_lbl, ts_ratio, rv_slope_lbl
        )

        entry_row = spx_df[spx_df["t"] >= entry_t]
        if entry_row.empty:
            continue
        spx_entry = entry_row.iloc[0]["o"]
        bars_from_entry = spx_df[spx_df["t"] >= entry_t].copy()

        # ── Entry gate ────────────────────────────────────────────────────────
        rec = "GO" if score >= MIN_SCORE else ("WAIT" if score >= 35 else "SKIP")

        # Hard gate: slopes must be ok at entry for any position
        if rec == "GO" and (vp is None or vp < MIN_VP):
            rec = "SKIP"
            results.append(_row(ds, score, vix_val, rv, rv_slope_lbl, vwap_lbl, vp,
                                rec, 0, 0, 0, "VP_SKIP", "—", False, range_pct, ts_lbl, scores))
            print(f"{ds:<12} {score:>4} {vix_val:>5.1f} {rv or 0:>5.1f} "
                  f"{rv_slope_lbl:<8} {vwap_lbl:<8} {vp or 0:>5.2f}       —       —       —  "
                  f"SKIP — VP {vp:.2f} < {MIN_VP}")
            time.sleep(0.25)
            continue

        if rec == "GO" and not slopes_ok(vwap_lbl, rv_slope_lbl):
            rec = "SKIP"
            outcome_str = f"SKIP — slopes bad ({vwap_lbl}/{rv_slope_lbl})"
            results.append(_row(ds, score, vix_val, rv, rv_slope_lbl, vwap_lbl, vp,
                                rec, 0, 0, 0, "ENV_BLOCK", "—", False, range_pct, ts_lbl, scores))
            print(f"{ds:<12} {score:>4} {vix_val:>5.1f} {rv or 0:>5.1f} "
                  f"{rv_slope_lbl:<8} {vwap_lbl:<8} {vp or 0:>5.2f}       —       —       —  {outcome_str}")
            time.sleep(0.25)
            continue

        # ── Position 1 ────────────────────────────────────────────────────────
        pnl1, outcome1, exit_t1 = 0, "NO_TRADE", None
        pnl2, outcome2          = 0, "—"
        add_tried               = False

        if rec == "GO":
            T_e = T_from_bar(entry_row.iloc[0]["t"])
            pos1 = price_ibf(spx_entry, iv, T_e)

            # ── Size P1 to 1/3 of daily budget ───────────────────────────────
            # Deliberately leaving 2/3 of budget available for up to 2 adds.
            ml_per_spread1   = pos1["max_loss"] * SPX_MULTIPLIER
            n1, risk1        = calc_spreads(TRANCHE_RISK, ml_per_spread1)
            remaining_budget = DAILY_RISK_BUDGET - risk1

            if pos1["rr"] < MIN_RR:
                rec = "RR_SKIP"
                outcome1 = f"RR_SKIP ({pos1['rr']:.2f}x)"
            else:
                # P&L in dollars = per-spread result × n_spreads × $100 multiplier
                raw1, outcome1, exit_t1 = run_position(pos1, bars_from_entry, scale=1.0)
                slip1 = n1 * SLIPPAGE_PER_SPR * SPX_MULTIPLIER
                pnl1 = round(raw1 * n1 * SPX_MULTIPLIER - slip1, 0)

                # ── Ladder scan (up to MAX_POSITIONS-1 adds) ──────────────────
                adds_done   = 0
                last_center = pos1["center"]

                if outcome1 in ("TARGET", "TIME_STOP", "EXPIRY"):
                    for _, bar in bars_from_entry.iterrows():
                        if adds_done >= MAX_POSITIONS - 1:
                            break

                        bar_t = bar["t"]

                        # Stop scanning after P1 exits
                        if exit_t1 and bar_t >= exit_t1:
                            break

                        # Need at least 90 min left — enough theta to be worthwhile
                        if mins_to_close(bar_t) < 90:
                            break

                        # No budget left to add
                        if remaining_budget < 1_000:
                            break

                        spx_now = bar["c"]
                        drift   = abs(spx_now - last_center)
                        if drift < LADDER_DRIFT:
                            continue

                        # Recompute slopes in real time
                        _, _, rv_sl_now = calc_rv_slope(spx_df, bar_t)
                        vwap_now = calc_vwap_label(spx_df[spx_df["t"] <= bar_t].tail(30))

                        if not slopes_ok(vwap_now, rv_sl_now, allow_unknown=True):
                            # Slopes have turned — no add, and no further scanning
                            add_tried = True
                            outcome2  = f"ADD_BLOCKED ({vwap_now}/{rv_sl_now})"
                            break

                        # ── Open next IBF at current SPX ──────────────────────
                        # Refresh IV at add time: vol has shifted since 10am entry
                        add_tried = True
                        T_add     = T_from_bar(bar_t)
                        iv_at_add = adj_iv(iv, spx_entry, spx_now)
                        pos_add   = price_ibf(spx_now, iv_at_add, T_add)

                        if pos_add["rr"] < MIN_RR:
                            outcome2 = f"ADD_RR_SKIP ({pos_add['rr']:.2f}x)"
                            break

                        # Size this add to its own tranche (capped at remaining)
                        ml_add       = pos_add["max_loss"] * SPX_MULTIPLIER
                        n_add, r_add = calc_spreads(min(TRANCHE_RISK, remaining_budget), ml_add)
                        remaining_budget -= r_add

                        bars_from_add  = spx_df[spx_df["t"] >= bar_t].copy()
                        raw_add, oc_add, _ = run_position(pos_add, bars_from_add, scale=1.0)
                        slip_add  = n_add * SLIPPAGE_PER_SPR * SPX_MULTIPLIER
                        pnl2     += round(raw_add * n_add * SPX_MULTIPLIER - slip_add, 0)
                        tag       = "ADD" if adds_done == 0 else f"ADD{adds_done+1}"
                        outcome2  = f"{tag}_{oc_add}"
                        adds_done += 1
                        last_center = pos_add["center"]   # drift measured from new center

        combined = round(pnl1 + pnl2, 0)

        _n1    = n1    if rec == "GO" and outcome1 not in ("RR_SKIP",) else 0
        _risk1 = risk1 if rec == "GO" and outcome1 not in ("RR_SKIP",) else 0
        results.append(_row(ds, score, vix_val, rv, rv_slope_lbl, vwap_lbl, vp,
                            rec, pnl1, pnl2, combined, outcome1, outcome2,
                            add_tried, range_pct, ts_lbl, scores,
                            n1=_n1, risk1=_risk1))

        rv_str = f"{rv:.1f}" if rv else "—"
        vp_str = f"{vp:.2f}" if vp else "—"
        p1_str  = f"${pnl1:>+8,.0f}" if rec == "GO" else "         —"
        p2_str  = f"${pnl2:>+8,.0f}" if add_tried else "         —"
        tot_str = f"${combined:>+8,.0f}" if rec == "GO" else "         —"
        ns_str  = f"{n1}x" if rec == "GO" and 'n1' in dir() else "—"
        print(f"{ds:<12} {score:>4} {vix_val:>5.1f} {rv_str:>5} "
              f"{rv_slope_lbl:<8} {vwap_lbl:<8} {vp_str:>5} {ns_str:>4} "
              f"{p1_str} {p2_str} {tot_str}  "
              f"{outcome1}{'  +'+outcome2 if add_tried else ''}")

        time.sleep(0.25)

    return results


def _row(ds, score, vix, rv, rv_sl, vwap, vp, rec,
         pnl1, pnl2, combo, oc1, oc2, add_tried, range_pct, ts_lbl, scores,
         n1=0, risk1=0):
    return {
        "date":           ds,
        "score":          score,
        "recommendation": rec,
        "vix":            round(vix, 2) if vix else None,
        "rv":             round(rv, 2) if rv else None,
        "vp_ratio":       round(vp, 3) if vp else None,
        "rv_slope":       rv_sl,
        "vwap_slope":     vwap,
        "ts_label":       ts_lbl,
        "range_pct":      round(range_pct, 3) if range_pct else None,
        "n_spreads_p1":   n1,
        "risk_deployed_p1": risk1,
        "pnl_p1_dollars": pnl1,
        "outcome_p1":     oc1,
        "add_tried":      add_tried,
        "pnl_p2_dollars": pnl2,
        "outcome_p2":     str(oc2),
        "combined_pnl":   combo,
        "budget_used_pct": round((risk1 / DAILY_RISK_BUDGET * 100), 1) if risk1 else 0,
        "score_vol":      scores.get("vol_premium", 0),
        "score_regime":   scores.get("regime", 0),
        "score_ts":       scores.get("term_structure", 0),
    }


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary(results):
    df  = pd.DataFrame(results)
    if df.empty:
        print("No results."); return

    go  = df[df["recommendation"] == "GO"]
    env = df[df["recommendation"] == "ENV_BLOCK"]
    rrs = df[df["recommendation"] == "RR_SKIP"]

    print(f"\n{'=' * 65}")
    print(f"  IRON BUTTERFLY BACKTEST — {len(df)} days scanned")
    print(f"{'=' * 65}")
    print(f"  GO signals:      {len(go)}")
    print(f"  Env blocked:     {len(env)}  (slopes bad at entry)")
    print(f"  RR skipped:      {len(rrs)}  (insufficient premium)")
    print(f"  No trade / Wait: {len(df) - len(go) - len(env) - len(rrs)}")

    if go.empty:
        print("  No GO trades to analyse."); return

    wins  = (go["pnl_p1_dollars"] > 0).sum()
    loss  = (go["pnl_p1_dollars"] < 0).sum()
    wr    = wins / len(go) * 100
    avg   = go["pnl_p1_dollars"].mean()
    tot   = go["pnl_p1_dollars"].sum()
    aw    = go[go["pnl_p1_dollars"] > 0]["pnl_p1_dollars"].mean() if wins else 0
    al    = go[go["pnl_p1_dollars"] < 0]["pnl_p1_dollars"].mean() if loss else 0
    pf    = abs(aw * wins / (al * loss)) if loss and al else float("inf")

    print(f"\n  ── Position 1 (all GO trades) ──────────────────")
    print(f"  Trades:         {len(go)}")
    print(f"  Win rate:       {wr:.1f}%  ({wins}W / {loss}L)")
    print(f"  Avg P&L:        ${avg:+.3f}/spread")
    print(f"  Total P&L:      ${tot:+.3f}")
    print(f"  Profit factor:  {pf:.2f}")
    print(f"  Avg winner:     ${aw:+.3f}   Avg loser: ${al:+.3f}")

    print(f"\n  ── Exit breakdown ──────────────────────────────")
    for oc, grp in go.groupby("outcome_p1"):
        print(f"  {oc:<22} {len(grp):>3} trades  Avg ${grp['pnl_p1_dollars'].mean():+.3f}")

    adds = go[go["add_tried"] == True]
    blocked = adds[adds["outcome_p2"].str.startswith("ADD_BLOCKED")]
    traded  = adds[~adds["outcome_p2"].str.startswith("ADD_BLOCKED") &
                   ~adds["outcome_p2"].str.startswith("ADD_RR")]
    print(f"\n  ── Ladder adds ─────────────────────────────────")
    print(f"  Days with drift >{LADDER_DRIFT}pts:  {len(adds)}")
    print(f"  Blocked by slopes:      {len(blocked)}")
    print(f"  Actually traded:        {len(traded)}")

    if not traded.empty:
        a_wr  = (traded["pnl_p2_dollars"] > 0).mean() * 100
        a_avg = traded["pnl_p2_dollars"].mean()
        a_tot = traded["pnl_p2_dollars"].sum()
        c_avg = traded["combined_pnl"].mean()
        p1_avg_same = traded["pnl_p1_dollars"].mean()
        uplift = c_avg - p1_avg_same
        print(f"  Add win rate:           {a_wr:.1f}%")
        print(f"  Avg add P&L:            ${a_avg:+.3f}  (at {ADD_SIZE_SCALE:.0%} scale)")
        print(f"  Total add P&L:          ${a_tot:+.3f}")
        print(f"  Combined avg (P1+P2):   ${c_avg:+.3f}  vs P1-only ${p1_avg_same:+.3f}  → uplift ${uplift:+.3f}")

    print(f"\n  ── Performance by RV slope at entry ────────────")
    for lbl in ["FALLING", "STABLE", "RISING", "UNKNOWN"]:
        sub = go[go["rv_slope"] == lbl]
        if sub.empty: continue
        wr_s = (sub["pnl_p1_dollars"] > 0).mean() * 100
        print(f"  RV {lbl:<8}  {len(sub):>3} trades  WR {wr_s:.0f}%  Avg ${sub['pnl_p1_dollars'].mean():+.3f}")

    print(f"\n  ── Performance by VP ratio bucket ──────────────")
    for lo, hi, label in [(0, 1.2, "<1.2x"), (1.2, 1.5, "1.2–1.5x"), (1.5, 99, "≥1.5x")]:
        sub = go[(go["vp_ratio"] >= lo) & (go["vp_ratio"] < hi)]
        if sub.empty: continue
        wr_s = (sub["pnl_p1_dollars"] > 0).mean() * 100
        print(f"  VP {label:<10}  {len(sub):>3} trades  WR {wr_s:.0f}%  Avg ${sub['pnl_p1_dollars'].mean():+.3f}")

    print(f"\n  Results → {OUTPUT_FILE}")
    print(f"{'=' * 65}\n")


def save_csv(results):
    if not results: return
    with open(OUTPUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=results[0].keys())
        w.writeheader(); w.writerows(results)


if __name__ == "__main__":
    results = run_backtest()
    print_summary(results)
    save_csv(results)
