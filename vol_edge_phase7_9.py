#!/usr/bin/env python3
"""Phases 7-9: Theta surface, OOS validation, correlations (already done in main)."""

import json, csv, math, statistics, os
from datetime import datetime
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))

print("Loading data...")
with open(os.path.join(_DIR, 'spx_intraday_bars.json')) as f:
    BARS = json.load(f)
with open(os.path.join(_DIR, 'option_midpoints.json')) as f:
    OPTS = json.load(f)
with open(os.path.join(_DIR, 'research_all_trades.csv')) as f:
    _csv = list(csv.DictReader(f))
with open(os.path.join(_DIR, 'spx_gap_cache.json')) as f:
    GAPS = json.load(f)

REGIME = {row['date']: row for row in _csv}
DATES = sorted(set(BARS.keys()) & set(OPTS.keys()) & set(REGIME.keys()))
TIME_SLOTS = ['09:35', '10:00', '10:30', '11:00', '12:00', '13:00', '14:00', '15:00', '15:30']

OUT = []
def out(s=""):
    OUT.append(s)
    print(s)

def price_ibf(date, time_slot, wing_width):
    if date not in OPTS or time_slot not in OPTS[date]:
        return None
    slot = OPTS[date][time_slot]
    atm = slot['atm']
    strikes = slot['strikes']
    cs, ps = str(atm), str(atm)
    cl, pl = str(atm + wing_width), str(atm - wing_width)
    if cs not in strikes or 'C' not in strikes[cs]: return None
    if ps not in strikes or 'P' not in strikes[ps]: return None
    if cl not in strikes or 'C' not in strikes[cl]: return None
    if pl not in strikes or 'P' not in strikes[pl]: return None
    credit = strikes[cs]['C'] + strikes[ps]['P'] - strikes[cl]['C'] - strikes[pl]['P']
    if credit <= 0: return None
    return {
        'spx': slot['spx'], 'atm': atm, 'wing': wing_width,
        'credit': round(credit, 3), 'max_risk': round(wing_width - credit, 3),
        'call_wing': atm + wing_width, 'put_wing': atm - wing_width,
    }

def mark_ibf(date, time_slot, entry):
    if date not in OPTS or time_slot not in OPTS[date]: return None
    strikes = OPTS[date][time_slot]['strikes']
    cs, ps = str(entry['atm']), str(entry['atm'])
    cl, pl = str(entry['call_wing']), str(entry['put_wing'])
    if cs not in strikes or 'C' not in strikes[cs]: return None
    if ps not in strikes or 'P' not in strikes[ps]: return None
    if cl not in strikes or 'C' not in strikes[cl]: return None
    if pl not in strikes or 'P' not in strikes[pl]: return None
    return round(strikes[cs]['C'] + strikes[ps]['P'] - strikes[cl]['C'] - strikes[pl]['P'], 3)

def compute_wing_stop_time(date, entry):
    if date not in BARS: return None
    for t in sorted(BARS[date].keys()):
        bar = BARS[date][t]
        if bar['h'] >= entry['call_wing'] or bar['l'] <= entry['put_wing']:
            return (t, bar['c'])
    return None

def run_trade(date, entry_time, wing_width, target_pct, time_stop, use_wing_stop=True):
    entry = price_ibf(date, entry_time, wing_width)
    if not entry: return None
    credit = entry['credit']
    exit_slots = [t for t in TIME_SLOTS if t > entry_time]
    if not exit_slots: return None
    ws_exit = None
    if use_wing_stop:
        ws = compute_wing_stop_time(date, entry)
        if ws and ws[0] > entry_time:
            ws_exit = ws[0]
    for slot in exit_slots:
        if ws_exit and ws_exit <= slot:
            val = mark_ibf(date, slot, entry)
            if val is not None:
                return {'pnl': round(credit - val, 3), 'exit': 'WING_STOP', 'exit_time': slot, 'credit': credit}
            return {'pnl': round(credit - (wing_width * 0.7), 3), 'exit': 'WING_STOP_EST', 'exit_time': ws_exit, 'credit': credit}
        val = mark_ibf(date, slot, entry)
        if val is not None:
            pnl = credit - val
            if pnl >= credit * target_pct:
                return {'pnl': round(pnl, 3), 'exit': 'TARGET', 'exit_time': slot, 'credit': credit}
        if slot >= time_stop:
            if val is not None:
                return {'pnl': round(credit - val, 3), 'exit': 'TIME', 'exit_time': slot, 'credit': credit}
    last = exit_slots[-1]
    val = mark_ibf(date, last, entry)
    if val is not None:
        return {'pnl': round(credit - val, 3), 'exit': 'CLOSE', 'exit_time': last, 'credit': credit}
    return None

