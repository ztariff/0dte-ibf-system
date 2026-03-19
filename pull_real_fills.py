"""
Pull real bid/ask fill prices for N16, N17, N18 signal days.

For each signal day + entry time:
  - Entry: sell short legs at BID, buy long legs at ASK (real fill prices)
  - Exit: hold to expiry — P&L = real_credit - intrinsic_value_at_close
    (intrinsic from real SPX closing price already in spx_intraday_bars.json)

Endpoint: GET /v3/quotes/{ticker}?timestamp.gte=T&timestamp.lte=T+2min
Returns: bid_price, ask_price at the specific entry minute

Signal days:
  N16: Tuesday + Phoenix fire_count>0 + 11:00 entry (±25/±40 wings)
  N17: Phoenix fire_count>0 + VVIX<100    + 13:00 entry (±25/±40 wings)
  N18: 3-Laws (5d>1%,VP<2,prior!=FLAT)   + 14:00 entry (±10/±20 wings)

Output: real_fills.json — keyed by strategy -> date -> {credit, max_risk, pnl, ...}
        real_fills_log.txt — full coverage report
"""
import sys, io, os, json, time, requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

API_KEY = 'cBE5Kbq9yllt0Yj29mDQjBcIKfAYQlHF'
BASE    = 'https://api.polygon.io'

_DIR = os.path.dirname(os.path.abspath(__file__))

import pandas as pd
import numpy as np

df = pd.read_csv(os.path.join(_DIR, 'research_all_trades.csv'))
df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')

with open(os.path.join(_DIR, 'spx_intraday_bars.json')) as f: spx_bars  = json.load(f)
with open(os.path.join(_DIR, 'vvix_daily.json'))        as f: vvix_data = json.load(f)
with open(os.path.join(_DIR, 'vix9d_daily.json'))       as f: vix9d_data= json.load(f)

_gap_paths = [
    os.path.join(_DIR, 'spx_gap_cache.json'),
    os.path.join(_DIR, '.claude', 'worktrees', 'hungry-feynman', 'spx_gap_cache.json'),
]
_gap_file = next((p for p in _gap_paths if os.path.exists(p)), None)
with open(_gap_file) as f: gaps = json.load(f)

df['vvix']     = df['date'].map(vvix_data)
df['vix9d']    = df['date'].map(vix9d_data)
df['ts_ratio'] = df['vix9d'] / df['vix']

LOG_FILE  = 'real_fills_log.txt'
OUT_FILE  = 'real_fills.json'
log_lines = []

def log(msg):
    print(msg); log_lines.append(msg)

def save_log():
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(log_lines))

# ── Phoenix fire count (same as compute_stats.py) ────────────────────────────
def phoenix_fire_count(row):
    vp       = row.get('vp_ratio', 99)
    vix      = row['vix']
    ret5d    = row.get('prior_5d_return', -99)
    rv_chg   = row.get('rv_1d_change', -99)
    prior_dir= row.get('prior_day_direction', 'FLAT')
    in_range = bool(row.get('in_prior_week_range', 0)) if pd.notna(row.get('in_prior_week_range')) else True
    rv_slope = row.get('rv_slope', 'UNKNOWN')
    if pd.isna(vp):    vp    = 99
    if pd.isna(ret5d): ret5d = -99
    if pd.isna(rv_chg):rv_chg= -99
    g1 = vix <= 20 and vp <= 1.0  and ret5d > 0
    g2 = vp <= 1.3  and prior_dir == 'DOWN' and ret5d > 0
    g3 = vp <= 1.2  and ret5d > 0 and rv_chg > 0
    g4 = vp <= 1.5  and not in_range and ret5d > 0
    g5 = vp <= 1.3  and rv_slope != 'RISING' and ret5d > 0
    return sum([g1,g2,g3,g4,g5])

# ── Helpers ───────────────────────────────────────────────────────────────────
def time_to_min(t):
    h, m = t.split(':'); return int(h)*60 + int(m)

def find_spx_at(date, target_time, tol=3):
    bars = spx_bars.get(date, {})
    tgt  = time_to_min(target_time)
    best_val, best_diff = None, 9999
    for t, bar in bars.items():
        diff = abs(time_to_min(t) - tgt)
        if diff < best_diff:
            best_diff = diff
            best_val = bar['c']
    return best_val if best_diff <= tol else None

def get_atm(price, step=5):
    return int(round(round(price / step) * step))

def make_ticker(date, cp, strike):
    ymd   = date.replace('-','')[2:]
    s_str = f"{int(strike * 1000):08d}"
    return f"O:SPXW{ymd}{cp}{s_str}"

