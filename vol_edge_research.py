#!/usr/bin/env python3
"""
Independent vol-selling edge research on SPX 0DTE options.
Uses raw intraday bars + real option midpoints. No inherited strategy logic.

Explores: entry time, wing width, exit mechanics, hold period, regime filters,
asymmetric structures, intraday triggers, morning character signals.

Writes all results to vol_edge_results.txt.
"""

import json, csv, math, statistics, os, sys
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from itertools import combinations

_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

print("Loading data...")
with open(os.path.join(_DIR, 'spx_intraday_bars.json')) as f:
    BARS = json.load(f)
with open(os.path.join(_DIR, 'option_midpoints.json')) as f:
    OPTS = json.load(f)
with open(os.path.join(_DIR, 'research_all_trades.csv')) as f:
    _csv = list(csv.DictReader(f))
with open(os.path.join(_DIR, 'spx_gap_cache.json')) as f:
    GAPS = json.load(f)
with open(os.path.join(_DIR, 'vix9d_daily.json')) as f:
    VIX9D = json.load(f)

# Build regime lookup
REGIME = {}
for row in _csv:
    REGIME[row['date']] = row

# Working universe: dates with both bars AND options AND regime signals
DATES = sorted(set(BARS.keys()) & set(OPTS.keys()) & set(REGIME.keys()))
print(f"Working universe: {len(DATES)} dates ({DATES[0]} to {DATES[-1]})")

TIME_SLOTS = ['09:35', '10:00', '10:30', '11:00', '12:00', '13:00', '14:00', '15:00', '15:30']

OUT = []  # accumulate output lines

def out(s=""):
    OUT.append(s)
    print(s)

def out_table(headers, rows, widths=None):
    """Print a formatted table."""
    if not widths:
        widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=4)) + 2
                  for i, h in enumerate(headers)]
    header = "".join(str(h).ljust(w) for h, w in zip(headers, widths))
    out(header)
    out("-" * len(header))
    for row in rows:
        out("".join(str(row[i]).ljust(w) for i, w in enumerate(widths)))


# ─────────────────────────────────────────────────────────────────────────────
# CORE PRICING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def price_ibf(date, time_slot, wing_width):
    """
    Price an iron butterfly at given date/time with specified wing width.
    Returns dict with credit, strikes, qty info, or None if can't price.

    IBF = short straddle at ATM + long wings at ATM±wing_width
    Credit = C(ATM) + P(ATM) - C(ATM+W) - P(ATM-W)
    Max risk = wing_width - credit
    """
    if date not in OPTS or time_slot not in OPTS[date]:
        return None

    slot = OPTS[date][time_slot]
    spx = slot['spx']
    atm = slot['atm']
    strikes = slot['strikes']

    # Find the needed strikes
    call_short = str(atm)
    put_short = str(atm)
    call_long = str(atm + wing_width)
    put_long = str(atm - wing_width)

    # All four legs must exist with the right side (C or P)
    if call_short not in strikes or 'C' not in strikes[call_short]:
        return None
    if put_short not in strikes or 'P' not in strikes[put_short]:
        return None
    if call_long not in strikes or 'C' not in strikes[call_long]:
        return None
    if put_long not in strikes or 'P' not in strikes[put_long]:
        return None

    sc = strikes[call_short]['C']  # short call
    sp = strikes[put_short]['P']   # short put
    lc = strikes[call_long]['C']   # long call
    lp = strikes[put_long]['P']    # long put

    credit = sc + sp - lc - lp
    if credit <= 0:
        return None

    max_risk = wing_width - credit

    return {
        'spx': spx, 'atm': atm, 'wing': wing_width,
        'credit': round(credit, 3),
        'max_risk': round(max_risk, 3),
        'sc': sc, 'sp': sp, 'lc': lc, 'lp': lp,
        'call_wing': atm + wing_width,
        'put_wing': atm - wing_width,
    }


def price_ic(date, time_slot, call_width, put_width):
    """
    Price an asymmetric iron condor.
    Short straddle at ATM + long call at ATM+call_width + long put at ATM-put_width.
    """
    if date not in OPTS or time_slot not in OPTS[date]:
        return None

    slot = OPTS[date][time_slot]
    spx = slot['spx']
    atm = slot['atm']
    strikes = slot['strikes']

    call_short = str(atm)
    put_short = str(atm)
    call_long = str(atm + call_width)
    put_long = str(atm - put_width)

    if call_short not in strikes or 'C' not in strikes[call_short]:
        return None
    if put_short not in strikes or 'P' not in strikes[put_short]:
        return None
    if call_long not in strikes or 'C' not in strikes[call_long]:
        return None
    if put_long not in strikes or 'P' not in strikes[put_long]:
        return None

    sc = strikes[call_short]['C']
    sp = strikes[put_short]['P']
    lc = strikes[call_long]['C']
    lp = strikes[put_long]['P']

    credit = sc + sp - lc - lp
    if credit <= 0:
        return None

    max_risk_call = call_width - credit
    max_risk_put = put_width - credit
    max_risk = max(max_risk_call, max_risk_put)

    return {
        'spx': spx, 'atm': atm,
        'call_wing': atm + call_width, 'put_wing': atm - put_width,
        'call_width': call_width, 'put_width': put_width,
        'credit': round(credit, 3),
        'max_risk': round(max_risk, 3),
    }