def run_all_trades(et, ww, tp, ts, ws=True):
    return [(d, r['pnl'], r) for d in DATES for r in [run_trade(d, et, ww, tp, ts, ws)] if r]

def calc_stats(pnls):
    if not pnls: return None
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    avg = total / n
    wr = len(wins) / n * 100
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')
    cum, peak, dd = 0, 0, 0
    for p in pnls:
        cum += p; peak = max(peak, cum); dd = min(dd, cum - peak)
    if n > 1:
        se = statistics.stdev(pnls) / math.sqrt(n)
        t_stat = avg / se if se > 0 else 0
        from math import erfc
        p_val = erfc(abs(t_stat) / math.sqrt(2))
    else:
        t_stat, p_val = 0, 1.0
    return {'n': n, 'total': round(total, 2), 'avg': round(avg, 3),
            'win_rate': round(wr, 1), 'pf': round(pf, 2),
            'max_dd': round(dd, 2), 't_stat': round(t_stat, 2), 'p_val': round(p_val, 4)}

def get_regime_val(date, col, as_float=True):
    r = REGIME.get(date)
    if not r or col not in r or r[col] == '': return None
    if as_float:
        try: return float(r[col])
        except: return None
    return r[col]

def compute_morning_range(date, start='09:30', end='10:00'):
    if date not in BARS: return None
    hi, lo = -1e9, 1e9
    for t in sorted(BARS[date].keys()):
        if t < start: continue
        if t > end: break
        bar = BARS[date][t]
        hi = max(hi, bar['h']); lo = min(lo, bar['l'])
    return round(hi - lo, 2) if hi > 0 else None

FILTERS = {
    'VIX<15': lambda d: (get_regime_val(d, 'vix') or 99) < 15,
    'VIX<18': lambda d: (get_regime_val(d, 'vix') or 99) < 18,
    'VIX<20': lambda d: (get_regime_val(d, 'vix') or 99) < 20,
    'VP<1.3': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.3,
    'VP<1.5': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.5,
    'VP<1.7': lambda d: (get_regime_val(d, 'vp_ratio') or 99) < 1.7,
    '5dRet>0': lambda d: (get_regime_val(d, 'prior_5d_return') or -99) > 0,
    '5dRet>1': lambda d: (get_regime_val(d, 'prior_5d_return') or -99) > 1,
    'PriorDOWN': lambda d: get_regime_val(d, 'prior_day_direction', False) == 'DOWN',
    'RV!RISING': lambda d: get_regime_val(d, 'rv_slope', False) != 'RISING',
    'MornRng<15': lambda d: (compute_morning_range(d) or 99) < 15,
    'MornRng<10': lambda d: (compute_morning_range(d) or 99) < 10,
}

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7: THETA SURFACE
# ─────────────────────────────────────────────────────────────────────────────

out("=" * 100)
out("PHASE 7: THETA SURFACE — P&L by (entry_time, exit_time) pair")
out("=" * 100)

