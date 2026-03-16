"""Compute V3-V14 strategy performance stats with correct definitions from cockpit_feed.py."""
import pandas as pd
import numpy as np
import json, os

_DIR = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(_DIR, 'research_all_trades.csv'))
with open(os.path.join(_DIR, 'spx_gap_cache.json')) as f:
    gaps = json.load(f)

# ── Trade-detail helpers ──────────────────────────────────────────────────────
def _safe_f(v, dec=2):
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, dec)
    except: return None

def _safe_s(v):
    try: return str(v) if pd.notna(v) and str(v) not in ('', '-', '—', 'nan') else None
    except: return None

def _get_exit_time(result, row):
    et = result['exit_type']
    if et == 'WING_STOP':
        ws = row.get('ws_time', '')
        if pd.notna(ws) and ws:
            try:
                t = str(ws).split(' ')[1][:5]
                return t + ' ET'
            except: pass
    slot = result.get('exit_slot')
    if slot and slot != 'close':
        return slot[:2] + ':' + slot[2:] + ' ET'
    if slot == 'close': return '16:15 ET'
    return None

def _build_intraday(row):
    tcs = ['1030','1100','1130','1200','1230','1300','1330','1400','1430','1500','1530','1545']
    d = {}
    for tc in tcs:
        v = row.get(f'pnl_at_{tc}')
        if pd.notna(v):
            try: d[tc[:2]+':'+tc[2:]] = round(float(v), 2)
            except: pass
    return d

# CORRECT strategy definitions from cockpit_feed.py lines 51-88
strats = [
    {"ver":"v3","type":"phoenix","mech":"50%/close/1T","entry":"10:00","desc":"PHOENIX Concentrated Signal Model"},
    {"ver":"v6","vix":[0,15],"pd":"DN","rng":"IN","gap":"GFL","filter":"VP<=1.7","mech":"50%/1530/1T","entry":"10:00",
     "desc":"LOW VIX | Prior Down | In Range | Flat Gap"},
    {"ver":"v7","vix":[0,15],"pd":"FL","rng":"IN","gap":"GUP","filter":None,"mech":"40%/close/1T","entry":"10:00",
     "desc":"LOW VIX | Prior Flat | In Range | Gap Up"},
    {"ver":"v8","vix":[20,25],"pd":"UP","rng":"IN","gap":"GDN","filter":None,"mech":"40%/1530/1T","entry":"10:30",
     "desc":"ELEV VIX | Prior Up | In Range | Gap Down"},
    {"ver":"v9","vix":[15,20],"pd":"UP","rng":"OT","gap":"GFL","filter":"!RISING","mech":"70%/1545/1T","entry":"10:00",
     "desc":"MID VIX | Prior Up | Outside Range | Flat Gap"},
    {"ver":"v10","vix":[15,20],"pd":"DN","rng":"OT","gap":"GFL","filter":None,"mech":"70%/1545/1T","entry":"11:00",
     "desc":"MID VIX | Prior Down | Outside Range | Flat Gap"},
    {"ver":"v12","vix":[0,15],"pd":"UP","rng":"OT","gap":"GUP","filter":"5dRet>1","mech":"40%/close/1T","entry":"10:00",
     "desc":"LOW VIX | Prior Up | Outside Range | Gap Up"},
    {"ver":"v14","vix":[15,20],"pd":"DN","rng":"IN","gap":"GDN","filter":"ScoreVol<18","mech":"50%/close/1T","entry":"10:00",
     "desc":"MID VIX | Prior Down | In Range | Gap Down"},
]