def get_api(url, params=None, retries=4):
    p = dict(params or {}); p['apiKey'] = API_KEY
    for attempt in range(retries):
        try:
            r = requests.get(url, params=p, timeout=30)
            if r.status_code == 429:
                time.sleep(12); continue
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            time.sleep(2)
    return None

def pull_quote_at_time(date, ticker, entry_time_et):
    """
    Pull best bid/ask for ticker at entry_time on date.
    Returns (bid, ask) or (None, None) if not available.
    """
    hh, mm = entry_time_et.split(':')
    # Build ISO timestamps for ±90 seconds around entry
    t_from = f"{date}T{hh}:{mm}:00-05:00"
    # Add 2 minutes to get a window
    mm2 = int(mm) + 2
    hh2 = int(hh) + mm2 // 60
    mm2 = mm2 % 60
    t_to = f"{date}T{hh2:02d}:{mm2:02d}:00-05:00"

    url = f"{BASE}/v3/quotes/{ticker}"
    data = get_api(url, {
        'timestamp.gte': t_from,
        'timestamp.lte': t_to,
        'limit': 10,
        'sort': 'timestamp',
        'order': 'asc',
    })

    if not data or not data.get('results'):
        return None, None

    # Use first quote in the window (right at entry time)
    q = data['results'][0]
    bid = q.get('bid_price')
    ask = q.get('ask_price')
    if bid is None or ask is None:
        return None, None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    return float(bid), float(ask)

def compute_real_fill_pnl(date, entry_time, short_offset, long_offset, risk_budget=50000):
    """
    Compute real P&L using bid/ask fill prices.
    Entry: sell short legs at BID, buy long legs at ASK.
    Exit: hold to expiry (intrinsic value from SPX close).
    Returns dict with pnl and fill details, or None if data missing.
    """
    spx_price = find_spx_at(date, entry_time)
    if spx_price is None:
        return {'status': 'missing_spx'}

    atm   = get_atm(spx_price)
    width = long_offset - short_offset

    # Tickers
    sc_ticker = make_ticker(date, 'C', atm + short_offset)
    lc_ticker = make_ticker(date, 'C', atm + long_offset)
    sp_ticker = make_ticker(date, 'P', atm - short_offset)
    lp_ticker = make_ticker(date, 'P', atm - long_offset)

    # Pull quotes concurrently
    results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            ex.submit(pull_quote_at_time, date, sc_ticker, entry_time): 'sc',
            ex.submit(pull_quote_at_time, date, lc_ticker, entry_time): 'lc',
            ex.submit(pull_quote_at_time, date, sp_ticker, entry_time): 'sp',
            ex.submit(pull_quote_at_time, date, lp_ticker, entry_time): 'lp',
        }
        for fut in as_completed(futs):
            key = futs[fut]
            results[key] = fut.result()  # (bid, ask)

    sc_bid, sc_ask = results.get('sc', (None, None))
    lc_bid, lc_ask = results.get('lc', (None, None))
    sp_bid, sp_ask = results.get('sp', (None, None))
    lp_bid, lp_ask = results.get('lp', (None, None))

    # Check all quotes available
    if any(v is None for v in [sc_bid, lc_ask, sp_bid, lp_ask]):
        missing = [k for k,(b,a) in [('sc',(sc_bid,sc_ask)),('lc',(lc_bid,lc_ask)),
                                       ('sp',(sp_bid,sp_ask)),('lp',(lp_bid,lp_ask))]
                   if b is None or a is None]
        return {'status': f'missing_quotes:{missing}'}

    # Real fill prices (sell at bid, buy at ask)
    call_credit = sc_bid  - lc_ask   # what we actually receive for bear call spread
    put_credit  = sp_bid  - lp_ask   # what we actually receive for bull put spread
    total_credit = call_credit + put_credit

    if total_credit <= 0:
        return {'status': f'no_credit:{total_credit:.3f}'}

    max_risk_per = (width - total_credit) * 100
    if max_risk_per <= 0:
        return {'status': 'negative_risk'}

    # SPX closing price for expiry value
    day_spx = spx_bars.get(date, {})
    spx_close = None
    for t in ['15:59','15:58','15:57','15:56','15:55','15:50','15:45']:
        b = day_spx.get(t)
        if b: spx_close = b['c']; break
    if spx_close is None:
        return {'status': 'missing_spx_close'}

    # P&L at expiry (intrinsic)
    call_val = max(0, min(spx_close - (atm + short_offset), width))
    put_val  = max(0, min((atm - short_offset) - spx_close,  width))
    pnl_per  = (total_credit - call_val - put_val) * 100

    qty     = max(1, int(risk_budget / max_risk_per))
    total_pnl = pnl_per * qty

    return {
        'status': 'ok',
        'date': date,
        'spx_at_entry': round(spx_price, 2),
        'atm': atm,
        'entry_time': entry_time,
        'sc_bid': round(sc_bid, 3), 'lc_ask': round(lc_ask, 3),
        'sp_bid': round(sp_bid, 3), 'lp_ask': round(lp_ask, 3),
        'call_credit': round(call_credit, 3),
        'put_credit':  round(put_credit, 3),
        'total_credit': round(total_credit, 3),
        'max_risk_per': round(max_risk_per, 2),
        'spx_close': round(spx_close, 2),
        'call_val_at_expiry': round(call_val, 3),
        'put_val_at_expiry':  round(put_val, 3),
        'pnl_per_spread': round(pnl_per, 2),
        'qty': qty,
        'total_pnl': round(total_pnl, 2),
        'is_win': total_pnl > 0,
        'width': width,
        'short_offset': short_offset,
        'long_offset': long_offset,
    }