for ww in [30, 40, 50]:
    out(f"\n--- Wing={ww}, no target/stop, pure hold-to-exit (avg P&L per spread) ---\n")
    header = f"{'Entry':>8}"
    for ex in TIME_SLOTS:
        header += f"  {ex:>8}"
    header += "  {'N':>5}"
    out(header)
    out("-" * len(header))

    for et in TIME_SLOTS[:-1]:
        line = f"{et:>8}"
        for ex in TIME_SLOTS:
            if ex <= et:
                line += f"  {'---':>8}"
                continue
            pnls = []
            for date in DATES:
                entry = price_ibf(date, et, ww)
                if not entry: continue
                val = mark_ibf(date, ex, entry)
                if val is not None:
                    pnls.append(entry['credit'] - val)
            if pnls:
                line += f"  {statistics.mean(pnls):>8.2f}"
            else:
                line += f"  {'n/a':>8}"
        # Count for this entry time
        n = sum(1 for d in DATES if price_ibf(d, et, ww) is not None)
        line += f"  {n:>5}"
        out(line)

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 8: OUT-OF-SAMPLE VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 8: OUT-OF-SAMPLE SPLIT — First half vs Second half")
out("=" * 100)

mid = len(DATES) // 2
H1 = set(DATES[:mid])
H2 = set(DATES[mid:])
out(f"H1: {DATES[0]} to {DATES[mid-1]} ({mid} days)")
out(f"H2: {DATES[mid]} to {DATES[-1]} ({len(DATES)-mid} days)")