def classify_row(row):
    vix = row['vix']
    if vix < 15: vr = 'LOW'
    elif vix < 20: vr = 'MID'
    elif vix < 25: vr = 'ELEV'
    else: vr = 'HIGH'
    prior_dir = row.get('prior_day_direction', 'FLAT')
    pd_label = 'UP' if prior_dir == 'UP' else 'DN' if prior_dir == 'DOWN' else 'FL'
    in_range = bool(row.get('in_prior_week_range', 0)) if pd.notna(row.get('in_prior_week_range')) else True
    rng = 'IN' if in_range else 'OT'
    d = str(row['date'])[:10]
    gap_pct = gaps.get(d, 0)
    if isinstance(gap_pct, dict): gap_pct = gap_pct.get('gap_pct', 0)
    gap_label = 'GUP' if gap_pct > 0.25 else 'GDN' if gap_pct < -0.25 else 'GFL'
    return vr, pd_label, rng, gap_label, vix

def phoenix_fire_count(row):
    vp = row.get('vp_ratio', 99)
    vix = row['vix']
    ret5d = row.get('prior_5d_return', -99)
    rv_chg = row.get('rv_1d_change', -99)
    prior_dir = row.get('prior_day_direction', 'FLAT')
    in_range = bool(row.get('in_prior_week_range', 0)) if pd.notna(row.get('in_prior_week_range')) else True
    rv_slope = row.get('rv_slope', 'UNKNOWN')
    if pd.isna(vp): vp = 99
    if pd.isna(ret5d): ret5d = -99
    if pd.isna(rv_chg): rv_chg = -99
    g1 = vix <= 20 and vp <= 1.0 and ret5d > 0
    g2 = vp <= 1.3 and prior_dir == 'DOWN' and ret5d > 0
    g3 = vp <= 1.2 and ret5d > 0 and rv_chg > 0
    g4 = vp <= 1.5 and not in_range and ret5d > 0
    g5 = vp <= 1.3 and rv_slope != 'RISING' and ret5d > 0
    return sum([g1,g2,g3,g4,g5]), [g1,g2,g3,g4,g5]

# ── Regime strategy sizing: Option A+C combined ──────────────────────────────
# C (static cap): based on backtest profit factor
#   PF >= 2.0  → $100K ceiling  (V7, V10, V12, V14)
#   PF 1.5-2.0 → $75K ceiling   (V6, V9)
#   PF < 1.5   → $50K ceiling   (V8)
# A (VP scale within cap): lower VP = more premium edge = bigger size
#   VP ≤ 1.0   → 100%  (implied ≈ realized, selling into fair premium)
#   VP 1.0-1.2 → 75%
#   VP 1.2-1.5 → 50%
#   VP > 1.5   → 25%  (barely passed filter, minimum sizing)
REGIME_MAX_BUDGET = {
    'v6':  75000,   # p<0.05, Sharpe 7.11, small n — $75K
    'v7':  25000,   # ns, n=5, cannot trust — $25K
    'v8':  25000,   # ns, Sharpe 1.58, conflicts Law 1 — $25K
    'v9':  100000,  # p<0.01, Sharpe 8.35, worst only -$16K — $100K
    'v10': 75000,   # p<0.1, Sharpe 4.72, real edge — $75K
    'v12': 75000,   # ns but power problem (n=11), Sharpe 5.88 — $75K
    'v14': 75000,   # p<0.05, Sharpe 11.61, but single loss = -$75K — $75K
}

def regime_budget(ver, vp):
    if pd.isna(vp) or vp is None: vp = 1.5
    max_bud = REGIME_MAX_BUDGET.get(ver, 100000)
    if   vp <= 1.0: scale = 1.00
    elif vp <= 1.2: scale = 0.75
    elif vp <= 1.5: scale = 0.50
    else:           scale = 0.25
    return int(max_bud * scale)

def check_filter(filt, row):
    if filt is None: return True
    vp = row.get('vp_ratio', 99)
    ret5d = row.get('prior_5d_return', -99)
    rv_slope = row.get('rv_slope', 'UNKNOWN')
    score_vol = row.get('score_vol', 99)
    range_pct = row.get('range_pct', 99)
    if pd.isna(vp): vp = 99
    if pd.isna(ret5d): ret5d = -99
    if pd.isna(score_vol): score_vol = 99
    if pd.isna(range_pct): range_pct = 99
    if filt == '5dRet>0': return ret5d > 0
    if filt == '5dRet>1': return ret5d > 1.0
    if filt == 'VP<=1.7': return vp <= 1.7
    if filt == 'VP<=2.0': return vp <= 2.0
    if filt == '!RISING': return rv_slope != 'RISING'
    if filt == 'ScoreVol<18': return score_vol < 18
    if filt == 'Rng<=0.3': return range_pct <= 0.3
    return True

