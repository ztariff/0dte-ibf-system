"""
Per-trade sizing score module.
Each strategy gets a composite score based on market-regime factors.
Score maps to a sizing multiplier (25%, 50%, 75%, 100% of max budget).

Factors derived from 776-day backtest factor analysis (sizing_factor_research.py).
In-sample — must be validated forward.
"""

# ── Per-strategy scoring functions ─────────────────────────────────
# Each takes a dict with keys: prior_dir, prior_1d, fire_count, rv, dow,
# rv_slope, ts_label, vp_ratio, gap_pct, in_prior_week_range, prior_day_range
# Returns integer score.

def score_v3(t):
    """PHOENIX — Prior Day Dir (p=.014), |Ret| (p=.022), Fire Count (p=.022),
    RV level (p=.065), Day of Week (p=.061)."""
    s = 0
    pd = t.get("prior_dir", "")
    if pd == "DOWN":     s += 3
    elif pd == "UP":     s -= 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:     s += 2
        elif ar < 0.7:   s -= 2

    fc = t.get("fire_count", 0) or 0
    if fc >= 4:    s += 3
    elif fc == 3:  s += 2
    elif fc == 2:  s -= 1

    rv = t.get("rv", 0) or 0
    if rv > 18:    s += 2
    elif rv < 12:  s -= 1

    dow = t.get("dow", "")
    if dow == "Wednesday":  s += 1
    elif dow == "Tuesday":  s -= 2
    return s


def score_n15(t):
    """PHOENIX CLEAR — Prior Day Dir (p=.014), Day of Week (p=.034),
    RV Slope (marginal)."""
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
    """QUIET REBOUND — VP ratio (directional), |Ret| (p=.062),
    RV Slope (p=.067)."""
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
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
    """BREAKOUT STALL — VP 1.3-1.7 (p=.076), |Ret| 0.7-1.2% (p=.039),
    RV Slope (p=.087), Day of Week (directional)."""
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
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
    """BULL SQUEEZE — VP sweet spot (directional), 5dRet (directional),
    |Ret| (directional). n=11, low confidence."""
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:   s += 1
    elif 1.0 <= vp < 1.3:  s -= 2

    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5: s += 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:     s += 1
        elif ar < 0.7:   s -= 2
    return s


def score_n17(t):
    """AFTERNOON LOCK — RV Slope (p=.008), Day of Week (p=.030),
    Term Structure (p=.049), Gap (p=.071), Prior Wk Range (p=.069)."""
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
        if not iwr:  s += 1
        else:        s -= 1
    return s


def score_n18(t):
    """LATE SQUEEZE — Day of Week (p=.014), Prior Day Dir (directional),
    Gap (directional), Prior Day Range (directional)."""
    s = 0
    dow = t.get("dow", "")
    if dow == "Tuesday":     s += 2
    elif dow == "Wednesday": s += 1
    elif dow == "Monday":    s -= 2

    pd = t.get("prior_dir", "")
    if pd == "UP":     s += 1
    elif pd == "DOWN": s -= 1

    gap = t.get("gap_pct")
    if gap is not None:
        if gap > 0.25:    s += 1
        elif gap < -0.25: s -= 1

    pdr = t.get("prior_day_range")
    if pdr is not None and pdr > 1.0:
        s += 1
    return s


# ── Score function registry ────────────────────────────────────────

SCORE_FUNCTIONS = {
    "v3":  score_v3,
    "n15": score_n15,
    "v6":  score_v6,
    "v7":  None,        # n=5, no reliable factors
    "v9":  score_v9,
    "v12": score_v12,
    "n17": score_n17,
    "n18": score_n18,
}

# ── Per-strategy score thresholds ──────────────────────────────────
# Derived from quartile analysis of historical score distributions.
# (t25, t50, t75) — scores at or below t25 get 25% sizing, etc.
# These are calibrated from the 776-day backtest.

SCORE_THRESHOLDS = {
    "v3":  (-2, 0, 3),   # range [-5, 11]
    "n15": (-2, -1, 2),  # range [-6, 5]
    "v6":  (-3, 0, 2),   # range [-3, 6]
    "v9":  (-3, -1, 3),  # range [-6, 7]
    "v12": (-3, 1, 2),   # range [-4, 3]
    "n17": (-2, 0, 3),   # range [-8, 6]
    "n18": (0, 1, 2),    # range [-4, 4]
}


def score_to_multiplier(score, ver):
    """Map composite score to sizing multiplier (0.25, 0.50, 0.75, or 1.00)."""
    thresholds = SCORE_THRESHOLDS.get(ver)
    if thresholds is None:
        return 1.0   # no scoring for this strategy
    t25, t50, t75 = thresholds
    if score <= t25:  return 0.25
    if score <= t50:  return 0.50
    if score <= t75:  return 0.75
    return 1.00


def compute_sizing(ver, context):
    """
    Main entry point: given a strategy version and trade context dict,
    return (multiplier, score).

    context keys used (provide what's available, missing is OK):
      prior_dir, prior_1d, fire_count, rv, dow, rv_slope, ts_label,
      vp_ratio, gap_pct, in_prior_week_range, prior_day_range, prior_5d
    """
    fn = SCORE_FUNCTIONS.get(ver)
    if fn is None:
        return 1.0, 0
    score = fn(context)
    mult = score_to_multiplier(score, ver)
    return mult, score
