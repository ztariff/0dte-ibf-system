#!/usr/bin/env python3
"""
Sizing Factor Research
======================
For each strategy, segment trades by market-regime factors and compare
P&L distributions across buckets. Goal: identify which factors predict
trade quality within each strategy so we can build per-trade sizing heuristics.

Factors tested:
  1. VIX level (buckets: <14, 14-17, 17-20, >20)
  2. VP ratio (buckets: <1.0, 1.0-1.3, 1.3-1.7, >1.7)
  3. Overnight gap % (negative <-0.25, flat, positive >+0.25)
  4. Day of week
  5. RV slope (RISING / STABLE / FALLING)
  6. Term structure (CONTANGO / FLAT / INVERTED)
  7. Prior day direction (UP / DOWN / FLAT)
  8. 5-day return (< -0.5%, -0.5 to 0.5%, 0.5 to 1.5%, > 1.5%)
  9. Prior day return magnitude |ret| (<0.3%, 0.3-0.7%, 0.7-1.2%, >1.2%)
 10. Wing width (40 / 45 / 50+ i.e. narrow/medium/wide)
 11. In prior week range (True / False)
 12. Entry credit as % of wing width
 13. Fire count (V3/N15 only: 1, 2, 3, 4-5)
 14. VIX9D/VIX ratio (buckets: <0.85, 0.85-0.95, 0.95-1.05, >1.05)
 15. RV level (buckets: <8, 8-12, 12-18, >18)
 16. Prior day range % (buckets: <0.3, 0.3-0.6, 0.6-1.0, >1.0)

Output: sizing_factor_research.json + printed tables
"""

import os, json, csv, math
from collections import defaultdict
import statistics

