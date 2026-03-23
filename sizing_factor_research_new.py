#!/usr/bin/env python3
"""
Sizing Factor Research — NEW STRATEGIES
========================================
Same methodology as sizing_factor_research.py but applied to the 10 new
strategies from calendar_trades.json. Trades are enriched with regime data
from research_all_trades.csv + spx_gap_cache.json + vix9d_daily.json.
"""

import os, json, csv, math
from collections import defaultdict
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))


def ttest_ind_welch(a, b):
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return 0.0, 1.0
    ma, mb = sum(a)/na, sum(b)/nb
    va = sum((x - ma)**2 for x in a) / (na - 1)
    vb = sum((x - mb)**2 for x in b) / (nb - 1)
    se = math.sqrt(va/na + vb/nb) if (va/na + vb/nb) > 0 else 1e-9
    t_stat = (ma - mb) / se
    num = (va/na + vb/nb)**2
    denom = (va/na)**2/(na-1) + (vb/nb)**2/(nb-1) if ((va/na)**2/(na-1) + (vb/nb)**2/(nb-1)) > 0 else 1
    df = num / denom
    p_val = _t_pvalue(abs(t_stat), df)
    return t_stat, p_val

def _t_pvalue(t, df):
    x = df / (df + t*t)
    return _betai(df/2.0, 0.5, x)

