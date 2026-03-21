"""
research/stats.py — Statistical analysis and validation.

Implements:
  - Trade-level statistics (win rate, PF, Sharpe, drawdown)
  - t-test on dollar P&L (per CLAUDE.md: the correct test)
  - Bootstrap confidence intervals
  - Walk-forward out-of-sample validation
  - Multiple comparison correction (Bonferroni, Holm)
  - Monthly/yearly breakdowns
  - Correlation between strategy P&L streams
"""

import math
import statistics as pystats
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from research.exits import TradeResult


# ─────────────────────────────────────────────────────────────────────────────
# CORE STATISTICS
# ─────────────────────────────────────────────────────────────────────────────

def calc_stats(trades: List[TradeResult], label: str = "") -> Optional[Dict]:
    """
    Compute comprehensive statistics from a list of TradeResults.
    Uses dollar P&L for the t-test (per CLAUDE.md).
    """
    if not trades:
        return None

    pnls = [t.pnl_per_spread for t in trades]
    dollars = [t.pnl_dollar for t in trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    total_dollar = sum(dollars)
    avg_pnl = total_pnl / n
    avg_dollar = total_dollar / n

    win_rate = len(wins) / n * 100
    avg_win = pystats.mean(wins) if wins else 0
    avg_loss = pystats.mean(losses) if losses else 0
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    pf = gross_wins / gross_losses if gross_losses > 0 else float('inf')

    # Max drawdown on cumulative P&L
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    # Max drawdown in dollars
    cum_d = 0
    peak_d = 0
    max_dd_dollar = 0
    for d in dollars:
        cum_d += d
        peak_d = max(peak_d, cum_d)
        max_dd_dollar = min(max_dd_dollar, cum_d - peak_d)

    # t-test on dollar P&L (H0: mean = 0)
    t_stat, p_val = 0, 1.0
    if n > 1:
        se = pystats.stdev(dollars) / math.sqrt(n)
        if se > 0:
            t_stat = avg_dollar / se
            from math import erfc
            p_val = erfc(abs(t_stat) / math.sqrt(2))  # two-tailed

    # Sharpe-like ratio (annualized, assuming ~252 trading days)
    if n > 1 and pystats.stdev(pnls) > 0:
        sharpe = (avg_pnl / pystats.stdev(pnls)) * math.sqrt(min(n, 252))
    else:
        sharpe = 0

    # Calmar ratio
    calmar = total_dollar / abs(max_dd_dollar) if max_dd_dollar < 0 else float('inf')

    # Exit type breakdown
    exit_counts = defaultdict(int)
    for t in trades:
        exit_counts[t.exit_type] += 1

    return {
        'label': label,
        'n': n,
        'win_rate': round(win_rate, 1),
        'avg_pnl': round(avg_pnl, 4),
        'avg_dollar': round(avg_dollar, 2),
        'total_pnl': round(total_pnl, 2),
        'total_dollar': round(total_dollar, 2),
        'avg_win': round(avg_win, 4),
        'avg_loss': round(avg_loss, 4),
        'pf': round(pf, 2),
        'max_dd': round(max_dd, 4),
        'max_dd_dollar': round(max_dd_dollar, 2),
        't_stat': round(t_stat, 2),
        'p_val': round(p_val, 6),
        'sharpe': round(sharpe, 2),
        'calmar': round(calmar, 2),
        'exit_counts': dict(exit_counts),
    }


def fmt_stats(s: Optional[Dict]) -> str:
    """One-line summary of stats dict."""
    if not s:
        return "NO DATA"
    return (f"n={s['n']}  WR={s['win_rate']}%  avg=${s['avg_dollar']:.0f}  "
            f"PF={s['pf']:.2f}  total=${s['total_dollar']:,.0f}  "
            f"DD=${s['max_dd_dollar']:,.0f}  t={s['t_stat']:.2f}  p={s['p_val']:.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY / YEARLY BREAKDOWNS
# ─────────────────────────────────────────────────────────────────────────────

def monthly_breakdown(trades: List[TradeResult]) -> Dict[str, Dict]:
    """Group trades by month and compute stats per month."""
    by_month = defaultdict(list)
    for t in trades:
        by_month[t.date[:7]].append(t)
    return {m: calc_stats(ts, label=m) for m, ts in sorted(by_month.items())}


def yearly_breakdown(trades: List[TradeResult]) -> Dict[str, Dict]:
    """Group trades by year."""
    by_year = defaultdict(list)
    for t in trades:
        by_year[t.date[:4]].append(t)
    return {y: calc_stats(ts, label=y) for y, ts in sorted(by_year.items())}


def day_of_week_breakdown(trades: List[TradeResult]) -> Dict[str, Dict]:
    """Group trades by day of week."""
    dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    by_dow = defaultdict(list)
    for t in trades:
        dt = datetime.strptime(t.date, '%Y-%m-%d')
        by_dow[dow_names[dt.weekday()]].append(t)
    return {d: calc_stats(by_dow[d], label=d) for d in dow_names if by_dow[d]}


# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP CONFIDENCE INTERVALS
# ─────────────────────────────────────────────────────────────────────────────

def bootstrap_ci(trades: List[TradeResult], n_boot: int = 10000,
                 ci: float = 0.95, stat_fn=None) -> Tuple[float, float, float]:
    """
    Bootstrap confidence interval for mean dollar P&L.

    Returns: (mean, lower_bound, upper_bound)
    """
    import random
    if not trades:
        return (0, 0, 0)

    dollars = [t.pnl_dollar for t in trades]
    if stat_fn is None:
        stat_fn = lambda x: sum(x) / len(x)

    boot_stats = []
    n = len(dollars)
    for _ in range(n_boot):
        sample = [random.choice(dollars) for _ in range(n)]
        boot_stats.append(stat_fn(sample))

    boot_stats.sort()
    alpha = (1 - ci) / 2
    lo_idx = int(alpha * n_boot)
    hi_idx = int((1 - alpha) * n_boot)
    mean = stat_fn(dollars)

    return (round(mean, 2), round(boot_stats[lo_idx], 2), round(boot_stats[hi_idx], 2))


# ─────────────────────────────────────────────────────────────────────────────
# WALK-FORWARD VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def walk_forward_split(trades: List[TradeResult],
                       train_months: int = 6,
                       test_months: int = 2) -> List[Tuple[List, List]]:
    """
    Generate rolling walk-forward train/test splits.
    Returns list of (train_trades, test_trades) tuples.

    Each split: train on [T, T+train_months), test on [T+train_months, T+train_months+test_months)
    Roll forward by test_months each time.
    """
    if not trades:
        return []

    # Group by month
    by_month = defaultdict(list)
    for t in trades:
        by_month[t.date[:7]].append(t)
    months = sorted(by_month.keys())

    splits = []
    i = 0
    while i + train_months + test_months <= len(months):
        train_months_list = months[i:i + train_months]
        test_months_list = months[i + train_months:i + train_months + test_months]

        train = []
        for m in train_months_list:
            train.extend(by_month[m])
        test = []
        for m in test_months_list:
            test.extend(by_month[m])

        if train and test:
            splits.append((train, test))
        i += test_months

    return splits


def half_split(trades: List[TradeResult]) -> Tuple[List, List]:
    """Simple chronological 50/50 split."""
    mid = len(trades) // 2
    return trades[:mid], trades[mid:]


# ─────────────────────────────────────────────────────────────────────────────
# MULTIPLE COMPARISON CORRECTION
# ─────────────────────────────────────────────────────────────────────────────

def bonferroni_threshold(n_tests: int, alpha: float = 0.05) -> float:
    """Bonferroni-corrected significance threshold."""
    return alpha / n_tests


def holm_bonferroni(p_values: List[Tuple[str, float]], alpha: float = 0.05) -> List[Tuple[str, float, bool]]:
    """
    Holm-Bonferroni step-down procedure.
    More powerful than Bonferroni while still controlling FWER.

    Args:
        p_values: list of (label, p_value) tuples
        alpha: family-wise error rate

    Returns:
        list of (label, p_value, significant) tuples, sorted by p_value
    """
    sorted_pvals = sorted(p_values, key=lambda x: x[1])
    n = len(sorted_pvals)
    results = []
    rejected = True
    for i, (label, p) in enumerate(sorted_pvals):
        threshold = alpha / (n - i)
        if rejected and p <= threshold:
            results.append((label, p, True))
        else:
            rejected = False
            results.append((label, p, False))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY CORRELATION
# ─────────────────────────────────────────────────────────────────────────────

def daily_pnl_correlation(trades_a: List[TradeResult],
                          trades_b: List[TradeResult]) -> Optional[float]:
    """
    Pearson correlation between daily P&L of two strategies.
    Only uses dates where BOTH strategies have trades.
    """
    pnl_a = {t.date: t.pnl_dollar for t in trades_a}
    pnl_b = {t.date: t.pnl_dollar for t in trades_b}

    common_dates = sorted(set(pnl_a.keys()) & set(pnl_b.keys()))
    if len(common_dates) < 5:
        return None

    xs = [pnl_a[d] for d in common_dates]
    ys = [pnl_b[d] for d in common_dates]

    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def overlap_analysis(trades_a: List[TradeResult],
                     trades_b: List[TradeResult]) -> Dict:
    """
    Analyze date overlap between two strategies.
    Per CLAUDE.md: V3/N15 double-counting is a known risk.
    """
    dates_a = set(t.date for t in trades_a)
    dates_b = set(t.date for t in trades_b)
    overlap = dates_a & dates_b

    return {
        'n_a': len(dates_a),
        'n_b': len(dates_b),
        'overlap': len(overlap),
        'overlap_pct_a': round(len(overlap) / len(dates_a) * 100, 1) if dates_a else 0,
        'overlap_pct_b': round(len(overlap) / len(dates_b) * 100, 1) if dates_b else 0,
    }
