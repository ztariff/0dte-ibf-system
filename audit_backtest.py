"""
Audit script: Cross-check v5 backtest fills against raw Polygon data.
Picks 5 representative days and verifies entry price, exit price,
wing stop logic, target math, and spread/dollar calculations.

Usage: python3 audit_backtest.py YOUR_POLYGON_KEY
"""

import sys, requests, time, math
import pandas as pd
import pytz

API_KEY = sys.argv[1] if len(sys.argv) > 1 else input("Polygon API key: ").strip()
ET = pytz.timezone("America/New_York")
WING_WIDTH = 40
TARGET_PCT = 0.50
SPX_MULTIPLIER = 100
SLIPPAGE_PER_SPR = 1.00
DAILY_RISK_BUDGET = 100_000
TRANCHE_RISK = DAILY_RISK_BUDGET / 3

def snap(x, step=5):
    return round(round(x / step) * step, 0)

def get_bars(ticker, day_str):
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{day_str}/{day_str}"
    r = requests.get(url, params={"adjusted":"true","sort":"asc","limit":500,"apiKey":API_KEY}, timeout=15)
    if r.status_code == 200:
        rows = r.json().get("results", [])
        if not rows: return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
        return df.sort_values("t").reset_index(drop=True)
    print(f"  !! get_bars({ticker}, {day_str}) returned {r.status_code}")
    return pd.DataFrame()

def get_opt_bars(exp_date, strike, cp, day_str):
    yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
    strike_int = int(round(strike * 1000))
    ticker = f"O:SPXW{yy}{mm}{dd}{cp}{strike_int:08d}"
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{day_str}/{day_str}"
    r = requests.get(url, params={"adjusted":"true","sort":"asc","limit":500,"apiKey":API_KEY}, timeout=15)
    if r.status_code == 200:
        rows = r.json().get("results", [])
        if not rows:
            # Try SPX fallback
            ticker2 = f"O:SPX{yy}{mm}{dd}{cp}{strike_int:08d}"
            r2 = requests.get(f"https://api.polygon.io/v2/aggs/ticker/{ticker2}/range/1/minute/{day_str}/{day_str}",
                             params={"adjusted":"true","sort":"asc","limit":500,"apiKey":API_KEY}, timeout=15)
            if r2.status_code == 200:
                rows = r2.json().get("results", [])
                if rows:
                    ticker = ticker2
            if not rows:
                print(f"  !! No data for {ticker}")
                return pd.DataFrame(), ticker
        df = pd.DataFrame(rows)
        df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
        return df.sort_values("t").reset_index(drop=True), ticker
    print(f"  !! get_opt_bars({ticker}) returned {r.status_code}")
    return pd.DataFrame(), ticker

def price_at(df, target_t):
    if df.empty: return None
    near = df[df["t"] <= target_t]
    if not near.empty: return near.iloc[-1]["c"]
    return df.iloc[0]["c"]

def ibf_value(ap_df, ac_df, wp_df, wc_df, t):
    ap = price_at(ap_df, t)
    ac = price_at(ac_df, t)
    wp = price_at(wp_df, t)
    wc = price_at(wc_df, t)
    if any(v is None for v in [ap, ac, wp, wc]):
        return None, ap, ac, wp, wc
    return ap + ac - wp - wc, ap, ac, wp, wc


# ── Audit days ──────────────────────────────────────────────────────────────
results = pd.read_csv("/sessions/tender-laughing-feynman/mnt/traderCowork/backtest_v5_livequotes_results.csv")
go = results[results["recommendation"] == "GO"]

audit_days = [
    ("TARGET win",    go[go["outcome_p1"] == "TARGET"].iloc[0]["date"]),
    ("WING_STOP",     go[go["outcome_p1"] == "WING_STOP"].iloc[0]["date"]),
    ("TIME_STOP",     go[go["outcome_p1"] == "TIME_STOP"].iloc[0]["date"]),
    ("Biggest win",   go.nlargest(1, "combined_pnl").iloc[0]["date"]),
    ("Biggest loss",  go.nsmallest(1, "combined_pnl").iloc[0]["date"]),
]