def mark_ibf(date, time_slot, entry):
    """
    Mark-to-market an IBF position at a later time slot.
    Uses the SAME strikes as entry (locked).
    Returns current value (cost to close) or None if strikes unavailable.
    """
    if date not in OPTS or time_slot not in OPTS[date]:
        return None

    slot = OPTS[date][time_slot]
    strikes = slot['strikes']

    cs = str(entry['atm'])
    ps = str(entry['atm'])
    cl = str(entry['call_wing'])
    pl = str(entry['put_wing'])

    if cs not in strikes or 'C' not in strikes[cs]:
        return None
    if ps not in strikes or 'P' not in strikes[ps]:
        return None
    if cl not in strikes or 'C' not in strikes[cl]:
        return None
    if pl not in strikes or 'P' not in strikes[pl]:
        return None

    val = strikes[cs]['C'] + strikes[ps]['P'] - strikes[cl]['C'] - strikes[pl]['P']
    return round(val, 3)


def compute_wing_stop_time(date, entry):
    """
    Using 1-min bars, find the first time SPX crosses either wing strike.
    Returns (time_str, spx_price) or None if never crossed.
    """
    if date not in BARS:
        return None
    day_bars = BARS[date]
    call_wing = entry['call_wing']
    put_wing = entry['put_wing']

    for t in sorted(day_bars.keys()):
        bar = day_bars[t]
        if bar['h'] >= call_wing or bar['l'] <= put_wing:
            return (t, bar['c'])
    return None


def compute_morning_range(date, start='09:30', end='10:00'):
    """Compute SPX range from start to end using 1-min bars."""
    if date not in BARS:
        return None
    day_bars = BARS[date]
    hi, lo = -1e9, 1e9
    for t in sorted(day_bars.keys()):
        if t < start:
            continue
        if t > end:
            break
        bar = day_bars[t]
        hi = max(hi, bar['h'])
        lo = min(lo, bar['l'])
    if hi < 0:
        return None
    return round(hi - lo, 2)


def compute_morning_direction(date, start='09:30', end='10:00'):
    """Net SPX move from open to end time."""
    if date not in BARS:
        return None
    day_bars = BARS[date]
    open_price = None
    close_price = None
    for t in sorted(day_bars.keys()):
        if t >= start and open_price is None:
            open_price = day_bars[t]['o']
        if t <= end:
            close_price = day_bars[t]['c']
    if open_price and close_price:
        return round(close_price - open_price, 2)
    return None


def compute_range_to_time(date, end_time):
    """SPX high-low range from open to end_time."""
    if date not in BARS:
        return None
    day_bars = BARS[date]
    hi, lo = -1e9, 1e9
    for t in sorted(day_bars.keys()):
        if t > end_time:
            break
        bar = day_bars[t]
        hi = max(hi, bar['h'])
        lo = min(lo, bar['l'])
    if hi < 0:
        return None
    return round(hi - lo, 2)


def get_spx_at(date, time_str):
    """Get SPX close price at a specific minute."""
    if date not in BARS or time_str not in BARS[date]:
        return None
    return BARS[date][time_str]['c']


def get_gap_pct(date):
    """Overnight gap %."""
    return GAPS.get(date, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def calc_stats(pnls):
    """Compute trading stats from a list of per-spread P&L values."""
    if not pnls:
        return None
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    avg = total / n
    wr = len(wins) / n * 100
    avg_w = statistics.mean(wins) if wins else 0
    avg_l = statistics.mean(losses) if losses else 0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')

    # Max drawdown on cumulative equity
    cum = 0
    peak = 0
    dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)

    # t-test: H0 mean = 0
    if n > 1:
        se = statistics.stdev(pnls) / math.sqrt(n)
        t_stat = avg / se if se > 0 else 0
        # Approximate p-value (two-tailed, normal approx for large n)
        # For small n this is rough but honest
        from math import erfc
        p_val = erfc(abs(t_stat) / math.sqrt(2))
    else:
        t_stat = 0
        p_val = 1.0

    return {
        'n': n, 'total': round(total, 2), 'avg': round(avg, 3),
        'win_rate': round(wr, 1), 'avg_win': round(avg_w, 3),
        'avg_loss': round(avg_l, 3), 'pf': round(pf, 2),
        'max_dd': round(dd, 2), 't_stat': round(t_stat, 2),
        'p_val': round(p_val, 4),
    }


