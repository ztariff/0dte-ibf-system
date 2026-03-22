#!/usr/bin/env python3
"""
Sizing Score Backtest
=====================
Builds a composite sizing score per trade per strategy using the significant
factors identified in sizing_factor_research.py. Then backtests the impact
of score-based sizing vs the original flat/tiered sizing.

Approach:
  - Each strategy gets its own scoring rubric (factors + weights)
  - Each historical trade is scored → mapped to a sizing tier (25/50/75/100%)
  - P&L is scaled proportionally: new_pnl = original_pnl * (new_size / original_size)
  - Compare: total P&L, max drawdown, Sharpe, return-to-drawdown, win rate

Caveat: factors were discovered on THIS dataset — in-sample optimization.
Real edge must be validated on forward data.
"""

import os, json, csv, math
from collections import defaultdict

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load data ──────────────────────────────────────────────────────

def load_trades():
    with open(os.path.join(_DIR, "strategy_trades.json")) as f:
        return json.load(f)

def load_csv():
    lookup = {}
    with open(os.path.join(_DIR, "research_all_trades.csv")) as f:
        for row in csv.DictReader(f):
            lookup[row["date"]] = row
    return lookup

def load_gap_cache():
    with open(os.path.join(_DIR, "spx_gap_cache.json")) as f:
        return json.load(f)

def load_vix9d():
    with open(os.path.join(_DIR, "vix9d_daily.json")) as f:
        return json.load(f)


# ── Per-strategy scoring rubrics ───────────────────────────────────
# Each factor function returns a score contribution.
# Total score maps to sizing tier.

def score_v3(t):
    """PHOENIX scoring rubric.
    Significant factors (p<0.10):
      - Prior Day Dir: DOWN=+3, FLAT=0, UP=-1          (p=0.014)
      - Prior Day |Ret|: <0.3%=+2, 0.3-0.7%=-2, else 0 (p=0.022)
      - Fire Count: 4-5=+3, 3=+2, 2=-1, 1=0            (p=0.022)
      - RV Level: >18=+2, 12-18=0, <12=-1               (p=0.065)
      - Day of Week: Wed=+1, Tue=-2                      (p=0.061)
    """
    s = 0
    # Prior day direction
    pd = t.get("prior_dir", "")
    if pd == "DOWN":     s += 3
    elif pd == "UP":     s -= 1

    # Prior day |return|
    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:     s += 2
        elif ar < 0.7:   s -= 2

    # Fire count
    fc = t.get("fire_count", 0)
    if fc >= 4:    s += 3
    elif fc == 3:  s += 2
    elif fc == 2:  s -= 1

    # RV level
    rv = t.get("rv", 0)
    if rv > 18:    s += 2
    elif rv < 12:  s -= 1

    # Day of week
    dow = t.get("dow", "")
    if dow == "Wednesday":  s += 1
    elif dow == "Tuesday":  s -= 2

    return s


def score_n15(t):
    """PHOENIX CLEAR scoring rubric.
    Significant factors:
      - Prior Day Dir: DOWN=+3, UP=-2                   (p=0.014)
      - Day of Week: Wed/Fri=+1, Tue=-3                 (p=0.034)
      - RV Slope: FALLING=+1, RISING=-1                 (marginal)
    """
    s = 0
    pd = t.get("prior_dir", "")
    if pd == "DOWN":     s += 3
    elif pd == "UP":     s -= 2

    dow = t.get("dow", "")
    if dow in ("Wednesday", "Friday"):  s += 1
    elif dow == "Tuesday":              s -= 3

    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":   s += 1
    elif rvs == "RISING":  s -= 1

    return s


def score_v6(t):
    """QUIET REBOUND scoring rubric (n=16, low confidence).
    Directional factors:
      - VP ratio: <1.0=+3, 1.0-1.3=+1, 1.3-1.7=-1
      - Prior Day |Ret|: <0.3%=+2, else -1               (p=0.062)
      - RV Slope: RISING=+1 (counterintuitive but data says so)
    """
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp < 1.0:     s += 3
    elif vp < 1.3:   s += 1
    elif vp < 1.7:   s -= 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        if abs(p1d) < 0.3:  s += 2
        else:                s -= 1

    rvs = t.get("rv_slope", "")
    if rvs == "RISING":   s += 1
    elif rvs == "STABLE": s -= 1

    return s