def _betai(a, b, x):
    if x < 0 or x > 1: return 0.0
    if x == 0 or x == 1: return x
    lbeta = math.lgamma(a+b) - math.lgamma(a) - math.lgamma(b) + a*math.log(x) + b*math.log(1-x)
    if x < (a+1)/(a+b+2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    else:
        return 1.0 - math.exp(lbeta) * _betacf(b, a, 1-x) / b

def _betacf(a, b, x):
    MAXIT, EPS = 200, 1e-10
    qab, qap, qam = a+b, a+1, a-1
    c, d = 1.0, max(1.0 - qab*x/qap, EPS)
    d = 1.0/d; h = d
    for m in range(1, MAXIT+1):
        m2 = 2*m
        aa = m*(b-m)*x / ((qam+m2)*(a+m2))
        d = max(1.0+aa*d, EPS); c = max(1.0+aa/c, EPS); d = 1.0/d; h *= d*c
        aa = -(a+m)*(qab+m)*x / ((a+m2)*(qap+m2))
        d = max(1.0+aa*d, EPS); c = max(1.0+aa/c, EPS); d = 1.0/d
        delta = d*c; h *= delta
        if abs(delta-1.0) < EPS: break
    return h


# ── Load data ──────────────────────────────────────────────────────

def load_new_trades():
    with open(os.path.join(_DIR, "calendar_trades.json")) as f:
        return json.load(f)["trades"]

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


# ── Factor bucketing ───────────────────────────────────────────────

def bucket_vix(vix):
    if vix is None: return None
    if vix < 14: return "VIX<14"
    if vix < 17: return "VIX 14-17"
    if vix < 20: return "VIX 17-20"
    return "VIX>20"

def bucket_vp(vp):
    if vp is None: return None
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
    if ret is None: return None
    if ret < -0.5: return "5d<-0.5%"
    if ret < 0.5: return "5d -0.5 to 0.5%"
    if ret < 1.5: return "5d 0.5-1.5%"
    return "5d>1.5%"

def bucket_prior_day_ret(ret):
    if ret is None: return None
    ar = abs(ret)
    if ar < 0.3: return "|1d|<0.3%"
    if ar < 0.7: return "|1d| 0.3-0.7%"
    if ar < 1.2: return "|1d| 0.7-1.2%"
    return "|1d|>1.2%"

def bucket_in_range(ir):
    if ir is None: return None
    return "In Range" if ir else "Out Range"

def bucket_credit_pct(credit, wing):
    if wing is None or wing == 0 or credit is None: return None
    pct = credit / wing * 100
    if pct < 25: return "Credit<25%"
    if pct < 35: return "Credit 25-35%"
    if pct < 45: return "Credit 35-45%"
    return "Credit>45%"

def bucket_rv(rv):
    if rv is None: return None
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

def bucket_vix9d_ratio(ratio):
    if ratio is None: return None
    if ratio < 0.85: return "V9D/V<0.85"
    if ratio < 0.95: return "V9D/V 0.85-0.95"
    if ratio < 1.05: return "V9D/V 0.95-1.05"
    return "V9D/V>1.05"

def bucket_hold_minutes(mins):
    if mins is None: return None
    if mins < 15: return "Hold<15m"
    if mins < 30: return "Hold 15-30m"
    if mins < 60: return "Hold 30-60m"
    return "Hold>60m"


# ── Analysis engine ────────────────────────────────────────────────

def analyze_factor(pairs, factor_name):
    buckets = defaultdict(list)
    for label, pnl in pairs:
        if label is not None:
            buckets[label].append(pnl)

    result = {}
    for label, pnls in sorted(buckets.items()):
        n = len(pnls)
        if n == 0: continue
        total = sum(pnls)
        mean = total / n
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / n * 100
        std = math.sqrt(sum((p - mean)**2 for p in pnls) / (n - 1)) if n > 1 else 0
        result[label] = {
            "n": n, "total_pnl": round(total), "mean_pnl": round(mean),
            "win_rate": round(wr, 1), "std": round(std),
            "max_loss": round(min(pnls)), "max_win": round(max(pnls)),
            "sharpe": round(mean / std, 2) if std > 0 else 0,
        }

    viable = {k: v for k, v in buckets.items() if len(v) >= 5}
    keys = sorted(viable.keys())
    comparisons = []
    if len(keys) >= 2:
        means = {k: sum(viable[k])/len(viable[k]) for k in keys}
        best_k = max(means, key=means.get)
        worst_k = min(means, key=means.get)
        if best_k != worst_k:
            t_stat, p_val = ttest_ind_welch(viable[best_k], viable[worst_k])
            comparisons.append({
                "best": best_k, "worst": worst_k,
                "t_stat": round(t_stat, 3), "p_value": round(p_val, 4),
                "significant_10": p_val < 0.10, "significant_05": p_val < 0.05,
            })

    return {"buckets": result, "comparisons": comparisons}


def enrich_trade(trade, csv_lookup, gap_cache, vix9d_cache):
    dt = trade["date"]
    csv_row = csv_lookup.get(dt, {})
    t = dict(trade)

    # From CSV
    t["rv_slope"] = csv_row.get("rv_slope", "")
    t["ts_label"] = csv_row.get("ts_label", "")
    t["prior_dir"] = csv_row.get("prior_day_direction", "")
    t["dow"] = csv_row.get("dow", "")
    t["prior_day_return"] = float(csv_row["prior_day_return"]) if csv_row.get("prior_day_return") else None
    t["prior_5d_return"] = float(csv_row["prior_5d_return"]) if csv_row.get("prior_5d_return") else None
    t["prior_day_range"] = float(csv_row["prior_day_range"]) if csv_row.get("prior_day_range") else None
    t["rv"] = float(csv_row["rv"]) if csv_row.get("rv") else None
    t["vp_ratio"] = float(csv_row["vp_ratio"]) if csv_row.get("vp_ratio") else None
    t["in_prior_week_range"] = bool(int(csv_row["in_prior_week_range"])) if csv_row.get("in_prior_week_range") else None

    # If no CSV match, try to get DOW from date
    if not t["dow"]:
        try:
            t["dow"] = ["Monday","Tuesday","Wednesday","Thursday","Friday"][
                datetime.strptime(dt, "%Y-%m-%d").weekday()]
        except: pass

    # Gap
    gap = gap_cache.get(dt)
    t["gap_pct"] = gap

    # VIX — use CSV if available, else trade's own vix field
    if csv_row.get("vix"):
        t["vix_val"] = float(csv_row["vix"])
    else:
        t["vix_val"] = trade.get("vix")

    # VIX9D ratio
    vix9d = vix9d_cache.get(dt)
    vix = t["vix_val"]
    t["vix9d_ratio"] = (vix9d / vix) if (vix9d and vix and vix > 0) else None

    # Wing width from structure string (e.g. "IBF_4130_75w" → 75)
    ww = None
    struct = trade.get("structure", "")
    if "_" in struct:
        parts = struct.split("_")
        for p in parts:
            if p.endswith("w"):
                try: ww = int(p[:-1])
                except: pass
    t["wing_width"] = ww

    return t


def run_analysis():
    trades = load_new_trades()
    csv_lookup = load_csv()
    gap_cache = load_gap_cache()
    vix9d_cache = load_vix9d()

    enriched = [enrich_trade(t, csv_lookup, gap_cache, vix9d_cache) for t in trades]

    # Check enrichment coverage
    has_csv = sum(1 for t in enriched if t.get("rv_slope"))
    print(f"Trades enriched with CSV data: {has_csv}/{len(enriched)} ({has_csv/len(enriched)*100:.0f}%)")

    factors = {
        "VIX Level": lambda t: bucket_vix(t.get("vix_val")),
        "VP Ratio": lambda t: bucket_vp(t.get("vp_ratio")),
        "Gap Direction": lambda t: bucket_gap(t.get("gap_pct")),
        "Day of Week": lambda t: t.get("dow") or None,
        "RV Slope": lambda t: t.get("rv_slope") or None,
        "Term Structure": lambda t: t.get("ts_label") or None,
        "Prior Day Dir": lambda t: t.get("prior_dir") or None,
        "5d Return": lambda t: bucket_5d_ret(t.get("prior_5d_return")),
        "Prior Day |Ret|": lambda t: bucket_prior_day_ret(t.get("prior_day_return")),
        "In Prior Wk Range": lambda t: bucket_in_range(t.get("in_prior_week_range")),
        "Credit/Wing %": lambda t: bucket_credit_pct(t.get("entry_credit"), t.get("wing_width")),
        "RV Level": lambda t: bucket_rv(t.get("rv")),
        "Prior Day Range": lambda t: bucket_prior_day_range(t.get("prior_day_range")),
        "VIX9D/VIX Ratio": lambda t: bucket_vix9d_ratio(t.get("vix9d_ratio")),
    }

    # Get ordered strategy list
    strat_names = sorted(set(t["strategy"] for t in enriched))

    all_results = {}
    significant_findings = []

    for strat_name in strat_names:
        strat_trades = [t for t in enriched if t["strategy"] == strat_name]
        n = len(strat_trades)
        total_pnl = sum(t["pnl_dollar_sized"] for t in strat_trades)

        print(f"\n{'='*80}")
        print(f"  {strat_name}  |  n={n}  |  Total P&L: ${total_pnl:,.0f}")
        print(f"{'='*80}")

        strat_results = {"name": strat_name, "n": n, "total_pnl": total_pnl, "factors": {}}

        for factor_name, extractor in factors.items():
            pairs = [(extractor(t), t["pnl_dollar_sized"]) for t in strat_trades]
            pairs = [(b, p) for b, p in pairs if b is not None]
            if not pairs: continue

            result = analyze_factor(pairs, factor_name)
            buckets = result["buckets"]
            if len(buckets) < 2: continue

            strat_results["factors"][factor_name] = result

            means = [v["mean_pnl"] for v in buckets.values()]
            if max(means) - min(means) < 200 and n > 50:
                continue  # skip trivial spread for high-n strategies

            print(f"\n  ── {factor_name} ──")
            print(f"  {'Bucket':<22} {'N':>4} {'WinR':>6} {'Mean$':>8} {'Total$':>10} {'Sharpe':>7} {'MaxLoss':>9}")
            print(f"  {'-'*22} {'----':>4} {'------':>6} {'--------':>8} {'----------':>10} {'-------':>7} {'---------':>9}")
            for label, s in sorted(buckets.items(), key=lambda x: -x[1]["mean_pnl"]):
                print(f"  {label:<22} {s['n']:>4} {s['win_rate']:>5.1f}% {s['mean_pnl']:>+8,} {s['total_pnl']:>+10,} {s['sharpe']:>+7.2f} {s['max_loss']:>+9,}")

            if result["comparisons"]:
                c = result["comparisons"][0]
                sig = "**p<0.05**" if c["significant_05"] else ("*p<0.10*" if c["significant_10"] else "not sig")
                print(f"  ↳ Best vs Worst: {c['best']} vs {c['worst']}  t={c['t_stat']:.2f}  p={c['p_value']:.4f}  {sig}")

        all_results[strat_name] = strat_results

        # Collect significant findings
        for fname, fdata in strat_results["factors"].items():
            for c in fdata.get("comparisons", []):
                if c["significant_10"]:
                    significant_findings.append({
                        "strategy": strat_name, "factor": fname,
                        "best": c["best"], "worst": c["worst"],
                        "p_value": c["p_value"],
                    })

    # ── Summary ──
    print(f"\n\n{'='*80}")
    print(f"  FACTOR SIGNIFICANCE SUMMARY (p<0.10)")
    print(f"{'='*80}")
    print(f"  {'Strategy':<30} {'Factor':<22} {'Best Bucket':<22} {'vs Worst':<22} {'p-val':>7}")
    print(f"  {'-'*30} {'-'*22} {'-'*22} {'-'*22} {'-'*7}")
    for f in sorted(significant_findings, key=lambda x: x["p_value"]):
        print(f"  {f['strategy']:<30} {f['factor']:<22} {f['best']:<22} {f['worst']:<22} {f['p_value']:>7.4f}")

    print(f"\n  Total significant findings: {len(significant_findings)}")

    out_path = os.path.join(_DIR, "sizing_factor_research_new.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"  Results saved to {out_path}")

    sig_path = os.path.join(_DIR, "sizing_significant_factors_new.json")
    with open(sig_path, "w") as f:
        json.dump(significant_findings, f, indent=2)
    print(f"  Significant factors saved to {sig_path}")

    return all_results, significant_findings


if __name__ == "__main__":
    run_analysis()