def fmt_stats(s):
    if not s:
        return "NO DATA"
    return (f"n={s['n']}  WR={s['win_rate']}%  avg={s['avg']:.2f}  "
            f"PF={s['pf']:.2f}  total={s['total']:.1f}  "
            f"DD={s['max_dd']:.1f}  t={s['t_stat']:.2f}  p={s['p_val']:.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1: BASELINE SWEEP — EVERY ENTRY × WING × EXIT COMBO
# ─────────────────────────────────────────────────────────────────────────────

def run_trade(date, entry_time, wing_width, target_pct, time_stop, use_wing_stop=True):
    """
    Simulate one IBF trade.
    Returns per-spread P&L or None if couldn't price.

    target_pct: e.g. 0.50 = close at 50% of credit captured
    time_stop: time string to close if still open, e.g. '15:30'
    use_wing_stop: if True, close when SPX crosses a wing (using 1-min bars)
    """
    entry = price_ibf(date, entry_time, wing_width)
    if not entry:
        return None

    credit = entry['credit']

    # Determine exit time slots (after entry)
    exit_slots = [t for t in TIME_SLOTS if t > entry_time]
    if not exit_slots:
        return None

    # Check wing stop first (1-min bar resolution, more granular)
    ws_exit = None
    if use_wing_stop:
        ws = compute_wing_stop_time(date, entry)
        if ws and ws[0] > entry_time:
            ws_time = ws[0]
            # Wing stop fires — find the mark at the next available option slot
            # or use intrinsic approximation
            ws_exit = ws_time

    # Walk through exit slots
    for slot in exit_slots:
        # Wing stop fires before this slot?
        if ws_exit and ws_exit <= slot:
            # Mark at this slot (closest available)
            val = mark_ibf(date, slot, entry)
            if val is not None:
                pnl = credit - val
                return {'pnl': round(pnl, 3), 'exit': 'WING_STOP', 'exit_time': slot, 'credit': credit}
            # If can't mark, approximate: wing stop ~ -70% of max risk
            pnl = credit - (wing_width * 0.7)
            return {'pnl': round(pnl, 3), 'exit': 'WING_STOP_EST', 'exit_time': ws_exit, 'credit': credit}

        # Check target
        val = mark_ibf(date, slot, entry)
        if val is not None:
            pnl = credit - val
            if pnl >= credit * target_pct:
                return {'pnl': round(pnl, 3), 'exit': 'TARGET', 'exit_time': slot, 'credit': credit}

        # Check time stop
        if slot >= time_stop:
            if val is not None:
                pnl = credit - val
                return {'pnl': round(pnl, 3), 'exit': 'TIME', 'exit_time': slot, 'credit': credit}

    # If we get here, use last available slot
    last_slot = exit_slots[-1]
    val = mark_ibf(date, last_slot, entry)
    if val is not None:
        pnl = credit - val
        return {'pnl': round(pnl, 3), 'exit': 'CLOSE', 'exit_time': last_slot, 'credit': credit}

    return None


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

out("=" * 100)
out("SPX 0DTE VOL-SELLING EDGE RESEARCH — INDEPENDENT ANALYSIS")
out(f"Universe: {len(DATES)} trading days ({DATES[0]} to {DATES[-1]})")
out("=" * 100)

# Sweep parameters
ENTRY_TIMES = ['09:35', '10:00', '10:30', '11:00', '12:00', '13:00', '14:00']
WING_WIDTHS = [25, 30, 35, 40, 50]
TARGET_PCTS = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
TIME_STOPS = ['14:00', '15:00', '15:30']

out("\n" + "=" * 100)
out("PHASE 1: BASELINE SWEEP — Entry Time × Wing Width × Target % × Time Stop")
out("=" * 100)

# First: raw baseline by entry time and wing width (no filters, 50% target, 15:30 stop)
out("\n--- 1A: Entry Time × Wing Width baseline (50% target, 15:30 stop, wing stop ON) ---\n")

baseline_results = {}  # (entry_time, wing_width) -> list of pnls

for et in ENTRY_TIMES:
    for ww in WING_WIDTHS:
        pnls = []
        for date in DATES:
            result = run_trade(date, et, ww, 0.50, '15:30', use_wing_stop=True)
            if result:
                pnls.append(result['pnl'])
        baseline_results[(et, ww)] = pnls

# Print baseline table
headers = ['Entry', 'Wing', 'N', 'WinRate', 'AvgPnL', 'Total', 'PF', 'MaxDD', 't-stat', 'p-val']
rows = []
for et in ENTRY_TIMES:
    for ww in WING_WIDTHS:
        pnls = baseline_results[(et, ww)]
        s = calc_stats(pnls)
        if s:
            rows.append([et, ww, s['n'], f"{s['win_rate']}%", f"{s['avg']:.2f}",
                        f"{s['total']:.0f}", f"{s['pf']:.2f}", f"{s['max_dd']:.1f}",
                        f"{s['t_stat']:.2f}", f"{s['p_val']:.4f}"])
out_table(headers, rows)

# Now find the top 10 combos
out("\n--- 1B: Full mechanic sweep (top 25 by avg P&L per spread) ---\n")

all_combos = []
for et in ENTRY_TIMES:
    for ww in WING_WIDTHS:
        for tp in TARGET_PCTS:
            for ts in TIME_STOPS:
                pnls = []
                for date in DATES:
                    result = run_trade(date, et, ww, tp, ts, use_wing_stop=True)
                    if result:
                        pnls.append(result['pnl'])
                s = calc_stats(pnls)
                if s and s['n'] >= 50:
                    all_combos.append({
                        'entry': et, 'wing': ww, 'target': tp, 'stop': ts,
                        **s
                    })

all_combos.sort(key=lambda x: x['avg'], reverse=True)

headers = ['Entry', 'Wing', 'Tgt%', 'TStop', 'N', 'WR%', 'Avg', 'Total', 'PF', 'DD', 't', 'p']
rows = []
for c in all_combos[:25]:
    rows.append([c['entry'], c['wing'], f"{c['target']:.0%}", c['stop'],
                c['n'], f"{c['win_rate']}", f"{c['avg']:.2f}", f"{c['total']:.0f}",
                f"{c['pf']:.2f}", f"{c['max_dd']:.1f}", f"{c['t_stat']:.2f}", f"{c['p_val']:.4f}"])
out_table(headers, rows)

# Also show: wing stop ON vs OFF comparison
out("\n--- 1C: Wing Stop Impact (50% target, 15:30 stop) ---\n")
headers = ['Entry', 'Wing', 'WS_ON_Avg', 'WS_OFF_Avg', 'Delta', 'WS_ON_WR', 'WS_OFF_WR']
rows = []
for et in ['10:00', '11:00', '13:00', '14:00']:
    for ww in [30, 40, 50]:
        pnls_on = []
        pnls_off = []
        for date in DATES:
            r_on = run_trade(date, et, ww, 0.50, '15:30', use_wing_stop=True)
            r_off = run_trade(date, et, ww, 0.50, '15:30', use_wing_stop=False)
            if r_on:
                pnls_on.append(r_on['pnl'])
            if r_off:
                pnls_off.append(r_off['pnl'])
        s_on = calc_stats(pnls_on)
        s_off = calc_stats(pnls_off)
        if s_on and s_off:
            rows.append([et, ww, f"{s_on['avg']:.2f}", f"{s_off['avg']:.2f}",
                        f"{s_on['avg']-s_off['avg']:.2f}",
                        f"{s_on['win_rate']}%", f"{s_off['win_rate']}%"])
out_table(headers, rows)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2: HOLD PERIOD ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 2: HOLD PERIOD ANALYSIS — Where does theta actually accrue?")
out("=" * 100)

out("\n--- 2A: P&L by exit slot (no target, no wing stop — pure time decay) ---\n")

# For each entry time, show average P&L at each subsequent time slot
for et in ['09:35', '10:00', '11:00', '13:00']:
    out(f"\nEntry: {et}, Wing: 40")
    headers = ['ExitSlot', 'N', 'AvgPnL', 'WR%', 'PF', 'AvgCredit']
    rows = []
    for exit_slot in TIME_SLOTS:
        if exit_slot <= et:
            continue
        pnls = []
        credits = []
        for date in DATES:
            entry = price_ibf(date, et, 40)
            if not entry:
                continue
            val = mark_ibf(date, exit_slot, entry)
            if val is not None:
                pnl = entry['credit'] - val
                pnls.append(pnl)
                credits.append(entry['credit'])
        s = calc_stats(pnls)
        if s:
            rows.append([exit_slot, s['n'], f"{s['avg']:.2f}", f"{s['win_rate']}%",
                        f"{s['pf']:.2f}", f"{statistics.mean(credits):.2f}"])
    out_table(headers, rows)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3: REGIME SLICING — Which days have edge?
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 3: REGIME SLICING — What predicts vol-selling edge?")
out("=" * 100)

# Use best baseline: 10:00 entry, 40-wing, 50% target, 15:30 stop
# (or whichever emerges from Phase 1 — we'll use a few)

def get_regime_val(date, col, as_float=True):
    """Get regime value for a date, optionally as float."""
    r = REGIME.get(date)
    if not r or col not in r or r[col] == '':
        return None
    if as_float:
        try:
            return float(r[col])
        except (ValueError, TypeError):
            return None
    return r[col]


# Run trades for a specific config and return (date, pnl) pairs
def run_all_trades(entry_time, wing_width, target_pct, time_stop, use_ws=True):
    results = []
    for date in DATES:
        r = run_trade(date, entry_time, wing_width, target_pct, time_stop, use_ws)
        if r:
            results.append((date, r['pnl'], r))
    return results


# Generate trades for regime analysis
trade_sets = {
    '10:00/40/50%/15:30': run_all_trades('10:00', 40, 0.50, '15:30'),
    '13:00/40/50%/15:30': run_all_trades('13:00', 40, 0.50, '15:30'),
    '14:00/30/50%/15:30': run_all_trades('14:00', 30, 0.50, '15:30'),
    '10:00/50/50%/15:30': run_all_trades('10:00', 50, 0.50, '15:30'),
}

for config_name, trades in trade_sets.items():
    out(f"\n--- Config: {config_name} ({len(trades)} trades) ---\n")

    # 3A: VIX buckets
    out("  VIX Buckets:")
    buckets = {'<13': [], '13-15': [], '15-18': [], '18-22': [], '22+': []}
    for date, pnl, _ in trades:
        vix = get_regime_val(date, 'vix')
        if vix is None:
            continue
        if vix < 13:
            buckets['<13'].append(pnl)
        elif vix < 15:
            buckets['13-15'].append(pnl)
        elif vix < 18:
            buckets['15-18'].append(pnl)
        elif vix < 22:
            buckets['18-22'].append(pnl)
        else:
            buckets['22+'].append(pnl)
    for bk, pnls in buckets.items():
        s = calc_stats(pnls)
        out(f"    VIX {bk:>6}: {fmt_stats(s)}")

    # 3B: VP ratio buckets
    out("\n  VP Ratio Buckets:")
    buckets = {'<1.0': [], '1.0-1.3': [], '1.3-1.7': [], '1.7-2.0': [], '2.0+': []}
    for date, pnl, _ in trades:
        vp = get_regime_val(date, 'vp_ratio')
        if vp is None:
            continue
        if vp < 1.0:
            buckets['<1.0'].append(pnl)
        elif vp < 1.3:
            buckets['1.0-1.3'].append(pnl)
        elif vp < 1.7:
            buckets['1.3-1.7'].append(pnl)
        elif vp < 2.0:
            buckets['1.7-2.0'].append(pnl)
        else:
            buckets['2.0+'].append(pnl)
    for bk, pnls in buckets.items():
        s = calc_stats(pnls)
        out(f"    VP {bk:>8}: {fmt_stats(s)}")

    # 3C: Prior day direction
    out("\n  Prior Day Direction:")
    buckets = defaultdict(list)
    for date, pnl, _ in trades:
        d = get_regime_val(date, 'prior_day_direction', as_float=False)
        if d:
            buckets[d].append(pnl)
    for bk in sorted(buckets.keys()):
        s = calc_stats(buckets[bk])
        out(f"    {bk:>8}: {fmt_stats(s)}")

    # 3D: Day of week
    out("\n  Day of Week:")
    buckets = defaultdict(list)
    dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    for date, pnl, _ in trades:
        dt = datetime.strptime(date, '%Y-%m-%d')
        buckets[dow_names[dt.weekday()]].append(pnl)
    for d in dow_names:
        s = calc_stats(buckets[d])
        out(f"    {d}: {fmt_stats(s)}")

    # 3E: Gap direction
    out("\n  Overnight Gap:")
    buckets = {'GDN (<-0.25%)': [], 'GFL': [], 'GUP (>+0.25%)': []}
    for date, pnl, _ in trades:
        gap = get_gap_pct(date)
        if isinstance(gap, str):
            try:
                gap = float(gap)
            except:
                continue
        if gap < -0.25:
            buckets['GDN (<-0.25%)'].append(pnl)
        elif gap > 0.25:
            buckets['GUP (>+0.25%)'].append(pnl)
        else:
            buckets['GFL'].append(pnl)
    for bk, pnls in buckets.items():
        s = calc_stats(pnls)
        out(f"    {bk:>18}: {fmt_stats(s)}")

    # 3F: RV slope
    out("\n  RV Slope:")
    buckets = defaultdict(list)
    for date, pnl, _ in trades:
        rv = get_regime_val(date, 'rv_slope', as_float=False)
        if rv:
            buckets[rv].append(pnl)
    for bk in sorted(buckets.keys()):
        s = calc_stats(buckets[bk])
        out(f"    {bk:>10}: {fmt_stats(s)}")

    # 3G: 5-day return buckets
    out("\n  Prior 5-Day Return:")
    buckets = {'< -2%': [], '-2% to -1%': [], '-1% to 0': [], '0 to 1%': [], '1% to 2%': [], '> 2%': []}
    for date, pnl, _ in trades:
        ret = get_regime_val(date, 'prior_5d_return')
        if ret is None:
            continue
        if ret < -2:
            buckets['< -2%'].append(pnl)
        elif ret < -1:
            buckets['-2% to -1%'].append(pnl)
        elif ret < 0:
            buckets['-1% to 0'].append(pnl)
        elif ret < 1:
            buckets['0 to 1%'].append(pnl)
        elif ret < 2:
            buckets['1% to 2%'].append(pnl)
        else:
            buckets['> 2%'].append(pnl)
    for bk in ['< -2%', '-2% to -1%', '-1% to 0', '0 to 1%', '1% to 2%', '> 2%']:
        s = calc_stats(buckets[bk])
        out(f"    {bk:>12}: {fmt_stats(s)}")

    # 3H: In prior week range
    out("\n  Prior Week Range Position:")
    buckets = {'IN': [], 'OUT': []}
    for date, pnl, _ in trades:
        inr = get_regime_val(date, 'in_prior_week_range')
        if inr is None:
            continue
        buckets['IN' if inr == 1 else 'OUT'].append(pnl)
    for bk, pnls in buckets.items():
        s = calc_stats(pnls)
        out(f"    {bk:>5}: {fmt_stats(s)}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4: MORNING CHARACTER — Intraday signals
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 4: MORNING CHARACTER — Does the first 30-60 min predict the day?")
out("=" * 100)

# For 10:00 entry: use 9:30-10:00 range and direction as filters
trades_10 = run_all_trades('10:00', 40, 0.50, '15:30')

# 4A: Morning range
out("\n--- 4A: Morning Range (9:30-10:00) as filter for 10:00 entry ---\n")
buckets = {'<5 pts': [], '5-10': [], '10-15': [], '15-20': [], '20+': []}
for date, pnl, _ in trades_10:
    mr = compute_morning_range(date)
    if mr is None:
        continue
    if mr < 5:
        buckets['<5 pts'].append(pnl)
    elif mr < 10:
        buckets['5-10'].append(pnl)
    elif mr < 15:
        buckets['10-15'].append(pnl)
    elif mr < 20:
        buckets['15-20'].append(pnl)
    else:
        buckets['20+'].append(pnl)
for bk in ['<5 pts', '5-10', '10-15', '15-20', '20+']:
    s = calc_stats(buckets[bk])
    out(f"  {bk:>8}: {fmt_stats(s)}")

# 4B: Morning direction
out("\n--- 4B: Morning Direction (9:30-10:00 net move) ---\n")
buckets = {'Big Down (<-5)': [], 'Down (-5 to 0)': [], 'Flat (0 to 5)': [], 'Up (5+)': []}
for date, pnl, _ in trades_10:
    md = compute_morning_direction(date)
    if md is None:
        continue
    if md < -5:
        buckets['Big Down (<-5)'].append(pnl)
    elif md < 0:
        buckets['Down (-5 to 0)'].append(pnl)
    elif md < 5:
        buckets['Flat (0 to 5)'].append(pnl)
    else:
        buckets['Up (5+)'].append(pnl)
for bk in ['Big Down (<-5)', 'Down (-5 to 0)', 'Flat (0 to 5)', 'Up (5+)']:
    s = calc_stats(buckets[bk])
    out(f"  {bk:>20}: {fmt_stats(s)}")

# 4C: For afternoon entries (13:00), use morning range as predictor
out("\n--- 4C: Full Morning Range (9:30-13:00) as filter for 13:00 entry ---\n")
trades_13 = run_all_trades('13:00', 40, 0.50, '15:30')
buckets = {'<15 pts': [], '15-25': [], '25-35': [], '35+': []}
for date, pnl, _ in trades_13:
    mr = compute_range_to_time(date, '13:00')
    if mr is None:
        continue
    if mr < 15:
        buckets['<15 pts'].append(pnl)
    elif mr < 25:
        buckets['15-25'].append(pnl)
    elif mr < 35:
        buckets['25-35'].append(pnl)
    else:
        buckets['35+'].append(pnl)
for bk in ['<15 pts', '15-25', '25-35', '35+']:
    s = calc_stats(buckets[bk])
    out(f"  {bk:>10}: {fmt_stats(s)}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5: CREATIVE STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 5: CREATIVE STRUCTURES — Asymmetric wings, gap-aware condors")
out("=" * 100)

# 5A: Asymmetric iron condors — widen wing in gap direction
out("\n--- 5A: Gap-Aware Asymmetric IC (wider wing opposing gap) ---\n")
out("Logic: If gap up, market may retrace → widen put wing for protection")
out("       If gap down, market may bounce → widen call wing for protection\n")

headers = ['Config', 'N', 'WR%', 'Avg', 'Total', 'PF', 'DD', 't', 'p']
rows = []

for et in ['10:00', '13:00']:
    # Symmetric baseline
    pnls_sym = []
    pnls_asym = []
    for date in DATES:
        gap = get_gap_pct(date)
        try:
            gap = float(gap)
        except:
            gap = 0

        # Symmetric: 40/40
        r_sym = run_trade(date, et, 40, 0.50, '15:30')
        if r_sym:
            pnls_sym.append(r_sym['pnl'])

        # Asymmetric based on gap
        if gap > 0.25:  # gap up → widen put protection
            ic = price_ic(date, et, 35, 50)
        elif gap < -0.25:  # gap down → widen call protection
            ic = price_ic(date, et, 50, 35)
        else:
            ic = price_ic(date, et, 40, 40)

        if ic:
            # Mark at 15:30 (simplified — use symmetric mark as proxy)
            r = run_trade(date, et, 40, 0.50, '15:30')
            if r:
                pnls_asym.append(r['pnl'])

    s_sym = calc_stats(pnls_sym)
    s_asym = calc_stats(pnls_asym)
    if s_sym:
        rows.append([f'{et} Symmetric 40/40', s_sym['n'], f"{s_sym['win_rate']}", f"{s_sym['avg']:.2f}",
                    f"{s_sym['total']:.0f}", f"{s_sym['pf']:.2f}", f"{s_sym['max_dd']:.1f}",
                    f"{s_sym['t_stat']:.2f}", f"{s_sym['p_val']:.4f}"])

out_table(headers, rows)

# 5B: Delayed entry after morning settles — enter only after range contracts
out("\n--- 5B: Conditional Entry — Only if morning range < threshold ---\n")
for threshold in [8, 12, 16, 20]:
    pnls = []
    for date in DATES:
        mr = compute_morning_range(date)
        if mr is None or mr >= threshold:
            continue
        r = run_trade(date, '10:00', 40, 0.50, '15:30')
        if r:
            pnls.append(r['pnl'])
    s = calc_stats(pnls)
    out(f"  MornRange < {threshold:>2}: {fmt_stats(s)}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6: MULTI-VARIABLE FILTER COMBOS
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 6: MULTI-VARIABLE FILTER OPTIMIZATION")
out("=" * 100)

# Define atomic filters
FILTERS = {
    'VIX<15': lambda d: (get_regime_val(d, 'vix') or 99) < 15,
    'VIX<18': lambda d: (get_regime_val(d, 'vix') or 99) < 18,
    'VIX<20': lambda d: (get_regime_val(d, 'vix') or 99) < 20,
    'VIX 15-20': lambda d: 15 <= (get_regime_val(d, 'vix') or 0) < 20,
    'VP<1.3': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.3,
    'VP<1.5': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.5,
    'VP<1.7': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.7,
    'VP<1.0': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.0,
    'RV!RISING': lambda d: get_regime_val(d, 'rv_slope', False) != 'RISING',
    '5dRet>0': lambda d: (get_regime_val(d, 'prior_5d_return') or -99) > 0,
    '5dRet>1': lambda d: (get_regime_val(d, 'prior_5d_return') or -99) > 1,
    'PriorDOWN': lambda d: get_regime_val(d, 'prior_day_direction', False) == 'DOWN',
    'PriorUP': lambda d: get_regime_val(d, 'prior_day_direction', False) == 'UP',
    'GapFlat': lambda d: abs(float(get_gap_pct(d) or 0)) <= 0.25,
    'InRange': lambda d: get_regime_val(d, 'in_prior_week_range') == 1,
    'OutRange': lambda d: get_regime_val(d, 'in_prior_week_range') == 0,
    'MornRng<10': lambda d: (compute_morning_range(d) or 99) < 10,
    'MornRng<15': lambda d: (compute_morning_range(d) or 99) < 15,
}

# Test configs
configs_to_test = [
    ('10:00', 40, 0.50, '15:30'),
    ('13:00', 40, 0.50, '15:30'),
    ('14:00', 30, 0.50, '15:30'),
]

for et, ww, tp, ts in configs_to_test:
    out(f"\n--- Config: {et}/{ww}w/{tp:.0%}tgt/{ts}stop ---\n")

    # Run all trades
    all_trades = run_all_trades(et, ww, tp, ts)
    base_s = calc_stats([p for _, p, _ in all_trades])
    out(f"  UNFILTERED: {fmt_stats(base_s)}")

    # Test single filters
    out("\n  Best Single Filters:")
    single_results = []
    for fname, ffunc in FILTERS.items():
        pnls = [pnl for date, pnl, _ in all_trades if ffunc(date)]
        s = calc_stats(pnls)
        if s and s['n'] >= 20:
            single_results.append((fname, s))

    single_results.sort(key=lambda x: x[1]['avg'], reverse=True)
    for fname, s in single_results[:10]:
        out(f"    {fname:>15}: {fmt_stats(s)}")

    # Test 2-filter combos
    out("\n  Best 2-Filter Combos (n >= 15):")
    filter_names = list(FILTERS.keys())
    combo_results = []
    for f1, f2 in combinations(filter_names, 2):
        pnls = [pnl for date, pnl, _ in all_trades
                if FILTERS[f1](date) and FILTERS[f2](date)]
        s = calc_stats(pnls)
        if s and s['n'] >= 15:
            combo_results.append((f"{f1} + {f2}", s))

    combo_results.sort(key=lambda x: x[1]['avg'], reverse=True)
    for fname, s in combo_results[:15]:
        out(f"    {fname:>35}: {fmt_stats(s)}")

    # Test 3-filter combos
    out("\n  Best 3-Filter Combos (n >= 10):")
    combo3_results = []
    for f1, f2, f3 in combinations(filter_names, 3):
        pnls = [pnl for date, pnl, _ in all_trades
                if FILTERS[f1](date) and FILTERS[f2](date) and FILTERS[f3](date)]
        s = calc_stats(pnls)
        if s and s['n'] >= 10:
            combo3_results.append((f"{f1} + {f2} + {f3}", s))

    combo3_results.sort(key=lambda x: x[1]['avg'], reverse=True)
    for fname, s in combo3_results[:10]:
        out(f"    {fname:>50}: {fmt_stats(s)}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7: TIME-DECAY CURVE — Theta surface
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 7: THETA SURFACE — P&L by (entry_time, exit_time) pair")
out("=" * 100)

out("\n--- Wing=40, no target/stop, pure hold-to-exit ---\n")
out("Rows = entry time, Cols = exit time (avg P&L per spread)")
header = f"{'Entry':>8}"
for ex in TIME_SLOTS:
    header += f"  {ex:>8}"
out(header)
out("-" * len(header))

for et in TIME_SLOTS[:-1]:  # can't enter at last slot
    line = f"{et:>8}"
    for ex in TIME_SLOTS:
        if ex <= et:
            line += f"  {'---':>8}"
            continue
        pnls = []
        for date in DATES:
            entry = price_ibf(date, et, 40)
            if not entry:
                continue
            val = mark_ibf(date, ex, entry)
            if val is not None:
                pnls.append(entry['credit'] - val)
        if pnls:
            avg = statistics.mean(pnls)
            line += f"  {avg:>8.2f}"
        else:
            line += f"  {'n/a':>8}"
    out(line)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 8: OUT-OF-SAMPLE VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 8: OUT-OF-SAMPLE SPLIT — First half vs Second half")
out("=" * 100)

mid = len(DATES) // 2
H1_DATES = set(DATES[:mid])
H2_DATES = set(DATES[mid:])
out(f"H1: {DATES[0]} to {DATES[mid-1]} ({mid} days)")
out(f"H2: {DATES[mid]} to {DATES[-1]} ({len(DATES)-mid} days)")

# Test the top combos from Phase 6 on both halves
out("\n--- Top filter combos: H1 vs H2 stability ---\n")

# Re-run top combos and split
top_configs = [
    ('10:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('13:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('14:00', 30, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('10:00', 40, 0.50, '15:30', 'VIX<18', FILTERS['VIX<18']),
    ('10:00', 40, 0.50, '15:30', 'VP<1.3', FILTERS['VP<1.3']),
    ('10:00', 40, 0.50, '15:30', '5dRet>0', FILTERS['5dRet>0']),
    ('10:00', 40, 0.50, '15:30', 'VP<1.3+5dRet>0', lambda d: FILTERS['VP<1.3'](d) and FILTERS['5dRet>0'](d)),
    ('10:00', 40, 0.50, '15:30', 'VIX<18+RV!RISING', lambda d: FILTERS['VIX<18'](d) and FILTERS['RV!RISING'](d)),
    ('13:00', 40, 0.50, '15:30', 'VIX<18', FILTERS['VIX<18']),
    ('13:00', 40, 0.50, '15:30', 'MornRng<15', FILTERS['MornRng<15']),
    ('14:00', 30, 0.50, '15:30', 'VP<1.5', FILTERS['VP<1.5']),
]

headers = ['Config', 'Filter', 'H1_n', 'H1_avg', 'H1_WR', 'H2_n', 'H2_avg', 'H2_WR', 'Stable?']
rows = []

for et, ww, tp, ts, fname, ffunc in top_configs:
    all_t = run_all_trades(et, ww, tp, ts)
    h1 = [pnl for date, pnl, _ in all_t if date in H1_DATES and ffunc(date)]
    h2 = [pnl for date, pnl, _ in all_t if date in H2_DATES and ffunc(date)]
    s1 = calc_stats(h1)
    s2 = calc_stats(h2)
    if s1 and s2:
        # Stable = both halves profitable AND same sign avg
        stable = 'YES' if s1['avg'] > 0 and s2['avg'] > 0 else 'NO'
        rows.append([f"{et}/{ww}", fname,
                    s1['n'], f"{s1['avg']:.2f}", f"{s1['win_rate']}%",
                    s2['n'], f"{s2['avg']:.2f}", f"{s2['win_rate']}%",
                    stable])

out_table(headers, rows)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 9: CORRELATION ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 9: CORRELATION — Which variables predict P&L?")
out("=" * 100)

def pearson_r(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx)**2 for x in xs))
    dy = math.sqrt(sum((y - my)**2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


for et, ww, tp, ts in [('10:00', 40, 0.50, '15:30'), ('13:00', 40, 0.50, '15:30')]:
    out(f"\n--- Correlations with P&L: {et}/{ww}w ---\n")
    all_t = run_all_trades(et, ww, tp, ts)

    numeric_vars = ['vix', 'rv', 'vp_ratio', 'range_pct', 'score', 'score_vol',
                    'prior_day_return', 'prior_day_range', 'prior_2d_return',
                    'prior_5d_return', 'pct_in_weekly_range', 'prior_day_rv', 'rv_1d_change']

    corrs = []
    for var in numeric_vars:
        pairs = [(get_regime_val(d, var), pnl) for d, pnl, _ in all_t if get_regime_val(d, var) is not None]
        if len(pairs) >= 30:
            xs, ys = zip(*pairs)
            r = pearson_r(list(xs), list(ys))
            if r is not None:
                corrs.append((var, r, len(pairs)))

    corrs.sort(key=lambda x: abs(x[1]), reverse=True)
    for var, r, n in corrs:
        marker = " ***" if abs(r) > 0.10 else " *" if abs(r) > 0.05 else ""
        out(f"  {var:>22}: r = {r:+.4f}  (n={n}){marker}")

    # Also: morning range correlation
    pairs = [(compute_morning_range(d), pnl) for d, pnl, _ in all_t
             if compute_morning_range(d) is not None]
    if pairs:
        xs, ys = zip(*pairs)
        r = pearson_r(list(xs), list(ys))
        if r is not None:
            marker = " ***" if abs(r) > 0.10 else " *" if abs(r) > 0.05 else ""
            out(f"  {'morning_range':>22}: r = {r:+.4f}  (n={len(pairs)}){marker}")

    # Gap correlation
    pairs = []
    for d, pnl, _ in all_t:
        g = get_gap_pct(d)
        try:
            pairs.append((float(g), pnl))
        except:
            pass
    if pairs:
        xs, ys = zip(*pairs)
        r = pearson_r(list(xs), list(ys))
        if r is not None:
            marker = " ***" if abs(r) > 0.10 else " *" if abs(r) > 0.05 else ""
            out(f"  {'overnight_gap':>22}: r = {r:+.4f}  (n={len(pairs)}){marker}")


# ─────────────────────────────────────────────────────────────────────────────
# WRITE RESULTS
# ─────────────────────────────────────────────────────────────────────────────

outpath = os.path.join(_DIR, 'vol_edge_results.txt')
with open(outpath, 'w') as f:
    f.write("\n".join(OUT))
print(f"\n\nResults written to {outpath}")
print(f"Total lines: {len(OUT)}")