# ══════════════════════════════════════════════════════════════════════════════
# Build signal day lists
# ══════════════════════════════════════════════════════════════════════════════
n16_days, n17_days, n18_days = [], [], []

for _, row in df.iterrows():
    date = row['date']
    fc = phoenix_fire_count(row)
    vvix_val = row['vvix']

    # N16: Tuesday + Phoenix
    if row['dow'] == 'Tuesday' and fc > 0:
        n16_days.append(date)

    # N17: Phoenix + VVIX<100
    if fc > 0 and not pd.isna(vvix_val) and vvix_val < 100:
        n17_days.append(date)

    # N18: 3-Laws
    if (row.get('prior_5d_return', -99) > 1.0 and
        row.get('vp_ratio', 99) < 2.0 and
        row.get('prior_day_direction', 'FLAT') != 'FLAT'):
        n18_days.append(date)

log(f"Signal days: N16={len(n16_days)}  N17={len(n17_days)}  N18={len(n18_days)}")
log(f"Total quote pulls needed: ~{(len(n16_days)+len(n17_days)+len(n18_days))*4:,}")

# Load existing cache if any
real_fills = {}
if os.path.exists(OUT_FILE):
    with open(OUT_FILE) as f: real_fills = json.load(f)
    log(f"Loaded cache: {sum(len(v) for v in real_fills.values())} existing results")

for strat_name, days, entry_time, short_off, long_off, risk_bud in [
    ('N16', n16_days, '11:00', 25, 40, 50000),
    ('N17', n17_days, '13:00', 25, 40, 50000),
    ('N18', n18_days, '14:00', 10, 20, 50000),
]:
    if strat_name not in real_fills:
        real_fills[strat_name] = {}

    missing = [d for d in days if d not in real_fills[strat_name]]
    log(f"\n{'='*70}")
    log(f"{strat_name}: {len(days)} signal days, {len(missing)} need pulling")
    log(f"Entry: {entry_time}  Wings: ±{short_off}/±{long_off}  Risk: ${risk_bud:,}")
    log(f"{'='*70}")

    ok_count = miss_count = skip_count = 0
    for i, date in enumerate(missing):
        result = compute_real_fill_pnl(date, entry_time, short_off, long_off, risk_bud)
        real_fills[strat_name][date] = result

        if result['status'] == 'ok':
            ok_count += 1
        elif 'missing' in result['status'] or 'no_credit' in result['status']:
            miss_count += 1
        else:
            skip_count += 1

        if (i+1) % 20 == 0 or i == len(missing)-1:
            log(f"  [{i+1}/{len(missing)}] {date}: {result['status']} | ok={ok_count} miss={miss_count}")
            with open(OUT_FILE, 'w') as f: json.dump(real_fills, f, indent=2)

    with open(OUT_FILE, 'w') as f: json.dump(real_fills, f, indent=2)
    total_ok = sum(1 for v in real_fills[strat_name].values() if v.get('status')=='ok')
    total_miss = sum(1 for v in real_fills[strat_name].values() if v.get('status')!='ok')
    miss_pct = total_miss / len(days) * 100 if days else 0
    log(f"  {strat_name} complete: {total_ok} trades, {total_miss} missing ({miss_pct:.1f}%)")
    if miss_pct > 5:
        log(f"  *** WARNING: {miss_pct:.1f}% missing exceeds CLAUDE.md 5% threshold ***")
        log(f"  Missing detail: {[d for d,v in real_fills[strat_name].items() if v.get('status')!='ok'][:10]}")

save_log()
log(f"\nDone. Saved to {OUT_FILE} and {LOG_FILE}")
print(f"\nAll done. {sum(1 for s in real_fills.values() for v in s.values() if v.get('status')=='ok')} total valid fills.")