def ttest_ind_welch(a, b):
    """Welch's t-test without scipy."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0, 1.0
    ma, mb = sum(a)/na, sum(b)/nb
    va = sum((x - ma)**2 for x in a) / (na - 1)
    vb = sum((x - mb)**2 for x in b) / (nb - 1)
    se = math.sqrt(va/na + vb/nb) if (va/na + vb/nb) > 0 else 1e-9
    t_stat = (ma - mb) / se
    # Welch-Satterthwaite degrees of freedom
    num = (va/na + vb/nb)**2
    denom = (va/na)**2/(na-1) + (vb/nb)**2/(nb-1) if ((va/na)**2/(na-1) + (vb/nb)**2/(nb-1)) > 0 else 1
    df = num / denom
    # Approximate p-value using t-distribution CDF (regularized incomplete beta)
    p_val = _t_pvalue(abs(t_stat), df)
    return t_stat, p_val

def _t_pvalue(t, df):
    """Two-tailed p-value from t-distribution using approximation."""
    # Use the normal approximation for large df, exact-ish for small
    x = df / (df + t*t)
    # Regularized incomplete beta function approximation
    p = _betai(df/2.0, 0.5, x)
    return p

def _betai(a, b, x):
    """Regularized incomplete beta function I_x(a, b) via continued fraction."""
    if x < 0 or x > 1:
        return 0.0
    if x == 0 or x == 1:
        return x
    lbeta = _lgamma(a + b) - _lgamma(a) - _lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    else:
        return 1.0 - math.exp(lbeta) * _betacf(b, a, 1 - x) / b

def _betacf(a, b, x):
    """Continued fraction for incomplete beta."""
    MAXIT = 200
    EPS = 1e-10
    qab = a + b
    qap = a + 1
    qam = a - 1
    c = 1.0
    d = max(1.0 - qab * x / qap, EPS)
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = max(1.0 + aa * d, EPS)
        c = max(1.0 + aa / c, EPS)
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = max(1.0 + aa * d, EPS)
        c = max(1.0 + aa / c, EPS)
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h

def _lgamma(x):
    return math.lgamma(x)

_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load data sources ──────────────────────────────────────────────

def load_trades():
    with open(os.path.join(_DIR, "strategy_trades.json")) as f:
        return json.load(f)

def load_csv():
    """Load research CSV into dict keyed by date."""
    lookup = {}
    path = os.path.join(_DIR, "research_all_trades.csv")
    with open(path) as f:
        for row in csv.DictReader(f):
            lookup[row["date"]] = row
    return lookup

def load_gap_cache():
    with open(os.path.join(_DIR, "spx_gap_cache.json")) as f:
        return json.load(f)

def load_vix9d():
    with open(os.path.join(_DIR, "vix9d_daily.json")) as f:
        return json.load(f)

# ── Factor bucketing functions ─────────────────────────────────────

def bucket_vix(vix):
    if vix < 14: return "VIX<14"
    if vix < 17: return "VIX 14-17"
    if vix < 20: return "VIX 17-20"
    return "VIX>20"

def bucket_vp(vp):
    if vp < 1.0: return "VP<1.0"
    if vp < 1.3: return "VP 1.0-1.3"
    if vp < 1.7: return "VP 1.3-1.7"
    return "VP>1.7"

def bucket_gap(gap_pct):
    if gap_pct is None: return None
    if gap_pct < -0.25: return "Gap DOWN"
    if gap_pct > 0.25: return "Gap UP"
    return "Gap FLAT"

def bucket_5d_ret(ret):
    if ret < -0.5: return "5d<-0.5%"
    if ret < 0.5: return "5d -0.5 to 0.5%"
    if ret < 1.5: return "5d 0.5-1.5%"
    return "5d>1.5%"

def bucket_prior_day_ret(ret):
    ar = abs(ret)
    if ar < 0.3: return "|1d|<0.3%"
    if ar < 0.7: return "|1d| 0.3-0.7%"
    if ar < 1.2: return "|1d| 0.7-1.2%"
    return "|1d|>1.2%"

def bucket_wing(ww):
    if ww <= 40: return "Wing 40"
    if ww <= 50: return "Wing 45-50"
    return "Wing 55+"

def bucket_in_range(ir):
    return "In Range" if ir else "Out Range"

def bucket_credit_pct(credit, wing):
    if wing == 0: return None
    pct = credit / wing * 100
    if pct < 25: return "Credit<25%"
    if pct < 35: return "Credit 25-35%"
    if pct < 45: return "Credit 35-45%"
    return "Credit>45%"

def bucket_fire_count(fc):
    if fc <= 1: return "Fire=1"
    if fc == 2: return "Fire=2"
    if fc == 3: return "Fire=3"
    return "Fire=4-5"

def bucket_vix9d_ratio(ratio):
    if ratio is None: return None
    if ratio < 0.85: return "V9D/V<0.85"
    if ratio < 0.95: return "V9D/V 0.85-0.95"
    if ratio < 1.05: return "V9D/V 0.95-1.05"
    return "V9D/V>1.05"

def bucket_rv(rv):
    if rv < 8: return "RV<8"
    if rv < 12: return "RV 8-12"
    if rv < 18: return "RV 12-18"
    return "RV>18"

def bucket_prior_day_range(rpct):
    if rpct is None: return None
    if rpct < 0.3: return "DayRng<0.3%"
    if rpct < 0.6: return "DayRng 0.3-0.6%"
    if rpct < 1.0: return "DayRng 0.6-1.0%"
    return "DayRng>1.0%"


# ── Analysis engine ────────────────────────────────────────────────

def analyze_factor(trades_with_factor, factor_name):
    """
    Given list of (bucket_label, pnl) tuples, compute per-bucket stats.
    Returns dict of bucket -> stats.
    """
    buckets = defaultdict(list)
    for label, pnl in trades_with_factor:
        if label is not None:
            buckets[label].append(pnl)

    result = {}
    for label, pnls in sorted(buckets.items()):
        n = len(pnls)
        if n == 0:
            continue
        total = sum(pnls)
        mean = total / n
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n * 100
        if n > 1:
            std = math.sqrt(sum((p - mean)**2 for p in pnls) / (n - 1))
        else:
            std = 0
        max_loss = min(pnls) if pnls else 0
        max_win = max(pnls) if pnls else 0
        result[label] = {
            "n": n,
            "total_pnl": round(total),
            "mean_pnl": round(mean),
            "win_rate": round(wr, 1),
            "std": round(std),
            "max_loss": round(max_loss),
            "max_win": round(max_win),
            "sharpe": round(mean / std, 2) if std > 0 else 0,
        }

    # Run pairwise t-tests between buckets if >= 2 buckets with n >= 5
    viable = {k: v for k, v in buckets.items() if len(v) >= 5}
    keys = sorted(viable.keys())
    comparisons = []
    if len(keys) >= 2:
        # Compare best vs worst by mean
        means = {k: sum(viable[k])/len(viable[k]) for k in keys}
        best_k = max(means, key=means.get)
        worst_k = min(means, key=means.get)
        if best_k != worst_k:
            t_stat, p_val = ttest_ind_welch(viable[best_k], viable[worst_k])
            comparisons.append({
                "best": best_k,
                "worst": worst_k,
                "t_stat": round(t_stat, 3),
                "p_value": round(p_val, 4),
                "significant_10": p_val < 0.10,
                "significant_05": p_val < 0.05,
            })

    return {"buckets": result, "comparisons": comparisons}


def enrich_trade(trade, csv_lookup, gap_cache, vix9d_cache):
    """Add CSV-sourced fields to trade dict."""
    dt = trade["date"]
    csv_row = csv_lookup.get(dt, {})
    enriched = dict(trade)

    # From CSV
    enriched["rv_slope"] = csv_row.get("rv_slope", "")
    enriched["ts_label"] = csv_row.get("ts_label", "")
    enriched["prior_dir"] = trade.get("prior_dir", csv_row.get("prior_day_direction", ""))
    enriched["dow"] = csv_row.get("dow", "")
    enriched["prior_day_range"] = float(csv_row["prior_day_range"]) if csv_row.get("prior_day_range") else None
    enriched["in_prior_week_range"] = bool(int(csv_row["in_prior_week_range"])) if csv_row.get("in_prior_week_range") else None
    enriched["prior_2d_return"] = float(csv_row["prior_2d_return"]) if csv_row.get("prior_2d_return") else None
    enriched["rv_1d_change"] = float(csv_row["rv_1d_change"]) if csv_row.get("rv_1d_change") else None
    enriched["prior_day_body_pct"] = float(csv_row["prior_day_body_pct"]) if csv_row.get("prior_day_body_pct") else None

    # From gap cache
    gap = gap_cache.get(dt)
    enriched["gap_pct"] = gap

    # VIX9D/VIX ratio
    vix9d = vix9d_cache.get(dt)
    vix = trade.get("vix")
    if vix9d and vix and vix > 0:
        enriched["vix9d_ratio"] = vix9d / vix
    else:
        enriched["vix9d_ratio"] = None

    return enriched


def run_analysis():
    trades = load_trades()
    csv_lookup = load_csv()
    gap_cache = load_gap_cache()
    vix9d_cache = load_vix9d()

    # Filter out removed strategies
    surviving = {"v3", "n15", "v6", "v7", "v9", "v12", "n17", "n18"}
    trades = [t for t in trades if t["ver"] in surviving]

    # Enrich
    enriched = [enrich_trade(t, csv_lookup, gap_cache, vix9d_cache) for t in trades]

    # Define factor extractors
    factors = {
        "VIX Level": lambda t: bucket_vix(t["vix"]) if t.get("vix") else None,
        "VP Ratio": lambda t: bucket_vp(t["vp_ratio"]) if t.get("vp_ratio") else None,
        "Gap Direction": lambda t: bucket_gap(t.get("gap_pct")),
        "Day of Week": lambda t: t.get("dow") or None,
        "RV Slope": lambda t: t.get("rv_slope") or None,
        "Term Structure": lambda t: t.get("ts_label") or None,
        "Prior Day Dir": lambda t: t.get("prior_dir") or None,
        "5d Return": lambda t: bucket_5d_ret(t["prior_5d"]) if t.get("prior_5d") is not None else None,
        "Prior Day |Ret|": lambda t: bucket_prior_day_ret(t["prior_1d"]) if t.get("prior_1d") is not None else None,
        "Wing Width": lambda t: bucket_wing(t["wing_width"]) if t.get("wing_width") else None,
        "In Prior Wk Range": lambda t: bucket_in_range(t["in_prior_week_range"]) if t.get("in_prior_week_range") is not None else None,
        "Credit/Wing %": lambda t: bucket_credit_pct(t["entry_credit"], t["wing_width"]) if t.get("entry_credit") and t.get("wing_width") else None,
        "RV Level": lambda t: bucket_rv(t["rv"]) if t.get("rv") else None,
        "Prior Day Range": lambda t: bucket_prior_day_range(t.get("prior_day_range")),
        "VIX9D/VIX Ratio": lambda t: bucket_vix9d_ratio(t.get("vix9d_ratio")),
    }
    # Fire count only for V3/N15
    fire_count_factor = lambda t: bucket_fire_count(t["fire_count"]) if t.get("fire_count") else None

    # ── Per-strategy analysis ──
    strat_order = ["v3", "n15", "v6", "v7", "v9", "v12", "n17", "n18"]
    ver_names = {
        "v3": "PHOENIX", "n15": "PHOENIX CLEAR", "v6": "QUIET REBOUND",
        "v7": "FLAT-GAP FADE", "v9": "BREAKOUT STALL", "v12": "BULL SQUEEZE",
        "n17": "AFTERNOON LOCK", "n18": "LATE SQUEEZE"
    }

    all_results = {}

    for ver in strat_order:
        strat_trades = [t for t in enriched if t["ver"] == ver]
        n = len(strat_trades)
        total_pnl = sum(t["pnl"] for t in strat_trades)
        name = ver_names[ver]

        print(f"\n{'='*80}")
        print(f"  {name} ({ver})  |  n={n}  |  Total P&L: ${total_pnl:,.0f}")
        print(f"{'='*80}")

        strat_results = {"name": name, "n": n, "total_pnl": total_pnl, "factors": {}}

        # Run each factor
        active_factors = dict(factors)
        if ver in ("v3", "n15"):
            active_factors["Fire Count"] = fire_count_factor

        for factor_name, extractor in active_factors.items():
            pairs = [(extractor(t), t["pnl"]) for t in strat_trades]
            # Filter out None buckets
            pairs = [(b, p) for b, p in pairs if b is not None]
            if not pairs:
                continue

            result = analyze_factor(pairs, factor_name)

            # Only print if there are meaningful differences
            buckets = result["buckets"]
            if len(buckets) < 2:
                continue

            means = [v["mean_pnl"] for v in buckets.values()]
            if max(means) - min(means) < 100 and n > 20:
                # Skip factors with trivial spread for large-n strategies
                strat_results["factors"][factor_name] = result
                continue

            strat_results["factors"][factor_name] = result

            # Print table
            print(f"\n  ── {factor_name} ──")
            print(f"  {'Bucket':<22} {'N':>4} {'WinR':>6} {'Mean$':>8} {'Total$':>10} {'Sharpe':>7} {'MaxLoss':>9}")
            print(f"  {'-'*22} {'----':>4} {'------':>6} {'--------':>8} {'----------':>10} {'-------':>7} {'---------':>9}")
            for label, s in sorted(buckets.items(), key=lambda x: -x[1]["mean_pnl"]):
                print(f"  {label:<22} {s['n']:>4} {s['win_rate']:>5.1f}% {s['mean_pnl']:>+8,} {s['total_pnl']:>+10,} {s['sharpe']:>+7.2f} {s['max_loss']:>+9,}")

            if result["comparisons"]:
                c = result["comparisons"][0]
                sig = "**p<0.05**" if c["significant_05"] else ("*p<0.10*" if c["significant_10"] else "not sig")
                print(f"  ↳ Best vs Worst: {c['best']} vs {c['worst']}  t={c['t_stat']:.2f}  p={c['p_value']:.4f}  {sig}")

        all_results[ver] = strat_results

    # ── Cross-strategy factor importance summary ──
    print(f"\n\n{'='*80}")
    print("  FACTOR SIGNIFICANCE SUMMARY (p<0.10)")
    print(f"{'='*80}")
    print(f"  {'Strategy':<20} {'Factor':<22} {'Best Bucket':<22} {'vs Worst':<22} {'p-value':>8}")
    print(f"  {'-'*20} {'-'*22} {'-'*22} {'-'*22} {'-'*8}")

    significant_findings = []
    for ver in strat_order:
        sr = all_results[ver]
        for fname, fdata in sr["factors"].items():
            for c in fdata.get("comparisons", []):
                if c["significant_10"]:
                    print(f"  {ver_names[ver]:<20} {fname:<22} {c['best']:<22} {c['worst']:<22} {c['p_value']:>8.4f}")
                    significant_findings.append({
                        "strategy": ver, "name": ver_names[ver],
                        "factor": fname, "best": c["best"], "worst": c["worst"],
                        "p_value": c["p_value"]
                    })

    if not significant_findings:
        print("  (no factors reached p<0.10 significance)")

    # ── Save full results ──
    out_path = os.path.join(_DIR, "sizing_factor_research.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n\nFull results saved to {out_path}")
    print(f"Significant findings: {len(significant_findings)}")

    # ── Also save the significant findings separately ──
    sig_path = os.path.join(_DIR, "sizing_significant_factors.json")
    with open(sig_path, "w") as f:
        json.dump(significant_findings, f, indent=2)
    print(f"Significant factors saved to {sig_path}")

    return all_results, significant_findings


if __name__ == "__main__":
    run_analysis()