configs = [
    ('10:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('13:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('14:00', 50, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('13:00', 40, 0.30, '15:30', 'UNFILTERED', lambda d: True),
    ('10:00', 40, 0.50, '15:30', 'VIX<18', FILTERS['VIX<18']),
    ('10:00', 40, 0.50, '15:30', 'VP<1.3+5dRet>0', lambda d: FILTERS['VP<1.3'](d) and FILTERS['5dRet>0'](d)),
    ('10:00', 40, 0.50, '15:30', 'PriorDOWN', FILTERS['PriorDOWN']),
    ('10:00', 40, 0.50, '15:30', 'PriorDOWN+5dRet>0', lambda d: FILTERS['PriorDOWN'](d) and FILTERS['5dRet>0'](d)),
    ('13:00', 40, 0.50, '15:30', 'VP<1.5', FILTERS['VP<1.5']),
    ('13:00', 40, 0.50, '15:30', 'VP<1.5+5dRet>1', lambda d: FILTERS['VP<1.5'](d) and FILTERS['5dRet>1'](d)),
    ('13:00', 40, 0.50, '15:30', 'MornRng<15', FILTERS['MornRng<15']),
    ('14:00', 50, 0.50, '15:30', 'VP<1.7+RV!RISING', lambda d: FILTERS['VP<1.7'](d) and FILTERS['RV!RISING'](d)),
    ('12:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('12:00', 40, 0.30, '15:30', 'UNFILTERED', lambda d: True),
    ('12:00', 40, 0.50, '15:30', 'VP<1.5', FILTERS['VP<1.5']),
]

out(f"\n{'Config':<20} {'Filter':<25} {'H1_n':>5} {'H1_avg':>8} {'H1_WR':>7} {'H1_PF':>7} {'H2_n':>5} {'H2_avg':>8} {'H2_WR':>7} {'H2_PF':>7} {'Stable':>7}")
out("-" * 120)

for et, ww, tp, ts, fname, ffunc in configs:
    all_t = run_all_trades(et, ww, tp, ts)
    h1 = [pnl for d, pnl, _ in all_t if d in H1 and ffunc(d)]
    h2 = [pnl for d, pnl, _ in all_t if d in H2 and ffunc(d)]
    s1 = calc_stats(h1)
    s2 = calc_stats(h2)
    if s1 and s2:
        stable = 'YES' if s1['avg'] > 0 and s2['avg'] > 0 else 'NO'
        out(f"{et}/{ww}/{tp:.0%}/{ts:<8} {fname:<25} {s1['n']:>5} {s1['avg']:>8.2f} {s1['win_rate']:>6.1f}% {s1['pf']:>7.2f} {s2['n']:>5} {s2['avg']:>8.2f} {s2['win_rate']:>6.1f}% {s2['pf']:>7.2f} {stable:>7}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 9: EQUITY CURVES — Cumulative P&L for top configs
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 9: MONTHLY P&L BREAKDOWN — Top configs")
out("=" * 100)

top_cfgs = [
    ('13:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('13:00', 40, 0.30, '15:30', 'UNFILTERED', lambda d: True),
    ('13:00', 40, 0.50, '15:30', 'VP<1.5', FILTERS['VP<1.5']),
    ('12:00', 40, 0.50, '15:30', 'UNFILTERED', lambda d: True),
    ('10:00', 40, 0.50, '15:30', 'PriorDOWN+5dRet>0', lambda d: FILTERS['PriorDOWN'](d) and FILTERS['5dRet>0'](d)),
    ('14:00', 50, 0.50, '15:30', 'UNFILTERED', lambda d: True),
]

for et, ww, tp, ts, fname, ffunc in top_cfgs:
    out(f"\n--- {et}/{ww}/{tp:.0%}/{ts} [{fname}] ---")
    all_t = [(d, pnl, r) for d, pnl, r in run_all_trades(et, ww, tp, ts) if ffunc(d)]

    # Group by month
    monthly = defaultdict(list)
    for d, pnl, _ in all_t:
        monthly[d[:7]].append(pnl)

    out(f"{'Month':<10} {'N':>4} {'Total':>8} {'Avg':>8} {'WR':>6} {'Wins':>5} {'Losses':>7}")
    out("-" * 55)
    months = sorted(monthly.keys())
    for m in months:
        pnls = monthly[m]
        w = sum(1 for p in pnls if p > 0)
        l = sum(1 for p in pnls if p <= 0)
        out(f"{m:<10} {len(pnls):>4} {sum(pnls):>8.1f} {statistics.mean(pnls):>8.2f} {w/len(pnls)*100:>5.0f}% {w:>5} {l:>7}")

    # Summary
    all_pnls = [p for _, p, _ in all_t]
    cum = 0
    peak = 0
    dd = 0
    losing_months = 0
    for m in months:
        msum = sum(monthly[m])
        cum += msum
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        if msum < 0:
            losing_months += 1
    out(f"\nTotal: {sum(all_pnls):.1f}  MaxDD: {dd:.1f}  Losing months: {losing_months}/{len(months)}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 10: EXIT TYPE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

out("\n" + "=" * 100)
out("PHASE 10: EXIT TYPE BREAKDOWN — How do trades end?")
out("=" * 100)

for et, ww, tp, ts in [('10:00', 40, 0.50, '15:30'), ('13:00', 40, 0.50, '15:30'),
                         ('14:00', 50, 0.50, '15:30'), ('12:00', 40, 0.50, '15:30')]:
    out(f"\n--- {et}/{ww}/{tp:.0%}/{ts} ---")
    all_t = run_all_trades(et, ww, tp, ts)
    exits = defaultdict(list)
    for d, pnl, r in all_t:
        exits[r['exit']].append(pnl)

    out(f"{'Exit':>15} {'N':>5} {'Avg':>8} {'WR':>6} {'Total':>8}")
    out("-" * 50)
    for ex_type in sorted(exits.keys()):
        pnls = exits[ex_type]
        w = sum(1 for p in pnls if p > 0)
        out(f"{ex_type:>15} {len(pnls):>5} {statistics.mean(pnls):>8.2f} {w/len(pnls)*100:>5.0f}% {sum(pnls):>8.1f}")


# ─────────────────────────────────────────────────────────────────────────────
# WRITE
# ─────────────────────────────────────────────────────────────────────────────

outpath = os.path.join(_DIR, 'vol_edge_results_p7_10.txt')
with open(outpath, 'w') as f:
    f.write("\n".join(OUT))
print(f"\nResults written to {outpath} ({len(OUT)} lines)")