def score_v9(t):
    """BREAKOUT STALL scoring rubric (n=20).
    Significant factors:
      - VP 1.3-1.7=+2, >1.7=-2                          (p=0.076)
      - Prior Day |Ret|: 0.7-1.2%=+2, 0.3-0.7%=-2       (p=0.039)
      - RV Slope: FALLING=+2, STABLE=-1                  (p=0.087)
      - Day of Week: Thu=+1, Wed=-1                       (directional)
    """
    s = 0
    vp = t.get("vp_ratio", 999)
    if 1.3 <= vp < 1.7:  s += 2
    elif vp >= 1.7:       s -= 2

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if 0.7 <= ar < 1.2:   s += 2
        elif 0.3 <= ar < 0.7: s -= 2

    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":   s += 2
    elif rvs == "STABLE":  s -= 1

    dow = t.get("dow", "")
    if dow == "Thursday":    s += 1
    elif dow == "Wednesday": s -= 1

    return s


def score_v12(t):
    """BULL SQUEEZE scoring rubric (n=11, very low confidence).
    Directional only:
      - VP 1.3-1.7=+1, 1.0-1.3=-2
      - 5dRet > 1.5%=+1
      - Prior Day |Ret|: <0.3%=+1, 0.3-0.7%=-2
    """
    s = 0
    vp = t.get("vp_ratio", 999)
    if 1.3 <= vp < 1.7:  s += 1
    elif 1.0 <= vp < 1.3: s -= 2

    r5d = t.get("prior_5d", 0)
    if r5d > 1.5: s += 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:     s += 1
        elif ar < 0.7:   s -= 2

    return s


def score_n17(t):
    """AFTERNOON LOCK scoring rubric (n=82).
    Significant factors:
      - RV Slope: FALLING=+3, STABLE=+1, RISING=-3      (p=0.008)
      - Day of Week: Wed/Thu=+2, Mon/Fri=-2              (p=0.030)
      - Term Structure: FLAT=+1, CONTANGO=-1             (p=0.049)
      - Gap DOWN=+1, Gap UP=-1                           (p=0.071)
      - Out of Prior Wk Range=+1, In Range=-1            (p=0.069)
    """
    s = 0
    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":   s += 3
    elif rvs == "STABLE":  s += 1
    elif rvs == "RISING":  s -= 3

    dow = t.get("dow", "")
    if dow in ("Wednesday", "Thursday"):  s += 2
    elif dow in ("Monday", "Friday"):     s -= 2

    ts = t.get("ts_label", "")
    if ts == "FLAT":       s += 1
    elif ts == "CONTANGO": s -= 1

    gap = t.get("gap_pct")
    if gap is not None:
        if gap < -0.25:   s += 1
        elif gap > 0.25:  s -= 1

    iwr = t.get("in_prior_week_range")
    if iwr is not None:
        if not iwr:  s += 1   # out of range is better
        else:        s -= 1

    return s


def score_n18(t):
    """LATE SQUEEZE scoring rubric (n=139).
    Significant factors:
      - Day of Week: Tue=+2, Wed=+1, Mon=-2             (p=0.014)
      - Prior Day Dir: UP=+1, DOWN=-1                    (directional)
      - Gap UP=+1, Gap DOWN=-1                           (directional)
      - Prior Day Range > 1.0%=+1                        (directional)
    """
    s = 0
    dow = t.get("dow", "")
    if dow == "Tuesday":     s += 2
    elif dow == "Wednesday": s += 1
    elif dow == "Monday":    s -= 2

    pd = t.get("prior_dir", "")
    if pd == "UP":    s += 1
    elif pd == "DOWN": s -= 1

    gap = t.get("gap_pct")
    if gap is not None:
        if gap > 0.25:   s += 1
        elif gap < -0.25: s -= 1

    pdr = t.get("prior_day_range")
    if pdr is not None and pdr > 1.0:
        s += 1

    return s


SCORE_FN = {
    "v3": score_v3,
    "n15": score_n15,
    "v6": score_v6,
    "v7": None,       # n=5, no reliable factors
    "v9": score_v9,
    "v12": score_v12,
    "n17": score_n17,
    "n18": score_n18,
}