def compute_pnl(row, strat, fire_count=0, override_budget=None):
    ww = row['wing_width']
    n_spreads = row['n_spreads_p1']
    risk_deployed = row['risk_deployed_p1']
    if pd.isna(n_spreads) or n_spreads <= 0 or pd.isna(risk_deployed): return None
    entry_credit = ww - risk_deployed / n_spreads / 100
    if entry_credit <= 0: return None

    mech = strat['mech']
    parts = mech.split('/')
    target_pct = int(parts[0].replace('%','')) / 100
    time_stop = parts[1]
    target_credit = entry_credit * target_pct

    exit_type = None
    exit_slot = None
    pnl_per_spread = 0
    time_cols = ['1030','1100','1130','1200','1230','1300','1330','1400','1430','1500','1530','1545']

    if time_stop == '1530': stop_col_idx = time_cols.index('1530')
    elif time_stop == '1545': stop_col_idx = time_cols.index('1545')
    else: stop_col_idx = len(time_cols)

    for i, t in enumerate(time_cols):
        col = f'pnl_at_{t}'
        if col in row.index and pd.notna(row[col]):
            if row[col] < -(ww - entry_credit) * 0.70:
                pnl_per_spread = row[col]; exit_type = 'WING_STOP'; exit_slot = t; break
            if row[col] >= target_credit:
                pnl_per_spread = target_credit; exit_type = 'TARGET'; exit_slot = t; break
            if i >= stop_col_idx:
                pnl_per_spread = row[col]; exit_type = 'TIME'; exit_slot = t; break

    if exit_type is None:
        pnl_per_spread = row.get('pnl_at_close', 0)
        if pd.isna(pnl_per_spread): pnl_per_spread = 0
        exit_type = 'CLOSE'
        exit_slot = 'close'

    if strat.get('type') == 'phoenix':
        tier_map = {0: 0, 1: 25000, 2: 50000, 3: 75000, 4: 100000, 5: 100000}
        risk_budget = tier_map.get(fire_count, 100000)
    elif override_budget is not None:
        risk_budget = override_budget
    else:
        risk_budget = 100000

    max_loss_per = (ww - entry_credit) * 100
    if max_loss_per <= 0: return None
    qty = int(risk_budget // max_loss_per)
    if qty <= 0: return None

    dollar_pnl = qty * (pnl_per_spread * 100 - 100)
    return {
        'dollar_pnl': dollar_pnl, 'pnl_per_spread': pnl_per_spread,
        'entry_credit': entry_credit, 'exit_type': exit_type, 'exit_slot': exit_slot,
        'qty': qty, 'risk_budget': risk_budget, 'ww': ww,
        'fire_count': fire_count, 'is_win': pnl_per_spread > 0,
    }

all_stats = {}
all_trades_export = []
for strat in strats:
    ver = strat['ver']
    trades = []
    for idx, row in df.iterrows():
        vr, pd_label, rng, gap_label, vix_val = classify_row(row)
        if ver == 'v3':
            fc, details = phoenix_fire_count(row)
            if fc > 0:
                result = compute_pnl(row, strat, fc)
                if result:
                    result['date'] = str(row['date'])[:10]
                    trades.append(result)
                    all_trades_export.append({
                        'date': result['date'], 'ver': ver,
                        'pnl': round(result['dollar_pnl']),
                        'qty': result['qty'], 'exit': result['exit_type'],
                        'fire_count': fc, 'risk_budget': result['risk_budget'],
                        'pnl_ps': round(result['pnl_per_spread'], 2),
                        'is_win': result['is_win'],
                        'entry_time': strat['entry'],
                        'exit_time': _get_exit_time(result, row),
                        'wing_width': int(result['ww']),
                        'entry_credit': round(result['entry_credit'], 2),
                        'vix': _safe_f(row.get('vix')),
                        'score': _safe_f(row.get('score'), 0),
                        'vp_ratio': _safe_f(row.get('vp_ratio'), 3),
                        'rv': _safe_f(row.get('rv')),
                        'prior_5d': _safe_f(row.get('prior_5d_return'), 3),
                        'prior_1d': _safe_f(row.get('prior_day_return'), 3),
                        'prior_dir': _safe_s(row.get('prior_day_direction')),
                        'max_ps': _safe_f(row.get('max_pnl')),
                        'min_ps': _safe_f(row.get('min_pnl')),
                        'intraday': _build_intraday(row),
                        'fire_signals': details,
                    })
        else:
            vix_range = strat.get('vix')
            if not vix_range: continue
            if not (vix_val >= vix_range[0] and vix_val < vix_range[1]): continue
            if pd_label != strat['pd']: continue
            if rng != strat['rng']: continue
            if gap_label != strat['gap']: continue
            if not check_filter(strat.get('filter'), row): continue
            vp = row.get('vp_ratio', 1.5)
            if ver == 'v9' and float(vp or 0) > 2.0: continue  # VP cap: extreme stress, all losers
            bud = regime_budget(ver, vp)
            if ver == 'v10':  # half-size in deep downtrend
                ret5 = _safe_f(row.get('prior_5d_return'), 3)
                if ret5 is not None and ret5 <= -1.5:
                    bud = bud // 2
            result = compute_pnl(row, strat, override_budget=bud)
            if result:
                result['date'] = str(row['date'])[:10]
                trades.append(result)
                all_trades_export.append({
                    'date': result['date'], 'ver': ver,
                    'pnl': round(result['dollar_pnl']),
                    'qty': result['qty'], 'exit': result['exit_type'],
                    'fire_count': None, 'risk_budget': result['risk_budget'],
                    'pnl_ps': round(result['pnl_per_spread'], 2),
                    'is_win': result['is_win'],
                    'entry_time': strat['entry'],
                    'exit_time': _get_exit_time(result, row),
                    'wing_width': int(result['ww']),
                    'entry_credit': round(result['entry_credit'], 2),
                    'vix': _safe_f(row.get('vix')),
                    'score': _safe_f(row.get('score'), 0),
                    'vp_ratio': _safe_f(row.get('vp_ratio'), 3),
                    'rv': _safe_f(row.get('rv')),
                    'prior_5d': _safe_f(row.get('prior_5d_return'), 3),
                    'prior_1d': _safe_f(row.get('prior_day_return'), 3),
                    'prior_dir': _safe_s(row.get('prior_day_direction')),
                    'max_ps': _safe_f(row.get('max_pnl')),
                    'min_ps': _safe_f(row.get('min_pnl')),
                    'intraday': _build_intraday(row),
                    'fire_signals': None,
                })

    if trades:
        pnls = [t['dollar_pnl'] for t in trades]
        wins = [t for t in trades if t['is_win']]
        losses = [t for t in trades if not t['is_win']]
        gross_wins = sum(t['dollar_pnl'] for t in wins)
        gross_losses = abs(sum(t['dollar_pnl'] for t in losses))

        exit_counts = {}; exit_pnl = {}
        for t in trades:
            et = t['exit_type']
            exit_counts[et] = exit_counts.get(et, 0) + 1
            exit_pnl[et] = exit_pnl.get(et, 0) + t['dollar_pnl']

        monthly = {}
        for t in trades:
            ym = t['date'][:7]; monthly[ym] = monthly.get(ym, 0) + t['dollar_pnl']

        fc_dist = {}
        if ver == 'v3':
            for t in trades:
                fc = t['fire_count']
                if fc not in fc_dist: fc_dist[fc] = {'count': 0, 'pnl': 0, 'wins': 0}
                fc_dist[fc]['count'] += 1; fc_dist[fc]['pnl'] += t['dollar_pnl']
                if t['is_win']: fc_dist[fc]['wins'] += 1

        streak_w = streak_l = max_sw = max_sl = 0
        for t in trades:
            if t['is_win']: streak_w += 1; streak_l = 0; max_sw = max(max_sw, streak_w)
            else: streak_l += 1; streak_w = 0; max_sl = max(max_sl, streak_l)

        equity = peak = max_dd = 0
        for t in trades:
            equity += t['dollar_pnl']; peak = max(peak, equity); max_dd = max(max_dd, peak - equity)

        stats = {
            'ver': ver, 'mech': strat['mech'], 'entry_time': strat['entry'],
            'filter': strat.get('filter') or ('PHOENIX 5-signal confluence' if ver == 'v3' else 'None'),
            'desc': strat.get('desc', ''),
            'total_trades': len(trades), 'wins': len(wins), 'losses': len(losses),
            'win_rate': round(len(wins) / len(trades) * 100, 1),
            'total_pnl': round(sum(pnls)), 'avg_pnl': round(sum(pnls) / len(trades)),
            'max_win': round(max(pnls)), 'max_loss': round(min(pnls)),
            'median_pnl': round(float(np.median(pnls))),
            'gross_wins': round(gross_wins), 'gross_losses': round(gross_losses),
            'profit_factor': round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999,
            'avg_win': round(gross_wins / len(wins)) if wins else 0,
            'avg_loss': round(-gross_losses / len(losses)) if losses else 0,
            'exit_counts': exit_counts, 'exit_pnl': {k: round(v) for k, v in exit_pnl.items()},
            'monthly_pnl': {k: round(v) for k, v in sorted(monthly.items())},
            'max_win_streak': max_sw, 'max_loss_streak': max_sl,
            'max_drawdown': round(max_dd), 'final_equity': round(equity),
            'avg_qty': round(np.mean([t['qty'] for t in trades]), 1),
            'avg_entry_credit': round(np.mean([t['entry_credit'] for t in trades]), 2),
            'avg_ww': round(np.mean([t['ww'] for t in trades]), 1),
        }
        if ver == 'v3':
            stats['fire_count_dist'] = {str(k): v for k, v in sorted(fc_dist.items())}
        all_stats[ver] = stats
    else:
        all_stats[ver] = {'ver': ver, 'total_trades': 0, 'total_pnl': 0, 'desc': strat.get('desc','')}

with open(os.path.join(_DIR, 'strategy_trades.json'), 'w') as f:
    json.dump(all_trades_export, f, indent=2)

with open(os.path.join(_DIR, 'strategy_stats.json'), 'w') as f:
    json.dump(all_stats, f, indent=2)

print(f"{'Ver':<5} {'N':>4} {'WR%':>6} {'TotalP&L':>12} {'AvgP&L':>9} {'PF':>6} {'MaxDD':>9} {'AvgWin':>8} {'AvgLoss':>9}")
print("-" * 80)
total_pnl = 0
for ver in ['v3','v6','v7','v8','v9','v10','v12','v14']:
    s = all_stats.get(ver, {})
    n = s.get('total_trades', 0)
    total_pnl += s.get('total_pnl', 0)
    if n > 0:
        print(f"{ver:<5} {n:>4} {s['win_rate']:>5.1f}% ${s['total_pnl']:>10,} ${s['avg_pnl']:>7,} {s['profit_factor']:>5.2f} ${s['max_drawdown']:>7,} ${s['avg_win']:>6,} ${s['avg_loss']:>7,}")
    else:
        print(f"{ver:<5}    0    ---")
print(f"\nCombined: ${total_pnl:,}")
print(f"Period: {df['date'].min()} to {df['date'].max()} ({len(df)} days)")
