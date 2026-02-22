#!/usr/bin/env python3
"""
Monte Carlo Bootstrap Analysis for V3 Iron Butterfly Strategy
─────────────────────────────────────────────────────────────
Resamples the 161 actual trade P&Ls 10,000 times to build
confidence intervals for PF, Total P&L, Sharpe, Max DD, Win Rate.
Also runs:
  - Binomial test on win rate
  - Permutation test on mean P&L
  - Time-stability analysis (rolling windows)
  - Ruin probability estimation
"""

import csv, json, math, random, statistics
from collections import defaultdict

random.seed(42)
N_SIMS = 10_000

# ── Load daily P&Ls from equity curve (top1 = winner config) ──
daily_pnls = []
with open('ensemble_v3_equity.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        v = float(row['top1_daily'])
        if v != 0.0:
            daily_pnls.append(v)

print(f"Loaded {len(daily_pnls)} non-zero trade days")
print(f"Actual total: ${sum(daily_pnls):,.0f}")
print(f"Actual avg: ${statistics.mean(daily_pnls):,.0f}")
print(f"Actual win rate: {sum(1 for p in daily_pnls if p > 0)/len(daily_pnls)*100:.1f}%")
winners = [p for p in daily_pnls if p > 0]
losers = [p for p in daily_pnls if p <= 0]
actual_pf = sum(winners) / abs(sum(losers)) if losers else float('inf')
print(f"Actual PF: {actual_pf:.2f}")
print(f"Actual max DD: ${min_dd(daily_pnls):,.0f}" if False else "")

def max_drawdown(pnls):
    """Compute max drawdown from a sequence of daily P&Ls."""
    cum = 0
    peak = 0
    dd = 0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        if cum - peak < dd:
            dd = cum - peak
    return dd

def sharpe(pnls):
    if len(pnls) < 2:
        return 0
    mu = statistics.mean(pnls)
    sd = statistics.stdev(pnls)
    if sd == 0:
        return 0
    # Annualize: ~252 trading days
    return (mu / sd) * math.sqrt(252)

def profit_factor(pnls):
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    if gross_loss == 0:
        return 99.0
    return gross_win / gross_loss

actual_dd = max_drawdown(daily_pnls)
actual_sharpe = sharpe(daily_pnls)
actual_total = sum(daily_pnls)
actual_wr = sum(1 for p in daily_pnls if p > 0) / len(daily_pnls)

print(f"Actual max DD: ${actual_dd:,.0f}")
print(f"Actual Sharpe: {actual_sharpe:.2f}")

# ══════════════════════════════════════════════════════════════
# 1. MONTE CARLO BOOTSTRAP — resample trades with replacement
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  MONTE CARLO BOOTSTRAP — {N_SIMS:,} simulations")
print(f"{'='*70}")

sim_totals = []
sim_pfs = []
sim_sharpes = []
sim_dds = []
sim_wrs = []
sim_calmars = []

n = len(daily_pnls)
for _ in range(N_SIMS):
    sample = random.choices(daily_pnls, k=n)
    sim_totals.append(sum(sample))
    sim_pfs.append(profit_factor(sample))
    sim_sharpes.append(sharpe(sample))
    sim_dds.append(max_drawdown(sample))
    sim_wrs.append(sum(1 for p in sample if p > 0) / n)
    dd = max_drawdown(sample)
    sim_calmars.append(sum(sample) / abs(dd) if dd != 0 else 99)

def percentile(data, pct):
    s = sorted(data)
    idx = int(len(s) * pct / 100)
    idx = min(idx, len(s) - 1)
    return s[idx]

def ci(data, lo=5, hi=95):
    return percentile(data, lo), percentile(data, hi)

print(f"\n  Metric               Actual      5th %ile    Median     95th %ile")
print(f"  {'─'*70}")

metrics = [
    ("Total P&L",     actual_total,  sim_totals,  "${:>12,.0f}"),
    ("Profit Factor", actual_pf,     sim_pfs,     "{:>12.2f}   "),
    ("Sharpe Ratio",  actual_sharpe, sim_sharpes, "{:>12.2f}   "),
    ("Win Rate",      actual_wr*100, [w*100 for w in sim_wrs], "{:>11.1f}%  "),
    ("Max Drawdown",  actual_dd,     sim_dds,     "${:>12,.0f}"),
    ("Calmar Ratio",  actual_total/abs(actual_dd) if actual_dd!=0 else 99, sim_calmars, "{:>12.2f}   "),
]

for name, actual, sims, fmt in metrics:
    lo, hi = ci(sims)
    med = percentile(sims, 50)
    actual_s = fmt.format(actual)
    lo_s = fmt.format(lo)
    med_s = fmt.format(med)
    hi_s = fmt.format(hi)
    print(f"  {name:<18} {actual_s}  {lo_s}  {med_s}  {hi_s}")

# Probability of loss
prob_loss = sum(1 for t in sim_totals if t <= 0) / N_SIMS * 100
print(f"\n  Probability of total loss (P&L ≤ 0): {prob_loss:.2f}%")

# Probability PF < 1
prob_pf_below1 = sum(1 for p in sim_pfs if p < 1.0) / N_SIMS * 100
print(f"  Probability PF < 1.0: {prob_pf_below1:.2f}%")

# ══════════════════════════════════════════════════════════════
# 2. BINOMIAL TEST — is 55.3% win rate significant vs 50%?
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  BINOMIAL TEST")
print(f"{'='*70}")

n_wins = sum(1 for p in daily_pnls if p > 0)
n_total = len(daily_pnls)

# Compute p-value using normal approximation to binomial
# H0: p = 0.5, H1: p > 0.5
p0 = 0.5
z = (n_wins/n_total - p0) / math.sqrt(p0 * (1-p0) / n_total)
# One-tailed p-value from z-score (approximation)
# Using survival function approximation
p_value = 0.5 * math.erfc(z / math.sqrt(2))

print(f"  Winners: {n_wins}/{n_total} = {n_wins/n_total*100:.1f}%")
print(f"  H0: true win rate = 50%")
print(f"  Z-score: {z:.3f}")
print(f"  One-tailed p-value: {p_value:.6f}")
if p_value < 0.01:
    print(f"  → Highly significant (p < 0.01)")
elif p_value < 0.05:
    print(f"  → Significant (p < 0.05)")
else:
    print(f"  → Not significant at 95% level")

# Also test vs breakeven win rate (where PF=1 given avg win/loss ratio)
avg_win = statistics.mean(winners) if winners else 0
avg_loss = abs(statistics.mean(losers)) if losers else 1
breakeven_wr = avg_loss / (avg_win + avg_loss)
z_be = (n_wins/n_total - breakeven_wr) / math.sqrt(breakeven_wr * (1-breakeven_wr) / n_total)
p_be = 0.5 * math.erfc(z_be / math.sqrt(2))
print(f"\n  Breakeven win rate (given avg W/L ratio): {breakeven_wr*100:.1f}%")
print(f"  Z-score vs breakeven: {z_be:.3f}")
print(f"  One-tailed p-value: {p_be:.6f}")
if p_be < 0.01:
    print(f"  → Highly significant (p < 0.01)")
elif p_be < 0.05:
    print(f"  → Significant (p < 0.05)")
else:
    print(f"  → Not significant at 95% level")

# ══════════════════════════════════════════════════════════════
# 3. PERMUTATION TEST — is mean P&L significantly > 0?
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  PERMUTATION TEST — Mean P&L")
print(f"{'='*70}")

actual_mean = statistics.mean(daily_pnls)
n_perms = 50_000
count_ge = 0
for _ in range(n_perms):
    # Randomly flip signs
    perm = [p * random.choice([-1, 1]) for p in daily_pnls]
    if statistics.mean(perm) >= actual_mean:
        count_ge += 1

perm_p = count_ge / n_perms
print(f"  Actual mean P&L: ${actual_mean:,.0f}")
print(f"  Permutation p-value ({n_perms:,} permutations): {perm_p:.6f}")
if perm_p < 0.01:
    print(f"  → Highly significant (p < 0.01)")
elif perm_p < 0.05:
    print(f"  → Significant (p < 0.05)")
else:
    print(f"  → Not significant at 95% level")

# ══════════════════════════════════════════════════════════════
# 4. ROLLING STABILITY — 50-trade windows
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  ROLLING WINDOW STABILITY (50-trade windows)")
print(f"{'='*70}")

window = 50
if len(daily_pnls) >= window:
    roll_pfs = []
    roll_wrs = []
    roll_sharps = []
    for i in range(len(daily_pnls) - window + 1):
        chunk = daily_pnls[i:i+window]
        roll_pfs.append(profit_factor(chunk))
        roll_wrs.append(sum(1 for p in chunk if p > 0) / window)
        roll_sharps.append(sharpe(chunk))

    print(f"  Windows: {len(roll_pfs)}")
    print(f"  PF range: {min(roll_pfs):.2f} → {max(roll_pfs):.2f}  (mean: {statistics.mean(roll_pfs):.2f})")
    print(f"  WR range: {min(roll_wrs)*100:.0f}% → {max(roll_wrs)*100:.0f}%  (mean: {statistics.mean(roll_wrs)*100:.0f}%)")
    print(f"  Sharpe range: {min(roll_sharps):.2f} → {max(roll_sharps):.2f}")
    pf_below_1 = sum(1 for p in roll_pfs if p < 1.0)
    print(f"  Windows with PF < 1.0: {pf_below_1}/{len(roll_pfs)} ({pf_below_1/len(roll_pfs)*100:.0f}%)")

# ══════════════════════════════════════════════════════════════
# 5. RUIN PROBABILITY — chance of hitting -X drawdown
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  RUIN PROBABILITY (drawdown thresholds)")
print(f"{'='*70}")

thresholds = [50_000, 100_000, 150_000, 200_000, 300_000, 500_000]
ruin_counts = {t: 0 for t in thresholds}

for _ in range(N_SIMS):
    sample = random.choices(daily_pnls, k=n)
    dd = max_drawdown(sample)
    for t in thresholds:
        if abs(dd) >= t:
            ruin_counts[t] += 1

print(f"  {'Drawdown Threshold':<22} {'Probability':>12}")
print(f"  {'─'*36}")
for t in thresholds:
    prob = ruin_counts[t] / N_SIMS * 100
    print(f"  ≥ ${t:>10,}         {prob:>10.1f}%")

# ══════════════════════════════════════════════════════════════
# 6. CONFLUENCE DEPTH ANALYSIS — bootstrap by depth
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  CONFLUENCE DEPTH — Bootstrap by Signal Count")
print(f"{'='*70}")

# Load calendar data to get signal counts per trade
with open('calendar_data_v3.json') as f:
    cal_data = json.load(f)

# The calendar data has 112 trades, V3 backtest has 161
# We'll note the calendar subsample statistics
depth_pnls = defaultdict(list)
for trade in cal_data:
    ns = trade.get('ns', 1)
    # cp values appear to be in thousands based on the scale
    # (risk_used 50000 → cp of ~13 means cp is in $K)
    depth_pnls[ns].append(trade['cp'] * 1000)  # scale to dollars

print(f"\n  Calendar trades (112) by depth:")
for depth in sorted(depth_pnls.keys()):
    pnls = depth_pnls[depth]
    w = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    pf = profit_factor(pnls)
    print(f"  {depth} signal(s): {len(pnls):>3} trades, WR {w/len(pnls)*100:.0f}%, PF {pf:.2f}, Total ${total:,.0f}")

    # Bootstrap this depth
    if len(pnls) >= 5:
        boot_pfs = []
        for _ in range(5000):
            s = random.choices(pnls, k=len(pnls))
            boot_pfs.append(profit_factor(s))
        lo5, hi95 = ci(boot_pfs)
        below1 = sum(1 for p in boot_pfs if p < 1) / 5000 * 100
        print(f"         Bootstrap PF: [{lo5:.2f}, {hi95:.2f}] — P(PF<1): {below1:.1f}%")

# ══════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print(f"  STATISTICAL SIGNIFICANCE SUMMARY")
print(f"{'='*70}")
print(f"""
  ┌─────────────────────────────────────────────────────────────────┐
  │ Test                    │ Result            │ Verdict           │
  ├─────────────────────────┼───────────────────┼───────────────────┤
  │ Bootstrap P(loss)       │ {prob_loss:>6.2f}%           │ {'STRONG' if prob_loss < 5 else 'WEAK':>17} │
  │ Bootstrap P(PF<1)       │ {prob_pf_below1:>6.2f}%           │ {'STRONG' if prob_pf_below1 < 5 else 'WEAK':>17} │
  │ Binomial (WR>50%)       │ p={p_value:>8.5f}     │ {'SIGNIFICANT' if p_value < 0.05 else 'NOT SIG':>17} │
  │ Binomial (WR>BE)        │ p={p_be:>8.5f}     │ {'SIGNIFICANT' if p_be < 0.05 else 'NOT SIG':>17} │
  │ Permutation (μ>0)       │ p={perm_p:>8.5f}     │ {'SIGNIFICANT' if perm_p < 0.05 else 'NOT SIG':>17} │
  │ Rolling PF stability    │ {pf_below_1}/{len(roll_pfs)} windows <1│ {'STABLE' if pf_below_1/len(roll_pfs) < 0.3 else 'UNSTABLE':>17} │
  │ 5th %ile Total P&L      │ ${percentile(sim_totals, 5):>12,.0f}│ {'PROFITABLE' if percentile(sim_totals, 5) > 0 else 'AT RISK':>17} │
  │ 5th %ile PF             │ {percentile(sim_pfs, 5):>12.2f}   │ {'EDGE EXISTS' if percentile(sim_pfs, 5) > 1 else 'UNCERTAIN':>17} │
  └─────────────────────────┴───────────────────┴───────────────────┘

  Overall Assessment:
  With 161 trades, the strategy shows {"statistically significant edge" if prob_loss < 5 and p_value < 0.05 else "borderline significance"}.
  Even at the pessimistic 5th percentile, {"the strategy remains profitable" if percentile(sim_totals, 5) > 0 else "the strategy could lose money"}.

  Recommended next step: Out-of-sample validation on held-out data.
""")