# ── Score → sizing tier mapping ────────────────────────────────────
# Each strategy has its own thresholds tuned to its score distribution.
# We use quartile-based mapping: bottom 25% of scores → 25% size, etc.

def score_to_multiplier(score, thresholds):
    """Map a composite score to a sizing multiplier.
    thresholds = (t25, t50, t75) — score cutoffs for 25/50/75/100%.
    """
    t25, t50, t75 = thresholds
    if score <= t25:   return 0.25
    if score <= t50:   return 0.50
    if score <= t75:   return 0.75
    return 1.00


def compute_thresholds(scores):
    """Compute quartile boundaries from a list of scores."""
    if not scores:
        return (0, 0, 0)
    ss = sorted(scores)
    n = len(ss)
    q25 = ss[max(0, int(n * 0.25) - 1)]
    q50 = ss[max(0, int(n * 0.50) - 1)]
    q75 = ss[max(0, int(n * 0.75) - 1)]
    return (q25, q50, q75)


# ── Enrich trade with CSV data ─────────────────────────────────────

def enrich(trade, csv_lookup, gap_cache, vix9d_cache):
    dt = trade["date"]
    csv_row = csv_lookup.get(dt, {})
    t = dict(trade)
    t["rv_slope"] = csv_row.get("rv_slope", "")
    t["ts_label"] = csv_row.get("ts_label", "")
    t["prior_dir"] = trade.get("prior_dir", csv_row.get("prior_day_direction", ""))
    t["dow"] = csv_row.get("dow", "")
    t["prior_day_range"] = float(csv_row["prior_day_range"]) if csv_row.get("prior_day_range") else None
    t["in_prior_week_range"] = bool(int(csv_row["in_prior_week_range"])) if csv_row.get("in_prior_week_range") else None
    t["gap_pct"] = gap_cache.get(dt)
    vix9d = vix9d_cache.get(dt)
    vix = trade.get("vix")
    t["vix9d_ratio"] = (vix9d / vix) if (vix9d and vix and vix > 0) else None
    return t


# ── P&L scaling logic ──────────────────────────────────────────────

def scale_pnl(original_pnl, original_qty, multiplier, wing_width):
    """Scale P&L by sizing multiplier.
    We compute pnl_per_spread from original, then apply to new qty.
    new_qty = round(original_qty * multiplier), min 1.
    """
    if original_qty == 0:
        return 0, 0
    pnl_per_spread = original_pnl / original_qty
    new_qty = max(1, round(original_qty * multiplier))
    return pnl_per_spread * new_qty, new_qty


# ── Metrics ────────────────────────────────────────────────────────

def compute_metrics(pnls):
    if not pnls:
        return {}
    n = len(pnls)
    total = sum(pnls)
    mean = total / n
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / n * 100
    std = math.sqrt(sum((p - mean)**2 for p in pnls) / (n - 1)) if n > 1 else 0

    # Max drawdown (peak-to-trough on equity curve)
    equity = []
    cum = 0
    for p in pnls:
        cum += p
        equity.append(cum)
    peak = 0
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        dd = peak - e
        max_dd = max(max_dd, dd)

    sharpe = mean / std if std > 0 else 0
    rtd = total / max_dd if max_dd > 0 else float('inf')

    return {
        "n": n,
        "total_pnl": round(total),
        "mean_pnl": round(mean),
        "win_rate": round(wr, 1),
        "max_drawdown": round(max_dd),
        "sharpe": round(sharpe, 3),
        "return_to_dd": round(rtd, 2),
        "max_loss": round(min(pnls)),
        "max_win": round(max(pnls)),
    }


# ── Main backtest ──────────────────────────────────────────────────