for label, ds in audit_days:
    row = go[go["date"] == ds].iloc[0]
    print(f"\n{'='*80}")
    print(f"  AUDIT: {label} — {ds}")
    print(f"  Backtest says: P1=${row['pnl_p1_dollars']:+,.0f} ({row['outcome_p1']})")
    print(f"  N spreads: {row['n_spreads_p1']}, Risk deployed: ${row['risk_deployed_p1']:,.0f}")
    print(f"{'='*80}")

    time.sleep(1)  # rate limit

    # Get SPX bars
    spx = get_bars("I:SPX", ds)
    if spx.empty:
        print("  !! No SPX data"); continue

    entry_t = spx["t"].iloc[0].replace(hour=10, minute=0, second=0, microsecond=0)
    entry_bar = spx[spx["t"] >= entry_t]
    if entry_bar.empty:
        print("  !! No entry bar"); continue
    spx_at_entry = entry_bar.iloc[0]["o"]
    print(f"\n  SPX at 10:00am open: {spx_at_entry}")

    # Compute strikes
    atm = snap(spx_at_entry)
    wp = snap(spx_at_entry - WING_WIDTH)
    wc = snap(spx_at_entry + WING_WIDTH)
    print(f"  Strikes: put wing={wp}, ATM={atm}, call wing={wc}")

    # Parse date for option ticker
    from datetime import datetime
    exp = datetime.strptime(ds, "%Y-%m-%d").date()

    # Fetch option bars
    time.sleep(1)
    ap_df, ap_tk = get_opt_bars(exp, atm, "P", ds)
    time.sleep(1)
    ac_df, ac_tk = get_opt_bars(exp, atm, "C", ds)
    time.sleep(1)
    wp_df, wp_tk = get_opt_bars(exp, wp, "P", ds)
    time.sleep(1)
    wc_df, wc_tk = get_opt_bars(exp, wc, "C", ds)

    print(f"\n  Option tickers:")
    print(f"    ATM Put:  {ap_tk}  ({len(ap_df)} bars)")
    print(f"    ATM Call: {ac_tk}  ({len(ac_df)} bars)")
    print(f"    Wing Put: {wp_tk}  ({len(wp_df)} bars)")
    print(f"    Wing Call:{wc_tk}  ({len(wc_df)} bars)")

    # ── CHECK 1: Entry credit ──────────────────────────────────────────────
    credit_entry, ap_e, ac_e, wp_e, wc_e = ibf_value(ap_df, ac_df, wp_df, wc_df, entry_t)
    print(f"\n  ── ENTRY at 10:00am ──")
    print(f"    ATM Put  @ 10am: {ap_e}")
    print(f"    ATM Call @ 10am: {ac_e}")
    print(f"    Wing Put @ 10am: {wp_e}")
    print(f"    Wing Call@ 10am: {wc_e}")
    print(f"    Credit (sell straddle - buy wings): {credit_entry:.3f}" if credit_entry else "    !! Missing data")

    if credit_entry:
        max_loss = (atm - wp) - credit_entry
        target = credit_entry * TARGET_PCT
        rr = credit_entry / max_loss if max_loss > 0 else 0
        print(f"    Max loss per spread: {max_loss:.3f}")
        print(f"    50% target: {target:.3f}")
        print(f"    R:R ratio: {rr:.3f}")

        # Sizing check
        ml_dollars = max_loss * SPX_MULTIPLIER
        n_spreads = max(1, int(TRANCHE_RISK / ml_dollars))
        risk_deployed = n_spreads * ml_dollars
        print(f"\n    Sizing: {n_spreads} spreads × ${ml_dollars:,.0f} max loss = ${risk_deployed:,.0f} risk")
        print(f"    Backtest used: {int(row['n_spreads_p1'])} spreads, ${row['risk_deployed_p1']:,.0f} risk")
        if n_spreads != int(row['n_spreads_p1']):
            print(f"    !! MISMATCH in spread count!")
        else:
            print(f"    ✓ Spread count matches")

    # ── CHECK 2: Walk bars to find exit ────────────────────────────────────
    print(f"\n  ── POSITION WALK ──")
    if credit_entry:
        bars_after_entry = spx[spx["t"] >= entry_t]
        exit_found = False
        for _, bar in bars_after_entry.iterrows():
            bar_t = bar["t"]
            s_now = bar["c"]

            # Check wing stop
            if s_now <= wp or s_now >= wc:
                val_at_exit, _, _, _, _ = ibf_value(ap_df, ac_df, wp_df, wc_df, bar_t)
                pnl_per_spread = credit_entry - val_at_exit if val_at_exit else None
                print(f"    WING STOP at {bar_t.strftime('%H:%M')} — SPX={s_now:.1f} (wing={wp if s_now<=wp else wc})")
                print(f"    IBF value at exit: {val_at_exit:.3f}" if val_at_exit else "    !! Missing exit data")
                if pnl_per_spread is not None:
                    print(f"    P&L per spread: {pnl_per_spread:.3f}")
                    total_pnl = pnl_per_spread * n_spreads * SPX_MULTIPLIER - n_spreads * SLIPPAGE_PER_SPR * SPX_MULTIPLIER
                    print(f"    Total P&L: ${total_pnl:+,.0f}  (backtest: ${row['pnl_p1_dollars']:+,.0f})")
                exit_found = True
                break

            # Check time stop
            if bar_t.hour > 15 or (bar_t.hour == 15 and bar_t.minute >= 30):
                val_at_exit, _, _, _, _ = ibf_value(ap_df, ac_df, wp_df, wc_df, bar_t)
                pnl_per_spread = credit_entry - val_at_exit if val_at_exit else None
                print(f"    TIME STOP at {bar_t.strftime('%H:%M')} — SPX={s_now:.1f}")
                print(f"    IBF value at exit: {val_at_exit:.3f}" if val_at_exit else "    !! Missing exit data")
                if pnl_per_spread is not None:
                    print(f"    P&L per spread: {pnl_per_spread:.3f}")
                    total_pnl = pnl_per_spread * n_spreads * SPX_MULTIPLIER - n_spreads * SLIPPAGE_PER_SPR * SPX_MULTIPLIER
                    print(f"    Total P&L: ${total_pnl:+,.0f}  (backtest: ${row['pnl_p1_dollars']:+,.0f})")
                exit_found = True
                break

            # Check 50% target (using LIVE option prices, not BS)
            val_now, _, _, _, _ = ibf_value(ap_df, ac_df, wp_df, wc_df, bar_t)
            if val_now is not None:
                pnl_now = credit_entry - val_now
                if pnl_now >= target:
                    print(f"    TARGET at {bar_t.strftime('%H:%M')} — SPX={s_now:.1f}")
                    print(f"    IBF value: {val_now:.3f}, P&L: {pnl_now:.3f} >= target {target:.3f}")
                    total_pnl = pnl_now * n_spreads * SPX_MULTIPLIER - n_spreads * SLIPPAGE_PER_SPR * SPX_MULTIPLIER
                    print(f"    Total P&L: ${total_pnl:+,.0f}  (backtest: ${row['pnl_p1_dollars']:+,.0f})")
                    exit_found = True
                    break

        if not exit_found:
            print(f"    !! No exit found walking bars — check logic")

        # Also note: the backtest uses CALIBRATED BS to detect exit, then fetches live price
        # So exit timing may differ from a pure live-price walk
        print(f"\n    NOTE: Backtest uses calibrated BS for exit DETECTION, then live price for P&L.")
        print(f"    If the audit walk (pure live) exits at a different time, that's the BS detection gap.")

    print(f"\n  Backtest outcome: {row['outcome_p1']}, P1=${row['pnl_p1_dollars']:+,.0f}")

PYEOF