def run():
    trades = load_trades()
    csv_lookup = load_csv()
    gap_cache = load_gap_cache()
    vix9d_cache = load_vix9d()

    surviving = {"v3", "n15", "v6", "v7", "v9", "v12", "n17", "n18"}
    trades = [t for t in trades if t["ver"] in surviving]

    enriched = [enrich(t, csv_lookup, gap_cache, vix9d_cache) for t in trades]

    strat_order = ["v3", "n15", "v6", "v7", "v9", "v12", "n17", "n18"]
    ver_names = {
        "v3": "PHOENIX", "n15": "PHOENIX CLEAR", "v6": "QUIET REBOUND",
        "v7": "FLAT-GAP FADE", "v9": "BREAKOUT STALL", "v12": "BULL SQUEEZE",
        "n17": "AFTERNOON LOCK", "n18": "LATE SQUEEZE"
    }

    all_results = {}

    for ver in strat_order:
        strat_trades = [t for t in enriched if t["ver"] == ver]
        if not strat_trades:
            continue

        name = ver_names[ver]
        score_fn = SCORE_FN.get(ver)

        # ── Original (flat) P&L ──
        orig_pnls = [t["pnl"] for t in strat_trades]
        orig_metrics = compute_metrics(orig_pnls)

        if score_fn is None:
            # No scoring for this strategy (too few trades)
            all_results[ver] = {
                "name": name,
                "note": "No scoring — insufficient data (n=%d)" % len(strat_trades),
                "original": orig_metrics,
                "scored": orig_metrics,
                "improvement": {},
            }
            print(f"\n{'='*80}")
            print(f"  {name} ({ver})  |  n={len(strat_trades)}  |  SKIPPED (too few trades)")
            print(f"{'='*80}")
            continue

        # ── Score every trade ──
        scored_trades = []
        for t in strat_trades:
            sc = score_fn(t)
            scored_trades.append((t, sc))

        scores = [sc for _, sc in scored_trades]
        thresholds = compute_thresholds(scores)

        # ── Apply sizing multipliers ──
        scored_pnls = []
        trade_details = []
        for t, sc in scored_trades:
            mult = score_to_multiplier(sc, thresholds)
            new_pnl, new_qty = scale_pnl(t["pnl"], t["qty"], mult, t.get("wing_width", 40))
            scored_pnls.append(new_pnl)
            trade_details.append({
                "date": t["date"],
                "score": sc,
                "multiplier": mult,
                "orig_pnl": t["pnl"],
                "orig_qty": t["qty"],
                "new_pnl": round(new_pnl),
                "new_qty": new_qty,
            })

        scored_metrics = compute_metrics(scored_pnls)

        # ── Compute per-tier stats ──
        tier_stats = {}
        for tier_mult in [0.25, 0.50, 0.75, 1.00]:
            tier_trades = [(t, d) for (t, _), d in zip(scored_trades, trade_details) if d["multiplier"] == tier_mult]
            if tier_trades:
                tier_pnls_orig = [t["pnl"] for t, _ in tier_trades]
                tier_pnls_new = [d["new_pnl"] for _, d in tier_trades]
                tier_stats[str(tier_mult)] = {
                    "n": len(tier_trades),
                    "avg_orig_pnl": round(sum(tier_pnls_orig) / len(tier_pnls_orig)),
                    "total_orig_pnl": round(sum(tier_pnls_orig)),
                    "avg_new_pnl": round(sum(tier_pnls_new) / len(tier_pnls_new)),
                    "total_new_pnl": round(sum(tier_pnls_new)),
                    "win_rate": round(sum(1 for p in tier_pnls_orig if p > 0) / len(tier_pnls_orig) * 100, 1),
                }

        # ── Improvement deltas ──
        improvement = {}
        for key in ["total_pnl", "max_drawdown", "sharpe", "return_to_dd"]:
            o = orig_metrics.get(key, 0)
            s = scored_metrics.get(key, 0)
            if key == "max_drawdown":
                # Reduction is good
                improvement[key] = {"original": o, "scored": s, "delta": o - s, "pct": round((o - s) / o * 100, 1) if o else 0}
            else:
                improvement[key] = {"original": o, "scored": s, "delta": s - o, "pct": round((s - o) / abs(o) * 100, 1) if o else 0}

        all_results[ver] = {
            "name": name,
            "thresholds": thresholds,
            "score_range": [min(scores), max(scores)],
            "original": orig_metrics,
            "scored": scored_metrics,
            "improvement": improvement,
            "tier_stats": tier_stats,
            "trade_details": trade_details,
        }

        # ── Print results ──
        print(f"\n{'='*80}")
        print(f"  {name} ({ver})  |  n={len(strat_trades)}")
        print(f"  Score range: [{min(scores)}, {max(scores)}]  Thresholds: {thresholds}")
        print(f"{'='*80}")

        print(f"\n  {'Metric':<20} {'Original':>12} {'Scored':>12} {'Delta':>12} {'Change':>8}")
        print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12} {'-'*8}")
        for key, label in [("total_pnl", "Total P&L"), ("max_drawdown", "Max Drawdown"),
                           ("sharpe", "Sharpe"), ("return_to_dd", "Return/DD")]:
            o = orig_metrics.get(key, 0)
            s = scored_metrics.get(key, 0)
            imp = improvement[key]
            if key in ("sharpe", "return_to_dd"):
                print(f"  {label:<20} {o:>12.3f} {s:>12.3f} {imp['delta']:>+12.3f} {imp['pct']:>+7.1f}%")
            else:
                print(f"  {label:<20} ${o:>11,} ${s:>11,} ${imp['delta']:>+11,} {imp['pct']:>+7.1f}%")
        print(f"  {'Win Rate':<20} {orig_metrics['win_rate']:>11.1f}% {scored_metrics['win_rate']:>11.1f}%")

        print(f"\n  Per-Tier Breakdown:")
        print(f"  {'Tier':<8} {'N':>4} {'WinR':>6} {'AvgPnL(orig)':>14} {'TotalPnL(orig)':>16} {'Verdict':>10}")
        print(f"  {'-'*8} {'----':>4} {'------':>6} {'-'*14} {'-'*16} {'-'*10}")
        for tier_mult in [1.00, 0.75, 0.50, 0.25]:
            ts = tier_stats.get(str(tier_mult))
            if ts:
                verdict = "SIZE UP" if ts["avg_orig_pnl"] > 0 else "SIZE DOWN"
                print(f"  {tier_mult:>6.0%}   {ts['n']:>4} {ts['win_rate']:>5.1f}% ${ts['avg_orig_pnl']:>+13,} ${ts['total_orig_pnl']:>+15,} {verdict:>10}")

    # ── Grand summary ──
    print(f"\n\n{'='*80}")
    print(f"  GRAND SUMMARY")
    print(f"{'='*80}")
    print(f"\n  {'Strategy':<20} {'Orig P&L':>12} {'Scored P&L':>12} {'Orig DD':>10} {'Scored DD':>10} {'Orig R/DD':>10} {'New R/DD':>10}")
    print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    total_orig = 0
    total_scored = 0
    for ver in strat_order:
        r = all_results.get(ver)
        if not r:
            continue
        o = r["original"]
        s = r["scored"]
        total_orig += o["total_pnl"]
        total_scored += s["total_pnl"]
        print(f"  {r['name']:<20} ${o['total_pnl']:>+11,} ${s['total_pnl']:>+11,} ${o['max_drawdown']:>9,} ${s['max_drawdown']:>9,} {o.get('return_to_dd',0):>+10.2f} {s.get('return_to_dd',0):>+10.2f}")

    print(f"  {'-'*20} {'-'*12} {'-'*12}")
    print(f"  {'TOTAL':<20} ${total_orig:>+11,} ${total_scored:>+11,}")
    pct_retained = total_scored / total_orig * 100 if total_orig else 0
    print(f"\n  P&L retained: {pct_retained:.1f}% of original")
    print(f"  (Scored sizing uses 25-100% of capital vs 100% flat → lower P&L expected,")
    print(f"   but the KEY metric is return-to-drawdown improvement)")

    # ── Save ──
    # Strip trade_details for the summary file (too large)
    save_results = {}
    for ver, r in all_results.items():
        sr = dict(r)
        sr.pop("trade_details", None)
        save_results[ver] = sr

    out_path = os.path.join(_DIR, "sizing_score_backtest.json")
    with open(out_path, "w") as f:
        json.dump(save_results, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    # Save full trade-level details separately
    details_path = os.path.join(_DIR, "sizing_score_trades.json")
    details = {}
    for ver, r in all_results.items():
        if "trade_details" in r:
            details[ver] = r["trade_details"]
    with open(details_path, "w") as f:
        json.dump(details, f, indent=2)
    print(f"  Trade details saved to {details_path}")

    return all_results


if __name__ == "__main__":
    run()
